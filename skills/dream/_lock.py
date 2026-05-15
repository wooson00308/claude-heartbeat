"""dream_meta.md 동시성 보호 락.

portalocker 기반 cross-platform 파일 락. 윈도우/macOS/Linux 동일 동작.

v0.6.0: 락 타임아웃 시 fail-closed (이전엔 warning 후 lock 없이 yield).
LockTimeout 예외를 raise하고, 호출처(mark_processed)의 try/except가 잡아서
logger.warning + return 처리. dream_meta.md cursor state의 race로 인한
중복 흡수 / 라운드 윈도우 누락 위험 차단.
"""

from __future__ import annotations

import contextlib
import logging
import time

import portalocker

from .paths import get_project_dir

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_SEC = 30.0


class LockTimeout(RuntimeError):
    """LOCK_TIMEOUT_SEC 안에 dream_meta.md 락을 잡지 못함."""


@contextlib.contextmanager
def _acquire_meta_lock(slug: str):
    """Context manager: acquire exclusive lock on .dream.lock file.

    Waits up to LOCK_TIMEOUT_SEC. 못 잡으면 LockTimeout raise (fail-closed).
    lock 파일 자체를 못 열면 fail-open으로 yield (lock 디렉토리 권한 등은
    main flow를 죽일 만한 사고는 아니라고 판단).
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
            raise LockTimeout(
                f"could not acquire dream meta lock within {LOCK_TIMEOUT_SEC:.0f}s"
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
