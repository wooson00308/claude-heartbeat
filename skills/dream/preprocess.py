"""Transcript JSONL preprocessor for /dream.

Extracts user text + assistant text from raw transcript JSONL files,
outputs lightweight markdown files ready for LLM consumption.
"""

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


def find_unprocessed_transcripts(slug: str) -> list[Path]:
    """Find transcript JSONL files not yet processed by /dream."""
    project_dir = get_project_dir(slug)
    processed = get_combined_processed(slug)

    transcripts = []
    for f in sorted(project_dir.glob("*.jsonl")):
        if f.name not in processed:
            transcripts.append(f)

    return transcripts


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


def preprocess_project(slug: str, output_dir: Path | None = None, limit: int = 5) -> None:
    """Preprocess unprocessed transcripts for a project."""
    unprocessed = find_unprocessed_transcripts(slug)

    if not unprocessed:
        print(f"[{slug}] 미처리 transcript 없음")
        return

    print(f"[{slug}] 미처리 transcript {len(unprocessed)}개 발견, {min(limit, len(unprocessed))}개 처리")

    # Output directory
    if output_dir is None:
        output_dir = get_project_dir(slug) / "memory" / "_dream_prep"
    output_dir.mkdir(parents=True, exist_ok=True)

    batch = unprocessed[:limit]
    all_sections = []

    for transcript_path in batch:
        session_id = transcript_path.stem
        conversation = extract_conversation(transcript_path)

        if not conversation:
            continue

        md = conversation_to_markdown(session_id, conversation)
        if md:
            all_sections.append(md)

    if all_sections:
        output_file = output_dir / f"prep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        content = f"# Dream Prep — {slug}\n\n처리 대상: {len(batch)}개 transcript\n\n---\n\n"
        content += "\n---\n\n".join(all_sections)
        output_file.write_text(content, encoding="utf-8")
        print(f"[{slug}] → {output_file} ({len(all_sections)}개 세션)")
    else:
        print(f"[{slug}] 의미 있는 대화 없음")


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

    # list
    sub.add_parser("list", help="List projects with transcripts")

    # status
    p_status = sub.add_parser("status", help="Show processing status for a project")
    p_status.add_argument("--slug", "-s", required=True, help="Project slug")

    # mark
    p_mark = sub.add_parser("mark", help="Mark a transcript as processed (v2 format)")
    p_mark.add_argument("--slug", "-s", required=True, help="Project slug")
    p_mark.add_argument("--file", "-f", required=True, dest="filename", help="Transcript filename (e.g. abc.jsonl)")
    p_mark.add_argument("--last-uuid", required=True, help="UUID of the last processed message")
    p_mark.add_argument("--status", default="sealed", choices=["sealed", "active"], help="Processing status (default: sealed)")

    args = parser.parse_args()

    if args.command == "prep":
        preprocess_project(args.slug, limit=args.limit)
    elif args.command == "list":
        for slug in list_projects():
            count = len(list(get_project_dir(slug).glob("*.jsonl")))
            print(f"  {slug} ({count} transcripts)")
    elif args.command == "status":
        unprocessed = find_unprocessed_transcripts(args.slug)
        total = len(list(get_project_dir(args.slug).glob("*.jsonl")))
        print(f"  전체: {total}, 처리됨: {total - len(unprocessed)}, 미처리: {len(unprocessed)}")
    elif args.command == "mark":
        mark_processed(args.slug, args.filename, args.last_uuid, args.status)
        print(f"  [{args.slug}] marked: {args.filename} ({args.status})")
    else:
        parser.print_help()
