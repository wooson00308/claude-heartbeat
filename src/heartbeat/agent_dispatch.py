"""Provider-neutral dispatch: reserve work, start runs, supervise, recover.

The runtime never chooses a workflow target by itself.  It asks the project's
app-managed reservation helper for one target at a time, and starts a provider
only after that helper reported a lease of its own.  Everything a started run
needs to be found again lives in SQLite and in the run's event file.  A short
control CLI queues the run, then a detached worker owns the provider supervisor
until the terminal event and lease cleanup are persisted.  A daemon tick or a
state read only recovers legacy runs and workers that actually disappeared.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # source installs may not have refreshed dependencies yet
    FileSystemEvent = Any  # type: ignore[misc,assignment]
    FileSystemEventHandler = object  # type: ignore[misc,assignment]
    Observer = None  # type: ignore[assignment,misc]

from heartbeat import agent_contract
from heartbeat.agent_contract import (
    AgentConfiguration,
    validate_configuration,
)
from heartbeat.agent_store import AgentStore, utc_now
from heartbeat.providers import ClaudeProvider, CodexProvider
from heartbeat.providers.lifecycle import (
    Reservation,
    LifecycleFailure,
    last_event,
    read_reservation,
    recover_run,
    start_reserved_run,
)
from heartbeat.providers.process import (
    NO_WINDOW,
    AgentProvider,
    ProviderExecutionRequest,
    ProviderRunHandle,
    default_event_root,
    observe_process,
    process_identity,
)

RESERVATION_HELPER = ".workflow/rules/wf-reserve.sh"
RESERVATION_HELPER_WINDOWS = ".workflow/rules/wf-reserve.ps1"
CLAIM_HELPER = ".workflow/rules/wf-claim.sh"
CLAIM_HELPER_WINDOWS = ".workflow/rules/wf-claim.ps1"
MIGRATION_LOCK = ".workflow/.runtime/migration.lock"
LEASE_DIRECTORY = ".workflow/.runtime/leases"

# The role session renews and releases the lease itself once it takes over, so
# the runtime only needs a window long enough to hand the work across.
RESERVATION_MINUTES = 30
PLAN_TTL_SECONDS = 120
ACTIVE_STATES = frozenset({"reserved", "queued", "running", "paused"})

# 장부가 recovery_required로 내렸지만 정리가 끝나지 않은 실행의 상태. 그 프로세스는
# 아직 살아 일하고 있을 수 있으므로(#38의 오판이 정확히 그 모양) 슬롯 계산은 활성과
# 함께 센다. ``remaining``을 비우는 것은 sweep_dropped_runs뿐이라, 끝났음이 증명된
# 행만 점유에서 빠진다.
OCCUPYING_STATES = frozenset({"recovery_required"})
WORKER_POLL_SECONDS = 2.0
WORKER_START_TIMEOUT_SECONDS = 10.0
DISPATCHER_POLL_CAP_SECONDS = 5.0
DISPATCHER_START_TIMEOUT_SECONDS = 10.0
TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
TERMINAL_BY_EVENT = {
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "failed",
}


@dataclass(frozen=True)
class ToolInvocation:
    """One helper call, judged by its exit code and never by its text."""

    returncode: int
    stdout: str


def run_tool(argv: Sequence[str], cwd: Path) -> ToolInvocation:
    """Run an app-managed helper without a shell, from the project root."""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ToolInvocation(returncode=1, stdout="")
    return ToolInvocation(returncode=completed.returncode, stdout=completed.stdout)


@dataclass(frozen=True)
class DispatchFailure:
    """A refusal named by one of the contract's six failure stages."""

    stage: str
    reason: str


@dataclass
class WorkflowHelpers:
    """The reservation and claim helpers as this project has them installed.

    Both are app-managed assets.  This class calls them and reads exit codes;
    it never writes, repairs, or substitutes for a lease file.
    """

    working_directory: Path
    runner: Callable[[Sequence[str], Path], ToolInvocation] = run_tool

    def _helper(self, posix: str, windows: str) -> tuple[list[str], bool]:
        relative = windows if os.name == "nt" else posix
        installed = (self.working_directory / relative).is_file()
        if os.name == "nt":
            return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", relative], installed)
        return (["sh", relative], installed)

    def reserve(self, role: str, agent: str, minutes: int = RESERVATION_MINUTES) -> Reservation | DispatchFailure:
        """Reserve one target, or say which stage refused before any process."""
        command, installed = self._helper(RESERVATION_HELPER, RESERVATION_HELPER_WINDOWS)
        if not installed:
            return DispatchFailure(stage="reservation", reason="reservation_helper_missing")
        invocation = self.runner([*command, "acquire", role, agent, str(minutes)], self.working_directory)
        outcome = read_reservation(invocation.returncode, invocation.stdout)
        if isinstance(outcome, LifecycleFailure):
            # A usage error repeats for the same arguments, so it is the caller's
            # request that is wrong rather than the reservation itself.
            stage = "request_validation" if outcome.reason == "reservation_usage_error" else "reservation"
            return DispatchFailure(stage=stage, reason=outcome.reason)
        return outcome

    def eligible(self, role: str) -> dict[str, Any] | DispatchFailure:
        """Read the complete role verdict without reserving or starting anything."""
        command, installed = self._helper(
            ".workflow/rules/wf-eligible.sh", ".workflow/rules/wf-eligible.ps1"
        )
        if not installed:
            # v0.8 projects only installed the atomic reservation helper. Keep
            # that compatibility path until the app refreshes managed assets;
            # only v0.9 assets can distinguish idle from an opaque refusal.
            return {
                "role": role,
                "targetId": "__defer_to_reservation__",
                "candidates": [],
                "verdict": "eligible",
                "deferred": True,
            }
        invocation = self.runner([*command, role, "--json"], self.working_directory)
        if invocation.returncode not in (0, 1):
            return DispatchFailure(
                stage="request_validation" if invocation.returncode == 2 else "reservation",
                reason="eligibility_usage_error" if invocation.returncode == 2 else "eligibility_unavailable",
            )
        try:
            payload = json.loads(invocation.stdout)
        except json.JSONDecodeError:
            return DispatchFailure(stage="reservation", reason="eligibility_malformed")
        if not isinstance(payload, dict) or payload.get("role") != role:
            return DispatchFailure(stage="reservation", reason="eligibility_malformed")
        return payload

    def renew(self, target_id: str, lease_id: str, minutes: int = RESERVATION_MINUTES) -> int:
        command, installed = self._helper(CLAIM_HELPER, CLAIM_HELPER_WINDOWS)
        if not installed:
            return 1
        return self.runner(
            [*command, "renew", target_id, lease_id, str(minutes)], self.working_directory
        ).returncode

    def release(self, target_id: str, lease_id: str) -> int:
        command, installed = self._helper(CLAIM_HELPER, CLAIM_HELPER_WINDOWS)
        if not installed:
            return 1
        return self.runner([*command, "release", target_id, lease_id], self.working_directory).returncode

    def migration_locked(self) -> bool:
        return (self.working_directory / MIGRATION_LOCK).exists()

    def lease_is_active(self, target_id: str, *, now: datetime) -> bool:
        """Read one lease file to see whether it still holds its target."""
        path = self.working_directory / LEASE_DIRECTORY / f"{target_id}.yml"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        for line in text.splitlines():
            if line.startswith("expires_at:"):
                try:
                    expires = datetime.strptime(line.split(":", 1)[1].strip(), "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    return False
                return expires.replace(tzinfo=UTC) > now
        return False


def build_provider(name: str) -> AgentProvider:
    """Build one provider object for one tick, then let the caller drop it."""
    return CodexProvider() if name == "codex" else ClaudeProvider()


@dataclass
class RoleSlots:
    """What the caller asked for on one role."""

    role: str
    slots: int
    manual_targets: tuple[str, ...] = ()


@dataclass
class RolePlan:
    """What the runtime would actually start for one role, and why not more."""

    role: str
    provider: str
    execution_mode: str
    requested: int
    granted: int
    excluded: list[str] = field(default_factory=list)
    manual_targets: tuple[str, ...] = ()
    target_id: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = "no_target"
    diagnostic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "executionMode": self.execution_mode,
            "requested": self.requested,
            "granted": self.granted,
            "excluded": sorted(set(self.excluded)),
            "manualTargets": list(self.manual_targets),
            "targetId": self.target_id,
            "candidates": self.candidates,
            "verdict": self.verdict,
            "diagnostic": self.diagnostic,
        }


