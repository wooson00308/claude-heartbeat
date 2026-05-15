"""dream-prep CLI — transcript JSONL 전처리 진입점."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .extract import (
    ROUND_MAX_CHUNKS_ACTIVE,
    conversation_to_markdown,
    extract_conversation,
    extract_partial_conversation,
)
from .meta import mark_processed, parse_meta_v2
from .paths import PROJECTS_DIR, get_project_dir
from .window import (
    CursorNotFoundError,
    classify_transcript,
    extract_last_message_uuid,
    find_unprocessed_transcripts,
)

logger = logging.getLogger(__name__)


def preprocess_project(
    slug: str,
    output_dir: Path | None = None,
    limit: int = 5,
    dry_run: bool = False,
    reset_cursor: bool = False,
) -> None:
    """Preprocess unprocessed transcripts for a project.

    Branches per transcript:
      - sealed-candidate (not active AND not huge): full extract → status="sealed"
      - active-or-huge (active OR huge): partial extract from cursor_uuid → status="active"

    dry_run=True suppresses mark_processed calls (no dream_meta.md side effects).
    reset_cursor=True allows restarting from line 0 when cursor_uuid is missing
    (default: hard fail to prevent duplicate memory ingestion).
    """
    unprocessed = find_unprocessed_transcripts(slug)

    if not unprocessed:
        print(f"[{slug}] 미처리 transcript 없음")
        return

    print(f"[{slug}] 미처리 transcript {len(unprocessed)}개 발견, {min(limit, len(unprocessed))}개 처리"
          + (" [dry-run]" if dry_run else ""))

    if output_dir is None:
        output_dir = get_project_dir(slug) / "memory" / "_dream_prep"
    output_dir.mkdir(parents=True, exist_ok=True)

    meta_v2 = parse_meta_v2(slug)

    batch = unprocessed[:limit]
    processed_count = 0

    for transcript_path in batch:
        session_id = transcript_path.stem
        filename = transcript_path.name
        cls = classify_transcript(transcript_path)

        is_active = cls["is_active"]
        is_huge = cls["is_huge"]
        use_partial = is_active or is_huge

        if use_partial:
            # --- 분기 B: active OR huge — 부분 처리 ---
            file_meta = meta_v2.get(filename, {})
            cursor_uuid: str | None = file_meta.get("last_uuid") or None

            print(
                f"[{slug}] {filename} → active-or-huge"
                f" (active={is_active}, huge={is_huge},"
                f" size={cls['size_bytes'] // 1024}KB,"
                f" cursor={'set' if cursor_uuid else 'none'})"
            )

            try:
                conversation, next_cursor_uuid, stats = extract_partial_conversation(
                    transcript_path,
                    cursor_uuid=cursor_uuid,
                    max_chunks=ROUND_MAX_CHUNKS_ACTIVE,
                    allow_reset=reset_cursor,
                )
            except CursorNotFoundError as exc:
                print(f"[{slug}] {filename} cursor 미스 (skip): {exc}")
                print(f"[{slug}] {filename} 재처리하려면 --reset-cursor 플래그 사용")
                continue

            print(
                f"[{slug}] {filename} partial result:"
                f" messages={stats['messages_processed']},"
                f" chars={stats['chars_processed']},"
                f" hit_cap={stats['hit_cap']},"
                f" next_cursor={'set' if next_cursor_uuid else 'none'}"
            )

            if not conversation:
                print(f"[{slug}] {filename} 처리 가능한 메시지 없음 (skip mark)")
                continue

            md = conversation_to_markdown(session_id, conversation)
            if not md:
                continue

            round_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_short = session_id[:12]
            output_file = output_dir / f"prep_{session_short}_partial_{round_ts}.md"
            output_file.write_text(
                f"# Dream Prep (partial) — {slug}\n\n"
                f"파일: {filename}\ncursor: {cursor_uuid or 'start'} → {next_cursor_uuid}\n"
                f"hit_cap: {stats['hit_cap']}\n\n---\n\n{md}",
                encoding="utf-8",
            )
            print(f"[{slug}] → {output_file}")

            if next_cursor_uuid is None:
                print(f"[{slug}] {filename} next_cursor_uuid=None → mark_processed 스킵")
            else:
                _maybe_mark(dry_run, slug, filename, next_cursor_uuid, "active")

        else:
            # --- 분기 A: sealed-candidate — 전체 처리 ---
            print(
                f"[{slug}] {filename} → sealed-candidate"
                f" (size={cls['size_bytes'] // 1024}KB,"
                f" age={cls['mtime_age_seconds'] / 60:.1f}min)"
            )

            conversation = extract_conversation(transcript_path)

            if not conversation:
                print(f"[{slug}] {filename} 의미 있는 대화 없음")
                continue

            md = conversation_to_markdown(session_id, conversation)
            if not md:
                continue

            # uuid 보존 안 됨(extract_conversation은 role/text/time만) → 별도 호출
            last_uuid = extract_last_message_uuid(transcript_path)

            print(
                f"[{slug}] {filename} sealed result:"
                f" turns={len(conversation)},"
                f" last_uuid={'set' if last_uuid else 'none'}"
            )

            round_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_short = session_id[:12]
            output_file = output_dir / f"prep_{session_short}_{round_ts}.md"
            output_file.write_text(
                f"# Dream Prep — {slug}\n\n처리 대상: {filename}\n\n---\n\n{md}",
                encoding="utf-8",
            )
            print(f"[{slug}] → {output_file}")

            _maybe_mark(dry_run, slug, filename, last_uuid or "", "sealed")

        processed_count += 1

    print(f"[{slug}] 완료: {processed_count}개 처리" + (" [dry-run]" if dry_run else ""))


def _maybe_mark(dry_run: bool, slug: str, filename: str, last_uuid: str, status: str) -> None:
    """Call mark_processed unless dry_run is set. Logs but never raises."""
    if dry_run:
        print(f"[dry-run] mark_processed({filename!r}, last_uuid={last_uuid!r}, status={status!r})")
        return
    try:
        mark_processed(slug, filename, last_uuid, status)
        print(f"[{slug}] marked: {filename} ({status})")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dream-prep] mark_processed call failed for %s: %s — continuing", filename, exc)


def list_projects() -> list[str]:
    """List all project slugs that have transcripts."""
    if not PROJECTS_DIR.exists():
        return []
    result = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and list(d.glob("*.jsonl")):
            result.append(d.name)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dream-prep",
        description="Preprocess transcript JSONL files for /dream"
    )
    sub = parser.add_subparsers(dest="command")

    # prep
    p_prep = sub.add_parser("prep", help="Preprocess transcripts for a project")
    p_prep.add_argument("--slug", "-s", required=True, help="Project slug (e.g. -Users-yourname)")
    p_prep.add_argument("--limit", "-n", type=int, default=5, help="Max transcripts to process")
    p_prep.add_argument("--dry-run", action="store_true", help="Skip mark_processed calls (no dream_meta.md changes)")
    p_prep.add_argument(
        "--reset-cursor",
        action="store_true",
        help="cursor_uuid가 transcript에 없을 때 처음부터 재처리 허용 (기본: hard fail)",
    )

    # list
    sub.add_parser("list", help="List projects with transcripts")

    # status
    p_status = sub.add_parser("status", help="Show processing status for a project")
    p_status.add_argument("--slug", "-s", required=True, help="Project slug")

    # check-unprocessed (heartbeat condition용 — exit code만 보면 됨, 셸 의존 0)
    p_check = sub.add_parser(
        "check-unprocessed",
        help="Exit 0 if there are unprocessed transcripts, else exit 1 (for heartbeat condition)",
    )
    p_check.add_argument("--slug", "-s", required=True, help="Project slug")

    # classify
    p_classify = sub.add_parser("classify", help="Show processing eligibility for all transcripts in a project")
    p_classify.add_argument("--slug", "-s", required=True, help="Project slug")

    # mark
    p_mark = sub.add_parser("mark", help="Mark a transcript as processed (v2 format)")
    p_mark.add_argument("--slug", "-s", required=True, help="Project slug")
    p_mark.add_argument("--file", "-f", required=True, dest="filename", help="Transcript filename (e.g. abc.jsonl)")
    p_mark.add_argument("--last-uuid", required=True, help="UUID of the last processed message")
    p_mark.add_argument("--status", default="sealed", choices=["sealed", "active"], help="Processing status (default: sealed)")

    args = parser.parse_args()

    if args.command == "prep":
        preprocess_project(
            args.slug,
            limit=args.limit,
            dry_run=args.dry_run,
            reset_cursor=args.reset_cursor,
        )
    elif args.command == "list":
        for slug in list_projects():
            count = len(list(get_project_dir(slug).glob("*.jsonl")))
            print(f"  {slug} ({count} transcripts)")
    elif args.command == "status":
        unprocessed = find_unprocessed_transcripts(args.slug)
        total = len(list(get_project_dir(args.slug).glob("*.jsonl")))
        print(f"  전체: {total}, 처리됨: {total - len(unprocessed)}, 미처리: {len(unprocessed)}")
    elif args.command == "check-unprocessed":
        unprocessed = find_unprocessed_transcripts(args.slug)
        sys.exit(0 if unprocessed else 1)
    elif args.command == "classify":
        project_dir = get_project_dir(args.slug)
        jsonl_files = sorted(project_dir.glob("*.jsonl"))
        if not jsonl_files:
            print(f"  [{args.slug}] jsonl 파일 없음")
        else:
            header = f"{'filename':<50} {'size_MB':>8} {'mtime_age_min':>14} {'is_active':>9} {'is_huge':>8} {'should_process':>14}"
            print(header)
            print("-" * len(header))
            for f in jsonl_files:
                info = classify_transcript(f)
                size_mb = info["size_bytes"] / (1024 * 1024)
                age_min = info["mtime_age_seconds"] / 60
                print(
                    f"{f.name:<50} {size_mb:>8.2f} {age_min:>14.1f}"
                    f" {str(info['is_active']):>9} {str(info['is_huge']):>8}"
                    f" {str(info['should_process']):>14}"
                )
    elif args.command == "mark":
        mark_processed(args.slug, args.filename, args.last_uuid, args.status)
        print(f"  [{args.slug}] marked: {args.filename} ({args.status})")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
