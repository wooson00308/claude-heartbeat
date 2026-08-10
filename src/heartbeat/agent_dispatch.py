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
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from heartbeat.agent_contract import (
    AgentConfiguration,
    validate_configuration,
)
from heartbeat.agent_store import AgentStore, utc_now
from heartbeat.providers import ClaudeProvider, CodexProvider
from heartbeat.providers.lifecycle import (
    Reservation,
    LifecycleFailure,
    last_event_kind,
    read_reservation,
    recover_run,
    start_reserved_run,
)
from heartbeat.providers.process import (
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
            "diagnostic": self.diagnostic,
        }


def configuration_of(store: AgentStore, project_id: str) -> AgentConfiguration | None:
    stored = store.get_configuration(project_id)
    return validate_configuration(stored) if stored else None


def runtime_revision(store: AgentStore, configuration: AgentConfiguration) -> str:
    """Fingerprint everything a plan assumed, so a changed world is visible."""
    active = sorted(run["runId"] for run in store.list_runs(states=ACTIVE_STATES))
    payload = json.dumps(
        {"configuration": configuration.to_dict(), "deviceMaxParallel": store.device_limit(), "active": active},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _device_remaining(store: AgentStore, configuration: AgentConfiguration) -> int:
    limit = store.device_limit()
    return max(0, limit - len(store.list_runs(states=ACTIVE_STATES)))


def _project_remaining(store: AgentStore, configuration: AgentConfiguration) -> int:
    active = len(store.list_runs(configuration.project_id, states=ACTIVE_STATES))
    return max(0, configuration.project_max_parallel - active)


def _role_remaining(store: AgentStore, configuration: AgentConfiguration, role: str) -> int:
    active = [run for run in store.list_runs(configuration.project_id, states=ACTIVE_STATES) if run["role"] == role]
    return max(0, configuration.roles[role].max_parallel - len(active))


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
    locked = helpers.migration_locked()
    plans: list[RolePlan] = []
    budget = min(device_remaining, project_remaining)

    for request in requests:
        policy = configuration.roles[request.role]
        diagnostic = provider_factory(policy.provider).diagnose()
        plan = RolePlan(
            role=request.role,
            provider=policy.provider,
            execution_mode=policy.execution_mode,
            requested=request.slots,
            granted=0,
            diagnostic={"status": diagnostic.status, "provider": diagnostic.provider, "version": diagnostic.version},
        )
        granted = min(request.slots, _role_remaining(store, configuration, request.role), budget)
        if policy.execution_limit is not None:
            granted = min(granted, policy.execution_limit)
        if configuration.paused:
            granted = 0
            plan.excluded.append("project_paused")
        if locked:
            granted = 0
            plan.excluded.append("migration_lock")
        if diagnostic.status != "ready":
            granted = 0
            plan.excluded.append(f"provider_{diagnostic.status}")
        if request.manual_targets:
            accepted, reasons = validate_manual_targets(helpers, request.manual_targets, now=now)
            plan.manual_targets = accepted
            plan.excluded.extend(reasons)
            granted = min(granted, len(accepted))
        if granted < request.slots and not plan.excluded:
            plan.excluded.append(
                "execution_limit" if policy.execution_limit is not None and granted == policy.execution_limit
                else "limit_reached"
            )
        plan.granted = granted
        budget -= granted
        plans.append(plan)

    return {
        "planId": uuid.uuid4().hex,
        "projectId": configuration.project_id,
        "revision": runtime_revision(store, configuration),
        "expiresAt": (now + timedelta(seconds=PLAN_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deviceRemaining": device_remaining,
        "projectRemaining": project_remaining,
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


def _fail(store: AgentStore, row: dict[str, Any], *, stage: str, reason: str, state: str = "failed") -> dict[str, Any]:
    row.update({"state": state, "failureStage": stage, "reason": reason})
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
) -> dict[str, Any]:
    """Persist work before a detached supervisor is launched for it."""
    policy = configuration.roles[role]
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
    row["state"] = "reserved"
    store.save_run(row)
    reservation = helpers.reserve(role, f"heartbeat-runtime-{role}")
    if isinstance(reservation, DispatchFailure):
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

    provider = provider_factory(policy.provider)
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
    row = queue_one_run(
        store,
        configuration,
        role,
        manual_targets=manual_targets,
        previous_run_id=previous_run_id,
        intent_id=intent_id,
    )
    if launch_run_worker(store, row["runId"]):
        deadline = time.monotonic() + WORKER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            current = store.get_run(row["runId"]) or row
            if current["state"] not in {"queued", "reserved"} or current.get("targetId"):
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
        return 1

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
    if not store.list_all_intents():
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
    for intent in store.list_all_intents():
        configuration = configuration_of(store, intent["projectId"])
        if configuration is None or not configuration.paused:
            due = _parse_utc(intent.get("nextPollAt"))
            waits.append(0.05 if due is None else max(0.05, (due - now).total_seconds()))
    if not waits:
        return DISPATCHER_POLL_CAP_SECONDS
    return min(DISPATCHER_POLL_CAP_SECONDS, min(waits))


def serve_continuous_intents(
    store: AgentStore,
    *,
    helpers_factory: Callable[[Path], WorkflowHelpers] = WorkflowHelpers,
    provider_factory: Callable[[str], AgentProvider] = build_provider,
) -> int:
    """Own all repeat policies until none remain, independent of the GUI."""
    pid = os.getpid()
    identity = process_identity(pid)
    if not identity or not store.claim_dispatcher(pid, identity):
        return 0
    try:
        while store.list_all_intents():
            now = datetime.now(UTC)
            tick_all_projects(
                store,
                helpers_factory=helpers_factory,
                provider_factory=provider_factory,
                detached_workers=True,
                now=now,
            )
            if not store.list_all_intents():
                break
            time.sleep(_dispatcher_sleep_seconds(store, now=datetime.now(UTC)))
        return 0
    finally:
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
        if worker.liveness == "unknown" or (
            worker.liveness == "running" and worker.identity != worker_identity
        ):
            row.update({
                "state": "recovery_required",
                "failureStage": "recovery",
                "reason": "supervisor_identity_unverified",
                "remaining": ["process_termination", "event_close", "lease_release"],
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
        })
        store.save_run(row)
        store.record_error(row["projectId"], row["runId"], {"stage": "recovery", "reason": recovery.reason})
        return row

    terminal = TERMINAL_BY_EVENT.get(last_event_kind(Path(row["eventPath"])) or "", "failed")
    released = _release_lease(helpers, row)
    row.update({
        "state": terminal if released else "recovery_required",
        "remaining": [] if released else ["lease_release"],
        "failureStage": None if released else "cleanup",
        "reason": None if released else "lease_release_failed",
    })
    store.save_run(row)
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
    started: int = 0
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

    if configuration.paused:
        return report

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
        store.save_intent(project_id, intent)
        while _role_remaining(store, configuration, role) > 0 and min(
            _project_remaining(store, configuration), _device_remaining(store, configuration)
        ) > 0:
            if policy.execution_limit is not None and started_count >= policy.execution_limit:
                store.drop_intent(intent["intentId"])
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
                report.failures.append(row.get("reason") or "start_failed")
                if row.get("failureStage") == "request_validation":
                    store.drop_intent(intent["intentId"])
                break
            report.started += 1
            started_count += 1
            intent["startedCount"] = started_count
            if manual:
                intent["manualTargets"] = [target for target in manual if target != row["targetId"]]
                manual = tuple(intent["manualTargets"])
            if (intent["mode"] == "manual" and not manual) or (
                policy.execution_limit is not None and started_count >= policy.execution_limit
            ):
                store.drop_intent(intent["intentId"])
                break
            store.save_intent(project_id, intent)
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
