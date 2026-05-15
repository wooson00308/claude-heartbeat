"""Transcript JSONL 분류 및 라운드 윈도우 캡처.

활성/거대 transcript 게이트, cursor_uuid 기반 라운드 윈도우 동결.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .meta import get_dream_meta, parse_meta_v2
from .paths import get_project_dir

logger = logging.getLogger(__name__)

# 활성/거대 transcript 처리 게이트
ACTIVE_MTIME_QUIET_SEC = 30 * 60          # 30분 — 이 시간 이상 조용하면 비활성으로 간주
HUGE_FILE_SIZE_BYTES = 10 * 1024 * 1024   # 10MB — 이 크기 이상이면 mtime 무시하고 강제 처리
COMPACT_BOUNDARY_MARKER = "This session is being continued from a previous conversation"


class CursorNotFoundError(RuntimeError):
    """cursor_uuid가 transcript에 없을 때. allow_reset=True가 아니면 발생."""


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

    처리됨 판정:
    - legacy `processed:` 항목 → 완전히 끝남, 스킵
    - v2 `status: sealed` → 완전히 끝남, 스킵
    - v2 `status: active` → 부분 처리, 다음 라운드에서 cursor부터 이어 처리.
      classify gate(active small은 제외 / huge는 강제 처리)가 통과시키면 잡힘
    - 마킹 없음 → 신규 파일, classify gate 통과 시 잡힘

    이전엔 active도 "처리됨" set에 들어가서 영영 안 잡혔다 (issue #9).
    """
    project_dir = get_project_dir(slug)
    legacy = get_dream_meta(slug).get("processed", set())
    v2 = parse_meta_v2(slug)

    transcripts = []
    for f in sorted(project_dir.glob("*.jsonl")):
        if f.name in legacy:
            continue
        meta = v2.get(f.name)
        if meta and meta.get("status") == "sealed":
            continue
        # 신규 또는 active → classify gate
        if classify_transcript(f)["should_process"]:
            transcripts.append(f)

    return transcripts


def compute_round_window(
    transcript_path: Path,
    cursor_uuid: str | None = None,
    allow_reset: bool = False,
) -> dict:
    """Capture an atomic snapshot of the transcript for one processing round.

    Performs a single sequential scan of the file to collect:
      - H_bytes / total_lines  (EOF at the moment of stat())
      - compact_boundaries      (0-indexed line numbers)
      - last_message_uuid       (last user/assistant uuid)
      - cursor_line             (first line to process, derived from cursor_uuid)

    cursor_uuid resolution:
      - None  → cursor_line = None  (caller should start from line 0)
      - Found → cursor_line = (that line index + 1), i.e. resume *after* it
      - Not found:
          - allow_reset=True  → cursor_line = None + WARNING log (restart from 0)
          - allow_reset=False → raise CursorNotFoundError (default; safer)
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

                if msg_type == "user":
                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, str) and COMPACT_BOUNDARY_MARKER in content[:200]:
                        boundaries.append(lineno)

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
        result["cursor_line"] = None
    elif cursor_line_found is not None:
        result["cursor_line"] = cursor_line_found + 1
    else:
        # cursor_uuid 미스. fail-open이 LLM 중복 흡수 사고를 만들었던 이력이 있어
        # 기본은 hard fail. --reset-cursor 같은 명시 의사가 있을 때만 restart 허용.
        if not allow_reset:
            raise CursorNotFoundError(
                f"cursor_uuid {cursor_uuid!r} not found in {transcript_path}. "
                f"Pass --reset-cursor to restart from line 0 explicitly."
            )
        logger.warning(
            "[dream-prep] compute_round_window: cursor_uuid %r not found in %s — "
            "allow_reset=True, restarting from line 0",
            cursor_uuid, transcript_path,
        )
        result["cursor_line"] = None

    return result
