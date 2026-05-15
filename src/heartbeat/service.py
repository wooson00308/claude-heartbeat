"""OS별 백그라운드 서비스 등록 어댑터.

macOS: launchd (LaunchAgent plist)
Windows: Task Scheduler (schtasks.exe)
Linux: systemd user unit — Phase 3 예정. 현재는 수동 가이드만.

best-effort 자동화. 실패 시 수동 명령을 출력하고 non-zero exit.
print_only=True로 호출하면 실제 OS 등록 없이 명령/파일 내용만 출력.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PLIST_LABEL = "com.claude-heartbeat"
TASK_NAME = "claude-heartbeat"

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{heartbeat_bin}</string>
        <string>start</string>
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


def _heartbeat_bin() -> str | None:
    return shutil.which("heartbeat")


def install_service(print_only: bool = False) -> int:
    """OS 감지하여 자동 등록. 0 = success, non-zero = failure."""
    if sys.platform == "darwin":
        return _install_launchd(print_only)
    if sys.platform == "win32":
        return _install_task_scheduler(print_only)
    if sys.platform.startswith("linux"):
        print("Linux(systemd user unit) 자동 등록은 Phase 3 예정.")
        print("수동 등록 가이드: https://github.com/wooson00308/claude-heartbeat (docs/setup.md)")
        return 1
    print(f"지원하지 않는 플랫폼: {sys.platform}")
    return 1


def uninstall_service() -> int:
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _uninstall_task_scheduler()
    if sys.platform.startswith("linux"):
        print("Linux 자동 해제 미지원 (Phase 3)")
        return 1
    print(f"지원하지 않는 플랫폼: {sys.platform}")
    return 1


# --- macOS / launchd ---

def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _render_launchd_plist() -> str | None:
    bin_path = _heartbeat_bin()
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


def _install_launchd(print_only: bool) -> int:
    plist = _render_launchd_plist()
    if plist is None:
        return 1

    plist_path = _launchd_plist_path()

    if print_only:
        print(f"# {plist_path}")
        print(plist)
        print(f"# Load:\nlaunchctl load {plist_path}")
        return 0

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


def _uninstall_launchd() -> int:
    plist_path = _launchd_plist_path()
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


# --- Windows / Task Scheduler ---

def _task_install_cmd(bin_path: str) -> list[str]:
    return [
        "schtasks.exe", "/create",
        "/tn", TASK_NAME,
        "/tr", f'"{bin_path}" start',
        "/sc", "onlogon",
        "/rl", "limited",
        "/f",  # overwrite if exists
    ]


def _install_task_scheduler(print_only: bool) -> int:
    bin_path = _heartbeat_bin()
    if not bin_path:
        print("⚠ heartbeat CLI를 PATH에서 찾을 수 없음. pip install 후 다시 시도.")
        return 1

    cmd = _task_install_cmd(bin_path)

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


def _uninstall_task_scheduler() -> int:
    cmd = ["schtasks.exe", "/delete", "/tn", TASK_NAME, "/f"]
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
