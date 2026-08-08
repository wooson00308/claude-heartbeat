"""ServiceAdapter — OS별 백그라운드 서비스 등록 어댑터의 공통 인터페이스.

각 OS 어댑터는 이 클래스를 상속해서 render / install / uninstall / detect / restart만
구현하면 된다. 새 어댑터 추가는 ADAPTERS dict에 한 줄 등록 (linux는 startswith라 별도 분기).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime

# inspect()가 돌려주는 닫힌 결과 어휘. 세 어댑터가 같은 값을 같은 뜻으로 쓴다.
SERVICE_RESULTS = (
    "registered",
    "not_registered",
    "executable_missing",
    "ambiguous_registration",
    "permission_denied",
    "tool_missing",
    "unsupported_platform",
)

# 등록이 확인됐고 실행 파일이 있을 때만 복구 가능이다. 확인 자체를 못 한 경우는
# 가능·불가능 어느 쪽으로도 답하지 않는다.
_RECOVERABLE = {
    "registered": True,
    "not_registered": False,
    "executable_missing": False,
    "ambiguous_registration": False,
    "permission_denied": None,
    "tool_missing": None,
    "unsupported_platform": None,
}


def checked_at() -> str:
    """조회 시각. 응답의 사실이 언제 읽힌 값인지 알리는 용도다."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ServiceStatus:
    """OS에 실제로 등록된 서비스를 읽기만 해서 만든 구조화된 상태.

    `registered`와 `running`은 세 값을 가진다. None은 확인하지 못했다는 뜻이며,
    확인하지 못한 값은 어떤 경우에도 실행 중으로 올라가지 않는다. `evidence`는 그
    판정이 무엇을 보고 내려졌는지를 남긴다. 플랫폼 고유 이름과 명령 출력은
    `detail`에만 두고 계약 필드에는 넣지 않는다.
    """

    platform: str
    result: str
    registered: bool | None = None
    running: bool | None = None
    label: str = ""
    executable: str = ""
    evidence: tuple[str, ...] = ()
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def recoverable(self) -> bool | None:
        return _RECOVERABLE[self.result]

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "result": self.result,
            "registered": self.registered,
            "running": self.running,
            "label": self.label,
            "executable": self.executable,
            "recoverable": self.recoverable,
            "checkedAt": checked_at(),
            "evidence": list(self.evidence),
            "detail": dict(self.detail),
        }


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
        """Stable launcher 우선의 heartbeat CLI 경로.

        앱이 검증한 독립 실행형 런타임을 활성화하면 이 환경 변수에 stable
        launcher를 넣는다. 서비스 정의가 version 디렉터리를 직접 물지 않아 업데이트
        실패 때 현재 실행본을 보존하고, 정상 전환 뒤에는 같은 고정 경로가 새 버전을
        실행한다. 기존 pip 설치는 PATH 탐색으로 그대로 동작한다.
        """
        stable_launcher = os.environ.get("HEARTBEAT_RUNTIME_LAUNCHER")
        if stable_launcher:
            candidate = os.path.abspath(stable_launcher)
            if os.path.isfile(candidate):
                return candidate
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

    def inspect(self) -> ServiceStatus:
        """등록·실행 상태를 구조화해 반환한다. 아무것도 쓰지 않는다."""
        raise NotImplementedError
