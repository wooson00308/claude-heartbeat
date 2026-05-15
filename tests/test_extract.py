"""extract.py 단위 테스트.

마크다운 압축, 도구 호출 합치기, 라운드 캡 동작.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# extract → window → meta(fcntl). 윈도우는 Phase 2 전까지 통째 skip.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="extract → window → meta 체인이 fcntl 의존",
)

from skills.dream.extract import (  # noqa: E402
    _compress_code_blocks,
    _merge_consecutive_tool_calls,
    _summarize_tools,
    extract_partial_conversation,
)


def test_compress_code_short_kept_intact():
    text = "전\n```py\na = 1\nb = 2\n```\n후"
    out = _compress_code_blocks(text)
    assert "a = 1" in out
    assert "b = 2" in out
    assert "생략" not in out


def test_compress_code_long_truncated():
    body = "\n".join(f"line {i}" for i in range(10))
    text = f"```py\n{body}\n```"
    out = _compress_code_blocks(text)
    assert "line 0" in out
    assert "line 9" not in out
    assert "생략" in out


def test_merge_consecutive_tool_calls():
    turns = [
        {"role": "assistant", "text": "[도구 호출: Bash]", "time": ""},
        {"role": "assistant", "text": "[도구 호출: Read, Read]", "time": ""},
        {"role": "user", "text": "다음", "time": "t"},
    ]
    merged = _merge_consecutive_tool_calls(turns)
    assert len(merged) == 2
    assert merged[0]["text"].startswith("[도구:")
    assert "Bash" in merged[0]["text"]
    assert "Read x2" in merged[0]["text"]


def test_summarize_tools_counts_duplicates():
    assert _summarize_tools(["Bash", "Read", "Read"]) == "Bash, Read x2"
    assert _summarize_tools(["Edit"]) == "Edit"


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")


def test_extract_partial_messages_cap(tmp_path):
    """max_messages 도달하면 즉시 정지하고 hit_cap='messages' 보고."""
    f = tmp_path / "t.jsonl"
    objs = []
    for i in range(10):
        objs.append({
            "type": "user",
            "uuid": f"u{i}",
            "message": {"content": f"메시지 {i} 본문"},
        })
    _write_jsonl(f, objs)

    conv, next_cursor, stats = extract_partial_conversation(
        f, cursor_uuid=None, max_messages=3,
    )
    assert len(conv) == 3
    assert stats["hit_cap"] == "messages"
    assert next_cursor == "u2"  # 0~2번째까지 들어감


def test_extract_partial_skips_short_user_text(tmp_path):
    """3자 이하 user 메시지는 노이즈로 제거된다."""
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "uuid": "u1", "message": {"content": "ㅇ"}},
        {"type": "user", "uuid": "u2", "message": {"content": "정상 본문"}},
    ])
    conv, _, _ = extract_partial_conversation(f, cursor_uuid=None)
    assert len(conv) == 1
    assert conv[0]["text"] == "정상 본문"
