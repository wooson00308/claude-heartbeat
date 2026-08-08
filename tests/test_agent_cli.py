"""`heartbeat agent` keeps stdout a parseable JSON contract."""

from __future__ import annotations

import io
import json

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
        ("run.start", json.dumps({"apiVersion": "1", "requestId": "r"}), "unsupported_command"),
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
    configuration["deviceMaxParallel"] = 17

    exit_code, response = _invoke("config.write", _request(configuration), store)

    assert exit_code == 2
    assert response["error"]["code"] == "device_limit_exceeded"
    assert store.get_configuration("project-one") is None


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
