"""Codex CLI provider using ``codex exec --json`` without an SDK dependency."""

from __future__ import annotations

from collections.abc import Sequence

from heartbeat.providers.process import CliProvider, NormalizedLine, ProviderExecutionRequest, is_usage_limited


class CodexProvider(CliProvider):
    """Run the installed ``codex`` CLI through the shared provider contract."""

    name = "codex"
    executable = "codex"

    def command(self, request: ProviderExecutionRequest) -> list[str]:
        command = [
            self.executable,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(request.project_root),
        ]
        if request.model:
            command.extend(["--model", request.model])
        command.append("-")
        return command

    def authentication_command(self) -> list[str]:
        return [self.executable, "login", "status"]

    def normalize_line(self, value: dict[object, object]) -> tuple[NormalizedLine, ...]:
        event_type = value.get("type")
        if not isinstance(event_type, str):
            return ()
        raw_id = _first_string(value, ("thread_id", "threadId", "turn_id", "turnId", "item_id", "itemId", "id"))
        if event_type in {"thread.started", "turn.started"}:
            return (NormalizedLine("progress", raw_id=raw_id),)
        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = value.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            kind = "tool" if item_type in {"command_execution", "mcp_tool_call", "function_call"} else "progress"
            return (NormalizedLine(kind, raw_id=raw_id),)
        if event_type == "turn.completed":
            return (NormalizedLine("completed", raw_id=raw_id),)
        if event_type in {"turn.failed", "error"}:
            message = _first_string(value, ("message", "error")) or "provider error"
            status = "usage_limited" if is_usage_limited(message) else "failed"
            return (NormalizedLine("failed", raw_id=raw_id, detail="provider error", status=status),)
        return ()


def _first_string(value: dict[object, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None
