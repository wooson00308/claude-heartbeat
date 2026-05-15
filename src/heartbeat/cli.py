"""Heartbeat CLI — install skills, manage daemon, run jobs."""

import shutil
import sys
from pathlib import Path

from heartbeat.core import (
    _is_running,
    _load_state,
    _remove_pid,
    _setup_log_file,
    _write_pid,
    heartbeat_loop,
    parse_heartbeat_md,
    run_job,
    LOG_DIR,
)
from heartbeat.service import install_service, uninstall_service

HEARTBEAT_FILE = Path.home() / ".claude" / "HEARTBEAT.md"
SKILLS_DIR = Path.home() / ".claude" / "skills"


def _get_package_skills_dir() -> Path:
    """Get the skills/ directory.

    skills는 pyproject.toml에서 top-level 패키지로 install된다
    (`packages = ["src/heartbeat", "skills"]`). editable / wheel / zipapp 모두
    `import skills`로 정확한 위치를 받는다. 이전엔 `Path(__file__).parent / "skills"`
    fallback이 site-packages/heartbeat/skills를 가리켜서 wheel install 시
    "사용 가능한 스킬 없음"이 나오는 회귀가 있었다 (issue #7).
    """
    import skills
    return Path(skills.__file__).resolve().parent


def _list_available_skills() -> list[str]:
    """List skill names available in the package."""
    skills_dir = _get_package_skills_dir()
    if not skills_dir.exists():
        return []
    return [d.name for d in sorted(skills_dir.iterdir()) if d.is_dir() and (d / "SKILL.md").exists()]


