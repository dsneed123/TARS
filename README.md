# TARS — Task Automation & Repository Steward

> *"I have a cue light I can use to show you when I'm joking, if you want."* — TARS, Interstellar

TARS is an autonomous coding system that runs on your machine while you're away. It uses Claude CLI in headless mode to continuously discover tasks, implement code, fix errors, run tests, push to GitHub, and log everything to Discord — all without human intervention.

---

## How It Works

```
GitHub Issues + Manual Queue + Auto-Discovery
              ↓
      Task Prioritizer (scored: manual=100, issue=80, auto=40)
              ↓
      Token Budget Check → over budget? → sleep & pace
              ↓
      Workspace Prepare (clone/pull, create branch)
              ↓
      Plan Phase (Claude generates implementation plan)
              ↓
      Implementation Phase (Claude writes code)
              ↓
      Verification (build → test → lint)
              ↓
         ┌── PASS ──────────────────┐
         │                          │
         │   FAIL → Auto-Patch Loop │
         │     (max 3 attempts)     │
         │     ↓                    │
         │   Fixed? → yes ──────────┤
         │            no → Escalate │
         ↓                          ↓
      Self-Review (Claude reviews own diff)
              ↓
      Git Push + PR Create
              ↓
      Discord Notification
              ↓
      Next Task
```

TARS works in its own cloned copies of your repos (in `repos/`), never touching your working trees.

---

## Quick Start

### 1. Check dependencies

```bash
./tars.sh setup
```

This verifies you have: `python3` (3.10+), `claude` CLI, `gh` CLI, `git`, `jq`, and the required Python packages.

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure a project

Copy the example and edit it:

```bash
cp config/projects/example-project.yaml config/projects/my-app.yaml
```

```yaml
repo: "your-username/your-repo"
enabled: true
git:
  strategy: "branch-pr"        # branch-pr | auto-merge | direct-main
  base_branch: "main"
build:
  command: "npm run build"      # or null to skip
test:
  command: "npm test"           # or null to skip
issues:
  enabled: true
  labels: ["tars"]
claude:
  model: "sonnet"
  max_turns: 20
```

### 4. Add a task

Edit `config/queue.yaml`:

```yaml
tasks:
  - id: "add-logging"
    title: "Add structured logging to API endpoints"
    description: "Replace print statements with proper logging using the logging module"
    project: "my-app"
    priority: 100
```

### 5. Run

```bash
# Test with a single task
./tars.sh run-once

# Or start the daemon
./tars.sh start
```

---

## Commands

| Command | Description |
|---------|-------------|
| `./tars.sh start` | Start the daemon in the background |
| `./tars.sh stop` | Stop the daemon |
| `./tars.sh restart` | Restart the daemon |
| `./tars.sh status` | Show daemon status, token usage, and task stats |
| `./tars.sh run-once` | Run one task cycle and exit |
| `./tars.sh setup` | Check dependencies and configuration |
| `./tars.sh health` | Run a health check |
| `./tars.sh logs` | Tail daemon logs |
| `./tars.sh queue` | Show pending tasks with priorities |

---

## Task Sources

TARS discovers tasks from three sources, each with a default priority score:

| Source | Priority | Description |
|--------|----------|-------------|
| **Manual queue** | 100 | Tasks in `config/queue.yaml` |
| **GitHub issues** | 80 | Issues with configured labels (e.g. `tars`) |
| **Auto-discovery** | 40 | Claude analyzes the codebase and suggests improvements |

Higher priority tasks are executed first.

---

## Discord Notifications

Set up one-way logging to a Discord channel:

```bash
cp config/discord.yaml.example config/discord.yaml
```

Edit `config/discord.yaml` with your webhook URL (Server Settings → Integrations → Webhooks).

TARS sends color-coded embeds:
- **Green** — task completed successfully
- **Yellow** — warnings (budget threshold, self-review issues)
- **Red** — failures, circuit breaker trips
- **Purple** — daily summary

---

## Safety Mechanisms

### Circuit Breakers
3 consecutive failures on a project → locked for 1 hour. Lockout doubles on each repeat (max 24h). Resets on success.

