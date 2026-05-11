"""Transcript JSONL preprocessor for /dream.

Extracts user text + assistant text from raw transcript JSONL files,
outputs lightweight markdown files ready for LLM consumption.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 활성/거대 transcript 처리 게이트
ACTIVE_MTIME_QUIET_SEC = 30 * 60          # 30분 — 이 시간 이상 조용하면 비활성으로 간주
HUGE_FILE_SIZE_BYTES = 10 * 1024 * 1024   # 10MB — 이 크기 이상이면 mtime 무시하고 강제 처리
COMPACT_BOUNDARY_MARKER = "This session is being continued from a previous conversation"

# 라운드당 처리량 cap
ROUND_MAX_MESSAGES = 300
ROUND_MAX_CHARS = 1500 * 1024          # 1500 KB 마크다운
ROUND_MAX_CHUNKS_ACTIVE = 2            # 활성 transcript는 라운드당 최대 N개 compact 청크


def get_project_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug


def get_dream_meta(slug: str) -> dict:
    """Read dream_meta.md and return processed transcript list."""
    meta_path = get_project_dir(slug) / "memory" / "dream_meta.md"
    if not meta_path.exists():
        return {"last_dream": None, "processed": set()}

    content = meta_path.read_text(encoding="utf-8")
    processed = set()
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- ") and line.endswith(".jsonl"):
            processed.add(line[2:])

    return {"processed": processed}


def parse_meta_v2(slug: str) -> dict[str, dict]:
    """Parse processed_v2 section from dream_meta.md.

    Returns {filename: {"last_uuid": "...", "status": "sealed"|"active"}} dict.
    Returns empty dict on any error (old-parser-compatible spirit).
    """
    meta_path = get_project_dir(slug) / "memory" / "dream_meta.md"
    if not meta_path.exists():
        return {}

    try:
        content = meta_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, dict] = {}
    in_v2 = False
    current: dict | None = None

    for raw_line in content.split("\n"):
        line = raw_line.strip()

        # Section header detection
        if line == "processed_v2:":
            in_v2 = True
            current = None
            continue

        # Exit v2 section on any non-indented non-empty line that looks like a new section header
        if in_v2 and line and not raw_line.startswith(" ") and not raw_line.startswith("-") and not raw_line.startswith("\t"):
            # Next top-level key → leave v2 section
            in_v2 = False
            current = None
            continue

        if not in_v2:
            continue

        # New entry: "- file: xxx.jsonl"
        if line.startswith("- file:"):
            if current is not None and "file" in current:
                result[current["file"]] = {
                    "last_uuid": current.get("last_uuid", ""),
                    "status": current.get("status", "sealed"),
                }
            fname = line[len("- file:"):].strip()
            current = {"file": fname}
            continue

        # Sub-fields under current entry
        if current is not None:
            if line.startswith("last_uuid:"):
                current["last_uuid"] = line[len("last_uuid:"):].strip()
            elif line.startswith("status:"):
                current["status"] = line[len("status:"):].strip()

    # Flush last entry
    if current is not None and "file" in current:
        result[current["file"]] = {
            "last_uuid": current.get("last_uuid", ""),
            "status": current.get("status", "sealed"),
        }

    return result


def get_combined_processed(slug: str) -> set[str]:
    """Return union of legacy processed and v2 processed filenames."""
    legacy = get_dream_meta(slug).get("processed", set())
    v2_keys = set(parse_meta_v2(slug).keys())
    return legacy | v2_keys


@contextlib.contextmanager
def _acquire_meta_lock(slug: str):
    """Context manager: acquire fcntl exclusive lock on .dream.lock file.

    Waits up to 30 seconds; if lock cannot be acquired, logs and yields anyway
    (fail-open for operational safety).
    """
    lock_path = get_project_dir(slug) / "memory" / ".dream.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_fd = open(lock_path, "w")  # noqa: WPS515
    except OSError as exc:
        logger.warning("[dream-prep] lock file open failed: %s — proceeding without lock", exc)
        yield
        return

    acquired = False
    try:
        # Non-blocking poll up to 30 s in 0.1 s increments
        import time
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.1)

        if not acquired:
            logger.warning("[dream-prep] could not acquire lock within 30 s — proceeding without lock")

        yield
    finally:
        if acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            lock_fd.close()
        except OSError:
            pass


def mark_processed(slug: str, filename: str, last_uuid: str, status: str = "sealed") -> None:
    """Append or update a processed_v2 entry in dream_meta.md.

    Thread/process safe via fcntl lock + atomic rename.
    Never raises; logs and returns on any error.
    """
    meta_path = get_project_dir(slug) / "memory" / "dream_meta.md"

    if not meta_path.exists():
        logger.warning("[dream-prep] mark_processed: dream_meta.md not found at %s", meta_path)
        return

    try:
        with _acquire_meta_lock(slug):
            _mark_processed_locked(meta_path, filename, last_uuid, status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dream-prep] mark_processed failed: %s", exc)


def _mark_processed_locked(meta_path: Path, filename: str, last_uuid: str, status: str) -> None:
    """Internal: perform the actual read-modify-write under caller's lock."""
    content = meta_path.read_text(encoding="utf-8")
    lines = content.split("\n")

    # Locate processed_v2 section and existing entry for this filename
    v2_section_idx: int | None = None
    entry_start: int | None = None
    entry_end: int | None = None

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "processed_v2:":
            v2_section_idx = i
            i += 1
            continue

        if v2_section_idx is not None:
            raw = lines[i]
            # Check if we've left the v2 section (non-indented non-list line)
            if raw and not raw.startswith(" ") and not raw.startswith("-") and not raw.startswith("\t") and stripped:
                break  # left section

            if stripped.startswith("- file:") and stripped[len("- file:"):].strip() == filename:
                entry_start = i
                # Consume sub-fields
                j = i + 1
                while j < len(lines):
                    sub = lines[j]
                    sub_s = sub.strip()
                    if sub_s.startswith("last_uuid:") or sub_s.startswith("status:"):
                        j += 1
                    else:
                        break
                entry_end = j
                break
        i += 1

    new_entry_lines = [
        f"- file: {filename}",
        f"  last_uuid: {last_uuid}",
        f"  status: {status}",
    ]

    if entry_start is not None:
        # Replace existing entry in-place
        lines[entry_start:entry_end] = new_entry_lines
    elif v2_section_idx is not None:
        # Append after the last line of the v2 section
        # Find insertion point: first non-v2 line after section header
        insert_at = v2_section_idx + 1
        while insert_at < len(lines):
            raw = lines[insert_at]
            s = raw.strip()
            if raw and not raw.startswith(" ") and not raw.startswith("-") and not raw.startswith("\t") and s:
                break
            insert_at += 1
        for offset, el in enumerate(new_entry_lines):
            lines.insert(insert_at + offset, el)
    else:
        # No processed_v2 section yet — append at end
        lines.append("")
        lines.append("processed_v2:")
        lines.extend(new_entry_lines)

    new_content = "\n".join(lines)

    # Atomic write: tmp file in same dir, then rename
    dir_path = meta_path.parent
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=dir_path, prefix=".dream_meta_", suffix=".tmp")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            os.rename(tmp_path, meta_path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as exc:
        raise RuntimeError(f"atomic write failed: {exc}") from exc


def extract_last_message_uuid(transcript_path: Path) -> str | None:
    """Return the uuid of the last user or assistant message line in the transcript.

    Scans forward and keeps the latest match (simple, correct for append-only jsonl).
    Returns None if the file is missing, unreadable, or has no qualifying lines.
    """
    try:
        last_uuid: str | None = None
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in {"user", "assistant"}:
                    uid = obj.get("uuid")
                    if uid:
                        last_uuid = uid
        return last_uuid
    except OSError:
        return None


def extract_last_prompt_leaf_uuid(transcript_path: Path) -> str | None:
    """Return the leafUuid of the last last-prompt entry in the transcript.

    Returns None if not found or on any error.
    """
    try:
        last_leaf: str | None = None
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "last-prompt":
                    leaf = obj.get("leafUuid")
                    if leaf:
                        last_leaf = leaf
        return last_leaf
    except OSError:
        return None


def detect_compact_boundaries(transcript_path: Path) -> list[int]:
    """Return 0-indexed line numbers where a compact boundary message appears.

    A boundary line satisfies both:
      - obj["type"] == "user"
      - obj["message"]["content"] is a str containing COMPACT_BOUNDARY_MARKER
        within the first 200 characters

    Returns an empty list on any error or when no boundary is found.
    """
    result: list[int] = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                content = obj.get("message", {}).get("content", "")
                if not isinstance(content, str):
                    continue
                if COMPACT_BOUNDARY_MARKER in content[:200]:
                    result.append(lineno)
    except OSError:
        pass
    return result


def classify_transcript(transcript_path: Path) -> dict:
    """Return a diagnostic dict describing the processing eligibility of a transcript.

    Keys:
        path            — absolute path string
        size_bytes      — file size in bytes
        mtime_age_seconds — seconds since last modification
        is_active       — True if mtime_age < ACTIVE_MTIME_QUIET_SEC
        is_huge         — True if size_bytes >= HUGE_FILE_SIZE_BYTES
        should_process  — True when eligible: (not is_active) or is_huge
    """
    import time

    try:
        stat = transcript_path.stat()
        size_bytes = stat.st_size
        mtime_age = time.time() - stat.st_mtime
    except OSError:
        return {
            "path": str(transcript_path),
            "size_bytes": 0,
            "mtime_age_seconds": float("inf"),
            "is_active": False,
            "is_huge": False,
            "should_process": False,
        }

    is_active = mtime_age < ACTIVE_MTIME_QUIET_SEC
    is_huge = size_bytes >= HUGE_FILE_SIZE_BYTES
    should_process = (not is_active) or is_huge

    return {
        "path": str(transcript_path),
        "size_bytes": size_bytes,
        "mtime_age_seconds": mtime_age,
        "is_active": is_active,
        "is_huge": is_huge,
        "should_process": should_process,
    }


def find_unprocessed_transcripts(slug: str) -> list[Path]:
    """Find transcript JSONL files not yet processed by /dream.

    Applies classify_transcript gate: active small files are excluded.
    Huge active files bypass the mtime gate and are included.
    """
    project_dir = get_project_dir(slug)
    processed = get_combined_processed(slug)

    transcripts = []
    for f in sorted(project_dir.glob("*.jsonl")):
        if f.name not in processed:
            if classify_transcript(f)["should_process"]:
                transcripts.append(f)

    return transcripts


def compute_round_window(transcript_path: Path, cursor_uuid: str | None = None) -> dict:
    """Capture an atomic snapshot of the transcript for one processing round.

    Performs a single sequential scan of the file to collect:
      - H_bytes / total_lines  (EOF at the moment of stat())
      - compact_boundaries      (0-indexed line numbers)
      - last_message_uuid       (last user/assistant uuid)
      - cursor_line             (first line to process, derived from cursor_uuid)

    cursor_uuid resolution:
      - None  → cursor_line = None  (caller should start from line 0)
      - Found → cursor_line = (that line index + 1), i.e. resume *after* it
      - Not found → cursor_line = None + WARNING log  (fail-open: restart from 0)

    Returns:
        {
            "transcript_path": str,
            "H_bytes": int,
            "total_lines": int,          # line-count-precision EOF cap
            "last_message_uuid": str | None,
            "compact_boundaries": list[int],
            "cursor_line": int | None,
            "cursor_uuid": str | None,
            "captured_at": str,          # ISO timestamp
        }
    """
    result: dict = {
        "transcript_path": str(transcript_path),
        "H_bytes": 0,
        "total_lines": 0,
        "last_message_uuid": None,
        "compact_boundaries": [],
        "cursor_line": None,
        "cursor_uuid": cursor_uuid,
        "captured_at": datetime.utcnow().isoformat() + "Z",
    }

    try:
        stat_info = transcript_path.stat()
        result["H_bytes"] = stat_info.st_size
    except OSError as exc:
        logger.warning("[dream-prep] compute_round_window: stat() failed for %s: %s", transcript_path, exc)
        return result

    # Single sequential scan — collect everything in one pass
    boundaries: list[int] = []
    last_uuid: str | None = None
    cursor_line_found: int | None = None
    total_lines = 0

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh):
                total_lines = lineno + 1
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")

                # Track compact boundaries
                if msg_type == "user":
                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, str) and COMPACT_BOUNDARY_MARKER in content[:200]:
                        boundaries.append(lineno)

                # Track last uuid + cursor resolution
                if msg_type in {"user", "assistant"}:
                    uid = obj.get("uuid")
                    if uid:
                        last_uuid = uid
                        if cursor_uuid is not None and uid == cursor_uuid:
                            cursor_line_found = lineno

    except OSError as exc:
        logger.warning("[dream-prep] compute_round_window: read failed for %s: %s", transcript_path, exc)
        return result

    result["total_lines"] = total_lines
    result["compact_boundaries"] = boundaries
    result["last_message_uuid"] = last_uuid

    if cursor_uuid is None:
        # No cursor supplied — start from beginning
        result["cursor_line"] = None
    elif cursor_line_found is not None:
        # Resume from the line *after* the cursor message
        result["cursor_line"] = cursor_line_found + 1
    else:
        # cursor_uuid supplied but not found in file — fail-open
        logger.warning(
            "[dream-prep] compute_round_window: cursor_uuid %r not found in %s — resetting to line 0",
            cursor_uuid, transcript_path,
        )
        result["cursor_line"] = None

    return result


