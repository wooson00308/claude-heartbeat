"""dream_meta.md 관리.

processed_v2 섹션 파싱 / 마킹 / GC + fcntl lock + atomic rename.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import tempfile
import time
from pathlib import Path

from .paths import get_project_dir

logger = logging.getLogger(__name__)

# processed_v2 GC: sealed 항목 누적 시 dream_meta.md가 비대해진다 (UUID 36자 × N).
# 누적 임계 초과 시 오래된 sealed 항목의 last_uuid/status 라인을 제거하고
# 파일명 한 줄로 압축한다. 파일명은 그대로 남아서 "이미 처리됨" 판단은 유지된다.
SEALED_GC_THRESHOLD = 200              # sealed 항목이 이 개수 초과 시 GC 트리거
SEALED_GC_KEEP_FULL = 100              # 최신 N개 sealed 항목만 full 형태 유지


INITIAL_META_TEMPLATE = """\
---
name: dream_meta
description: /dream 프로세스 메타데이터
type: reference
---
last_dream:
last_lint:

processed_v2:
"""


def get_dream_meta(slug: str) -> dict:
    """Read dream_meta.md and return processed transcript list (legacy parser)."""
    meta_path = get_project_dir(slug) / "memory" / "dream_meta.md"
    if not meta_path.exists():
        return {"last_dream": None, "processed": set()}

    content = meta_path.read_text(encoding="utf-8")
    processed = set()
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- ") and line.endswith(".jsonl"):
            processed.add(line[2:])

    return {"processed": processed}


def parse_meta_v2(slug: str) -> dict[str, dict]:
    """Parse processed_v2 section from dream_meta.md.

    Returns {filename: {"last_uuid": "...", "status": "sealed"|"active"}} dict.
    Returns empty dict on any error (old-parser-compatible spirit).
    """
    meta_path = get_project_dir(slug) / "memory" / "dream_meta.md"
    if not meta_path.exists():
        return {}

    try:
        content = meta_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, dict] = {}
    in_v2 = False
    current: dict | None = None

    for raw_line in content.split("\n"):
        line = raw_line.strip()

        # Section header detection
        if line == "processed_v2:":
            in_v2 = True
            current = None
            continue

        # Exit v2 section on any non-indented non-empty line that looks like a new section header
        if in_v2 and line and not raw_line.startswith(" ") and not raw_line.startswith("-") and not raw_line.startswith("\t"):
            in_v2 = False
            current = None
            continue

        if not in_v2:
            continue

        # New entry: "- file: xxx.jsonl"
        if line.startswith("- file:"):
            if current is not None and "file" in current:
                result[current["file"]] = {
                    "last_uuid": current.get("last_uuid", ""),
                    "status": current.get("status", "sealed"),
                }
            fname = line[len("- file:"):].strip()
            current = {"file": fname}
            continue

        # Sub-fields under current entry
        if current is not None:
            if line.startswith("last_uuid:"):
                current["last_uuid"] = line[len("last_uuid:"):].strip()
            elif line.startswith("status:"):
                current["status"] = line[len("status:"):].strip()

    # Flush last entry
    if current is not None and "file" in current:
        result[current["file"]] = {
            "last_uuid": current.get("last_uuid", ""),
            "status": current.get("status", "sealed"),
        }

    return result


def get_combined_processed(slug: str) -> set[str]:
    """Return union of legacy processed and v2 processed filenames."""
    legacy = get_dream_meta(slug).get("processed", set())
    v2_keys = set(parse_meta_v2(slug).keys())
    return legacy | v2_keys


@contextlib.contextmanager
def _acquire_meta_lock(slug: str):
    """Context manager: acquire fcntl exclusive lock on .dream.lock file.

    Waits up to 30 seconds; if lock cannot be acquired, logs and yields anyway
    (fail-open for operational safety).
    """
    lock_path = get_project_dir(slug) / "memory" / ".dream.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_fd = open(lock_path, "w")  # noqa: WPS515
    except OSError as exc:
        logger.warning("[dream-prep] lock file open failed: %s — proceeding without lock", exc)
        yield
        return

    acquired = False
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.1)

        if not acquired:
            logger.warning("[dream-prep] could not acquire lock within 30 s — proceeding without lock")

        yield
    finally:
        if acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            lock_fd.close()
        except OSError:
            pass


def _create_initial_meta(meta_path: Path) -> None:
    """dream_meta.md 정규 포맷 초기 생성. 이미 존재하면 no-op."""
    if meta_path.exists():
        return
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(INITIAL_META_TEMPLATE, encoding="utf-8")
    logger.info("[dream-prep] dream_meta.md 초기 생성: %s", meta_path)


def mark_processed(slug: str, filename: str, last_uuid: str, status: str = "sealed") -> None:
    """Append or update a processed_v2 entry in dream_meta.md.

    Thread/process safe via fcntl lock + atomic rename.
    Never raises; logs and returns on any error.

    메타 파일이 없으면 정규 포맷으로 자동 초기화한 뒤 마킹을 진행한다.
    """
    meta_path = get_project_dir(slug) / "memory" / "dream_meta.md"

    if not meta_path.exists():
        _create_initial_meta(meta_path)

    try:
        with _acquire_meta_lock(slug):
            _mark_processed_locked(meta_path, filename, last_uuid, status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dream-prep] mark_processed failed: %s", exc)


def _mark_processed_locked(meta_path: Path, filename: str, last_uuid: str, status: str) -> None:
    """Internal: perform the actual read-modify-write under caller's lock."""
    content = meta_path.read_text(encoding="utf-8")
    lines = content.split("\n")

    # Locate processed_v2 section and existing entry for this filename
    v2_section_idx: int | None = None
    entry_start: int | None = None
    entry_end: int | None = None

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "processed_v2:":
            v2_section_idx = i
            i += 1
            continue

        if v2_section_idx is not None:
            raw = lines[i]
            if raw and not raw.startswith(" ") and not raw.startswith("-") and not raw.startswith("\t") and stripped:
                break  # left section

            if stripped.startswith("- file:") and stripped[len("- file:"):].strip() == filename:
                entry_start = i
                j = i + 1
                while j < len(lines):
                    sub = lines[j]
                    sub_s = sub.strip()
                    if sub_s.startswith("last_uuid:") or sub_s.startswith("status:"):
                        j += 1
                    else:
                        break
                entry_end = j
                break
        i += 1

    new_entry_lines = [
        f"- file: {filename}",
        f"  last_uuid: {last_uuid}",
        f"  status: {status}",
    ]

    if entry_start is not None:
        lines[entry_start:entry_end] = new_entry_lines
    elif v2_section_idx is not None:
        insert_at = v2_section_idx + 1
        while insert_at < len(lines):
            raw = lines[insert_at]
            s = raw.strip()
            if raw and not raw.startswith(" ") and not raw.startswith("-") and not raw.startswith("\t") and s:
                break
            insert_at += 1
        for offset, el in enumerate(new_entry_lines):
            lines.insert(insert_at + offset, el)
    else:
        lines.append("")
        lines.append("processed_v2:")
        lines.extend(new_entry_lines)

    # GC: 오래된 sealed 항목 압축 (dream_meta.md 비대화 방지)
    lines = _gc_compact_old_sealed(lines)

    new_content = "\n".join(lines)

    # Atomic write: tmp file in same dir, then rename
    dir_path = meta_path.parent
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=dir_path, prefix=".dream_meta_", suffix=".tmp")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            os.rename(tmp_path, meta_path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as exc:
        raise RuntimeError(f"atomic write failed: {exc}") from exc


def _gc_compact_old_sealed(
    lines: list[str],
    threshold: int = SEALED_GC_THRESHOLD,
    keep_full: int = SEALED_GC_KEEP_FULL,
) -> list[str]:
    """processed_v2 섹션의 오래된 sealed 항목을 한 줄로 압축.

    sealed 총 개수가 threshold 초과 시, 위에서부터 (total - keep_full)개의
    full-form sealed 항목을 `- file: xxx.jsonl` 한 줄로 줄인다.
    파일명은 그대로 남아서 get_combined_processed의 "이미 처리됨" 판정은 유지된다.
    active 항목은 절대 건드리지 않는다 (last_uuid가 cursor 역할).
    """
    entries: list[tuple[int, int, str, str, bool]] = []
    # (start_line, end_line_exclusive, file, status, is_compact)

    in_v2 = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        if stripped == "processed_v2:":
            in_v2 = True
            i += 1
            continue

        if in_v2:
            if stripped and not raw.startswith(" ") and not raw.startswith("-") and not raw.startswith("\t"):
                break

            if stripped.startswith("- file:"):
                start = i
                file = stripped[len("- file:"):].strip()
                status = "sealed"  # 기본 (sub-line 없으면 압축된 sealed로 간주)
                j = i + 1
                while j < len(lines):
                    sub = lines[j].strip()
                    if sub.startswith("last_uuid:"):
                        j += 1
                    elif sub.startswith("status:"):
                        status = sub[len("status:"):].strip()
                        j += 1
                    else:
                        break
                end = j
                is_compact = (end == start + 1)
                entries.append((start, end, file, status, is_compact))
                i = end
                continue

        i += 1

    sealed_total = sum(1 for e in entries if e[3] == "sealed")
    if sealed_total <= threshold:
        return lines

    excess = sealed_total - keep_full
    compactable = [e for e in entries if e[3] == "sealed" and not e[4]]
    to_compact = compactable[:excess]
    if not to_compact:
        return lines

    for start, end, file, _, _ in sorted(to_compact, key=lambda e: e[0], reverse=True):
        lines[start:end] = [f"- file: {file}"]

    return lines
