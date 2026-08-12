"""Windows Task Scheduler 어댑터."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape, unescape

from .base import RestartResult, ServiceAdapter, ServiceStatus

TASK_NAME = "claude-heartbeat"


class TaskSchedulerAdapter(ServiceAdapter):
    name = "task_scheduler"

    def _install_cmd(self, xml_path: str) -> list[str]:
        return [
            "schtasks.exe", "/create",
            "/tn", TASK_NAME,
            "/xml", xml_path,
            "/f",  # overwrite if exists
        ]

    def _task_xml(self, bin_path: str) -> str:
        """Build the Task Scheduler definition with crash recovery enabled.

        ``schtasks /sc onlogon`` alone only starts once at sign-in.  The XML
        form exposes RestartOnFailure, which is the Task Scheduler equivalent
        of launchd KeepAlive and systemd Restart=always.
        """
        command = escape(bin_path)
        return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>agent-dispatcher</Arguments></Exec></Actions>
</Task>
'''

    def _uninstall_cmd(self) -> list[str]:
        return ["schtasks.exe", "/delete", "/tn", TASK_NAME, "/f"]

    def detect(self) -> str | None:
        try:
            result = subprocess.run(
                ["schtasks.exe", "/query", "/tn", TASK_NAME], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return None
        return TASK_NAME if result.returncode == 0 else None

    def _registered_command(self) -> str:
        """등록물이 실행하는 경로. XML 태그 이름은 로케일에 흔들리지 않는다."""
        try:
            queried = subprocess.run(
                ["schtasks.exe", "/query", "/tn", TASK_NAME, "/xml", "ONELINE"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            return ""
        if queried.returncode != 0:
            return ""
        match = re.search(r"<Command>(.*?)</Command>", queried.stdout, re.DOTALL)
        return unescape(match.group(1).strip()) if match else ""

    def inspect(self) -> ServiceStatus:
        try:
            queried = subprocess.run(
                ["schtasks.exe", "/query", "/tn", TASK_NAME], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return ServiceStatus(self.name, "tool_missing", registered=None, running=None,
                                 evidence=("schtasks_query",))
        if queried.returncode != 0:
            if "denied" in (queried.stderr or "").casefold():
                return ServiceStatus(self.name, "permission_denied", registered=None, running=None,
                                     evidence=("schtasks_query",))
            return ServiceStatus(self.name, "not_registered", registered=False, running=False,
                                 evidence=("schtasks_query",))

        command = self._registered_command()
        if not command or not Path(command).is_file():
            return ServiceStatus(self.name, "executable_missing", registered=True, running=None,
                                 label=TASK_NAME, executable=command,
                                 evidence=("schtasks_query", "schtasks_query_xml"))
        # 실행 여부는 돌려주지 않는다. schtasks의 상태 문자열이 로케일에 따라 달라져
        # 계약 값으로 파싱할 수 없고, 모르는 값을 실행 중으로 올리지 않는다.
        return ServiceStatus(
            self.name, "registered", registered=True, running=None,
            label=TASK_NAME, executable=command,
            evidence=("schtasks_query", "schtasks_query_xml"),
            detail={"runningUnknown": "schtasks status output is locale dependent"},
        )

    def restart(self) -> RestartResult:
        """/end 후 /run.

        launchd·systemd와 달리 `not-loaded`를 구분하지 않는다. schtasks의 실행 상태
        문자열이 로케일 의존이라 파싱을 계약에 넣을 수 없다. 안 돌고 있으면 /end가
        실패하는데 그건 무시하고 /run으로 띄운다 — 등록돼 있으면 결과는 항상 "뜬 상태".
        """
        task = self.detect()
        if task is None:
            return RestartResult("skipped", "not-registered")

        try:
            subprocess.run(["schtasks.exe", "/end", "/tn", task], capture_output=True, text=True)
            result = subprocess.run(
                ["schtasks.exe", "/run", "/tn", task], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return RestartResult("failed", "schtasks-missing", task)

        if result.returncode == 0:
            return RestartResult("ok", "restarted", task)
        return RestartResult("failed", "restart-failed", task)

    def render(self) -> str | None:
        """등록할 XML 정의를 반환한다."""
        bin_path = self._heartbeat_bin()
        if not bin_path:
            print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
            return None
        return self._task_xml(bin_path)

    def install(self, print_only: bool = False) -> int:
        bin_path = self._heartbeat_bin()
        if not bin_path:
            print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
            return 1

        definition = self._task_xml(bin_path)

        if print_only:
            print("# Windows Task Scheduler XML 정의 (RestartOnFailure 포함):")
            print(definition)
            print(f"# 등록: schtasks.exe /create /tn {TASK_NAME} /xml <definition.xml> /f")
            return 0

        descriptor, xml_name = tempfile.mkstemp(prefix="claude-heartbeat-", suffix=".xml")
        xml_path = Path(xml_name)
        registered = False
        try:
            with open(descriptor, "w", encoding="utf-16") as definition_file:
                definition_file.write(definition)
            cmd = self._install_cmd(str(xml_path))
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            registered = True
            subprocess.run(
                ["schtasks.exe", "/run", "/tn", TASK_NAME],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"✓ Task Scheduler 등록 완료: {TASK_NAME}")
            return 0
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            if registered:
                try:
                    subprocess.run(self._uninstall_cmd(), capture_output=True, text=True)
                except FileNotFoundError:
                    pass
            stderr = getattr(exc, "stderr", "") or str(exc)
            print(f"⚠ schtasks 등록 실패: {stderr.strip()}")
            print(f"  수동 등록: schtasks.exe /create /tn {TASK_NAME} /xml <definition.xml> /f")
            return 1
        finally:
            xml_path.unlink(missing_ok=True)

    def uninstall(self, print_only: bool = False) -> int:
        cmd = self._uninstall_cmd()

        if print_only:
            print(f"# Uninstall: {' '.join(cmd)}")
            return 0

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"✓ Task Scheduler 해제 완료: {TASK_NAME}")
                return 0
            print(f"  Task Scheduler 해제: {result.stderr.strip() or '잡 없음'}")
            return 0
        except FileNotFoundError:
            print("⚠ schtasks.exe 없음 (Windows 전용)")
            return 1

    def migrate(self) -> int:
        try:
            queried = subprocess.run(
                ["schtasks.exe", "/query", "/tn", TASK_NAME, "/xml"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return 1
        if queried.returncode != 0:
            return self.install()
        original = queried.stdout
        installed = self.install()
        status = self.inspect() if installed == 0 else None
        if installed == 0 and status is not None and status.registered is True:
            return 0
        descriptor, xml_name = tempfile.mkstemp(
            prefix="claude-heartbeat-rollback-", suffix=".xml",
        )
        xml_path = Path(xml_name)
        try:
            with open(descriptor, "w", encoding="utf-16") as definition_file:
                definition_file.write(original)
            subprocess.run(self._install_cmd(str(xml_path)), capture_output=True, text=True)
        finally:
            xml_path.unlink(missing_ok=True)
        print("관리형 서비스 검증 실패. 기존 작업 스케줄러 정의를 복구했습니다.")
        return 1
