"""Restart recovery, lease handover, cancellation and retry for started runs."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from heartbeat.agent_store import AgentStore
from heartbeat.agent_dispatch import (
    WorkflowHelpers,
    apply_cancel,
    ensure_continuous_dispatcher,
    queue_and_launch_run,
    queue_one_run,
    preview_cancel,
    reconcile_project_runs,
    reconcile_run,
    retry_run,
    serve_queued_run,
    start_one_run,
    stop_continuous_dispatcher_for_service,
    tick_project,
)
from heartbeat.providers.process import process_identity
from tests.test_agent_dispatch import (
    FIXTURE_CLI,
    FixtureProvider,
    claim_log,
    configure,
    control,
    make_project,
    reserve_calls,
    store_at,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake helpers are POSIX shell scripts")


@pytest.fixture
def provider_factory(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """A ready provider whose runs land under this test's own runtime home."""
    script = tmp_path / "provider_cli.py"
    script.write_text(FIXTURE_CLI, encoding="utf-8")
    monkeypatch.setenv("HEARTBEAT_AGENT_HOME", str(tmp_path / "runtime-home"))
    return lambda name: FixtureProvider(script)


def start_slow_run(tmp_path: Path, provider_factory, monkeypatch, **kwargs):  # type: ignore[no-untyped-def]
    """Start one run whose provider stays alive until something stops it."""
    monkeypatch.setenv("PROVIDER_MODE", "slow")
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one", **kwargs)
    row = start_one_run(
        store, configuration, "developer", helpers=WorkflowHelpers(root), provider_factory=provider_factory
    )
    assert row["state"] == "running"
    wait_for_events(Path(row["eventPath"]))
    return store, root, row


def wait_for_events(path: Path, lines: int = 2) -> None:
    for _ in range(100):
        if path.exists() and len(path.read_text(encoding="utf-8").splitlines()) >= lines:
            return
        time.sleep(0.05)


def stop_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    for process in [*root.children(recursive=True), root]:
        try:
            process.kill()
        except psutil.Error:
            continue
    psutil.wait_procs([root], timeout=5)


def test_a_live_process_is_resumed_instead_of_started_again(tmp_path: Path, provider_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)
    reservations_before = reserve_calls(root)

    try:
        report = tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
        current = store.get_run(row["runId"])

        assert report.started == 0
        assert reserve_calls(root) == reservations_before
        assert current["state"] == "running"
        assert current["lastOffset"] > 0
        assert psutil.pid_exists(row["pid"])
    finally:
        stop_tree(row["pid"])


def test_events_resume_from_the_last_offset_without_duplicates(tmp_path: Path, provider_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)

    try:
        tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
        first, cursor = store.read_events("project-one", row["runId"])
        tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
        second, _ = store.read_events("project-one", row["runId"], cursor=cursor)

        assert [event["kind"] for event in first] == ["started", "progress"]
        assert second == []
    finally:
        stop_tree(row["pid"])


