"""Heartbeat daemon — reads ~/.claude/HEARTBEAT.md and runs jobs on schedule.

Generic scheduler that periodically executes claude CLI with configured prompts.
"""

import json
import os
import re
import signal
import subprocess
import time
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HEARTBEAT_FILE = Path.home() / ".claude" / "HEARTBEAT.md"
LOG_DIR = Path.home() / ".claude" / "heartbeat"
PID_FILE = LOG_DIR / "heartbeat.pid"
STATE_FILE = LOG_DIR / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [heartbeat] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("heartbeat")


def _setup_log_file() -> None:
    """Set up file logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"heartbeat_{datetime.now().strftime('%Y%m%d')}.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [heartbeat] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)


def _write_pid() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def _is_running() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, OSError):
        _remove_pid()
        return None


# --- State persistence ---

def _load_state() -> dict:
    """Load persisted state from state.json."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """Save state to state.json."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --- macOS notifications ---

def _notify(title: str, message: str) -> None:
    """Send macOS native notification via osascript."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _should_notify(job: dict, event: str) -> bool:
    """Check if notification should be sent for this event.

    event: 'start', 'success', 'failure'
    notify setting: 'all', 'failure', 'none' (default: 'all')
    """
    notify = job.get("notify", "all").lower()
    if notify == "none":
        return False
    if notify == "failure":
        return event == "failure"
    return True  # all


# --- Parsing ---

def _parse_interval(s: str) -> int:
    """Parse interval string like '3h', '30m', '1d' to seconds."""
    s = s.strip().lower()
    match = re.match(r"^(\d+)\s*(s|m|h|d)$", s)
    if match:
        val, unit = int(match.group(1)), match.group(2)
        return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    try:
        return int(s)
    except ValueError:
        return 3600


def _parse_timeout(s: str) -> int:
    """Parse timeout string to seconds."""
    return _parse_interval(s)


def parse_heartbeat_md() -> tuple[dict, list[dict]]:
    """Parse ~/.claude/HEARTBEAT.md and return (global_config, job_configs)."""
    if not HEARTBEAT_FILE.exists():
        return {"tick": 60}, []

    content = HEARTBEAT_FILE.read_text(encoding="utf-8")
    global_config = {"tick": 60}
    jobs = []
    current_job = None

    for line in content.split("\n"):
        line = line.strip()

        if line.startswith("## "):
            if current_job:
                jobs.append(current_job)
            current_job = {
                "name": line[3:].strip(),
                "slug": "",
                "prompt": "",
                "interval": 3600,
                "timeout": 600,
                "condition": "",
                "notify": "all",
            }
        elif current_job and line.startswith("- "):
            kv = line[2:]
            if ":" in kv:
                key, val = kv.split(":", 1)
                key = key.strip().lower()
                val = val.strip()
                if key == "slug":
                    current_job["slug"] = val
                elif key == "prompt":
                    current_job["prompt"] = val
                elif key == "interval":
                    current_job["interval"] = _parse_interval(val)
                elif key == "timeout":
                    current_job["timeout"] = _parse_timeout(val)
                elif key == "condition":
                    current_job["condition"] = val
                elif key == "notify":
                    current_job["notify"] = val
        elif not current_job and line.startswith("- "):
            # Global config (before any ## job header)
            kv = line[2:]
            if ":" in kv:
                key, val = kv.split(":", 1)
                key = key.strip().lower()
                val = val.strip()
                if key == "tick":
                    global_config["tick"] = _parse_interval(val)

    if current_job:
        jobs.append(current_job)

    return global_config, [j for j in jobs if j["slug"] and j["prompt"]]


def _check_condition(job: dict) -> bool:
    """Run condition command. Returns True if job should run."""
    condition = job.get("condition", "")
    if not condition:
        return True

    try:
        result = subprocess.run(
            condition, shell=True, capture_output=True, timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return True


def _kill_process_group(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """Forcefully terminate process group with SIGTERM, then SIGKILL fallback.

    Required because `subprocess.run(timeout=...)` only sends SIGKILL to the
    direct child. If the child spawned grandchildren that hold stdout/stderr
    pipes open, the parent's `wait()` blocks forever. Sending the signal to
    the entire process group (created via `start_new_session=True`) ensures
    grandchildren are cleaned up too.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error(f"process group {pgid} kill failed after SIGKILL")


def _slug_to_cwd(slug: str) -> Path:
    """Convert project slug to CWD path.

    Slug is CWD with '/' replaced by '-' and prefixed with '-'.
    Simple replace('-', '/') breaks folder names containing hyphens (e.g. dr2-unity).
    Instead, walk from root and greedily match existing directories.
    """
    parts = slug.lstrip("-").split("-")
    path = Path("/")
    i = 0
    while i < len(parts):
        # Try longest match first: join remaining parts with '-' and shrink
        matched = False
        for end in range(len(parts), i, -1):
            candidate = "-".join(parts[i:end])
            trial = path / candidate
            if trial.exists():
                path = trial
                i = end
                matched = True
                break
        if not matched:
            # Fallback: take single part
            path = path / parts[i]
            i += 1
    return path


