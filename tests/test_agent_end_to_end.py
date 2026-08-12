"""Release-gate scenarios for the provider-neutral agent runtime.

The suite uses in-memory workflow reservations and a local Python fixture CLI.
It never imports a provider SDK, reads credentials, or sends a paid request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pytest
import psutil

from heartbeat.agent_contract import contract_description, default_configuration, validate_configuration
from heartbeat.agent_dispatch import DispatchFailure, RoleSlots, build_plan, start_one_run
from heartbeat.agent_store import AgentStore
from heartbeat.providers.lifecycle import Reservation
from heartbeat.providers.process import CliProvider, NormalizedLine, ProviderDiagnostic


FIXTURE_CLI = """\
import json
import sys

prompt = sys.stdin.read()
print(json.dumps({"type": "progress", "promptLength": len(prompt)}), flush=True)
print(json.dumps({"type": "done"}), flush=True)
"""


class FixtureProvider(CliProvider):
    """A Claude/Codex-shaped provider backed only by the local interpreter."""

    executable = sys.executable

    def __init__(self, name: str, script: Path) -> None:
        super().__init__()
        self.name = name
        self.script = script

    def command(self, request) -> list[str]:  # type: ignore[no-untyped-def]
        return [self.executable, str(self.script)]

    def authentication_command(self) -> list[str]:
        return [self.executable, "-c", "pass"]

    def normalize_line(self, value: dict[object, object]) -> tuple[NormalizedLine, ...]:
        if value.get("type") == "progress":
            return (NormalizedLine("progress", raw_id="fixture"),)
        if value.get("type") == "done":
            return (NormalizedLine("completed", raw_id="fixture"),)
        return ()

    def _diagnose(self, environment, *, billing_route_acknowledged):  # type: ignore[no-untyped-def]
        return ProviderDiagnostic(self.name, "ready", self.executable, version="fixture-1")


class MemoryHelpers:
    """The reservation contract without an OS shell, shared by all CI targets."""

    def __init__(self, root: Path, targets: list[str], *, migration_locked: bool = False) -> None:
        self.working_directory = root
        self.targets = deque(targets)
        self.claimed: set[str] = set()
        self.lock = threading.Lock()
        self.is_migration_locked = migration_locked

    def migration_locked(self) -> bool:
        return self.is_migration_locked

    def lease_is_active(self, target_id: str, *, now: datetime) -> bool:
        del now
        return target_id in self.claimed

    def reserve(self, role: str, agent: str, minutes: int = 30) -> Reservation | DispatchFailure:
        del agent, minutes
        with self.lock:
            if self.is_migration_locked:
                return DispatchFailure("reservation", "migration_locked")
            while self.targets:
                target = self.targets.popleft()
                if target in self.claimed:
                    continue
                self.claimed.add(target)
                return Reservation(
                    contract_version=1,
                    role=role,
                    target_id=target,
                    lease_id=f"lease-{target}",
                    result_prefix=f"RESULT-{target}",
                    expires_at="2030-01-01T00:00:00Z",
                    prompt_version=1,
                    role_prompt="fixture prompt that must not be persisted",
                )
        return DispatchFailure("reservation", "reservation_unavailable")

    def renew(self, target_id: str, lease_id: str, minutes: int = 30) -> int:
        del minutes
        return 0 if target_id in self.claimed and lease_id == f"lease-{target_id}" else 5

    def release(self, target_id: str, lease_id: str) -> int:
        with self.lock:
            if target_id not in self.claimed or lease_id != f"lease-{target_id}":
                return 5
            self.claimed.remove(target_id)
            return 0


@pytest.fixture
def fixture_script(tmp_path: Path) -> Path:
    script = tmp_path / "provider_fixture.py"
    script.write_text(FIXTURE_CLI, encoding="utf-8")
    return script


def configuration(root: Path, project_id: str, **overrides):  # type: ignore[no-untyped-def]
    data = default_configuration(project_id, str(root)).to_dict()
    data.update(overrides)
    return validate_configuration(data)


def provider_factory(script: Path):  # type: ignore[no-untyped-def]
    return lambda name: FixtureProvider(name, script)


def test_contract_and_persisted_projects_are_provider_neutral_and_dream_free(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "agent.sqlite3")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = configuration(first_root, "project-one")
    second_data = default_configuration("project-two", str(second_root)).to_dict()
    second_data["roles"]["architect"]["provider"] = "codex"
    second = validate_configuration(second_data)
    store.save_configuration(first)
    store.save_configuration(second)
    store.save_intent("project-one", {"intentId": "repeat-one", "role": "developer", "mode": "auto", "manualTargets": []})

    reopened = AgentStore(store.database_path)
    contract = contract_description()

    assert contract["providers"] == ["claude", "codex"]
    assert "dream" not in json.dumps(contract).lower()
    assert reopened.get_configuration("project-one")["roles"]["architect"]["provider"] == "claude"
    assert reopened.get_configuration("project-two")["roles"]["architect"]["provider"] == "codex"
    assert reopened.list_intents("project-one")[0]["intentId"] == "repeat-one"
    assert reopened.list_intents("project-two") == []


def test_limits_choose_two_targets_and_fake_clis_leave_no_prompt_in_state(
    tmp_path: Path, fixture_script: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HEARTBEAT_AGENT_HOME", str(tmp_path / "runtime-home"))
    root = tmp_path / "project"
    root.mkdir()
    store = AgentStore(tmp_path / "agent.sqlite3")
    roles = default_configuration("project-one", str(root)).to_dict()["roles"]
    roles["developer"]["maxParallel"] = 3
    config = configuration(root, "project-one", projectMaxParallel=5, deviceMaxParallel=5, roles=roles)
    store.save_configuration(config)
    store.save_run({
        "runId": "foreign", "projectId": "project-two", "role": "planner", "provider": "claude",
        "state": "running", "targetId": "TASK-X", "leaseId": "lease-x",
    })
    helpers = MemoryHelpers(root, ["TASK-1", "TASK-2"])
    factory = provider_factory(fixture_script)

    plan = build_plan(store, config, [RoleSlots("developer", 3)], helpers=helpers, provider_factory=factory)
    rows = [start_one_run(store, config, "developer", helpers=helpers, provider_factory=factory) for _ in range(3)]

    assert plan["deviceRemaining"] == 4
    assert plan["roles"][0]["granted"] == 3
    assert [row["state"] for row in rows].count("running") == 2
    assert rows[-1]["reason"] == "reservation_unavailable"
    persisted = (tmp_path / "agent.sqlite3").read_bytes()
    assert b"fixture prompt that must not be persisted" not in persisted


def test_migration_lock_and_no_target_start_no_provider(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = AgentStore(tmp_path / "agent.sqlite3")
    config = configuration(root, "project-one")
    calls = 0

    def forbidden_provider(name: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError(f"provider {name} must not start")

    locked = start_one_run(
        store, config, "developer", helpers=MemoryHelpers(root, ["TASK-1"], migration_locked=True),
        provider_factory=forbidden_provider,
    )
    empty = start_one_run(
        store, config, "developer", helpers=MemoryHelpers(root, []), provider_factory=forbidden_provider,
    )

    assert (locked["failureStage"], locked["reason"]) == ("reservation", "migration_locked")
    assert (empty["failureStage"], empty["reason"]) == ("reservation", "reservation_unavailable")
    assert calls == 0


def test_competing_dispatchers_start_one_provider_for_one_target(
    tmp_path: Path, fixture_script: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HEARTBEAT_AGENT_HOME", str(tmp_path / "runtime-home"))
    root = tmp_path / "project"
    root.mkdir()
    store = AgentStore(tmp_path / "agent.sqlite3")
    config = configuration(root, "project-one", projectMaxParallel=4, deviceMaxParallel=4)
    helpers = MemoryHelpers(root, ["TASK-SHARED", "TASK-SHARED", "TASK-SHARED", "TASK-SHARED"])
    factory = provider_factory(fixture_script)
    rows: list[dict] = []
    rows_lock = threading.Lock()

    def dispatch() -> None:
        row = start_one_run(store, config, "developer", helpers=helpers, provider_factory=factory)
        with rows_lock:
            rows.append(row)

    threads = [threading.Thread(target=dispatch) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [row["state"] for row in rows].count("running") == 1
    assert {row["targetId"] for row in rows if row["state"] == "running"} == {"TASK-SHARED"}
    assert helpers.claimed == {"TASK-SHARED"}


@pytest.mark.skipif(
    not os.environ.get("HEARTBEAT_SERVICE_SMOKE_BINARY"),
    reason="실제 서비스 smoke는 target별 릴리스 job에서만 실행한다",
)
def test_platform_service_registration_restart_and_cleanup() -> None:
    """Register the built artifact, kill it, require a new PID, and always unregister it."""
    binary = Path(os.environ["HEARTBEAT_SERVICE_SMOKE_BINARY"]).resolve()
    assert binary.is_file(), f"검사할 runtime 실행 파일이 없습니다: {binary}"
    environment = {
        **os.environ,
        "HEARTBEAT_RUNTIME_LAUNCHER": str(binary),
        "PATH": f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    current_pid: int | None = None

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), *arguments], env=environment, text=True, capture_output=True, timeout=30, check=False,
        )

    store = AgentStore()

    def wait_for_pid(*, different_from: int | None = None, timeout: float = 90) -> int:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = store.get_dispatcher()
            if last is not None:
                pid = int(last["pid"])
                if pid != different_from and psutil.pid_exists(pid):
                    return pid
            time.sleep(1)
        raise AssertionError(f"dispatcher PID를 확인하지 못했습니다: {last}")

    try:
        installed = invoke("install-service")
        assert installed.returncode == 0, (
            "이 runner에서 사용자 서비스 등록을 사용할 수 없습니다. 성공으로 건너뛰지 않습니다.\n"
            f"stdout: {installed.stdout}\nstderr: {installed.stderr}"
        )
        first_pid = wait_for_pid()
        psutil.Process(first_pid).kill()
        psutil.Process(first_pid).wait(timeout=15)
        current_pid = wait_for_pid(different_from=first_pid)
        assert current_pid != first_pid
    finally:
        uninstalled = invoke("uninstall-service")
        if current_pid and psutil.pid_exists(current_pid):
            try:
                psutil.Process(current_pid).terminate()
                psutil.Process(current_pid).wait(timeout=10)
            except psutil.Error:
                pass
        assert uninstalled.returncode == 0, (
            f"서비스 smoke 등록물을 정리하지 못했습니다.\nstdout: {uninstalled.stdout}\nstderr: {uninstalled.stderr}"
        )
