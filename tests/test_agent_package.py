"""Standalone runtime package and legacy migration safeguards."""

from __future__ import annotations

from pathlib import Path

import pytest

from heartbeat.legacy_migration import (
    MANIFEST_NAME,
    RuntimeIntegrityError,
    activate_stable_launcher,
    runtime_target,
    verify_runtime_manifest,
    write_runtime_manifest,
)


def _runtime(version_dir, *, executable="heartbeat"):
    version_dir.mkdir(parents=True)
    executable_path = version_dir / executable
    executable_path.write_bytes(b"standalone-heartbeat")
    executable_path.chmod(0o755)
    (version_dir / "_internal").mkdir()
    (version_dir / "_internal" / "runtime.bin").write_bytes(b"runtime")
    write_runtime_manifest(version_dir, target="linux-x86_64")
    return version_dir


def test_manifest_covers_every_runtime_file_and_detects_tampering(tmp_path):
    runtime_dir = _runtime(tmp_path / "runtime")

    manifest = verify_runtime_manifest(runtime_dir)

    assert manifest["target"] == "linux-x86_64"
    assert {entry["path"] for entry in manifest["files"]} == {"heartbeat", "_internal/runtime.bin"}
    (runtime_dir / "_internal" / "runtime.bin").write_bytes(b"changed")
    with pytest.raises(RuntimeIntegrityError, match="verification failed"):
        verify_runtime_manifest(runtime_dir)


def test_failed_candidate_never_replaces_the_existing_stable_launcher(tmp_path):
    install_root = tmp_path / "install"
    first = _runtime(install_root / "versions" / "0.8.0")
    launcher = activate_stable_launcher(install_root, first)
    original = launcher.read_text(encoding="utf-8")

    candidate = _runtime(install_root / "versions" / "0.8.1")
    (candidate / "heartbeat").write_bytes(b"tampered")
    with pytest.raises(RuntimeIntegrityError):
        activate_stable_launcher(install_root, candidate)

    assert launcher.read_text(encoding="utf-8") == original
    assert "0.8.0" in original


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "macos-universal"),
        ("Darwin", "x86_64", "macos-universal"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Windows", "AMD64", "windows-x86_64"),
    ],
)
def test_published_runtime_targets_are_stable(system, machine, expected):
    assert runtime_target(system, machine) == expected


def test_package_spec_excludes_provider_sdks_and_manifest_is_not_self_hashed():
    repository = Path(__file__).resolve().parents[1]
    spec = (repository / "packaging" / "heartbeat.spec").read_text(encoding="utf-8")
    manifest = (repository / "pyproject.toml").read_text(encoding="utf-8")

    assert '"claude_agent_sdk"' in spec and '"codex"' in spec
    assert "pyinstaller" in manifest.lower()
    assert MANIFEST_NAME == "runtime-manifest.json"
