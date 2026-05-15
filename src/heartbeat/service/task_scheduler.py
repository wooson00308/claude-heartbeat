"""Windows Task Scheduler 어댑터."""

from __future__ import annotations

import subprocess

from .base import ServiceAdapter

TASK_NAME = "claude-heartbeat"


class TaskSchedulerAdapter(ServiceAdapter):
    name = "task_scheduler"

    def _install_cmd(self, bin_path: str) -> list[str]:
        return [
            "schtasks.exe", "/create",
            "/tn", TASK_NAME,
            "/tr", f'"{bin_path}" start',
            "/sc", "onlogon",
            "/rl", "limited",
            "/f",  # overwrite if exists
        ]

    def _uninstall_cmd(self) -> list[str]:
        return ["schtasks.exe", "/delete", "/tn", TASK_NAME, "/f"]

    def render(self) -> str | None:
        """등록 명령을 한 줄 문자열로 반환. (Task Scheduler는 파일 정의가 아니라 명령)."""
        bin_path = self._heartbeat_bin()
        if not bin_path:
            print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
            return None
        return " ".join(self._install_cmd(bin_path))

    def install(self, print_only: bool = False) -> int:
        bin_path = self._heartbeat_bin()
        if not bin_path:
            print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
            return 1

        cmd = self._install_cmd(bin_path)

        if print_only:
            print("# Windows Task Scheduler 등록 명령:")
            print(" ".join(cmd))
            return 0

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            print(f"✓ Task Scheduler 등록 완료: {TASK_NAME}")
            return 0
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            stderr = getattr(exc, "stderr", "") or str(exc)
            print(f"⚠ schtasks 등록 실패: {stderr.strip()}")
            print(f"  수동 등록: {' '.join(cmd)}")
            return 1

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
