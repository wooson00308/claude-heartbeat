"""배리어 없는 slug 그룹 디스패치 (v0.8.0 P0-B).

이전 루프는 사이클마다 전 그룹 완료를 기다려서, 한 프로젝트의 장기 세션이
다른 프로젝트의 다음 tick까지 세웠다. 디스패치는 제출만 하고 돌아오며,
실행 중인 그룹만 건너뛴다.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from heartbeat import core


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4)
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture(autouse=True)
def clean_inflight():
    yield
    with core._inflight_lock:
        core._inflight.clear()


def _job(name, slug):
    return {
        "name": name,
        "slug": slug,
        "prompt": "p",
        "interval": 0,
        "timeout": 5,
        "condition": "",
        "notify": "none",
        "max_per": None,
        "model": "",
    }


def test_inflight_slug_is_skipped(executor, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(core, "_run_slug_group", lambda slug, group, state: ran.append(slug))

    with core._inflight_lock:
        core._inflight.add("-busy")

    futures = core._dispatch_due_groups(
        [_job("a", "-busy"), _job("b", "-free")], {}, executor
    )

    assert set(futures) == {"-free"}
    futures["-free"].result(timeout=5)
    assert ran == ["-free"]


def test_dispatch_returns_while_slow_group_still_runs(executor, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def _slow_or_fast(slug, group, state):
        if slug == "-slow":
            started.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(core, "_run_slug_group", _slow_or_fast)

    futures = core._dispatch_due_groups(
        [_job("a", "-slow"), _job("b", "-fast")], {}, executor
    )

    # 느린 그룹이 아직 도는 중에도 디스패치는 이미 돌아왔고, 빠른 그룹은 끝난다.
    assert started.wait(timeout=5)
    futures["-fast"].result(timeout=5)
    assert not futures["-slow"].done()

    release.set()
    futures["-slow"].result(timeout=5)


def test_inflight_guard_is_released_after_completion(executor, monkeypatch):
    monkeypatch.setattr(core, "_run_slug_group", lambda slug, group, state: None)

    first = core._dispatch_due_groups([_job("a", "-proj")], {}, executor)
    first["-proj"].result(timeout=5)

    second = core._dispatch_due_groups([_job("a", "-proj")], {}, executor)
    assert set(second) == {"-proj"}
    second["-proj"].result(timeout=5)


def test_inflight_guard_is_released_even_when_group_raises(executor, monkeypatch):
    def _boom(slug, group, state):
        raise RuntimeError("boom")

    monkeypatch.setattr(core, "_run_slug_group", _boom)

    futures = core._dispatch_due_groups([_job("a", "-proj")], {}, executor)
    futures["-proj"].result(timeout=5)

    with core._inflight_lock:
        assert "-proj" not in core._inflight


def test_interval_due_check_still_applies(executor, monkeypatch):
    """디스패치는 그룹을 통째로 넘기고 due 판정은 그룹 안에서 잡마다 그 시점 시각으로 한다."""
    from datetime import datetime

    called: list[str] = []
    monkeypatch.setattr(core, "run_job", lambda job, state: called.append(job["name"]))

    state = {"fresh": {"last_run": datetime.now().isoformat()}}
    jobs = [
        {**_job("fresh", "-proj"), "interval": 3600},
        {**_job("due", "-proj"), "interval": 0},
    ]

    futures = core._dispatch_due_groups(jobs, state, executor)
    futures["-proj"].result(timeout=5)

    assert called == ["due"]