def configuration_of(store: AgentStore, project_id: str) -> AgentConfiguration | None:
    stored = store.get_configuration(project_id)
    return validate_configuration(stored) if stored else None


# 동의가 없어 시작하지 못한 것은 실행 실패가 아니라 대기다. 실패로 기록하면 사용자는
# 실행 도구나 권한에서 원인을 찾게 되고, 정작 고칠 자리인 동의 화면에 도달하지 못한다.
EXECUTION_CONSENT_REQUIRED = "execution_consent_required"


def consent_is_valid(record: dict[str, Any] | None) -> bool:
    """Judge one consent record against the notice version required now.

    This is the only place the rule lives. The consent commands answer with the
    same judgement, so what the app is told is valid and what execution treats
    as valid cannot drift apart.
    """
    if record is None:
        return False
    notice_version = record.get("noticeVersion")
    # 요구 버전은 계약 모듈에서 그때그때 읽는다. 값을 여기로 복사해 두면 요구 버전이
    # 올라갔을 때 명령이 답하는 유효와 실행이 판정하는 유효가 갈라진다.
    return isinstance(notice_version, int) and notice_version >= agent_contract.REQUIRED_NOTICE_VERSION


def project_may_execute(store: AgentStore, project_id: str) -> bool:
    """Answer whether this project may start a new run at all."""
    return consent_is_valid(store.get_consent(project_id))


def sync_automation(store: AgentStore, configuration: AgentConfiguration) -> None:
    """Keep role participation separate from the project's master switch."""
    current = {intent["role"]: intent for intent in store.list_intents(configuration.project_id)}
    disabled_roles = {
        role for role, policy in configuration.roles.items() if policy.execution_mode != "continuous"
    }
    store.drop_role_intents(configuration.project_id, disabled_roles)
    for role, policy in sorted(configuration.roles.items()):
        if policy.execution_mode != "continuous" or role in current:
            continue
        store.activate_intent(
            configuration.project_id,
            {
                "intentId": uuid.uuid4().hex,
                "role": role,
                "mode": "auto",
                "manualTargets": [],
                "startedCount": 0,
                "nextPollAt": utc_now(),
                "lastCheckAt": None,
                "lastResult": "waiting",
                "lastAssignedAt": None,
            },
        )


