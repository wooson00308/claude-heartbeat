"""Claude CLI provider using stream JSON instead of the Agent SDK."""

from __future__ import annotations

from collections.abc import Sequence

from heartbeat.providers.process import CliProvider, NormalizedLine, ProviderExecutionRequest, is_usage_limited


class ClaudeProvider(CliProvider):
    """Run the installed ``claude`` CLI through the shared provider contract."""

    name = "claude"
    executable = "claude"
    billing_environment_keys = ("ANTHROPIC_API_KEY",)

    def command(self, request: ProviderExecutionRequest) -> list[str]:
        command = [self.executable, "-p", "--output-format", "stream-json", "--verbose"]
        if request.model:
            command.extend(["--model", request.model])
        return command

    def authentication_command(self) -> list[str]:
        return [self.executable, "auth", "status", "--json"]

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
            message = _first_string(value, ("result", "error", "message")) or "provider result failed"
            status = "usage_limited" if is_usage_limited(message) else "failed"
            return (NormalizedLine("failed", raw_id=raw_id, detail="provider result failed", status=status),)
        if event_type in {"error", "fatal_error"}:
            message = _first_string(value, ("error", "message")) or "provider error"
            status = "usage_limited" if is_usage_limited(message) else "failed"
            return (NormalizedLine("failed", raw_id=raw_id, detail="provider error", status=status),)
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
