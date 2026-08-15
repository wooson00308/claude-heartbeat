"""JSON-only command handlers for ``heartbeat agent``."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from heartbeat import agent_contract
from heartbeat.agent_contract import (
    AGENT_COMMANDS,
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
UPDATE_COMMANDS = ("update.plan", "update.apply")
# 동의 명령은 저장된 프로젝트 설정을 요구하지 않는다. 사용자가 자동 배정을 처음 켜는
# 순간에는 아직 설정이 저장되기 전일 수 있어, 설정을 먼저 요구하면 동의를 남길 방법이 없다.
CONSENT_COMMANDS = ("consent.read", "consent.grant", "consent.revoke")


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


def _consent_payload(project_id: str, record: dict[str, Any] | None) -> dict[str, Any]:
    """Shape the one consent view every consent command answers with.

    ``granted`` says a record exists at all; ``valid`` says that record still
    meets the notice version the runtime requires now. Raising the required
    version therefore turns an old consent invalid without deleting it.
    """
    notice_version = record["noticeVersion"] if record is not None else None
    return {
        "consent": {
            "projectId": project_id,
            "granted": record is not None,
            "valid": agent_dispatch.consent_is_valid(record),
            "noticeVersion": notice_version,
            "grantedAt": record["grantedAt"] if record is not None else None,
            "requiredNoticeVersion": agent_contract.REQUIRED_NOTICE_VERSION,
        }
    }


def _consent_command(
    command: str,
    request: dict[str, Any],
    request_id: str,
    store: AgentStore,
) -> dict[str, Any]:
    """Read, grant, or revoke one project's execution consent."""
    project_id = _project_id(request, request_id)
    if command == "consent.read":
        return _consent_payload(project_id, store.get_consent(project_id))

    if command == "consent.revoke":
        store.delete_consent(project_id)
        return _consent_payload(project_id, store.get_consent(project_id))

    notice_version = request.get("noticeVersion")
    if isinstance(notice_version, bool) or not isinstance(notice_version, int):
        raise AgentContractError("invalid_request", "noticeVersion must be an integer", request_id=request_id)
    if notice_version < agent_contract.REQUIRED_NOTICE_VERSION:
        raise AgentContractError(
            "consent_notice_outdated",
            f"noticeVersion must be at least {agent_contract.REQUIRED_NOTICE_VERSION}",
            request_id=request_id,
        )
    return _consent_payload(project_id, store.save_consent(project_id, notice_version))


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


def _update_command(
    command: str,
    request: dict[str, Any],
    request_id: str,
    store: AgentStore,
) -> tuple[str, dict[str, Any]]:
    """Plan or apply one runtime update. These are device-wide, not per project."""
    from heartbeat.runtime_management import apply_update, plan_update

    install_root = _required_path(request, "installRoot", request_id)
    version_dir = _required_path(request, "versionDir", request_id)
    if command == "update.plan":
        plan = plan_update(install_root, version_dir, store=store)
        outcome = "success" if plan.result == "ready" else "failure"
        data = plan.to_dict()
        return outcome, {**data, "stage": None if outcome == "success" else "request_validation",
                         "reason": None if outcome == "success" else plan.result}

    plan_id = request.get("planId")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise AgentContractError("invalid_request", "planId must be a non-empty string", request_id=request_id)
    applied = apply_update(
        install_root, version_dir, store=store, plan_id=plan_id, confirmed=request.get("confirmed") is True,
    )
    data = applied.to_dict()
    if applied.result == "success":
        return "success", data
    if applied.result == "partial_success":
        return "partial_success", data
    return "failure", {**data, "stage": "request_validation" if applied.result != "failure" else "cleanup",
                       "reason": applied.result}


