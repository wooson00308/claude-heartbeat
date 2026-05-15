"""_slug_to_cwd 함정 케이스 회귀.

Phase 1 리뷰 미반영 항목: greedy longest-first match는 폴더명에 하이픈이 많은
환경에서 잘못된 매치를 만들 수 있다. 예를 들어 `/a-b/c-d` (의도)와 `/a-b-c`
(혼동) 두 디렉토리가 시스템에 동시에 존재하면 현재 구현은 longer 단일 매치
(`a-b-c`)를 먼저 잡아 `/a-b-c/d`로 해석한다.

이 한계 자체를 즉시 고치진 않는다 (cwd 존재 검증을 추가하려면 별도 알고리즘
필요). 다만 동작을 명시적으로 박아둬서 미래에 algorithm을 바꾸면 이 테스트가
의도적으로 깨지게 한다.
"""

from __future__ import annotations

import sys

import pytest

from heartbeat import core


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_slug_to_cwd_ambiguity_picks_longer_match(tmp_path):
    """`/a-b/c-d`와 `/a-b-c` 동시 존재 시 greedy longest-first는 `a-b-c` 선택.

    의도는 `/a-b/c-d`였을 수 있지만 현재 구현의 한계 — 잘못된 매치를 만든다.
    """
    correct = tmp_path / "a-b" / "c-d"
    correct.mkdir(parents=True)
    (correct / "marker_correct.txt").write_text("correct", encoding="utf-8")

    decoy = tmp_path / "a-b-c"
    decoy.mkdir()
    (decoy / "marker_decoy.txt").write_text("decoy", encoding="utf-8")

    # tmp_path를 root로 한 슬러그 생성
    parts = str(tmp_path).lstrip("/").split("/")
    slug = "-" + "-".join(parts) + "-a-b-c-d"

    resolved = core._slug_to_cwd(slug)

    # greedy longest-first: parts 끝에서 "a-b-c"가 "a-b"보다 먼저 시도되고
    # /tmp_path/a-b-c가 존재하므로 매치. 그 다음 "d"는 fallback path concat.
    assert resolved == decoy / "d"
    assert not resolved.exists()  # 실제 경로는 존재 안 함 (잘못된 매치 증거)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_slug_to_cwd_no_ambiguity_resolves_correctly(tmp_path):
    """혼동 케이스가 없으면 정확히 매치 (sanity)."""
    target = tmp_path / "a-b" / "c-d"
    target.mkdir(parents=True)
    (target / "sentinel.txt").write_text("ok", encoding="utf-8")

    # /a-b-c 같은 decoy 없음
    parts = str(tmp_path).lstrip("/").split("/")
    slug = "-" + "-".join(parts) + "-a-b-c-d"

    resolved = core._slug_to_cwd(slug)
    # greedy가 "a-b-c-d" → "a-b-c" → "a-b"까지 내려가서 정상 매치
    assert (resolved / "sentinel.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
def test_slug_to_cwd_falls_back_when_no_match_exists(tmp_path):
    """존재하지 않는 슬러그 → fallback으로 single-part concat. 결과는 비존재 경로."""
    parts = str(tmp_path).lstrip("/").split("/")
    slug = "-" + "-".join(parts) + "-nonexistent-folder"

    resolved = core._slug_to_cwd(slug)
    # 어떤 형태든 raise 안 함이 핵심
    assert isinstance(resolved.name, str)
    assert not resolved.exists()
