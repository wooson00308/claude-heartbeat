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
    assert "<Arguments>agent-dispatcher</Arguments>" in definition


def test_all_service_definitions_launch_the_agent_dispatcher(monkeypatch):
    monkeypatch.setattr(LaunchdAdapter, "_heartbeat_bin", lambda self: "/fake/heartbeat")
    monkeypatch.setattr(SystemdAdapter, "_heartbeat_bin", lambda self: "/fake/heartbeat")

    assert "<string>agent-dispatcher</string>" in LaunchdAdapter().render()
    assert "ExecStart=/fake/heartbeat agent-dispatcher" in SystemdAdapter().render()


def test_launchd_reads_both_boolean_and_word_disabled_formats(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            0,
            '\t"com.old-heartbeat" => disabled\n'
            '\t"com.bool-heartbeat" => true\n'
            '\t"com.live-heartbeat" => enabled\n',
        ),
    )

    assert LaunchdAdapter()._disabled_labels() == {
        "com.old-heartbeat",
        "com.bool-heartbeat",
    }


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


# --- 구조화된 inspect: 세 어댑터가 같은 필드를 같은 뜻으로 채운다 ---

CONTRACT_FIELDS = {
    "platform", "result", "registered", "running", "label", "executable",
    "recoverable", "checkedAt", "evidence", "detail",
}


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _plist(path, label, program):
    import plistlib

    with path.open("wb") as stream:
        plistlib.dump({"Label": label, "ProgramArguments": [str(program), "start"]}, stream)
    return path


def _unit(path, program):
    path.write_text(f"[Service]\nExecStart={program} start\nRestart=always\n", encoding="utf-8")
    return path


def _launchd(monkeypatch, plists, run=None):
    adapter = LaunchdAdapter()
    monkeypatch.setattr(LaunchdAdapter, "_heartbeat_plists", lambda self: list(plists))
    if run is not None:
        monkeypatch.setattr(subprocess, "run", run)
    return adapter


def _systemd(monkeypatch, unit_path, run=None):
    adapter = SystemdAdapter()
    monkeypatch.setattr(SystemdAdapter, "_unit_path", lambda self: unit_path)
    if run is not None:
        monkeypatch.setattr(subprocess, "run", run)
    return adapter


def test_every_adapter_returns_the_same_inspect_fields(monkeypatch, tmp_path):
    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    def answer(command, *args, **kwargs):
        # 세 어댑터가 한 테스트 안에서 같은 subprocess를 쓰므로 명령으로 갈라 답한다.
        if command[0] == "launchctl":
            return _completed(0, '\t"PID" = 4242;\n')
        return _completed(0, "active\n")

    launchd = _launchd(
        monkeypatch,
        [_plist(tmp_path / "com.claude-heartbeat.plist", "com.claude-heartbeat", program)],
        run=answer,
    )
    systemd = _systemd(monkeypatch, _unit(tmp_path / "unit.service", program), run=answer)
    windows = TaskSchedulerAdapter()
    monkeypatch.setattr(TaskSchedulerAdapter, "_registered_command", lambda self: str(program))

    statuses = [launchd.inspect(), systemd.inspect(), windows.inspect()]

    assert [set(status.to_dict()) for status in statuses] == [CONTRACT_FIELDS] * 3
    assert [status.result for status in statuses] == ["registered"] * 3
    assert [status.registered for status in statuses] == [True] * 3
    assert [status.recoverable for status in statuses] == [True] * 3
    assert [status.executable for status in statuses] == [str(program)] * 3
    # Windows는 실행 여부를 로케일 의존 출력에서 읽지 않으므로 모른다고 답한다.
    assert [status.running for status in statuses] == [True, True, None]


def test_a_registration_without_its_executable_is_not_recoverable(monkeypatch, tmp_path):
    missing = tmp_path / "gone" / "heartbeat"
    launchd = _launchd(monkeypatch, [_plist(tmp_path / "a.plist", "com.claude-heartbeat", missing)])
    systemd = _systemd(monkeypatch, _unit(tmp_path / "unit.service", missing))

    for status in (launchd.inspect(), systemd.inspect()):
        assert status.result == "executable_missing"
        assert status.registered is True
        assert status.running is None
        assert status.recoverable is False


def test_two_registrations_are_ambiguous_instead_of_one_guess(monkeypatch, tmp_path):
    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    first = _plist(tmp_path / "com.claude-heartbeat.plist", "com.claude-heartbeat", program)
    second = _plist(tmp_path / "com.other-heartbeat.plist", "com.other-heartbeat", program)

    status = _launchd(monkeypatch, [first, second]).inspect()

    assert status.result == "ambiguous_registration"
    assert status.running is None
    assert status.recoverable is False
    assert "com.other-heartbeat" in status.detail["registrations"]


def test_missing_registration_is_its_own_result(monkeypatch, tmp_path):
    launchd = _launchd(monkeypatch, [])
    systemd = SystemdAdapter()
    monkeypatch.setattr(SystemdAdapter, "_unit_path", lambda self: tmp_path / "absent.service")

    for status in (launchd.inspect(), systemd.inspect()):
        assert status.result == "not_registered"
        assert (status.registered, status.running, status.recoverable) == (False, False, False)


