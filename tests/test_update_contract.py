"""`heartbeat update`의 출력 계약 (docs/config-contract.md).

앱이 사람 대신 부르는 명령이라 stdout 줄 자체가 계약이다. 여기서 지키는 것:
계약 줄 형식(key=value, 값에 공백 없음), step 순서, `result=` 줄이 마지막에
정확히 하나, 그리고 원인별 종료 코드.

실제 git pull / pip install / 서비스 재기동은 하지 않는다. `_git`·subprocess·
`restart_service`를 전부 대역으로 갈아끼운다.
"""

from __future__ import annotations

import re
import subprocess

import pytest

import heartbeat
from heartbeat import update
from heartbeat.service.base import RestartResult

DISK_VERSION = "9.9.9"

# 계약 줄 한 줄의 모양: 공백으로 나뉜 key=value, 값에 공백 없음.
CONTRACT_LINE = re.compile(r"^[a-z]+=\S+( [a-z]+=\S+)*$")


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _fake_git(table: dict):
    """`update._git` 대역. 키는 인자 전체("rev-parse HEAD") 또는 첫 인자("status").

    값이 예외면 raise한다 — git 부재·타임아웃 경로를 같은 표로 표현하려고.
    """
    def _run(_root, *args):
        response = table.get(" ".join(args), table.get(args[0], _cp(0)))
        if isinstance(response, BaseException) or (
            isinstance(response, type) and issubclass(response, BaseException)
        ):
            raise response
        return response
    return _run


def _git_table(overrides: dict | None = None) -> dict:
    """기본 시나리오: 깨끗한 트리 + upstream 있음 + 이미 최신."""
    table = {
        "status": _cp(0, ""),
        "rev-parse --abbrev-ref --symbolic-full-name @{u}": _cp(0, "origin/main\n"),
        "fetch": _cp(0),
        "rev-parse HEAD": _cp(0, "1111111\n"),
        "rev-parse @{u}": _cp(0, "1111111\n"),
        "merge-base": _cp(0),
        "merge": _cp(0),
    }
    table.update(overrides or {})
    return table


def _moved_head() -> dict:
    """HEAD가 upstream보다 뒤에 있어 fast-forward가 일어나는 표."""
    return _git_table({
        "rev-parse HEAD": _cp(0, "1111111aaaa\n"),
        "rev-parse @{u}": _cp(0, "2222222bbbb\n"),
    })


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """git 설치본 흉내. `_version_on_disk`가 실제로 읽을 파일까지 만들어 둔다."""
    source = tmp_path / "src" / "heartbeat" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text(f'__version__ = "{DISK_VERSION}"\n', encoding="utf-8")

    monkeypatch.setattr(update, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(update, "_git", _fake_git(_git_table()))
    monkeypatch.setattr(update, "_is_running", lambda: None)
    monkeypatch.setattr(update, "_load_state", lambda: {})
    monkeypatch.setattr(update, "_inside_daemon_tree", lambda pid: False)
    monkeypatch.setattr(
        update.service, "restart_service", lambda: RestartResult("ok", "restarted", "com.test"),
    )
    # 이 단계에 남은 subprocess.run 호출은 pip 하나뿐이다(_git은 위에서 대역).
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: _cp(0))
    return tmp_path


def _run(capsys) -> tuple[int, list[dict], str]:
    """update를 돌리고 (종료 코드, 파싱된 계약 줄, stderr)를 준다."""
    code = update.cmd_update(None)
    captured = capsys.readouterr()

    lines = [l for l in captured.out.strip().split("\n") if l]
    for line in lines:
        assert CONTRACT_LINE.match(line), f"계약 줄 형식 위반: {line!r}"
    parsed = [dict(pair.split("=", 1) for pair in line.split()) for line in lines]

    _assert_invariants(parsed, code)
    return code, parsed, captured.err


