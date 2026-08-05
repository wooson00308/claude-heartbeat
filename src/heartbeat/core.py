"""Heartbeat daemon — reads ~/.claude/HEARTBEAT.md and runs jobs on schedule.

Generic scheduler that periodically executes claude CLI with configured prompts.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import psutil

from heartbeat import __version__

# subprocess.Popen 호출 시 OS별 process group / session 분리 옵션.
# POSIX: start_new_session=True → setsid()로 새 session leader, killpg 가능.
# Windows: creationflags=CREATE_NEW_PROCESS_GROUP → CTRL+BREAK_EVENT 분리, 부모와 시그널 격리.
_POPEN_GROUP_KWARGS: dict = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    if sys.platform == "win32"
    else {"start_new_session": True}
)

HEARTBEAT_FILE = Path.home() / ".claude" / "HEARTBEAT.md"
LOG_DIR = Path.home() / ".claude" / "heartbeat"
PID_FILE = LOG_DIR / "heartbeat.pid"
STATE_FILE = LOG_DIR / "state.json"
# v0.8.0: 프로젝트당 잡 파일 하나. 여러 도구가 HEARTBEAT.md 한 파일을 나눠 쓰다
# 서로의 잡을 지우는 사고(2026-08-04 실측)를 파일 단위 소유로 막는다.
JOBS_DIR = LOG_DIR / "jobs.d"

# state.json 최상위에서 밑줄로 시작하는 키는 데몬 예약 영역이다(잡 이름이 아니다).
# 잡 목록을 훑는 소비자는 이 키들을 건너뛴다 — 계약은 docs/config-contract.md.
DAEMON_STATE_KEY = "_daemon"

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
    """Save state to state.json (원자적 교체).

    소비자 앱이 이 파일을 폴링한다. write_text 직접 쓰기는 도중 상태(잘린 JSON)를
    읽힐 수 있고, 앱은 파싱 실패를 조용히 무시해 그 틱의 데이터가 유실된다.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def _record_daemon_start(state: dict) -> None:
    """기동한 데몬의 정체(버전·pid·시각)를 state.json에 남긴다.

    소비자 앱이 "지금 도는 데몬이 몇 버전인가"를 알 원천이다. 설치본 버전
    (`heartbeat --version`)과 다르면 코드는 갱신됐는데 프로세스가 옛 코드라는
    뜻이고, 그게 2026-08-05 사고(디스크 main / 메모리 v0.8.0)의 신호다.

    잡 항목과 같은 dict에 살지만 키가 `_`로 시작해 잡 이름과 섞이지 않는다.
    """
    state[DAEMON_STATE_KEY] = {
        "version": __version__,
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
    }
    _save_state(state)


def iter_job_states(state: dict):
    """state.json에서 잡 항목만 (이름, 정보)로 훑는다. `_` 예약 키는 건너뛴다."""
    return ((name, info) for name, info in state.items() if not name.startswith("_"))


# --- macOS notifications ---

def _notify(title: str, message: str) -> None:
    """Cross-platform desktop notification.

    plyer가 설치돼있으면 plyer.notification.notify()를 호출 (macOS / Windows /
    Linux 자동 처리). 미설치 / 백엔드 누락 시 조용히 로그로만 남기고 끝낸다.

    plyer는 [notify] extras로만 설치된다: pip install claude-heartbeat[notify]
    """
    try:
        from plyer import notification  # type: ignore[import-not-found]
        notification.notify(title=title, message=message, timeout=5)
    except Exception:
        # plyer 미설치 / 백엔드 없음 / 호출 실패 — main flow 멈추지 않음
        log.info(f"[notify] {title}: {message}")


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


def _parse_max_per(s: str) -> tuple[int, int] | None:
    """Parse `max_per` value like '5/24h' or '3/1d' → (count, window_seconds).

    슬라이딩 윈도우 quota: "지난 N초 안에 최대 M번". 자정 리셋 안 씀
    (timezone 의존 0).

    형식 위반 시 None — 파서는 quota 없는 잡으로 취급.
    """
    s = s.strip()
    if "/" not in s:
        return None
    count_str, window_str = s.split("/", 1)
    try:
        count = int(count_str.strip())
    except ValueError:
        return None
    if count <= 0:
        return None
    window = _parse_interval(window_str.strip())
    if window <= 0:
        return None
    return (count, window)


