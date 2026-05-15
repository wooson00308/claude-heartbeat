"""heartbeat.core 파싱 단위 테스트.

HEARTBEAT.md 파서, interval 파싱, slug → cwd 변환.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# core가 fcntl/signal/fork에 직접 의존하진 않지만, 같은 패키지 import 시점에
# 플랫폼 분기 없이 동작해야 한다. 윈도우 sanity check 차원으로 import만 통과시킴.
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
