"""Reservation-bound run lifecycle a dispatcher can persist, resume, and close.

``process.py`` owns one provider process: start it, read its event file, stop
it, wait for it.  This module binds those steps to the work the runtime already
reserved, and adds the two judgements a dispatcher cannot make from the process
alone: whether a persisted handle still names the same live process, and which
cleanup stages a finished or cancelled run leaves behind.

Candidate selection, queue fairness, SQLite persistence and lease renewal stay
outside.  This module reads a reservation response and reports what a run still
needs; it never reserves, releases, or stores anything.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from heartbeat.providers.process import (
    AgentProvider,
    CancelSignal,
    ProviderEvent,
    ProviderExecutionRequest,
    ProviderRunHandle,
    ProviderStartFailure,
    RunStatus,
    observe_process,
)

PROVIDER_LIFECYCLE_CONTRACT_VERSION = 1
RESERVATION_CONTRACT_VERSION = 1
# 계약 2는 개발 세션의 격리 작업 사본을 싣는다. 사본 준비와 세션 지시는 예약 헬퍼가 하고, 이
# 런타임은 그 사실을 담은 필드를 알아보고 통과시킨다. 버전을 검증하는 이유가 "격리를 모르는
# 실행 환경은 개발 실행을 시작하지 않는다"이므로, 여기 들어 있다는 것이 곧 그 선언이다.
SUPPORTED_RESERVATION_CONTRACT_VERSIONS = (1, 2)
# 계약 2가 실어 오는 격리 필드. 없으면 None으로 남는다 — 계약 1의 예약과 격리를 준비하지 않는
# 역할(기획자·아키텍트)의 예약이 그 모양이다.
_ISOLATION_FIELDS = {
    "workspace_path": "workspacePath",
    "control_root": "controlRoot",
    "base_commit": "baseCommit",
    "branch": "branch",
}
TERMINAL_EVENT_KINDS = frozenset({"completed", "failed", "cancelled", "timed_out"})

LifecycleStage = Literal["reservation", "provider_start"]
CleanupStage = Literal["process_termination", "event_close", "lease_release"]
RecoveryOutcome = Literal["resumed", "recovery_required", "cleanup_required"]

# One event line is bounded before it is written, so the last complete line of
# an event file always lies inside this window.
_TAIL_WINDOW = 65536


@dataclass(frozen=True)
class Reservation:
    """One successful reservation response, kept as the reservation helper wrote it.

    The helper owns these names and this module never rewrites them.
    ``role_prompt`` is the role handoff text.  It is delivered on the child
    process's standard input only, and never copied into a handle, an event
    file, a terminal result, or a log.
    """

    contract_version: int
    role: str
    target_id: str
    lease_id: str
    result_prefix: str
    expires_at: str
    prompt_version: int
    role_prompt: str
    # 계약 2의 격리 작업 사본. 계약 1이거나 격리 없는 역할이면 None이다.
    workspace_path: str | None = None
    control_root: str | None = None
    base_commit: str | None = None
    branch: str | None = None


@dataclass(frozen=True)
class LifecycleFailure:
    """A run that never started, named by the stage that refused it."""

    stage: LifecycleStage
    reason: str
    start_failure: ProviderStartFailure | None = None


@dataclass(frozen=True)
class RunConclusion:
    """A finished run tied back to its reservation, with what is still open."""

    run_id: str
    target_id: str
    lease_id: str | None
    status: RunStatus
    returncode: int | None
    process_stopped: bool
    events_closed: bool
    remaining: tuple[CleanupStage, ...]
    events: tuple[ProviderEvent, ...]
    next_offset: int
    detail: str | None = None


@dataclass(frozen=True)
class RunRecovery:
    """What a persisted handle turned out to name after a runtime restart."""

    run_id: str
    target_id: str
    lease_id: str | None
    outcome: RecoveryOutcome
    reason: str | None
    observed_identity: str | None
    events: tuple[ProviderEvent, ...]
    next_offset: int
    remaining: tuple[CleanupStage, ...] = ()


def read_reservation(exit_code: int, payload: str) -> Reservation | LifecycleFailure:
    """Read one reservation response, or say why there is no run to start.

    A failed reservation, a lost race and a migration lock reach this function
    as the same exit code with no output, so they produce one reason.  What the
    caller must do is identical in all three: nothing was reserved, so no
    provider is started.
    """
    if exit_code == 2:
        return LifecycleFailure(stage="reservation", reason="reservation_usage_error")
    if exit_code != 0:
        return LifecycleFailure(stage="reservation", reason="reservation_unavailable")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return LifecycleFailure(stage="reservation", reason="reservation_malformed")
    if not isinstance(decoded, dict):
        return LifecycleFailure(stage="reservation", reason="reservation_malformed")
    contract_version = decoded.get("contractVersion")
    if contract_version not in SUPPORTED_RESERVATION_CONTRACT_VERSIONS:
        return LifecycleFailure(stage="reservation", reason="unsupported_reservation_contract")

    text_fields = {
        "role": "role",
        "target_id": "targetId",
        "lease_id": "leaseId",
        "result_prefix": "resultPrefix",
        "expires_at": "expiresAt",
        "role_prompt": "rolePrompt",
    }
    values: dict[str, str] = {}
    for field, key in text_fields.items():
        value = decoded.get(key)
        if not isinstance(value, str) or not value.strip():
            return LifecycleFailure(stage="reservation", reason="reservation_malformed")
        values[field] = value
    prompt_version = decoded.get("promptVersion")
    if isinstance(prompt_version, bool) or not isinstance(prompt_version, int):
        return LifecycleFailure(stage="reservation", reason="reservation_malformed")
    isolation: dict[str, str | None] = {}
    for field, key in _ISOLATION_FIELDS.items():
        value = decoded.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return LifecycleFailure(stage="reservation", reason="reservation_malformed")
        isolation[field] = value
    return Reservation(
        contract_version=contract_version,
        prompt_version=prompt_version,
        role=values["role"],
        target_id=values["target_id"],
        lease_id=values["lease_id"],
        result_prefix=values["result_prefix"],
        expires_at=values["expires_at"],
        role_prompt=values["role_prompt"],
        workspace_path=isolation["workspace_path"],
        control_root=isolation["control_root"],
        base_commit=isolation["base_commit"],
        branch=isolation["branch"],
    )


def start_reserved_run(
    provider: AgentProvider,
    reservation: Reservation,
    request: ProviderExecutionRequest,
    *,
    environment: Mapping[str, str] | None = None,
    cancel_event: CancelSignal | None = None,
) -> ProviderRunHandle | LifecycleFailure:
    """Start one run only when the request names exactly the reserved work.

    The comparison happens before the provider is touched, so a request for
    another target or another lease starts nothing.  The handle is compared
    again afterwards, because only a handle that carries the reservation can be
    persisted as that reservation's run.
    """
    if not _matches(reservation, request.role, request.target_id, request.lease_id):
        return LifecycleFailure(stage="reservation", reason="reservation_mismatch")

    started = provider.start(request, environment=environment, cancel_event=cancel_event)
    if isinstance(started, ProviderStartFailure):
        # 진단 실패의 사유는 단계 이름이 아니라 진단 상태다. detail이 그 상태를 담고 있으므로
        # 계획 조회의 제외 사유와 같은 provider_ 접두 코드로 남긴다. 화면은 이 코드로 사용자가
        # 할 다음 행동(설치·로그인·업데이트)을 말할 수 있다.
        reason = started.stage
        if started.stage == "diagnostic" and started.detail:
            reason = f"provider_{started.detail}"
        return LifecycleFailure(
            stage="provider_start",
            reason=reason,
            start_failure=started,
        )
    if not _matches(reservation, started.role, started.target_id, started.lease_id):
        provider.cancel(started)
        provider.wait(started)
        return LifecycleFailure(stage="provider_start", reason="handle_mismatch")
    return started


def conclude_run(
    provider: AgentProvider,
    handle: ProviderRunHandle,
    *,
    offset: int = 0,
) -> RunConclusion:
    """Wait for a started run and report its result against the reservation.

    Waiting reports the role session's own outcome.  A successful start never
    becomes a successful run here: ``status`` comes from the provider result.
    """
    result = provider.wait(handle)
    return _conclude(
        provider,
        handle,
        status=result.status,
        returncode=result.returncode,
        process_stopped=_process_stopped(handle),
        offset=offset,
        detail=result.detail,
    )


def cancel_run(
    provider: AgentProvider,
    handle: ProviderRunHandle,
    *,
    offset: int = 0,
) -> RunConclusion:
    """Cancel one run through its handle and report every stage left open."""
    cancellation = provider.cancel(handle)
    if cancellation.detail == "unknown_run":
        # A handle this provider never started, which is what a restarted
        # runtime holds.  Nothing here can wait for it, so the stages are
        # reported from what the PID and the event file still show, and
        # ``recover_run`` decides what that PID actually is.
        return _conclude(
            provider,
            handle,
            status="cancelled",
            returncode=None,
            process_stopped=_process_stopped(handle),
            offset=offset,
            detail="unknown_run",
        )
    result = provider.wait(handle)
    return _conclude(
        provider,
        handle,
        status=result.status,
        returncode=result.returncode,
        process_stopped=cancellation.process_stopped and _process_stopped(handle),
        offset=offset,
        detail=cancellation.detail or result.detail,
    )


def recover_run(
    provider: AgentProvider,
    handle: ProviderRunHandle,
    *,
    offset: int = 0,
) -> RunRecovery:
    """Decide what a persisted handle names now, without starting anything.

    A PID whose creation identity differs, or that cannot be inspected at all,
    is never adopted as the running job.  Only an identical live process is
    resumed, and then the events continue from the offset the caller last
    persisted.
    """
    observation = observe_process(handle.pid)
    batch = provider.watch(handle, offset=offset)
    if handle.process_identity is None:
        outcome: RecoveryOutcome = "recovery_required"
        reason: str | None = "handle_identity_missing"
    elif observation.liveness == "unknown":
        outcome, reason = "recovery_required", "process_identity_unavailable"
    elif observation.liveness == "gone":
        outcome, reason = "cleanup_required", "process_exited"
    elif observation.identity != handle.process_identity:
        outcome, reason = "recovery_required", "process_identity_mismatch"
    else:
        outcome, reason = "resumed", None

    remaining: tuple[CleanupStage, ...] = ()
    if outcome == "cleanup_required":
        remaining = _remaining_stages(
            handle,
            process_stopped=True,
            events_closed=_events_closed(handle.event_path),
        )
    return RunRecovery(
        run_id=handle.run_id,
        target_id=handle.target_id,
        lease_id=handle.lease_id,
        outcome=outcome,
        reason=reason,
        observed_identity=observation.identity,
        events=batch.events,
        next_offset=batch.next_offset,
        remaining=remaining,
    )


def last_event_kind(event_path: Path) -> str | None:
    """Return the kind of the last complete event, or None when there is none."""
    event = last_event(event_path)
    kind = event.get("kind") if event is not None else None
    return kind if isinstance(kind, str) else None


def last_event(event_path: Path) -> dict[str, object] | None:
    """Return the final complete normalized event without retaining raw provider output."""
    try:
        with event_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - _TAIL_WINDOW))
            data = stream.read()
    except OSError:
        return None
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return None
    lines = [line for line in data[:boundary].split(b"\n") if line.strip()]
    if not lines:
        return None
    try:
        decoded = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _matches(
    reservation: Reservation,
    role: str,
    target_id: str,
    lease_id: str | None,
) -> bool:
    return (role, target_id, lease_id) == (
        reservation.role,
        reservation.target_id,
        reservation.lease_id,
    )


def _events_closed(event_path: Path) -> bool:
    return last_event_kind(event_path) in TERMINAL_EVENT_KINDS


def _process_stopped(handle: ProviderRunHandle) -> bool:
    """Report whether the started process is confirmed to be gone.

    A PID that now belongs to a different process counts as gone, and a PID
    that cannot be inspected is never counted as gone.
    """
    observation = observe_process(handle.pid)
    if observation.liveness == "unknown":
        return False
    return observation.liveness == "gone" or observation.identity != handle.process_identity


def _remaining_stages(
    handle: ProviderRunHandle,
    *,
    process_stopped: bool,
    events_closed: bool,
) -> tuple[CleanupStage, ...]:
    """List the cleanup this run still needs, in the order it must happen."""
    stages: list[CleanupStage] = []
    if not process_stopped:
        stages.append("process_termination")
    if not events_closed:
        stages.append("event_close")
    if handle.lease_id:
        # The lease is the caller's to release, so it stays on this list
        # whenever a run carries one.  This module never touches lease files.
        stages.append("lease_release")
    return tuple(stages)


def _conclude(
    provider: AgentProvider,
    handle: ProviderRunHandle,
    *,
    status: RunStatus,
    returncode: int | None,
    process_stopped: bool,
    offset: int,
    detail: str | None,
) -> RunConclusion:
    batch = provider.watch(handle, offset=offset)
    events_closed = _events_closed(handle.event_path)
    return RunConclusion(
        run_id=handle.run_id,
        target_id=handle.target_id,
        lease_id=handle.lease_id,
        status=status,
        returncode=returncode,
        process_stopped=process_stopped,
        events_closed=events_closed,
        remaining=_remaining_stages(
            handle,
            process_stopped=process_stopped,
            events_closed=events_closed,
        ),
        events=batch.events,
        next_offset=batch.next_offset,
        detail=detail,
    )
