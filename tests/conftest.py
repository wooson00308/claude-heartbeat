"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_projects_dir(tmp_path, monkeypatch) -> Path:
    """Replace PROJECTS_DIR with a tmp dir for the duration of the test.

    All dream.* modules import get_project_dir / PROJECTS_DIR from .paths,
    so monkeypatching the source module is enough.
    """
    from skills.dream import paths

    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def slug() -> str:
    return "-test-project"


@pytest.fixture
def project_dir(isolated_projects_dir, slug) -> Path:
    """Create the project + memory dir and return the project root."""
    pdir = isolated_projects_dir / slug
    (pdir / "memory").mkdir(parents=True)
    return pdir
