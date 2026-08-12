"""Codex CLI provider using ``codex exec --json`` without an SDK dependency."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Mapping

from heartbeat.providers import process as process_module
from heartbeat.providers.process import (
    CliProvider, NormalizedLine, ProbeFailure, ProviderDiagnostic, ProviderExecutionRequest,
    ProviderModel, ProviderModelCatalog, is_usage_limited,
)


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

    def diagnose(self, *, environment: Mapping[str, str] | None = None) -> ProviderDiagnostic:
        diagnostic = super().diagnose(environment=environment)
        if diagnostic.status != "ready":
            return diagnostic
        probe = process_module.run_probe(
            [self.executable, "debug", "models"], environment=self._environment(environment),
        )
        if isinstance(probe, ProbeFailure) or probe.returncode != 0:
            return diagnostic
        return replace(diagnostic, model_catalog=_model_catalog(probe.stdout))

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
            message = _error_message(value) or "provider error"
            status = "usage_limited" if is_usage_limited(message) else "failed"
            return (NormalizedLine("failed", raw_id=raw_id, detail=message, status=status),)
        return ()


def _first_string(value: dict[object, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _error_message(value: dict[object, object]) -> str | None:
    for key in ("message", "error", "detail"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if isinstance(candidate, dict):
            nested = _error_message(candidate)
            if nested:
                return nested
    return None


def _model_catalog(raw: str) -> ProviderModelCatalog:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return ProviderModelCatalog("unavailable")
    values = decoded.get("models") if isinstance(decoded, dict) else None
    if not isinstance(values, list):
        return ProviderModelCatalog("unavailable")
    models: list[tuple[int, ProviderModel]] = []
    for value in values:
        if not isinstance(value, dict) or value.get("visibility") != "list":
            continue
        slug = value.get("slug")
        label = value.get("display_name")
        priority = value.get("priority")
        if not isinstance(slug, str) or not slug.strip():
            continue
        models.append((
            priority if isinstance(priority, int) else 10_000,
            ProviderModel(slug, label if isinstance(label, str) and label.strip() else slug),
        ))
    if not models:
        return ProviderModelCatalog("unavailable")
    models.sort(key=lambda item: (item[0], item[1].id))
    return ProviderModelCatalog("available", tuple(model for _, model in models))
