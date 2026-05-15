"""dream-prep CLI의 check-unprocessed 명령 검증.

heartbeat condition으로 사용되는 셸-의존 0의 exit-code 게이트.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


def _make_inactive_jsonl(path: Path) -> None:
    """sealed-candidate로 분류되도록 충분히 오래된 small jsonl 생성."""
    path.write_text(
        json.dumps({"type": "user", "uuid": "u1", "message": {"content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    old = time.time() - (60 * 60)  # 1시간 전 (ACTIVE_MTIME_QUIET_SEC = 30분 초과)
    os.utime(path, (old, old))


def _invoke_check(slug: str) -> int:
    """argv 세팅 + main 호출. SystemExit code 반환."""
    from skills.dream.cli import main

    saved = sys.argv
    # `--slug=...` 형식: 슬러그가 "-"로 시작하면 별도 인자로 주면 옵션으로 오인됨
    sys.argv = ["dream-prep", "check-unprocessed", f"--slug={slug}"]
    try:
        with pytest.raises(SystemExit) as exc:
            main()
        return exc.value.code if isinstance(exc.value.code, int) else 0
    finally:
        sys.argv = saved


def test_check_unprocessed_exit0_when_unprocessed_exists(isolated_projects_dir):
    slug = "-test-slug"
    project_dir = isolated_projects_dir / slug
    project_dir.mkdir(parents=True)
    _make_inactive_jsonl(project_dir / "session.jsonl")

    assert _invoke_check(slug) == 0


def test_check_unprocessed_exit1_when_none(isolated_projects_dir):
    slug = "-empty-slug"
    project_dir = isolated_projects_dir / slug
    project_dir.mkdir(parents=True)
    # jsonl 자체 없음 → 미처리 0

    assert _invoke_check(slug) == 1
