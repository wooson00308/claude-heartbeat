"""dream_meta.md atomic write 실패 시 잔여물 정리 회귀.

Phase 1 리뷰 미반영 항목: tempfile.mkstemp + os.replace 흐름에서 도중 실패해도
`.dream_meta_*.tmp` 파일이 디렉토리에 남으면 안 된다.
"""

from __future__ import annotations

import os

import pytest

from skills.dream import meta
from skills.dream.meta import INITIAL_META_TEMPLATE


def _bootstrap_meta(project_dir):
    """정규 포맷 dream_meta.md 초기 생성."""
    meta_path = project_dir / "memory" / "dream_meta.md"
    meta_path.write_text(INITIAL_META_TEMPLATE, encoding="utf-8")
    return meta_path


def test_atomic_write_failure_at_replace_cleans_up_tmp(project_dir, monkeypatch):
    """os.replace에서 실패 → tmp 파일 unlink → 잔여물 0."""
    meta_path = _bootstrap_meta(project_dir)

    def _fail_replace(*args, **kwargs):
        raise OSError("simulated: replace failed (disk full / permission)")

    monkeypatch.setattr(os, "replace", _fail_replace)

    with pytest.raises(RuntimeError, match="atomic write failed"):
        meta._mark_processed_locked(meta_path, "x.jsonl", "u1", "sealed")

    leftover = list(meta_path.parent.glob(".dream_meta_*.tmp"))
    assert leftover == [], f"unexpected tmp leftover: {leftover}"


def test_atomic_write_failure_at_write_cleans_up_tmp(project_dir, monkeypatch):
    """os.fdopen(fd, 'w').write에서 실패 → tmp 파일 unlink → 잔여물 0."""
    meta_path = _bootstrap_meta(project_dir)

    real_fdopen = os.fdopen

    class _BombFile:
        def __init__(self, fd, mode, encoding=None):
            # fd는 열어두지만 write가 터지게
            self._underlying = real_fdopen(fd, mode, encoding=encoding)

        def write(self, *args, **kwargs):
            raise OSError("simulated: write failed (out of space)")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            try:
                self._underlying.close()
            except Exception:
                pass
            return False

    monkeypatch.setattr(os, "fdopen", _BombFile)

    with pytest.raises(RuntimeError, match="atomic write failed"):
        meta._mark_processed_locked(meta_path, "y.jsonl", "u2", "sealed")

    leftover = list(meta_path.parent.glob(".dream_meta_*.tmp"))
    assert leftover == [], f"unexpected tmp leftover: {leftover}"


def test_mark_processed_swallows_atomic_failure(project_dir, monkeypatch):
    """mark_processed 진입점은 RuntimeError를 잡고 logger.warning + return."""
    _bootstrap_meta(project_dir)

    def _fail_replace(*args, **kwargs):
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", _fail_replace)

    # raise 안 함 — mark_processed 내부 except가 잡는다
    meta.mark_processed("-test-project", "z.jsonl", "u3", "sealed")

    # entry는 안 박혔어야 함 (write 실패라 변경 안 됨)
    assert meta.parse_meta_v2("-test-project") == {}
