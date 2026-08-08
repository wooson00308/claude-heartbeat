"""Regression tests for CLI-backed agent providers without provider SDKs."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from heartbeat.providers import ClaudeProvider, CodexProvider
from heartbeat.providers import process as process_module
from heartbeat.providers.process import (
    CliProvider,
    NormalizedLine,
    ProviderDiagnostic,
    ProviderExecutionRequest,
    execute_process,
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


def request(tmp_path: Path, *, prompt: str = "write the private plan") -> ProviderExecutionRequest:
    return ProviderExecutionRequest(
        project_id="project-1",
        role="developer",
        target_id="TASK-1",
        project_root=tmp_path,
        prompt=prompt,
        model=None,
        timeout_seconds=5,
    )


def complete(command: list[str], returncode: int = 0, stdout: str = "cli 1.2.3", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_claude_command_is_stream_json_and_keeps_prompt_off_command_line(tmp_path: Path) -> None:
    value = request(tmp_path, prompt="do not put this in argv")

    command = ClaudeProvider(executable="fake-claude").command(value)

    assert command == ["fake-claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert value.prompt not in command
    assert ClaudeProvider(executable="fake-claude").command(
        ProviderExecutionRequest(**{**value.__dict__, "model": "sonnet"})
    )[-2:] == ["--model", "sonnet"]


def test_codex_command_is_json_exec_and_keeps_prompt_off_command_line(tmp_path: Path) -> None:
    value = request(tmp_path, prompt="do not put this in argv")

    command = CodexProvider(executable="fake-codex").command(value)

    assert command == [
        "fake-codex", "exec", "--json", "--sandbox", "workspace-write", "-C", str(tmp_path), "-",
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
    monkeypatch.setattr(provider, "_executable_exists", lambda: True)
    iterator = iter(probes)
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(iterator))

    result = provider.diagnose(environment={})

    assert result.status == expected


def test_missing_executable_is_not_confused_with_other_diagnostics() -> None:
    result = CodexProvider(executable="definitely-not-an-agent-cli").diagnose(environment={})

    assert result.status == "executable_missing"


def test_claude_api_billing_route_requires_acknowledgement_before_start(monkeypatch, tmp_path: Path) -> None:
    provider = ClaudeProvider(executable="fake-claude")
    monkeypatch.setattr(provider, "_executable_exists", lambda: True)
    probes = iter([complete(["claude", "--version"])])
    monkeypatch.setattr(process_module, "run_probe", lambda *args, **kwargs: next(probes))
    monkeypatch.setattr(process_module, "execute_process", lambda *args, **kwargs: pytest.fail("must not start"))

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


def test_provider_jsonl_normalizers_cover_tool_and_failure_events() -> None:
    claude_events = ClaudeProvider().normalize_line(
        {"type": "assistant", "session_id": "session-1", "message": {"content": [{"type": "tool_use"}]}}
    )
    codex_events = CodexProvider().normalize_line(
        {"type": "error", "thread_id": "thread-1", "message": "rate limit"}
    )

    assert claude_events[0].kind == "tool"
    assert codex_events[0].status == "usage_limited"
