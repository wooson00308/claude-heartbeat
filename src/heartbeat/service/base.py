"""ServiceAdapter — OS별 백그라운드 서비스 등록 어댑터의 공통 인터페이스.

각 OS 어댑터는 이 클래스를 상속해서 render / install / uninstall / detect / restart만
구현하면 된다. 새 어댑터 추가는 ADAPTERS dict에 한 줄 등록 (linux는 startswith라 별도 분기).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass
class RestartResult:
    """재기동 시도의 결과. `heartbeat update`의 service 단계 출력이 이걸 그대로 싣는다.

    status: ok | skipped | failed
    detail: 닫힌 어휘 토큰 (목록은 docs/config-contract.md에 고정)
    label: OS 스케줄러가 아는 이름(launchd label / systemd unit / task name). 모르면 빈 문자열.
    """

    status: str
    detail: str
    label: str = ""


class ServiceAdapter:
    """OS별 서비스 등록 어댑터의 추상 베이스."""

    name: str = ""

    def _heartbeat_bin(self) -> str | None:
        """heartbeat CLI 경로. PATH에서 찾을 수 없으면 None."""
        return shutil.which("heartbeat")

    def render(self) -> str | None:
        """서비스 정의 본문(plist / 등록 명령 / unit)을 문자열로 반환.

        bin 미발견 같은 사전 조건 실패 시 None + 가이드 print.
        """
        raise NotImplementedError

    def install(self, print_only: bool = False) -> int:
        """등록. 0 = success, non-zero = failure (가이드 출력 포함)."""
        raise NotImplementedError

    def uninstall(self, print_only: bool = False) -> int:
        """해제. 0 = success."""
        raise NotImplementedError

    def detect(self) -> str | None:
        """이 머신에 실제로 등록된 heartbeat 서비스 이름. 미등록이면 None.

        코드가 아는 표준 이름과 다를 수 있다 — 실기기에 예전 이름으로 등록된
        서비스가 있으면 그 이름을 돌려준다. 재기동은 이 이름을 대상으로 한다.
        """
        raise NotImplementedError

    def restart(self) -> RestartResult:
        """등록된 서비스를 재기동한다. 등록/기동 상태에 따라 skipped일 수 있다."""
        raise NotImplementedError