def run_job(job: dict, state: dict) -> bool:
    """Execute a single heartbeat job."""
    name = job["name"]
    slug = job["slug"]
    prompt = job["prompt"]
    timeout = job["timeout"]

    with _state_lock:
        job_state = state.setdefault(name, {})

    # Check condition
    if not _check_condition(job):
        log.info(f"[{name}] condition 불충족, 스킵")
        # condition 불충족도 last_run을 갱신한다. 갱신하지 않으면 interval 만료된 잡이
        # 매 tick마다 condition 체크를 반복하면서 로그 폭주 + 외부 프로세스 호출이 누적된다.
        with _state_lock:
            job_state["last_run"] = datetime.now().isoformat()
            job_state["last_result"] = "skipped"
            job_state["last_duration"] = 0
            _save_state(state)
        return False

    cwd = _slug_to_cwd(slug)
    if not cwd.exists():
        log.warning(f"[{name}] CWD {cwd} 존재하지 않음, 스킵")
        return False

    log.info(f"[{name}] 실행: claude -p \"{prompt}\" (cwd: {cwd})")

    if _should_notify(job, "start"):
        _notify("Heartbeat", f"[{name}] 실행 시작")

    start_time = time.time()

    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # 새 process group → killpg로 손자까지 잡음
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # 파이프 잔량 회수 (이미 죽었으니 즉시 반환)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            # 기존 except 블록이 받아서 처리하도록 재발생
            raise

        returncode = proc.returncode
        elapsed = round(time.time() - start_time, 1)

        if returncode == 0:
            log.info(f"[{name}] 완료 ({elapsed}초)")
            with _state_lock:
                job_state["last_run"] = datetime.now().isoformat()
                job_state["last_result"] = "success"
                job_state["last_duration"] = elapsed
                _save_state(state)
            if _should_notify(job, "success"):
                _notify("Heartbeat", f"[{name}] 완료 ({elapsed}초)")
            return True
        else:
            log.error(f"[{name}] 실패 (exit {returncode}, {elapsed}초): {stderr[:200]}")
            with _state_lock:
                job_state["last_run"] = datetime.now().isoformat()
                job_state["last_result"] = "failure"
                job_state["last_duration"] = elapsed
                _save_state(state)
            if _should_notify(job, "failure"):
                _notify("Heartbeat", f"[{name}] 실패 (exit {returncode})")
            return False

    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - start_time, 1)
        log.error(f"[{name}] 타임아웃 ({timeout}초)")
        with _state_lock:
            job_state["last_run"] = datetime.now().isoformat()
            job_state["last_result"] = "timeout"
            job_state["last_duration"] = elapsed
            _save_state(state)
        if _should_notify(job, "failure"):
            _notify("Heartbeat", f"[{name}] 타임아웃 ({timeout}초)")
        return False
    except FileNotFoundError:
        log.error("claude CLI를 찾을 수 없음")
        if _should_notify(job, "failure"):
            _notify("Heartbeat", "claude CLI를 찾을 수 없음")
        return False


_state_lock = threading.Lock()


def _run_slug_group(_slug: str, jobs: list[dict], state: dict, now: float) -> None:
    """Run all due jobs for a single slug, sequentially."""
    for job in jobs:
        name = job["name"]
        interval = job["interval"]

        with _state_lock:
            job_state = state.get(name, {})
            last_run_str = job_state.get("last_run")

        if last_run_str:
            try:
                last_run_ts = datetime.fromisoformat(last_run_str).timestamp()
            except ValueError:
                last_run_ts = 0
        else:
            last_run_ts = 0

        if now - last_run_ts >= interval:
            try:
                run_job(job, state)
            except Exception as e:
                log.error(f"[{name}] 에러: {e}")
                with _state_lock:
                    state.setdefault(name, {})["last_run"] = datetime.now().isoformat()
                    _save_state(state)


def heartbeat_loop() -> None:
    """Main heartbeat loop. Re-reads HEARTBEAT.md each cycle.

    Jobs with the same slug run sequentially (to avoid file conflicts).
    Different slugs run in parallel.
    """
    log.info("Heartbeat 데몬 시작")

    state = _load_state()

    while True:
        config, jobs = parse_heartbeat_md()
        tick = config.get("tick", 60)

        if not jobs:
            log.warning(f"HEARTBEAT.md에 잡이 없음, {tick}초 후 재확인")
            time.sleep(tick)
            continue

        # Group jobs by slug
        slug_groups: dict[str, list[dict]] = defaultdict(list)
        for job in jobs:
            slug_groups[job["slug"]].append(job)

        now = time.time()

        # Run slug groups in parallel
        with ThreadPoolExecutor(max_workers=len(slug_groups)) as executor:
            futures = {
                executor.submit(_run_slug_group, slug, group, state, now): slug
                for slug, group in slug_groups.items()
            }
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    future.result()
                except Exception as e:
                    log.error(f"[{slug}] 그룹 실행 에러: {e}")

        time.sleep(tick)


