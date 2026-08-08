"""Restart recovery, lease handover, cancellation and retry for started runs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil
import pytest

from heartbeat.agent_dispatch import (
    WorkflowHelpers,
    apply_cancel,
    preview_cancel,
    reconcile_run,
    retry_run,
    start_one_run,
    tick_project,
)
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