def extract_partial_conversation(
    transcript_path: Path,
    cursor_uuid: str | None = None,
    max_messages: int = ROUND_MAX_MESSAGES,
    max_chars: int = ROUND_MAX_CHARS,
    max_chunks: int | None = None,
) -> tuple[list[dict], str | None, dict]:
    """Extract a bounded slice of conversation from cursor_uuid up to cap limits.

    Bounded extraction for large / active transcripts. Processes lines from
    cursor_uuid (exclusive) forward, stopping at the first cap reached.

    Args:
        transcript_path: Path to the JSONL transcript.
        cursor_uuid: UUID of the last already-processed message. None = start from line 0.
        max_messages: Stop after accumulating this many user/assistant messages.
        max_chars: Stop after accumulated content characters exceed this threshold.
        max_chunks: Stop after this many compact-boundary chunks have been entered.
                    None = no chunk cap.

    Returns:
        (conversation, next_cursor_uuid, stats)

        conversation:
            List of dicts in extract_conversation format:
            {"role": "user"|"assistant", "text": str, "time": str}

        next_cursor_uuid:
            UUID of the last message added to conversation.
            None if no messages were processed.

        stats:
            {
                "messages_processed": int,
                "chars_processed": int,
                "chunks_processed": int,
                "hit_cap": "messages"|"chars"|"chunks"|"end_of_window"|"none",
                "window": dict,   # full compute_round_window output
            }
    """
    window = compute_round_window(transcript_path, cursor_uuid)
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
                # Honour EOF cap (lines written after stat() are next round's job)
                if lineno >= eof_line:
                    break

                # Skip lines before cursor
                if lineno < start_line:
                    continue

                # Compact boundary encountered → increment chunk counter
                # (Check before parse to avoid double-parsing; re-parse is cheap)
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

                msg_type = obj.get("type")
                if msg_type not in {"user", "assistant"}:
                    continue

                timestamp = obj.get("timestamp", "")
                uid = obj.get("uuid")

                if msg_type == "user":
                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        text = content.strip()
                        if not text or text.startswith("<") or len(text) <= 2:
                            continue
                        turn = {"role": "user", "text": text, "time": timestamp}
                    else:
                        continue

                else:  # assistant
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        texts = []
                        tool_names = []
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
                            turn = {
                                "role": "assistant",
                                "text": "\n".join(texts),
                                "time": timestamp,
                            }
                        elif tool_names:
                            turn = {
                                "role": "assistant",
                                "text": f"[도구 호출: {', '.join(tool_names)}]",
                                "time": timestamp,
                            }
                        else:
                            continue
                    else:
                        continue

                # Accumulate
                turn_chars = len(str(turn["text"]))
                conversation.append(turn)
                if uid:
                    next_cursor_uuid = uid
                messages_processed += 1
                chars_processed += turn_chars

                # Cap checks (evaluated after adding so the triggering message is included)
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

    # Handle unclosed code block
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
    """Extract meaningful conversation turns from a transcript JSONL."""
    lines = transcript_path.read_text(encoding="utf-8").strip().split("\n")
    conversation = []

    for line in lines:
        try:
            obj = json.loads(line)
            msg_type = obj.get("type", "")
            timestamp = obj.get("timestamp", "")

            if msg_type == "user":
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str):
                    text = content.strip()
                    # Skip system/command messages and short noise
                    if text and not text.startswith("<") and len(text) > 2:
                        conversation.append({
                            "role": "user",
                            "text": text,
                            "time": timestamp,
                        })

            elif msg_type == "assistant":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    texts = []
                    tool_names = []
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
                        conversation.append({
                            "role": "assistant",
                            "text": "\n".join(texts),
                            "time": timestamp,
                        })
                    elif tool_names:
                        conversation.append({
                            "role": "assistant",
                            "text": f"[도구 호출: {', '.join(tool_names)}]",
                            "time": timestamp,
                        })

        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    return conversation