def parse_heartbeat_md() -> tuple[dict, list[dict]]:
    """Parse HEARTBEAT.md + jobs.d/*.md and return (global_config, job_configs).

    전역 설정(tick)은 HEARTBEAT.md만 읽는다. jobs.d/<slug>.md의 잡은 파일 이름의
    slug 소속으로 강제된다. 잡 이름이 겹치면 jobs.d가 HEARTBEAT.md를 이기고,
    jobs.d끼리 겹치면 정렬 순서상 먼저 읽은 파일이 이긴다 — 어느 쪽이든 경고를 남긴다.
    """
    if HEARTBEAT_FILE.exists():
        global_config, base_jobs = _parse_jobs_text(HEARTBEAT_FILE.read_text(encoding="utf-8"))
    else:
        global_config, base_jobs = {"tick": 60}, []

    merged: list[dict] = []
    seen: set[str] = set()
    if JOBS_DIR.exists():
        for path in sorted(JOBS_DIR.glob("*.md")):
            # `.md`로 끝나는 디렉토리 같은 비파일은 무시한다. 계약: jobs.d에서 읽는
            # 것은 일반 `.md` 파일뿐이다.
            if not path.is_file():
                continue
            slug = path.stem
            # jobs.d 파일의 전역 설정은 버린다. 프로젝트 파일이 데몬 전체 tick을
            # 바꿀 수 있으면 파일 단위 소유가 다시 깨진다.
            _, file_jobs = _parse_jobs_text(path.read_text(encoding="utf-8"))
            for job in file_jobs:
                if job["slug"] and job["slug"] != slug:
                    log.warning(
                        f"[jobs.d/{path.name}] {job['name']}의 slug `{job['slug']}`를 "
                        f"파일 소속 `{slug}`로 강제"
                    )
                job["slug"] = slug
                if job["name"] in seen:
                    log.warning(f"[jobs.d/{path.name}] 중복 잡 이름 `{job['name']}` — 먼저 읽은 정의 유지")
                    continue
                seen.add(job["name"])
                merged.append(job)

    for job in base_jobs:
        if job["name"] in seen:
            log.warning(f"[HEARTBEAT.md] `{job['name']}`은 jobs.d 정의가 우선")
            continue
        merged.append(job)

    return global_config, [j for j in merged if j["slug"] and j["prompt"]]


def _parse_jobs_text(content: str) -> tuple[dict, list[dict]]:
    """잡 문법 텍스트 한 덩이를 (전역 설정, 잡 목록)으로 읽는다. 필터링은 호출자 몫."""
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
                "max_per": None,  # (count, window_sec) 튜플 또는 None
                "model": "",  # claude --model 값. 비어 있으면 CLI 기본 모델
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
                elif key == "model":
                    current_job["model"] = val
                elif key == "max_per":
                    parsed = _parse_max_per(val)
                    if parsed is not None:
                        current_job["max_per"] = parsed
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

    return global_config, jobs


def _check_condition(job: dict) -> tuple[bool, str]:
    """Run condition command. Returns (should_run, reason).

    reason은 condition stdout의 첫 줄(최대 200자)이다. 스크립트가 아무것도 내지
    않으면 빈 문자열 — 데몬은 사유의 통로만 보장하고 내용은 조건 소유자 몫이다.
    v0.8.0: 소비자 화면이 "왜 건너뛰었는지"를 보여줄 길이 없던 문제(스크립트→데몬→앱
    세 층 모두에서 사유 증발)의 데몬 몫.

    v0.6.0: timeout / exception 시 fail-closed로 변경 (이전엔 fail-open이라
    조건 검사가 깨져도 claude를 깨워서 zero-cost gating 약속과 모순됐다).
    명시적으로 fail-open이 필요하면 condition에 `|| true`를 박는다.
    """
    condition = job.get("condition", "")
    if not condition:
        return True, ""

    name = job.get("name", "?")
    try:
        # claude 호출과 동일하게 프로젝트 cwd에서 실행한다. cwd 없이 돌리면
        # launchd 데몬의 cwd 기준이 되어 상대 경로 condition이 전부 깨진다.
        result = subprocess.run(
            condition,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(_slug_to_cwd(job.get("slug", ""))),
        )
        lines = (result.stdout or "").strip().splitlines()
        reason = lines[0][:200] if lines else ""
        return result.returncode == 0, reason
    except subprocess.TimeoutExpired:
        log.warning(f"[{name}] condition 타임아웃 (10s) → 안전을 위해 skip")
        return False, "condition 타임아웃 (10s)"
    except Exception as exc:
        log.warning(f"[{name}] condition 검사 실패 ({type(exc).__name__}: {exc}) → skip")
        return False, f"condition 실행 실패 ({type(exc).__name__})"