def _detect_slugs() -> list[str]:
    """Detect available project slugs from ~/.claude/projects/."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []
    return [d.name for d in sorted(projects_dir.iterdir()) if d.is_dir() and list(d.glob("*.jsonl"))]


def _slug_short(slug: str) -> str:
    """Extract short name from slug. -Users-catze-Git-GMLM → gmlm"""
    parts = slug.strip("-").split("-")
    return parts[-1].lower() if parts else slug


def cmd_install(args) -> None:
    """Install a skill: copy SKILL.md + register heartbeat job."""
    skill_name = args.skill
    skills_dir = _get_package_skills_dir()
    skill_dir = skills_dir / skill_name

    if not skill_dir.exists() or not (skill_dir / "SKILL.md").exists():
        available = _list_available_skills()
        print(f"스킬 '{skill_name}'을 찾을 수 없음")
        if available:
            print(f"사용 가능한 스킬: {', '.join(available)}")
        sys.exit(1)

    # 1. Copy SKILL.md
    dest_dir = SKILLS_DIR / skill_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_dir / "SKILL.md", dest_dir / "SKILL.md")
    print(f"✓ {skill_dir / 'SKILL.md'} → {dest_dir / 'SKILL.md'}")

    # 2. Register heartbeat job(s)
    template_path = skill_dir / "heartbeat.md"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")

        # Detect slugs or use provided one
        if args.slug:
            slugs = [args.slug]
        else:
            slugs = _detect_slugs()
            if not slugs:
                print("⚠ 프로젝트 slug를 자동 감지할 수 없음. --slug 옵션으로 지정하세요.")
                return

        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)

        existing = HEARTBEAT_FILE.read_text(encoding="utf-8") if HEARTBEAT_FILE.exists() else ""

        added = []
        for slug in slugs:
            short = _slug_short(slug)
            job_block = template.replace("{slug}", slug).replace("{slug_short}", short)
            job_name = f"{skill_name}-{short}"

            # Skip if already registered
            if f"## {job_name}" in existing:
                print(f"  [{job_name}] 이미 등록됨, 스킵")
                continue

            existing = existing.rstrip() + "\n\n" + job_block
            added.append(job_name)

        if added:
            # Ensure header
            if not existing.strip().startswith("# HEARTBEAT"):
                existing = "# HEARTBEAT\n\n" + existing.lstrip()
            HEARTBEAT_FILE.write_text(existing, encoding="utf-8")
            for name in added:
                print(f"✓ HEARTBEAT.md에 [{name}] 등록")
        else:
            print("  추가할 잡 없음")
    else:
        print(f"  (heartbeat.md 템플릿 없음, 잡 등록 생략)")

    # 3. Install CLI dependencies
    extras = {"dream": "dream-prep"}
    if skill_name in extras:
        cmd = extras[skill_name]
        if shutil.which(cmd):
            print(f"✓ {cmd} CLI 이미 설치됨")
        else:
            print(f"  {cmd} CLI 설치 중...")
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", f"claude-heartbeat[{skill_name}]"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"✓ {cmd} CLI 설치 완료")
            else:
                print(f"⚠ {cmd} CLI 설치 실패. 수동으로 실행하세요: pip install claude-heartbeat[{skill_name}]")

    print(f"\n'{skill_name}' 스킬 설치 완료")


def cmd_init(_args) -> None:
    """Initialize heartbeat: create HEARTBEAT.md if missing."""
    if HEARTBEAT_FILE.exists():
        print(f"✓ {HEARTBEAT_FILE} 이미 존재")
    else:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text("# HEARTBEAT\n\n", encoding="utf-8")
        print(f"✓ {HEARTBEAT_FILE} 생성")

    # Check launchd
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_files = list(plist_dir.glob("*heartbeat*")) if plist_dir.exists() else []
    if plist_files:
        print(f"✓ launchd plist 감지: {plist_files[0].name}")
    else:
        print(f"  launchd 미등록. 상세 설정은 docs/setup.md를 참고하세요.")

    print("\n초기화 완료. heartbeat install <skill> 로 스킬을 설치하세요.")
    available = _list_available_skills()
    if available:
        print(f"사용 가능한 스킬: {', '.join(available)}")


def cmd_skills(_args) -> None:
    """List available skills."""
    available = _list_available_skills()
    if not available:
        print("사용 가능한 스킬 없음")
        return

    # Check which are installed
    for name in available:
        installed = (SKILLS_DIR / name / "SKILL.md").exists()
        marker = "✓" if installed else " "
        skill_dir = _get_package_skills_dir() / name
        readme = skill_dir / "README.md"
        desc = ""
        if readme.exists():
            first_line = readme.read_text(encoding="utf-8").split("\n")[0]
            desc = first_line.lstrip("# ").strip()
        print(f"  [{marker}] {name} — {desc}")


def main() -> None:
    import argparse
    import os
    import signal

    parser = argparse.ArgumentParser(
        prog="heartbeat",
        description="Heartbeat — periodic claude agent scheduler"
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialize heartbeat (create HEARTBEAT.md)")

    # start
    p_start = sub.add_parser("start", help="Start heartbeat (foreground)")
    p_start.add_argument(
        "--foreground", "-f",
        action="store_true",
        help="(deprecated since v0.4.0) start는 항상 foreground 동작. 인자만 유지.",
    )

    # stop
    sub.add_parser("stop", help="Stop heartbeat daemon")

    # status
    sub.add_parser("status", help="Check heartbeat status")

    # once
    p_once = sub.add_parser("once", help="Run all jobs once and exit")
    p_once.add_argument("--job", "-j", help="Run specific job by name")

    # jobs
    sub.add_parser("jobs", help="List configured jobs from HEARTBEAT.md")

    # skills
    sub.add_parser("skills", help="List available skills")

    # install
    p_install = sub.add_parser("install", help="Install a skill")
    p_install.add_argument("skill", help="Skill name to install")
    p_install.add_argument("--slug", "-s", help="Project slug (auto-detected if omitted)")

    # install-service / uninstall-service (OS background scheduler 등록)
    p_install_service = sub.add_parser(
        "install-service",
        help="Register heartbeat with the OS background scheduler (launchd / Task Scheduler / systemd)",
    )
    p_install_service.add_argument(
        "--print-only", action="store_true",
        help="실제 등록 없이 등록 명령/파일 내용만 출력",
    )
    p_uninstall_service = sub.add_parser(
        "uninstall-service",
        help="Remove heartbeat from the OS background scheduler",
    )
    p_uninstall_service.add_argument(
        "--print-only", action="store_true",
        help="실제 해제 없이 해제 명령만 출력",
    )

    args = parser.parse_args()

    if args.command == "start":
        existing = _is_running()
        if existing:
            print(f"Heartbeat 이미 실행 중 (PID {existing})")
            sys.exit(1)

        # v0.4.0: 데몬 detach(os.fork/os.setsid) 제거. 항상 foreground 동작.
        # 백그라운드 실행은 OS 스케줄러(launchd / systemd / Task Scheduler)에
        # 위임한다. 윈도우는 fork 자체가 없어 cross-platform 일관성을 위해
        # detach 모드를 일괄 제거. --foreground 플래그는 호환을 위해 인자만
        # 유지하며 동작상 noop.
        _setup_log_file()
        _write_pid()

        def _shutdown(_sig, _frame):
            from heartbeat.core import log
            log.info("Heartbeat 종료")
            _remove_pid()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        heartbeat_loop()

    elif args.command == "stop":
        existing = _is_running()
        if existing:
            os.kill(existing, signal.SIGTERM)
            print(f"Heartbeat 종료 (PID {existing})")
        else:
            print("실행 중인 heartbeat 없음")

    elif args.command == "status":
        existing = _is_running()
        if existing:
            print(f"Heartbeat 실행 중 (PID {existing})")
        else:
            print("실행 중인 heartbeat 없음")

        state = _load_state()
        if state:
            print()
            for name, info in state.items():
                last = info.get("last_run", "-")
                result = info.get("last_result", "-")
                duration = info.get("last_duration", "-")
                print(f"  [{name}] 마지막: {last} | 결과: {result} | 소요: {duration}초")

        if existing:
            log_files = sorted(LOG_DIR.glob("heartbeat_*.log"))
            if log_files:
                print()
                last_lines = log_files[-1].read_text(encoding="utf-8").strip().split("\n")[-5:]
                print("최근 로그:")
                for l in last_lines:
                    print(f"  {l}")

    elif args.command == "once":
        _setup_log_file()
        state = _load_state()
        _, jobs = parse_heartbeat_md()
        if args.job:
            jobs = [j for j in jobs if j["name"] == args.job]
        for job in jobs:
            run_job(job, state)

    elif args.command == "jobs":
        _, jobs = parse_heartbeat_md()
        if not jobs:
            print("HEARTBEAT.md에 잡이 없음")
        else:
            for j in jobs:
                interval_h = j["interval"] / 3600
                notify = j.get("notify", "all")
                print(f"  {j['name']} — {j['prompt']} (매 {interval_h:.1f}시간, notify: {notify}, slug: {j['slug']})")

    elif args.command == "init":
        cmd_init(args)

    elif args.command == "skills":
        cmd_skills(args)

    elif args.command == "install":
        cmd_install(args)

    elif args.command == "install-service":
        sys.exit(install_service(print_only=args.print_only))

    elif args.command == "uninstall-service":
        sys.exit(uninstall_service(print_only=args.print_only))

    else:
        parser.print_help()