def test_unreadable_state_never_becomes_running(monkeypatch, tmp_path):
    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    denied = _launchd(
        monkeypatch,
        [_plist(tmp_path / "a.plist", "com.claude-heartbeat", program)],
        run=lambda *a, **k: _completed(1, "", "Operation not permitted"),
    )
    denied_status = denied.inspect()

    def missing_tool(*args, **kwargs):
        raise FileNotFoundError

    absent = _systemd(monkeypatch, _unit(tmp_path / "unit.service", program), run=missing_tool)
    absent_status = absent.inspect()

    assert (denied_status.result, denied_status.running, denied_status.recoverable) == (
        "permission_denied", None, None,
    )
    assert (absent_status.result, absent_status.running, absent_status.recoverable) == (
        "tool_missing", None, None,
    )


def test_systemd_reports_a_stopped_unit_as_not_running(monkeypatch, tmp_path):
    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    adapter = _systemd(monkeypatch, _unit(tmp_path / "unit.service", program),
                       run=lambda *a, **k: _completed(3, "inactive\n"))

    status = adapter.inspect()

    assert (status.result, status.registered, status.running) == ("registered", True, False)
    assert status.detail["activeState"] == "inactive"


def test_a_platform_without_an_adapter_answers_in_the_same_shape(monkeypatch):
    monkeypatch.setattr(service, "_get_adapter", lambda: None)

    status = service.inspect_service()

    assert set(status.to_dict()) == CONTRACT_FIELDS
    assert status.result == "unsupported_platform"
    assert (status.registered, status.running, status.recoverable) == (None, None, None)


def test_inspect_writes_nothing_the_adapters_could_have_changed(monkeypatch, tmp_path):
    import hashlib

    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    plist = _plist(tmp_path / "com.claude-heartbeat.plist", "com.claude-heartbeat", program)
    unit = _unit(tmp_path / "unit.service", program)

    def digests():
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(tmp_path.iterdir()) if path.is_file()
        }

    before = digests()
    _launchd(monkeypatch, [plist], run=lambda *a, **k: _completed(0, '\t"PID" = 1;\n')).inspect()
    _systemd(monkeypatch, unit, run=lambda *a, **k: _completed(0, "active\n")).inspect()
    TaskSchedulerAdapter().inspect()

    assert digests() == before


def test_launchd_migration_preserves_foreign_plist_and_disables_its_label(monkeypatch, tmp_path):
    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    foreign = _plist(tmp_path / "com.old-heartbeat.plist", "com.old-heartbeat", program)
    before = foreign.read_bytes()
    adapter = _launchd(monkeypatch, [foreign])
    own = tmp_path / "com.claude-heartbeat.plist"
    monkeypatch.setattr(LaunchdAdapter, "_plist_path", lambda self: own)
    monkeypatch.setattr(LaunchdAdapter, "_heartbeat_bin", lambda self: str(program))
    disabled = set()
    calls = []

    def answer(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["launchctl", "disable"]:
            disabled.add("com.old-heartbeat")
        if command[:2] == ["launchctl", "load"] and command[-1] == str(own):
            own.write_text(adapter.render(), encoding="utf-8")
        if command[:2] == ["launchctl", "list"]:
            return _completed(0, '\t"PID" = 42;\n')
        return _completed()

    monkeypatch.setattr(adapter, "_disabled_labels", lambda: set(disabled))
    monkeypatch.setattr(subprocess, "run", answer)
    original_install = adapter.install

    def install(print_only=False):
        monkeypatch.setattr(adapter, "_heartbeat_plists", lambda: [own, foreign])
        return original_install(print_only)

    monkeypatch.setattr(adapter, "install", install)

    assert adapter.migrate() == 0
    assert foreign.read_bytes() == before
    assert "com.old-heartbeat" in disabled
    assert any(call[:2] == ["launchctl", "unload"] for call in calls)


def test_launchd_migration_rolls_back_when_managed_service_does_not_run(monkeypatch, tmp_path):
    program = tmp_path / "heartbeat"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    foreign = _plist(tmp_path / "com.old-heartbeat.plist", "com.old-heartbeat", program)
    adapter = _launchd(monkeypatch, [foreign])
    own = tmp_path / "com.claude-heartbeat.plist"
    monkeypatch.setattr(LaunchdAdapter, "_plist_path", lambda self: own)
    monkeypatch.setattr(LaunchdAdapter, "_heartbeat_bin", lambda self: str(program))
    calls = []

    def answer(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["launchctl", "list"] and command[-1] == "com.old-heartbeat":
            return _completed(1)
        if command[:2] == ["launchctl", "load"] and command[-1] == str(own):
            own.write_text(adapter.render(), encoding="utf-8")
        if command[:2] == ["launchctl", "list"]:
            return _completed(0, "")
        return _completed()

    monkeypatch.setattr(adapter, "_disabled_labels", lambda: {"com.old-heartbeat"} if own.exists() else set())
    monkeypatch.setattr(subprocess, "run", answer)
    original_install = adapter.install

    def install(print_only=False):
        monkeypatch.setattr(adapter, "_heartbeat_plists", lambda: [own, foreign])
        return original_install(print_only)

    monkeypatch.setattr(adapter, "install", install)

    assert adapter.migrate() == 1
    assert not own.exists()
    assert any(call[:2] == ["launchctl", "enable"] for call in calls)
    assert not any(call[:2] == ["launchctl", "load"] and call[-1] == str(foreign) for call in calls)