def _assert_invariants(parsed: list[dict], code: int) -> None:
    """모든 시나리오가 지켜야 하는 것 — 여기가 계약의 뼈대다."""
    results = [f for f in parsed if "result" in f]
    assert len(results) == 1, "result= 줄은 정확히 하나"
    assert results[0] is parsed[-1], "result= 줄은 항상 마지막"
    assert int(results[0]["exit"]) == code, "exit= 값은 프로세스 종료 코드와 같다"

    steps = [f["step"] for f in parsed if "step" in f]
    assert len(steps) <= 3
    order = ["repo", "deps", "service"]
    assert steps == [s for s in order if s in steps], f"step 순서 위반: {steps}"


def _step(parsed: list[dict], name: str) -> dict | None:
    return next((f for f in parsed if f.get("step") == name), None)


# --- 성공 경로 ---

def test_up_to_date_skips_deps_and_service(repo, capsys):
    """이미 최신이고 도는 데몬도 없으면 세 단계가 다 공회전하고 ok."""
    code, parsed, _ = _run(capsys)

    assert code == update.EXIT_OK
    assert _step(parsed, "repo") == {"step": "repo", "status": "ok", "detail": "up-to-date"}
    assert _step(parsed, "deps")["detail"] == "not-needed"
    assert _step(parsed, "service")["detail"] == "not-needed"
    assert parsed[-1] == {"result": "ok", "version": DISK_VERSION, "exit": "0"}


def test_updated_head_reinstalls_and_restarts(repo, monkeypatch, capsys):
    """HEAD가 움직이면 repo → deps → service가 다 돈다. 버전은 디스크 값."""
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))

    code, parsed, _ = _run(capsys)

    assert code == update.EXIT_OK
    repo_line = _step(parsed, "repo")
    assert repo_line["detail"] == "updated"
    # 짧은 해시가 from/to로 붙는다 (키 추가는 하위호환 변경)
    assert repo_line["from"] == "1111111"
    assert repo_line["to"] == "2222222"
    assert _step(parsed, "deps")["detail"] == "reinstalled"
    assert _step(parsed, "service") == {
        "step": "service", "status": "ok", "detail": "restarted", "label": "com.test",
    }
    # import된 값(pull 이전)이 아니라 pull 이후 디스크 값을 보고한다
    assert parsed[-1]["version"] == DISK_VERSION != heartbeat.__version__


def test_stale_daemon_restarts_without_pull(repo, monkeypatch, capsys):
    """코드는 최신인데 도는 프로세스가 옛 버전이면 재기동한다 (2026-08-05 사고 모양)."""
    monkeypatch.setattr(update, "_is_running", lambda: 4242)
    monkeypatch.setattr(update, "_load_state", lambda: {"_daemon": {"version": "0.0.1"}})

    code, parsed, _ = _run(capsys)

    assert code == update.EXIT_OK
    assert _step(parsed, "repo")["detail"] == "up-to-date"  # 당길 게 없었는데도
    assert _step(parsed, "service")["detail"] == "restarted"


def test_matching_daemon_version_skips_restart(repo, monkeypatch, capsys):
    """도는 데몬이 이미 디스크 버전이면 건드리지 않는다."""
    calls = []
    monkeypatch.setattr(update, "_is_running", lambda: 4242)
    monkeypatch.setattr(update, "_load_state", lambda: {"_daemon": {"version": DISK_VERSION}})
    monkeypatch.setattr(
        update.service, "restart_service", lambda: calls.append(1) or RestartResult("ok", "restarted"),
    )

    code, parsed, _ = _run(capsys)

    assert code == update.EXIT_OK
    assert _step(parsed, "service")["detail"] == "not-needed"
    assert calls == [], "재기동이 필요 없으면 서비스를 건드리지 않는다"


# --- repo 단계 실패: 원인별 종료 코드 ---

