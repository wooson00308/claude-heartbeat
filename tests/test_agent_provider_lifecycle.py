"""Lifecycle checks for reserved provider runs: start, watch, cancel, recover.

Every run here uses a fake CLI driven through the real Claude and Codex
provider classes, so the normalizers and the process contract under test are
the shipped ones.  No installed agent CLI, network, or account is involved.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from heartbeat.providers import ClaudeProvider, CodexProvider
from heartbeat.providers import process as process_module
from heartbeat.providers.lifecycle import (
    LifecycleFailure,
    Reservation,
    cancel_run,
    conclude_run,
    read_reservation,
    recover_run,
    start_reserved_run,
)
from heartbeat.providers.process import (
    ProcessObservation,
    ProviderDiagnostic,
    ProviderExecutionRequest,
    ProviderRunHandle,
)
from heartbeat.providers import lifecycle as lifecycle_module

FAKE_CLI = """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

flavor = sys.argv[1]
if flavor == "auth":
    raise SystemExit(0)
prompt = sys.stdin.read()
Path(os.environ["FAKE_CLI_PROMPT"]).write_text(prompt, encoding="utf-8")
mode = os.environ.get("FAKE_CLI_MODE", "success")
progress = {
    "claude": {"type": "assistant", "session_id": "session-1"},
    "codex": {"type": "thread.started", "thread_id": "thread-1"},
}[flavor]
finished = {
    "claude": {"type": "result", "subtype": "success", "session_id": "session-1"},
    "codex": {"type": "turn.completed", "thread_id": "thread-1"},
}[flavor]
print(json.dumps(progress), flush=True)
if mode == "leaky":
    print(json.dumps({"type": "error", "message": prompt + " " + os.environ["TEST_API_KEY"]}), flush=True)
    raise SystemExit(1)
