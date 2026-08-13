"""Claude CLI provider using stream JSON instead of the Agent SDK."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Mapping

from heartbeat.providers.process import (
    CliProvider, NormalizedLine, ProviderDiagnostic, ProviderExecutionRequest, ProviderModel,
    ProviderModelCatalog, is_usage_limited,
)


CLAUDE_MODEL_CATALOG = ProviderModelCatalog(
    "unverified",
    (
        ProviderModel("best", "Best · 사용 가능한 최고 성능"),
        ProviderModel("fable", "Fable · 장기 자율 작업"),
        ProviderModel("opus", "Opus · 복잡한 판단"),
        ProviderModel("sonnet", "Sonnet · 균형"),
        ProviderModel("haiku", "Haiku · 빠르고 경제적"),
        ProviderModel("opusplan", "Opusplan · 계획 Opus, 실행 Sonnet"),
    ),
)


class ClaudeProvider(CliProvider):
    """Run the installed ``claude`` CLI through the shared provider contract."""

    name = "claude"
    executable = "claude"
    billing_environment_keys = ("ANTHROPIC_API_KEY",)

    def command(self, request: ProviderExecutionRequest) -> list[str]:
        # A headless run has nobody to answer permission prompts, so without an
        # explicit policy every edit and command is silently denied — the
        # session starts, does nothing, and reports empty-handed. That only
        # went unnoticed on machines whose personal settings auto-approve.
        # Full permission matches the trust the user already grants the
        # parallel worker model (codex runs sandboxed full-auto); a managed
        # per-project allowlist is the planned refinement.
        command = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if request.model:
            command.extend(["--model", request.model])
        return command

    def authentication_command(self) -> list[str]:
        return [self.executable, "auth", "status", "--json"]

    def diagnose(self, *, environment: Mapping[str, str] | None = None) -> ProviderDiagnostic:
        return replace(super().diagnose(environment=environment), model_catalog=CLAUDE_MODEL_CATALOG)

    def normalize_line(self, value: dict[object, object]) -> tuple[NormalizedLine, ...]:
        event_type = value.get("type")
        if not isinstance(event_type, str):
            return ()
        raw_id = _first_string(value, ("session_id", "sessionId", "uuid", "id"))
        if event_type == "result":
            subtype = value.get("subtype")
            failed = value.get("is_error") is True or (isinstance(subtype, str) and subtype != "success")
            if not failed:
                return (NormalizedLine("completed", raw_id=raw_id),)
            message = _error_message(value, ("result", "error", "message")) or "provider result failed"
            status = "usage_limited" if is_usage_limited(message) else "failed"
            return (NormalizedLine("failed", raw_id=raw_id, detail=message, status=status),)
        if event_type in {"error", "fatal_error"}:
            message = _error_message(value, ("error", "message")) or "provider error"
            status = "usage_limited" if is_usage_limited(message) else "failed"
            return (NormalizedLine("failed", raw_id=raw_id, detail=message, status=status),)
        if event_type == "tool_use" or _contains_tool_use(value.get("message")):
            return (NormalizedLine("tool", raw_id=raw_id),)
        if event_type in {"assistant", "user", "system", "stream_event"}:
            return (NormalizedLine("progress", raw_id=raw_id),)
        return ()


def _contains_tool_use(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    content = value.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "tool_use" for item in content)


def _first_string(value: dict[object, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _error_message(value: dict[object, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if isinstance(candidate, dict):
            nested = _error_message(candidate, ("message", "error", "detail"))
            if nested:
                return nested
    return None
