"""service/ 어댑터 검증.

실제 OS에 등록하지 않는다. print-only 모드 출력 검증 + monkeypatch로 회귀 시나리오.
"""

from __future__ import annotations

import subprocess
import sys
from io import StringIO

import pytest

from heartbeat import service
from heartbeat.service import (
    LaunchdAdapter,
    ServiceAdapter,
    SystemdAdapter,
    TaskSchedulerAdapter,
)
from heartbeat.service.systemd import UNIT_NAME


def _capture_stdout(fn, *args, **kwargs) -> tuple[int, str]:
    buf = StringIO()
    saved = sys.stdout
    sys.stdout = buf
    try:
        rc = fn(*args, **kwargs)
    finally:
        sys.stdout = saved
    return rc, buf.getvalue()


# --- print-only output sanity ---

@pytest.mark.skipif(sys.platform != "darwin", reason="launchd plist 출력은 macOS에서만")
def test_install_service_print_only_macos(monkeypatch):
    monkeypatch.setattr(LaunchdAdapter, "_heartbeat_bin", lambda self: "/fake/bin/heartbeat")
    rc, out = _capture_stdout(service.install_service, print_only=True)

    assert rc == 0
    assert "<plist" in out
    assert "com.claude-heartbeat" in out
    assert "/fake/bin/heartbeat" in out
    assert "launchctl load" in out


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler 명령은 Windows에서만")
def test_install_service_print_only_windows(monkeypatch):
    monkeypatch.setattr(TaskSchedulerAdapter, "_heartbeat_bin", lambda self: r"C:\fake\heartbeat.exe")
    rc, out = _capture_stdout(service.install_service, print_only=True)

    assert rc == 0
    assert "schtasks.exe" in out
    assert "claude-heartbeat" in out
    assert r"C:\fake\heartbeat.exe" in out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd unit 출력은 Linux에서만")
def test_install_service_print_only_linux(monkeypatch):
    monkeypatch.setattr(SystemdAdapter, "_heartbeat_bin", lambda self: "/fake/bin/heartbeat")
    rc, out = _capture_stdout(service.install_service, print_only=True)

    assert rc == 0
    assert "[Unit]" in out
    assert "[Service]" in out
    assert "Description=Claude Heartbeat" in out
    assert "/fake/bin/heartbeat" in out
    assert f"systemctl --user enable --now {UNIT_NAME}" in out
    assert f"systemctl --user status {UNIT_NAME}" in out  # 자가검증 진입로
    assert "loginctl enable-linger" in out
    assert "DBUS_SESSION_BUS_ADDRESS" in out  # SSH 세션 깨짐 가이드


# --- 모든 OS 공통: missing bin ---

def test_install_service_missing_bin(monkeypatch):
    """heartbeat CLI가 PATH에 없으면 non-zero exit + 가이드 출력."""
    monkeypatch.setattr(ServiceAdapter, "_heartbeat_bin", lambda self: None)

    rc, out = _capture_stdout(service.install_service, print_only=True)
    assert rc == 1
    assert "PATH" in out or "찾을 수 없음" in out


def test_windows_service_definition_restarts_after_crashes_and_rejects_duplicates():
    """Task Scheduler XML carries the same recovery semantics as the Unix adapters."""
    definition = TaskSchedulerAdapter()._task_xml(r"C:\Runtime\bin\heartbeat.exe")

    assert "<RestartOnFailure>" in definition
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in definition
    assert "<LogonTrigger>" in definition
    assert r"C:\Runtime\bin\heartbeat.exe" in definition


