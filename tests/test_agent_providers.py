"""Regression tests for CLI-backed agent providers without provider SDKs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import psutil
import pytest

from heartbeat.providers import ClaudeProvider, CodexProvider
from heartbeat.providers import process as process_module
from heartbeat.providers.process import (
    CliProvider,
    NormalizedLine,
    ProviderDiagnostic,
    ProviderEventBatch,
    ProviderExecutionRequest,
    ProviderModelCatalog,
    ProviderRunHandle,
    ProviderStartFailure,
    execute_process,
    redact_sensitive_text,
)


class FixtureProvider(CliProvider):
    """A provider that uses a Python fixture while exercising the shared runner."""

    name = "fixture"
    executable = sys.executable

    def __init__(self, script: Path) -> None:
        super().__init__()
        self.script = script

    def command(self, request: ProviderExecutionRequest) -> list[str]:
        return [self.executable, str(self.script)]

    def authentication_command(self) -> list[str]:
        return [self.executable, str(self.script), "auth"]

    def normalize_line(self, value: dict[object, object]) -> tuple[NormalizedLine, ...]:
        event_type = value.get("type")
        message = value.get("message")
        detail = message if isinstance(message, str) else None
        if event_type == "progress":
            return (NormalizedLine("progress", raw_id="fixture-event", detail=detail),)
        if event_type == "done":
            return (NormalizedLine("completed", raw_id="fixture-event"),)
        if event_type == "failure":
            status = "usage_limited" if detail and "rate limit" in detail else "failed"
            return (NormalizedLine("failed", raw_id="fixture-event", detail=detail, status=status),)
        return ()

    def _diagnose(self, environment, *, billing_route_acknowledged):  # type: ignore[no-untyped-def]
        return ProviderDiagnostic(self.name, "ready", self.executable, version="1.0")


@pytest.fixture
def fixture_script(tmp_path: Path) -> Path:
    script = tmp_path / "fixture_cli.py"
    script.write_text(
        """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = os.environ.get("PROVIDER_TEST_MODE", "success")
if mode == "auth":
    raise SystemExit(0)
prompt = sys.stdin.read()
args_path = os.environ.get("PROVIDER_TEST_ARGS")
prompt_path = os.environ.get("PROVIDER_TEST_PROMPT")
if args_path:
    Path(args_path).write_text(json.dumps(sys.argv), encoding="utf-8")
if prompt_path:
    Path(prompt_path).write_text(prompt, encoding="utf-8")
if mode == "broken":
    print("not-json", flush=True)
    print(json.dumps({"type": "done"}), flush=True)
elif mode == "stderr-warning":
    print("warning: optional integration unavailable", file=sys.stderr, flush=True)
    print(json.dumps({"type": "done"}), flush=True)
elif mode == "stderr-failure":
    print("selected model failed; token=private-value", file=sys.stderr, flush=True)
    raise SystemExit(7)
elif mode == "usage":
    print(json.dumps({"type": "failure", "message": "rate limit for test"}), flush=True)