def runtime_revision(store: AgentStore, configuration: AgentConfiguration) -> str:
    """Fingerprint everything a plan assumed, so a changed world is visible."""
    active = sorted(run["runId"] for run in store.list_runs(states=ACTIVE_STATES))
    # 점유 실행도 계획의 배정 수를 정하므로 지문에 들어간다. 빠뜨리면 정리가 슬롯을
    # 놓은 뒤에도 점유를 가정한 이전 계획이 그대로 적용된다.
    occupied = sorted(run["runId"] for run in _occupying_runs(store))
    payload = json.dumps(
        {
            "configuration": configuration.to_dict(),
            "deviceMaxParallel": store.device_limit(),
            "active": active,
            "occupied": occupied,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _occupying_runs(store: AgentStore, project_id: str | None = None) -> list[dict[str, Any]]:
    """Dropped runs whose cleanup has not finished: they still hold their slot.

    The ledger dropping a run says nothing about its process, which may still
    be alive and working on its target. Counting these rows as free is what
    let real concurrency climb past every limit (2026-08-19: seven sessions on
    one project). Only a finished cleanup — an empty ``remaining`` — proves the
    slot is truly free, and ``sweep_dropped_runs`` is the one place that
    empties it.
    """
    return [
        run for run in store.list_runs(project_id, states=OCCUPYING_STATES)
        if run.get("remaining")
    ]


def _device_remaining(store: AgentStore, configuration: AgentConfiguration) -> int:
    limit = store.device_limit()
    held = len(store.list_runs(states=ACTIVE_STATES)) + len(_occupying_runs(store))
    return max(0, limit - held)


def _project_remaining(store: AgentStore, configuration: AgentConfiguration) -> int:
    held = len(store.list_runs(configuration.project_id, states=ACTIVE_STATES)) + len(
        _occupying_runs(store, configuration.project_id)
    )
    return max(0, configuration.project_max_parallel - held)


def _role_remaining(store: AgentStore, configuration: AgentConfiguration, role: str) -> int:
    active = [run for run in store.list_runs(configuration.project_id, states=ACTIVE_STATES) if run["role"] == role]
    occupied = [run for run in _occupying_runs(store, configuration.project_id) if run["role"] == role]
    return max(0, configuration.roles[role].max_parallel - len(active) - len(occupied))


def validate_manual_targets(
    helpers: WorkflowHelpers,
    targets: Sequence[str],
    *,
    now: datetime,
) -> tuple[tuple[str, ...], list[str]]:
    """Reject what cannot become a run before any provider is considered.

    Whether a target is already finished or rejected is the reservation
    helper's judgement, not the runtime's: the helper only ever selects an
    eligible target, so a request naming a finished one simply never matches.
    """
    accepted: list[str] = []
    reasons: list[str] = []
    for target in targets:
        if target in accepted:
            reasons.append("duplicate_target")
        elif not TARGET_PATTERN.match(target):
            reasons.append("invalid_target")
        elif helpers.lease_is_active(target, now=now):
            reasons.append("active_lease")
        else:
            accepted.append(target)
    return tuple(accepted), reasons


def build_plan(
    store: AgentStore,
    configuration: AgentConfiguration,
    requests: Sequence[RoleSlots],
    *,
    helpers: WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decide what a start would do, without reserving or starting anything."""
    now = now or datetime.now(UTC)
    device_remaining = _device_remaining(store, configuration)
    project_remaining = _project_remaining(store, configuration)
    device_occupied = len(_occupying_runs(store))
    project_occupying = _occupying_runs(store, configuration.project_id)
    locked = helpers.migration_locked()
    plans: list[RolePlan] = []
    budget = min(device_remaining, project_remaining)
    # 점유가 없었다면 남았을 몫. granted가 이보다 작게 잡힌 역할은 평소의 한도 도달이
    # 아니라 장부가 놓은 실행이 자리를 잡고 있는 것이므로, 배제 사유를 갈라서 낸다.
    # 활성 0건인데 한도 도달로만 보이던 화면(2026-08-19)이 이 구분이 필요한 이유다.
    budget_if_swept = min(
        device_remaining + device_occupied,
        project_remaining + len(project_occupying),
    )

    for request in requests:
        policy = configuration.roles[request.role]
        eligibility = _eligibility(helpers, request.role)
        plan = RolePlan(
            role=request.role,
            provider=policy.provider,
            execution_mode=policy.execution_mode,
            requested=request.slots,
            granted=0,
        )
        granted = min(request.slots, _role_remaining(store, configuration, request.role), budget)
        granted_if_swept = min(
            request.slots,
            _role_remaining(store, configuration, request.role)
            + sum(1 for run in project_occupying if run["role"] == request.role),
            budget_if_swept,
        )
        if policy.execution_limit is not None:
            granted = min(granted, policy.execution_limit)
            granted_if_swept = min(granted_if_swept, policy.execution_limit)
        if configuration.paused:
            granted = 0
            plan.excluded.append("project_paused")
        if locked:
            granted = 0
            plan.excluded.append("migration_lock")
        if isinstance(eligibility, DispatchFailure):
            granted = 0
            plan.excluded.append(eligibility.reason)
        else:
            plan.target_id = eligibility.get("targetId")
            raw_candidates = eligibility.get("candidates")
            if isinstance(raw_candidates, list):
                plan.candidates = [item for item in raw_candidates if isinstance(item, dict)]
            raw_verdict = eligibility.get("verdict")
            plan.verdict = raw_verdict if isinstance(raw_verdict, str) else "no_target"
        if request.manual_targets:
            accepted, reasons = validate_manual_targets(helpers, request.manual_targets, now=now)
            eligible_ids = {
                item.get("id") for item in plan.candidates if item.get("reason") == "eligible"
            }
            if not (isinstance(eligibility, dict) and eligibility.get("deferred") is True):
                accepted = tuple(target for target in accepted if target in eligible_ids)
            if not accepted and not reasons:
                reasons.append("manual_target_unavailable")
            plan.manual_targets = accepted
            plan.excluded.extend(reasons)
            granted = min(granted, len(accepted))
        elif plan.target_id is None:
            granted = 0
            plan.excluded.append(plan.verdict)

        # Target availability is free to check. Provider diagnostics can launch
        # a CLI, so only do it when there is work that could actually start.
        if granted > 0:
            diagnostic = provider_factory(policy.provider).diagnose()
            model_status = _model_status(
                policy.model, diagnostic.model_catalog.status, diagnostic.model_catalog.models
            )
            plan.diagnostic = {
                "status": diagnostic.status,
                "provider": diagnostic.provider,
                "version": diagnostic.version,
                "selectedModel": policy.model,
                "modelStatus": model_status,
                "modelCatalog": diagnostic.model_catalog.to_dict(),
            }
            if diagnostic.status != "ready":
                granted = 0
                plan.excluded.append(f"provider_{diagnostic.status}")
            # A stored model that vanished from the account catalog is not the user's
            # mistake to fix — CLI updates rename models. The run proceeds on the CLI
            # default instead (see start), so the plan keeps its grant and the
            # diagnostic's modelStatus carries the fact for the screen.
        if granted < request.slots and not plan.excluded:
            if policy.execution_limit is not None and granted == policy.execution_limit:
                plan.excluded.append("execution_limit")
            elif granted < granted_if_swept:
                plan.excluded.append("slots_held_by_unrecovered_runs")
            else:
                plan.excluded.append("limit_reached")
        plan.granted = granted
        budget -= granted
        budget_if_swept -= granted
        plans.append(plan)

    return {
        "planId": uuid.uuid4().hex,
        "projectId": configuration.project_id,
        "revision": runtime_revision(store, configuration),
        "expiresAt": (now + timedelta(seconds=PLAN_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deviceRemaining": device_remaining,
        "projectRemaining": project_remaining,
        "deviceOccupied": device_occupied,
        "projectOccupied": len(project_occupying),
        "limits": {
            "projectMaxParallel": configuration.project_max_parallel,
            "deviceMaxParallel": store.device_limit(),
            "roleMaxParallel": {role: policy.max_parallel for role, policy in sorted(configuration.roles.items())},
        },
        "billingRouteRisk": any(
            plan.diagnostic.get("status") == "billing_route_acknowledgement_required" for plan in plans
        ),
        "roles": [plan.to_dict() for plan in plans],
    }


def _model_status(model: str | None, catalog_status: str, models: Sequence[object]) -> str:
    if model is None:
        return "default"
    if catalog_status != "available":
        return "unverified"
    return "available" if any(getattr(candidate, "id", None) == model for candidate in models) else "unavailable"


def _eligibility(helpers: WorkflowHelpers, role: str) -> dict[str, Any] | DispatchFailure:
    """Use the richer eligibility contract when available.

    Older embedders and release fixtures only implement atomic reservation. A
    deferred sentinel keeps those callers on that safe path while managed
    v0.9 projects expose the complete candidate list before reserving.
    """
    eligible = getattr(helpers, "eligible", None)
    if not callable(eligible):
        return {
            "targetId": "deferred-reservation",
            "targetKind": "reservation_helper",
            "candidates": [],
            "verdict": "eligible",
            "deferred": True,
        }
    return eligible(role)


def _new_run_row(
    configuration: AgentConfiguration,
    role: str,
    provider: str,
    previous_run_id: str | None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "runId": uuid.uuid4().hex,
        "projectId": configuration.project_id,
        "role": role,
        "provider": provider,
        "state": "reserved",
        "targetId": None,
        "leaseId": None,
        "resultPrefix": None,
        "reservationExpiresAt": None,
        "providerRunId": None,
        "pid": None,
        "startedAt": None,
        "finishedAt": None,
        "processIdentity": None,
        "eventPath": None,
        "lastOffset": 0,
        "previousRunId": previous_run_id,
        "intentId": intent_id,
        "failureStage": None,
        "reason": None,
        "remaining": [],
        "leaseHandedOver": False,
        "createdAt": utc_now(),
    }


def _not_started(
    configuration: AgentConfiguration,
    role: str,
    provider: str,
    *,
    reason: str,
    manual: bool,
    previous_run_id: str | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    """Return a start result that is deliberately absent from execution history."""
    row = _new_run_row(configuration, role, provider, previous_run_id, intent_id)
    row.update({
        "state": "not_started" if manual else "idle",
        "failureStage": "reservation" if manual else None,
        "reason": reason,
        "finishedAt": utc_now(),
    })
    return row


def _preflight_target(
    store: AgentStore,
    configuration: AgentConfiguration,
    role: str,
    helpers: WorkflowHelpers,
    manual_targets: Sequence[str],
    *,
    previous_run_id: str | None = None,
    intent_id: str | None = None,
) -> dict[str, Any] | None:
    policy = configuration.roles[role]
    if not project_may_execute(store, configuration.project_id):
        # 자격 조회보다 앞이다. 예약 도구 호출과 실행 도구 시작이 모두 이 뒤에 있으므로
        # 여기서 멈추면 대상 선점도 한도 차감도 일어나지 않는다. 수동 대상을 지정한
        # 요청도 대기로 돌려주어, 동의 부족이 실패 목록에 실리지 않게 한다.
        return _not_started(
            configuration,
            role,
            policy.provider,
            reason=EXECUTION_CONSENT_REQUIRED,
            manual=False,
            previous_run_id=previous_run_id,
            intent_id=intent_id,
        )
    verdict = _eligibility(helpers, role)
    if isinstance(verdict, DispatchFailure):
        return _not_started(
            configuration,
            role,
            policy.provider,
            reason=verdict.reason,
            manual=bool(manual_targets),
            previous_run_id=previous_run_id,
            intent_id=intent_id,
        )
    candidates = verdict.get("candidates")
    eligible_ids = {
        candidate.get("id")
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("reason") == "eligible"
    } if isinstance(candidates, list) else set()
    target_id = verdict.get("targetId")
    if manual_targets:
        if verdict.get("deferred") is True:
            return None
        if not any(target in eligible_ids for target in manual_targets):
            return _not_started(
                configuration,
                role,
                policy.provider,
                reason="manual_target_unavailable",
                manual=True,
                previous_run_id=previous_run_id,
                intent_id=intent_id,
            )
        return None
    if not isinstance(target_id, str) or not target_id:
        reason = verdict.get("verdict")
        return _not_started(
            configuration,
            role,
            policy.provider,
            reason=reason if isinstance(reason, str) and reason else "no_target",
            manual=False,
            previous_run_id=previous_run_id,
            intent_id=intent_id,
        )
    return None


def _fail(store: AgentStore, row: dict[str, Any], *, stage: str, reason: str, state: str = "failed") -> dict[str, Any]:
    row.update({"state": state, "failureStage": stage, "reason": reason, "finishedAt": utc_now()})
    store.save_run(row)
    store.record_error(row["projectId"], row["runId"], {"stage": stage, "reason": reason, "role": row["role"]})
    return row


def start_one_run(
    store: AgentStore,
    configuration: AgentConfiguration,
    role: str,
    *,
    helpers: WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
    manual_targets: Sequence[str] = (),
    previous_run_id: str | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    """Start inside a persistent daemon or test process that owns supervision."""
    policy = configuration.roles[role]
    preflight = _preflight_target(
        store,
        configuration,
        role,
        helpers,
        manual_targets,
        previous_run_id=previous_run_id,
        intent_id=intent_id,
    )
    if preflight is not None:
        return preflight
    row = _new_run_row(configuration, role, policy.provider, previous_run_id, intent_id)
    row["manualTargets"] = list(manual_targets)
    return _start_run_row(
        store,
        configuration,
        row,
        helpers=helpers,
        provider_factory=provider_factory,
    )


def queue_one_run(
    store: AgentStore,
    configuration: AgentConfiguration,
    role: str,
    *,
    manual_targets: Sequence[str] = (),
    previous_run_id: str | None = None,
    intent_id: str | None = None,
    helpers: WorkflowHelpers | None = None,
) -> dict[str, Any]:
    """Persist work before a detached supervisor is launched for it."""
    policy = configuration.roles[role]
    helpers = helpers or WorkflowHelpers(Path(configuration.working_directory))
    preflight = _preflight_target(
        store,
        configuration,
        role,
        helpers,
        manual_targets,
        previous_run_id=previous_run_id,
        intent_id=intent_id,
    )
    if preflight is not None:
        return preflight
    row = _new_run_row(configuration, role, policy.provider, previous_run_id, intent_id)
    row.update({"state": "queued", "manualTargets": list(manual_targets)})
    store.save_run(row)
    return row


def _start_run_row(
    store: AgentStore,
    configuration: AgentConfiguration,
    row: dict[str, Any],
    *,
    helpers: WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
) -> dict[str, Any]:
    """Reserve and start the already identified run without changing its id."""
    role = row["role"]
    policy = configuration.roles[role]
    manual_targets = tuple(row.get("manualTargets", ()))
    preflight = _preflight_target(
        store,
        configuration,
        role,
        helpers,
        manual_targets,
        previous_run_id=row.get("previousRunId"),
        intent_id=row.get("intentId"),
    )
    if preflight is not None:
        row.update({
            "state": preflight["state"],
            "failureStage": preflight["failureStage"],
            "reason": preflight["reason"],
            "finishedAt": preflight["finishedAt"],
        })
        store.save_run(row)
        return row
    provider = None
    if policy.model is not None:
        provider = provider_factory(policy.provider)
        diagnostic = provider.diagnose()
        if _model_status(policy.model, diagnostic.model_catalog.status, diagnostic.model_catalog.models) == "unavailable":
            # The stored model vanished from the account catalog — CLI updates rename
            # models, and failing every run until the user rediscovers settings is the
            # wrong owner for that event. Run on the CLI default and record what was
            # substituted so the screen can say it.
            row["requestedModel"] = policy.model
            row["modelFallback"] = True
            policy = dataclasses.replace(policy, model=None)
    row["state"] = "reserved"
    store.save_run(row)
    reservation = helpers.reserve(role, f"heartbeat-runtime-{role}")
    if isinstance(reservation, DispatchFailure):
        if reservation.reason == "reservation_unavailable":
            eligibility = _eligibility(helpers, role)
            if isinstance(eligibility, dict) and eligibility.get("deferred") is True:
                return _fail(store, row, stage=reservation.stage, reason=reservation.reason)
            row.update({
                "state": "not_started" if manual_targets else "idle",
                "failureStage": "reservation" if manual_targets else None,
                "reason": "manual_target_unavailable" if manual_targets else "no_target",
                "finishedAt": utc_now(),
            })
            store.save_run(row)
            return row
        return _fail(store, row, stage=reservation.stage, reason=reservation.reason)
    if manual_targets and reservation.target_id not in manual_targets:
        # The helper picks the target itself, so a manual request can only be
        # honoured by refusing what it did not ask for and giving the lease back.
        helpers.release(reservation.target_id, reservation.lease_id)
        return _fail(store, row, stage="reservation", reason="manual_target_unavailable")

    row.update({
        "targetId": reservation.target_id,
        "leaseId": reservation.lease_id,
        "resultPrefix": reservation.result_prefix,
        "reservationExpiresAt": reservation.expires_at,
    })
    store.save_run(row)

    stored = store.get_run(row["runId"])
    if stored is None or (stored["targetId"], stored["leaseId"]) != (reservation.target_id, reservation.lease_id):
        helpers.release(reservation.target_id, reservation.lease_id)
        return _fail(store, row, stage="reservation", reason="reservation_not_persisted")

    provider = provider or provider_factory(policy.provider)
    event_root = default_event_root() / configuration.project_id
    request = ProviderExecutionRequest(
        project_id=configuration.project_id,
        role=role,
        target_id=reservation.target_id,
        project_root=Path(configuration.working_directory),
        prompt=reservation.role_prompt,
        model=policy.model,
        timeout_seconds=None,
        event_root=event_root,
        lease_id=reservation.lease_id,
    )
    started = start_reserved_run(provider, reservation, request)
    if isinstance(started, LifecycleFailure):
        released = helpers.release(reservation.target_id, reservation.lease_id)
        row["remaining"] = [] if released in (0, 5) else ["lease_release"]
        stage = "provider_start" if started.stage == "provider_start" else "reservation"
        state = "failed" if released in (0, 5) else "recovery_required"
        return _fail(store, row, stage=stage, reason=started.reason, state=state)

    row.update({
        "state": "running",
        "providerRunId": started.run_id,
        "pid": started.pid,
        "startedAt": started.started_at,
        "processIdentity": started.process_identity,
        "eventPath": str(started.event_path),
    })
    store.save_run(row)
    return row


def _worker_command(run_id: str) -> list[str]:
    """Start the same runtime in its private worker mode in source and bundles."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "agent-worker", run_id]
    return [sys.executable, str(Path(__file__).with_name("cli.py")), "agent-worker", run_id]


def launch_run_worker(store: AgentStore, run_id: str) -> bool:
    """Launch a worker detached from the short JSON control command."""
    environment = dict(os.environ)
    environment["HEARTBEAT_AGENT_DATABASE"] = str(store.database_path)
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    try:
        worker = subprocess.Popen(_worker_command(run_id), **options)
    except OSError:
        return False
    store.attach_supervisor(run_id, worker.pid, process_identity(worker.pid))
    return True


def queue_and_launch_run(
    store: AgentStore,
    configuration: AgentConfiguration,
    role: str,
    *,
    manual_targets: Sequence[str] = (),
    previous_run_id: str | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    """Queue one run and report whether its detached supervisor was created."""
    helpers = WorkflowHelpers(Path(configuration.working_directory))
    row = queue_one_run(
        store,
        configuration,
        role,
        manual_targets=manual_targets,
        previous_run_id=previous_run_id,
        intent_id=intent_id,
        helpers=helpers,
    )
    if row["state"] in {"idle", "not_started"}:
        return row
    if launch_run_worker(store, row["runId"]):
        deadline = time.monotonic() + WORKER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            current = store.get_run(row["runId"]) or row
            if current["state"] not in {"queued", "reserved"} or current.get("targetId"):
                if current["state"] in {"idle", "not_started"}:
                    store.delete_run(current["runId"])
                return current
            worker_pid = current.get("supervisorPid")
            if isinstance(worker_pid, int) and observe_process(worker_pid).liveness == "gone":
                reconcile_project_runs(store, configuration.project_id)
                return store.get_run(row["runId"]) or current
            time.sleep(0.02)
        reconcile_project_runs(store, configuration.project_id)
        return store.get_run(row["runId"]) or row
    return _fail(store, row, stage="provider_start", reason="worker_not_started")


def serve_queued_run(
    store: AgentStore,
    run_id: str,
    *,
    helpers_factory: Callable[[Path], WorkflowHelpers] = WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
    poll_seconds: float = WORKER_POLL_SECONDS,
) -> int:
    """Own one queued provider until its terminal state and lease are durable."""
    row = store.get_run(run_id)
    if row is None or row.get("state") != "queued":
        return 1
    store.attach_supervisor(run_id, os.getpid(), process_identity(os.getpid()))
    row = store.get_run(run_id)
    if row is None:
        return 1
    configuration = configuration_of(store, row["projectId"])
    if configuration is None or row["role"] not in configuration.roles:
        _fail(store, row, stage="request_validation", reason="project_not_configured")
        return 1
    helpers = helpers_factory(Path(configuration.working_directory))
    started = _start_run_row(
        store,
        configuration,
        row,
        helpers=helpers,
        provider_factory=provider_factory,
    )
    if started["state"] != "running":
        return 0 if started["state"] in {"idle", "not_started"} else 1

    while True:
        time.sleep(max(0.01, poll_seconds))
        current = store.get_run(run_id)
        if current is None or current["state"] not in ACTIVE_STATES:
            return 0 if current is not None and current["state"] == "succeeded" else 1
        reconcile_run(
            store,
            current,
            helpers=helpers,
            provider_factory=provider_factory,
            supervisor_pid=os.getpid(),
        )


def _dispatcher_command() -> list[str]:
    """Start the same runtime in private repeat-dispatcher mode."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "agent-dispatcher"]
    return [sys.executable, str(Path(__file__).with_name("cli.py")), "agent-dispatcher"]


def _dispatcher_is_live(row: dict[str, Any]) -> bool:
    observation = observe_process(row["pid"])
    return (
        observation.liveness == "running"
        and bool(row.get("processIdentity"))
        and observation.identity == row["processIdentity"]
    )


def _claim_dispatcher_process(store: AgentStore, pid: int, identity: str) -> bool:
    """Claim the singleton after proving any prior owner is stale.

    A service restart can kill the previous process before its ``finally`` block
    releases the database row.  An unreadable process is never guessed about,
    while a gone process or a reused PID can be released with the persisted
    identity as the compare-and-delete guard.
    """
    current = store.get_dispatcher()
    if current is not None:
        current_identity = current.get("processIdentity")
        observation = observe_process(current["pid"])
        if (
            observation.liveness == "running"
            and bool(current_identity)
            and observation.identity == current_identity
        ):
            return current["pid"] == pid and current_identity == identity
        if observation.liveness == "unknown":
            return False
        store.release_dispatcher(current["pid"], current_identity)
    return store.claim_dispatcher(pid, identity)


def launch_continuous_dispatcher(store: AgentStore) -> bool:
    """Detach one device-wide scheduler from the short control command."""
    environment = dict(os.environ)
    environment["HEARTBEAT_AGENT_DATABASE"] = str(store.database_path)
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen(_dispatcher_command(), **options)
    except OSError:
        return False
    return True


def ensure_continuous_dispatcher(store: AgentStore) -> bool:
    """Keep one scheduler alive whenever at least one repeat intent exists."""
    if not _active_automation_intents(store):
        return False
    current = store.get_dispatcher()
    if current is not None:
        observation = observe_process(current["pid"])
        if _dispatcher_is_live(current):
            return True
        if observation.liveness == "unknown":
            return False
        store.release_dispatcher(current["pid"], current.get("processIdentity"))
    if not launch_continuous_dispatcher(store):
        return False
    deadline = time.monotonic() + DISPATCHER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = store.get_dispatcher()
        if current is not None and _dispatcher_is_live(current):
            return True
        time.sleep(0.02)
    return False


def stop_continuous_dispatcher_for_service(
    store: AgentStore,
    *,
    timeout_seconds: float = 5.0,
) -> str:
    """Stop only the persisted dispatcher process whose OS identity still matches.

    The managed OS service must own the singleton after migration. A detached
    dispatcher left by an earlier control call would otherwise keep the DB claim
    forever and make the service repeatedly restart. PID reuse and unreadable
    process state are never guessed.
    """
    row = store.get_dispatcher()
    if row is None:
        return "none"
    observation = observe_process(row["pid"])
    identity = row.get("processIdentity")
    if observation.liveness == "unknown":
        return "blocked"
    if observation.liveness == "gone" or not identity or observation.identity != identity:
        store.release_dispatcher(row["pid"], identity)
        return "none"
    try:
        process = psutil.Process(row["pid"])
        process.terminate()
        process.wait(timeout=max(0.1, timeout_seconds))
    except psutil.NoSuchProcess:
        pass
    except (psutil.AccessDenied, psutil.TimeoutExpired):
        return "blocked"
    store.release_dispatcher(row["pid"], identity)
    return "stopped"


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _next_poll_at(now: datetime, seconds: int) -> str:
    return (now + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dispatcher_sleep_seconds(store: AgentStore, *, now: datetime) -> float:
    waits: list[float] = []
    for intent in _active_automation_intents(store):
        configuration = configuration_of(store, intent["projectId"])
        if configuration is None or not configuration.paused:
            due = _parse_utc(intent.get("nextPollAt"))
            waits.append(0.05 if due is None else max(0.05, (due - now).total_seconds()))
    if not waits:
        return DISPATCHER_POLL_CAP_SECONDS
    return min(DISPATCHER_POLL_CAP_SECONDS, min(waits))


def _active_automation_intents(store: AgentStore) -> list[dict[str, Any]]:
    active = []
    for intent in store.list_all_intents():
        configuration = configuration_of(store, intent["projectId"])
        if (
            configuration is not None
            and configuration.automation_enabled
            and not configuration.paused
            and configuration.roles.get(intent["role"]) is not None
            and configuration.roles[intent["role"]].execution_mode == "continuous"
        ):
            active.append(intent)
    return active


class _WorkflowEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, project_id: str, changed: Callable[[str], None]) -> None:
        super().__init__()
        self.project_id = project_id
        self.changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        if not getattr(event, "is_directory", False):
            self.changed(self.project_id)


class WorkflowChangeWatcher:
    """Watch enabled projects and expose only debounced project identities."""

    def __init__(self, store: AgentStore, *, debounce_seconds: float = 0.5) -> None:
        self.store = store
        self.debounce_seconds = debounce_seconds
        self._observer: Any | None = None
        self._roots: dict[str, str] = {}
        self._changed_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self.status = "stopped"

    def _changed(self, project_id: str) -> None:
        with self._lock:
            self._changed_at[project_id] = time.monotonic()

    def refresh(self, configurations: Sequence[AgentConfiguration]) -> None:
        roots = {
            configuration.project_id: str(Path(configuration.working_directory) / ".workflow")
            for configuration in configurations
            if (Path(configuration.working_directory) / ".workflow").is_dir()
        }
        if roots == self._roots and self._observer is not None:
            return
        self.stop()
        self._roots = roots
        if not roots:
            return
        if Observer is None:
            self.status = "degraded"
            for project_id in roots:
                self.store.save_watcher_state(
                    project_id,
                    "degraded",
                    error="watcher_dependency_unavailable",
                )
            return
        try:
            observer = Observer()
            for project_id, root in roots.items():
                observer.schedule(_WorkflowEventHandler(project_id, self._changed), root, recursive=True)
            observer.start()
            self._observer = observer
            self.status = "watching"
            for project_id in roots:
                self.store.save_watcher_state(project_id, "watching")
        except (OSError, RuntimeError) as error:
            self.status = "degraded"
            self._observer = None
            for project_id in roots:
                self.store.save_watcher_state(
                    project_id,
                    "degraded",
                    error=type(error).__name__,
                )

    def ready_projects(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            ready = [
                project_id
                for project_id, changed_at in self._changed_at.items()
                if now - changed_at >= self.debounce_seconds
            ]
            for project_id in ready:
                self._changed_at.pop(project_id, None)
        return ready

    def stop(self) -> None:
        project_ids = tuple(self._roots)
        observer = self._observer
        self._observer = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)
        self._roots = {}
        with self._lock:
            self._changed_at.clear()
        self.status = "stopped"
        for project_id in project_ids:
            self.store.save_watcher_state(project_id, "stopped")


def serve_continuous_intents(
    store: AgentStore,
    *,
    helpers_factory: Callable[[Path], WorkflowHelpers] = WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
) -> int:
    """Own repeat policies for the lifetime of the managed OS service."""
    pid = os.getpid()
    identity = process_identity(pid)
    if not identity or not _claim_dispatcher_process(store, pid, identity):
        return 0
    watcher = WorkflowChangeWatcher(store)
    try:
        while True:
            intents = _active_automation_intents(store)
            configurations = []
            seen_projects: set[str] = set()
            for intent in intents:
                if intent["projectId"] in seen_projects:
                    continue
                configuration = configuration_of(store, intent["projectId"])
                if configuration is not None:
                    configurations.append(configuration)
                    seen_projects.add(configuration.project_id)
            watcher.refresh(configurations)
            for project_id in watcher.ready_projects():
                store.mark_automations_due(project_id)
                store.save_watcher_state(
                    project_id,
                    "watching",
                    last_event_at=utc_now(),
                )
            now = datetime.now(UTC)
            tick_all_projects(
                store,
                helpers_factory=helpers_factory,
                provider_factory=provider_factory,
                detached_workers=True,
                now=now,
            )
            if not intents:
                watcher.refresh([])
                time.sleep(1.0)
                continue
            fallback_sleep = _dispatcher_sleep_seconds(store, now=datetime.now(UTC))
            time.sleep(min(0.1 if watcher.status == "watching" else 1.0, fallback_sleep))
        return 0
    finally:
        watcher.stop()
        store.release_dispatcher(pid, identity)


def handle_of(row: dict[str, Any]) -> ProviderRunHandle:
    """Rebuild the handle a persisted run row describes."""
    return ProviderRunHandle(
        run_id=row["providerRunId"],
        provider=row["provider"],
        project_id=row["projectId"],
        role=row["role"],
        target_id=row["targetId"],
        pid=row["pid"],
        started_at=row["startedAt"],
        event_path=Path(row["eventPath"]),
        process_identity=row["processIdentity"],
        lease_id=row["leaseId"],
    )


def terminate_run_tree(row: dict[str, Any]) -> bool:
    """Stop a persisted run's process tree, but never a reused PID.

    The identity is checked first because the runtime may have restarted since
    the run began, and by then the number alone proves nothing.
    """
    observation = observe_process(row["pid"])
    if observation.liveness == "gone":
        return True
    if observation.liveness == "unknown" or observation.identity != row["processIdentity"]:
        return False
    try:
        root = psutil.Process(row["pid"])
        processes = [*root.children(recursive=True), root]
    except psutil.Error:
        return False
    for process in processes:
        try:
            process.terminate()
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(alive, timeout=3)
    return not alive


def _release_lease(helpers: WorkflowHelpers, row: dict[str, Any]) -> bool:
    """Release the run's own lease. Not being the owner is already released."""
    if not row.get("leaseId") or not row.get("targetId"):
        return True
    return helpers.release(row["targetId"], row["leaseId"]) in (0, 5)


def _drain_events(store: AgentStore, provider: AgentProvider, row: dict[str, Any]) -> None:
    batch = provider.watch(handle_of(row), offset=row.get("lastOffset", 0))
    if batch.events:
        store.append_events(row["projectId"], row["runId"], [
            {
                "kind": event.kind, "provider": event.provider, "role": event.role,
                "targetId": event.target_id, "startedAt": event.started_at,
                "elapsedSeconds": event.elapsed_seconds, "rawId": event.raw_id, "detail": event.detail,
            }
            for event in batch.events
        ])
    row["lastOffset"] = batch.next_offset


def _live_supervisor_owned_elsewhere(row: dict[str, Any], supervisor_pid: int | None) -> bool:
    pid = row.get("supervisorPid")
    identity = row.get("supervisorIdentity")
    if not isinstance(pid, int) or not identity:
        return False
    observation = observe_process(pid)
    return (
        observation.liveness == "running"
        and observation.identity == identity
        and supervisor_pid != pid
    )


def reconcile_run(
    store: AgentStore,
    row: dict[str, Any],
    *,
    helpers: WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
    supervisor_pid: int | None = None,
) -> dict[str, Any]:
    """Bring one persisted run up to what its process and events now show."""
    if _live_supervisor_owned_elsewhere(row, supervisor_pid):
        return row

    worker_pid = row.get("supervisorPid")
    worker_identity = row.get("supervisorIdentity")
    if isinstance(worker_pid, int) and worker_identity and worker_pid != supervisor_pid:
        worker = observe_process(worker_pid)
        if worker.liveness == "unknown":
            # ``observe_process`` keeps ``unknown`` apart from ``gone`` so that an
            # unreadable process is never judged, and every other caller honours
            # that by leaving the state alone.  A worker can be momentarily
            # unobservable while it spawns its provider, and judging that beat as
            # an identity failure destroyed five newborn runs in one tick
            # (2026-08-18).  Leave the row as it stands; the next reconcile
            # observes again.
            return row
        if worker.liveness == "running" and worker.identity != worker_identity:
            row.update({
                "state": "recovery_required",
                "failureStage": "recovery",
                "reason": "supervisor_identity_unverified",
                "remaining": ["process_termination", "event_close", "lease_release"],
                "finishedAt": utc_now(),
            })
            store.save_run(row)
            store.record_error(
                row["projectId"], row["runId"],
                {"stage": "recovery", "reason": "supervisor_identity_unverified"},
            )
            return row
        if worker.liveness == "gone" and row.get("pid"):
            provider_process = observe_process(row["pid"])
            if provider_process.liveness != "gone":
                row.update({
                    "state": "recovery_required",
                    "failureStage": "recovery",
                    "reason": "supervisor_gone",
                    "remaining": ["process_termination", "event_close", "lease_release"],
                    "finishedAt": utc_now(),
                })
                store.save_run(row)
                store.record_error(
                    row["projectId"], row["runId"],
                    {"stage": "recovery", "reason": "supervisor_gone"},
                )
                return row

    if row["state"] == "queued":
        return _fail(store, row, stage="provider_start", reason="worker_not_running")
    if row["state"] == "reserved" or not row.get("providerRunId"):
        released = _release_lease(helpers, row)
        row["remaining"] = [] if released else ["lease_release"]
        return _fail(
            store,
            row,
            stage="provider_start",
            reason="reserved_without_process",
            state="failed" if released else "recovery_required",
        )

    provider = provider_factory(row["provider"])
    recovery = recover_run(provider, handle_of(row), offset=row.get("lastOffset", 0))
    _drain_events(store, provider, row)

    if recovery.outcome == "resumed":
        if not row.get("leaseHandedOver"):
            # Exit code 5 means the role session already released the lease it
            # was handed.  The process keeps running; only the renewing stops.
            row["leaseHandedOver"] = helpers.renew(row["targetId"], row["leaseId"]) == 5
        store.save_run(row)
        return row

    if recovery.outcome == "recovery_required":
        row.update({
            "state": "recovery_required",
            "failureStage": "recovery",
            "reason": recovery.reason,
            "remaining": list(recovery.remaining),
            "finishedAt": utc_now(),
        })
        store.save_run(row)
        store.record_error(row["projectId"], row["runId"], {"stage": "recovery", "reason": recovery.reason})
        return row

    final_event = last_event(Path(row["eventPath"]))
    event_kind = final_event.get("kind") if final_event is not None else None
    terminal = TERMINAL_BY_EVENT.get(event_kind if isinstance(event_kind, str) else "", "failed")
    event_detail = final_event.get("detail") if final_event is not None else None
    released = _release_lease(helpers, row)
    role_failure = released and terminal == "failed"
    row.update({
        "state": terminal if released else "recovery_required",
        "remaining": [] if released else ["lease_release"],
        "finishedAt": utc_now(),
        "failureStage": "role_session" if role_failure else (None if released else "cleanup"),
        "reason": (
            (event_detail if isinstance(event_detail, str) and event_detail else "provider_failed")
            if role_failure else (None if released else "lease_release_failed")
        ),
    })
    store.save_run(row)
    if role_failure:
        store.record_error(
            row["projectId"], row["runId"],
            {"stage": "role_session", "reason": row["reason"], "role": row["role"]},
        )
    return row


def reconcile_project_runs(
    store: AgentStore,
    project_id: str,
    *,
    helpers_factory: Callable[[Path], WorkflowHelpers] = WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
) -> int:
    """Recover only runs without another live detached supervisor."""
    configuration = configuration_of(store, project_id)
    if configuration is None:
        return 0
    helpers = helpers_factory(Path(configuration.working_directory))
    reconciled = 0
    for row in store.list_runs(project_id, states=ACTIVE_STATES):
        if _live_supervisor_owned_elsewhere(row, None):
            continue
        reconcile_run(store, row, helpers=helpers, provider_factory=provider_factory)
        reconciled += 1
    return reconciled


def sweep_dropped_runs(
    store: AgentStore,
    project_id: str,
    *,
    helpers: WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
) -> int:
    """Finish the cleanup dropped runs still owe, and only then free their slots.

    A row whose process still matches its recorded identity is a wrongly
    dropped worker that is alive and working: it is left completely alone —
    killing it would destroy healthy work, and releasing its lease would let a
    second session claim the same target. Unknown liveness counts as alive,
    because judging an unobservable process was the original mistake. A gone
    process (a reused PID is gone: the identity no longer matches) has nothing
    left to protect, so its remaining steps run here and the row moves to the
    terminal state its last event describes.
    """
    finished = 0
    for row in _occupying_runs(store, project_id):
        pid = row.get("pid")
        if isinstance(pid, int):
            observation = observe_process(pid)
            identity = row.get("processIdentity")
            if observation.liveness == "unknown":
                continue
            if observation.liveness == "running" and (not identity or observation.identity == identity):
                continue
        remaining = [step for step in row.get("remaining", ()) if step != "process_termination"]
        if "event_close" in remaining:
            # 남길 수 있는 이벤트는 장부로 옮기고 닫는다. 이벤트 파일이 이미 사라진
            # 행에서 닫기를 미제로 남기면 그 슬롯이 영영 풀리지 않으므로, 옮기기는
            # 최선 노력이고 닫힘은 여기서 확정된다.
            try:
                _drain_events(store, provider_factory(row["provider"]), row)
            except Exception:  # noqa: BLE001 - 기록을 더 못 옮겨도 정리는 진행한다
                pass
            remaining.remove("event_close")
        if "lease_release" in remaining and _release_lease(helpers, row):
            remaining.remove("lease_release")
        if remaining:
            row["remaining"] = remaining
            store.save_run(row)
            continue
        final_event = last_event(Path(row["eventPath"])) if row.get("eventPath") else None
        event_kind = final_event.get("kind") if final_event is not None else None
        row.update({
            "state": TERMINAL_BY_EVENT.get(event_kind if isinstance(event_kind, str) else "", "failed"),
            "remaining": [],
            "finishedAt": row.get("finishedAt") or utc_now(),
        })
        store.save_run(row)
        finished += 1
    return finished


def preview_cancel(row: dict[str, Any]) -> dict[str, Any]:
    """Describe what a cancel would touch, without touching any of it."""
    observation = observe_process(row["pid"]) if row.get("pid") else None
    children = 0
    if observation is not None and observation.liveness == "running":
        try:
            children = len(psutil.Process(row["pid"]).children(recursive=True))
        except psutil.Error:
            children = 0
    return {
        "runId": row["runId"],
        "targetId": row.get("targetId"),
        "leaseId": row.get("leaseId"),
        "pid": row.get("pid"),
        "processLiveness": observation.liveness if observation else "gone",
        "childProcesses": children,
        "cleanup": ["process_termination", "event_close", "lease_release"],
    }


def apply_cancel(store: AgentStore, row: dict[str, Any], *, helpers: WorkflowHelpers) -> dict[str, Any]:
    """Cancel one run and report each stage separately, never as one success."""
    stopped = terminate_run_tree(row) if row.get("pid") else True
    released = _release_lease(helpers, row)
    remaining = [stage for stage, done in (("process_termination", stopped), ("lease_release", released)) if not done]
    row.update({
        "state": "cancelled" if not remaining else "recovery_required",
        "remaining": remaining,
        "finishedAt": utc_now(),
        "failureStage": None if not remaining else "cleanup",
        "reason": None if not remaining else "partial_cleanup",
    })
    store.save_run(row)
    if remaining:
        store.record_error(row["projectId"], row["runId"], {"stage": "cleanup", "reason": "partial_cleanup"})
    return row


def retry_run(
    store: AgentStore,
    configuration: AgentConfiguration,
    previous: dict[str, Any],
    *,
    helpers: WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
) -> dict[str, Any]:
    """Start a new run that remembers the failed one, leaving that row intact."""
    return start_one_run(
        store,
        configuration,
        previous["role"],
        helpers=helpers,
        provider_factory=provider_factory,
        previous_run_id=previous["runId"],
    )


@dataclass
class TickReport:
    """What one dispatch tick did for one project."""

    project_id: str
    reconciled: int = 0
    cleaned: int = 0
    started: int = 0
    waiting: list[str] = field(default_factory=list)
    attention: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def tick_project(
    store: AgentStore,
    project_id: str,
    *,
    helpers_factory: Callable[[Path], WorkflowHelpers] = WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
    detached_workers: bool = False,
    now: datetime | None = None,
) -> TickReport:
    """Recover what is running, then refill repeat slots that are due."""
    report = TickReport(project_id=project_id)
    now = now or datetime.now(UTC)
    configuration = configuration_of(store, project_id)
    if configuration is None:
        return report
    helpers = helpers_factory(Path(configuration.working_directory))
    report.reconciled = reconcile_project_runs(
        store,
        project_id,
        helpers_factory=helpers_factory,
        provider_factory=provider_factory,
    )
    # 슬롯 집계를 읽기 전이어야 한다. 뒤에 두면 정리로 풀릴 슬롯이 이 주기 내내 점유로
    # 잡혀 배정이 한 주기를 통째로 쉰다. 일시정지 판정보다도 앞이다 — 정리는 배정이
    # 아니라 위생이라, 멈춘 프로젝트의 밀린 정리도 여기서 함께 끝난다.
    report.cleaned = sweep_dropped_runs(
        store,
        project_id,
        helpers=helpers,
        provider_factory=provider_factory,
    )

    # 회복 뒤, 배정 루프 앞이다. 앞에 두면 실행 중이던 세션의 감시가 끊기고, 뒤에 두면
    # 의도의 시작 수가 먼저 올라간다. 이 자리에서 반환하면 그 프로젝트만 멈추므로 같은
    # 기기의 다른 프로젝트 배정은 그대로 이어진다.
    if configuration.paused or not configuration.automation_enabled or not project_may_execute(
        store, project_id
    ):
        return report

    due_intents: list[dict[str, Any]] = []
    for intent in store.list_intents(project_id):
        role = intent["role"]
        policy = configuration.roles.get(role)
        if policy is None or policy.execution_mode != "continuous":
            store.drop_intent(intent["intentId"])
            continue
        manual = tuple(intent.get("manualTargets", ()))
        if intent["mode"] == "manual" and not manual:
            store.drop_intent(intent["intentId"])
            continue
        started_count = int(intent.get("startedCount", 0))
        if policy.execution_limit is not None and started_count >= policy.execution_limit:
            store.drop_intent(intent["intentId"])
            continue
        due = _parse_utc(intent.get("nextPollAt"))
        if due is not None and due > now:
            continue
        intent["nextPollAt"] = _next_poll_at(now, policy.poll_interval_seconds)
        intent["lastCheckAt"] = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        store.save_intent(project_id, intent)
        due_intents.append(intent)

    role_order = {role: index for index, role in enumerate(("planner", "architect", "developer"))}
    due_intents.sort(key=lambda intent: (
        bool(intent.get("lastAssignedAt")),
        intent.get("lastAssignedAt") or "",
        role_order.get(intent["role"], len(role_order)),
    ))

    # One role gets one seat per pass. Re-sorting on the next tick by
    # lastAssignedAt keeps scarce project slots moving between roles, while a
    # single tick can still fill every currently available seat.
    progress = True
    while due_intents and progress and min(
        _project_remaining(store, configuration), _device_remaining(store, configuration)
    ) > 0:
        progress = False
        for intent in list(due_intents):
            role = intent["role"]
            policy = configuration.roles[role]
            manual = tuple(intent.get("manualTargets", ()))
            started_count = int(intent.get("startedCount", 0))
            if _role_remaining(store, configuration, role) <= 0:
                due_intents.remove(intent)
                continue
            if policy.execution_limit is not None and started_count >= policy.execution_limit:
                store.drop_intent(intent["intentId"])
                due_intents.remove(intent)
                break
            if detached_workers:
                row = queue_and_launch_run(
                    store,
                    configuration,
                    role,
                    manual_targets=manual,
                    intent_id=intent["intentId"],
                )
            else:
                row = start_one_run(
                    store,
                    configuration,
                    role,
                    helpers=helpers,
                    provider_factory=provider_factory,
                    manual_targets=manual,
                    intent_id=intent["intentId"],
                )
            if row["state"] not in {"queued", "reserved", "running"}:
                reason = row.get("reason") or "start_failed"
                intent["lastResult"] = reason
                store.save_intent(project_id, intent)
                if row["state"] == "idle":
                    if reason in {"no-target", "no_target"}:
                        report.waiting.append(role)
                    else:
                        report.attention.append(reason)
                else:
                    report.failures.append(reason)
                if row.get("failureStage") == "request_validation" and row["state"] != "idle":
                    store.drop_intent(intent["intentId"])
                due_intents.remove(intent)
                continue
            report.started += 1
            progress = True
            started_count += 1
            intent["startedCount"] = started_count
            intent["lastResult"] = "running"
            intent["lastAssignedAt"] = utc_now()
            if manual:
                intent["manualTargets"] = [target for target in manual if target != row["targetId"]]
                manual = tuple(intent["manualTargets"])
            if (intent["mode"] == "manual" and not manual) or (
                policy.execution_limit is not None and started_count >= policy.execution_limit
            ):
                store.drop_intent(intent["intentId"])
                due_intents.remove(intent)
                continue
            store.save_intent(project_id, intent)
            if _role_remaining(store, configuration, role) <= 0:
                due_intents.remove(intent)
            if min(_project_remaining(store, configuration), _device_remaining(store, configuration)) <= 0:
                break
    return report


def tick_all_projects(
    store: AgentStore,
    *,
    helpers_factory: Callable[[Path], WorkflowHelpers] = WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
    detached_workers: bool = False,
    now: datetime | None = None,
) -> list[TickReport]:
    """Serve every configured project, least recently started first.

    One project's broken configuration or provider never ends the sweep, and
    the order is what keeps a repeating project from holding the device slots.
    """
    reports = []
    for project_id in _fair_order(store):
        try:
            reports.append(
                tick_project(
                    store,
                    project_id,
                    helpers_factory=helpers_factory,
                    provider_factory=provider_factory,
                    detached_workers=detached_workers,
                    now=now,
                )
            )
        except Exception as error:  # noqa: BLE001 - one project must not stop the sweep
            report = TickReport(project_id=project_id)
            report.failures.append(type(error).__name__)
            store.record_error(project_id, None, {"stage": "reservation", "reason": type(error).__name__})
            reports.append(report)
    return reports


def _fair_order(store: AgentStore) -> list[str]:
    latest: dict[str, str] = {}
    for run in store.list_runs():
        started = run.get("startedAt") or run.get("createdAt") or ""
        latest[run["projectId"]] = max(latest.get(run["projectId"], ""), started)
    return sorted(store.list_project_ids(), key=lambda project_id: (latest.get(project_id, ""), project_id))