def _kill_process_group(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """Cross-platform 프로세스 트리 종료.

    psutil로 자식들까지 모아서 SIGTERM(POSIX) / terminate(Windows) → grace 대기
    → 안 죽으면 SIGKILL / kill. 손자가 stdout/stderr 파이프를 들고 있어도
    parent.wait()가 블록되지 않도록 트리 전체를 잡는다.
    """
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return

    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []

    targets = children + [parent]

    for p in targets:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    _, alive = psutil.wait_procs(targets, timeout=grace)

    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if alive:
        _, still_alive = psutil.wait_procs(alive, timeout=5)
        if still_alive:
            pids = [p.pid for p in still_alive]
            log.error(f"process tree {proc.pid} kill failed after SIGKILL: {pids}")


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


def _quota_exceeded(job_state: dict, max_per: tuple[int, int] | None) -> bool:
    """슬라이딩 윈도우 quota 체크. 호출 시 윈도우 밖 timestamp는 정리한다.

    max_per=(count, window_sec). 윈도우 안 timestamp 수가 count 이상이면 True.
    """
    if max_per is None:
        return False
    count, window_sec = max_per
    now_ts = time.time()
    recent = [t for t in job_state.get("recent_runs", []) if now_ts - t < window_sec]
    job_state["recent_runs"] = recent
    return len(recent) >= count


def _record_quota_run(job_state: dict, max_per: tuple[int, int] | None) -> None:
    """claude 호출 시점 timestamp를 recent_runs에 기록. count 초과분은 잘라낸다."""
    if max_per is None:
        return
    count, _ = max_per
    recent = job_state.setdefault("recent_runs", [])
    recent.append(time.time())
    job_state["recent_runs"] = recent[-(count * 2):]  # 안전상 두 배까지만 유지


def run_job(job: dict, state: dict) -> bool:
    """Execute a single heartbeat job."""
    name = job["name"]
    slug = job["slug"]
    prompt = job["prompt"]
    timeout = job["timeout"]
    max_per = job.get("max_per")
    model = job.get("model", "")

    # Quota 체크 — condition보다 먼저 (quota 초과면 condition subprocess 비용도 아낌).
    # _quota_exceeded가 recent_runs를 정리(변이)하므로 락 안에서 부른다. 배리어 제거로
    # 동시 그룹이 늘어 락 밖 변이는 직렬화 중인 json.dumps와 경합한다.
    with _state_lock:
        job_state = state.setdefault(name, {})
        quota_hit = _quota_exceeded(job_state, max_per)

    if quota_hit:
        count, window_sec = max_per  # type: ignore[misc]
        log.info(f"[{name}] quota 초과 ({len(job_state['recent_runs'])}/{count} in {window_sec}s), 스킵")
        with _state_lock:
            job_state["last_run"] = datetime.now().isoformat()
            job_state["last_result"] = "quota_skipped"
            job_state["last_duration"] = 0
            _save_state(state)
        return False

    # CWD 확인 — condition보다 먼저. condition이 프로젝트 cwd에서 돌기 때문에,
    # 없는 경로에서는 "condition 실행 실패"가 진짜 원인(CWD 없음)을 가린다.
    cwd = _slug_to_cwd(slug)
    if not cwd.exists():
        log.warning(f"[{name}] CWD {cwd} 존재하지 않음, 스킵")
        return False

    # Check condition
    condition_ok, condition_reason = _check_condition(job)
    if not condition_ok:
        log.info(f"[{name}] condition 불충족, 스킵" + (f" — {condition_reason}" if condition_reason else ""))
        # condition 불충족도 last_run을 갱신한다. 갱신하지 않으면 interval 만료된 잡이
        # 매 tick마다 condition 체크를 반복하면서 로그 폭주 + 외부 프로세스 호출이 누적된다.
        with _state_lock:
            job_state["last_run"] = datetime.now().isoformat()
            job_state["last_result"] = "skipped"
            job_state["last_duration"] = 0
            # 빈 출력이면 낡은 사유를 지운다. 이전 스킵의 사유가 이번 스킵의 사유로 보이면 안 된다.
            if condition_reason:
                job_state["last_condition_output"] = condition_reason
            else:
                job_state.pop("last_condition_output", None)
            _save_state(state)
        return False

    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]

    log.info(f"[{name}] 실행: claude -p \"{prompt}\"{f' --model {model}' if model else ''} (cwd: {cwd})")

    if _should_notify(job, "start"):
        _notify("Heartbeat", f"[{name}] 실행 시작")

    start_time = time.time()
    # active(monotonic) vs wall(time.time) 분리 측정.
    # macOS sleep 등으로 wall은 흐르지만 monotonic은 멈추는 케이스를 timeout
    # 로그에서 구분하기 위함. communicate(timeout=N)은 monotonic 기반이라
    # configured ≈ active << wall 패턴이 나오면 sleep 정황으로 판단 가능.
    start_mono = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_POPEN_GROUP_KWARGS,  # OS별 process group / session 분리
        )
        # claude 호출 시점에 quota 카운팅 — success/failure/timeout 모두 포함
        # (실패해도 토큰은 이미 소비된 거라 quota는 까야 함)
        with _state_lock:
            _record_quota_run(job_state, max_per)
            if condition_reason:
                job_state["last_condition_output"] = condition_reason
            else:
                job_state.pop("last_condition_output", None)
            _save_state(state)
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
        active = round(time.monotonic() - start_mono, 1)
        log.error(
            f"[{name}] 타임아웃 (설정 {timeout}s, active {active}s, wall {elapsed}s)"
        )
        with _state_lock:
            job_state["last_run"] = datetime.now().isoformat()
            job_state["last_result"] = "timeout"
            job_state["last_duration"] = elapsed
            _save_state(state)
        if _should_notify(job, "failure"):
            _notify("Heartbeat", f"[{name}] 타임아웃 ({elapsed}s)")
        return False
    except FileNotFoundError:
        log.error("claude CLI를 찾을 수 없음")
        if _should_notify(job, "failure"):
            _notify("Heartbeat", "claude CLI를 찾을 수 없음")
        return False


