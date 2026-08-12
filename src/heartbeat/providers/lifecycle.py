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
    if decoded.get("contractVersion") != RESERVATION_CONTRACT_VERSION:
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
    return Reservation(
        contract_version=RESERVATION_CONTRACT_VERSION,
        prompt_version=prompt_version,
        **values,
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
        return LifecycleFailure(
            stage="provider_start",
            reason=started.stage,
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
