"""dream_meta.md 파싱/마킹/GC 회귀 방지 테스트.

v0.2.1 hotfix 보호:
- mark_processed: meta 없을 때 자동 초기화
- _gc_compact_old_sealed: sealed 누적 시 압축, active 보존
"""

from __future__ import annotations

from skills.dream import meta


def test_parse_meta_v2_missing_file(project_dir, slug):
    assert meta.parse_meta_v2(slug) == {}


def test_parse_meta_v2_full_form(project_dir, slug):
    (project_dir / "memory" / "dream_meta.md").write_text(
        "---\nname: dream_meta\ntype: reference\n---\n"
        "last_dream: 2026-05-15T00:00:00\n\n"
        "processed_v2:\n"
        "- file: aaa.jsonl\n"
        "  last_uuid: u1\n"
        "  status: sealed\n"
        "- file: bbb.jsonl\n"
        "  last_uuid: u2\n"
        "  status: active\n",
        encoding="utf-8",
    )
    result = meta.parse_meta_v2(slug)
    assert result == {
        "aaa.jsonl": {"last_uuid": "u1", "status": "sealed"},
        "bbb.jsonl": {"last_uuid": "u2", "status": "active"},
    }


def test_parse_meta_v2_compact_form(project_dir, slug):
    """GC로 압축된 entry (`- file: xxx.jsonl` 한 줄)도 인식해야 한다."""
    (project_dir / "memory" / "dream_meta.md").write_text(
        "processed_v2:\n"
        "- file: compacted.jsonl\n"
        "- file: full.jsonl\n"
        "  last_uuid: u3\n"
        "  status: sealed\n",
        encoding="utf-8",
    )
    result = meta.parse_meta_v2(slug)
    assert "compacted.jsonl" in result
    assert result["compacted.jsonl"]["status"] == "sealed"
    assert result["full.jsonl"]["last_uuid"] == "u3"


def test_get_combined_processed_unions_legacy_and_v2(project_dir, slug):
    """legacy(processed:)와 v2(processed_v2:) 모두 처리됨 판정에 들어가야 한다."""
    (project_dir / "memory" / "dream_meta.md").write_text(
        "processed:\n- legacy.jsonl\n\nprocessed_v2:\n- file: new.jsonl\n  last_uuid: u\n",
        encoding="utf-8",
    )
    combined = meta.get_combined_processed(slug)
    # legacy parser는 sloppy match라 v2 라인을 noise로 잡을 수 있지만
    # 실제 처리됨 판정에 영향 없음 (set union의 본질만 검증)
    assert "legacy.jsonl" in combined
    assert "new.jsonl" in combined


def test_mark_processed_auto_initializes_missing_meta(project_dir, slug):
    """v0.2.1 hotfix: meta 파일 없을 때 정규 포맷으로 자동 초기화."""
    meta_path = project_dir / "memory" / "dream_meta.md"
    assert not meta_path.exists()

    meta.mark_processed(slug, "x.jsonl", "uuid-xxx", "sealed")

    assert meta_path.exists()
    content = meta_path.read_text(encoding="utf-8")
    assert "name: dream_meta" in content
    assert "type: reference" in content
    assert "- file: x.jsonl" in content
    assert "last_uuid: uuid-xxx" in content
    assert "status: sealed" in content


def test_mark_processed_updates_existing_entry(project_dir, slug):
    """동일 파일명 마킹 시 in-place 갱신 (라인 추가 금지)."""
    meta.mark_processed(slug, "y.jsonl", "u1", "active")
    meta.mark_processed(slug, "y.jsonl", "u2", "sealed")

    parsed = meta.parse_meta_v2(slug)
    assert parsed == {"y.jsonl": {"last_uuid": "u2", "status": "sealed"}}


def test_gc_compact_under_threshold_noop():
    lines = ["processed_v2:"]
    for i in range(50):
        lines += [f"- file: f{i}.jsonl", f"  last_uuid: u{i}", "  status: sealed"]

    result = meta._gc_compact_old_sealed(lines, threshold=200, keep_full=100)
    assert result == lines


def test_gc_compact_over_threshold_compacts_oldest():
    """sealed 250개 → 위 150개 compact, 아래 100개 full 유지."""
    lines = ["processed_v2:"]
    for i in range(250):
        lines += [f"- file: f{i}.jsonl", f"  last_uuid: u{i}", "  status: sealed"]

    result = meta._gc_compact_old_sealed(lines, threshold=200, keep_full=100)

    # 압축돼도 "- file:" 라인은 남는다 (파일명 보존이 GC의 핵심).
    # 압축 여부는 sub-line(last_uuid/status) 잔존 카운트로 판정.
    file_lines = sum(1 for ln in result if ln.startswith("- file: f"))
    last_uuid_lines = sum(1 for ln in result if ln.startswith("  last_uuid:"))
    status_lines = sum(1 for ln in result if ln.startswith("  status:"))

    assert file_lines == 250  # 전부 보존
    assert last_uuid_lines == 100  # full form만 남음
    assert status_lines == 100

    # 처리됨 판정도 250개 그대로 보존되는지 확인
    parsed_files = [ln.strip()[len("- file:"):].strip() for ln in result if ln.startswith("- file:")]
    assert len(set(parsed_files)) == 250


def test_gc_never_touches_active_entries():
    """active 항목은 last_uuid가 cursor라서 절대 압축하면 안 된다."""
    lines = ["processed_v2:"]
    # 250개 active — threshold 넘었지만 sealed 0개 → no-op
    for i in range(250):
        lines += [f"- file: a{i}.jsonl", f"  last_uuid: u{i}", "  status: active"]

    result = meta._gc_compact_old_sealed(lines, threshold=200, keep_full=100)
    assert result == lines
