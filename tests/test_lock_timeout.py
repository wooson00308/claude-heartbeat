"""dream_meta.md 락 타임아웃 fail-closed 회귀 (issue #11).

v0.6.0: 락 못 잡으면 LockTimeout 예외. mark_processed의 try/except가 잡아서
graceful하게 처리. 이전엔 warning 후 lock 없이 yield (fail-open) — race로
인한 중복 흡수 / 라운드 윈도우 누락 위험.
"""

from __future__ import annotations

import portalocker
import pytest

from skills.dream import _lock as lock_mod
from skills.dream import meta


def _force_lock_failure(monkeypatch):
    """portalocker.lock을 항상 LockException raise하게 + 타임아웃 짧게."""

    def _always_fail(*args, **kwargs):
        raise portalocker.LockException("test: lock unavailable")

    monkeypatch.setattr(portalocker, "lock", _always_fail)
    monkeypatch.setattr(lock_mod, "LOCK_TIMEOUT_SEC", 0.2)


def test_acquire_lock_raises_timeout_when_unavailable(project_dir, slug, monkeypatch):
    """LOCK_TIMEOUT_SEC 안에 못 잡으면 LockTimeout."""
    _force_lock_failure(monkeypatch)

    with pytest.raises(lock_mod.LockTimeout):
        with lock_mod._acquire_meta_lock(slug):
            pass


def test_mark_processed_graceful_on_lock_timeout(project_dir, slug, monkeypatch, caplog):
    """mark_processed는 LockTimeout을 잡아서 logger.warning + return.

    main flow는 안 죽지만 entry는 안 들어감 (차라리 안 박는 게 race보다 안전).
    """
    _force_lock_failure(monkeypatch)

    # raise 안 함
    meta.mark_processed(slug, "x.jsonl", "uuid-x", "sealed")

    # entry는 안 박혔어야 함 (lock 없이 RMW 진행을 막은 게 핵심)
    parsed = meta.parse_meta_v2(slug)
    assert parsed == {}


def test_acquire_lock_succeeds_normal_path(project_dir, slug):
    """일반 경로 — 락 정상 잡힘 + yield."""
    with lock_mod._acquire_meta_lock(slug):
        pass  # 예외 없이 종료
