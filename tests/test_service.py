"""service.py — install-service / uninstall-service 어댑터 검증.

실제 OS에 등록하지 않는다. print-only 모드의 출력 형식과 OS 분기 동작만 검증.
"""

from __future__ import annotations

import sys
from io import StringIO

import pytest

from heartbeat import service


def _capture_stdout(fn, *args, **kwargs) -> tuple[int, str]:
    buf = StringIO()
    saved = sys.stdout
    sys.stdout = buf
    try:
        rc = fn(*args, **kwargs)
    finally:
        sys.stdout = saved
    return rc, buf.getvalue()


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd plist 출력은 macOS에서만")
def test_install_service_print_only_macos(monkeypatch):
    monkeypatch.setattr(service, "_heartbeat_bin", lambda: "/fake/bin/heartbeat")
    rc, out = _capture_stdout(service.install_service, print_only=True)

    assert rc == 0
    assert "<plist" in out
    assert "com.claude-heartbeat" in out
    assert "/fake/bin/heartbeat" in out
    assert "launchctl load" in out


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler 명령은 Windows에서만")
def test_install_service_print_only_windows(monkeypatch):
    monkeypatch.setattr(service, "_heartbeat_bin", lambda: r"C:\fake\heartbeat.exe")
    rc, out = _capture_stdout(service.install_service, print_only=True)

    assert rc == 0
    assert "schtasks.exe" in out
    assert "claude-heartbeat" in out
    assert r"C:\fake\heartbeat.exe" in out


def test_install_service_missing_bin(monkeypatch):
    """heartbeat CLI가 PATH에 없으면 non-zero exit + 가이드 출력."""
    monkeypatch.setattr(service, "_heartbeat_bin", lambda: None)

    if sys.platform.startswith("linux"):
        # Linux는 어차피 1 반환 (Phase 3) — bin 체크 전 단계에서 끝남
        rc, _ = _capture_stdout(service.install_service, print_only=True)
        assert rc == 1
        return

    rc, out = _capture_stdout(service.install_service, print_only=True)
    assert rc == 1
    assert "PATH" in out or "찾을 수 없음" in out
