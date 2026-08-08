"""Reservation-bound dispatch: limits, refills, manual targets, isolation."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from heartbeat import agent_dispatch
from heartbeat.agent_contract import default_configuration, validate_configuration
from heartbeat.agent_dispatch import (
    RoleSlots,
    WorkflowHelpers,
    build_plan,
    start_one_run,
    tick_all_projects,
    tick_project,
)
from heartbeat.agent_store import AgentStore
from heartbeat.providers.process import CliProvider, NormalizedLine, ProviderDiagnostic

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake helpers are POSIX shell scripts")

RESERVE_SCRIPT = """#!/bin/sh
rules=".workflow/rules"
mode=$(cat "$rules/reserve-mode" 2>/dev/null || echo ok)
[ "$mode" = unavailable ] && exit 1
[ "$mode" = usage ] && exit 2
count=$(cat "$rules/reserve-count" 2>/dev/null || echo 0)
limit=$(cat "$rules/reserve-limit" 2>/dev/null || echo 99)
count=$((count + 1))
echo "$count" > "$rules/reserve-count"
if [ "$count" -gt "$limit" ]; then exit 1; fi
target=$(sed -n "${count}p" "$rules/reserve-targets" 2>/dev/null)
[ -n "$target" ] || target="TASK-$count"
if ! (set -C; : > "$rules/claimed-$target") 2>/dev/null; then exit 1; fi
prompt=$(cat "$rules/reserve-prompt" 2>/dev/null || echo prompt-body)
printf '{"contractVersion":1,"role":"%s","targetId":"%s","leaseId":"lease-%s",' "$2" "$target" "$target"
printf '"resultPrefix":"RESULT-%s","expiresAt":"2030-01-01T00:00:00Z",' "$target"
printf '"promptVersion":1,"rolePrompt":"%s"}\\n' "$prompt"
"""

CLAIM_SCRIPT = """#!/bin/sh
rules=".workflow/rules"
echo "$1 $2 $3" >> "$rules/claim-log"
code=$(cat "$rules/claim-$1-code" 2>/dev/null || echo 0)
exit "$code"
"""

FIXTURE_CLI = """\
import json
import os
import sys
import time

prompt = sys.stdin.read()
path = os.environ.get("PROVIDER_PROMPT_PATH")
if path:
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(prompt + "\\n")
print(json.dumps({"type": "progress"}), flush=True)
if os.environ.get("PROVIDER_MODE") == "slow":
    while True:
        time.sleep(0.05)
