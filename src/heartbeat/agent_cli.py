"""JSON-only command handlers for ``heartbeat agent``."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from heartbeat.agent_contract import (
    ROLES,
    AgentContractError,
    contract_description,
    envelope,
    require_api_version,
    validate_configuration,
)
from heartbeat.agent_store import AgentStore

from heartbeat import agent_dispatch
from heartbeat.agent_dispatch import RoleSlots, WorkflowHelpers, configuration_of

EXECUTION_COMMANDS = (
    "plan.read", "run.start", "project.pause", "project.resume", "run.cancel", "run.retry",
    "logs.read", "provider.diagnose",
)


def _read_json(stream: TextIO) -> dict[str, Any]:
    try:
        value = json.load(stream)
    except json.JSONDecodeError as error:
        raise AgentContractError("invalid_json", "stdin must contain one JSON object") from error
    if not isinstance(value, dict):
        raise AgentContractError("invalid_request", "stdin must contain a JSON object")
    return value


def _write_json(value: dict[str, Any], stream: TextIO) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _project_id(request: dict[str, Any], request_id: str) -> str:
    project_id = request.get("projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        raise AgentContractError("invalid_request", "projectId must be a non-empty string", request_id=request_id)
    return project_id


def _configuration_or_reject(store: AgentStore, project_id: str, request_id: str):
    configuration = configuration_of(store, project_id)
    if configuration is None:
        raise AgentContractError("project_not_configured", "projectId has no stored configuration", request_id=request_id)
    return configuration


def _role_requests(request: dict[str, Any], request_id: str) -> list[RoleSlots]:
    roles = request.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise AgentContractError("invalid_request", "roles must be a non-empty object", request_id=request_id)
    requests = []
    for role, value in sorted(roles.items()):
        if role not in ROLES or not isinstance(value, dict):
            raise AgentContractError("invalid_request", "roles names one unknown role", request_id=request_id)
        slots = value.get("slots", 1)
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise AgentContractError("invalid_request", f"roles.{role}.slots must be a non-negative integer", request_id=request_id)
        targets = value.get("targets", [])
        if not isinstance(targets, list) or any(not isinstance(target, str) for target in targets):
            raise AgentContractError("invalid_request", f"roles.{role}.targets must be strings", request_id=request_id)
        requests.append(RoleSlots(role=role, slots=slots, manual_targets=tuple(targets)))
    return requests


def _first_stage(data: dict[str, Any]) -> str:
    """Name the stage a failed execution stopped at, from what it reported."""
    failures = data.get("failures")
    if isinstance(failures, list) and failures and isinstance(failures[0], dict):
        return failures[0].get("stage") or "reservation"
    run = data.get("run")
    if isinstance(run, dict) and run.get("failureStage"):
        return run["failureStage"]
    return "reservation"


def _run_or_reject(store: AgentStore, request: dict[str, Any], request_id: str, project_id: str) -> dict[str, Any]:
    run_id = request.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise AgentContractError("invalid_request", "runId must be a non-empty string", request_id=request_id)
    row = store.get_run(run_id)
    if row is None or row["projectId"] != project_id:
        raise AgentContractError("run_not_found", "runId is not a run of this project", request_id=request_id)
    return row


def _execution_command(
    command: str,
    request: dict[str, Any],
    request_id: str,
    store: AgentStore,
) -> tuple[str, dict[str, Any]]:
    """Run one execution command and return its outcome and response data."""
    project_id = _project_id(request, request_id)
    if command == "project.pause" or command == "project.resume":
        configuration = store.set_paused(project_id, command == "project.pause")
        if configuration is None:
            raise AgentContractError("project_not_configured", "projectId has no stored configuration", request_id=request_id)
        return "success", {"configuration": configuration}

    configuration = _configuration_or_reject(store, project_id, request_id)
    helpers = WorkflowHelpers(Path(configuration.working_directory))

    if command == "provider.diagnose":
        providers = sorted({policy.provider for policy in configuration.roles.values()})
        diagnostics = [agent_dispatch.build_provider(name).diagnose() for name in providers]
        return "success", {
            "providers": [
                {"provider": item.provider, "status": item.status, "version": item.version} for item in diagnostics
            ]
        }

    if command == "plan.read":
        plan = agent_dispatch.build_plan(
            store,
            configuration,
            _role_requests(request, request_id),
            helpers=helpers,
            provider_factory=agent_dispatch.build_provider,
        )
        store.save_plan(project_id, plan)
        return "success", {"plan": plan}

    if command == "run.start":
        return _start_from_plan(request, request_id, store, configuration, helpers)

    if command == "logs.read":
        row = _run_or_reject(store, request, request_id, project_id)
        cursor = request.get("cursor", 0)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise AgentContractError("invalid_request", "cursor must be a non-negative integer", request_id=request_id)
        events, next_cursor = store.read_events(project_id, row["runId"], cursor=cursor)
        return "success", {"runId": row["runId"], "events": events, "nextCursor": next_cursor}

    if command == "run.cancel":
        row = _run_or_reject(store, request, request_id, project_id)
        if request.get("confirmed") is not True:
            return "success", {"preview": agent_dispatch.preview_cancel(row)}
        cancelled = agent_dispatch.apply_cancel(store, row, helpers=helpers)
        outcome = "success" if not cancelled["remaining"] else "partial_success"
        return outcome, {"run": cancelled}

    if command == "run.retry":
        previous = _run_or_reject(store, request, request_id, project_id)
        row = agent_dispatch.retry_run(
            store, configuration, previous, helpers=helpers, provider_factory=agent_dispatch.build_provider
        )
        outcome = "success" if row["state"] == "running" else "failure"
        return outcome, {"run": row, "previousRunId": previous["runId"]}

    raise AgentContractError("unsupported_command", "agent command is not implemented", request_id=request_id)


def _start_from_plan(
    request: dict[str, Any],
    request_id: str,
    store: AgentStore,
    configuration,
    helpers,
) -> tuple[str, dict[str, Any]]:
    """Start only the plan the caller confirmed, and only if nothing moved."""
    plan_id = request.get("planId")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise AgentContractError("invalid_request", "planId must be a non-empty string", request_id=request_id)
    if request.get("confirmed") is not True:
        raise AgentContractError("confirmation_required", "run.start needs an explicit confirmation", request_id=request_id)
    plan = store.take_plan(configuration.project_id, plan_id)
    if plan is None:
        return "failure", {"planId": plan_id, "reason": "plan_not_found", "stage": "request_validation"}
    if plan["expiresAt"] <= agent_dispatch.utc_now():
        return "failure", {"planId": plan_id, "reason": "plan_expired", "stage": "request_validation"}
    if plan["revision"] != agent_dispatch.runtime_revision(store, configuration):
        return "failure", {"planId": plan_id, "reason": "runtime_changed", "stage": "request_validation"}

    started: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for role_plan in plan["roles"]:
        remaining_targets = list(role_plan["manualTargets"])
        for _ in range(role_plan["granted"]):
            row = agent_dispatch.start_one_run(
                store,
                configuration,
                role_plan["role"],
                helpers=helpers,
                provider_factory=agent_dispatch.build_provider,
                manual_targets=tuple(remaining_targets),
            )
            if row["state"] == "running":
                started.append(row)
                if remaining_targets and row["targetId"] in remaining_targets:
                    remaining_targets.remove(row["targetId"])
            else:
                failures.append({"role": role_plan["role"], "stage": row["failureStage"], "reason": row["reason"]})
                break
        if role_plan["executionMode"] == "continuous":
            store.save_intent(configuration.project_id, {
                "intentId": uuid.uuid4().hex,
                "role": role_plan["role"],
                "mode": "manual" if role_plan["manualTargets"] else "auto",
                "manualTargets": remaining_targets,
            })

    if started and failures:
        return "partial_success", {"started": started, "failures": failures}
    if failures:
        return "failure", {"started": started, "failures": failures}
    return "success", {"started": started, "failures": failures}


def run_agent_command(
    command: str,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    store: AgentStore | None = None,
) -> int:
    """Run one implemented command and keep stdout to one JSON response."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    request_id = str(uuid.uuid4())
    try:
        if command == "contract.read":
            description = contract_description()
            description["implementedCommands"] = sorted(
                [*description["implementedCommands"], *EXECUTION_COMMANDS]
            )
            description["reservedCommands"] = []
            _write_json(envelope(command, request_id, outcome="success", data=description), output_stream)
            return 0

        request = _read_json(input_stream)
        request_id = require_api_version(request)
        runtime_store = store or AgentStore()
        if command in {"config.validate", "config.write"}:
            configuration = validate_configuration(request.get("configuration"), request_id=request_id)
            if command == "config.write":
                runtime_store.save_configuration(configuration)
            _write_json(
                envelope(command, request_id, outcome="success", data={"configuration": configuration.to_dict()}),
                output_stream,
            )
            return 0
        if command == "config.read":
            configuration = runtime_store.get_configuration(_project_id(request, request_id))
            _write_json(envelope(command, request_id, outcome="success", data={"configuration": configuration}), output_stream)
            return 0
        if command == "state.read":
            state = runtime_store.get_state(_project_id(request, request_id))
            _write_json(envelope(command, request_id, outcome="success", data=state), output_stream)
            return 0
        if command in EXECUTION_COMMANDS:
            outcome, data = _execution_command(command, request, request_id, runtime_store)
            if outcome == "failure":
                _write_json(
                    envelope(
                        command,
                        request_id,
                        outcome="failure",
                        data=data,
                        error={
                            "stage": data.get("stage") or _first_stage(data),
                            "code": data.get("reason") or "execution_failed",
                            "message": "the command did not start the requested work",
                        },
                    ),
                    output_stream,
                )
                return 1
            _write_json(envelope(command, request_id, outcome=outcome, data=data), output_stream)  # type: ignore[arg-type]
            return 0
        raise AgentContractError("unsupported_command", "agent command is not implemented", request_id=request_id)
    except AgentContractError as error:
        _write_json(
            envelope(
                command,
                error.request_id or request_id,
                outcome="failure",
                error={"stage": "request_validation", "code": error.code, "message": str(error)},
            ),
            output_stream,
        )
        return 2
