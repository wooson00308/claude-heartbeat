"""heartbeat.core 단위 테스트.

HEARTBEAT.md 파서, interval 파싱, slug → cwd 변환,
그리고 v0.2.1 hotfix(condition 미충족 시 last_run 갱신) 회귀 방지.
"""

from __future__ import annotations

import json
import sys

import pytest

from heartbeat import core  # noqa: E402


def test_parse_interval_units():
    assert core._parse_interval("30s") == 30
    assert core._parse_interval("5m") == 300
    assert core._parse_interval("2h") == 7200
    assert core._parse_interval("1d") == 86400


def test_parse_interval_fallback_to_int():
    assert core._parse_interval("120") == 120


def test_parse_interval_invalid_returns_default():
    assert core._parse_interval("garbage") == 3600


def test_parse_heartbeat_md_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "HEARTBEAT_FILE", tmp_path / "no-such.md")
    config, jobs = core.parse_heartbeat_md()
    assert config == {"tick": 60}
    assert jobs == []


def test_parse_heartbeat_md_global_and_jobs(tmp_path, monkeypatch):
    f = tmp_path / "HEARTBEAT.md"
    f.write_text(
        "# HEARTBEAT\n\n"
        "- tick: 30s\n\n"
        "## job-a\n"
        "- slug: -Users-test\n"
        "- prompt: do thing\n"
        "- interval: 2h\n"
        "- timeout: 5m\n"
        "- notify: failure\n\n"
        "## job-b\n"
        "- slug: -Users-other\n"
        "- prompt: another\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "HEARTBEAT_FILE", f)

    config, jobs = core.parse_heartbeat_md()
    assert config["tick"] == 30
    assert len(jobs) == 2

    job_a = next(j for j in jobs if j["name"] == "job-a")
    assert job_a["slug"] == "-Users-test"
    assert job_a["prompt"] == "do thing"
    assert job_a["interval"] == 7200
    assert job_a["timeout"] == 300
    assert job_a["notify"] == "failure"

    job_b = next(j for j in jobs if j["name"] == "job-b")
    assert job_b["interval"] == 3600  # default 1h
    assert job_b["notify"] == "all"


def test_parse_heartbeat_md_skips_jobs_without_slug_or_prompt(tmp_path, monkeypatch):
    f = tmp_path / "HEARTBEAT.md"
    f.write_text(
        "## broken\n- slug:\n- prompt:\n\n"
        "## good\n- slug: -x\n- prompt: y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "HEARTBEAT_FILE", f)
    _, jobs = core.parse_heartbeat_md()
    assert [j["name"] for j in jobs] == ["good"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_slug_to_cwd_simple(tmp_path):
    """tmp_path 아래 폴더를 만들고 슬러그가 그걸 가리키는지 검증.

    _slug_to_cwd는 '/' root부터 greedy match라 tmp_path가 root에 있어야 한다.
    macOS/Linux 임시 디렉토리는 보통 /var/folders/... 또는 /tmp/...
    """
    # tmp_path = /private/var/folders/.../pytest-...
    # slug = "-private-var-folders-..." 형태로 변환
    parts = str(tmp_path).lstrip("/").split("/")
    slug = "-" + "-".join(parts)

    # 마지막 디렉토리에 sentinel 파일을 둬서 진짜 같은 곳을 가리키는지 확인
    (tmp_path / "sentinel.txt").write_text("ok", encoding="utf-8")

    resolved = core._slug_to_cwd(slug)
    assert (resolved / "sentinel.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_slug_to_cwd_handles_hyphenated_folder(tmp_path):
    """폴더명에 하이픈이 있는 케이스 (예: dr2-unity). greedy longest-match."""
    inner = tmp_path / "dr2-unity"
    inner.mkdir()
    (inner / "marker.txt").write_text("ok", encoding="utf-8")

    parts = str(inner).lstrip("/").split("/")
    slug = "-" + "-".join(parts)

    resolved = core._slug_to_cwd(slug)
    assert (resolved / "marker.txt").read_text(encoding="utf-8") == "ok"


# --- v0.2.1 hotfix: condition 미충족 시 last_run 갱신 ---
#
# 갱신하지 않으면 interval 만료된 잡이 매 tick마다 condition 재체크를 반복하면서
# 로그 폭주 + 외부 프로세스 호출이 누적된다. 이 회귀가 다시 들어오면 두 테스트가 빨간불.

@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """STATE_FILE을 tmp_path로 격리. 실제 ~/.claude/heartbeat/ 건드리지 않음."""
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(core, "_notify", lambda *a, **k: None)
    return tmp_path


def _make_job(condition: str = "false") -> dict:
    return {
        "name": "test-job",
        "slug": "-test",
        "prompt": "irrelevant",
        "timeout": 10,
        "condition": condition,
        "notify": "none",
    }


def test_run_job_condition_failed_updates_state_and_skips_claude(isolated_state, monkeypatch):
    """condition 불충족 시 state 갱신 + claude 미호출 + skipped 마킹."""
    monkeypatch.setattr(core, "_check_condition", lambda job: False)

    def _no_call(*args, **kwargs):
        raise RuntimeError("claude must NOT be invoked when condition fails")

    monkeypatch.setattr(core.subprocess, "Popen", _no_call)

    state: dict = {}
    result = core.run_job(_make_job(), state)

    assert result is False
    assert "test-job" in state
    assert state["test-job"]["last_result"] == "skipped"
    assert state["test-job"]["last_run"]  # ISO timestamp string, non-empty
    assert state["test-job"]["last_duration"] == 0


def test_run_job_condition_failed_persists_to_disk(isolated_state, monkeypatch):
    """state.json에 실제로 저장돼서 데몬 재시작 후에도 last_run 유지.

    이게 깨지면 재시작 후 첫 tick에서 condition 재체크 폭주가 다시 시작된다.
    """
    monkeypatch.setattr(core, "_check_condition", lambda job: False)
    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **k: pytest.fail("claude invoked"))

    state: dict = {}
    core.run_job(_make_job(), state)

    state_file = isolated_state / "state.json"
    assert state_file.exists()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["test-job"]["last_result"] == "skipped"
    assert persisted["test-job"]["last_run"]


# --- v0.6.0: condition 검사 fail-closed (issue #10) ---
#
# 이전엔 timeout / exception 시 True 반환(fail-open)이라 dream-prep 깨지면
# 매 tick마다 claude를 깨워서 토큰 비용 누적. 이제 둘 다 False (skip).

def test_check_condition_timeout_returns_false(monkeypatch):
    """condition 명령이 타임아웃 → fail-closed (skip)."""
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "x", timeout=10)

    monkeypatch.setattr(core.subprocess, "run", _timeout)
    assert core._check_condition({"name": "j", "condition": "sleep 999"}) is False


def test_check_condition_exception_returns_false(monkeypatch):
    """condition 명령에서 예외(예: bin 없음, OSError) → fail-closed."""
    def _boom(*args, **kwargs):
        raise FileNotFoundError("dream-prep: command not found")

    monkeypatch.setattr(core.subprocess, "run", _boom)
    assert core._check_condition({"name": "j", "condition": "dream-prep status"}) is False


def test_check_condition_empty_condition_returns_true():
    """condition 미지정 시 무조건 통과 (디폴트 동작 그대로)."""
    assert core._check_condition({"name": "j", "condition": ""}) is True
    assert core._check_condition({"name": "j"}) is True


def test_check_condition_zero_exit_returns_true(monkeypatch):
    """exit 0 → True (정상 흐름이 깨지지 않았는지)."""
    monkeypatch.setattr(
        core.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    assert core._check_condition({"name": "j", "condition": "true"}) is True


def test_check_condition_nonzero_exit_returns_false(monkeypatch):
    """exit non-zero → False (정상 흐름이 깨지지 않았는지)."""
    monkeypatch.setattr(
        core.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""),
    )
    assert core._check_condition({"name": "j", "condition": "false"}) is False


# `subprocess` 모듈은 fixture에서 monkeypatch하지만 ImportError 방지용 top-level import.
import subprocess  # noqa: E402, I001  (intentionally placed here for the tests above)
