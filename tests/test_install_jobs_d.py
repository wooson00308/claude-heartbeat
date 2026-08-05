"""heartbeat install의 잡 등록이 jobs.d/<slug>.md로 간다 (v0.8.0)."""

from __future__ import annotations

import argparse

import pytest

from heartbeat import cli, core


@pytest.fixture
def fake_skill(tmp_path, monkeypatch):
    skill_dir = tmp_path / "pkg-skills" / "myskill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# myskill\n", encoding="utf-8")
    (skill_dir / "heartbeat.md").write_text(
        "## myskill-{slug_short}\n- slug: {slug}\n- prompt: /myskill\n- interval: 1h\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_get_package_skills_dir", lambda: tmp_path / "pkg-skills")
    monkeypatch.setattr(cli, "SKILLS_DIR", tmp_path / "installed-skills")
    monkeypatch.setattr(cli, "HEARTBEAT_FILE", tmp_path / "HEARTBEAT.md")
    monkeypatch.setattr(core, "JOBS_DIR", tmp_path / "jobs.d")
    return tmp_path


def _install(slug="-proj-a"):
    cli.cmd_install(argparse.Namespace(skill="myskill", slug=slug))


def test_install_writes_job_into_jobs_d_not_heartbeat_md(fake_skill):
    _install()

    target = fake_skill / "jobs.d" / "-proj-a.md"
    assert "## myskill-a" in target.read_text(encoding="utf-8")
    assert not (fake_skill / "HEARTBEAT.md").exists()


def test_install_is_idempotent(fake_skill):
    _install()
    _install()

    content = (fake_skill / "jobs.d" / "-proj-a.md").read_text(encoding="utf-8")
    assert content.count("## myskill-a") == 1


def test_install_skips_job_already_in_legacy_heartbeat_md(fake_skill):
    (fake_skill / "HEARTBEAT.md").write_text("## myskill-a\n- prompt: old\n", encoding="utf-8")

    _install()

    # 레거시에 있는 이름을 jobs.d에 또 쓰면 파싱이 jobs.d를 우선해 레거시 정의가
    # 조용히 무시되므로, 등록 자체를 건너뛴다.
    assert not (fake_skill / "jobs.d" / "-proj-a.md").exists()
