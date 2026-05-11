# dream skill

_Sleep well, remember everything._

**[한국어](README_ko.md)**

---

```
  Transcript        dream-prep         /dream             Memory
  (raw JSONL)       (preprocess)       (consolidate)      (topic files)

  Session 1 ─┐
  Session 2 ─┤
  Session 3 ─┼─────► Markdown ─────► ┌─ Orient ─────────►  MEMORY.md
  Session 4 ─┤       (compact)       │  Gather             user.md
  Session 5 ─┘                       │  Consolidate        feedback.md
                                     └─ Prune              project.md

      L3                                L2                     L1
  (raw logs)                        (knowledge)              (index)
```

---

## The Problem

Claude Code saves every conversation as a transcript JSONL file, but there is no way to read these files in the next session. When a new session starts, Claude is a blank slate — the 4-hour debugging session from yesterday and the architecture decision from last week are gone.

Claude Code only carries two things across sessions:

| Storage | Auto-loaded | Limitation |
|---------|-------------|------------|
| CLAUDE.md | Entire file | You write and maintain it manually |
| MEMORY.md | First 200 lines only | Topic files require manual Read to access |

Everything is in the transcripts. The memory system already exists. The only missing piece is the bridge between them.

## The Solution

Humans experience things during the day and consolidate memories during sleep. The hippocampus reviews the day's experiences, keeps what matters, and discards the rest.

dream does the same thing for Claude. It periodically reads transcripts, strips the noise, and turns them into memories that are ready for the next conversation.

No need to say "remember this." It remembers.
No need to say "organize that." It organizes.

That's why it's called dream.

---

## The Process

### 1. Heartbeat --- Decide whether to wake up

```
[heartbeat daemon]
     │
     ├─ Wakes every 60 seconds
     ├─ Parses job list from HEARTBEAT.md
     ├─ Checks interval per job (e.g. every 3 hours)
     └─ Runs condition check
         └─ "Any unprocessed transcripts?"
              ├─ No  → Skip (zero cost)
              └─ Yes → Next step
```

The daemon itself never calls the LLM. Always-on, zero token cost. It only wakes Claude when there's work to do.

### 2. dream-prep --- Preprocess

Raw transcript JSONL is too large and noisy to feed directly to an LLM. dream-prep extracts only user/assistant text, compresses code blocks, merges consecutive tool calls, and strips system messages.

Thousands of JSONL lines become a compact, readable markdown.

### 3. /dream --- Consolidate

Claude wakes up and runs the `/dream` skill. It follows the 4 phases of KAIROS autoDream:

| Phase | Name | Action |
|-------|------|--------|
| 1 | Orient | Survey current memory state |
| 2 | Gather | Read preprocessed markdown (marking owned by dream-prep CLI) |
| 3 | Consolidate | Merge with existing memory, create/update topic files |
| 4 | Prune & Index | Deduplicate, update MEMORY.md index (LLM updates timestamps only) |
| 5 | Lint | Integrity check, orphan pointer detection |

### 4. Memory --- Result

After consolidation, memory is up to date. In the next session, Claude already knows the context.

```
Before dream:
  "What's the project structure?"  ← asked every single session

After dream:
  (Already knows. Starts working immediately.)
```

---

## Memory Layers

| Layer | Description |
|-------|-------------|
| L1: MEMORY.md | Always loaded. 200-line index. |
| L2: Topic Files | Refined knowledge, referenced on need. (user.md, feedback.md, project.md ...) |
| L3: Transcript JSONL | Raw conversation logs. Auto-saved, auto-refined, zero effort. |

---

## Install

```bash
heartbeat install dream
```

This will:
1. Copy SKILL.md to `~/.claude/skills/dream/`
2. Register dream jobs in `~/.claude/HEARTBEAT.md` for each detected project
3. Activate the `dream-prep` CLI

## Manual usage

```bash
dream-prep list                            # Transcript count per project
dream-prep status --slug="-Users-yourname" # Processing status
dream-prep prep --slug="-Users-yourname" -n 5  # Run preprocessing
```

## Configuration

The heartbeat job runs every 3 hours by default. It checks for unprocessed transcripts first — if there are none, Claude is not woken up (zero cost).

Edit `~/.claude/HEARTBEAT.md` to adjust interval, timeout, or notification settings.
