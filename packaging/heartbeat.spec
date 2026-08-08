"""PyInstaller one-folder specification for the standalone Heartbeat runtime.

Run with ``pyinstaller packaging/heartbeat.spec --noconfirm --clean``.  The
release workflow writes ``runtime-manifest.json`` after COLLECT finishes so the
manifest hashes the exact files users receive.
"""

from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
src = ROOT / "src"
skills = ROOT / "skills"
target_arch = os.environ.get("HEARTBEAT_MACOS_TARGET_ARCH") if sys.platform == "darwin" else None

analysis = Analysis(
    [str(src / "heartbeat" / "cli.py")],
    pathex=[str(src)],
    binaries=[],
    datas=[(str(skills), "skills")],
    hiddenimports=collect_submodules("heartbeat"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["anthropic", "claude_agent_sdk", "codex"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    name="heartbeat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    exclude_binaries=True,
    target_arch=target_arch,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=True,
    name="heartbeat",
)