if mode == "slow":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(os.environ["FAKE_CLI_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
    while True:
        time.sleep(0.1)
        print(json.dumps(progress), flush=True)
print(json.dumps(finished), flush=True)
"""


class _FakeCli:
    """Wire a shipped provider class to the fixture CLI and count its starts."""

    def __init__(self, script: Path) -> None:
        super().__init__(executable=sys.executable)  # type: ignore[call-arg]
        self.script = script
        self.start_calls = 0

    def command(self, request: ProviderExecutionRequest) -> list[str]:
        return [self.executable, str(self.script), self.name]

    def authentication_command(self) -> list[str]:
        return [self.executable, str(self.script), "auth"]

    def start(self, request, **keywords):  # type: ignore[no-untyped-def]
        self.start_calls += 1
        return super().start(request, **keywords)

    def _diagnose(self, environment, *, billing_route_acknowledged):  # type: ignore[no-untyped-def]
        return ProviderDiagnostic(self.name, "ready", self.executable, version="1.0")


class FakeClaude(_FakeCli, ClaudeProvider):
    """The Claude provider with a fake executable and its real normalizer."""


class FakeCodex(_FakeCli, CodexProvider):
    """The Codex provider with a fake executable and its real normalizer."""


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_cli.py"
    path.write_text(FAKE_CLI, encoding="utf-8")
    return path


@pytest.fixture
def started_runs():  # type: ignore[no-untyped-def]
    """Stop every run a test started, even when the test failed early."""
    handles: list[tuple[object, ProviderRunHandle]] = []
    yield handles
    for provider, handle in handles:
        provider.cancel(handle)  # type: ignore[attr-defined]
        try:
            provider.wait(handle)  # type: ignore[attr-defined]
        except LookupError:
            pass


def reservation_payload(**overrides: object) -> str:
    """One success line of the workflow reservation helper, field for field."""
    payload: dict[str, object] = {
        "contractVersion": 1,
        "role": "developer",
        "targetId": "TASK-1",
        "leaseId": "lease-1-20260808000000",
        "resultPrefix": "RES-20260808T000000Z-1-20260808000000",
        "expiresAt": "2026-08-08T01:00:00Z",
        "promptVersion": 1,
        "rolePrompt": "You are the developer role for one pre-reserved LLM Workflow target.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def reserved(**overrides: object) -> Reservation:
    value = read_reservation(0, reservation_payload(**overrides))
    assert isinstance(value, Reservation)
    return value


def request(
    tmp_path: Path,
    reservation: Reservation,
    *,
    prompt: str = "write the private plan",
    timeout_seconds: float | None = None,
    target_id: str | None = None,
    lease_id: str | None = None,
) -> ProviderExecutionRequest:
    return ProviderExecutionRequest(
        project_id="project-1",
        role=reservation.role,
        target_id=target_id or reservation.target_id,
        project_root=tmp_path,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        event_root=tmp_path / "runs",
        lease_id=reservation.lease_id if lease_id is None else lease_id,
    )


def environment(tmp_path: Path, *, mode: str = "success", secret: str = "api-secret-456") -> dict[str, str]:
    values = {
        "FAKE_CLI_MODE": mode,
        "FAKE_CLI_PROMPT": str(tmp_path / "prompt.txt"),
        "FAKE_CLI_CHILD_PID": str(tmp_path / "child.pid"),
        "TEST_API_KEY": secret,
    }
    return values


def wait_until(condition, message: str):  # type: ignore[no-untyped-def]
    for _ in range(100):
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def is_gone(pid: int) -> bool:
    for _ in range(40):
        try:
            if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("provider_class", [FakeClaude, FakeCodex])
def test_reserved_start_returns_a_handle_that_names_the_reserved_work(
    provider_class, script: Path, tmp_path: Path, started_runs
) -> None:  # type: ignore[no-untyped-def]
    provider = provider_class(script)
    reservation = reserved()

    handle = start_reserved_run(
        provider,
        reservation,
        request(tmp_path, reservation),
        environment=environment(tmp_path, mode="slow"),
    )

    assert isinstance(handle, ProviderRunHandle)
    started_runs.append((provider, handle))
    wait_until((tmp_path / "child.pid").exists, "the fake CLI never started its child")
    assert handle.provider == provider.name
    assert handle.run_id
    assert psutil.pid_exists(handle.pid)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", handle.started_at)
    assert handle.process_identity is not None
    assert (handle.target_id, handle.lease_id) == (reservation.target_id, reservation.lease_id)
    assert handle.event_path.parent == tmp_path / "runs"
    # The handle arrives while the run is still going: the process is alive and
    # its event file already holds the start event.
    assert provider.watch(handle).events[0].kind == "started"
    assert psutil.pid_exists(handle.pid)


def test_start_success_is_not_role_success_and_the_failing_stages_stay_apart(
    script: Path, tmp_path: Path, started_runs
) -> None:  # type: ignore[no-untyped-def]
    class MissingExecutable(FakeClaude):
        def command(self, value: ProviderExecutionRequest) -> list[str]:
            return ["definitely-not-an-agent-cli"]

    reservation = reserved()
    diagnostic_provider = ClaudeProvider(executable="definitely-not-an-agent-cli")
    spawn_provider = MissingExecutable(script)
    role_provider = FakeClaude(script)

    diagnostic = start_reserved_run(
        diagnostic_provider, reservation, request(tmp_path, reservation)
    )
    spawn = start_reserved_run(spawn_provider, reservation, request(tmp_path, reservation))
    started = start_reserved_run(
        role_provider,
        reservation,
        request(tmp_path, reservation),
        environment=environment(tmp_path, mode="leaky"),
    )
    assert isinstance(started, ProviderRunHandle)
    started_runs.append((role_provider, started))
    conclusion = conclude_run(role_provider, started)

    assert isinstance(diagnostic, LifecycleFailure)
    assert (diagnostic.stage, diagnostic.reason) == ("provider_start", "provider_executable_missing")
    assert diagnostic.start_failure is not None
    assert diagnostic.start_failure.diagnostic is not None
    assert diagnostic.start_failure.diagnostic.status == "executable_missing"
    assert isinstance(spawn, LifecycleFailure)
    assert (spawn.stage, spawn.reason) == ("provider_start", "spawn")
    # The same start that produced a handle ends as a failed role session.
    assert conclusion.status == "failed"
    assert (conclusion.run_id, conclusion.target_id, conclusion.lease_id) == (
        started.run_id,
        reservation.target_id,
        reservation.lease_id,
    )


def test_contract_two_carries_the_isolated_working_copy_and_contract_one_stays_bare() -> None:
    """예약 헬퍼 v4는 개발 대상에 격리 작업 사본을 실은 계약 2를 낸다(2026-08-15 실측).

    계약 1만 알던 런타임은 그 예약을 반납할 방법도 없이 거절해 죽은 선점만 남겼다. 계약 2를
    받아들인다는 것이 곧 격리 필드를 알아본다는 선언이므로, 두 계약을 함께 고정한다.
    """
    isolated = reserved(
        contractVersion=2,
        workspacePath="/repo/.workflow/.runtime/worktrees/TASK-1/lease-1",
        controlRoot="/repo/.workflow",
        baseCommit="0123abc",
        branch="wf-iso/TASK-1/lease-1",
    )
    bare = reserved()

    assert isolated.contract_version == 2
    assert isolated.workspace_path == "/repo/.workflow/.runtime/worktrees/TASK-1/lease-1"
    assert isolated.control_root == "/repo/.workflow"
    assert isolated.base_commit == "0123abc"
    assert isolated.branch == "wf-iso/TASK-1/lease-1"
    assert bare.contract_version == 1
    assert bare.workspace_path is None
    assert bare.branch is None

    # 격리 필드가 있다고 주장하면서 빈 값을 실은 예약은 계약 밖이다.
    blank = read_reservation(0, reservation_payload(contractVersion=2, workspacePath="  "))
    assert isinstance(blank, LifecycleFailure)
    assert blank.reason == "reservation_malformed"


def test_reservation_failures_and_mismatches_never_start_a_provider(
    script: Path, tmp_path: Path
) -> None:
    provider = FakeClaude(script)
    reservation = reserved()

    # A failed reservation, a lost race and a migration lock all reach the
    # runtime as exit code 1 with no output.
    unavailable = read_reservation(1, "")
    usage_error = read_reservation(2, "")
    malformed = read_reservation(0, "not json at all")
    future_contract = read_reservation(0, reservation_payload(contractVersion=3))
    other_target = start_reserved_run(
        provider, reservation, request(tmp_path, reservation, target_id="TASK-2")
    )
    other_lease = start_reserved_run(
        provider, reservation, request(tmp_path, reservation, lease_id="lease-somebody-else")
    )

    assert isinstance(unavailable, LifecycleFailure)
    assert (unavailable.stage, unavailable.reason) == ("reservation", "reservation_unavailable")
    assert isinstance(usage_error, LifecycleFailure)
    assert usage_error.reason == "reservation_usage_error"
    assert isinstance(malformed, LifecycleFailure)
    assert malformed.reason == "reservation_malformed"
    assert isinstance(future_contract, LifecycleFailure)
    assert future_contract.reason == "unsupported_reservation_contract"
    assert isinstance(other_target, LifecycleFailure)
    assert (other_target.stage, other_target.reason) == ("reservation", "reservation_mismatch")
    assert isinstance(other_lease, LifecycleFailure)
    assert other_lease.reason == "reservation_mismatch"
    assert provider.start_calls == 0
    assert not (tmp_path / "runs").exists()


def test_the_reservation_response_is_read_without_changing_its_contract() -> None:
    reservation = reserved()

    assert reservation.contract_version == 1
    assert reservation.role == "developer"
    assert reservation.target_id == "TASK-1"
    assert reservation.lease_id == "lease-1-20260808000000"
    assert reservation.result_prefix.startswith("RES-")
    assert reservation.expires_at == "2026-08-08T01:00:00Z"
    assert reservation.prompt_version == 1
    assert reservation.role_prompt.startswith("You are the developer role")


def test_a_restarted_runtime_resumes_the_same_process_without_starting_a_provider(
    script: Path, tmp_path: Path, started_runs
) -> None:  # type: ignore[no-untyped-def]
    provider = FakeCodex(script)
    reservation = reserved()
    handle = start_reserved_run(
        provider,
        reservation,
        request(tmp_path, reservation),
        environment=environment(tmp_path, mode="slow"),
    )
    assert isinstance(handle, ProviderRunHandle)
    started_runs.append((provider, handle))
    wait_until((tmp_path / "child.pid").exists, "the fake CLI never started its child")
    first = wait_until(
        lambda: provider.watch(handle) if len(provider.watch(handle).events) >= 2 else None,
        "the run never produced its first events",
    )

    # The supervising runtime is gone; only the persisted handle and the last
    # confirmed offset survive into this provider instance.
    successor = FakeCodex(script)
    resumed = wait_until(
        lambda: (
            recovery
            if (recovery := recover_run(successor, handle, offset=first.next_offset)).events
            else None
        ),
        "the recovered run produced no further events",
    )
    cancel_run(provider, handle)
    whole = successor.watch(handle)

    assert successor.start_calls == 0
    assert resumed.outcome == "resumed"
    assert resumed.reason is None
    assert (resumed.run_id, resumed.target_id, resumed.lease_id) == (
        handle.run_id,
        reservation.target_id,
        reservation.lease_id,
    )
    assert resumed.next_offset > first.next_offset
    boundary = len(first.events)
    assert whole.events[:boundary] == first.events
    assert whole.events[boundary : boundary + len(resumed.events)] == resumed.events


def test_pid_reuse_and_unverifiable_identity_are_never_adopted_as_the_run(
    monkeypatch, script: Path, tmp_path: Path, started_runs
) -> None:  # type: ignore[no-untyped-def]
    provider = FakeClaude(script)
    reservation = reserved()
    handle = start_reserved_run(
        provider,
        reservation,
        request(tmp_path, reservation),
        environment=environment(tmp_path, mode="slow"),
    )
    assert isinstance(handle, ProviderRunHandle)
    started_runs.append((provider, handle))
    wait_until((tmp_path / "child.pid").exists, "the fake CLI never started its child")

    reused_pid = recover_run(provider, replace(handle, process_identity="1.000000"))
    without_identity = recover_run(provider, replace(handle, process_identity=None))
    monkeypatch.setattr(
        lifecycle_module, "observe_process", lambda pid: ProcessObservation("unknown", None)
    )
    unverifiable = recover_run(provider, handle)

    assert (reused_pid.outcome, reused_pid.reason) == (
        "recovery_required",
        "process_identity_mismatch",
    )
    assert reused_pid.observed_identity == handle.process_identity
    assert (without_identity.outcome, without_identity.reason) == (
        "recovery_required",
        "handle_identity_missing",
    )
    assert (unverifiable.outcome, unverifiable.reason) == (
        "recovery_required",
        "process_identity_unavailable",
    )
    assert unverifiable.observed_identity is None
    assert provider.start_calls == 1


def test_a_finished_process_recovers_into_the_cleanup_stages(
    script: Path, tmp_path: Path
) -> None:
    provider = FakeCodex(script)
    reservation = reserved()
    handle = start_reserved_run(
        provider, reservation, request(tmp_path, reservation), environment=environment(tmp_path)
    )
    assert isinstance(handle, ProviderRunHandle)

    conclusion = conclude_run(provider, handle)
    # A restarted runtime holds the handle but not the run that produced it.
    successor = FakeCodex(script)
    recovery = recover_run(successor, handle, offset=conclusion.next_offset)
    not_owned = cancel_run(successor, handle)

    assert conclusion.status == "success"
    assert (conclusion.process_stopped, conclusion.events_closed) == (True, True)
    assert conclusion.remaining == ("lease_release",)
    assert [event.kind for event in conclusion.events][-1] == "completed"
    assert (recovery.outcome, recovery.reason) == ("cleanup_required", "process_exited")
    assert recovery.remaining == ("lease_release",)
    assert recovery.events == ()
    assert not_owned.detail == "unknown_run"
    assert not_owned.remaining == ("lease_release",)
    assert successor.start_calls == 0


def test_cancelling_through_the_handle_leaves_no_child_and_only_the_lease_open(
    script: Path, tmp_path: Path
) -> None:
    provider = FakeClaude(script)
    reservation = reserved()
    handle = start_reserved_run(
        provider,
        reservation,
        request(tmp_path, reservation),
        environment=environment(tmp_path, mode="slow"),
    )
    assert isinstance(handle, ProviderRunHandle)
    child_pid_path = tmp_path / "child.pid"
    wait_until(child_pid_path.exists, "the fake CLI never started its child")

    conclusion = cancel_run(provider, handle)

    assert conclusion.status == "cancelled"
    assert (conclusion.process_stopped, conclusion.events_closed) == (True, True)
    assert conclusion.remaining == ("lease_release",)
    assert (conclusion.run_id, conclusion.target_id, conclusion.lease_id) == (
        handle.run_id,
        reservation.target_id,
        reservation.lease_id,
    )
    assert is_gone(int(child_pid_path.read_text(encoding="utf-8")))
    assert is_gone(handle.pid)


def test_an_unconfirmed_termination_is_returned_as_a_partial_cancellation(
    monkeypatch, script: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    provider = FakeClaude(script)
    reservation = reserved()
    handle = start_reserved_run(
        provider,
        reservation,
        request(tmp_path, reservation),
        environment=environment(tmp_path, mode="slow"),
    )
    assert isinstance(handle, ProviderRunHandle)
    child_pid_path = tmp_path / "child.pid"
    wait_until(child_pid_path.exists, "the fake CLI never started its child")

    real_terminate = process_module.terminate_process_tree

    def unconfirmed(process, **keywords):  # type: ignore[no-untyped-def]
        # The tree is really stopped, but the check reports what a descendant
        # that outlived the signal looks like: termination was not confirmed.
        real_terminate(process, **keywords)
        return False

    monkeypatch.setattr(process_module, "terminate_process_tree", unconfirmed)
    conclusion = cancel_run(provider, handle)

    assert conclusion.status == "cancelled"
    assert conclusion.process_stopped is False
    assert conclusion.remaining == ("process_termination", "lease_release")
    assert conclusion.events_closed is True
    assert is_gone(int(child_pid_path.read_text(encoding="utf-8")))


def test_reading_the_same_offset_twice_is_deterministic_and_withholds_a_partial_line(
    script: Path, tmp_path: Path
) -> None:
    provider = FakeClaude(script)
    reservation = reserved()
    handle = start_reserved_run(
        provider, reservation, request(tmp_path, reservation), environment=environment(tmp_path)
    )
    assert isinstance(handle, ProviderRunHandle)
    conclusion = conclude_run(provider, handle)

    whole = recover_run(provider, handle)
    again = recover_run(provider, handle)
    tail = recover_run(provider, handle, offset=conclusion.next_offset)
    payload = json.dumps({"kind": "progress", "provider": provider.name})
    with handle.event_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload[: len(payload) // 2])
    partial = recover_run(provider, handle, offset=conclusion.next_offset)

    assert whole.events == again.events
    assert whole.next_offset == again.next_offset == conclusion.next_offset
    assert (tail.events, tail.next_offset) == ((), conclusion.next_offset)
    assert (partial.events, partial.next_offset) == ((), conclusion.next_offset)


def test_the_prompt_and_the_secret_stay_out_of_the_handle_events_result_and_log(
    caplog, script: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    provider = FakeCodex(script)
    reservation = reserved()
    prompt = "prompt-secret-123"
    secret = "api-secret-456"

    with caplog.at_level(logging.DEBUG):
        handle = start_reserved_run(
            provider,
            reservation,
            request(tmp_path, reservation, prompt=prompt),
            environment=environment(tmp_path, mode="leaky", secret=secret),
        )
        assert isinstance(handle, ProviderRunHandle)
        conclusion = conclude_run(provider, handle)

    recorded = handle.event_path.read_text(encoding="utf-8")
    searched = [repr(handle), recorded, repr(conclusion), caplog.text]

    # The prompt did reach the CLI, on standard input only.
    assert (tmp_path / "prompt.txt").read_text(encoding="utf-8") == prompt
    assert conclusion.status == "failed"
    assert [event.kind for event in conclusion.events][-1] == "failed"
    for text in searched:
        assert prompt not in text
        assert secret not in text