def _merge_consecutive_tool_calls(turns: list[dict]) -> list[dict]:
    """Merge consecutive assistant tool-call-only turns into one line."""
    merged = []
    tool_buffer = []

    for turn in turns:
        is_tool = turn["role"] == "assistant" and turn["text"].startswith("[도구 호출:")

        if is_tool:
            # Extract tool names from "[도구 호출: X, Y]"
            names_str = turn["text"][len("[도구 호출: "):-1]
            tool_buffer.extend(n.strip() for n in names_str.split(","))
        else:
            if tool_buffer:
                # Collapse buffer: count duplicates
                merged.append({
                    "role": "assistant",
                    "text": f"[도구: {_summarize_tools(tool_buffer)}]",
                    "time": turn["time"],
                })
                tool_buffer = []
            merged.append(turn)

    # Flush remaining
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

    # Merge consecutive tool calls
    conversation = _merge_consecutive_tool_calls(conversation)

    lines = [f"## 세션 {session_id[:8]}"]

    # Get date from first timestamp
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

        # Compress code blocks
        text = _compress_code_blocks(text)

        lines.append(f"{role}: {text}")
        lines.append("")

    return "\n".join(lines)


def preprocess_project(
    slug: str,
    output_dir: Path | None = None,
    limit: int = 5,
    dry_run: bool = False,
) -> None:
    """Preprocess unprocessed transcripts for a project.

    Branches per transcript:
      - sealed-candidate (not active AND not huge): full extract → status="sealed"
      - active-or-huge (active OR huge): partial extract from cursor_uuid → status="active"

    dry_run=True suppresses mark_processed calls (no dream_meta.md side effects).
    """
    unprocessed = find_unprocessed_transcripts(slug)

    if not unprocessed:
        print(f"[{slug}] 미처리 transcript 없음")
        return

    print(f"[{slug}] 미처리 transcript {len(unprocessed)}개 발견, {min(limit, len(unprocessed))}개 처리"
          + (" [dry-run]" if dry_run else ""))

    # Output directory
    if output_dir is None:
        output_dir = get_project_dir(slug) / "memory" / "_dream_prep"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load v2 meta for cursor_uuid resolution (active files)
    meta_v2 = parse_meta_v2(slug)

    batch = unprocessed[:limit]
    processed_count = 0

    for transcript_path in batch:
        session_id = transcript_path.stem
        filename = transcript_path.name
        cls = classify_transcript(transcript_path)

        is_active = cls["is_active"]
        is_huge = cls["is_huge"]
        use_partial = is_active or is_huge

        if use_partial:
            # --- 분기 B: active OR huge — 부분 처리 ---
            file_meta = meta_v2.get(filename, {})
            cursor_uuid: str | None = file_meta.get("last_uuid") or None

            print(
                f"[{slug}] {filename} → active-or-huge"
                f" (active={is_active}, huge={is_huge},"
                f" size={cls['size_bytes'] // 1024}KB,"
                f" cursor={'set' if cursor_uuid else 'none'})"
            )

            conversation, next_cursor_uuid, stats = extract_partial_conversation(
                transcript_path,
                cursor_uuid=cursor_uuid,
                max_chunks=ROUND_MAX_CHUNKS_ACTIVE,
            )

            print(
                f"[{slug}] {filename} partial result:"
                f" messages={stats['messages_processed']},"
                f" chars={stats['chars_processed']},"
                f" hit_cap={stats['hit_cap']},"
                f" next_cursor={'set' if next_cursor_uuid else 'none'}"
            )

            if not conversation:
                print(f"[{slug}] {filename} 처리 가능한 메시지 없음 (skip mark)")
                continue

            md = conversation_to_markdown(session_id, conversation)
            if not md:
                continue

            # Active 파일은 같은 세션이 여러 라운드에 걸침 → round timestamp suffix
            round_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_short = session_id[:12]
            output_file = output_dir / f"prep_{session_short}_partial_{round_ts}.md"
            output_file.write_text(
                f"# Dream Prep (partial) — {slug}\n\n"
                f"파일: {filename}\ncursor: {cursor_uuid or 'start'} → {next_cursor_uuid}\n"
                f"hit_cap: {stats['hit_cap']}\n\n---\n\n{md}",
                encoding="utf-8",
            )
            print(f"[{slug}] → {output_file}")

            if next_cursor_uuid is None:
                print(f"[{slug}] {filename} next_cursor_uuid=None → mark_processed 스킵")
            else:
                _maybe_mark(dry_run, slug, filename, next_cursor_uuid, "active")

        else:
            # --- 분기 A: sealed-candidate — 전체 처리 ---
            print(
                f"[{slug}] {filename} → sealed-candidate"
                f" (size={cls['size_bytes'] // 1024}KB,"
                f" age={cls['mtime_age_seconds'] / 60:.1f}min)"
            )

            conversation = extract_conversation(transcript_path)

            if not conversation:
                print(f"[{slug}] {filename} 의미 있는 대화 없음")
                continue

            md = conversation_to_markdown(session_id, conversation)
            if not md:
                continue

            # uuid 보존 안 됨(extract_conversation은 role/text/time만) → 별도 호출
            last_uuid = extract_last_message_uuid(transcript_path)

            print(
                f"[{slug}] {filename} sealed result:"
                f" turns={len(conversation)},"
                f" last_uuid={'set' if last_uuid else 'none'}"
            )

            # 기존 prep 파일 네이밍 패턴 유지 (sealed는 파일마다 단일 섹션)
            round_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_short = session_id[:12]
            output_file = output_dir / f"prep_{session_short}_{round_ts}.md"
            output_file.write_text(
                f"# Dream Prep — {slug}\n\n처리 대상: {filename}\n\n---\n\n{md}",
                encoding="utf-8",
            )
            print(f"[{slug}] → {output_file}")

            _maybe_mark(dry_run, slug, filename, last_uuid or "", "sealed")

        processed_count += 1

    print(f"[{slug}] 완료: {processed_count}개 처리" + (" [dry-run]" if dry_run else ""))