def test_a_dead_process_is_concluded_and_its_lease_released(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    row = start_one_run(
        store, configuration, "developer", helpers=WorkflowHelpers(root), provider_factory=provider_factory
    )
    wait_for_events(Path(row["eventPath"]), lines=3)

    reconcile_run(store, dict(row), helpers=WorkflowHelpers(root), provider_factory=provider_factory)
    current = store.get_run(row["runId"])

    assert current["state"] == "succeeded"
    assert current["remaining"] == []
    assert f"release {row['targetId']} {row['leaseId']}" in claim_log(root)


def test_a_detached_worker_owns_supervision_until_the_run_and_lease_are_closed(
    tmp_path: Path, provider_factory
) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    queued = queue_one_run(store, configuration, "developer")

    exit_code = serve_queued_run(
        store,
        queued["runId"],
        helpers_factory=WorkflowHelpers,
        provider_factory=provider_factory,
        poll_seconds=0.01,
    )
    current = store.get_run(queued["runId"])

    assert exit_code == 0
    assert current["state"] == "succeeded"
    assert current["remaining"] == []
    assert current["supervisorPid"] == os.getpid()
    assert f"release {current['targetId']} {current['leaseId']}" in claim_log(root)


def test_the_real_detached_worker_outlives_the_control_call(
    tmp_path: Path, monkeypatch
) -> None:
    root = make_project(tmp_path)
    store = AgentStore(tmp_path / "runtime-home" / "agent-runtime.sqlite3")
    configuration = configure(store, root, "project-one")
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('claude 1.0.0')\n"
        "elif 'auth' in sys.argv:\n"
        "    print(json.dumps({'loggedIn': True}))\n"
        "else:\n"
        "    sys.stdin.read()\n"
        "    print(json.dumps({'type': 'assistant'}), flush=True)\n"
        "    time.sleep(0.2)\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    launched = queue_and_launch_run(store, configuration, "developer")
    assert launched["state"] in {"reserved", "running"}
    assert launched["supervisorPid"] != os.getpid()

    deadline = time.monotonic() + 10
    current = store.get_run(launched["runId"])
    while current["state"] in {"queued", "reserved", "running"} and time.monotonic() < deadline:
        time.sleep(0.05)
        current = store.get_run(launched["runId"])

    assert current["state"] == "succeeded"
    assert [event["kind"] for event in store.read_events("project-one", current["runId"])[0]] == [
        "started", "progress", "completed",
    ]
    assert f"release {current['targetId']} {current['leaseId']}" in claim_log(root)


def test_the_real_repeat_dispatcher_outlives_the_control_call_and_starts_the_next_target(
    tmp_path: Path, monkeypatch
) -> None:
    from tests.test_agent_dispatch import continuous_roles

    root = make_project(tmp_path)
    store = AgentStore(tmp_path / "runtime-home" / "agent-runtime.sqlite3")
    roles = continuous_roles(
        root,
        "project-one",
        pollIntervalSeconds=1,
        executionLimit=2,
    )
    configure(store, root, "project-one", roles=roles)
    control(root, "reserve-targets", "TASK-A\nTASK-B\n")
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('claude 1.0.0')\n"
        "elif 'auth' in sys.argv:\n"
        "    print(json.dumps({'loggedIn': True}))\n"
        "else:\n"
        "    sys.stdin.read()\n"
        "    print(json.dumps({'type': 'assistant'}), flush=True)\n"
        "    time.sleep(0.1)\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HEARTBEAT_AGENT_HOME", str(tmp_path / "runtime-home"))
    store.activate_intent("project-one", {
        "intentId": "intent-1",
        "role": "developer",
        "mode": "auto",
        "manualTargets": [],
        "startedCount": 0,
        "nextPollAt": "2000-01-01T00:00:00Z",
    })

    assert ensure_continuous_dispatcher(store) is True
    try:
        dispatcher = store.get_dispatcher()
        assert dispatcher is not None and dispatcher["pid"] != os.getpid()

        deadline = time.monotonic() + 15
        runs = store.list_runs("project-one")
        while (
            len(runs) < 2
            or any(run["state"] in {"queued", "reserved", "running"} for run in runs)
            or store.list_intents("project-one")
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
            runs = store.list_runs("project-one")

        assert len(runs) == 2
        assert {run["state"] for run in runs} == {"succeeded"}
        assert {run["targetId"] for run in runs} == {"TASK-A", "TASK-B"}
        assert {run["intentId"] for run in runs} == {"intent-1"}
        assert store.list_intents("project-one") == []
        assert reserve_calls(root) == 2
    finally:
        assert stop_continuous_dispatcher_for_service(store) in {"stopped", "none"}


def test_state_refresh_leaves_a_live_detached_supervisor_as_the_single_event_owner(
    tmp_path: Path, provider_factory, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)
    stored = store.get_run(row["runId"])
    stored["supervisorPid"] = os.getpid()
    stored["supervisorIdentity"] = process_identity(os.getpid())
    store.save_run(stored)

    try:
        reconciled = reconcile_project_runs(
            store,
            "project-one",
            helpers_factory=WorkflowHelpers,
            provider_factory=provider_factory,
        )
        current = store.get_run(row["runId"])

        assert reconciled == 0
        assert current["state"] == "running"
        assert current["lastOffset"] == 0
        assert not any(line.startswith("renew ") for line in claim_log(root))
    finally:
        stop_tree(row["pid"])


def test_a_reused_pid_is_never_adopted_as_the_running_job(tmp_path: Path, provider_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)

    try:
        stored = store.get_run(row["runId"])
        stored["processIdentity"] = "0.000001"
        store.save_run(stored)
        reconcile_run(store, stored, helpers=WorkflowHelpers(root), provider_factory=provider_factory)
        current = store.get_run(row["runId"])

        assert current["state"] == "recovery_required"
        assert current["failureStage"] == "recovery"
        assert current["reason"] == "process_identity_mismatch"
        assert psutil.pid_exists(row["pid"])
    finally:
        stop_tree(row["pid"])


def test_a_reserved_row_without_a_process_is_cleaned_up(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configure(store, root, "project-one")
    store.save_run({
        "runId": "half-started", "projectId": "project-one", "role": "developer", "provider": "claude",
        "state": "reserved", "targetId": "TASK-9", "leaseId": "lease-9", "providerRunId": None, "lastOffset": 0,
    })

    tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
    current = store.get_run("half-started")

    assert current["state"] == "failed"
    assert (current["failureStage"], current["reason"]) == ("provider_start", "reserved_without_process")
    assert "release TASK-9 lease-9" in claim_log(root)


def test_losing_lease_ownership_while_running_keeps_the_process(tmp_path: Path, provider_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)
    control(root, "claim-renew-code", "5")

    try:
        reconcile_run(store, store.get_run(row["runId"]), helpers=WorkflowHelpers(root), provider_factory=provider_factory)
        current = store.get_run(row["runId"])

        assert current["state"] == "running"
        assert current["leaseHandedOver"] is True
        assert psutil.pid_exists(row["pid"])
    finally:
        stop_tree(row["pid"])


@pytest.mark.parametrize(("release_code", "expected"), [("5", "succeeded"), ("1", "recovery_required")])
def test_release_exit_codes_separate_cleanup_success_from_failure(
    tmp_path: Path, provider_factory, release_code: str, expected: str
) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    row = start_one_run(
        store, configuration, "developer", helpers=WorkflowHelpers(root), provider_factory=provider_factory
    )
    wait_for_events(Path(row["eventPath"]), lines=3)
    control(root, "claim-release-code", release_code)

    reconcile_run(store, dict(row), helpers=WorkflowHelpers(root), provider_factory=provider_factory)
    current = store.get_run(row["runId"])

    assert current["state"] == expected
    assert current["remaining"] == ([] if expected == "succeeded" else ["lease_release"])


def test_cancel_previews_the_tree_then_stops_it(tmp_path: Path, provider_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)

    preview = preview_cancel(store.get_run(row["runId"]))
    cancelled = apply_cancel(store, store.get_run(row["runId"]), helpers=WorkflowHelpers(root))

    assert preview["processLiveness"] == "running"
    assert preview["cleanup"] == ["process_termination", "event_close", "lease_release"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["remaining"] == []
    assert not psutil.pid_exists(row["pid"]) or psutil.Process(row["pid"]).status() == psutil.STATUS_ZOMBIE
    assert f"release {row['targetId']} {row['leaseId']}" in claim_log(root)


def test_partial_cancel_cleanup_is_not_reported_as_success(tmp_path: Path, provider_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)
    control(root, "claim-release-code", "1")

    try:
        cancelled = apply_cancel(store, store.get_run(row["runId"]), helpers=WorkflowHelpers(root))

        assert cancelled["state"] == "recovery_required"
        assert cancelled["remaining"] == ["lease_release"]
        assert cancelled["failureStage"] == "cleanup"
    finally:
        stop_tree(row["pid"])


def test_retry_keeps_the_failed_run_and_links_the_new_one(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    control(root, "reserve-mode", "unavailable")
    failed = start_one_run(
        store, configuration, "developer", helpers=WorkflowHelpers(root), provider_factory=provider_factory
    )
    (root / ".workflow" / "rules" / "reserve-mode").unlink()

    retried = retry_run(store, configuration, failed, helpers=WorkflowHelpers(root), provider_factory=provider_factory)

    assert failed["state"] == "failed"
    assert store.get_run(failed["runId"])["state"] == "failed"
    assert retried["runId"] != failed["runId"]
    assert retried["previousRunId"] == failed["runId"]
    assert retried["state"] == "running"
    assert json.dumps(retried).count(failed["targetId"] or "none") == 0


def test_a_verified_alive_provider_is_adopted_when_its_supervisor_died(
    tmp_path: Path, provider_factory, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """감독자만 죽은 검증된 세션은 앱 밖으로 빠지는 대신 재조정 주체가 이어받는다."""
    import subprocess

    store, root, row = start_slow_run(tmp_path, provider_factory, monkeypatch)
    corpse = subprocess.Popen([sys.executable, "-c", "pass"])
    corpse.wait()
    stored = store.get_run(row["runId"])
    stored["supervisorPid"] = corpse.pid
    stored["supervisorIdentity"] = "worker-at"
    store.save_run(stored)

    try:
        reconcile_run(
            store, store.get_run(row["runId"]), helpers=WorkflowHelpers(root), provider_factory=provider_factory
        )
        current = store.get_run(row["runId"])

        assert current["state"] == "running"
        assert (current["supervisorPid"], current["supervisorIdentity"]) == (None, None)
        assert psutil.pid_exists(row["pid"])
    finally:
        stop_tree(row["pid"])
