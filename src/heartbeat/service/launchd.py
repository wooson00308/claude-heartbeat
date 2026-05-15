"""macOS launchd LaunchAgent 어댑터."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import ServiceAdapter

PLIST_LABEL = "com.claude-heartbeat"

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


class LaunchdAdapter(ServiceAdapter):
    name = "launchd"

    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

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
