"""The versioned JSON boundary must reject bad input before state changes."""

from __future__ import annotations

from copy import deepcopy

import pytest

from heartbeat import __version__
from heartbeat.agent_contract import (
    API_VERSION,
    AgentContractError,
    contract_description,
    default_configuration,
    envelope,
    validate_configuration,
)


@pytest.fixture
def configuration(tmp_path):
    return default_configuration("project-one", str(tmp_path)).to_dict()


def test_default_configuration_has_conservative_limits(configuration):
    assert configuration["projectMaxParallel"] == 3
    assert configuration["deviceMaxParallel"] == 16
    assert {role["maxParallel"] for role in configuration["roles"].values()} == {1}


def test_configuration_round_trips_through_normalization(configuration):
    assert validate_configuration(configuration).to_dict() == configuration


def test_project_id_and_actual_directory_are_required(configuration):
    without_id = deepcopy(configuration)
    del without_id["projectId"]
    without_directory = deepcopy(configuration)
    del without_directory["workingDirectory"]

    with pytest.raises(AgentContractError, match="projectId"):
        validate_configuration(without_id)
    with pytest.raises(AgentContractError, match="workingDirectory"):
        validate_configuration(without_directory)


def test_missing_working_directory_is_rejected(configuration, tmp_path):
    configuration["workingDirectory"] = str(tmp_path / "missing")

    with pytest.raises(AgentContractError) as error:
        validate_configuration(configuration)

    assert error.value.code == "working_directory_not_found"


def test_device_cap_accepts_an_explicit_user_override(configuration):
    configuration["deviceMaxParallel"] = 64

    assert validate_configuration(configuration).device_max_parallel == 64


def test_project_limit_may_exceed_the_shared_device_limit_but_role_may_not_exceed_project(configuration):
    configuration["projectMaxParallel"] = 17
    assert validate_configuration(configuration).project_max_parallel == 17

    configuration["projectMaxParallel"] = 3
    configuration["roles"]["developer"]["maxParallel"] = 4
    with pytest.raises(AgentContractError) as role_error:
        validate_configuration(configuration)
    assert role_error.value.code == "role_limit_exceeded"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("provider", "dream", "unsupported_provider"),
        ("executionMode", "daemon", "unsupported_execution_mode"),
    ],
)
def test_unknown_provider_or_execution_mode_is_rejected(configuration, field, value, code):
    configuration["roles"]["planner"][field] = value

    with pytest.raises(AgentContractError) as error:
        validate_configuration(configuration)

    assert error.value.code == code


def test_roles_must_be_the_three_workflow_roles_exactly_once(configuration):
    configuration["roles"].pop("architect")
    configuration["roles"]["reviewer"] = configuration["roles"]["planner"]

    with pytest.raises(AgentContractError) as error:
        validate_configuration(configuration)

    assert error.value.code == "invalid_roles"


def test_sensitive_or_unknown_configuration_fields_are_not_accepted(configuration):
    configuration["prompt"] = "must not persist"

    with pytest.raises(AgentContractError) as error:
        validate_configuration(configuration)

    assert error.value.code == "unknown_configuration_field"


def test_contract_advertises_versions_commands_and_defaults():
    description = contract_description()

    assert description["supportedApiVersions"] == [API_VERSION]
    assert description["runtimeVersion"] == __version__
    assert "config.write" in description["implementedCommands"]
    assert "run.start" in description["implementedCommands"]
    assert "run.start" not in description["reservedCommands"]
    assert description["defaults"] == {
        "automationEnabled": False,
        "roleMaxParallel": 1,
        "projectMaxParallel": 3,
        "deviceMaxParallel": 16,
        "deviceMaxParallelIsRecommendationFallback": True,
        "eventRetentionDays": 30,
    }
    assert "recovery_required" in description["runStates"]
    assert "provider_start" in description["failureStages"]


def test_response_envelope_keeps_request_identity_and_failure_stage():
    response = envelope(
        "config.write",
        "request-42",
        outcome="failure",
        error={"stage": "request_validation", "code": "invalid_request", "message": "bad request"},
    )

    assert response["apiVersion"] == API_VERSION
    assert response["runtimeVersion"] == __version__
    assert response["requestId"] == "request-42"
    assert response["command"] == "config.write"
    assert response["outcome"] == "failure"
    assert response["error"]["stage"] == "request_validation"
