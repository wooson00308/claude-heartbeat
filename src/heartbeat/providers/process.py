"""Provider-neutral process supervision and result types.

The runtime deliberately keeps this module free of provider SDK imports.  A
provider receives a prompt on stdin, emits normalized events through a callback,
and is always launched in a process group that can be cleaned up as one unit.
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
    directly to the child process's standard input.
    """

    project_id: str
    role: str
    target_id: str
    project_root: Path
    prompt: str
    model: str | None = None
    timeout_seconds: float | None = None
    billing_route_acknowledged: bool = False


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


@dataclass(frozen=True)
class ProviderExecutionResult:
    """Normalized terminal state and events for one provider invocation."""

    provider: str
    status: RunStatus
    diagnostic: ProviderDiagnostic | None
    events: tuple[ProviderEvent, ...]
    returncode: int | None
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


class AgentProvider(Protocol):
    """The common interface implemented by every CLI-backed provider."""

    name: str

    def diagnose(self, *, environment: Mapping[str, str] | None = None) -> ProviderDiagnostic:
        """Report whether the provider can be started safely."""

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

    def run(
        self,
        request: ProviderExecutionRequest,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProviderExecutionResult:
        """Run a CLI with a stdin-only prompt and normalized event stream."""
        child_environment = self._environment(environment)
        diagnostic = self._diagnose(
            child_environment,
            billing_route_acknowledged=request.billing_route_acknowledged,
        )
        if diagnostic.status != "ready":
            return ProviderExecutionResult(
                provider=self.name,
                status="failed",
                diagnostic=diagnostic,
                events=(),
                returncode=None,
                detail=diagnostic.status,
            )

        started_at = utc_now()
        started_monotonic = time.monotonic()
        secrets = sensitive_values(child_environment, request.prompt)
        events: list[ProviderEvent] = [
            ProviderEvent(
                kind="started",
                provider=self.name,
                project_id=request.project_id,
                role=request.role,
                target_id=request.target_id,
                started_at=started_at,
                elapsed_seconds=0,
            )
        ]
        observed_status: RunStatus | None = None
        off_contract = False

        def event(kind: EventKind, *, raw_id: str | None = None, detail: str | None = None) -> None:
            events.append(
                ProviderEvent(
                    kind=kind,
                    provider=self.name,
                    project_id=request.project_id,
                    role=request.role,
                    target_id=request.target_id,
                    started_at=started_at,
                    elapsed_seconds=round(time.monotonic() - started_monotonic, 3),
                    raw_id=raw_id,
                    detail=redact_sensitive_text(detail, secrets=secrets) if detail else None,
                )
            )

        def consume(_: LineSource, line: str) -> None:
            nonlocal observed_status, off_contract
            if not line:
                return
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                off_contract = True
                return
            if not isinstance(decoded, dict):
                off_contract = True
                return
            normalized = self.normalize_line(decoded)
            if not normalized:
                off_contract = True
                return
            for value in normalized:
                event(value.kind, raw_id=value.raw_id, detail=value.detail)
                if value.status is not None:
                    observed_status = value.status

        execution = execute_process(
            self.command(request),
            prompt=request.prompt,
            cwd=request.project_root,
            environment=child_environment,
            timeout_seconds=request.timeout_seconds,
            cancel_event=cancel_event,
            on_line=consume,
        )
        if execution.state == "cancelled":
            status: RunStatus = "cancelled"
            terminal_kind: EventKind = "cancelled"
        elif execution.state == "timed_out":
            status = "timed_out"
            terminal_kind = "timed_out"
        elif execution.state == "start_failed":
            status = "failed"
            terminal_kind = "failed"
        elif observed_status == "usage_limited":
            status = "usage_limited"
            terminal_kind = "failed"
        elif observed_status == "failed" or execution.returncode != 0:
            status = "failed"
            terminal_kind = "failed"
        elif off_contract:
            status = "off_contract"
            terminal_kind = "failed"
        else:
            status = "success"
            terminal_kind = "completed"

        if not events or events[-1].kind != terminal_kind:
            event(terminal_kind, detail=execution.detail)
        return ProviderExecutionResult(
            provider=self.name,
            status=status,
            diagnostic=diagnostic,
            events=tuple(events),
            returncode=execution.returncode,
            detail=execution.detail,
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
    cancel_event: threading.Event | None,
    on_line: Callable[[LineSource, str], None],
) -> ProcessExecution:
    """Run one CLI and drain both streams until its complete process tree ends.

    Lines are given to the caller as they arrive.  They are never retained in
    the result, which prevents prompt echoes and credentials from becoming
    routine runtime state.
    """
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
        process = subprocess.Popen(list(command), **options)  # type: ignore[arg-type]
    except FileNotFoundError:
        return ProcessExecution(returncode=None, state="start_failed", detail="executable_missing")
    except PermissionError:
        return ProcessExecution(returncode=None, state="start_failed", detail="permission_denied")
    except OSError:
        return ProcessExecution(returncode=None, state="start_failed", detail="unavailable")

    assert process.stdin is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass

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
    closed_streams = 0
    while closed_streams < 2 or process.poll() is None:
        if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
            state = "cancelled"
            terminate_process_tree(process)
        elif deadline is not None and time.monotonic() >= deadline and process.poll() is None:
            state = "timed_out"
            terminate_process_tree(process)
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
    return ProcessExecution(returncode=process.wait(), state=state)


def terminate_process_tree(process: subprocess.Popen[str], *, grace_seconds: float = 1) -> None:
    """Terminate a separated process group and any descendants still alive."""
    if process.poll() is not None:
        return
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
