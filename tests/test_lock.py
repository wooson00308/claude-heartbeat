"""_lock.py 동시성 검증.

리뷰어 2 HIGH 항목: portalocker 락이 진짜 일하는지 multi-process로 검증.
N개 프로세스가 동시에 mark_processed를 호출해도 항목 손실 없이 N개 다 보존.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pytest

from skills.dream import meta, paths


def _worker(args: tuple[str, str, str]) -> None:
    """별도 프로세스에서 실행. PROJECTS_DIR을 명시적으로 monkeypatch."""
    projects_dir, slug, filename = args
    paths.PROJECTS_DIR = Path(projects_dir)
    meta.mark_processed(slug, filename, f"uuid-{filename}", "sealed")


@pytest.mark.skipif(
    sys.platform == "win32" and mp.get_start_method(allow_none=True) is None,
    reason="윈도우 spawn-only 환경에서 fork 가정한 monkeypatch 전파 차이",
)
def test_concurrent_mark_processed_no_loss(isolated_projects_dir, slug):
    """N개 프로세스가 동시에 마킹해도 항목 N개 모두 dream_meta.md에 보존."""
    project_dir = isolated_projects_dir / slug
    (project_dir / "memory").mkdir(parents=True)

    n = 20
    args = [(str(isolated_projects_dir), slug, f"f{i}.jsonl") for i in range(n)]

    # spawn 방식: 자식 프로세스가 paths를 깨끗이 import 후 _worker가 monkeypatch
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=n, mp_context=ctx) as ex:
        futures = [ex.submit(_worker, a) for a in args]
        for fut in as_completed(futures):
            fut.result()  # raise on error

    parsed = meta.parse_meta_v2(slug)
    assert len(parsed) == n
    for i in range(n):
        assert f"f{i}.jsonl" in parsed
        assert parsed[f"f{i}.jsonl"]["last_uuid"] == f"uuid-f{i}.jsonl"
