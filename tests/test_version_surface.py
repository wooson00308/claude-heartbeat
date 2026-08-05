"""버전 표면과 state.json의 `_daemon` (docs/config-contract.md).

소비자 앱이 "설치된 코드는 몇 버전인가"와 "지금 도는 프로세스는 몇 버전인가"를
따로 물을 수 있어야 한다. 두 답이 갈리는 상태(코드는 갱신됐는데 프로세스는 옛
코드)가 2026-08-05 사고의 모양이고, 그걸 보이게 하는 것이 이 표면의 존재 이유다.
"""

from __future__ import annotations

import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

import heartbeat
from heartbeat import cli, core

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_LINE = re.compile(r"^heartbeat \d+\.\d+\.\d+\S*$")


# --- 버전 출력 ---

def test_version_flag_prints_contract_line(monkeypatch, capsys):
    """`heartbeat --version`은 `heartbeat <X.Y.Z>` 한 줄 + 종료 코드 0."""
    monkeypatch.setattr(sys, "argv", ["heartbeat", "--version"])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"heartbeat {heartbeat.__version__}"
    assert VERSION_LINE.match(out)


def test_version_subcommand_matches_flag(monkeypatch, capsys):
    """서브커맨드로 찾는 손도 있다. 출력은 --version과 같은 줄이어야 한다."""
    monkeypatch.setattr(sys, "argv", ["heartbeat", "version"])

    cli.main()

    out = capsys.readouterr().out.strip()
    assert out == f"heartbeat {heartbeat.__version__}"


def test_version_is_parseable_from_last_field():
    """계약이 안내하는 파싱 방법(마지막 공백 뒤)이 실제로 통한다."""
    assert f"heartbeat {heartbeat.__version__}".rsplit(" ", 1)[1] == heartbeat.__version__


# --- 단일 원천 ---

def test_pyproject_reads_version_from_init():
    """빌드 메타데이터가 __version__을 읽어 간다 — 손으로 두 곳을 맞추지 않는다."""
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" in data["project"]["dynamic"]
    assert "version" not in data["project"], "정적 version이 남아 있으면 원천이 둘이 된다"
    assert data["tool"]["hatch"]["version"]["path"] == "src/heartbeat/__init__.py"


def test_core_and_cli_share_one_version():
    """데몬이 기록하는 버전과 CLI가 출력하는 버전이 같은 상수여야 한다."""
    assert core.__version__ is heartbeat.__version__
    assert cli.__version__ is heartbeat.__version__


# --- state.json의 `_daemon` ---

@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """STATE_FILE을 tmp_path로 격리. 실제 ~/.claude/heartbeat/ 건드리지 않음."""
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state.json")
    return tmp_path / "state.json"


def test_record_daemon_start_writes_identity(state_file):
    """기동 시 버전·pid·시각이 남는다. 앱이 도는 프로세스의 정체를 읽을 원천."""
    import os

    core._record_daemon_start({})

    entry = core._load_state()[core.DAEMON_STATE_KEY]
    assert entry["version"] == heartbeat.__version__
    assert entry["pid"] == os.getpid()
    datetime.fromisoformat(entry["started_at"])  # ISO 8601로 파싱된다


def test_record_daemon_start_preserves_jobs(state_file):
    """잡 이력과 같은 파일에 살지만 서로를 지우지 않는다."""
    state = {"job-a": {"last_run": "2026-08-05T00:00:00", "last_result": "success"}}

    core._record_daemon_start(state)

    loaded = core._load_state()
    assert loaded["job-a"]["last_result"] == "success"
    assert core.DAEMON_STATE_KEY in loaded


def test_record_daemon_start_overwrites_previous(state_file):
    """재기동하면 항목이 쌓이지 않고 갈린다."""
    state = {}
    core._record_daemon_start(state)
    state[core.DAEMON_STATE_KEY]["version"] = "0.0.1"  # 옛 데몬이 남긴 값 흉내

    core._record_daemon_start(state)

    assert core._load_state()[core.DAEMON_STATE_KEY]["version"] == heartbeat.__version__


def test_iter_job_states_skips_reserved_keys():
    """밑줄로 시작하는 최상위 키는 잡이 아니다. 예약 키가 늘어도 소비자는 안 깨진다."""
    state = {
        "job-a": {"last_result": "success"},
        core.DAEMON_STATE_KEY: {"version": "0.8.0"},
        "_future_reserved": {"whatever": 1},
        "job-b": {"last_result": "failure"},
    }

    assert [name for name, _ in core.iter_job_states(state)] == ["job-a", "job-b"]


def test_status_lists_jobs_without_daemon_key(monkeypatch, capsys, tmp_path):
    """`heartbeat status`가 `_daemon`을 잡인 것처럼 늘어놓지 않는다."""
    monkeypatch.setattr(sys, "argv", ["heartbeat", "status"])
    monkeypatch.setattr(cli, "_is_running", lambda: 4242)
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    monkeypatch.setattr(cli, "_load_state", lambda: {
        "job-a": {"last_run": "2026-08-05T00:00:00", "last_result": "success", "last_duration": 3},
        core.DAEMON_STATE_KEY: {"version": "0.0.1", "pid": 4242},
    })

    cli.main()

    out = capsys.readouterr().out
    assert "[job-a]" in out
    assert f"[{core.DAEMON_STATE_KEY}]" not in out
    # 도는 데몬과 설치본이 갈린 상태가 눈에 보여야 한다
    assert "v0.0.1" in out
    assert f"v{heartbeat.__version__}" in out
