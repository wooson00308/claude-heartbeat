"""jobs.d 프로젝트별 잡 파일 (v0.8.0 P0-A).

여러 도구가 HEARTBEAT.md 한 파일을 나눠 쓰다 서로의 잡을 지우는 사고를
파일 단위 소유로 막는다. 병합 우선순위: jobs.d > HEARTBEAT.md,
jobs.d끼리는 정렬 순서상 먼저 읽은 파일이 이긴다.
"""

from __future__ import annotations

import pytest

from heartbeat import core


@pytest.fixture
def homes(tmp_path, monkeypatch):
    heartbeat_md = tmp_path / "HEARTBEAT.md"
    jobs_dir = tmp_path / "jobs.d"
    monkeypatch.setattr(core, "HEARTBEAT_FILE", heartbeat_md)
    monkeypatch.setattr(core, "JOBS_DIR", jobs_dir)
    return heartbeat_md, jobs_dir


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_jobs_d_merges_with_heartbeat_md(homes):
    heartbeat_md, jobs_dir = homes
    _write(heartbeat_md, "- tick: 30s\n\n## job-a\n- slug: -proj-a\n- prompt: do a\n")
    _write(jobs_dir / "-proj-b.md", "## job-b\n- prompt: do b\n")

    config, jobs = core.parse_heartbeat_md()

    assert config["tick"] == 30
    assert {j["name"] for j in jobs} == {"job-a", "job-b"}
    # slug 줄이 없어도 파일 이름이 소속을 정한다.
    job_b = next(j for j in jobs if j["name"] == "job-b")
    assert job_b["slug"] == "-proj-b"


def test_jobs_d_slug_is_forced_from_filename(homes):
    _, jobs_dir = homes
    _write(jobs_dir / "-proj-b.md", "## job-b\n- slug: -other-project\n- prompt: do b\n")

    _, jobs = core.parse_heartbeat_md()

    assert jobs[0]["slug"] == "-proj-b"


def test_jobs_d_overrides_heartbeat_md_on_name_collision(homes):
    heartbeat_md, jobs_dir = homes
    _write(heartbeat_md, "## dup\n- slug: -proj-a\n- prompt: old\n- interval: 1h\n")
    _write(jobs_dir / "-proj-a.md", "## dup\n- prompt: new\n- interval: 5m\n")

    _, jobs = core.parse_heartbeat_md()

    assert len(jobs) == 1
    assert jobs[0]["prompt"] == "new"
    assert jobs[0]["interval"] == 300


def test_jobs_d_first_sorted_file_wins_on_cross_file_collision(homes):
    _, jobs_dir = homes
    _write(jobs_dir / "-proj-a.md", "## dup\n- prompt: from a\n")
    _write(jobs_dir / "-proj-b.md", "## dup\n- prompt: from b\n")

    _, jobs = core.parse_heartbeat_md()

    assert len(jobs) == 1
    assert jobs[0]["slug"] == "-proj-a"
    assert jobs[0]["prompt"] == "from a"


def test_jobs_d_globals_are_ignored(homes):
    heartbeat_md, jobs_dir = homes
    _write(heartbeat_md, "- tick: 45s\n")
    _write(jobs_dir / "-proj-a.md", "- tick: 5s\n\n## job-a\n- prompt: do a\n")

    config, jobs = core.parse_heartbeat_md()

    assert config["tick"] == 45
    assert len(jobs) == 1


def test_jobs_d_job_without_prompt_is_filtered(homes):
    _, jobs_dir = homes
    _write(jobs_dir / "-proj-a.md", "## broken\n- interval: 5m\n\n## good\n- prompt: y\n")

    _, jobs = core.parse_heartbeat_md()

    assert [j["name"] for j in jobs] == ["good"]


def test_jobs_d_ignores_non_file_entries(homes):
    _, jobs_dir = homes
    (jobs_dir / "weird.md").mkdir(parents=True)
    _write(jobs_dir / "-proj-a.md", "## job-a\n- prompt: do a\n")

    _, jobs = core.parse_heartbeat_md()

    assert [j["name"] for j in jobs] == ["job-a"]


def test_missing_jobs_d_dir_keeps_heartbeat_md_only(homes):
    heartbeat_md, _ = homes
    _write(heartbeat_md, "## job-a\n- slug: -proj-a\n- prompt: do a\n")

    _, jobs = core.parse_heartbeat_md()

    assert [j["name"] for j in jobs] == ["job-a"]
