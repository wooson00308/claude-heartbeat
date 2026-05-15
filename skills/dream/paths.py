"""Shared paths for dream skill modules."""

from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def get_project_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug
