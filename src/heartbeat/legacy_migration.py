"""Standalone runtime integrity and legacy Heartbeat discovery helpers.

The GUI installs unpacked runtime directories itself.  This module keeps the
runtime side small and deterministic: it can describe an artifact, verify its
contents before activation, atomically switch a stable launcher, and inspect
old Heartbeat data without modifying it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from heartbeat import __version__
from heartbeat.agent_contract import API_VERSION, ROLES

MANIFEST_NAME = "runtime-manifest.json"


class RuntimeIntegrityError(ValueError):
    """An unpacked runtime is incomplete, changed, or unsafe to activate."""


def runtime_target(system: str | None = None, machine: str | None = None) -> str:
    """Return the published target name for a supported host."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "darwin" and machine in {"arm64", "x86_64"}:
        return "macos-universal"
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system in {"windows", "win32"} and machine in {"x86_64", "amd64"}:
        return "windows-x86_64"
    return f"unsupported-{system}-{machine}"


def runtime_executable_name(target: str | None = None) -> str:
    """The one-folder executable name for a target."""
    return "heartbeat.exe" if (target or runtime_target()).startswith("windows-") else "heartbeat"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_files(runtime_dir: Path) -> list[Path]:
    return sorted(
        path for path in runtime_dir.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    )


def runtime_manifest(runtime_dir: Path, *, target: str | None = None) -> dict[str, Any]:
    """Build the manifest for one unpacked one-folder runtime directory."""
    root = runtime_dir.resolve()
    executable = root / runtime_executable_name(target)
    if not root.is_dir() or not executable.is_file():
        raise RuntimeIntegrityError("runtime directory does not contain its heartbeat executable")
    files = [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path), "size": path.stat().st_size}
        for path in _artifact_files(root)
    ]
    return {
        "schemaVersion": 1,
        "target": target or runtime_target(),
        "runtimeVersion": __version__,
        "apiMajor": int(API_VERSION),
        "files": files,
    }


def write_runtime_manifest(runtime_dir: Path, *, target: str | None = None) -> Path:
    """Write an artifact manifest atomically after the bundle has been built."""
    root = runtime_dir.resolve()
    manifest_path = root / MANIFEST_NAME
    payload = json.dumps(runtime_manifest(root, target=target), ensure_ascii=False, indent=2) + "\n"
    _atomic_write(manifest_path, payload.encode("utf-8"))
    return manifest_path


def verify_runtime_manifest(runtime_dir: Path) -> dict[str, Any]:
    """Verify every listed artifact file before an installer changes a launcher."""
    root = runtime_dir.resolve()
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeIntegrityError("runtime manifest cannot be read") from error
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise RuntimeIntegrityError("runtime manifest schema is unsupported")
    if not isinstance(manifest.get("target"), str) or not isinstance(manifest.get("runtimeVersion"), str):
        raise RuntimeIntegrityError("runtime manifest is missing target or version")
    if manifest.get("apiMajor") != int(API_VERSION):
        raise RuntimeIntegrityError("runtime API major is incompatible")
    expected = manifest.get("files")
    if not isinstance(expected, list) or not expected:
        raise RuntimeIntegrityError("runtime manifest has no files")
    paths: set[str] = set()
    for entry in expected:
        if not isinstance(entry, dict):
            raise RuntimeIntegrityError("runtime manifest file entry is invalid")
        relative = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(relative, str) or not isinstance(digest, str) or not isinstance(size, int):
            raise RuntimeIntegrityError("runtime manifest file entry is incomplete")
        candidate = (root / relative).resolve()
        if root not in candidate.parents or candidate == root or relative in paths:
            raise RuntimeIntegrityError("runtime manifest contains an unsafe path")
        paths.add(relative)
        if not candidate.is_file() or candidate.stat().st_size != size or _sha256(candidate) != digest:
            raise RuntimeIntegrityError(f"runtime file verification failed: {relative}")
    actual = {path.relative_to(root).as_posix() for path in _artifact_files(root)}
    if paths != actual:
        raise RuntimeIntegrityError("runtime directory and manifest file lists differ")
    executable = runtime_executable_name(manifest["target"])
    if executable not in paths:
        raise RuntimeIntegrityError("runtime manifest does not include the heartbeat executable")
    return manifest


def _atomic_write(destination: Path, content: bytes, *, mode: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def activate_stable_launcher(install_root: Path, version_dir: Path) -> Path:
    """Verify an installed version then atomically make the stable launcher target it.

    ``version_dir`` must already be below ``install_root/versions``.  The check
    happens before the replacement, so a corrupt update cannot break the prior
    executable a service is running.
    """
    root = install_root.resolve()
    candidate = version_dir.resolve()
    versions = root / "versions"
    if candidate.parent != versions or not candidate.name:
        raise RuntimeIntegrityError("version directory must be a direct child of install_root/versions")
    manifest = verify_runtime_manifest(candidate)
    executable = runtime_executable_name(manifest["target"])
    if manifest["target"].startswith("windows-"):
        launcher = root / "bin" / "heartbeat.cmd"
        body = f'@echo off\r\n"%~dp0..\\versions\\{candidate.name}\\{executable}" %*\r\n'
        _atomic_write(launcher, body.encode("utf-8"))
    else:
        launcher = root / "bin" / "heartbeat"
        body = f'#!/bin/sh\nexec "$(dirname "$0")/../versions/{candidate.name}/{executable}" "$@"\n'
        _atomic_write(launcher, body.encode("utf-8"), mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    return launcher


def _parse_legacy_jobs(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    jobs: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", raw)
        field = re.match(r"^\s*-\s*([A-Za-z_]+):\s*(.*?)\s*$", raw)
        if heading:
            current = {"name": heading.group(1), "source": str(path)}
            jobs.append(current)
        elif field and current is not None:
            current[field.group(1)] = field.group(2)
    return jobs


def preview_legacy_migration(home: Path | None = None) -> dict[str, Any]:
    """Return a read-only migration preview for legacy jobs and execution history."""
    base = (home or Path.home()) / ".claude"
    heartbeat_file = base / "HEARTBEAT.md"
    jobs_dir = base / "heartbeat" / "jobs.d"
    state_file = base / "heartbeat" / "state.json"
    jobs = _parse_legacy_jobs(heartbeat_file)
    for job_file in sorted(jobs_dir.glob("*.md")) if jobs_dir.is_dir() else []:
        jobs.extend(_parse_legacy_jobs(job_file))

    roles: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, str]] = []
    unmigratable: list[dict[str, str]] = []
    for job in jobs:
        name = job["name"]
        prompt = job.get("prompt", "")
        lower = f"{name} {prompt}".lower()
        if "dream" in lower:
            excluded.append({"name": name, "reason": "dream jobs remain outside the agent runtime"})
            continue
        role = job.get("role", "").lower()
        if role not in ROLES:
            role = next((candidate for candidate in ROLES if candidate in lower), "")
        if not role:
            unmigratable.append({"name": name, "reason": "no workflow role can be inferred"})
            continue
        provider = job.get("provider", "claude").lower()
        if provider not in {"claude", "codex"}:
            unmigratable.append({"name": name, "reason": f"unsupported provider: {provider}"})
            continue
        roles[role] = {
            "provider": provider,
            "model": job.get("model") or None,
            "interval": job.get("interval") or None,
            "timeout": job.get("timeout") or None,
            "maxPer": job.get("max_per") or None,
            "source": job["source"],
        }

    history: dict[str, Any] = {}
    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            history = {
                name: {key: value for key, value in value.items() if key in {"last_run", "last_result", "recent_runs"}}
                for name, value in loaded.items()
                if isinstance(value, dict) and not name.startswith("_")
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "legacyDetected": bool(jobs or state_file.exists()),
        "roleJobs": roles,
        "executionHistory": history,
        "excluded": excluded,
        "unmigratable": unmigratable,
        "writeRequired": False,
    }