def _maybe_mark(dry_run: bool, slug: str, filename: str, last_uuid: str, status: str) -> None:
    """Call mark_processed unless dry_run is set. Logs but never raises."""
    if dry_run:
        print(f"[dry-run] mark_processed({filename!r}, last_uuid={last_uuid!r}, status={status!r})")
        return
    try:
        mark_processed(slug, filename, last_uuid, status)
        print(f"[{slug}] marked: {filename} ({status})")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dream-prep] mark_processed call failed for %s: %s — continuing", filename, exc)


def list_projects() -> list[str]:
    """List all project slugs that have transcripts."""
    if not PROJECTS_DIR.exists():
        return []
    result = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and list(d.glob("*.jsonl")):
            result.append(d.name)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dream-prep",
        description="Preprocess transcript JSONL files for /dream"
    )
    sub = parser.add_subparsers(dest="command")

    # prep
    p_prep = sub.add_parser("prep", help="Preprocess transcripts for a project")
    p_prep.add_argument("--slug", "-s", required=True, help="Project slug (e.g. -Users-yourname)")
    p_prep.add_argument("--limit", "-n", type=int, default=5, help="Max transcripts to process")
    p_prep.add_argument("--dry-run", action="store_true", help="Skip mark_processed calls (no dream_meta.md changes)")

    # list
    sub.add_parser("list", help="List projects with transcripts")

    # status
    p_status = sub.add_parser("status", help="Show processing status for a project")
    p_status.add_argument("--slug", "-s", required=True, help="Project slug")

    # classify
    p_classify = sub.add_parser("classify", help="Show processing eligibility for all transcripts in a project")
    p_classify.add_argument("--slug", "-s", required=True, help="Project slug")

    # mark
    p_mark = sub.add_parser("mark", help="Mark a transcript as processed (v2 format)")
    p_mark.add_argument("--slug", "-s", required=True, help="Project slug")
    p_mark.add_argument("--file", "-f", required=True, dest="filename", help="Transcript filename (e.g. abc.jsonl)")
    p_mark.add_argument("--last-uuid", required=True, help="UUID of the last processed message")
    p_mark.add_argument("--status", default="sealed", choices=["sealed", "active"], help="Processing status (default: sealed)")

    args = parser.parse_args()

    if args.command == "prep":
        preprocess_project(args.slug, limit=args.limit, dry_run=args.dry_run)
    elif args.command == "list":
        for slug in list_projects():
            count = len(list(get_project_dir(slug).glob("*.jsonl")))
            print(f"  {slug} ({count} transcripts)")
    elif args.command == "status":
        unprocessed = find_unprocessed_transcripts(args.slug)
        total = len(list(get_project_dir(args.slug).glob("*.jsonl")))
        print(f"  전체: {total}, 처리됨: {total - len(unprocessed)}, 미처리: {len(unprocessed)}")
    elif args.command == "classify":
        project_dir = get_project_dir(args.slug)
        jsonl_files = sorted(project_dir.glob("*.jsonl"))
        if not jsonl_files:
            print(f"  [{args.slug}] jsonl 파일 없음")
        else:
            header = f"{'filename':<50} {'size_MB':>8} {'mtime_age_min':>14} {'is_active':>9} {'is_huge':>8} {'should_process':>14}"
            print(header)
            print("-" * len(header))
            for f in jsonl_files:
                info = classify_transcript(f)
                size_mb = info["size_bytes"] / (1024 * 1024)
                age_min = info["mtime_age_seconds"] / 60
                print(
                    f"{f.name:<50} {size_mb:>8.2f} {age_min:>14.1f}"
                    f" {str(info['is_active']):>9} {str(info['is_huge']):>8}"
                    f" {str(info['should_process']):>14}"
                )
    elif args.command == "mark":
        mark_processed(args.slug, args.filename, args.last_uuid, args.status)
        print(f"  [{args.slug}] marked: {args.filename} ({args.status})")
    else:
        parser.print_help()