# --- HIGH: systemctl 부재 (컨테이너/WSL1) ---

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd 어댑터 회귀 (Linux 한정 모듈)")
def test_systemd_install_systemctl_missing(monkeypatch, tmp_path):
    """systemctl이 PATH에 없는 환경(컨테이너/WSL1)에서 graceful fail + 가이드."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(SystemdAdapter, "_heartbeat_bin", lambda self: "/fake/bin/heartbeat")

    def _no_systemctl(*args, **kwargs):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(subprocess, "run", _no_systemctl)

    adapter = SystemdAdapter()
    rc, out = _capture_stdout(adapter.install, print_only=False)

    assert rc == 1
    assert "systemctl" in out
    # unit 파일은 이미 쓰였고, 그 사실이 가이드에 명시
    assert "unit 파일은 등록됨" in out
    assert adapter._unit_path().exists()


# --- HIGH: 부분 실패 (daemon-reload OK / enable FAIL) ---

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd 어댑터 회귀")
def test_systemd_install_partial_failure_enable_after_reload(monkeypatch, tmp_path):
    """daemon-reload는 통과 / enable --now에서 실패 → unit 파일 보존 + enable 재시도 안내."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(SystemdAdapter, "_heartbeat_bin", lambda self: "/fake/bin/heartbeat")

    call_count = {"n": 0}

    def _flaky_systemctl(args, *_, **__):
        call_count["n"] += 1
        # 1st call: daemon-reload → OK
        if call_count["n"] == 1:
            return subprocess.CompletedProcess(args, 0, "", "")
        # 2nd call: enable --now → fail
        raise subprocess.CalledProcessError(
            returncode=5, cmd=args, output="", stderr="Failed to enable unit"
        )

    monkeypatch.setattr(subprocess, "run", _flaky_systemctl)

    adapter = SystemdAdapter()
    rc, out = _capture_stdout(adapter.install, print_only=False)

    assert rc == 1
    assert "enable 실패" in out
    assert "Failed to enable unit" in out
    assert f"systemctl --user enable --now {UNIT_NAME}" in out  # 재시도 명령
    assert "DBUS_SESSION_BUS_ADDRESS" in out  # 가능 원인
    assert adapter._unit_path().exists()  # unit 파일 보존


# --- MEDIUM: print-only 부수효과 0 ---

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd 어댑터 회귀")
def test_systemd_install_print_only_no_side_effects(monkeypatch, tmp_path):
    """print-only는 mkdir / write / subprocess 호출 0건."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(SystemdAdapter, "_heartbeat_bin", lambda self: "/fake/bin/heartbeat")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called in print-only mode")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)

    adapter = SystemdAdapter()
    rc, _ = _capture_stdout(adapter.install, print_only=True)

    assert rc == 0
    # 디렉토리/파일 흔적 없음
    assert not (tmp_path / ".config").exists()
    assert not (tmp_path / ".claude").exists()
    assert not adapter._unit_path().exists()


# --- MEDIUM: uninstall edge — systemctl 없는데 unit 파일만 있음 ---

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd 어댑터 회귀")
def test_systemd_uninstall_systemctl_missing_but_unit_exists(monkeypatch, tmp_path):
    """환경 이전(systemd 환경에서 install → 다른 머신으로 dotfile 옮긴 케이스).

    systemctl이 없어도 unit 파일은 정리해야 함.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = SystemdAdapter()
    unit_path = adapter._unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text("dummy unit", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))

    rc, out = _capture_stdout(adapter.uninstall, print_only=False)

    assert rc == 0
    assert "해제 완료" in out
    assert not unit_path.exists()  # unit 파일 정리됨


# --- LOW: unit 파일 write 권한 실패 ---

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd 어댑터 회귀")
def test_systemd_install_unit_file_permission_error(monkeypatch, tmp_path):
    """~/.config 가 읽기전용인 환경에서 PermissionError 발생 → graceful fail."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(SystemdAdapter, "_heartbeat_bin", lambda self: "/fake/bin/heartbeat")

    def _denied_write(*args, **kwargs):
        raise PermissionError("read-only filesystem")

    # Path.write_text가 실패하도록 monkeypatch (Path 클래스 메서드 직접)
    from pathlib import Path
    monkeypatch.setattr(Path, "write_text", _denied_write)

    adapter = SystemdAdapter()
    rc, out = _capture_stdout(adapter.install, print_only=False)

    assert rc == 1
    assert "쓰기 실패" in out
    assert "권한" in out
