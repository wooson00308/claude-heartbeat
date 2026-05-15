"""heartbeat CLI의 스킬 디스커버리 회귀 방지.

이슈 #7 회귀: wheel install 후 `heartbeat skills`가 "사용 가능한 스킬 없음"을
출력하던 버그. 원인은 _get_package_skills_dir의 fallback이 site-packages/heartbeat/skills를
가리켰는데, pyproject.toml에서 skills를 top-level 패키지로 install해서 실제
위치는 site-packages/skills. import skills로 정확한 위치를 받게 fix.

실제 wheel install 회귀는 .github/workflows/ci.yml의 wheel-install-smoke 잡에서
별도로 검증. 이 unit test는 빠른 sanity (editable에서도 도는지).
"""

from __future__ import annotations

from heartbeat.cli import _get_package_skills_dir, _list_available_skills


def test_get_package_skills_dir_resolves_to_real_directory():
    """skills 디렉토리가 실제로 존재하는 path를 가리켜야 한다."""
    skills_dir = _get_package_skills_dir()
    assert skills_dir.exists()
    assert skills_dir.is_dir()


def test_list_available_skills_includes_dream():
    """패키지에 번들된 dream skill이 디스커버리되어야 한다."""
    available = _list_available_skills()
    assert "dream" in available, f"dream skill not found in: {available}"
