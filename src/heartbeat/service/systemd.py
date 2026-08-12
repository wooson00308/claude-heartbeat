"""Linux systemd user unit 어댑터.

daemon-reload와 enable --now를 분리해서 어디서 실패했는지 정확히 보고한다.
SSH 세션 등에서 systemctl --user가 깨지는 케이스(DBUS / XDG_RUNTIME_DIR 미설정)도
가이드에 한 줄 명시.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import RestartResult, ServiceAdapter, ServiceStatus

UNIT_NAME = "claude-heartbeat.service"

UNIT_TEMPLATE = """[Unit]
Description=Claude Heartbeat — periodic claude agent scheduler
After=default.target

[Service]
Type=simple
ExecStart={heartbeat_bin} agent-dispatcher
Restart=always
RestartSec=5
StandardOutput=append:{log_dir}/systemd_stdout.log
StandardError=append:{log_dir}/systemd_stderr.log
Environment=PATH={path_env}
Environment=HOME={home}

[Install]
WantedBy=default.target
"""


class SystemdAdapter(ServiceAdapter):
    name = "systemd"

    def _unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME

    def detect(self) -> str | None:
        return UNIT_NAME if self._unit_path().exists() else None

    def _exec_start(self) -> str:
        """unit이 실제로 실행하는 경로. 못 읽으면 빈 문자열."""
        try:
            text = self._unit_path().read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                command = line.split("=", 1)[1].strip()
                return command.split()[0] if command else ""
        return ""

    def inspect(self) -> ServiceStatus:
        unit = self.detect()
        if unit is None:
            return ServiceStatus(self.name, "not_registered", registered=False, running=False,
                                 evidence=("user_unit_directory",))
        program = self._exec_start()
        if not program or not Path(program).is_file():
            return ServiceStatus(self.name, "executable_missing", registered=True, running=None,
                                 label=unit, executable=program,
                                 evidence=("user_unit_directory", "exec_start"))

        try:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", unit], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return ServiceStatus(self.name, "tool_missing", registered=True, running=None,
                                 label=unit, executable=program,
                                 evidence=("user_unit_directory", "exec_start"))
        evidence = ("user_unit_directory", "exec_start", "systemctl_is_active")
        combined = f"{active.stdout}{active.stderr}".casefold()
        if "permission denied" in combined or "failed to connect to bus" in combined:
            # 사용자 버스에 붙지 못하면 실행 여부 자체를 읽을 수 없다.
            return ServiceStatus(self.name, "permission_denied", registered=True, running=None,
                                 label=unit, executable=program, evidence=evidence)
        state = active.stdout.strip()
        running = True if state == "active" else (False if state in {"inactive", "failed", "deactivating"} else None)
        return ServiceStatus(self.name, "registered", registered=True, running=running,
                             label=unit, executable=program, evidence=evidence,
                             detail={"activeState": state} if state else {})

    def restart(self) -> RestartResult:
        unit = self.detect()
        if unit is None:
            return RestartResult("skipped", "not-registered")

        try:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", unit], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return RestartResult("failed", "systemctl-missing", unit)

        if active.returncode != 0:
            # unit은 있는데 지금 안 돌고 있다. 재기동할 프로세스가 없다.
            return RestartResult("skipped", "not-loaded", unit)

        result = subprocess.run(
            ["systemctl", "--user", "restart", unit], capture_output=True, text=True,
        )
        if result.returncode == 0:
            return RestartResult("ok", "restarted", unit)
        return RestartResult("failed", "restart-failed", unit)

    def render(self) -> str | None:
        bin_path = self._heartbeat_bin()
        if not bin_path:
            print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
            return None

        home = str(Path.home())
        bin_dir = str(Path(bin_path).parent)
        path_env = f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"

        return UNIT_TEMPLATE.format(
            heartbeat_bin=bin_path,
            log_dir=f"{home}/.claude/heartbeat",
            path_env=path_env,
            home=home,
        )

    def install(self, print_only: bool = False) -> int:
        unit = self.render()
        if unit is None:
            return 1

        unit_path = self._unit_path()

        if print_only:
            print(f"# {unit_path}")
            print(unit)
            print("# Install commands:")
            print("systemctl --user daemon-reload")
            print(f"systemctl --user enable --now {UNIT_NAME}")
            print("# 확인:")
            print(f"systemctl --user status {UNIT_NAME}")
            print("# (선택) 로그아웃 후에도 돌리려면:")
            print("loginctl enable-linger $USER")
            print("# 주의: SSH 세션 등에서 DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR")
            print("#       미설정 시 systemctl --user 자체가 실패할 수 있음")
            return 0

        unit_path.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{Path.home()}/.claude/heartbeat").mkdir(parents=True, exist_ok=True)

        try:
            unit_path.write_text(unit, encoding="utf-8")
        except (OSError, PermissionError) as exc:
            print(f"⚠ unit 파일 쓰기 실패: {exc}")
            print(f"  대상: {unit_path}")
            print("  ~/.config 권한을 확인하거나 수동으로 unit 파일을 작성.")
            return 1

        # daemon-reload 단계
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            print("⚠ systemctl 명령을 찾을 수 없음 (systemd가 없는 환경?)")
            print(f"  unit 파일은 등록됨: {unit_path}")
            print(f"  수동: systemctl --user daemon-reload && systemctl --user enable --now {UNIT_NAME}")
            return 1
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            print(f"⚠ systemctl daemon-reload 실패: {stderr}")
            print(f"  unit 파일은 등록됨: {unit_path}")
            return 1

        # enable --now 단계 (이 단계에서 실패하면 unit은 살아있고 enable만 누락)
        try:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", UNIT_NAME],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            print(f"⚠ systemctl enable 실패: {stderr}")
            print(f"  unit 파일은 등록됨: {unit_path}")
            print(f"  enable만 다시 시도: systemctl --user enable --now {UNIT_NAME}")
            print(f"  상태 확인: systemctl --user status {UNIT_NAME}")
            print("  SSH 세션에서 DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR 미설정 시 실패 가능.")
            return 1

        print(f"✓ systemd user unit 등록 완료: {unit_path}")
        print(f"  확인: systemctl --user status {UNIT_NAME}")
        print("  로그아웃 후에도 돌리려면: loginctl enable-linger $USER")
        return 0

    def uninstall(self, print_only: bool = False) -> int:
        unit_path = self._unit_path()

        if print_only:
            print(f"# Uninstall: systemctl --user disable --now {UNIT_NAME}")
            print(f"# Then: rm {unit_path}")
            print("# Then: systemctl --user daemon-reload")
            return 0

        if not unit_path.exists():
            print(f"  {unit_path} 없음 (이미 해제 상태)")
            return 0

        # systemctl이 없는 환경(다른 머신으로 dotfile 옮긴 케이스)에서도 unit 파일은 정리.
        try:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", UNIT_NAME],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass

        unit_path.unlink()

        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass

        print(f"✓ systemd user unit 해제 완료: {unit_path}")
        return 0

    def migrate(self) -> int:
        unit_path = self._unit_path()
        if not unit_path.exists():
            return self.install()
        try:
            original = unit_path.read_bytes()
        except OSError as error:
            print(f"기존 unit 백업 실패: {error}")
            return 1
        try:
            stopped = subprocess.run(
                ["systemctl", "--user", "disable", "--now", UNIT_NAME],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            print("기존 unit 중지 실패: systemctl을 찾을 수 없습니다.")
            return 1
        if stopped.returncode != 0:
            print(f"기존 unit 중지 실패: {stopped.stderr.strip()}")
            return 1
        installed = self.install()
        status = self.inspect() if installed == 0 else None
        if installed == 0 and status is not None and status.registered is True and status.running is True:
            return 0
        unit_path.write_bytes(original)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", UNIT_NAME],
            capture_output=True,
            text=True,
        )
        print("관리형 서비스 검증 실패. 기존 unit을 복구했습니다.")
        return 1