print(json.dumps({"type": "done"}), flush=True)
"""


class FixtureProvider(CliProvider):
    """A ready provider backed by a Python fixture instead of a real CLI."""

    name = "claude"
    executable = sys.executable

    def __init__(self, script: Path) -> None:
        super().__init__()
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
        return ProviderDiagnostic(self.name, "ready", self.executable, version="1.0")


def make_project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    rules = root / ".workflow" / "rules"
    rules.mkdir(parents=True)
    (root / ".workflow" / ".runtime" / "leases").mkdir(parents=True)
    (rules / "wf-reserve.sh").write_text(RESERVE_SCRIPT, encoding="utf-8")
    (rules / "wf-claim.sh").write_text(CLAIM_SCRIPT, encoding="utf-8")
    return root


def control(root: Path, name: str, value: str) -> None:
    (root / ".workflow" / "rules" / name).write_text(value, encoding="utf-8")


def claim_log(root: Path) -> list[str]:
    path = root / ".workflow" / "rules" / "claim-log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def wait_for_text(path: Path, needle: str) -> str:
    """Wait for the child process to have written what the test asserts on."""
    for _ in range(200):
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if needle in text:
                return text
        time.sleep(0.05)
    raise AssertionError(f"{needle} never reached {path}")


def reserve_calls(root: Path) -> int:
    path = root / ".workflow" / "rules" / "reserve-count"
    return int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0


@pytest.fixture
def fixture_script(tmp_path: Path) -> Path:
    script = tmp_path / "provider_cli.py"
    script.write_text(FIXTURE_CLI, encoding="utf-8")
    return script


@pytest.fixture
def provider_factory(fixture_script: Path, tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HEARTBEAT_AGENT_HOME", str(tmp_path / "runtime-home"))
    return lambda name: FixtureProvider(fixture_script)


def configure(store: AgentStore, root: Path, project_id: str, **overrides) -> object:  # type: ignore[no-untyped-def]
    data = default_configuration(project_id, str(root)).to_dict()
    data.update(overrides)
    configuration = validate_configuration(data)
    store.save_configuration(configuration)
    return configuration


def store_at(tmp_path: Path) -> AgentStore:
    return AgentStore(tmp_path / "agent.sqlite3")


def start_slots(store, configuration, root, provider_factory, role="developer", slots=1, **kwargs):  # type: ignore[no-untyped-def]
    rows = []
    for _ in range(slots):
        rows.append(
            start_one_run(
                store,
                configuration,
                role,
                helpers=WorkflowHelpers(root),
                provider_factory=provider_factory,
                **kwargs,
            )
        )
    return rows


def test_started_runs_stop_at_the_smallest_limit(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    roles = default_configuration("project-one", str(root)).to_dict()["roles"]
    roles["developer"]["maxParallel"] = 3
    configuration = configure(store, root, "project-one", projectMaxParallel=5, deviceMaxParallel=5, roles=roles)
    control(root, "reserve-limit", "2")
    store.save_run({
        "runId": "foreign", "projectId": "project-other", "role": "planner", "provider": "claude",
        "state": "running", "targetId": "TASK-X", "leaseId": "lease-x",
    })

    plan = build_plan(
        store,
        configuration,
        [RoleSlots(role="developer", slots=3)],
        helpers=WorkflowHelpers(root),
        provider_factory=provider_factory,
    )
    rows = start_slots(store, configuration, root, provider_factory, slots=3)

    assert plan["deviceRemaining"] == 4
    assert plan["roles"][0]["granted"] == 3
    assert plan["limits"]["roleMaxParallel"]["developer"] == 3
    assert [row["state"] for row in rows] == ["running", "running", "failed"]
    assert rows[-1]["failureStage"] == "reservation"
    assert len(store.list_runs("project-one", states=frozenset({"running"}))) == 2


def test_reservation_exit_codes_become_distinct_failure_stages(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")

    control(root, "reserve-mode", "unavailable")
    unavailable = start_slots(store, configuration, root, provider_factory)[0]
    control(root, "reserve-mode", "usage")
    before = reserve_calls(root)
    usage = start_slots(store, configuration, root, provider_factory)[0]

    assert (unavailable["state"], unavailable["failureStage"]) == ("failed", "reservation")
    assert unavailable["reason"] == "reservation_unavailable"
    assert (usage["state"], usage["failureStage"]) == ("failed", "request_validation")
    assert usage["reason"] == "reservation_usage_error"
    assert unavailable["pid"] is None and usage["pid"] is None
    assert reserve_calls(root) == before


def test_missing_reservation_helper_starts_nothing_and_writes_no_lease(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    (root / ".workflow" / "rules" / "wf-reserve.sh").unlink()
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")

    row = start_slots(store, configuration, root, provider_factory)[0]

    assert (row["state"], row["failureStage"], row["reason"]) == ("failed", "reservation", "reservation_helper_missing")
    assert reserve_calls(root) == 0
    assert list((root / ".workflow" / ".runtime" / "leases").iterdir()) == []


def test_manual_requests_are_refused_before_any_reservation(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    (root / ".workflow" / ".runtime" / "leases" / "TASK-HELD.yml").write_text(
        "expires_at: 2030-01-01T00:00:00Z\n", encoding="utf-8"
    )

    plan = build_plan(
        store,
        configuration,
        [RoleSlots(role="developer", slots=3, manual_targets=("TASK-A", "TASK-A", "bad id", "TASK-HELD"))],
        helpers=WorkflowHelpers(root),
        provider_factory=provider_factory,
    )

    assert plan["roles"][0]["manualTargets"] == ["TASK-A"]
    assert plan["roles"][0]["excluded"] == ["active_lease", "duplicate_target", "invalid_target"]
    assert plan["roles"][0]["granted"] == 1
    assert reserve_calls(root) == 0


def test_a_manual_target_the_helper_does_not_select_is_given_back(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    control(root, "reserve-targets", "TASK-OTHER\n")

    row = start_slots(store, configuration, root, provider_factory, manual_targets=("TASK-WANTED",))[0]

    assert (row["state"], row["reason"]) == ("failed", "manual_target_unavailable")
    assert row["pid"] is None
    assert claim_log(root) == ["release TASK-OTHER lease-TASK-OTHER"]


def test_paused_project_blocks_new_assignment_only(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path, "paused")
    other = make_project(tmp_path, "other")
    store = store_at(tmp_path)
    configure(store, root, "project-one")
    configure(store, other, "project-two")
    running = start_slots(store, agent_dispatch.configuration_of(store, "project-one"), root, provider_factory)[0]
    store.set_paused("project-one", True)
    store.save_intent("project-one", {"intentId": "intent-1", "role": "developer", "mode": "auto", "manualTargets": []})
    store.save_intent("project-two", {"intentId": "intent-2", "role": "developer", "mode": "auto", "manualTargets": []})

    reports = {report.project_id: report for report in tick_all_projects(
        store, helpers_factory=WorkflowHelpers, provider_factory=provider_factory
    )}

    assert reports["project-one"].started == 0
    assert reports["project-two"].started >= 1
    assert store.get_run(running["runId"])["state"] in {"running", "succeeded"}


def test_once_does_not_refill_and_continuous_does(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configure(store, root, "project-one")
    control(root, "reserve-limit", "5")

    once_reports = tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
    store.save_intent("project-one", {"intentId": "intent-1", "role": "developer", "mode": "auto", "manualTargets": []})
    continuous = tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)

    assert once_reports.started == 0
    assert continuous.started == 1


def test_repeating_manual_work_stops_when_the_list_is_done(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configure(store, root, "project-one")
    control(root, "reserve-targets", "TASK-A\nTASK-B\n")
    store.save_intent("project-one", {
        "intentId": "intent-1", "role": "developer", "mode": "manual", "manualTargets": ["TASK-A"],
    })

    first = tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
    for row in store.list_runs("project-one", states=frozenset({"running"})):
        row["state"] = "succeeded"
        store.save_run(row)
    second = tick_project(store, "project-one", helpers_factory=WorkflowHelpers, provider_factory=provider_factory)

    assert first.started == 1
    assert second.started == 0
    assert store.list_intents("project-one") == []


def test_only_one_dispatcher_wins_the_same_target(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one", projectMaxParallel=4, deviceMaxParallel=4)
    control(root, "reserve-targets", "TASK-SHARED\nTASK-SHARED\nTASK-SHARED\nTASK-SHARED\n")
    rows: list[dict] = []
    lock = threading.Lock()

    def start() -> None:
        row = start_one_run(
            store, configuration, "developer", helpers=WorkflowHelpers(root), provider_factory=provider_factory
        )
        with lock:
            rows.append(row)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    started = [row for row in rows if row["state"] == "running"]
    assert len(started) == 1
    assert {row["targetId"] for row in started} == {"TASK-SHARED"}


def test_two_projects_take_turns_on_the_device_slots(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    first_root = make_project(tmp_path, "first")
    second_root = make_project(tmp_path, "second")
    store = store_at(tmp_path)
    configure(store, first_root, "project-one", deviceMaxParallel=2, projectMaxParallel=2)
    configure(store, second_root, "project-two", deviceMaxParallel=2, projectMaxParallel=2)
    for project in ("project-one", "project-two"):
        store.save_intent(project, {"intentId": f"intent-{project}", "role": "developer", "mode": "auto", "manualTargets": []})

    started: dict[str, int] = {"project-one": 0, "project-two": 0}
    for _ in range(3):
        reports = tick_all_projects(store, helpers_factory=WorkflowHelpers, provider_factory=provider_factory)
        for report in reports:
            started[report.project_id] += report.started
            assert report.started <= 2
        for row in store.list_runs(states=frozenset({"running"})):
            row["state"] = "succeeded"
            store.save_run(row)

    assert started["project-one"] > 0
    assert started["project-two"] > 0


def test_one_broken_project_does_not_stop_the_other(tmp_path: Path, provider_factory) -> None:  # type: ignore[no-untyped-def]
    broken_root = make_project(tmp_path, "broken")
    healthy_root = make_project(tmp_path, "healthy")
    store = store_at(tmp_path)
    configure(store, broken_root, "project-broken")
    configure(store, healthy_root, "project-healthy")
    control(broken_root, "reserve-mode", "unavailable")
    for project in ("project-broken", "project-healthy"):
        store.save_intent(project, {"intentId": f"intent-{project}", "role": "developer", "mode": "auto", "manualTargets": []})

    reports = {report.project_id: report for report in tick_all_projects(
        store, helpers_factory=WorkflowHelpers, provider_factory=provider_factory
    )}

    assert reports["project-broken"].started == 0
    assert reports["project-broken"].failures == ["reservation_unavailable"]
    assert reports["project-healthy"].started == 1


def test_role_prompt_reaches_stdin_but_never_the_database_or_events(
    tmp_path: Path, provider_factory, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    store = store_at(tmp_path)
    configuration = configure(store, root, "project-one")
    secret = "role-prompt-secret-do-not-persist"
    control(root, "reserve-prompt", secret)
    delivered = tmp_path / "delivered.txt"
    monkeypatch.setenv("PROVIDER_PROMPT_PATH", str(delivered))

    row = start_slots(store, configuration, root, provider_factory)[0]
    wait_for_text(delivered, secret)
    agent_dispatch.reconcile_run(store, dict(row), helpers=WorkflowHelpers(root), provider_factory=provider_factory)

    recorded, _ = store.read_events("project-one", row["runId"])
    assert row["state"] == "running"
    assert recorded
    assert secret not in json.dumps(store.get_run(row["runId"]))
    assert secret not in json.dumps(recorded)
    assert secret.encode() not in (tmp_path / "agent.sqlite3").read_bytes()
    assert secret not in Path(row["eventPath"]).read_text(encoding="utf-8")


def test_a_failing_agent_store_never_stops_the_heartbeat_loop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from heartbeat import core

    def explode(*args, **kwargs):
        raise RuntimeError("agent runtime is unavailable")

    monkeypatch.setattr("heartbeat.agent_store.AgentStore.__init__", explode)

    core._tick_agent_projects()
