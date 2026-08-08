"""Provider-neutral process supervision and result types.

The runtime deliberately keeps this module free of provider SDK imports.  A
provider receives a prompt on stdin, emits normalized events through a callback,
and is always launched in a process group that can be cleaned up as one unit.

A run is started and supervised separately: ``start`` returns a handle as soon
as the process exists, ``watch`` replays the run's event file from a byte
offset, ``cancel`` stops the tree, and ``wait`` produces the terminal result.
``run`` remains the one-call path built from those steps.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

import psutil

DiagnosticStatus = Literal[
    "ready",
    "executable_missing",
    "login_required",
    "permission_denied",
    "unsupported_version",
    "billing_route_acknowledgement_required",
    "unavailable",
]
EventKind = Literal["started", "progress", "tool", "completed", "failed", "cancelled", "timed_out"]
RunStatus = Literal["success", "failed", "usage_limited", "cancelled", "timed_out", "off_contract"]
LineSource = Literal["stdout", "stderr"]
StartStage = Literal["diagnostic", "spawn", "prompt_delivery"]
ProcessLiveness = Literal["running", "gone", "unknown"]

PROVIDER_RUN_CONTRACT_VERSION = 1

_SENSITIVE_ENV_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|credential|authorization)", re.IGNORECASE)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*([:=])\s*([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)bearer\s+[^\s,;]+")


@dataclass(frozen=True)
class ProviderDiagnostic:
    """A safe readiness result that never includes credential values."""

    provider: str
    status: DiagnosticStatus
    executable: str
    detail: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class ProviderExecutionRequest:
    """Inputs to a one-shot provider run.

    ``prompt`` is intentionally not copied into events or results.  It is sent
    directly to the child process's standard input.  ``target_id`` and
    ``lease_id`` name the reserved work, so a started run can be matched against
    the reservation it was launched for.
    """

    project_id: str
    role: str
    target_id: str
    project_root: Path
    prompt: str
    model: str | None = None
    timeout_seconds: float | None = None
    billing_route_acknowledged: bool = False
    event_root: Path | None = None
    lease_id: str | None = None


@dataclass(frozen=True)
class ProviderEvent:
    """A provider-independent progress event safe to persist or display."""

    kind: EventKind
    provider: str
    project_id: str
    role: str
    target_id: str
    started_at: str
    elapsed_seconds: float
    raw_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ProcessExecution:
    """The terminal state of a supervised process, without captured output."""

    returncode: int | None
    state: Literal["exited", "cancelled", "timed_out", "start_failed"]
    detail: str | None = None
    tree_stopped: bool = True


@dataclass(frozen=True)
class ProcessObservation:
    """What the operating system could actually confirm about one PID."""

    liveness: ProcessLiveness
    identity: str | None


@dataclass(frozen=True)
class ProviderExecutionResult:
    """Normalized terminal state and events for one provider invocation."""

    provider: str
    status: RunStatus
    diagnostic: ProviderDiagnostic | None
    events: tuple[ProviderEvent, ...]
    returncode: int | None
    detail: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ProviderRunHandle:
    """A started run, identified well enough to be persisted and resumed.

    ``process_identity`` separates this process from a later process that
    happens to reuse the same PID.  ``event_path`` is the append-only JSONL file
    the run writes, so a restarted runtime can keep reading it by byte offset.
    ``target_id`` and ``lease_id`` are the reserved work this run belongs to.
    """

    run_id: str
    provider: str
    project_id: str
    role: str
    target_id: str
    pid: int
    started_at: str
    event_path: Path
    process_identity: str | None = None
    lease_id: str | None = None
    contract_version: int = PROVIDER_RUN_CONTRACT_VERSION


@dataclass(frozen=True)
class ProviderStartFailure:
    """A start that produced no handle, classified by the stage that failed."""

    provider: str
    stage: StartStage
    diagnostic: ProviderDiagnostic | None = None
    detail: str | None = None
    events: tuple[ProviderEvent, ...] = ()


@dataclass(frozen=True)
class ProviderEventBatch:
    """Complete events read after an offset, with the offset to resume from."""

    events: tuple[ProviderEvent, ...]
    next_offset: int


@dataclass(frozen=True)
class ProviderCancellation:
    """The per-stage outcome of cancelling one run through its handle."""

    run_id: str
    cancelled: bool
    process_stopped: bool
    events_closed: bool
    detail: str | None = None


@dataclass(frozen=True)
class ProbeFailure:
    """A probe could not start or finish, classified without retaining output."""

    status: DiagnosticStatus


@dataclass(frozen=True)
class NormalizedLine:
    """One CLI JSON event expressed in the common event vocabulary."""

    kind: EventKind
    raw_id: str | None = None
    detail: str | None = None
    status: RunStatus | None = None


class CancelSignal(Protocol):
    """Anything that can report a cancellation request, including ``Event``."""

    def is_set(self) -> bool:
        """Report whether cancellation was requested."""


class AgentProvider(Protocol):
    """The common interface implemented by every CLI-backed provider."""

    name: str

    def diagnose(self, *, environment: Mapping[str, str] | None = None) -> ProviderDiagnostic:
        """Report whether the provider can be started safely."""

    def start(
        self,
        request: ProviderExecutionRequest,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: CancelSignal | None = None,
    ) -> ProviderRunHandle | ProviderStartFailure:
        """Create the process and return its handle without waiting for it."""

    def watch(self, handle: ProviderRunHandle, *, offset: int = 0) -> ProviderEventBatch:
        """Return the complete events written after ``offset``."""

    def cancel(self, handle: ProviderRunHandle) -> ProviderCancellation:
        """Stop the run's process tree and report each cleanup stage."""

    def wait(self, handle: ProviderRunHandle) -> ProviderExecutionResult:
        """Wait for a started run and return its normalized terminal result."""

    def run(
        self,
        request: ProviderExecutionRequest,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProviderExecutionResult:
        """Start, observe, and clean up one non-interactive CLI process."""


class CliProvider:
    """Shared implementation for a non-interactive CLI provider.

    Subclasses contribute only their fixed argument array, authentication probe,
    and JSONL event mapping.  The process lifecycle and credential filtering are
    identical for every provider.
    """

    name: str
    executable: str
    minimum_version: tuple[int, ...] = (0,)
    billing_environment_keys: tuple[str, ...] = ()

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or self.executable
        self._runs: dict[str, _SupervisedRun] = {}
        self._runs_lock = threading.Lock()

    def command(self, request: ProviderExecutionRequest) -> list[str]:
        """Return the fixed non-shell command for one request."""
        raise NotImplementedError

    def authentication_command(self) -> list[str]:
        """Return the provider-owned local authentication status command."""
        raise NotImplementedError

    def normalize_line(self, value: dict[object, object]) -> tuple[NormalizedLine, ...]:
        """Map one provider JSON object into common events."""
        raise NotImplementedError

    def diagnose(self, *, environment: Mapping[str, str] | None = None) -> ProviderDiagnostic:
        """Check executable, supported version, and CLI-owned authentication."""
        return self._diagnose(self._environment(environment), billing_route_acknowledged=False)

    def start(
        self,
        request: ProviderExecutionRequest,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: CancelSignal | None = None,
    ) -> ProviderRunHandle | ProviderStartFailure:
        """Create the CLI process and return its handle before it finishes.

        Success here means the process exists and the prompt channel is closed.
        It says nothing about the role result, which only ``wait`` reports.
        """
        child_environment = self._environment(environment)
        diagnostic = self._diagnose(
            child_environment,
            billing_route_acknowledged=request.billing_route_acknowledged,
        )
        if diagnostic.status != "ready":
            return ProviderStartFailure(
                provider=self.name,
                stage="diagnostic",
                diagnostic=diagnostic,
                detail=diagnostic.status,
            )

        started_at = utc_now()
        event_root = request.event_root or default_event_root()
        event_root.mkdir(parents=True, exist_ok=True)
        spawned = spawn_process(
            self.command(request),
            cwd=request.project_root,
            environment=child_environment,
        )
        if isinstance(spawned, ProcessExecution):
            return ProviderStartFailure(
                provider=self.name,
                stage="spawn",
                diagnostic=diagnostic,
                detail=spawned.detail,
                events=self._start_failure_events(request, started_at, spawned.detail),
            )
        if not deliver_prompt(spawned, request.prompt):
            terminate_process_tree(spawned)
            spawned.wait()
            return ProviderStartFailure(
                provider=self.name,
                stage="prompt_delivery",
                diagnostic=diagnostic,
                detail="prompt_delivery_failed",
                events=self._start_failure_events(request, started_at, "prompt_delivery_failed"),
            )

        run_id = uuid.uuid4().hex
        handle = ProviderRunHandle(
            run_id=run_id,
            provider=self.name,
            project_id=request.project_id,
            role=request.role,
            target_id=request.target_id,
            pid=spawned.pid,
            started_at=started_at,
            event_path=event_root / f"{run_id}.jsonl",
            process_identity=process_identity(spawned.pid),
            lease_id=request.lease_id,
        )
        supervised = _SupervisedRun(
            provider=self,
            request=request,
            handle=handle,
            process=spawned,
            diagnostic=diagnostic,
            secrets=sensitive_values(child_environment, request.prompt),
            cancel_event=cancel_event,
        )
        with self._runs_lock:
            self._runs[run_id] = supervised
        supervised.begin()
        return handle

    def watch(self, handle: ProviderRunHandle, *, offset: int = 0) -> ProviderEventBatch:
        """Return the complete events written after ``offset``.

        A trailing partial line is withheld until it is complete, so re-reading
        the same offset always produces the same batch.
        """
        try:
            with handle.event_path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read()
        except FileNotFoundError:
            return ProviderEventBatch((), offset)
        boundary = data.rfind(b"\n")
        if boundary < 0:
            return ProviderEventBatch((), offset)
        complete = data[: boundary + 1]
        events = []
        for line in complete.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(ProviderEvent(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return ProviderEventBatch(tuple(events), offset + len(complete))

    def cancel(self, handle: ProviderRunHandle) -> ProviderCancellation:
        """Stop the run's process tree and report every cleanup stage."""
        with self._runs_lock:
            supervised = self._runs.get(handle.run_id)
        if supervised is None:
            return ProviderCancellation(
                run_id=handle.run_id,
                cancelled=False,
                process_stopped=False,
                events_closed=False,
                detail="unknown_run",
            )
        return supervised.cancel()

    def wait(self, handle: ProviderRunHandle) -> ProviderExecutionResult:
        """Wait for a started run and return its normalized terminal result.

        A handle stays addressable through ``cancel`` and ``wait`` until this
        returns, which is where the run leaves the provider's live registry.
        The event file outlives it, so ``watch`` keeps working afterwards.
        """
        with self._runs_lock:
            supervised = self._runs.get(handle.run_id)
        if supervised is None:
            raise LookupError(f"no active run for handle {handle.run_id}")
        result = supervised.wait()
        with self._runs_lock:
            self._runs.pop(handle.run_id, None)
        return result

    def run(
        self,
        request: ProviderExecutionRequest,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProviderExecutionResult:
        """Run a CLI with a stdin-only prompt and normalized event stream."""
        started = self.start(request, environment=environment, cancel_event=cancel_event)
        if isinstance(started, ProviderStartFailure):
            return ProviderExecutionResult(
                provider=self.name,
                status="failed",
                diagnostic=started.diagnostic,
                events=started.events,
                returncode=None,
                detail=started.detail,
            )
        return self.wait(started)

    def _start_failure_events(
        self,
        request: ProviderExecutionRequest,
        started_at: str,
        detail: str | None,
    ) -> tuple[ProviderEvent, ...]:
        return (
            build_event("started", provider=self.name, request=request, started_at=started_at),
            build_event(
                "failed",
                provider=self.name,
                request=request,
                started_at=started_at,
                detail=detail,
            ),
        )

    def _environment(self, environment: Mapping[str, str] | None) -> dict[str, str]:
        result = dict(os.environ)
        if environment is not None:
            result.update(environment)
        return result

    def _diagnose(
        self,
        environment: Mapping[str, str],
        *,
        billing_route_acknowledged: bool,
    ) -> ProviderDiagnostic:
        if not self._executable_exists():
            return ProviderDiagnostic(self.name, "executable_missing", self.executable)

        version_probe = run_probe([self.executable, "--version"], environment=environment)
        if isinstance(version_probe, ProbeFailure):
            return ProviderDiagnostic(self.name, version_probe.status, self.executable)
        version_output = f"{version_probe.stdout}\n{version_probe.stderr}"
        if version_probe.returncode != 0:
            return ProviderDiagnostic(self.name, self._probe_failure_status(version_output), self.executable)
        parsed_version = extract_version(version_output)
        if parsed_version is None or not version_at_least(parsed_version, self.minimum_version):
            return ProviderDiagnostic(self.name, "unsupported_version", self.executable)

        if not billing_route_acknowledged and any(environment.get(key) for key in self.billing_environment_keys):
            return ProviderDiagnostic(
                self.name,
                "billing_route_acknowledgement_required",
                self.executable,
                detail="API billing route acknowledgement is required before starting this provider.",
            )

        auth_probe = run_probe(self.authentication_command(), environment=environment)
        if isinstance(auth_probe, ProbeFailure):
            return ProviderDiagnostic(self.name, auth_probe.status, self.executable, version=".".join(map(str, parsed_version)))
        if auth_probe.returncode != 0:
            output = f"{auth_probe.stdout}\n{auth_probe.stderr}"
            return ProviderDiagnostic(
                self.name,
                self._authentication_failure_status(output),
                self.executable,
                version=".".join(map(str, parsed_version)),
            )
        return ProviderDiagnostic(self.name, "ready", self.executable, version=".".join(map(str, parsed_version)))

    def _executable_exists(self) -> bool:
        if os.path.sep in self.executable or (os.altsep and os.altsep in self.executable):
            path = Path(self.executable)
            return path.is_file()
        return shutil.which(self.executable) is not None

    @staticmethod
    def _probe_failure_status(output: str) -> DiagnosticStatus:
        lowered = output.casefold()
        if "permission" in lowered or "denied" in lowered:
            return "permission_denied"
        return "unavailable"

    @staticmethod
    def _authentication_failure_status(output: str) -> DiagnosticStatus:
        lowered = output.casefold()
        if "permission" in lowered or "denied" in lowered:
            return "permission_denied"
        return "login_required"


class _SupervisedRun:
    """One started process, its append-only event file, and its result.

    The supervising thread is what makes ``start`` able to return early: it
    drains both streams, appends every normalized event to the run's file, and
    holds the terminal result until ``wait`` asks for it.
    """

    def __init__(
        self,
        *,
        provider: CliProvider,
        request: ProviderExecutionRequest,
        handle: ProviderRunHandle,
        process: subprocess.Popen[str],
        diagnostic: ProviderDiagnostic,
        secrets: Sequence[str],
        cancel_event: CancelSignal | None,
    ) -> None:
        self.handle = handle
        self._provider = provider
        self._request = request
        self._process = process
        self._diagnostic = diagnostic
        self._secrets = secrets
        self._requested_cancel = threading.Event()
        self._cancel_event = _EitherSignal(self._requested_cancel, cancel_event)
        self._started_monotonic = time.monotonic()
        self._events: list[ProviderEvent] = []
        self._observed_status: RunStatus | None = None
        self._off_contract = False
        self._execution: ProcessExecution | None = None
        self._result: ProviderExecutionResult | None = None
        self._finished = threading.Event()
        # newline="\n" keeps one event exactly one byte-terminated line on every
        # platform, which is what the byte offsets handed to ``watch`` count.
        self._stream = handle.event_path.open("a", encoding="utf-8", newline="\n")
        self._write_lock = threading.Lock()
        self._thread = threading.Thread(target=self._supervise, daemon=True)

    def begin(self) -> None:
        """Record the start event and hand the process to its supervisor."""
        self._append("started", elapsed_seconds=0)
        self._thread.start()

    def wait(self) -> ProviderExecutionResult:
        """Block until the process tree ends and return the terminal result."""
        self._finished.wait()
        assert self._result is not None
        return self._result

    def cancel(self, *, timeout_seconds: float = 10) -> ProviderCancellation:
        """Request cancellation and report only what was actually confirmed."""
        self._requested_cancel.set()
        finished = self._finished.wait(timeout=timeout_seconds)
        execution = self._execution
        process_stopped = (
            finished
            and execution is not None
            and execution.tree_stopped
            and self._process.poll() is not None
        )
        events_closed = self._stream.closed
        return ProviderCancellation(
            run_id=self.handle.run_id,
            cancelled=process_stopped and events_closed,
            process_stopped=process_stopped,
            events_closed=events_closed,
            detail=None if process_stopped else "process_termination_pending",
        )

    def _supervise(self) -> None:
        execution = supervise_process(
            self._process,
            timeout_seconds=self._request.timeout_seconds,
            cancel_event=self._cancel_event,
            on_line=self._consume,
        )
        self._execution = execution
        if execution.state == "cancelled":
            status: RunStatus = "cancelled"
            terminal_kind: EventKind = "cancelled"
        elif execution.state == "timed_out":
            status = "timed_out"
            terminal_kind = "timed_out"
        elif self._observed_status == "usage_limited":
            status = "usage_limited"
            terminal_kind = "failed"
        elif self._observed_status == "failed" or execution.returncode != 0:
            status = "failed"
            terminal_kind = "failed"
        elif self._off_contract:
            status = "off_contract"
            terminal_kind = "failed"
        else:
            status = "success"
            terminal_kind = "completed"

        if not self._events or self._events[-1].kind != terminal_kind:
            self._append(terminal_kind, detail=execution.detail)
        with self._write_lock:
            self._stream.close()
        self._result = ProviderExecutionResult(
            provider=self._provider.name,
            status=status,
            diagnostic=self._diagnostic,
            events=tuple(self._events),
            returncode=execution.returncode,
            detail=execution.detail,
            run_id=self.handle.run_id,
        )
        self._finished.set()

    def _consume(self, _source: LineSource, line: str) -> None:
        if not line:
            return
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            self._off_contract = True
            return
        if not isinstance(decoded, dict):
            self._off_contract = True
            return
        normalized = self._provider.normalize_line(decoded)
        if not normalized:
            self._off_contract = True
            return
        for value in normalized:
            self._append(value.kind, raw_id=value.raw_id, detail=value.detail)
            if value.status is not None:
                self._observed_status = value.status

    def _append(
        self,
        kind: EventKind,
        *,
        raw_id: str | None = None,
        detail: str | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        event = build_event(
            kind,
            provider=self._provider.name,
            request=self._request,
            started_at=self.handle.started_at,
            elapsed_seconds=(
                round(time.monotonic() - self._started_monotonic, 3)
                if elapsed_seconds is None
                else elapsed_seconds
            ),
            raw_id=raw_id,
            detail=redact_sensitive_text(detail, secrets=self._secrets) if detail else None,
        )
        self._events.append(event)
        with self._write_lock:
            if self._stream.closed:
                return
            self._stream.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            self._stream.flush()


class _EitherSignal:
    """Cancellation requested through the handle or by the original caller."""

    def __init__(self, first: CancelSignal, second: CancelSignal | None) -> None:
        self._first = first
        self._second = second

    def is_set(self) -> bool:
        return self._first.is_set() or (self._second is not None and self._second.is_set())


def default_event_root() -> Path:
    """Resolve the runtime-owned directory that holds run event files.

    The override name matches the agent store's, but it is read here instead of
    imported: the store imports the provider package, so depending on it from a
    provider would close an import cycle.
    """
    override = os.environ.get("HEARTBEAT_AGENT_HOME")
    home = Path(override) if override else Path.home() / ".claude" / "heartbeat"
    return home / "agent-runs"


def observe_process(pid: int) -> ProcessObservation:
    """Report whether a PID is still the same live process, and say so honestly.

    ``unknown`` is kept apart from ``gone`` on purpose: a PID the runtime may
    not inspect must never be recovered as a running job, and it must not be
    cleaned up as a finished one either.
    """
    try:
        process = psutil.Process(pid)
        identity = f"{process.create_time():.6f}"
        running = process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return ProcessObservation("gone", None)
    except psutil.Error:
        return ProcessObservation("unknown", None)
    return ProcessObservation("running" if running else "gone", identity)


def process_identity(pid: int) -> str | None:
    """Return a value that tells this process apart from a later PID reuse."""
    return observe_process(pid).identity


def build_event(
    kind: EventKind,
    *,
    provider: str,
    request: ProviderExecutionRequest,
    started_at: str,
    elapsed_seconds: float = 0,
    raw_id: str | None = None,
    detail: str | None = None,
) -> ProviderEvent:
    """Build one event carrying the run's project, role, and target."""
    return ProviderEvent(
        kind=kind,
        provider=provider,
        project_id=request.project_id,
        role=request.role,
        target_id=request.target_id,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        raw_id=raw_id,
        detail=detail,
    )


def utc_now() -> str:
    """Return the timestamp shape used by runtime events."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sensitive_values(environment: Mapping[str, str], prompt: str | None = None) -> tuple[str, ...]:
    """Collect values that must be removed from diagnostic event text.

    Only values behind secret-looking keys are inspected.  This identifies a
    value to redact without serializing, logging, or returning that value.
    """
    values = [value for key, value in environment.items() if _SENSITIVE_ENV_KEY.search(key) and value]
    if prompt:
        values.append(prompt)
    return tuple(sorted(set(values), key=len, reverse=True))


def redact_sensitive_text(value: str, *, secrets: Sequence[str] = ()) -> str:
    """Remove known secret values and common credential shapes from text."""
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _ASSIGNMENT_SECRET.sub(r"\1\2[REDACTED]", redacted)
    redacted = _BEARER_SECRET.sub("Bearer [REDACTED]", redacted)
    return redacted[:500]


def is_usage_limited(value: str) -> bool:
    """Recognize CLI wording that should lead to a retryable usage result."""
    lowered = value.casefold()
    return any(marker in lowered for marker in ("rate limit", "usage limit", "quota exceeded", "too many requests"))


def extract_version(value: str) -> tuple[int, ...] | None:
    """Read the first semantic-looking version without exposing command output."""
    match = re.search(r"(?<!\d)(\d+(?:\.\d+){0,2})", value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_at_least(actual: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    """Compare version components after padding the shorter side with zeros."""
    width = max(len(actual), len(minimum))
    return actual + (0,) * (width - len(actual)) >= minimum + (0,) * (width - len(minimum))


def run_probe(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: float = 10,
) -> subprocess.CompletedProcess[str] | ProbeFailure:
    """Run a short probe without a shell and classify launch failures safely."""
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environment),
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return ProbeFailure("executable_missing")
    except PermissionError:
        return ProbeFailure("permission_denied")
    except (OSError, subprocess.TimeoutExpired):
        return ProbeFailure("unavailable")


def execute_process(
    command: Sequence[str],
    *,
    prompt: str,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float | None,
    cancel_event: CancelSignal | None,
    on_line: Callable[[LineSource, str], None],
) -> ProcessExecution:
    """Run one CLI and drain both streams until its complete process tree ends.

    Lines are given to the caller as they arrive.  They are never retained in
    the result, which prevents prompt echoes and credentials from becoming
    routine runtime state.
    """
    process = spawn_process(command, cwd=cwd, environment=environment)
    if isinstance(process, ProcessExecution):
        return process
    deliver_prompt(process, prompt)
    return supervise_process(
        process,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
        on_line=on_line,
    )


def spawn_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[str] | ProcessExecution:
    """Create one CLI process in its own group, or classify why it could not."""
    options: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(cwd),
        "env": dict(environment),
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True

    try:
        return subprocess.Popen(list(command), **options)  # type: ignore[arg-type]
    except FileNotFoundError:
        return ProcessExecution(returncode=None, state="start_failed", detail="executable_missing")
    except PermissionError:
        return ProcessExecution(returncode=None, state="start_failed", detail="permission_denied")
    except OSError:
        return ProcessExecution(returncode=None, state="start_failed", detail="unavailable")


def deliver_prompt(process: subprocess.Popen[str], prompt: str) -> bool:
    """Write the prompt to stdin and close it, reporting whether it arrived."""
    assert process.stdin is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except (OSError, ValueError):
        return False
    return True


def supervise_process(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float | None,
    cancel_event: CancelSignal | None,
    on_line: Callable[[LineSource, str], None],
) -> ProcessExecution:
    """Drain both streams of a started process until its whole tree ends."""
    lines: queue.Queue[tuple[LineSource, str | None]] = queue.Queue()

    def drain(source: LineSource, stream: object) -> None:
        for line in stream:  # type: ignore[union-attr]
            lines.put((source, line.rstrip("\r\n")))
        lines.put((source, None))

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    state: Literal["exited", "cancelled", "timed_out"] = "exited"
    tree_stopped = True
    closed_streams = 0
    while closed_streams < 2 or process.poll() is None:
        if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
            state = "cancelled"
            tree_stopped = terminate_process_tree(process)
        elif deadline is not None and time.monotonic() >= deadline and process.poll() is None:
            state = "timed_out"
            tree_stopped = terminate_process_tree(process)
        try:
            source, line = lines.get(timeout=0.05)
        except queue.Empty:
            continue
        if line is None:
            closed_streams += 1
        else:
            on_line(source, line)

    for reader in readers:
        reader.join(timeout=1)
    return ProcessExecution(returncode=process.wait(), state=state, tree_stopped=tree_stopped)


def terminate_process_tree(process: subprocess.Popen[str], *, grace_seconds: float = 1) -> bool:
    """Terminate a separated process group and any descendants still alive.

    Returns whether the root and every descendant was confirmed gone, so a
    caller never reports a cancellation the operating system did not finish.
    """
    if process.poll() is not None:
        return True
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
    except psutil.Error:
        descendants = []
    for child in descendants:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        for child in descendants:
            try:
                if child.is_running():
                    child.kill()
            except psutil.Error:
                pass

    deadline = time.monotonic() + grace_seconds
    while True:
        if process.poll() is not None and not any(_still_running(child) for child in descendants):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _still_running(child: psutil.Process) -> bool:
    """Report whether a descendant is alive and not already a reaped zombie."""
    try:
        return child.is_running() and child.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
