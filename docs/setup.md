# Setup Guide

Detailed installation and configuration for claude-heartbeat.

## 1. Install

```bash
pip install claude-heartbeat
```

Verify:

```bash
heartbeat --help
```

## 2. Install a skill

```bash
# List available skills
heartbeat skills

# Install the dream skill (auto-detects projects)
heartbeat install dream
```

This copies the skill file to `~/.claude/skills/dream/` and registers heartbeat jobs in per-project files under `~/.claude/heartbeat/jobs.d/` (v0.8.0+).

To install for a specific project only:

```bash
heartbeat install dream --slug="-Users-yourname-Git-myproject"
```

## 3. Manual Test

Test before setting up automation:

```bash
# Run all jobs once
heartbeat once

# Run a specific job
heartbeat once -j "dream-home"
```

## 4. Register with launchd

Find the actual path of heartbeat:

```bash
which heartbeat
# e.g. /Users/yourname/.pyenv/versions/3.11.9/bin/heartbeat
```

Create the plist:

```bash
cat > ~/Library/LaunchAgents/com.claude-heartbeat.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-heartbeat</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/yourname/.pyenv/versions/3.11.9/bin/heartbeat</string>
        <string>start</string>
        <string>--foreground</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/yourname/.claude/heartbeat/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yourname/.claude/heartbeat/launchd_stderr.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/yourname</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/Users/yourname/.pyenv/versions/3.11.9/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/yourname</string>
    </dict>
</dict>
</plist>
EOF
```

> Replace `/Users/yourname` and the Python path with your actual paths.
> PATH must include the directory containing the `claude` CLI (usually `/opt/homebrew/bin`).

Load and start:

```bash
launchctl load ~/Library/LaunchAgents/com.claude-heartbeat.plist
```

Verify:

```bash
heartbeat status
```

Unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.claude-heartbeat.plist
```

> Since v0.4.0 `heartbeat start` always runs in the foreground — backgrounding is the OS scheduler's job.
> With KeepAlive enabled, the process auto-restarts if it dies.

---

## CLI Reference

```bash
heartbeat start            # Start (always foreground since v0.4.0)
heartbeat stop             # Stop daemon
heartbeat status           # Status + job states + recent logs
heartbeat jobs             # List configured jobs
heartbeat once             # Run all jobs once
heartbeat once -j "name"   # Run specific job once
heartbeat skills           # List available skills
heartbeat install <name>   # Install a skill (registers jobs into jobs.d)
heartbeat init             # Create HEARTBEAT.md + jobs.d
heartbeat migrate          # Split HEARTBEAT.md jobs into jobs.d files (--dry-run)
heartbeat install-service  # Register with launchd / Task Scheduler / systemd
heartbeat uninstall-service # Remove the OS scheduler entry
```

### Job File Format

Jobs live in `~/.claude/heartbeat/jobs.d/<slug>.md` (one file per project; the
filename is the slug, so the `slug` field is optional there). `~/.claude/HEARTBEAT.md`
still works as a legacy location and holds global settings like `tick`.
See [config-contract.md](config-contract.md) for the full contract.

```markdown
## job-name
- slug: -Users-yourname
- prompt: /dream
- interval: 3h
- timeout: 10m
- condition: dream-prep status --slug="-Users-yourname" 2>&1 | grep -q "미처리: [1-9]"
- notify: all
```

| Field | Description | Default |
|-------|-------------|---------|
| slug | Project slug (`~/.claude/projects/` subdirectory name) | Required |
| prompt | Prompt passed to `claude -p` | Required |
| interval | Run interval (s/m/h/d) | 1h |
| timeout | Timeout (s/m/h/d) | 600s |
| condition | Pre-run shell check (exit 0 = run) | None (always run) |
| notify | Desktop notification level: `all`, `failure`, `none` | all |
| max_per | Sliding-window quota, e.g. `5/24h` (at most 5 runs in any 24h) | None (no quota) |
| model | Model passed to `claude --model` (e.g. `opus`) | None (CLI default) |
