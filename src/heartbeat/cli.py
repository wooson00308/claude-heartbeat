"""Heartbeat CLI — install skills, manage daemon, run jobs."""

import json
import shutil
import sys
from pathlib import Path

from heartbeat import __version__
from heartbeat.core import (
    DAEMON_STATE_KEY,
    _is_running,
    _load_state,
    _remove_pid,
    _setup_log_file,
    _write_pid,
    heartbeat_loop,
    iter_job_states,
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

        from heartbeat import core

        # 잡은 프로젝트별 jobs.d/<slug>.md에 쓴다(v0.8.0 계약). HEARTBEAT.md는 읽기만 —
        # 레거시에 있는 이름을 jobs.d에 또 쓰면 파싱은 jobs.d가 이겨서 레거시 정의가
        # 조용히 무시되므로, 중복 판정에는 양쪽을 다 본다.
        legacy = HEARTBEAT_FILE.read_text(encoding="utf-8") if HEARTBEAT_FILE.exists() else ""

        added = []
        for slug in slugs:
            short = _slug_short(slug)
            job_block = template.replace("{slug}", slug).replace("{slug_short}", short)
            job_name = f"{skill_name}-{short}"
            target = core.JOBS_DIR / f"{slug}.md"
            existing = target.read_text(encoding="utf-8") if target.exists() else ""

            if f"## {job_name}" in existing or f"## {job_name}" in legacy:
                print(f"  [{job_name}] 이미 등록됨, 스킵")
                continue

            core.JOBS_DIR.mkdir(parents=True, exist_ok=True)
            updated = (existing.rstrip() + "\n\n" if existing.strip() else "") + job_block
            if not updated.endswith("\n"):
                updated += "\n"
            target.write_text(updated, encoding="utf-8")
            print(f"✓ jobs.d/{target.name}에 [{job_name}] 등록")
            added.append(job_name)

        if not added:
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


def cmd_migrate(args) -> None:
    """HEARTBEAT.md의 잡을 slug별 jobs.d/<slug>.md로 분리한다 (v0.8.0).

    slug가 없는 잡 블록과 전역 설정은 HEARTBEAT.md에 남는다. 대상 파일이 이미 있으면
    그 slug는 건너뛴다 — 덮어쓰기로 기존 프로젝트 파일을 깨지 않는다. HTML 주석 줄
    (외부 도구의 관리 마커)은 옮기지 않고 짝이 맞은 채로 HEARTBEAT.md에 남긴다.
    """
    from datetime import datetime

    from heartbeat import core

    if not HEARTBEAT_FILE.exists():
        print(f"{HEARTBEAT_FILE} 없음 — 옮길 잡이 없습니다.")
        return

    content = HEARTBEAT_FILE.read_text(encoding="utf-8")
    preamble: list[str] = []
    blocks: list[list[str]] = []
    for line in content.split("\n"):
        if line.strip().startswith("## "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            preamble.append(line)

    def _is_comment(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("<!--") and stripped.endswith("-->")

    def _block_slug(block: list[str]) -> str:
        for raw in block:
            stripped = raw.strip()
            if stripped.lower().startswith("- slug:"):
                return stripped.split(":", 1)[1].strip()
        return ""

    groups: dict[str, list[list[str]]] = {}
    kept_blocks: list[list[str]] = []
    kept_comments: list[str] = []
    for block in blocks:
        slug = _block_slug(block)
        if slug:
            kept_comments.extend(line for line in block if _is_comment(line))
            groups.setdefault(slug, []).append([l for l in block if not _is_comment(l)])
        else:
            kept_blocks.append(block)

    if not groups:
        print("slug 있는 잡이 없음 — 옮길 것이 없습니다.")
        return

    moved = 0
    for slug, slug_blocks in groups.items():
        target = core.JOBS_DIR / f"{slug}.md"
        if target.exists():
            print(f"⚠ {target} 이미 존재 — {slug} 잡 {len(slug_blocks)}개는 HEARTBEAT.md에 남김")
            kept_blocks.extend(slug_blocks)
            continue
        body = "\n\n".join("\n".join(b).strip() for b in slug_blocks) + "\n"
        if args.dry_run:
            print(f"[dry-run] {target} ← 잡 {len(slug_blocks)}개")
        else:
            core.JOBS_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            print(f"✓ {target} ← 잡 {len(slug_blocks)}개")
        moved += len(slug_blocks)

    parts = ["\n".join(preamble).strip()] + kept_comments + ["\n".join(b).strip() for b in kept_blocks]
    parts = [p for p in parts if p]
    new_content = ("\n\n".join(parts) + "\n") if parts else "# HEARTBEAT\n"

    if args.dry_run:
        print(f"[dry-run] HEARTBEAT.md에 남는 잡 블록 {len(kept_blocks)}개")
        return

    backup = HEARTBEAT_FILE.with_name(
        HEARTBEAT_FILE.name + ".bak-" + datetime.now().strftime("%Y%m%d%H%M%S")
    )
    shutil.copy2(HEARTBEAT_FILE, backup)
    HEARTBEAT_FILE.write_text(new_content, encoding="utf-8")
    print(f"✓ 잡 {moved}개 이동, 원본 백업: {backup.name}")
    print("데몬은 다음 사이클부터 jobs.d를 함께 읽습니다.")
    print("주의: HEARTBEAT.md에 직접 쓰는 도구(구버전 앱·스킬)가 잡을 재설치하면 중복 정의가")
    print("생기고, 파싱은 jobs.d가 이겨서 그쪽 편집이 조용히 무시됩니다. 도구를 먼저 전환하세요.")


def cmd_init(_args) -> None:
    """Initialize heartbeat: create HEARTBEAT.md + jobs.d if missing."""
    from heartbeat import core

    if HEARTBEAT_FILE.exists():
        print(f"✓ {HEARTBEAT_FILE} 이미 존재")
    else:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text("# HEARTBEAT\n\n", encoding="utf-8")
        print(f"✓ {HEARTBEAT_FILE} 생성")

    if not core.JOBS_DIR.exists():
        core.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"✓ {core.JOBS_DIR} 생성 (프로젝트별 잡 파일 자리)")

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


def runtime_status(install_root: "Path | None" = None) -> dict:
    """Report installed version, running version and service state as one value.

    Everything here is read: the launcher, the version manifests and the OS
    registration are only inspected, so an app can poll this without changing
    what it is asking about.  A fact that could not be read stays null instead
    of being filled in with a plausible one.
    """
    from heartbeat.agent_contract import API_VERSION
    from heartbeat.legacy_migration import installed_runtime, runtime_target, runtime_version_of
    from heartbeat.service import inspect_service
    from heartbeat.service.base import checked_at

    installed = installed_runtime(install_root)
    service_status = inspect_service()
    running_version = (
        runtime_version_of(Path(service_status.executable))
        if service_status.running and service_status.executable
        else None
    )
    target = runtime_target()
    if target.startswith("unsupported-") or service_status.result == "unsupported_platform":
        result = "unsupported_platform"
    elif installed["result"] == "unsupported_version":
        result = "unsupported_version"
    else:
        result = "ok"
    return {
        "schemaVersion": 1,
        "result": result,
        "checkedAt": checked_at(),
        "runtimeVersion": __version__,
        "installedVersion": installed["installedVersion"],
        "runningVersion": running_version,
        "apiMajor": int(API_VERSION),
        "target": target,
        "executable": str(Path(sys.argv[0]).resolve()),
        "installRoot": installed["installRoot"],
        "launcher": installed["launcher"],
        "installResult": installed["result"],
        "recoverable": service_status.recoverable,
        "service": service_status.to_dict(),
        "evidence": [*installed["evidence"], *service_status.evidence],
    }


def main() -> None:
    import argparse
    import os
    import signal

    parser = argparse.ArgumentParser(
        prog="heartbeat",
        description="Heartbeat — periodic claude agent scheduler"
    )
    # 설치본 버전. 출력은 `heartbeat <X.Y.Z>` 한 줄로 고정된다(계약).
    parser.add_argument("--version", action="version", version=f"heartbeat {__version__}")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialize heartbeat (create HEARTBEAT.md)")

    # version (--version과 같은 출력. 서브커맨드로도 찾는 사람이 있다)
    sub.add_parser("version", help="Print installed heartbeat version")

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

    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Split HEARTBEAT.md jobs into per-project jobs.d/<slug>.md files",
    )
    p_migrate.add_argument("--dry-run", action="store_true", help="실제 쓰기 없이 결과만 출력")

    # update (설치본 갱신 + 데몬 재기동)
    sub.add_parser(
        "update",
        help="Update this editable install (git pull --ff-only) and restart the daemon",
    )

    # agent (버전화된 앱-런타임 JSON 계약)
    p_agent = sub.add_parser("agent", help="Manage the project agent runtime through JSON")
    agent_sub = p_agent.add_subparsers(dest="agent_command")
    agent_sub.add_parser("contract", help="Print the supported agent contract as JSON")
    p_agent_config = agent_sub.add_parser("config", help="Validate, write, or read agent configuration")
    p_agent_config.add_argument("operation", choices=["validate", "write", "read"])
    agent_sub.add_parser("state", help="Read project-scoped agent state")
    agent_sub.add_parser("plan", help="Read an execution plan without starting anything")
    agent_sub.add_parser("logs", help="Read redacted run events from a cursor")
    p_agent_run = agent_sub.add_parser("run", help="Start, cancel, or retry runs")
    p_agent_run.add_argument("operation", choices=["start", "cancel", "retry"])
    p_agent_project = agent_sub.add_parser("project", help="Pause or resume new assignment for one project")
    p_agent_project.add_argument("operation", choices=["pause", "resume"])
    p_agent_provider = agent_sub.add_parser("provider", help="Report provider readiness")
    p_agent_provider.add_argument("operation", choices=["diagnose"])

    # runtime (독립 실행형 배포물의 manifest·stable launcher·읽기 전용 마이그레이션)
    p_runtime = sub.add_parser("runtime", help="Inspect or verify a standalone Heartbeat runtime")
    runtime_sub = p_runtime.add_subparsers(dest="runtime_command")
    p_inspect = runtime_sub.add_parser("inspect", help="Print runtime and service status as JSON")
    p_inspect.add_argument("--install-root", help="installed runtime root; defaults to the active stable launcher")
    p_manifest = runtime_sub.add_parser("write-manifest", help="Write hashes for an unpacked one-folder runtime")
    p_manifest.add_argument("--root", required=True, help="unpacked runtime directory")
    p_manifest.add_argument("--target", help="published target name")
    p_verify_manifest = runtime_sub.add_parser("verify-manifest", help="Verify an unpacked runtime before activation")
    p_verify_manifest.add_argument("--root", required=True, help="unpacked runtime directory")
    p_activate = runtime_sub.add_parser("activate", help="Atomically switch the stable runtime launcher")
    p_activate.add_argument("--install-root", required=True)
    p_activate.add_argument("--version-dir", required=True)
    p_preview = runtime_sub.add_parser("migration-preview", help="Read legacy jobs and history without writing")
    p_preview.add_argument("--home", help="home directory to inspect; defaults to the current user")

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
        daemon = state.get(DAEMON_STATE_KEY) or {}
        if existing and daemon:
            # 도는 프로세스와 디스크 코드가 갈린 상태를 여기서 잡는다.
            print(f"  데몬 v{daemon.get('version', '?')} / 설치본 v{__version__}")

        jobs = list(iter_job_states(state))
        if jobs:
            print()
            for name, info in jobs:
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
        running = _is_running()
        if running:
            # once는 데몬과 다른 프로세스라 in-flight 가드를 공유하지 않는다.
            print(f"⚠ 데몬 실행 중(PID {running}) — 같은 잡이 데몬과 겹쳐 실행될 수 있습니다.")
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

    elif args.command == "migrate":
        cmd_migrate(args)

    elif args.command == "install":
        cmd_install(args)

    elif args.command == "version":
        print(f"heartbeat {__version__}")

    elif args.command == "update":
        from heartbeat.update import cmd_update

        sys.exit(cmd_update(args))

    elif args.command == "install-service":
        sys.exit(install_service(print_only=args.print_only))

    elif args.command == "uninstall-service":
        sys.exit(uninstall_service(print_only=args.print_only))

    elif args.command == "agent":
        from heartbeat.agent_cli import run_agent_command

        command_map = {
            "contract": "contract.read",
            "state": "state.read",
            "validate": "config.validate",
            "write": "config.write",
            "read": "config.read",
            "plan": "plan.read",
            "logs": "logs.read",
            "run start": "run.start",
            "run cancel": "run.cancel",
            "run retry": "run.retry",
            "project pause": "project.pause",
            "project resume": "project.resume",
            "provider diagnose": "provider.diagnose",
        }
        selected = getattr(args, "agent_command", None)
        if selected == "config":
            selected = args.operation
        elif selected in {"run", "project", "provider"}:
            selected = f"{selected} {args.operation}"
        if selected not in command_map:
            p_agent.print_help()
            sys.exit(2)
        sys.exit(run_agent_command(command_map[selected]))

    elif args.command == "runtime":
        from heartbeat.agent_contract import API_VERSION
        from heartbeat.legacy_migration import (
            RuntimeIntegrityError,
            activate_stable_launcher,
            preview_legacy_migration,
            runtime_target,
            verify_runtime_manifest,
            write_runtime_manifest,
        )

        try:
            if args.runtime_command == "inspect":
                print(json.dumps(
                    runtime_status(Path(args.install_root) if args.install_root else None),
                    ensure_ascii=False, separators=(",", ":"),
                ))
            elif args.runtime_command == "write-manifest":
                manifest = write_runtime_manifest(Path(args.root), target=args.target)
                print(json.dumps({"outcome": "success", "manifest": str(manifest)}, ensure_ascii=False, separators=(",", ":")))
            elif args.runtime_command == "verify-manifest":
                manifest = verify_runtime_manifest(Path(args.root))
                print(json.dumps({"outcome": "success", "manifest": manifest}, ensure_ascii=False, separators=(",", ":")))
            elif args.runtime_command == "activate":
                launcher = activate_stable_launcher(Path(args.install_root), Path(args.version_dir))
                print(json.dumps({"outcome": "success", "launcher": str(launcher)}, ensure_ascii=False, separators=(",", ":")))
            elif args.runtime_command == "migration-preview":
                home = Path(args.home) if args.home else None
                print(json.dumps(preview_legacy_migration(home), ensure_ascii=False, separators=(",", ":")))
            else:
                p_runtime.print_help()
                sys.exit(2)
        except RuntimeIntegrityError as error:
            print(json.dumps({"outcome": "failure", "error": str(error)}, ensure_ascii=False, separators=(",", ":")))
            sys.exit(2)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
