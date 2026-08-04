"""heartbeat migrate — HEARTBEAT.md를 slug별 jobs.d 파일로 분리 (v0.8.0)."""

from __future__ import annotations

import argparse

import pytest

from heartbeat import cli, core


@pytest.fixture
def homes(tmp_path, monkeypatch):
    heartbeat_md = tmp_path / "HEARTBEAT.md"
    jobs_dir = tmp_path / "jobs.d"
    monkeypatch.setattr(cli, "HEARTBEAT_FILE", heartbeat_md)
    monkeypatch.setattr(core, "HEARTBEAT_FILE", heartbeat_md)
    monkeypatch.setattr(core, "JOBS_DIR", jobs_dir)
    return heartbeat_md, jobs_dir


SAMPLE = (
    "# HEARTBEAT\n\n"
    "- tick: 5m\n\n"
    "<!-- some-tool:managed:start -->\n"
    "## job-a1\n- slug: -proj-a\n- prompt: a one\n\n"
    "## job-b1\n- slug: -proj-b\n- prompt: b one\n"
    "<!-- some-tool:managed:end -->\n\n"
    "## job-a2\n- slug: -proj-a\n- prompt: a two\n\n"
    "## no-slug\n- prompt: stays\n"
)


def _migrate(dry_run=False):
    cli.cmd_migrate(argparse.Namespace(dry_run=dry_run))


def test_migrate_splits_jobs_by_slug(homes):
    heartbeat_md, jobs_dir = homes
    heartbeat_md.write_text(SAMPLE, encoding="utf-8")

    _migrate()

    file_a = (jobs_dir / "-proj-a.md").read_text(encoding="utf-8")
    file_b = (jobs_dir / "-proj-b.md").read_text(encoding="utf-8")
    assert "## job-a1" in file_a and "## job-a2" in file_a
    assert "## job-b1" in file_b

    # 파서가 이주 결과를 그대로 읽는다. 잡 이름과 slug가 이주 전과 같다.
    # (no-slug 잡은 slug가 없어 파서 필터에서 떨어진다 — 이주 전과도 같은 동작이다.)
    _, jobs = core.parse_heartbeat_md()
    assert {j["name"]: j["slug"] for j in jobs} == {
        "job-a1": "-proj-a",
        "job-a2": "-proj-a",
        "job-b1": "-proj-b",
    }


def test_migrate_keeps_globals_comments_and_slugless_blocks(homes):
    heartbeat_md, _ = homes
    heartbeat_md.write_text(SAMPLE, encoding="utf-8")

    _migrate()

    remainder = heartbeat_md.read_text(encoding="utf-8")
    assert "- tick: 5m" in remainder
    # 외부 도구의 관리 마커는 짝이 맞은 채로 남는다. 옮기면 마커 한 짝만 남아
    # 그 도구가 파일 손상으로 판정한다.
    assert "<!-- some-tool:managed:start -->" in remainder
    assert "<!-- some-tool:managed:end -->" in remainder
    assert "## no-slug" in remainder
    assert "## job-a1" not in remainder

    # 백업이 원본 내용 그대로 남는다.
    backups = list(heartbeat_md.parent.glob("HEARTBEAT.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE


def test_migrate_skips_slug_whose_target_exists(homes):
    heartbeat_md, jobs_dir = homes
    heartbeat_md.write_text(SAMPLE, encoding="utf-8")
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "-proj-a.md").write_text("## existing\n- prompt: keep\n", encoding="utf-8")

    _migrate()

    # 기존 파일은 덮어쓰지 않고, 그 slug의 잡은 HEARTBEAT.md에 남는다.
    assert "existing" in (jobs_dir / "-proj-a.md").read_text(encoding="utf-8")
    remainder = heartbeat_md.read_text(encoding="utf-8")
    assert "## job-a1" in remainder and "## job-a2" in remainder
    assert "## job-b1" not in remainder


def test_migrate_dry_run_writes_nothing(homes):
    heartbeat_md, jobs_dir = homes
    heartbeat_md.write_text(SAMPLE, encoding="utf-8")

    _migrate(dry_run=True)

    assert not jobs_dir.exists()
    assert heartbeat_md.read_text(encoding="utf-8") == SAMPLE
    assert not list(heartbeat_md.parent.glob("*.bak-*"))


def test_migrate_without_file_is_a_noop(homes, capsys):
    _migrate()
    assert "없음" in capsys.readouterr().out
