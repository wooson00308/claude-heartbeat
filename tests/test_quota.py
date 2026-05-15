"""슬라이딩 윈도우 quota (max_per) 회귀.

v0.7.0 신규 기능: HEARTBEAT.md의 max_per 필드 + run_job 진입 전 quota 체크.
- 파서: _parse_max_per('5/24h') → (5, 86400)
- 잡 dict에 max_per: (count, window_sec) 또는 None
- _quota_exceeded: 윈도우 밖 timestamp 정리 후 count 비교
- run_job: quota 초과 시 claude 안 깨움 + last_result="quota_skipped"
- max_per 없는 잡은 영향 0 (하위호환)
"""

from __future__ import annotations

import json
import subprocess
import time
from io import StringIO

import pytest

from heartbeat import core


# --- _parse_max_per ---

def test_parse_max_per_valid_formats():
    assert core._parse_max_per("5/24h") == (5, 86400)
    assert core._parse_max_per("3/1d") == (3, 86400)
    assert core._parse_max_per("10/1h") == (10, 3600)
    assert core._parse_max_per("1/30m") == (1, 1800)
    # 공백 허용
    assert core._parse_max_per("  5 / 24h  ") == (5, 86400)


def test_parse_max_per_invalid_returns_none():
    assert core._parse_max_per("5") is None              # 슬래시 없음
    assert core._parse_max_per("/24h") is None           # count 없음
    assert core._parse_max_per("abc/24h") is None        # count non-int
    assert core._parse_max_per("0/24h") is None          # 0은 의미 없음
    assert core._parse_max_per("-3/24h") is None         # 음수


def test_parse_max_per_unknown_window_unit_falls_back_to_default():
    """window 단위 파싱 실패 시 _parse_interval이 1h(3600s)로 fallback. 의도된 동작."""
    assert core._parse_max_per("5/garbage") == (5, 3600)


# --- parse_heartbeat_md max_per 필드 ---

def test_parse_heartbeat_md_max_per_field(tmp_path, monkeypatch):
    f = tmp_path / "HEARTBEAT.md"
    f.write_text(
        "## quota-job\n"
        "- slug: -x\n"
        "- prompt: do thing\n"
        "- max_per: 5/24h\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "HEARTBEAT_FILE", f)

    _, jobs = core.parse_heartbeat_md()
    job = jobs[0]
    assert job["max_per"] == (5, 86400)


def test_parse_heartbeat_md_no_max_per_defaults_to_none(tmp_path, monkeypatch):
    """기존 사용자 잡(max_per 없음)은 max_per=None — 하위호환 보장."""
    f = tmp_path / "HEARTBEAT.md"
    f.write_text("## j\n- slug: -x\n- prompt: y\n", encoding="utf-8")
    monkeypatch.setattr(core, "HEARTBEAT_FILE", f)

    _, jobs = core.parse_heartbeat_md()
    assert jobs[0]["max_per"] is None


# --- _quota_exceeded ---

def test_quota_not_exceeded_when_no_max_per():
    assert core._quota_exceeded({}, None) is False
    assert core._quota_exceeded({"recent_runs": [time.time()] * 100}, None) is False


def test_quota_exceeded_when_window_full():
    now = time.time()
    state = {"recent_runs": [now - 1, now - 100, now - 1000]}  # 3개 다 윈도우 안
    assert core._quota_exceeded(state, (3, 3600)) is True
    assert state["recent_runs"] == [now - 1, now - 100, now - 1000]  # 정리 후에도 3개 유지


def test_quota_not_exceeded_when_old_entries_drop_off():
    """윈도우 밖 timestamp는 정리되고, 윈도우 안 항목 수가 count 미만이면 통과."""
    now = time.time()
    state = {"recent_runs": [now - 10000, now - 9000, now - 100]}  # 첫 두 개는 1h 윈도우 밖
    assert core._quota_exceeded(state, (3, 3600)) is False
    # 정리 후 윈도우 안 1개만 남음
    assert state["recent_runs"] == [now - 100]


# --- run_job quota 통합 ---

@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """STATE_FILE / LOG_DIR을 tmp로 격리."""
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(core, "_notify", lambda *a, **k: None)
    # CWD 체크가 실패하지 않도록 _slug_to_cwd를 tmp로 강제
    monkeypatch.setattr(core, "_slug_to_cwd", lambda slug: tmp_path)
    return tmp_path


def _job_with_quota(max_per: tuple[int, int] | None) -> dict:
    return {
        "name": "quota-job",
        "slug": "-test",
        "prompt": "irrelevant",
        "timeout": 10,
        "condition": "",
        "notify": "none",
        "max_per": max_per,
    }


def test_run_job_quota_exceeded_skips_claude(isolated_state, monkeypatch):
    """quota 다 채워있으면 claude.Popen 호출 0건 + last_result=quota_skipped."""
    now = time.time()

    def _fail(*a, **k):
        pytest.fail("claude must NOT be invoked when quota is full")

    monkeypatch.setattr(core.subprocess, "Popen", _fail)

    state = {"quota-job": {"recent_runs": [now - 10, now - 20, now - 30]}}  # 윈도우 가득
    result = core.run_job(_job_with_quota((3, 3600)), state)

    assert result is False
    assert state["quota-job"]["last_result"] == "quota_skipped"
    assert state["quota-job"]["last_run"]


def test_run_job_quota_exceeded_persists_to_disk(isolated_state, monkeypatch):
    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **k: pytest.fail("claude invoked"))

    now = time.time()
    state = {"quota-job": {"recent_runs": [now - 10] * 5}}
    core.run_job(_job_with_quota((5, 3600)), state)

    persisted = json.loads((isolated_state / "state.json").read_text(encoding="utf-8"))
    assert persisted["quota-job"]["last_result"] == "quota_skipped"


def test_run_job_records_timestamp_when_claude_invoked(isolated_state, monkeypatch):
    """claude 호출되면 recent_runs에 timestamp append."""

    class _FakeProc:
        def __init__(self, *a, **k):
            self.pid = 99999
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **k: _FakeProc())

    state: dict = {}
    result = core.run_job(_job_with_quota((5, 3600)), state)

    assert result is True
    assert len(state["quota-job"]["recent_runs"]) == 1
    assert state["quota-job"]["last_result"] == "success"


def test_run_job_no_max_per_does_not_track_recent_runs(isolated_state, monkeypatch):
    """max_per 없는 잡은 recent_runs 박지 않는다 — 기존 사용자 영향 0."""

    class _FakeProc:
        pid = 99999
        returncode = 0
        def communicate(self, timeout=None):
            return ("", "")

    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **k: _FakeProc())

    state: dict = {}
    result = core.run_job(_job_with_quota(None), state)

    assert result is True
    assert "recent_runs" not in state["quota-job"]


def test_run_job_quota_window_slides(isolated_state, monkeypatch):
    """윈도우 밖 timestamp 빠지고 새 잡이 들어가는 시나리오."""

    class _FakeProc:
        pid = 99999
        returncode = 0
        def communicate(self, timeout=None):
            return ("", "")

    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **k: _FakeProc())

    now = time.time()
    # window=1h, 안에 2개, 밖에 3개
    state = {"quota-job": {"recent_runs": [now - 7200, now - 5000, now - 4000, now - 100, now - 50]}}
    result = core.run_job(_job_with_quota((3, 3600)), state)

    # 윈도우 밖 3개 빠지고 안에 2개 + 새로 1개 = 3개
    assert result is True
    assert len(state["quota-job"]["recent_runs"]) == 3
