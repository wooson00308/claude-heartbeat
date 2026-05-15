"""find_unprocessed_transcripts 회귀 방지.

이슈 #9: active 마킹된 파일이 영영 안 잡히던 버그. 다음 시나리오를 잠근다:
- legacy 마킹 → skip
- v2 sealed 마킹 → skip
- v2 active 마킹 → 다음 라운드에서 잡혀야 함 (cursor부터 이어 처리)
- 마킹 없음 (신규) → 잡힘
- classify gate 미통과 (active 작은 파일) → 안 잡힘
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from skills.dream import meta, window
from skills.dream.window import find_unprocessed_transcripts


def _make_old_jsonl(path: Path) -> None:
    """sealed-candidate로 분류되도록 충분히 오래된 small jsonl."""
    path.write_text('{"type":"user","uuid":"u1","message":{"content":"hi"}}\n', encoding="utf-8")
    old = time.time() - (window.ACTIVE_MTIME_QUIET_SEC + 60)
    os.utime(path, (old, old))


def _make_huge_jsonl(path: Path) -> None:
    """huge active 파일 — mtime gate bypass 대상."""
    path.write_bytes(b"x" * (window.HUGE_FILE_SIZE_BYTES + 1024))


def _make_active_small_jsonl(path: Path) -> None:
    """작고 최근 — gate가 막아야 함."""
    path.write_text("{}\n", encoding="utf-8")


def test_unprocessed_includes_new_inactive_file(project_dir, slug):
    _make_old_jsonl(project_dir / "new.jsonl")

    result = find_unprocessed_transcripts(slug)
    assert [p.name for p in result] == ["new.jsonl"]


def test_unprocessed_excludes_legacy_marked(project_dir, slug):
    _make_old_jsonl(project_dir / "old.jsonl")
    (project_dir / "memory" / "dream_meta.md").write_text(
        "processed:\n- old.jsonl\n", encoding="utf-8",
    )

    result = find_unprocessed_transcripts(slug)
    assert result == []


def test_unprocessed_excludes_v2_sealed(project_dir, slug):
    _make_old_jsonl(project_dir / "done.jsonl")
    meta.mark_processed(slug, "done.jsonl", "u1", "sealed")

    result = find_unprocessed_transcripts(slug)
    assert result == []


def test_unprocessed_INCLUDES_v2_active_huge_for_next_round(project_dir, slug):
    """이슈 #9 핵심 회귀: active 마킹된 huge 파일은 다음 라운드에서 다시 잡혀야 한다.

    이전엔 v2 entry가 있다는 이유로 영영 스킵됨. cursor 이어쓰기가 망가짐.
    """
    _make_huge_jsonl(project_dir / "active.jsonl")
    meta.mark_processed(slug, "active.jsonl", "cursor-uuid", "active")

    result = find_unprocessed_transcripts(slug)
    assert [p.name for p in result] == ["active.jsonl"]


def test_unprocessed_excludes_active_small_via_gate(project_dir, slug):
    """active로 마킹돼있어도 작고 최근 파일은 classify gate가 막는다.

    Claude가 아직 쓰는 중일 수 있어서 read-during-write 위험."""
    _make_active_small_jsonl(project_dir / "small_active.jsonl")
    meta.mark_processed(slug, "small_active.jsonl", "cursor-uuid", "active")

    result = find_unprocessed_transcripts(slug)
    assert result == []
