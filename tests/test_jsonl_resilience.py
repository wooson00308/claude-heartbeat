"""손상된 JSONL 입력 회복력 회귀.

Phase 1 리뷰 미반영 항목: 깨진 utf-8 / 잘린 마지막 라인 / BOM 등 비정상
JSONL에서도 extract_conversation / extract_partial_conversation이 raise하지
않고 가능한 turn만 추출하는지 검증.

이 회복력은 코드에 try/except + errors="replace"로 깔려있지만 실증된 적이
없었다. Claude Code가 transcript에 쓰는 중 인터럽트되거나 파일시스템 이슈로
일부 라인이 깨질 때 dream 흐름이 통째로 죽지 않게 잠근다.
"""

from __future__ import annotations

import json

from skills.dream.extract import extract_conversation, extract_partial_conversation


def _good_user_line(uid: str, text: str) -> bytes:
    return (json.dumps({"type": "user", "uuid": uid, "message": {"content": text}}) + "\n").encode("utf-8")


# --- extract_conversation (전체) ---

def test_extract_conversation_skips_invalid_utf8_line(tmp_path):
    """중간 라인이 invalid utf-8 byte sequence여도 앞뒤 정상 라인은 추출."""
    f = tmp_path / "broken_utf8.jsonl"
    f.write_bytes(
        _good_user_line("u1", "hi there")
        + b"\x80\x81\x82 garbage broken bytes\n"
        + _good_user_line("u2", "yo there")
    )

    result = extract_conversation(f)
    assert len(result) == 2
    assert result[0]["text"] == "hi there"
    assert result[1]["text"] == "yo there"


def test_extract_conversation_skips_truncated_last_line(tmp_path):
    """마지막 라인이 잘려 있어도(파일 인터럽트) 앞 정상 라인은 추출."""
    f = tmp_path / "truncated.jsonl"
    f.write_text(
        '{"type":"user","uuid":"u1","message":{"content":"complete line"}}\n'
        '{"type":"user","uuid":"u2","message":{"content":"incomp',
        encoding="utf-8",
    )

    result = extract_conversation(f)
    assert len(result) == 1
    assert result[0]["text"] == "complete line"


def test_extract_conversation_with_bom_does_not_crash(tmp_path):
    """BOM이 파일 시작에 붙어도 raise 안 함. 첫 라인 파싱은 실패 가능."""
    f = tmp_path / "bom.jsonl"
    f.write_bytes(
        b"\xef\xbb\xbf"
        + _good_user_line("u1", "after bom")
        + _good_user_line("u2", "second line")
    )

    # raise 안 함이 핵심 — 결과 길이는 1~2 (BOM 처리 여부에 따라)
    result = extract_conversation(f)
    assert isinstance(result, list)
    # 두 번째 라인은 BOM 없이 정상 utf-8라 무조건 잡혀야 함
    texts = [t["text"] for t in result]
    assert "second line" in texts


# --- extract_partial_conversation (부분) ---

def test_extract_partial_skips_invalid_utf8(tmp_path):
    """partial extract 경로도 동일한 회복력."""
    f = tmp_path / "broken_partial.jsonl"
    f.write_bytes(
        _good_user_line("u1", "first message")
        + b"\x80\xff garbage\n"
        + _good_user_line("u2", "second message")
    )

    conversation, next_cursor, _ = extract_partial_conversation(f, cursor_uuid=None)
    assert len(conversation) == 2
    assert conversation[0]["text"] == "first message"
    assert conversation[1]["text"] == "second message"
    assert next_cursor == "u2"


def test_extract_partial_handles_empty_lines(tmp_path):
    """파일 중간에 빈 줄이 끼어 있어도 무시하고 정상 라인 추출."""
    f = tmp_path / "with_blanks.jsonl"
    f.write_bytes(
        _good_user_line("u1", "before blank")
        + b"\n\n"  # 빈 줄 두 개
        + _good_user_line("u2", "after blank")
    )

    conversation, _, _ = extract_partial_conversation(f, cursor_uuid=None)
    assert len(conversation) == 2