@pytest.mark.parametrize(
    "table, detail, expected_code",
    [
        (_git_table({"status": _cp(0, " M src/heartbeat/core.py\n")}),
         "dirty-tree", update.EXIT_DIRTY_TREE),
        (_git_table({"rev-parse --abbrev-ref --symbolic-full-name @{u}": _cp(128, "", "no upstream")}),
         "no-upstream", update.EXIT_NO_UPSTREAM),
        (_git_table({"fetch": _cp(1, "", "could not resolve host")}),
         "fetch-failed", update.EXIT_FETCH_FAILED),
        (_git_table({"status": subprocess.TimeoutExpired("git", 120)}),
         "fetch-failed", update.EXIT_FETCH_FAILED),
        (_git_table({"status": _cp(128, "", "not a git repository")}),
         "not-a-git-repo", update.EXIT_NOT_A_GIT_REPO),
        (_git_table({"status": FileNotFoundError("git")}),
         "git-missing", update.EXIT_NOT_A_GIT_REPO),
        (_git_table({"rev-parse HEAD": _cp(0, "1111111\n"),
                     "rev-parse @{u}": _cp(0, "2222222\n"),
                     "merge-base": _cp(1)}),
         "non-fast-forward", update.EXIT_NON_FAST_FORWARD),
        (_git_table({"rev-parse HEAD": _cp(0, "1111111\n"),
                     "rev-parse @{u}": _cp(0, "2222222\n"),
                     "merge": _cp(1, "", "merge failed")}),
         "merge-failed", update.EXIT_NON_FAST_FORWARD),
    ],
)
def test_repo_failures_map_to_exit_codes(repo, monkeypatch, capsys, table, detail, expected_code):
    monkeypatch.setattr(update, "_git", _fake_git(table))

    code, parsed, err = _run(capsys)

    assert code == expected_code
    assert _step(parsed, "repo") == {"step": "repo", "status": "failed", "detail": detail}
    assert parsed[-1]["result"] == "failed", "저장소 단계에서 멈추면 바뀐 게 없다"
    # 뒤 단계는 아예 나오지 않는다
    assert _step(parsed, "deps") is None
    assert _step(parsed, "service") is None
    assert err.strip(), "사람용 진단은 stderr로 나간다"


def test_dirty_tree_stops_before_fetch(repo, monkeypatch, capsys):
    """미커밋 변경이면 네트워크를 건드리기 전에 멈춘다."""
    seen = []

    def _recording_git(_root, *args):
        seen.append(args[0])
        if args[0] == "status":
            return _cp(0, " M src/heartbeat/core.py\n")
        return _cp(0)

    monkeypatch.setattr(update, "_git", _recording_git)

    code, _, _ = _run(capsys)

    assert code == update.EXIT_DIRTY_TREE
    assert "fetch" not in seen


def test_not_a_git_install_reports_running_version(monkeypatch, capsys):
    """wheel 설치본은 대상이 아니다. 디스크를 읽을 수 없으니 도는 버전을 싣는다."""
    monkeypatch.setattr(update, "_repo_root", lambda: None)

    code, parsed, err = _run(capsys)

    assert code == update.EXIT_NOT_A_GIT_REPO
    assert _step(parsed, "repo")["detail"] == "not-a-git-repo"
    assert parsed[-1] == {
        "result": "failed", "version": heartbeat.__version__, "exit": str(update.EXIT_NOT_A_GIT_REPO),
    }
    assert "pip install" in err


# --- deps 단계 실패 ---

@pytest.mark.parametrize(
    "pip_behavior, detail",
    [
        (lambda *a, **k: _cp(1, "", "ERROR: dependency conflict"), "pip-failed"),
        (lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pip", 600)), "pip-timeout"),
    ],
)
def test_deps_failure_is_partial(repo, monkeypatch, capsys, pip_behavior, detail):
    """저장소는 이미 움직였다 — 여기서 멈추면 코드와 설치 상태가 어긋난 채 남는다."""
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))
    monkeypatch.setattr(update.subprocess, "run", pip_behavior)

    code, parsed, err = _run(capsys)

    assert code == update.EXIT_DEPS_FAILED
    assert _step(parsed, "deps") == {"step": "deps", "status": "failed", "detail": detail}
    assert _step(parsed, "service") is None
    assert parsed[-1]["result"] == "partial"
    assert err.strip()


