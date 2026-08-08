"""JSON-only command handlers for ``heartbeat agent``."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, TextIO

from heartbeat.agent_contract import (
    AgentContractError,
    contract_description,
    envelope,
    require_api_version,
    validate_configuration,
)
from heartbeat.agent_store import AgentStore


def _read_json(stream: TextIO) -> dict[str, Any]:
    try:
        value = json.load(stream)
    except json.JSONDecodeError as error:
        raise AgentContractError("invalid_json", "stdin must contain one JSON object") from error
    if not isinstance(value, dict):
        raise AgentContractError("invalid_request", "stdin must contain a JSON object")
    return value


def _write_json(value: dict[str, Any], stream: TextIO) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _project_id(request: dict[str, Any], request_id: str) -> str:
    project_id = request.get("projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        raise AgentContractError("invalid_request", "projectId must be a non-empty string", request_id=request_id)
    return project_id


def run_agent_command(
    command: str,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    store: AgentStore | None = None,
) -> int:
    """Run one implemented command and keep stdout to one JSON response."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    request_id = str(uuid.uuid4())
    try:
        if command == "contract.read":
            _write_json(envelope(command, request_id, outcome="success", data=contract_description()), output_stream)
            return 0

        request = _read_json(input_stream)
        request_id = require_api_version(request)
        runtime_store = store or AgentStore()
        if command in {"config.validate", "config.write"}:
            configuration = validate_configuration(request.get("configuration"), request_id=request_id)
            if command == "config.write":
                runtime_store.save_configuration(configuration)
            _write_json(
                envelope(command, request_id, outcome="success", data={"configuration": configuration.to_dict()}),
                output_stream,
            )
            return 0
        if command == "config.read":
            configuration = runtime_store.get_configuration(_project_id(request, request_id))
            _write_json(envelope(command, request_id, outcome="success", data={"configuration": configuration}), output_stream)
            return 0
        if command == "state.read":
            state = runtime_store.get_state(_project_id(request, request_id))
            _write_json(envelope(command, request_id, outcome="success", data=state), output_stream)
            return 0
        raise AgentContractError("unsupported_command", "agent command is not implemented", request_id=request_id)
    except AgentContractError as error:
        _write_json(
            envelope(
                command,
                error.request_id or request_id,
                outcome="failure",
                error={"stage": "request_validation", "code": error.code, "message": str(error)},
            ),
            output_stream,
        )
        return 2
