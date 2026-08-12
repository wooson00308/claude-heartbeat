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


# --- 기기 상태 조회: 설치 버전·실행 중 버전·서비스 상태를 한 응답으로 ---

DEVICE_QUERY_FIELDS = {
    "schemaVersion", "result", "checkedAt", "runtimeVersion", "installedVersion",
    "runningVersion", "apiMajor", "target", "executable", "installRoot", "launcher",
    "installResult", "recoverable", "service", "evidence",
}


def _installed(tmp_path, version="0.8.0"):
    from heartbeat.legacy_migration import activate_stable_launcher

    install_root = tmp_path / "install"
    version_dir = _runtime(install_root / "versions" / version)
    launcher = activate_stable_launcher(install_root, version_dir)
    return install_root, launcher


def _service(monkeypatch, **overrides):
    from heartbeat import service
    from heartbeat.service.base import ServiceStatus

    defaults = {
        "platform": "launchd", "result": "registered", "registered": True, "running": True,
        "label": "com.claude-heartbeat", "executable": "", "evidence": ("launch_agents_directory",),
    }
    status = ServiceStatus(**{**defaults, **overrides})
    monkeypatch.setattr(service, "inspect_service", lambda: status)
    return status


def test_status_reports_installed_running_and_service_facts_in_one_response(tmp_path, monkeypatch):
    from heartbeat.cli import runtime_status

    install_root, launcher = _installed(tmp_path)
    _service(monkeypatch, executable=str(launcher))

    status = runtime_status(install_root)

    assert set(status) == DEVICE_QUERY_FIELDS
    assert status["installedVersion"] == status["runningVersion"]
    assert status["installResult"] == "installed"
    assert status["launcher"] == str(launcher)
    assert status["apiMajor"] == 1
    assert status["recoverable"] is True
    assert status["service"]["label"] == "com.claude-heartbeat"
    assert "version_manifest" in status["evidence"]


def test_status_never_turns_an_unconfirmed_service_into_a_running_version(tmp_path, monkeypatch):
    from heartbeat.cli import runtime_status

    install_root, launcher = _installed(tmp_path)
    _service(monkeypatch, result="permission_denied", running=None, executable=str(launcher))

    status = runtime_status(install_root)

    assert status["runningVersion"] is None
    assert status["service"]["running"] is None
    assert status["recoverable"] is None
    assert status["installedVersion"] is not None


@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        (lambda root, launcher: launcher.unlink(), "launcher_missing"),
        (lambda root, launcher: launcher.write_text("#!/bin/sh\nexec true\n", encoding="utf-8"), "version_missing"),
        (lambda root, launcher: (root / "versions" / "0.8.0" / MANIFEST_NAME).unlink(), "manifest_unreadable"),
    ],
)
def test_installed_runtime_separates_the_ways_an_install_can_be_unreadable(tmp_path, break_it, expected):
    from heartbeat.legacy_migration import installed_runtime

    install_root, launcher = _installed(tmp_path)
    break_it(install_root, launcher)

    assert installed_runtime(install_root)["result"] == expected


def test_an_incompatible_api_major_is_its_own_result(tmp_path, monkeypatch):
    import json

    from heartbeat.cli import runtime_status
    from heartbeat.legacy_migration import installed_runtime

    install_root, launcher = _installed(tmp_path)
    manifest_path = install_root / "versions" / "0.8.0" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["apiMajor"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _service(monkeypatch, executable=str(launcher))

    assert installed_runtime(install_root)["result"] == "unsupported_version"
    assert runtime_status(install_root)["result"] == "unsupported_version"


def test_status_changes_nothing_it_reports_on(tmp_path, monkeypatch):
    import hashlib

    from heartbeat.cli import runtime_status

    install_root, launcher = _installed(tmp_path)
    database = tmp_path / "agent-runtime.sqlite3"
    database.write_bytes(b"sqlite-state")
    definition = tmp_path / "com.claude-heartbeat.plist"
    definition.write_text("<plist/>", encoding="utf-8")
    _service(monkeypatch, executable=str(launcher))

    def digests():
        return {
            str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(tmp_path.rglob("*")) if path.is_file()
        }

    before = digests()
    runtime_status(install_root)
    runtime_status(install_root)

    assert digests() == before


def test_only_one_command_answers_the_device_status_question():
    """조회 명령이 둘이 되면 앱이 무엇을 읽을지 정할 수 없다.

    이름이 다른 두 번째 조회가 생기는 것을 막는 것이 이 검사의 전부다. 새 하위
    명령을 더할 일이 있으면 이 목록도 같이 바뀌어야 하고, 그때 같은 사실을 두 번
    돌려주는지 다시 판단하게 된다.
    """
    import re

    from heartbeat.agent_cli import EXECUTION_COMMANDS

    source = (Path(__file__).resolve().parents[1] / "src" / "heartbeat" / "cli.py").read_text(encoding="utf-8")
    runtime_commands = set(re.findall(r'runtime_sub\.add_parser\("([^"]+)"', source))

    assert runtime_commands == {"inspect", "write-manifest", "verify-manifest", "activate", "migration-preview"}
    assert not [command for command in EXECUTION_COMMANDS if "runtime" in command or "device" in command]
