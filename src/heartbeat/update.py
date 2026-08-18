"""`heartbeat update` — editable 설치본을 갱신하고 데몬을 새 코드로 재기동한다.

존재 이유는 갱신과 재기동 사이의 창이다. 2026-08-05에 디스크의 코드는 main인데
메모리의 데몬은 v0.8.0인 상태가 실측됐다. 코드만 올라가고 프로세스가 안 바뀌면
아무도 그 사실을 모른다. 이 명령은 pull이 됐으면 재기동까지 가거나, 못 가면
그 사실을 종료 코드와 출력으로 명시한다.

출력 규격(계약):
- stdout에는 계약 줄만 나간다. 사람이 읽을 진단·가이드는 stderr로 간다.
- 계약 줄은 공백으로 나뉜 `key=value` 목록이고 값에는 공백이 없다.
- 단계 줄이 0~3개(`step=repo` → `step=deps` → `step=service` 순서), 마지막에
  `result=` 줄이 정확히 하나. `result=` 줄은 항상 마지막이다.
- 소비자는 모르는 key를 무시해야 한다. 키 추가는 하위호환 변경으로 취급한다.

자세한 어휘·종료 코드는 docs/config-contract.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import heartbeat
from heartbeat import service
from heartbeat.core import DAEMON_STATE_KEY, _is_running, _load_state

# 종료 코드. 앱은 이 값으로 실패 원인을 구분한다.
EXIT_OK = 0
EXIT_NOT_A_GIT_REPO = 10
EXIT_DIRTY_TREE = 11
EXIT_NON_FAST_FORWARD = 12
EXIT_FETCH_FAILED = 13
EXIT_NO_UPSTREAM = 14
EXIT_DEPS_FAILED = 20
EXIT_RESTART_FAILED = 30
EXIT_DAEMON_UNMANAGED = 31
EXIT_SELF_RESTART_BLOCKED = 32

GIT_TIMEOUT = 120
PIP_TIMEOUT = 600

# Windows에서 콘솔 창 없이 자식을 만든다. 이 명령은 앱이 띄운 콘솔 없는 CLI에서 돌기 때문에,
# git·pip 자식이 콘솔을 새로 만들면 화면에 창이 깜빡인다(사유 전문은 providers/process.py의
# NO_WINDOW). POSIX에서는 0이라 아무 뜻이 없다.
_NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def _emit(**fields: object) -> None:
    """계약 줄 하나를 stdout에 낸다. 키 순서는 넘긴 순서 그대로다.

    값 안의 공백은 `_`로 접는다. 계약이 공백 구분이라 값에 공백이 들어가면 줄이
    쪼개져 앱의 파싱이 어긋난다 — OS가 주는 값(서비스 label)이 이 경로로 들어온다.
    """
    print(
        " ".join(f"{key}={'_'.join(str(value).split())}" for key, value in fields.items()),
        flush=True,
    )


def _note(message: str) -> None:
    """사람용 진단. 계약이 아니므로 stderr로 보낸다."""
    print(message, file=sys.stderr, flush=True)


def _finish(result: str, version: str, code: int) -> int:
    _emit(result=result, version=version, exit=code)
    return code


def _repo_root() -> Path | None:
    """설치본의 git 저장소 루트. editable 설치가 아니면 None.

    src/heartbeat/__init__.py 기준으로 두 단계 위가 저장소 루트다. wheel 설치는
    site-packages라 .git이 없어 None이 되고, 그 경우 갱신은 pip 몫이다.
    """
    root = Path(heartbeat.__file__).resolve().parents[2]
    if (root / ".git").exists() and (root / "pyproject.toml").exists():
        return root
    return None


def _version_on_disk(root: Path) -> str:
    """pull 이후 디스크에 있는 버전.

    지금 프로세스에 import된 `heartbeat.__version__`은 pull 이전 코드의 값이다.
    갱신 결과를 보고하려면 파일을 다시 읽어야 한다 — 이 구분이 없으면 update가
    "갱신했다"면서 옛 버전을 보고한다.
    """
    source = root / "src" / "heartbeat" / "__init__.py"
    try:
        match = _VERSION_RE.search(source.read_text(encoding="utf-8"))
    except OSError:
        return heartbeat.__version__
    return match.group(1) if match else heartbeat.__version__


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
        creationflags=_NO_WINDOW,
    )


def _inside_daemon_tree(daemon_pid: int | None) -> bool:
    """지금 프로세스가 데몬 자신 또는 그 자손인가.

    데몬이 돌린 잡 안에서 update를 부르면, 재기동이 그 잡 프로세스 트리째 죽인다.
    죽는 시점이 계약 줄을 다 내기 전이면 앱은 결과를 못 받고 update가 어디까지
    갔는지도 모른다. 그래서 그 경로는 재기동을 하지 않고 명시적으로 실패시킨다.
    """
    if daemon_pid is None:
        return False
    if os.getpid() == daemon_pid:
        return True
    try:
        import psutil

        return any(parent.pid == daemon_pid for parent in psutil.Process(os.getpid()).parents())
    except Exception:
        # 프로세스 조회 실패는 "밖에 있다"로 본다. 여기서 막아버리면 정상 경로가
        # psutil 사정으로 죽는다.
        return False


def _step_repo(root: Path) -> tuple[bool, int | None]:
    """저장소 갱신. 반환은 (HEAD가 움직였는가, 실패 시 종료 코드).

    실패 원인은 서로 다른 종료 코드로 구분된다 — 앱이 "네가 커밋 안 한 게 있다"와
    "갈라져서 ff가 안 된다"에 다른 안내를 띄울 수 있어야 한다.
    """
    try:
        dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    except FileNotFoundError:
        _emit(step="repo", status="failed", detail="git-missing")
        _note("git 명령을 찾을 수 없습니다. git 설치 후 다시 실행하세요.")
        return False, EXIT_NOT_A_GIT_REPO
    except subprocess.TimeoutExpired:
        _emit(step="repo", status="failed", detail="fetch-failed")
        _note(f"git status가 {GIT_TIMEOUT}초 안에 끝나지 않았습니다.")
        return False, EXIT_FETCH_FAILED

    if dirty.returncode != 0:
        _emit(step="repo", status="failed", detail="not-a-git-repo")
        _note(f"{root}가 git 저장소가 아닙니다: {dirty.stderr.strip()}")
        return False, EXIT_NOT_A_GIT_REPO

    if dirty.stdout.strip():
        _emit(step="repo", status="failed", detail="dirty-tree")
        _note("추적 중인 파일에 미커밋 변경이 있어 갱신을 멈춥니다. 변경을 커밋하거나 되돌리세요:")
        for line in dirty.stdout.strip().split("\n")[:10]:
            _note(f"  {line}")
        return False, EXIT_DIRTY_TREE

    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        _emit(step="repo", status="failed", detail="no-upstream")
        _note("현재 브랜치에 upstream이 없어 무엇을 당길지 알 수 없습니다.")
        _note("  예: git branch --set-upstream-to=origin/main")
        return False, EXIT_NO_UPSTREAM

    try:
        fetched = _git(root, "fetch", "--quiet")
    except subprocess.TimeoutExpired:
        _emit(step="repo", status="failed", detail="fetch-failed")
        _note(f"git fetch가 {GIT_TIMEOUT}초 안에 끝나지 않았습니다(네트워크 확인).")
        return False, EXIT_FETCH_FAILED

    if fetched.returncode != 0:
        _emit(step="repo", status="failed", detail="fetch-failed")
        _note(f"git fetch 실패: {fetched.stderr.strip()}")
        return False, EXIT_FETCH_FAILED

    old = _git(root, "rev-parse", "HEAD").stdout.strip()
    target = _git(root, "rev-parse", "@{u}").stdout.strip()

    if old and old == target:
        _emit(step="repo", status="ok", detail="up-to-date")
        return False, None

    # ff 가능 여부를 미리 판정한다. `git pull --ff-only`의 실패 메시지로 원인을
    # 가리면 git 로케일에 계약이 묶인다.
    if _git(root, "merge-base", "--is-ancestor", "HEAD", "@{u}").returncode != 0:
        _emit(step="repo", status="failed", detail="non-fast-forward")
        _note("로컬 커밋이 upstream과 갈라져 fast-forward가 불가능합니다.")
        _note(f"  {upstream.stdout.strip()} 기준으로 rebase 하거나 브랜치를 정리하세요.")
        return False, EXIT_NON_FAST_FORWARD

    merged = _git(root, "merge", "--ff-only", "@{u}")
    if merged.returncode != 0:
        _emit(step="repo", status="failed", detail="merge-failed")
        _note(f"fast-forward 병합 실패: {merged.stderr.strip()}")
        return False, EXIT_NON_FAST_FORWARD

    _emit(step="repo", status="ok", detail="updated", **{"from": old[:7], "to": target[:7]})
    return True, None


def _step_deps(root: Path) -> int | None:
    """의존성 반영. HEAD가 움직였을 때만 돈다.

    editable 재설치는 새 의존성뿐 아니라 패키지 메타데이터도 갱신한다 — 메타데이터가
    `pip install -e` 시점에 굳는 성질 때문에 실측에서 실제로 어긋나 있었다.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(root)],
            capture_output=True, text=True, timeout=PIP_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        _emit(step="deps", status="failed", detail="pip-timeout")
        _note(f"pip install이 {PIP_TIMEOUT}초 안에 끝나지 않았습니다.")
        return EXIT_DEPS_FAILED

    if result.returncode != 0:
        _emit(step="deps", status="failed", detail="pip-failed")
        _note("pip install -e 실패:")
        for line in (result.stderr or result.stdout).strip().split("\n")[-10:]:
            _note(f"  {line}")
        return EXIT_DEPS_FAILED

    _emit(step="deps", status="ok", detail="reinstalled")
    return None


