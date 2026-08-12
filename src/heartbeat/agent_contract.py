"""Versioned JSON contract for the project agent runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from heartbeat import __version__
from heartbeat.providers import SUPPORTED_PROVIDERS

API_VERSION = "1"
DEFAULT_PROJECT_MAX_PARALLEL = 3
DEFAULT_DEVICE_MAX_PARALLEL = 16
DEFAULT_ROLE_MAX_PARALLEL = 1
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_EVENT_RETENTION_DAYS = 30
ROLES = frozenset({"planner", "architect", "developer"})
EXECUTION_MODES = frozenset({"once", "continuous"})
RUN_STATES = frozenset({
    "reserved", "queued", "running", "paused", "succeeded", "failed", "cancelled", "recovery_required",
})
FAILURE_STAGES = frozenset({
    "request_validation", "reservation", "provider_start", "role_session", "cleanup", "recovery",
})

# 계약이 알리는 명령 목록의 유일한 정의다. 응답을 만드는 층은 이 목록을 읽기만 하고
# 덧붙이거나 지우지 않는다. 두 곳에서 정하면 계약이 알리는 것과 실제로 되는 일이
# 갈라지고, 앱은 둘 중 무엇을 믿을지 정할 수 없게 된다.
AGENT_COMMANDS: tuple[str, ...] = (
    "config.read",
    "config.validate",
    "config.write",
    "contract.read",
    "logs.read",
    "plan.read",
    "project.pause",
    "project.resume",
    "provider.diagnose",
    "run.cancel",
    "run.retry",
    "run.start",
    "state.read",
    "update.apply",
    "update.plan",
)

# 기기 조회는 독립 실행형 런타임의 `heartbeat runtime` 명령군이 제공한다. 같은 사실을
# 돌려주는 두 번째 명령을 만들지 않기 위해 agent 명령으로 옮기지 않고, 대신 어떤 표면이
# 그 사실을 책임지는지를 여기 한 곳에 적는다.
RUNTIME_COMMANDS: tuple[str, ...] = (
    "runtime.activate",
    "runtime.inspect",
    "runtime.migration-preview",
    "runtime.verify-manifest",
    "runtime.write-manifest",
)

RESERVED_COMMANDS: tuple[str, ...] = ()


class AgentContractError(ValueError):
    """A client request that must be rejected before persistent state changes."""

    def __init__(self, code: str, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class RolePolicy:
    provider: str
    model: str | None
    execution_mode: Literal["once", "continuous"]
    max_parallel: int
    poll_interval_seconds: int
    execution_limit: int | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "provider": value["provider"],
            "model": value["model"],
            "executionMode": value["execution_mode"],
            "maxParallel": value["max_parallel"],
            "pollIntervalSeconds": value["poll_interval_seconds"],
            "executionLimit": value["execution_limit"],
        }


@dataclass(frozen=True)
class AgentConfiguration:
    project_id: str
    working_directory: str
    project_max_parallel: int
    device_max_parallel: int
    automation_enabled: bool
    paused: bool
    event_retention_days: int
    roles: dict[str, RolePolicy]

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "workingDirectory": self.working_directory,
            "projectMaxParallel": self.project_max_parallel,
            "deviceMaxParallel": self.device_max_parallel,
            "automationEnabled": self.automation_enabled,
            "paused": self.paused,
            "eventRetentionDays": self.event_retention_days,
            "roles": {name: policy.to_dict() for name, policy in sorted(self.roles.items())},
        }


def default_configuration(project_id: str, working_directory: str) -> AgentConfiguration:
    """Build the conservative first-run policy for all workflow roles."""
    policy = RolePolicy(
        provider="claude",
        model=None,
        execution_mode="once",
        max_parallel=DEFAULT_ROLE_MAX_PARALLEL,
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        execution_limit=None,
    )
    return AgentConfiguration(
        project_id=project_id,
        working_directory=working_directory,
        project_max_parallel=DEFAULT_PROJECT_MAX_PARALLEL,
        device_max_parallel=DEFAULT_DEVICE_MAX_PARALLEL,
        automation_enabled=False,
        paused=False,
        event_retention_days=DEFAULT_EVENT_RETENTION_DAYS,
        roles={role: policy for role in sorted(ROLES)},
    )


def _require_object(value: Any, name: str, request_id: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentContractError("invalid_request", f"{name} must be an object", request_id=request_id)
    return value


def _require_string(value: Any, name: str, request_id: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentContractError("invalid_request", f"{name} must be a non-empty string", request_id=request_id)
    return value


def _require_int(value: Any, name: str, request_id: str | None, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AgentContractError("invalid_request", f"{name} must be an integer at least {minimum}", request_id=request_id)
    return value


def read_request_id(request: dict[str, Any]) -> str:
    return _require_string(request.get("requestId"), "requestId", None)


def require_api_version(request: dict[str, Any]) -> str:
    request_id = request.get("requestId") if isinstance(request.get("requestId"), str) else None
    version = request.get("apiVersion")
    if version != API_VERSION:
        raise AgentContractError(
            "unsupported_api_version",
            f"apiVersion must be {API_VERSION}",
            request_id=request_id,
        )
    return read_request_id(request)


def validate_configuration(value: Any, *, request_id: str | None = None) -> AgentConfiguration:
    """Validate and normalize a complete configuration without writing it."""
    config = _require_object(value, "configuration", request_id)
    allowed = {
        "projectId", "workingDirectory", "projectMaxParallel", "deviceMaxParallel", "automationEnabled",
        "paused", "eventRetentionDays", "roles",
    }
    unknown = set(config) - allowed
    if unknown:
        raise AgentContractError("unknown_configuration_field", "configuration contains unsupported fields", request_id=request_id)

    project_id = _require_string(config.get("projectId"), "projectId", request_id)
    working_directory = _require_string(config.get("workingDirectory"), "workingDirectory", request_id)
    if not Path(working_directory).is_dir():
        raise AgentContractError("working_directory_not_found", "workingDirectory must name an existing directory", request_id=request_id)

    project_max_parallel = _require_int(
        config.get("projectMaxParallel", DEFAULT_PROJECT_MAX_PARALLEL),
        "projectMaxParallel",
        request_id,
    )
    device_max_parallel = _require_int(
        config.get("deviceMaxParallel", DEFAULT_DEVICE_MAX_PARALLEL),
        "deviceMaxParallel",
        request_id,
    )
    automation_enabled = config.get("automationEnabled", False)
    if not isinstance(automation_enabled, bool):
        raise AgentContractError(
            "invalid_request", "automationEnabled must be a boolean", request_id=request_id
        )
    paused = config.get("paused", False)
    if not isinstance(paused, bool):
        raise AgentContractError("invalid_request", "paused must be a boolean", request_id=request_id)
    event_retention_days = _require_int(
        config.get("eventRetentionDays", DEFAULT_EVENT_RETENTION_DAYS),
        "eventRetentionDays",
        request_id,
    )

    role_values = _require_object(config.get("roles"), "roles", request_id)
    if set(role_values) != ROLES:
        raise AgentContractError("invalid_roles", "roles must contain planner, architect, and developer exactly once", request_id=request_id)

    roles: dict[str, RolePolicy] = {}
    for role in sorted(ROLES):
        policy = _require_object(role_values[role], f"roles.{role}", request_id)
        allowed_policy = {
            "provider", "model", "executionMode", "maxParallel", "pollIntervalSeconds", "executionLimit",
        }
        if set(policy) - allowed_policy:
            raise AgentContractError("unknown_role_field", f"roles.{role} contains unsupported fields", request_id=request_id)
        provider = _require_string(policy.get("provider"), f"roles.{role}.provider", request_id)
        if provider not in SUPPORTED_PROVIDERS:
            raise AgentContractError("unsupported_provider", f"roles.{role}.provider is unsupported", request_id=request_id)
        model = policy.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise AgentContractError("invalid_request", f"roles.{role}.model must be a string or null", request_id=request_id)
        execution_mode = _require_string(policy.get("executionMode"), f"roles.{role}.executionMode", request_id)
        if execution_mode not in EXECUTION_MODES:
            raise AgentContractError("unsupported_execution_mode", f"roles.{role}.executionMode is unsupported", request_id=request_id)
        max_parallel = _require_int(policy.get("maxParallel", DEFAULT_ROLE_MAX_PARALLEL), f"roles.{role}.maxParallel", request_id)
        if max_parallel > project_max_parallel:
            raise AgentContractError("role_limit_exceeded", f"roles.{role}.maxParallel exceeds projectMaxParallel", request_id=request_id)
        poll_interval_seconds = _require_int(
            policy.get("pollIntervalSeconds", DEFAULT_POLL_INTERVAL_SECONDS),
            f"roles.{role}.pollIntervalSeconds",
            request_id,
        )
        execution_limit = policy.get("executionLimit")
        if execution_limit is not None:
            execution_limit = _require_int(execution_limit, f"roles.{role}.executionLimit", request_id)
        roles[role] = RolePolicy(
            provider=provider,
            model=model,
            execution_mode=execution_mode,  # type: ignore[arg-type]
            max_parallel=max_parallel,
            poll_interval_seconds=poll_interval_seconds,
            execution_limit=execution_limit,
        )

    return AgentConfiguration(
        project_id=project_id,
        working_directory=str(Path(working_directory)),
        project_max_parallel=project_max_parallel,
        device_max_parallel=device_max_parallel,
        automation_enabled=automation_enabled,
        paused=paused,
        event_retention_days=event_retention_days,
        roles=roles,
    )


def envelope(
    command: str,
    request_id: str,
    *,
    outcome: Literal["success", "partial_success", "failure"],
    data: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Produce the only JSON shape exposed by the agent CLI."""
    result: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "runtimeVersion": __version__,
        "requestId": request_id,
        "command": command,
        "outcome": outcome,
    }
    if data is not None:
        result["data"] = data
    if error is not None:
        result["error"] = error
    return result


def contract_description() -> dict[str, Any]:
    """Machine-readable description used by a consumer before making writes."""
    return {
        "supportedApiVersions": [API_VERSION],
        "runtimeVersion": __version__,
        "implementedCommands": list(AGENT_COMMANDS),
        "runtimeCommands": list(RUNTIME_COMMANDS),
        "reservedCommands": list(RESERVED_COMMANDS),
        "roles": sorted(ROLES),
        "providers": sorted(SUPPORTED_PROVIDERS),
        "executionModes": sorted(EXECUTION_MODES),
        "runStates": sorted(RUN_STATES),
        "failureStages": sorted(FAILURE_STAGES),
        "defaults": {
            "roleMaxParallel": DEFAULT_ROLE_MAX_PARALLEL,
            "projectMaxParallel": DEFAULT_PROJECT_MAX_PARALLEL,
            "deviceMaxParallel": DEFAULT_DEVICE_MAX_PARALLEL,
            "deviceMaxParallelIsRecommendationFallback": True,
            "eventRetentionDays": DEFAULT_EVENT_RETENTION_DAYS,
            "automationEnabled": False,
        },
    }