elif mode == "slow":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["PROVIDER_TEST_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
    print(json.dumps({"type": "progress", "message": "started"}), flush=True)
    while True:
        time.sleep(0.05)
else:
    print(json.dumps({"type": "progress", "message": prompt + " " + os.environ["TEST_API_KEY"]}), flush=True)
    print(json.dumps({"type": "done"}), flush=True)
""",
        encoding="utf-8",
    )
    return script


def request(
    tmp_path: Path,
    *,
    prompt: str = "write the private plan",
    timeout_seconds: float | None = 5,
) -> ProviderExecutionRequest:
    return ProviderExecutionRequest(
        project_id="project-1",
        role="developer",
        target_id="TASK-1",
        project_root=tmp_path,
        prompt=prompt,
        model=None,
        timeout_seconds=timeout_seconds,
        event_root=tmp_path / "runs",
    )


def complete(command: list[str], returncode: int = 0, stdout: str = "cli 1.2.3", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def slow_environment(tmp_path: Path) -> dict[str, str]:
    return {"PROVIDER_TEST_MODE": "slow", "PROVIDER_TEST_CHILD_PID": str(tmp_path / "child.pid")}


def wait_for_file(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def is_gone(pid: int) -> bool:
    for _ in range(20):
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def test_claude_command_is_stream_json_and_keeps_prompt_off_command_line(tmp_path: Path) -> None:
    value = request(tmp_path, prompt="do not put this in argv")

    command = ClaudeProvider(executable="fake-claude").command(value)

    assert command == ["fake-claude", "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    assert value.prompt not in command
    assert ClaudeProvider(executable="fake-claude").command(
        ProviderExecutionRequest(**{**value.__dict__, "model": "sonnet"})
    )[-2:] == ["--model", "sonnet"]


def test_codex_command_is_json_exec_and_keeps_prompt_off_command_line(tmp_path: Path) -> None:
    value = request(tmp_path, prompt="do not put this in argv")

    command = CodexProvider(executable="fake-codex").command(value)

    assert command == [
        "fake-codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(tmp_path), "-",
    ]
    assert value.prompt not in command
    assert CodexProvider(executable="fake-codex").command(
        ProviderExecutionRequest(**{**value.__dict__, "model": "gpt-5"})
    )[-3:] == ["--model", "gpt-5", "-"]


@pytest.mark.parametrize(
    ("probes", "expected"),
    [
        ([complete(["claude", "--version"]), complete(["claude", "auth"], 1, "not logged in")], "login_required"),
        ([complete(["claude", "--version"]), complete(["claude", "auth"], 1, "permission denied")], "permission_denied"),
        ([complete(["claude", "--version"], 0, "unknown")], "unsupported_version"),
    ],
)
def test_diagnostics_distinguish_login_permission_and_version(monkeypatch, probes, expected) -> None:  # type: ignore[no-untyped-def]
    provider = ClaudeProvider(executable="fake-claude")
    monkeypatch.setattr(provider, "_executable_exists", lambda *_: True)
    iterator = iter(probes)
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(iterator))

    result = provider.diagnose(environment={})

    assert result.status == expected


def test_missing_executable_is_not_confused_with_other_diagnostics() -> None:
    result = CodexProvider(executable="definitely-not-an-agent-cli").diagnose(environment={})

    assert result.status == "executable_missing"


def test_diagnostics_search_well_known_install_dirs_beyond_the_service_path(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    tool_dir = home / ".local" / "bin"
    tool_dir.mkdir(parents=True)
    tool = tool_dir / ("agent-cli.exe" if os.name == "nt" else "agent-cli")
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setattr(process_module.Path, "home", lambda: home)

    provider = ClaudeProvider(executable="agent-cli")
    environment = provider._environment({"PATH": str(tmp_path / "service-only")})

    assert str(tool_dir) in environment["PATH"].split(os.pathsep)
    assert provider._executable_exists(environment)


def test_codex_diagnosis_exposes_only_visible_account_models_in_priority_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = CodexProvider(executable="fake-codex")
    monkeypatch.setattr(provider, "_executable_exists", lambda *_: True)
    models = json.dumps({"models": [
        {"slug": "hidden", "display_name": "Hidden", "visibility": "hide", "priority": 0},
        {"slug": "gpt-terra", "display_name": "Terra", "visibility": "list", "priority": 2},
        {"slug": "gpt-sol", "display_name": "Sol", "visibility": "list", "priority": 1},
    ]})
    probes = iter([
        complete(["codex", "--version"]),
        complete(["codex", "login", "status"]),
        complete(["codex", "debug", "models"], stdout=models),
    ])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))

    diagnostic = provider.diagnose(environment={})

    assert diagnostic.model_catalog.status == "available"
    assert [(model.id, model.label) for model in diagnostic.model_catalog.models] == [
        ("gpt-sol", "Sol"), ("gpt-terra", "Terra"),
    ]


@pytest.mark.parametrize("output", ["not-json", "{}", '{"models": []}'])
def test_codex_model_catalog_failure_stays_unavailable_without_failing_provider(
    monkeypatch, output: str
) -> None:  # type: ignore[no-untyped-def]
    provider = CodexProvider(executable="fake-codex")
    monkeypatch.setattr(provider, "_executable_exists", lambda *_: True)
    probes = iter([
        complete(["codex", "--version"]),
        complete(["codex", "login", "status"]),
        complete(["codex", "debug", "models"], stdout=output),
    ])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))

    diagnostic = provider.diagnose(environment={})

    assert diagnostic.status == "ready"
    assert diagnostic.model_catalog == ProviderModelCatalog("unavailable")


def test_codex_model_catalog_command_failure_stays_unavailable_without_failing_provider(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    provider = CodexProvider(executable="fake-codex")
    monkeypatch.setattr(provider, "_executable_exists", lambda *_: True)
    probes = iter([
        complete(["codex", "--version"]),
        complete(["codex", "login", "status"]),
        complete(["codex", "debug", "models"], returncode=1, stderr="catalog unavailable"),
    ])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))

    diagnostic = provider.diagnose(environment={})

    assert diagnostic.status == "ready"
    assert diagnostic.model_catalog == ProviderModelCatalog("unavailable")


def test_claude_diagnosis_exposes_stable_aliases_as_unverified(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = ClaudeProvider(executable="fake-claude")
    monkeypatch.setattr(provider, "_executable_exists", lambda *_: True)
    probes = iter([complete(["claude", "--version"]), complete(["claude", "auth", "status"])])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))

    catalog = provider.diagnose(environment={}).model_catalog

    assert catalog.status == "unverified"
    assert [model.id for model in catalog.models] == ["best", "fable", "opus", "sonnet", "haiku", "opusplan"]


def test_nested_provider_errors_keep_safe_actionable_model_detail() -> None:
    codex = CodexProvider().normalize_line({
        "type": "error",
        "error": {"message": "The 'gpt-5.6' model is not supported; token=private-value"},
    })[0]
    claude = ClaudeProvider().normalize_line({
        "type": "error", "error": {"message": "selected model is unavailable"},
    })[0]

    assert redact_sensitive_text(codex.detail or "") == (
        "The 'gpt-5.6' model is not supported; token=[REDACTED]"
    )
    assert claude.detail == "selected model is unavailable"


def test_claude_api_billing_route_requires_acknowledgement_before_start(monkeypatch, tmp_path: Path) -> None:
    provider = ClaudeProvider(executable="fake-claude")
    monkeypatch.setattr(provider, "_executable_exists", lambda *_: True)
    probes = iter([complete(["claude", "--version"])])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))
    monkeypatch.setattr(process_module, "spawn_process", lambda *args, **kwargs: pytest.fail("must not start"))

    result = provider.run(request(tmp_path), environment={"ANTHROPIC_API_KEY": "very-secret-value"})

    assert result.diagnostic is not None
    assert result.diagnostic.status == "billing_route_acknowledgement_required"
    assert "very-secret-value" not in (result.diagnostic.detail or "")


def test_run_streams_prompt_through_stdin_and_redacts_prompt_and_environment_secret(
    fixture_script: Path, tmp_path: Path
) -> None:
    args_path = tmp_path / "argv.json"
    prompt_path = tmp_path / "prompt.txt"
    prompt = "prompt-secret-123"

    result = FixtureProvider(fixture_script).run(
        request(tmp_path, prompt=prompt),
        environment={
            "TEST_API_KEY": "api-secret-456",
            "PROVIDER_TEST_ARGS": str(args_path),
            "PROVIDER_TEST_PROMPT": str(prompt_path),
        },
    )

    assert result.status == "success"
    assert prompt_path.read_text(encoding="utf-8") == prompt
    assert prompt not in args_path.read_text(encoding="utf-8")
    event_text = " ".join(event.detail or "" for event in result.events)
    assert prompt not in event_text
    assert "api-secret-456" not in event_text
    assert [(event.project_id, event.role, event.target_id) for event in result.events] == [
        ("project-1", "developer", "TASK-1")
    ] * len(result.events)


def test_broken_json_line_does_not_stop_the_process_and_is_reported_off_contract(
    fixture_script: Path, tmp_path: Path
) -> None:
    result = FixtureProvider(fixture_script).run(
        request(tmp_path),
        environment={"TEST_API_KEY": "not-used", "PROVIDER_TEST_MODE": "broken"},
    )

    assert result.status == "off_contract"
    assert [event.kind for event in result.events][-2:] == ["completed", "failed"]


def test_non_json_stderr_warning_does_not_flip_a_completed_run_to_failure(
    fixture_script: Path, tmp_path: Path
) -> None:
    result = FixtureProvider(fixture_script).run(
        request(tmp_path),
        environment={"TEST_API_KEY": "not-used", "PROVIDER_TEST_MODE": "stderr-warning"},
    )

    assert result.status == "success"
    assert result.events[-1].kind == "completed"


def test_nonzero_exit_keeps_redacted_stderr_reason(
    fixture_script: Path, tmp_path: Path
) -> None:
    result = FixtureProvider(fixture_script).run(
        request(tmp_path),
        environment={"TEST_API_KEY": "not-used", "PROVIDER_TEST_MODE": "stderr-failure"},
    )

    assert result.status == "failed"
    assert result.returncode == 7
    assert result.events[-1].detail == "selected model failed; token=[REDACTED]"


def test_usage_limit_has_a_distinct_terminal_result(fixture_script: Path, tmp_path: Path) -> None:
    result = FixtureProvider(fixture_script).run(
        request(tmp_path),
        environment={"TEST_API_KEY": "not-used", "PROVIDER_TEST_MODE": "usage"},
    )

    assert result.status == "usage_limited"
    assert result.events[-1].kind == "failed"


@pytest.mark.parametrize("cancel", [True, False])
def test_cancellation_and_timeout_stop_the_full_child_process_tree(
    fixture_script: Path, tmp_path: Path, cancel: bool
) -> None:
    child_pid_path = tmp_path / "child.pid"
    cancellation = threading.Event() if cancel else None

    def on_line(_source: str, _line: str) -> None:
        if cancellation is not None:
            cancellation.set()

    result = execute_process(
        [sys.executable, str(fixture_script)],
        prompt="",
        cwd=tmp_path,
        environment={"PROVIDER_TEST_MODE": "slow", "PROVIDER_TEST_CHILD_PID": str(child_pid_path)},
        timeout_seconds=None if cancel else 0.2,
        cancel_event=cancellation,
        on_line=on_line,
    )

    assert result.state == ("cancelled" if cancel else "timed_out")
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(20):
        if not psutil.pid_exists(child_pid):
            break
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_start_returns_a_running_handle_with_pid_start_time_and_event_file(
    fixture_script: Path, tmp_path: Path
) -> None:
    provider = FixtureProvider(fixture_script)

    handle = provider.start(
        request(tmp_path, timeout_seconds=None), environment=slow_environment(tmp_path)
    )

    assert isinstance(handle, ProviderRunHandle)
    try:
        assert psutil.pid_exists(handle.pid)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", handle.started_at)
        assert handle.process_identity is not None
        assert handle.event_path.parent == tmp_path / "runs"
        assert provider.watch(handle).events[0].kind == "started"
    finally:
        provider.cancel(handle)
        provider.wait(handle)


def test_start_failures_separate_the_diagnostic_stage_from_the_spawn_stage(
    monkeypatch, fixture_script: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    claude = ClaudeProvider(executable="fake-claude")
    monkeypatch.setattr(claude, "_executable_exists", lambda *_: True)
    probes = iter([complete(["claude", "--version"])])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))

    class MissingExecutableProvider(FixtureProvider):
        def command(self, value: ProviderExecutionRequest) -> list[str]:
            return ["definitely-not-an-agent-cli"]

    diagnostic_failure = claude.start(
        request(tmp_path), environment={"ANTHROPIC_API_KEY": "very-secret-value"}
    )
    spawn_failure = MissingExecutableProvider(fixture_script).start(request(tmp_path))

    assert isinstance(diagnostic_failure, ProviderStartFailure)
    assert (diagnostic_failure.stage, diagnostic_failure.detail) == (
        "diagnostic",
        "billing_route_acknowledgement_required",
    )
    assert isinstance(spawn_failure, ProviderStartFailure)
    assert (spawn_failure.stage, spawn_failure.detail) == ("spawn", "executable_missing")


def test_watch_resumes_from_the_last_offset_and_withholds_a_partial_line(
    fixture_script: Path, tmp_path: Path
) -> None:
    provider = FixtureProvider(fixture_script)
    handle = provider.start(request(tmp_path), environment={"TEST_API_KEY": "not-used"})
    assert isinstance(handle, ProviderRunHandle)
    result = provider.wait(handle)

    first = provider.watch(handle)
    assert [event.kind for event in first.events] == [event.kind for event in result.events]
    assert first.next_offset == handle.event_path.stat().st_size
    assert provider.watch(handle) == first
    assert provider.watch(handle, offset=first.next_offset) == ProviderEventBatch((), first.next_offset)

    payload = json.dumps(asdict(result.events[-1]))
    middle = len(payload) // 2
    with handle.event_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload[:middle])
    assert provider.watch(handle, offset=first.next_offset) == ProviderEventBatch((), first.next_offset)
    with handle.event_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload[middle:] + "\n")

    resumed = provider.watch(handle, offset=first.next_offset)
    assert [event.kind for event in resumed.events] == [result.events[-1].kind]


def test_cancel_through_the_handle_stops_the_child_process_tree(
    fixture_script: Path, tmp_path: Path
) -> None:
    provider = FixtureProvider(fixture_script)
    child_pid_path = tmp_path / "child.pid"
    handle = provider.start(
        request(tmp_path, timeout_seconds=None), environment=slow_environment(tmp_path)
    )
    assert isinstance(handle, ProviderRunHandle)
    wait_for_file(child_pid_path)

    cancellation = provider.cancel(handle)
    result = provider.wait(handle)

    assert (cancellation.cancelled, cancellation.process_stopped, cancellation.events_closed) == (
        True,
        True,
        True,
    )
    assert result.status == "cancelled"
    assert result.run_id == handle.run_id
    assert is_gone(int(child_pid_path.read_text(encoding="utf-8")))
    assert not psutil.pid_exists(handle.pid) or psutil.Process(handle.pid).status() == psutil.STATUS_ZOMBIE


def test_event_file_never_contains_the_prompt_or_environment_secret(
    fixture_script: Path, tmp_path: Path
) -> None:
    provider = FixtureProvider(fixture_script)
    prompt = "prompt-secret-123"

    handle = provider.start(
        request(tmp_path, prompt=prompt), environment={"TEST_API_KEY": "api-secret-456"}
    )
    assert isinstance(handle, ProviderRunHandle)
    result = provider.wait(handle)

    recorded = handle.event_path.read_text(encoding="utf-8")
    assert result.status == "success"
    assert prompt not in recorded
    assert "api-secret-456" not in recorded
    assert prompt not in repr(handle)


def test_cancelling_a_run_this_provider_does_not_own_is_not_reported_as_success(tmp_path: Path) -> None:
    handle = ProviderRunHandle(
        run_id="not-started-here",
        provider="claude",
        project_id="project-1",
        role="developer",
        target_id="TASK-1",
        pid=1,
        started_at="2026-01-01T00:00:00Z",
        event_path=tmp_path / "missing.jsonl",
    )

    cancellation = ClaudeProvider().cancel(handle)

    assert (cancellation.cancelled, cancellation.detail) == (False, "unknown_run")
    assert ClaudeProvider().watch(handle) == ProviderEventBatch((), 0)


def test_provider_jsonl_normalizers_cover_tool_and_failure_events() -> None:
    claude_events = ClaudeProvider().normalize_line(
        {"type": "assistant", "session_id": "session-1", "message": {"content": [{"type": "tool_use"}]}}
    )
    codex_events = CodexProvider().normalize_line(
        {"type": "error", "thread_id": "thread-1", "message": "rate limit"}
    )

    assert claude_events[0].kind == "tool"
    assert codex_events[0].status == "usage_limited"