def cmd_update(_args) -> int:
    """update 전체 흐름. 반환값이 그대로 프로세스 종료 코드다."""
    root = _repo_root()
    if root is None:
        _emit(step="repo", status="failed", detail="not-a-git-repo")
        _note("이 설치본은 git 저장소가 아닙니다(editable 설치가 아님).")
        _note("  갱신: pip install -U claude-heartbeat")
        return _finish("failed", heartbeat.__version__, EXIT_NOT_A_GIT_REPO)

    head_moved, failure = _step_repo(root)
    if failure is not None:
        return _finish("failed", _version_on_disk(root), failure)

    if head_moved:
        failure = _step_deps(root)
        if failure is not None:
            # 저장소는 이미 움직였다. 여기서 멈추면 코드와 설치 상태가 어긋난 채 남는다.
            return _finish("partial", _version_on_disk(root), failure)
    else:
        _emit(step="deps", status="skipped", detail="not-needed")

    disk_version = _version_on_disk(root)
    daemon_pid = _is_running()
    daemon_version = (_load_state().get(DAEMON_STATE_KEY) or {}).get("version")

    # 재기동이 필요한 두 경우: 코드가 움직였거나, 코드는 그대로인데 도는 프로세스가
    # 옛 버전이거나. 후자가 2026-08-05 사고의 모양이고, 그래서 update는 pull이
    # 공회전이어도 프로세스를 확인한다.
    restart_needed = head_moved or (daemon_pid is not None and daemon_version != disk_version)

    if not restart_needed:
        _emit(step="service", status="skipped", detail="not-needed")
        return _finish("ok", disk_version, EXIT_OK)

    if _inside_daemon_tree(daemon_pid):
        _emit(step="service", status="skipped", detail="self-restart-blocked")
        _note("데몬 자신의 프로세스 트리 안에서 실행됐습니다. 재기동하면 이 명령이 먼저 죽습니다.")
        _note("  데몬 밖(앱·터미널)에서 heartbeat update를 다시 실행하세요.")
        return _finish("partial", disk_version, EXIT_SELF_RESTART_BLOCKED)

    outcome = service.restart_service()
    fields: dict[str, object] = {
        "step": "service", "status": outcome.status, "detail": outcome.detail,
    }
    if outcome.label:
        fields["label"] = outcome.label
    _emit(**fields)

    if outcome.status == "failed":
        _note("서비스 재기동에 실패했습니다. 코드는 갱신됐고 프로세스는 옛 코드입니다.")
        return _finish("partial", disk_version, EXIT_RESTART_FAILED)

    if outcome.status == "skipped" and daemon_pid is not None:
        # 서비스로 관리되지 않는 데몬이 돌고 있다(수동 기동). 우리가 대신 못 세운다.
        _note(f"데몬(PID {daemon_pid})이 OS 스케줄러 밖에서 돌고 있어 재기동하지 못했습니다.")
        _note("  직접 재기동하세요: heartbeat stop 후 heartbeat start")
        return _finish("partial", disk_version, EXIT_DAEMON_UNMANAGED)

    return _finish("ok", disk_version, EXIT_OK)
