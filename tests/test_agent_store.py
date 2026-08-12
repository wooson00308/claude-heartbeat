"""Project-scoped SQLite state for the agent runtime."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from heartbeat.agent_contract import default_configuration, validate_configuration
from heartbeat.agent_store import AGENT_SCHEMA_VERSION, AgentStore, recommend_device_parallelism


def _configuration(project_id, directory):
    return validate_configuration(default_configuration(project_id, str(directory)).to_dict())


def test_saved_configuration_is_read_back_without_changes(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    configuration = _configuration("project-one", tmp_path)

    store.save_configuration(configuration)

    assert store.get_configuration("project-one") == configuration.to_dict()


def test_project_records_do_not_mix(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    first = _configuration("project-one", first_directory)
    second_data = default_configuration("project-two", str(second_directory)).to_dict()
    second_data["roles"]["architect"]["provider"] = "codex"
    second = validate_configuration(second_data)

    store.save_configuration(first)
    store.save_configuration(second)

    assert store.get_configuration("project-one") == first.to_dict()
    assert store.get_configuration("project-two") == second.to_dict()


def test_device_limit_is_one_global_value_and_has_no_fixed_ceiling(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    first_data = default_configuration("project-one", str(first_directory)).to_dict()
    first_data["deviceMaxParallel"] = 48
    second = _configuration("project-two", second_directory)

    store.save_configuration(validate_configuration(first_data))
    assert store.get_configuration("project-one")["deviceMaxParallel"] == 48

    store.save_configuration(second)
    assert store.get_configuration("project-one")["deviceMaxParallel"] == 16
    assert store.get_configuration("project-two")["deviceMaxParallel"] == 16


def test_device_recommendation_uses_cpu_and_memory_without_becoming_a_cap():
    eight_gib = 8 * 1024 * 1024 * 1024
    recommendation = recommend_device_parallelism(12, eight_gib)

    assert recommendation["recommendedMaxParallel"] == 4
    assert recommendation["cpuAllowance"] == 11
    assert recommendation["memoryAllowance"] == 4


def test_device_capacity_lists_project_ceiling_and_live_use(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    project = _configuration("project-one", tmp_path)
    store.save_configuration(project)
    store.save_run({
        "runId": "run-1", "projectId": "project-one", "role": "developer",
        "provider": "claude", "state": "running",
    })

    capacity = store.device_capacity()

    assert capacity["effectiveMaxParallel"] == 16
    assert capacity["configuredMaxParallel"] == 16
    assert capacity["activeRuns"] == 1
    assert capacity["projects"] == [{
        "projectId": "project-one",
        "projectName": tmp_path.name,
        "projectMaxParallel": 3,
        "activeRuns": 1,
    }]


def test_state_is_project_scoped_and_empty_before_dispatch(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.save_configuration(_configuration("project-one", tmp_path))

    state = store.get_state("project-one")

    assert state["configuration"]["projectId"] == "project-one"
    assert state["queue"] == []
    assert state["runs"] == []
    assert state["errors"] == []
    assert store.get_state("unknown-project")["configuration"] is None


def test_state_freezes_legacy_terminal_runs_at_their_last_persisted_update(tmp_path):
    database = tmp_path / "agent.sqlite3"
    store = AgentStore(database)
    store.save_run({
        "runId": "run-failed", "projectId": "project-one", "role": "developer",
        "provider": "codex", "state": "failed", "startedAt": "2026-08-10T10:00:00Z",
    })
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_runs SET updated_at = ? WHERE run_id = ?",
            ("2026-08-10T10:00:09Z", "run-failed"),
        )

    run = store.get_state("project-one")["runs"][0]

    assert run["finishedAt"] == "2026-08-10T10:00:09Z"
    assert "finishedAt" not in store.get_run("run-failed")


def test_expired_plans_are_pruned_without_removing_live_plans_or_repeat_intents(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.save_plan("project-one", {"planId": "expired", "expiresAt": "2090-08-10T10:00:00Z"})
    store.save_plan("project-one", {"planId": "live", "expiresAt": "2092-08-10T12:00:00Z"})
    store.save_intent("project-one", {
        "intentId": "repeat", "role": "developer", "mode": "auto", "manualTargets": [],
    })

    removed = store.prune_expired_plans("project-one", now="2091-08-10T11:00:00Z")
    queue = store.get_state("project-one")["queue"]
    automation = store.get_state("project-one")["automation"]["roles"]

    assert removed == 1
    assert [item.get("planId") for item in queue] == ["live"]
    assert [item["intentId"] for item in automation] == ["repeat"]


def test_invalid_candidate_does_not_partially_replace_saved_configuration(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    saved = _configuration("project-one", tmp_path)
    store.save_configuration(saved)
    invalid = deepcopy(saved.to_dict())
    invalid["roles"]["developer"]["provider"] = "unknown"

    try:
        validate_configuration(invalid)
    except ValueError:
        pass
    else:  # pragma: no cover - makes the test's premise explicit
        raise AssertionError("invalid configuration was accepted")

    assert store.get_configuration("project-one") == saved.to_dict()


def test_simultaneous_project_updates_keep_valid_database(tmp_path):
    database = tmp_path / "agent.sqlite3"
    projects = []
    for number in range(6):
        directory = tmp_path / f"project-{number}"
        directory.mkdir()
        projects.append(_configuration(f"project-{number}", directory))

    with ThreadPoolExecutor(max_workers=len(projects)) as executor:
        list(executor.map(lambda configuration: AgentStore(database).save_configuration(configuration), projects))

    store = AgentStore(database)
    assert {store.get_configuration(configuration.project_id)["projectId"] for configuration in projects} == {
        configuration.project_id for configuration in projects
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == AGENT_SCHEMA_VERSION


def test_simultaneous_updates_to_one_project_commit_one_complete_configuration(tmp_path):
    database = tmp_path / "agent.sqlite3"
    configurations = []
    for number in range(6):
        data = default_configuration("project-one", str(tmp_path)).to_dict()
        data["roles"]["developer"]["model"] = f"model-{number}"
        configurations.append(validate_configuration(data))

    with ThreadPoolExecutor(max_workers=len(configurations)) as executor:
        list(executor.map(lambda configuration: AgentStore(database).save_configuration(configuration), configurations))

    saved = AgentStore(database).get_configuration("project-one")
    assert saved in [configuration.to_dict() for configuration in configurations]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_schema_v5_preserves_the_v4_model_correction_once(tmp_path):
    database = tmp_path / "agent.sqlite3"
    data = default_configuration("project-one", str(tmp_path)).to_dict()
    data["roles"]["planner"].update({"provider": "codex", "model": "gpt-5.6"})
    data["roles"]["architect"].update({"provider": "codex", "model": "gpt-5.6-terra"})
    data["roles"]["developer"].update({"provider": "claude", "model": "gpt-5.6"})
    store = AgentStore(database)
    store.save_configuration(validate_configuration(data))
    other = default_configuration("project-two", str(tmp_path)).to_dict()
    for role in other["roles"].values():
        role.update({"provider": "codex", "model": "gpt-5.6"})
    store.save_configuration(validate_configuration(other))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=3")

    migrated = AgentStore(database).get_configuration("project-one")
    other_migrated = AgentStore(database).get_configuration("project-two")
    with sqlite3.connect(database) as connection:
        first = connection.execute(
            "SELECT configuration_json, updated_at FROM agent_configurations WHERE project_id='project-one'"
        ).fetchone()
    reread = AgentStore(database).get_configuration("project-one")
    with sqlite3.connect(database) as connection:
        second = connection.execute(
            "SELECT configuration_json, updated_at FROM agent_configurations WHERE project_id='project-one'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert migrated["roles"]["planner"]["model"] == "gpt-5.6-sol"
    assert migrated["roles"]["architect"]["model"] == "gpt-5.6-terra"
    assert migrated["roles"]["developer"]["model"] == "gpt-5.6"
    assert {role["model"] for role in other_migrated["roles"].values()} == {"gpt-5.6-sol"}
    assert json.loads(first[0]) == migrated == reread
    assert first == second
    assert version == 5


def test_schema_v4_moves_repeat_intents_to_automation_and_enables_only_that_project(tmp_path):
    database = tmp_path / "agent.sqlite3"
    store = AgentStore(database)
    store.save_configuration(_configuration("active", tmp_path))
    store.save_configuration(_configuration("inactive", tmp_path))
    with sqlite3.connect(database) as connection:
        payload = json.dumps({
            "intentId": "intent-1", "planId": "intent-1", "role": "developer",
            "mode": "auto", "manualTargets": [], "nextPollAt": "2026-08-11T01:00:00Z",
        })
        connection.execute(
            "INSERT INTO agent_queue(run_id, project_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
            ("intent-1", "active", payload, "2026-08-11T00:00:00Z"),
        )
        connection.execute("PRAGMA user_version=4")

    migrated = AgentStore(database)

    assert migrated.get_configuration("active")["automationEnabled"] is True
    assert migrated.get_configuration("inactive")["automationEnabled"] is False
    assert migrated.list_intents("active")[0]["intentId"] == "intent-1"
    assert migrated.get_state("active")["queue"] == []
    # Opening again is idempotent.
    assert AgentStore(database).list_intents("active") == migrated.list_intents("active")


def test_retention_removes_only_old_terminal_history(tmp_path):
    database = tmp_path / "agent.sqlite3"
    store = AgentStore(database)
    for run_id, state in (("old", "failed"), ("recent", "succeeded"), ("running", "running")):
        store.save_run({
            "runId": run_id, "projectId": "project-one", "role": "developer",
            "provider": "codex", "state": state,
        })
        store.append_events("project-one", run_id, [{"kind": "progress"}])
        store.record_error("project-one", run_id, {"stage": "role_session", "reason": run_id})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_runs SET updated_at='2000-01-01T00:00:00Z' WHERE run_id='old'"
        )
        connection.execute(
            "UPDATE agent_events SET created_at='2000-01-01T00:00:00Z' WHERE run_id='old'"
        )
        connection.execute(
            "UPDATE agent_errors SET created_at='2000-01-01T00:00:00Z' WHERE run_id='old'"
        )

    removed = store.prune_history("project-one", 30)

    assert removed == {"runs": 1, "events": 1, "errors": 1}
    assert {run["runId"] for run in store.list_runs("project-one")} == {"recent", "running"}
