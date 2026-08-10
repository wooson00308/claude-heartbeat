"""`heartbeat agent` keeps stdout a parseable JSON contract."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from heartbeat.agent_cli import run_agent_command
from heartbeat.agent_contract import default_configuration
from heartbeat.agent_store import AgentStore


def _request(configuration, request_id="request-1"):
    return json.dumps({"apiVersion": "1", "requestId": request_id, "configuration": configuration})


def _invoke(command, payload="", store=None):
    output = io.StringIO()
    exit_code = run_agent_command(command, input_stream=io.StringIO(payload), output_stream=output, store=store)
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    return exit_code, json.loads(lines[0])


def test_contract_needs_no_input_and_returns_machine_readable_capabilities():
    exit_code, response = _invoke("contract.read")

    assert exit_code == 0
    assert response["outcome"] == "success"
    assert response["command"] == "contract.read"
    assert response["data"]["supportedApiVersions"] == ["1"]


def test_validate_is_pure_and_write_then_read_round_trips(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    configuration = default_configuration("project-one", str(tmp_path)).to_dict()

    validate_code, validated = _invoke("config.validate", _request(configuration), store)
    write_code, written = _invoke("config.write", _request(configuration), store)
    read_code, read = _invoke(
        "config.read",
        json.dumps({"apiVersion": "1", "requestId": "request-2", "projectId": "project-one"}),
        store,
    )

    assert validate_code == write_code == read_code == 0
    assert validated["data"]["configuration"] == configuration
    assert written["data"]["configuration"] == configuration
    assert read["data"]["configuration"] == configuration


def test_state_returns_only_the_requested_projects_empty_runtime_data(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    configuration = default_configuration("project-one", str(tmp_path)).to_dict()
    _invoke("config.write", _request(configuration), store)

    exit_code, response = _invoke(
        "state.read",
        json.dumps({"apiVersion": "1", "requestId": "request-3", "projectId": "project-one"}),
        store,
    )

    assert exit_code == 0
    assert response["data"]["configuration"]["projectId"] == "project-one"
    assert response["data"]["queue"] == response["data"]["runs"] == response["data"]["errors"] == []


@pytest.mark.parametrize(
    ("command", "payload", "code"),
    [
        ("config.write", "not-json", "invalid_json"),
        ("config.write", json.dumps({"apiVersion": "2", "requestId": "r", "configuration": {}}), "unsupported_api_version"),
        ("config.read", json.dumps({"apiVersion": "1", "requestId": "r"}), "invalid_request"),
        ("plan.write", json.dumps({"apiVersion": "1", "requestId": "r"}), "unsupported_command"),
    ],
)
def test_bad_requests_return_failure_envelopes_without_tracebacks(tmp_path, command, payload, code):
    exit_code, response = _invoke(command, payload, AgentStore(tmp_path / "agent.sqlite3"))

    assert exit_code == 2
    assert response["outcome"] == "failure"
    assert response["error"]["stage"] == "request_validation"
    assert response["error"]["code"] == code


def test_bad_write_does_not_store_a_configuration(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    configuration = default_configuration("project-one", str(tmp_path)).to_dict()
    configuration["deviceMaxParallel"] = 0

    exit_code, response = _invoke("config.write", _request(configuration), store)

    assert exit_code == 2
    assert response["error"]["code"] == "invalid_request"
    assert store.get_configuration("project-one") is None


def test_device_override_above_the_old_fixed_ceiling_round_trips(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    configuration = default_configuration("project-one", str(tmp_path)).to_dict()
    configuration["deviceMaxParallel"] = 48

    write_code, written = _invoke("config.write", _request(configuration), store)

    assert write_code == 0
    assert written["data"]["configuration"]["deviceMaxParallel"] == 48
    assert written["data"]["deviceCapacity"]["effectiveMaxParallel"] == 48


def test_rejected_secret_is_absent_from_response_and_database(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    configuration = default_configuration("project-one", str(tmp_path)).to_dict()
    secret = "agent-test-token-must-not-persist"
    configuration["apiToken"] = secret

    exit_code, response = _invoke("config.write", _request(configuration), store)

    assert exit_code == 2
    assert secret not in json.dumps(response)
    assert store.get_configuration("project-one") is None
    database = tmp_path / "agent.sqlite3"
    assert not database.exists() or secret.encode() not in database.read_bytes()


class _ExecutionProject:
    """One configured project with fake workflow helpers and a fake provider."""

    def __init__(self, tmp_path, monkeypatch):
        from heartbeat import agent_dispatch
        from tests.test_agent_dispatch import FixtureProvider, FIXTURE_CLI, configure, make_project

        self.root = make_project(tmp_path)
        self.store = AgentStore(tmp_path / "agent.sqlite3")
        script = tmp_path / "provider_cli.py"
        script.write_text(FIXTURE_CLI, encoding="utf-8")
        monkeypatch.setenv("HEARTBEAT_AGENT_HOME", str(tmp_path / "runtime-home"))
        monkeypatch.setattr(agent_dispatch, "build_provider", lambda name: FixtureProvider(script))
        def launch_in_test_process(store, run_id):
            row = store.get_run(run_id)
            configuration = agent_dispatch.configuration_of(store, row["projectId"])
            agent_dispatch._start_run_row(
                store,
                configuration,
                row,
                helpers=agent_dispatch.WorkflowHelpers(self.root),
                provider_factory=agent_dispatch.build_provider,
            )
            return True
        monkeypatch.setattr(agent_dispatch, "launch_run_worker", launch_in_test_process)
        self.configuration = configure(self.store, self.root, "project-one")

    def call(self, command, **request):
        payload = json.dumps({"apiVersion": "1", "requestId": "request-x", "projectId": "project-one", **request})
        return _invoke(command, payload, self.store)


@pytest.fixture
def execution_project(tmp_path, monkeypatch):
    if __import__("os").name == "nt":
        pytest.skip("the fake helpers are POSIX shell scripts")
    return _ExecutionProject(tmp_path, monkeypatch)


def test_contract_reports_the_execution_commands_as_implemented():
    _, response = _invoke("contract.read")

    implemented = response["data"]["implementedCommands"]
    assert set(implemented) >= {
        "plan.read", "run.start", "project.pause", "project.resume", "run.cancel", "run.retry",
        "logs.read", "provider.diagnose",
    }
    assert response["data"]["reservedCommands"] == []


def test_plan_reads_limits_and_diagnostics_without_starting_anything(execution_project):
    from tests.test_agent_dispatch import reserve_calls

    exit_code, response = execution_project.call("plan.read", roles={"developer": {"slots": 2}})

    plan = response["data"]["plan"]
    assert exit_code == 0
    assert plan["roles"][0]["granted"] == 1
    assert plan["roles"][0]["excluded"] == ["limit_reached"]
    assert plan["roles"][0]["diagnostic"]["status"] == "ready"
    assert plan["limits"]["projectMaxParallel"] == 3
    assert reserve_calls(execution_project.root) == 0
    assert execution_project.store.list_runs("project-one") == []


def test_start_needs_the_same_plan_and_an_explicit_confirmation(execution_project):
    _, planned = execution_project.call("plan.read", roles={"developer": {"slots": 1}})
    plan_id = planned["data"]["plan"]["planId"]

    unconfirmed = execution_project.call("run.start", planId=plan_id)
    unknown = execution_project.call("run.start", planId="not-a-plan", confirmed=True)
    started = execution_project.call("run.start", planId=plan_id, confirmed=True)
    replayed = execution_project.call("run.start", planId=plan_id, confirmed=True)

    assert unconfirmed[1]["error"]["code"] == "confirmation_required"
    assert unknown[1]["error"]["code"] == "plan_not_found"
    assert started[0] == 0
    assert len(started[1]["data"]["started"]) == 1
    assert replayed[1]["error"]["code"] == "plan_not_found"
    assert execution_project.store.list_intents("project-one") == []


def test_continuous_start_persists_one_role_instruction_and_starts_the_dispatcher(
    execution_project, monkeypatch
):
    from tests.test_agent_dispatch import continuous_roles

    configuration = default_configuration("project-one", str(execution_project.root)).to_dict()
    configuration["roles"] = continuous_roles(
        execution_project.root,
        "project-one",
        pollIntervalSeconds=30,
        executionLimit=3,
    )
    _invoke("config.write", _request(configuration), execution_project.store)
    started_dispatcher = []
    monkeypatch.setattr(
        "heartbeat.agent_dispatch.ensure_continuous_dispatcher",
        lambda store: started_dispatcher.append(store.database_path) or True,
    )
    _, planned = execution_project.call("plan.read", roles={"developer": {"slots": 1}})

    exit_code, response = execution_project.call(
        "run.start",
        planId=planned["data"]["plan"]["planId"],
        confirmed=True,
    )
    intents = execution_project.store.list_intents("project-one")

    assert exit_code == 0
    assert response["outcome"] == "success"
    assert len(intents) == 1
    assert intents[0]["role"] == "developer"
    assert intents[0]["startedCount"] == 1
    assert intents[0]["nextPollAt"].endswith("Z")
    assert started_dispatcher == [execution_project.store.database_path]


def test_saving_once_mode_stops_an_existing_repeat_instruction(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = AgentStore(tmp_path / "agent.sqlite3")
    continuous = default_configuration("project-one", str(root)).to_dict()
    continuous["roles"]["developer"]["executionMode"] = "continuous"
    monkeypatch.setattr("heartbeat.agent_dispatch.ensure_continuous_dispatcher", lambda store: True)
    _invoke("config.write", _request(continuous), store)
    store.save_intent("project-one", {
        "intentId": "intent-1", "role": "developer", "mode": "auto", "manualTargets": [],
    })
    once = default_configuration("project-one", str(root)).to_dict()

    exit_code, _ = _invoke("config.write", _request(once), store)

    assert exit_code == 0
    assert store.list_intents("project-one") == []


def test_state_read_reconciles_a_finished_process_before_reporting_capacity(execution_project):
    from tests.test_agent_recovery import wait_for_events

    _, planned = execution_project.call("plan.read", roles={"developer": {"slots": 1}})
    execution_project.call("run.start", planId=planned["data"]["plan"]["planId"], confirmed=True)
    row = execution_project.store.list_runs("project-one")[0]
    wait_for_events(Path(row["eventPath"]), lines=3)

    exit_code, response = execution_project.call("state.read")
    current = execution_project.store.get_run(row["runId"])

    assert exit_code == 0
    assert response["data"]["runs"][0]["state"] == "succeeded"
    assert current["state"] == "succeeded"


def test_start_refuses_a_plan_whose_runtime_moved(execution_project):
    _, planned = execution_project.call("plan.read", roles={"developer": {"slots": 1}})
    execution_project.store.save_run({
        "runId": "other", "projectId": "project-one", "role": "planner", "provider": "claude",
        "state": "running", "targetId": "TASK-Z", "leaseId": "lease-z",
    })

    exit_code, response = execution_project.call(
        "run.start", planId=planned["data"]["plan"]["planId"], confirmed=True
    )

    assert exit_code == 1
    assert response["outcome"] == "failure"
    assert response["error"]["code"] == "runtime_changed"
    assert [run["runId"] for run in execution_project.store.list_runs("project-one")] == ["other"]


def test_pause_and_resume_only_flip_the_stored_flag(execution_project):
    paused = execution_project.call("project.pause")
    resumed = execution_project.call("project.resume")

    assert paused[1]["data"]["configuration"]["paused"] is True
    assert resumed[1]["data"]["configuration"]["paused"] is False
    assert resumed[1]["data"]["configuration"]["roles"] == execution_project.configuration.to_dict()["roles"]


def test_logs_read_returns_redacted_events_and_a_next_cursor(execution_project):
    from heartbeat import agent_dispatch

    _, planned = execution_project.call("plan.read", roles={"developer": {"slots": 1}})
    execution_project.call("run.start", planId=planned["data"]["plan"]["planId"], confirmed=True)
    run_id = execution_project.store.list_runs("project-one")[0]["runId"]
    agent_dispatch.tick_project(
        execution_project.store,
        "project-one",
        provider_factory=agent_dispatch.build_provider,
    )

    exit_code, response = execution_project.call("logs.read", runId=run_id, cursor=0)

    assert exit_code == 0
    assert [event["kind"] for event in response["data"]["events"]][0] == "started"
    assert response["data"]["nextCursor"] > 0
    assert "prompt" not in json.dumps(response["data"]["events"])


def test_provider_diagnose_reports_each_configured_provider(execution_project):
    exit_code, response = execution_project.call("provider.diagnose")

    assert exit_code == 0
    assert [item["status"] for item in response["data"]["providers"]] == ["ready"]


def test_cancel_previews_before_it_applies(execution_project):
    _, planned = execution_project.call("plan.read", roles={"developer": {"slots": 1}})
    execution_project.call("run.start", planId=planned["data"]["plan"]["planId"], confirmed=True)
    run_id = execution_project.store.list_runs("project-one")[0]["runId"]

    preview = execution_project.call("run.cancel", runId=run_id)
    applied = execution_project.call("run.cancel", runId=run_id, confirmed=True)

    assert preview[1]["data"]["preview"]["cleanup"] == ["process_termination", "event_close", "lease_release"]
    assert execution_project.store.get_run(run_id)["state"] in {"cancelled", "recovery_required"}
    assert applied[1]["outcome"] in {"success", "partial_success"}
