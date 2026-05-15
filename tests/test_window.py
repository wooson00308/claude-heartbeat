"""classify_transcript / compute_round_window 회귀 방지.

v0.2.1 hotfix 보호:
- compute_round_window: cursor 미스 hard fail (allow_reset=False가 디폴트)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from skills.dream import window
from skills.dream.window import (
    CursorNotFoundError,
    classify_transcript,
    compute_round_window,
    extract_last_message_uuid,
)


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")


def test_classify_active_small_skipped(tmp_path):
    """작고 최근에 쓴 파일은 should_process=False (Claude가 아직 쓰는 중일 수 있음)."""
    f = tmp_path / "active.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    info = classify_transcript(f)
    assert info["is_active"] is True
    assert info["is_huge"] is False
    assert info["should_process"] is False


def test_classify_inactive_small_processed(tmp_path):
    """오래 조용한 작은 파일은 should_process=True."""
    f = tmp_path / "inactive.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    old = time.time() - (window.ACTIVE_MTIME_QUIET_SEC + 60)
    os.utime(f, (old, old))

    info = classify_transcript(f)
    assert info["is_active"] is False
    assert info["should_process"] is True


def test_classify_huge_active_bypasses_mtime_gate(tmp_path):
    """크기 임계 넘으면 active여도 처리 대상."""
    f = tmp_path / "huge.jsonl"
    f.write_bytes(b"x" * (window.HUGE_FILE_SIZE_BYTES + 1024))
    info = classify_transcript(f)
    assert info["is_huge"] is True
    assert info["should_process"] is True


def test_classify_missing_file(tmp_path):
    info = classify_transcript(tmp_path / "no-such.jsonl")
    assert info["should_process"] is False


def test_extract_last_message_uuid(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "message": {"content": []}},
        {"type": "user", "uuid": "u2", "message": {"content": "yo"}},
    ])
    assert extract_last_message_uuid(f) == "u2"


def test_compute_round_window_no_cursor_starts_from_zero(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "message": {"content": []}},
    ])
    win = compute_round_window(f, cursor_uuid=None)
    assert win["cursor_line"] is None
    assert win["last_message_uuid"] == "a1"
    assert win["total_lines"] == 2


def test_compute_round_window_cursor_resumes_after_match(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "message": {"content": []}},
        {"type": "user", "uuid": "u2", "message": {"content": "yo"}},
    ])
    win = compute_round_window(f, cursor_uuid="u1")
    assert win["cursor_line"] == 1  # u1은 line 0, 다음 라인부터


def test_compute_round_window_cursor_miss_hard_fail(tmp_path):
    """v0.2.1: 기본은 hard fail. 이전엔 fail-open으로 LLM 중복 흡수 사고."""
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
    ])
    with pytest.raises(CursorNotFoundError):
        compute_round_window(f, cursor_uuid="ghost-uuid")


def test_compute_round_window_cursor_miss_allow_reset(tmp_path):
    """allow_reset=True 명시 시에만 처음부터 재처리 허용."""
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
    ])
    win = compute_round_window(f, cursor_uuid="ghost-uuid", allow_reset=True)
    assert win["cursor_line"] is None  # restart from 0