### Token Pacing
Configurable daily budget with peak/off-peak awareness. During peak hours (9–5 by default), TARS uses only 50% of the budget to save capacity for overnight autonomous work.

### Auto-Patch Loop
When build or tests fail after implementation, TARS feeds the error output back to Claude for up to 3 fix attempts before escalating.

### Self-Review
Before pushing, Claude reviews its own diff looking for security issues, logic errors, and breaking changes. Rejected changes are escalated to Discord.

### Isolated Workspaces
TARS clones repos into `repos/` and never touches your working trees.

### Watchdog
Health check every 5 minutes. Auto-restarts the daemon on crash, rotates large logs, cleans stale locks, and reports issues to Discord.

---

## Git Strategies

Configure per-project in the project YAML:

| Strategy | Behavior |
|----------|----------|
| `branch-pr` | Creates a branch, pushes, opens a PR (default) |
| `auto-merge` | Creates a PR with auto-merge enabled (squash) |
| `direct-main` | Merges directly to main and pushes |

---

## Xcode Projects

For iOS/macOS projects, configure the build section:

```yaml
build:
  type: "xcode"
  workspace: "MyApp.xcworkspace"
  scheme: "MyApp"
  destination: "platform=iOS Simulator,name=iPhone 16"
test:
  command: null   # null = use xcodebuild test
```

---

## Configuration Reference

### `tars.conf`
Global shell settings: paths, poll intervals, Claude defaults, safety thresholds. Sourced by all shell scripts.

### `config/projects/<name>.yaml`
Per-project settings: repo, git strategy, build/test commands, issue labels, auto-discovery, Claude model.

### `config/queue.yaml`
Manual task queue. Tasks with `status: pending` are picked up by the scheduler.

### `config/token_budget.yaml`
Daily token limit, peak hours, rate limit backoff, warning threshold.

### `config/discord.yaml`
Webhook URL and notification preferences.

---

## Architecture

```
tars.sh                          Entry point (start/stop/status)
  └─ bin/tars-daemon.sh          Main loop (the heartbeat)
       ├─ bin/tars-scheduler.sh  Pick next task from all sources
       │    └─ lib/task_manager.py
       ├─ bin/tars-worker.sh     Execute one task end-to-end
       │    ├─ lib/claude_runner.py    Claude CLI subprocess wrapper
       │    ├─ lib/git_manager.py      Clone, branch, commit, push, PR
       │    ├─ lib/error_analyzer.py   Auto-patch loop + circuit breakers
       │    ├─ lib/plan_builder.py     Pre-implementation planning
       │    └─ lib/xcode_manager.py    Xcode build/test
       ├─ lib/token_tracker.py   Budget enforcement
       ├─ lib/discord_logger.py  Webhook notifications
       └─ lib/metrics.py         Daily summaries
  └─ bin/tars-health.sh          Watchdog (parallel process)
  └─ bin/tars-setup.sh           Dependency checker
```

**Design choices:**
- Shell for orchestration (process management, signals, PID files)
- Python for complex logic (task scoring, token math, Discord embeds)
- Claude CLI (`claude -p --output-format json`) for actual coding
- JSON files for state (human-readable, no database needed)
- Discord webhooks for logging (no bot infrastructure)

---

## Directory Layout

```
├── tars.sh              # Entry point
├── tars.conf            # Global config
├── bin/                 # Shell scripts (orchestration)
├── lib/                 # Python modules (logic)
├── config/              # YAML configuration
│   └── projects/        # Per-project configs
├── prompts/             # Claude prompt templates
├── state/               # Runtime state (gitignored)
├── logs/                # Log files (gitignored)
├── repos/               # Cloned repos (gitignored)
└── tests/               # Test suite
```

---

## Running Tests

```bash
python3 -m pytest tests/ -v
```

---

## Dependencies

**System:** `python3` (3.10+), `claude` CLI (authenticated), `gh` CLI (authenticated), `git`, `jq`

**Python:** `pyyaml`, `requests`, `python-dateutil`

**Optional:** `xcodebuild` (macOS, for Xcode projects)
