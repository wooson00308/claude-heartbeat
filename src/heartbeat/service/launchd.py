"""macOS launchd LaunchAgent 어댑터."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .base import RestartResult, ServiceAdapter, ServiceStatus

PLIST_LABEL = "com.claude-heartbeat"


def _has_pid(listed: str) -> bool:
    """`launchctl list <label>` 출력에 살아 있는 PID가 있는지.

    로드만 되고 프로세스가 없으면 PID 항목이 빠지거나 0이 온다. 등록 사실만으로
    실행 중이라고 답하지 않기 위해 이 값만 본다.
    """
    for line in listed.splitlines():
        if '"PID"' in line:
            digits = "".join(character for character in line.split("=")[-1] if character.isdigit())
            return bool(digits) and int(digits) > 0
    return False

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{heartbeat_bin}</string>
        <string>agent-dispatcher</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/launchd_stderr.log</string>
    <key>WorkingDirectory</key>
    <string>{home}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:{python_dir}</string>
        <key>HOME</key>
        <string>{home}</string>
    </dict>
</dict>
</plist>
"""


class LaunchdAdapter(ServiceAdapter):
    name = "launchd"

    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

    def _heartbeat_plists(self) -> list[Path]:
        """LaunchAgents에 있는 heartbeat 계열 plist 전부. 표준 라벨이 항상 앞이다.

        코드의 표준 라벨(com.claude-heartbeat)보다 먼저 다른 이름으로 설치된
        서비스가 실기기에 있다(실측: com.catze.dream-heartbeat). 재기동·중복 감지는
        코드가 아는 이름이 아니라 실제로 등록된 파일을 봐야 한다.
        """
        agents = Path.home() / "Library" / "LaunchAgents"
        if not agents.exists():
            return []
        found = sorted(p for p in agents.glob("*.plist") if "heartbeat" in p.stem.lower())
        own = self._plist_path()
        return [p for p in found if p == own] + [p for p in found if p != own]

    def _label_of(self, plist_path: Path) -> str:
        """plist의 Label 값. 못 읽으면 파일 이름으로 대신한다."""
        try:
            with plist_path.open("rb") as fh:
                label = plistlib.load(fh).get("Label")
        except (OSError, plistlib.InvalidFileException, ValueError):
            return plist_path.stem
        return label or plist_path.stem

    def detect(self) -> str | None:
        plists = self._heartbeat_plists()
        return self._label_of(plists[0]) if plists else None

    def _program_of(self, plist_path: Path) -> str:
        """등록물이 실제로 실행하는 경로. 못 읽으면 빈 문자열."""
        try:
            with plist_path.open("rb") as fh:
                arguments = plistlib.load(fh).get("ProgramArguments") or []
        except (OSError, plistlib.InvalidFileException, ValueError):
            return ""
        return arguments[0] if arguments and isinstance(arguments[0], str) else ""

    def _disabled_labels(self) -> set[str]:
        try:
            result = subprocess.run(
                ["launchctl", "print-disabled", f"gui/{os.getuid()}"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return set()
        if result.returncode != 0:
            return set()
        return {
            line.split('"', 2)[1]
            for line in result.stdout.splitlines()
            if '"' in line and ("=> true" in line or "=> disabled" in line)
        }

    def inspect(self) -> ServiceStatus:
        plists = self._heartbeat_plists()
        if not plists:
            return ServiceStatus(self.name, "not_registered", registered=False, running=False,
                                 evidence=("launch_agents_directory",))
        label = self._label_of(plists[0])
        program = self._program_of(plists[0])
        disabled = self._disabled_labels()
        own = self._plist_path()
        foreign = [path for path in plists if path != own]
        if len(plists) > 1 and (
            own not in plists
            or any(self._label_of(path) not in disabled for path in foreign)
        ):
            # 어느 등록물이 도는지 정할 수 없으면 재기동 대상도 정할 수 없다.
            return ServiceStatus(
                self.name, "ambiguous_registration", registered=True, running=None,
                label=label, executable=program, evidence=("launch_agents_directory",),
                detail={"registrations": ", ".join(self._label_of(path) for path in plists)},
            )
        if not program or not Path(program).is_file():
            return ServiceStatus(self.name, "executable_missing", registered=True, running=None,
                                 label=label, executable=program,
                                 evidence=("launch_agents_directory", "program_arguments"))

        try:
            listed = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
        except FileNotFoundError:
            return ServiceStatus(self.name, "tool_missing", registered=True, running=None,
                                 label=label, executable=program,
                                 evidence=("launch_agents_directory", "program_arguments"))
        evidence = ("launch_agents_directory", "program_arguments", "launchctl_list")
        if "not permitted" in (listed.stderr or "").casefold():
            return ServiceStatus(self.name, "permission_denied", registered=True, running=None,
                                 label=label, executable=program, evidence=evidence)
        running = listed.returncode == 0 and _has_pid(listed.stdout)
        return ServiceStatus(self.name, "registered", registered=True, running=running,
                             label=label, executable=program, evidence=evidence)

    def restart(self) -> RestartResult:
        label = self.detect()
        if label is None:
            return RestartResult("skipped", "not-registered")

        try:
            listed = subprocess.run(
                ["launchctl", "list", label], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return RestartResult("failed", "launchctl-missing", label)

        if listed.returncode != 0:
            # plist는 있는데 로드가 안 된 상태. 지금 도는 프로세스가 없으니 재기동할
            # 것도 없고, 다음 로드 때 새 코드로 뜬다.
            return RestartResult("skipped", "not-loaded", label)

        kick = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True,
        )
        if kick.returncode == 0:
            return RestartResult("ok", "restarted", label)
        return RestartResult("failed", "restart-failed", label)

    def render(self) -> str | None:
        bin_path = self._heartbeat_bin()
        if not bin_path:
            print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
            return None

        home = str(Path.home())
        return PLIST_TEMPLATE.format(
            label=PLIST_LABEL,
            heartbeat_bin=bin_path,
            log_dir=f"{home}/.claude/heartbeat",
            home=home,
            python_dir=str(Path(bin_path).parent),
        )

    def install(self, print_only: bool = False) -> int:
        plist = self.render()
        if plist is None:
            return 1

        plist_path = self._plist_path()
        # 다른 이름으로 이미 등록된 heartbeat 서비스가 있으면 새로 얹지 않는다.
        # 얹으면 데몬이 둘 뜨고 같은 잡이 두 번 실행된다.
        disabled = self._disabled_labels()
        foreign = [
            p for p in self._heartbeat_plists()
            if p != plist_path and self._label_of(p) not in disabled
        ]

        if print_only:
            if foreign:
                print(f"# ⚠ 이미 등록된 heartbeat 서비스: {', '.join(self._label_of(p) for p in foreign)}")
                print("# 실제 등록은 거부된다 (데몬 중복 실행 방지). 아래는 참고용 내용.")
            print(f"# {plist_path}")
            print(plist)
            print(f"# Load:\nlaunchctl load {plist_path}")
            return 0

        if foreign:
            labels = ", ".join(self._label_of(p) for p in foreign)
            print(f"⚠ 이미 등록된 heartbeat 서비스가 있음: {labels}")
            print("  그대로 등록하면 데몬이 둘 뜨고 같은 잡이 두 번 실행됩니다.")
            for p in foreign:
                print(f"  기존 해제: launchctl unload {p} && rm {p}")
            print("  해제 후 다시 실행하세요: heartbeat install-service")
            return 1

        plist_path.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{Path.home()}/.claude/heartbeat").mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist, encoding="utf-8")

        try:
            subprocess.run(
                ["launchctl", "load", str(plist_path)],
                check=True, capture_output=True, text=True,
            )
            print(f"✓ launchd 등록 완료: {plist_path}")
            return 0
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            stderr = getattr(exc, "stderr", "") or str(exc)
            print(f"⚠ launchctl load 실패: {stderr.strip()}")
            print(f"  수동 등록: launchctl load {plist_path}")
            return 1

    def migrate(self) -> int:
        """Disable one foreign registration without editing its plist, then verify ours."""
        own = self._plist_path()
        foreign = [path for path in self._heartbeat_plists() if path != own]
        disabled_labels = self._disabled_labels()
        active_foreign = [
            path for path in foreign if self._label_of(path) not in disabled_labels
        ]
        if own.exists():
            if active_foreign:
                print("활성 heartbeat 서비스가 여러 개라 자동 전환하지 않았습니다.")
                return 1
            status = self.inspect()
            expected = self._heartbeat_bin()
            if (
                status.label == PLIST_LABEL
                and status.registered is True
                and status.running is True
                and expected is not None
                and Path(status.executable).resolve() == Path(expected).resolve()
            ):
                return 0
            subprocess.run(
                ["launchctl", "unload", str(own)], capture_output=True, text=True,
            )
            installed = self.install()
            status = self.inspect() if installed == 0 else None
            return int(
                status is None
                or status.label != PLIST_LABEL
                or status.registered is not True
                or status.running is not True
            )
        if not foreign:
            return self.install()
        if len(foreign) != 1:
            print("서비스 등록이 여러 개라 자동 전환하지 않았습니다.")
            return 1
        previous = foreign[0]
        label = self._label_of(previous)
        domain = f"gui/{os.getuid()}"
        was_disabled = label in disabled_labels
        try:
            previous_state = subprocess.run(
                ["launchctl", "list", label], capture_output=True, text=True,
            )
        except FileNotFoundError:
            print("기존 서비스 상태 확인 실패: launchctl을 찾을 수 없습니다.")
            return 1
        was_loaded = previous_state.returncode == 0
        try:
            disabled = subprocess.run(
                ["launchctl", "disable", f"{domain}/{label}"], capture_output=True, text=True,
            )
        except FileNotFoundError:
            print("기존 서비스 비활성화 실패: launchctl을 찾을 수 없습니다.")
            return 1
        if disabled.returncode != 0:
            print(f"기존 서비스 비활성화 실패: {disabled.stderr.strip()}")
            return 1
        subprocess.run(["launchctl", "unload", str(previous)], capture_output=True, text=True)
        installed = self.install()
        status = self.inspect() if installed == 0 else None
        if (
            installed == 0
            and status is not None
            and status.label == PLIST_LABEL
            and status.registered is True
            and status.running is True
        ):
            print(f"✓ 기존 서비스 {label} 보존·비활성화, 관리형 서비스 전환 완료")
            return 0

        self.uninstall()
        if not was_disabled:
            subprocess.run(
                ["launchctl", "enable", f"{domain}/{label}"],
                capture_output=True,
                text=True,
            )
        restored_code = 0
        if was_loaded:
            restored = subprocess.run(
                ["launchctl", "load", str(previous)], capture_output=True, text=True,
            )
            restored_code = restored.returncode
        if status is not None:
            print(
                "관리형 서비스 상태: "
                f"{status.result}, registered={status.registered}, running={status.running}",
            )
        print("관리형 서비스 검증 실패. 기존 서비스를 복구했습니다.")
        return 1 if restored_code == 0 else 2

    def uninstall(self, print_only: bool = False) -> int:
        plist_path = self._plist_path()

        if print_only:
            print(f"# Uninstall: launchctl unload {plist_path}")
            print(f"# Then: rm {plist_path}")
            return 0

        if not plist_path.exists():
            print(f"  {plist_path} 없음 (이미 해제 상태)")
            return 0

        try:
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass

        plist_path.unlink()
        print(f"✓ launchd 해제 완료: {plist_path}")
        return 0
