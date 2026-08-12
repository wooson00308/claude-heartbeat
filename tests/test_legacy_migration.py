"""Legacy job discovery never changes a user's current Heartbeat installation."""

from __future__ import annotations

import json

from heartbeat.legacy_migration import preview_legacy_migration


def test_preview_reads_legacy_roles_and_history_without_writing(tmp_path):
    heartbeat = tmp_path / ".claude" / "HEARTBEAT.md"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text(
        """# HEARTBEAT

## planner-run
- role: planner
- provider: claude
- interval: 1h
- timeout: 10m
- max_per: 4/24h

## dream-memory
- prompt: /dream
""",
        encoding="utf-8",
    )
    state = tmp_path / ".claude" / "heartbeat" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"planner-run": {"last_result": "success", "recent_runs": [1]}}), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    preview = preview_legacy_migration(tmp_path)

    assert preview["writeRequired"] is False
    assert preview["roleJobs"]["planner"]["interval"] == "1h"
    assert preview["executionHistory"]["planner-run"]["last_result"] == "success"
    assert preview["excluded"] == [{"name": "dream-memory", "reason": "dream jobs remain outside the agent runtime"}]
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
