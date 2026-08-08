"""OS별 백그라운드 서비스 등록 어댑터.

dispatch는 sys.platform 기준. 새 어댑터 추가는 ADAPTERS 매핑 한 줄.
linux는 startswith 매칭이라 별도 분기.
"""

from __future__ import annotations

import sys

from .base import RestartResult, ServiceAdapter, ServiceStatus
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


def restart_service() -> RestartResult:
    """OS 감지하여 등록된 서비스를 재기동. `heartbeat update`의 service 단계."""
    adapter = _get_adapter()
    if adapter is None:
        return RestartResult("failed", "unsupported-platform")
    return adapter.restart()


def inspect_service() -> ServiceStatus:
    """등록·실행 상태를 구조화해 반환한다. 아무것도 쓰지 않는다.

    어댑터가 없는 플랫폼도 같은 모양으로 답한다. 앱이 플랫폼마다 다른 응답을
    구분할 필요가 없어야 한다.
    """
    adapter = _get_adapter()
    if adapter is None:
        return ServiceStatus(sys.platform, "unsupported_platform", evidence=("platform_dispatch",))
    return adapter.inspect()


def detect_service() -> str | None:
    """이 머신에 등록된 heartbeat 서비스 이름. 미등록이면 None."""
    adapter = _get_adapter()
    return adapter.detect() if adapter is not None else None


__all__ = [
    "ADAPTERS",
    "LaunchdAdapter",
    "RestartResult",
    "ServiceAdapter",
    "ServiceStatus",
    "SystemdAdapter",
    "TaskSchedulerAdapter",
    "detect_service",
    "inspect_service",
    "install_service",
    "restart_service",
    "uninstall_service",
]
