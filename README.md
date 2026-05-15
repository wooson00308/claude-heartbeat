# claude-heartbeat

*Keep Claude alive between sessions.*

**[한국어](docs/ko.md)**

---

Claude Code is reactive — it only works when you talk to it.
Heartbeat makes it proactive.

A lightweight daemon that periodically wakes Claude on schedule, runs skills, and goes back to sleep. Zero token cost when idle.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  HEARTBEAT  │     │   condition  │     │  claude -p  │
│  .md        │────►│   check      │────►│  "{prompt}" │
│ (job config)│     │  (shell cmd) │     │ (skill run) │
└─────────────┘     └──────────────┘     └─────────────┘
                      skip if nothing        wake only
                      to do (cost: 0)        when needed
```

---

## How it works

1. Heartbeat daemon runs in the background (via launchd)
2. Every 60 seconds, it checks configured jobs
3. For each job whose interval has elapsed, it runs a condition check
4. If the condition passes, it wakes Claude with `claude -p "{prompt}"`
5. Claude executes the skill and goes back to sleep

The daemon never calls the LLM itself. It only decides when to wake it.

## What can you run?

The `prompt` field accepts anything — a plain sentence, a skill command, or a reference to documentation. Heartbeat doesn't care what the prompt says. It just passes it to `claude -p`.

### Plain prompts

```markdown
## daily-summary
- slug: -Users-yourname-Git-myproject
- prompt: Check git log for the last 24 hours and summarize changes
- interval: 1d
- timeout: 5m

## lint-check
- slug: -Users-yourname-Git-myproject
- prompt: Run npm run lint and fix any errors
- interval: 6h
- timeout: 3m
```

### Skills

Claude Code supports [user-created skills](https://docs.anthropic.com/en/docs/claude-code) — reusable prompts that Claude can execute on demand. For more complex or multi-step tasks, you can write a skill and reference it in the prompt field.

The `dream` skill is included as a working example.

```bash
heartbeat skills              # List available skills
heartbeat install dream       # Install a skill
```

### dream (example skill)

Automatically consolidates session transcripts into long-term memory. Claude Code saves every conversation as JSONL, but never reads them again. The dream skill processes these transcripts and updates memory so the next session starts with full context.

See [skills/dream/README.md](skills/dream/README.md) for details.

---

## Prerequisites

- macOS / Windows / Linux
- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

## Quick Start

```bash
pip install claude-heartbeat

# Install the dream skill (copies SKILL.md + registers heartbeat jobs)
heartbeat install dream

# Verify
heartbeat jobs

# Test run
heartbeat once

# Register with the OS background scheduler (launchd / Task Scheduler)
heartbeat install-service

# Or run in the foreground for testing
heartbeat start
```

For manual setup, log paths, and the launchd / Task Scheduler details, see the [Setup Guide](docs/setup.md).

## Configuration

Jobs are defined in `~/.claude/HEARTBEAT.md`:

```markdown
# HEARTBEAT

- tick: 60s

## daily-summary
- slug: -Users-yourname-Git-myproject
- prompt: Summarize today's git changes
- interval: 1d
- timeout: 5m
- notify: failure

## lint-check
- slug: -Users-yourname-Git-myproject
- prompt: Run npm run lint and report errors
- interval: 6h
- timeout: 3m
- condition: test -f package.json
- notify: failure
```


| Field     | Description                                            | Default           |
| --------- | ------------------------------------------------------ | ----------------- |
| slug      | Project slug (`~/.claude/projects/` subdirectory name) | Required          |
| prompt    | Prompt passed to `claude -p`                           | Required          |
| interval  | Run interval (s/m/h/d)                                 | 1h                |
| timeout   | Timeout (s/m/h/d)                                      | 600s              |
| condition | Pre-run shell check (exit 0 = run)                     | None (always run) |
| notify    | macOS notification level: `all`, `failure`, `none`     | all               |

Global settings go before any `##` job header:

| Setting | Description | Default |
|---------|-------------|---------|
| tick | Daemon wake interval (s/m/h/d) | 60s |

## CLI

```bash
heartbeat start                # Start in foreground (since v0.4.0; OS scheduler handles backgrounding)
heartbeat stop                 # Stop running heartbeat
heartbeat status               # Status + job states + recent logs
heartbeat jobs                 # List configured jobs
heartbeat once                 # Run all jobs once
heartbeat once -j "name"       # Run specific job once
heartbeat skills               # List available skills
heartbeat install <name>       # Install a skill
heartbeat install-service      # Register with launchd (macOS) / Task Scheduler (Windows) / systemd (Linux)
heartbeat uninstall-service    # Remove the OS scheduler entry
```

## Migration from v0.1

If you're upgrading from `dream-preprocessor` v0.1:

- `dream-heartbeat` still works as an alias for `heartbeat`
- `dream-prep` still works as before
- No changes needed to your `HEARTBEAT.md` or launchd plist

## License

MIT

---

*under the moonlight, Claude dreams.*