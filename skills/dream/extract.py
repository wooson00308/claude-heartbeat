"""Conversation 추출 및 마크다운 변환.

JSONL → 경량 마크다운. 코드 블록 압축, 도구 호출 합치기, 라운드 캡 처리.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .window import compute_round_window

logger = logging.getLogger(__name__)

# 라운드당 처리량 cap
ROUND_MAX_MESSAGES = 300
ROUND_MAX_CHARS = 1500 * 1024          # 1500 KB 마크다운
ROUND_MAX_CHUNKS_ACTIVE = 2            # 활성 transcript는 라운드당 최대 N개 compact 청크


def extract_partial_conversation(
    transcript_path: Path,
    cursor_uuid: str | None = None,
    max_messages: int = ROUND_MAX_MESSAGES,
    max_chars: int = ROUND_MAX_CHARS,
    max_chunks: int | None = None,
    allow_reset: bool = False,
) -> tuple[list[dict], str | None, dict]:
    """Extract a bounded slice of conversation from cursor_uuid up to cap limits.

    Bounded extraction for large / active transcripts. Processes lines from
    cursor_uuid (exclusive) forward, stopping at the first cap reached.

    Returns:
        (conversation, next_cursor_uuid, stats)
    """
    window = compute_round_window(transcript_path, cursor_uuid, allow_reset=allow_reset)
    start_line = window["cursor_line"] if window["cursor_line"] is not None else 0
    eof_line = window["total_lines"]  # lines 0 .. eof_line-1 are valid

    conversation: list[dict] = []
    next_cursor_uuid: str | None = None
    messages_processed = 0
    chars_processed = 0
    chunks_processed = 0
    hit_cap: str | None = None

    boundary_set: set[int] = set(window["compact_boundaries"])

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh):
                if lineno >= eof_line:
                    break

                if lineno < start_line:
                    continue

                if lineno in boundary_set:
                    chunks_processed += 1
                    if max_chunks is not None and chunks_processed >= max_chunks:
                        hit_cap = "chunks"
                        break

                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                turn = _parse_turn(obj)
                if turn is None:
                    continue

                uid = obj.get("uuid")
                turn_chars = len(str(turn["text"]))
                conversation.append(turn)
                if uid:
                    next_cursor_uuid = uid
                messages_processed += 1
                chars_processed += turn_chars

                if messages_processed >= max_messages:
                    hit_cap = "messages"
                    break
                if chars_processed >= max_chars:
                    hit_cap = "chars"
                    break

    except OSError as exc:
        logger.warning("[dream-prep] extract_partial_conversation: read failed for %s: %s", transcript_path, exc)

    if hit_cap is None:
        hit_cap = "end_of_window" if messages_processed > 0 else "none"

    stats = {
        "messages_processed": messages_processed,
        "chars_processed": chars_processed,
        "chunks_processed": chunks_processed,
        "hit_cap": hit_cap,
        "window": window,
    }

    return conversation, next_cursor_uuid, stats


def _parse_turn(obj: dict) -> dict | None:
    """JSONL line 객체에서 user/assistant turn 추출.

    user: 문자열 content + 3자 초과 + `<` 시작 안 함 (system message 노이즈 제거).
    assistant: text 블록(우선) 또는 tool_use 블록 합쳐서 "[도구 호출: ...]" 형식.

    반환: {"role", "text", "time"} 또는 조건 위반 시 None.

    extract_conversation(전체)과 extract_partial_conversation(부분) 둘 다 호출.
    한쪽 버그 수정이 다른 쪽으로 드리프트하던 위험 차단.
    """
    msg_type = obj.get("type")
    if msg_type not in {"user", "assistant"}:
        return None

    timestamp = obj.get("timestamp", "")

    if msg_type == "user":
        content = obj.get("message", {}).get("content", "")
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text or text.startswith("<") or len(text) <= 2:
            return None
        return {"role": "user", "text": text, "time": timestamp}

    # assistant
    content = obj.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return None

    texts: list[str] = []
    tool_names: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            t = block.get("text", "").strip()
            if t:
                texts.append(t)
        elif block.get("type") == "tool_use":
            tool_names.append(block.get("name", "?"))

    if texts:
        return {"role": "assistant", "text": "\n".join(texts), "time": timestamp}
    if tool_names:
        return {
            "role": "assistant",
            "text": f"[도구 호출: {', '.join(tool_names)}]",
            "time": timestamp,
        }
    return None


def _compress_code_blocks(text: str) -> str:
    """Compress code blocks: keep 3 lines or less, truncate longer ones."""
    result = []
    in_code = False
    code_lines = []
    code_fence = ""

    for line in text.split("\n"):
        if not in_code and re.match(r"^```", line):
            in_code = True
            code_fence = line
            code_lines = []
        elif in_code and re.match(r"^```\s*$", line):
            in_code = False
            if len(code_lines) <= 3:
                result.append(code_fence)
                result.extend(code_lines)
                result.append("```")
            else:
                result.append(code_fence)
                result.append(code_lines[0])
                result.append(f"... ({len(code_lines)}줄 생략)")
                result.append("```")
        elif in_code:
            code_lines.append(line)
        else:
            result.append(line)

    if in_code and code_lines:
        if len(code_lines) <= 3:
            result.append(code_fence)
            result.extend(code_lines)
        else:
            result.append(code_fence)
            result.append(code_lines[0])
            result.append(f"... ({len(code_lines)}줄 생략)")

    return "\n".join(result)


def extract_conversation(transcript_path: Path) -> list[dict]:
    """Extract meaningful conversation turns from a transcript JSONL.

    errors="replace": Claude Code가 transcript 쓰는 중 invalid utf-8 byte가
    들어가도 crash 안 함. extract_partial_conversation은 처음부터 errors=replace
    였는데 이 함수만 누락이라 sealed-candidate 처리 흐름에서만 회복력 부재였음.
    """
    lines = transcript_path.read_text(encoding="utf-8", errors="replace").strip().split("\n")
    conversation: list[dict] = []

    for line in lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

        turn = _parse_turn(obj)
        if turn is not None:
            conversation.append(turn)

    return conversation


def _merge_consecutive_tool_calls(turns: list[dict]) -> list[dict]:
    """Merge consecutive assistant tool-call-only turns into one line."""
    merged = []
    tool_buffer = []

    for turn in turns:
        is_tool = turn["role"] == "assistant" and turn["text"].startswith("[도구 호출:")

        if is_tool:
            names_str = turn["text"][len("[도구 호출: "):-1]
            tool_buffer.extend(n.strip() for n in names_str.split(","))
        else:
            if tool_buffer:
                merged.append({
                    "role": "assistant",
                    "text": f"[도구: {_summarize_tools(tool_buffer)}]",
                    "time": turn["time"],
                })
                tool_buffer = []
            merged.append(turn)

    if tool_buffer:
        merged.append({
            "role": "assistant",
            "text": f"[도구: {_summarize_tools(tool_buffer)}]",
            "time": "",
        })

    return merged


def _summarize_tools(names: list[str]) -> str:
    """Summarize tool names with counts: ['Bash', 'Read', 'Read'] -> 'Bash, Read x2'"""
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1

    parts = []
    for name, count in counts.items():
        if count > 1:
            parts.append(f"{name} x{count}")
        else:
            parts.append(name)
    return ", ".join(parts)


def conversation_to_markdown(session_id: str, conversation: list[dict]) -> str:
    """Convert conversation list to a compact markdown format."""
    if not conversation:
        return ""

    conversation = _merge_consecutive_tool_calls(conversation)

    lines = [f"## 세션 {session_id[:8]}"]

    first_time = conversation[0].get("time", "")
    if first_time:
        try:
            dt = datetime.fromisoformat(first_time.replace("Z", "+00:00"))
            lines[0] += f" ({dt.strftime('%Y-%m-%d %H:%M')})"
        except (ValueError, TypeError):
            pass

    lines.append("")

    for turn in conversation:
        role = "U" if turn["role"] == "user" else "A"
        text = turn["text"]
        text = _compress_code_blocks(text)
        lines.append(f"{role}: {text}")
        lines.append("")

    return "\n".join(lines)