# --- service 단계 ---

def test_self_restart_blocked_does_not_restart(repo, monkeypatch, capsys):
    """데몬 자신의 트리 안에서 불리면 재기동하지 않는다. 죽으면 결과를 못 내보낸다."""
    calls = []
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))
    monkeypatch.setattr(update, "_is_running", lambda: 4242)
    monkeypatch.setattr(update, "_inside_daemon_tree", lambda pid: pid == 4242)
    monkeypatch.setattr(
        update.service, "restart_service", lambda: calls.append(1) or RestartResult("ok", "restarted"),
    )

    code, parsed, err = _run(capsys)

    assert code == update.EXIT_SELF_RESTART_BLOCKED
    assert _step(parsed, "service") == {
        "step": "service", "status": "skipped", "detail": "self-restart-blocked",
    }
    assert parsed[-1]["result"] == "partial"
    assert calls == [], "재기동을 시도조차 하지 않는다"
    assert "heartbeat update" in err  # 데몬 밖에서 다시 부르라는 안내


def test_restart_failure_is_partial(repo, monkeypatch, capsys):
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))
    monkeypatch.setattr(
        update.service, "restart_service",
        lambda: RestartResult("failed", "restart-failed", "com.test"),
    )

    code, parsed, err = _run(capsys)

    assert code == update.EXIT_RESTART_FAILED
    assert _step(parsed, "service")["status"] == "failed"
    assert parsed[-1]["result"] == "partial"
    assert err.strip()


def test_unmanaged_running_daemon_is_partial(repo, monkeypatch, capsys):
    """서비스 등록 없이 수동으로 뜬 데몬은 우리가 대신 세울 수 없다."""
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))
    monkeypatch.setattr(update, "_is_running", lambda: 4242)
    monkeypatch.setattr(
        update.service, "restart_service", lambda: RestartResult("skipped", "not-registered"),
    )

    code, parsed, err = _run(capsys)

    assert code == update.EXIT_DAEMON_UNMANAGED
    assert _step(parsed, "service")["detail"] == "not-registered"
    assert parsed[-1]["result"] == "partial"
    assert "4242" in err


def test_unregistered_service_without_daemon_is_ok(repo, monkeypatch, capsys):
    """등록도 없고 도는 데몬도 없으면 재기동할 대상이 없는 것뿐이다."""
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))
    monkeypatch.setattr(
        update.service, "restart_service", lambda: RestartResult("skipped", "not-registered"),
    )

    code, parsed, _ = _run(capsys)

    assert code == update.EXIT_OK
    assert parsed[-1]["result"] == "ok"


def test_service_label_with_spaces_stays_one_field(repo, monkeypatch, capsys):
    """OS가 주는 label에 공백이 있어도 계약 줄이 쪼개지지 않는다."""
    monkeypatch.setattr(update, "_git", _fake_git(_moved_head()))
    monkeypatch.setattr(
        update.service, "restart_service",
        lambda: RestartResult("ok", "restarted", "My Heartbeat Agent"),
    )

    code, parsed, _ = _run(capsys)  # _run이 계약 줄 정규식을 이미 검사한다

    assert code == update.EXIT_OK
    assert _step(parsed, "service")["label"] == "My_Heartbeat_Agent"


def test_exit_codes_are_disjoint():
    """앱이 종료 코드로 원인을 구분한다 — 값이 겹치면 그 구분이 무너진다."""
    codes = [
        update.EXIT_OK,
        update.EXIT_NOT_A_GIT_REPO, update.EXIT_DIRTY_TREE, update.EXIT_NON_FAST_FORWARD,
        update.EXIT_FETCH_FAILED, update.EXIT_NO_UPSTREAM,
        update.EXIT_DEPS_FAILED,
        update.EXIT_RESTART_FAILED, update.EXIT_DAEMON_UNMANAGED, update.EXIT_SELF_RESTART_BLOCKED,
    ]
    assert len(set(codes)) == len(codes)