def _required_path(request: dict[str, Any], name: str, request_id: str) -> Path:
    value = request.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AgentContractError("invalid_request", f"{name} must be a non-empty string", request_id=request_id)
    return Path(value)


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
        if command == "project.resume":
            agent_dispatch.ensure_continuous_dispatcher(store)
        return "success", {"configuration": configuration}

    configuration = _configuration_or_reject(store, project_id, request_id)
    helpers = WorkflowHelpers(Path(configuration.working_directory))

    if command == "provider.diagnose":
        providers = sorted({policy.provider for policy in configuration.roles.values()})
        diagnostics = [agent_dispatch.build_provider(name).diagnose() for name in providers]
        return "success", {
            "providers": [
                {
                    "provider": item.provider,
                    "status": item.status,
                    "version": item.version,
                    "modelCatalog": item.model_catalog.to_dict(),
                } for item in diagnostics
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
        row = agent_dispatch.queue_and_launch_run(
            store,
            configuration,
            previous["role"],
            previous_run_id=previous["runId"],
        )
        outcome = "success" if row["state"] in {"queued", "reserved", "running"} else "failure"
        data = {"run": row, "previousRunId": previous["runId"]}
        if row.get("reason") == agent_dispatch.EXECUTION_CONSENT_REQUIRED:
            # 봉투의 사유 코드는 응답 데이터의 최상위에서 온다. 여기서 싣지 않으면 동의
            # 부족이 일반 실패인 execution_failed로 바뀌어, 실행 도구 설치 실패나 권한
            # 부족과 구분되지 않는다.
            data.update({"reason": row["reason"], "stage": "reservation"})
        return outcome, data

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
    waiting: list[dict[str, Any]] = []
    for role_plan in plan["roles"]:
        remaining_targets = list(role_plan["manualTargets"])
        role_started = 0
        role_failure: dict[str, Any] | None = None
        for _ in range(role_plan["granted"]):
            row = agent_dispatch.queue_and_launch_run(
                store,
                configuration,
                role_plan["role"],
                manual_targets=tuple(remaining_targets),
            )
            if row["state"] in {"queued", "reserved", "running"}:
                started.append(row)
                role_started += 1
                if remaining_targets and row["targetId"] in remaining_targets:
                    remaining_targets.remove(row["targetId"])
            elif row["state"] == "idle":
                waiting.append({"role": role_plan["role"], "reason": row["reason"]})
                break
            else:
                role_failure = {
                    "role": role_plan["role"],
                    "stage": row["failureStage"],
                    "reason": row["reason"],
                }
                failures.append(role_failure)
                break
    if started and failures:
        return "partial_success", {"started": started, "failures": failures, "waiting": waiting}
    if failures:
        return "failure", {"started": started, "failures": failures, "waiting": waiting}
    return "success", {"started": started, "failures": failures, "waiting": waiting}


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
        # 계약이 알리는 목록이 곧 라우팅 기준이다. 목록과 실제로 처리되는 명령이
        # 갈라질 자리를 남기지 않기 위해 여기서 한 번만 판단한다.
        if command not in AGENT_COMMANDS:
            raise AgentContractError("unsupported_command", "agent command is not implemented", request_id=request_id)
        if command == "contract.read":
            _write_json(envelope(command, request_id, outcome="success", data=contract_description()), output_stream)
            return 0

        request = _read_json(input_stream)
        request_id = require_api_version(request)
        runtime_store = store or AgentStore()
        if command in {"config.validate", "config.write"}:
            configuration = validate_configuration(request.get("configuration"), request_id=request_id)
            if command == "config.write":
                runtime_store.save_configuration(configuration)
                agent_dispatch.sync_automation(runtime_store, configuration)
                agent_dispatch.ensure_continuous_dispatcher(runtime_store)
                configuration = validate_configuration(runtime_store.get_configuration(configuration.project_id))
            _write_json(
                envelope(command, request_id, outcome="success", data={
                    "configuration": configuration.to_dict(),
                    "deviceCapacity": runtime_store.device_capacity(),
                }),
                output_stream,
            )
            return 0
        if command == "config.read":
            configuration = runtime_store.get_configuration(_project_id(request, request_id))
            _write_json(envelope(command, request_id, outcome="success", data={
                "configuration": configuration,
                "deviceCapacity": runtime_store.device_capacity(),
            }), output_stream)
            return 0
        if command in CONSENT_COMMANDS:
            data = _consent_command(command, request, request_id, runtime_store)
            _write_json(envelope(command, request_id, outcome="success", data=data), output_stream)
            return 0
        if command == "state.read":
            project_id = _project_id(request, request_id)
            runtime_store.prune_expired_plans(project_id)
            configuration = agent_dispatch.configuration_of(runtime_store, project_id)
            if configuration is not None:
                runtime_store.prune_history(project_id, configuration.event_retention_days)
            agent_dispatch.reconcile_project_runs(runtime_store, project_id)
            agent_dispatch.ensure_continuous_dispatcher(runtime_store)
            state = runtime_store.get_state(project_id)
            _write_json(envelope(command, request_id, outcome="success", data=state), output_stream)
            return 0
        if command in EXECUTION_COMMANDS or command in UPDATE_COMMANDS:
            handler = _update_command if command in UPDATE_COMMANDS else _execution_command
            outcome, data = handler(command, request, request_id, runtime_store)
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