_state_lock = threading.Lock()


def _run_slug_group(_slug: str, jobs: list[dict], state: dict) -> None:
    """Run all due jobs for a single slug, sequentially.

    due 판정은 잡마다 그 시점 시각으로 한다. 디스패치 시각으로 고정하면 그룹 앞
    잡이 오래 도는 동안 뒤 잡의 판정 기준이 낡아 한 라운드씩 밀린다.
    """
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

        if time.time() - last_run_ts >= interval:
            try:
                run_job(job, state)
            except Exception as e:
                log.error(f"[{name}] 에러: {e}")
                with _state_lock:
                    state.setdefault(name, {})["last_run"] = datetime.now().isoformat()
                    _save_state(state)


# 실행 중인 slug 그룹. 배리어가 하던 역할 중 남겨야 하는 것은 "같은 그룹의 중복 실행
# 금지" 하나뿐이고, 이 가드가 그 역할만 잇는다(v0.8.0).
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _dispatch_due_groups(
    jobs: list[dict], state: dict, executor: ThreadPoolExecutor
) -> dict:
    """due 판정과 slug 그룹 제출만 하고 완료를 기다리지 않는다.

    실행 중인 그룹은 due여도 건너뛴다. 반환값은 이번에 제출한 slug → Future 맵이다.
    """
    slug_groups: dict[str, list[dict]] = defaultdict(list)
    for job in jobs:
        slug_groups[job["slug"]].append(job)

    futures: dict[str, object] = {}
    for slug, group in slug_groups.items():
        with _inflight_lock:
            if slug in _inflight:
                continue
            _inflight.add(slug)

        def _run(slug: str = slug, group: list[dict] = group) -> None:
            try:
                _run_slug_group(slug, group, state)
            except Exception as e:
                log.error(f"[{slug}] 그룹 실행 에러: {e}")
            finally:
                with _inflight_lock:
                    _inflight.discard(slug)

        futures[slug] = executor.submit(_run)
    return futures


def heartbeat_loop() -> None:
    """Main heartbeat loop. Re-reads HEARTBEAT.md + jobs.d each cycle.

    Jobs with the same slug run sequentially (to avoid file conflicts).
    Different slugs run independently — v0.8.0에서 사이클 배리어를 제거했다.
    이전에는 전 그룹 완료를 기다린 뒤 다음 tick을 재서, 한 프로젝트의 장기 세션이
    다른 프로젝트의 스케줄 전체를 세웠다(2026-08-04 실측: 10분짜리 세션이 다른
    프로젝트의 due 잡을 그만큼 미룸).
    """
    log.info(f"Heartbeat 데몬 시작 (v{__version__})")

    state = _load_state()
    _record_daemon_start(state)
    # max_workers는 동시 slug 그룹 수의 상한이다. 그룹당 스레드 하나가 잡 완료까지
    # 붙어 있으므로 프로젝트 수보다 넉넉하면 된다.
    executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hb-group")

    while True:
        config, jobs = parse_heartbeat_md()
        tick = config.get("tick", 60)

        if not jobs:
            log.warning(f"설정에 잡이 없음, {tick}초 후 재확인")
            time.sleep(tick)
            continue

        _dispatch_due_groups(jobs, state, executor)
        time.sleep(tick)


