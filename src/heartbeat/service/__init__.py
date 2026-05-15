"""OS별 백그라운드 서비스 등록 어댑터.

dispatch는 sys.platform 기준. 새 어댑터 추가는 ADAPTERS 매핑 한 줄.
linux는 startswith 매칭이라 별도 분기.
"""

from __future__ import annotations

import sys

from .base import ServiceAdapter
from .launchd import LaunchdAdapter
from .systemd import SystemdAdapter
from .task_scheduler import TaskSchedulerAdapter

ADAPTERS: dict[str, type[ServiceAdapter]] = {
    "darwin": LaunchdAdapter,
    "win32": TaskSchedulerAdapter,
}


def _get_adapter() -> ServiceAdapter | None:
    if sys.platform.startswith("linux"):
        return SystemdAdapter()
    cls = ADAPTERS.get(sys.platform)
    return cls() if cls else None


def install_service(print_only: bool = False) -> int:
    """OS 감지하여 자동 등록. 0 = success, non-zero = failure."""
    adapter = _get_adapter()
    if adapter is None:
        print(f"지원하지 않는 플랫폼: {sys.platform}")
        return 1
    return adapter.install(print_only)


def uninstall_service(print_only: bool = False) -> int:
    """OS 감지하여 자동 해제."""
    adapter = _get_adapter()
    if adapter is None:
        print(f"지원하지 않는 플랫폼: {sys.platform}")
        return 1
    return adapter.uninstall(print_only)


__all__ = [
    "ADAPTERS",
    "LaunchdAdapter",
    "ServiceAdapter",
    "SystemdAdapter",
    "TaskSchedulerAdapter",
    "install_service",
    "uninstall_service",
]
