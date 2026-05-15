"""ServiceAdapter — OS별 백그라운드 서비스 등록 어댑터의 공통 인터페이스.

각 OS 어댑터는 이 클래스를 상속해서 render / install / uninstall만 구현하면 된다.
새 어댑터 추가는 ADAPTERS dict에 한 줄 등록 (linux는 startswith라 별도 분기).
"""

from __future__ import annotations

import shutil


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
