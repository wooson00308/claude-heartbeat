"""dream_meta.md 동시성 보호 락.

portalocker 기반 cross-platform 파일 락. 윈도우/macOS/Linux 동일 동작.
fcntl 직접 호출은 Phase 2에서 제거됨 (v0.4.0).
"""

from __future__ import annotations

import contextlib
import logging
import time

import portalocker

from .paths import get_project_dir

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_SEC = 30.0


@contextlib.contextmanager
def _acquire_meta_lock(slug: str):
    """Context manager: acquire exclusive lock on .dream.lock file.

    Waits up to LOCK_TIMEOUT_SEC; if lock cannot be acquired, logs and yields
    anyway (fail-open for operational safety).
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
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                portalocker.lock(lock_fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
                acquired = True
                break
            except portalocker.LockException:
                time.sleep(0.1)

        if not acquired:
            logger.warning(
                "[dream-prep] could not acquire lock within %.0f s — proceeding without lock",
                LOCK_TIMEOUT_SEC,
            )

        yield
    finally:
        if acquired:
            try:
                portalocker.unlock(lock_fd)
            except (OSError, portalocker.LockException):
                pass
        try:
            lock_fd.close()
        except OSError:
            pass
