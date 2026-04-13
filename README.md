# TARS — Task Automation & Repository Steward

> *"I have a cue light I can use to show you when I'm joking, if you want."* — TARS, Interstellar

TARS is an autonomous coding system that runs on your machine while you're away. It uses Claude CLI in headless mode to continuously discover tasks, implement code, fix errors, run tests, push to GitHub, and log everything to Discord — all without human intervention.

**New:** TARS now includes a **Controller API + Dashboard** for managing a cluster of Mac Minis, and integrates with the **usetars.dev** web platform where users submit tasks.

---

## How It Works

```
Website (usetars.dev) → Controller API → Task Queue
GitHub Issues + Manual Queue + Auto-Discovery
              ↓
      Task Prioritizer (scored: manual=100, issue=80, web=70, auto=40)
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

This verifies you have: `python3` (3.9+), `claude` CLI, `gh` CLI, `git`, `jq`, and the required Python packages.

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

## Controller API + Dashboard

The controller runs locally on the "brain" Mac Mini and provides a visual dashboard + REST API for managing the cluster.

### Start the controller

```bash
cd controller
./start.sh
```

Open `http://localhost:8421/` to see the dashboard:
- **Cluster nodes** — online Mac Minis with status
- **Task queue** — all tasks with priority, status, distribution bar
- **Start/Stop TARS** — control the daemon from the browser
- **Cancel tasks** — cancel pending tasks from the UI
- **Metrics** — completed, failed, PRs created, cost
- **Auto-refreshes** every 5 seconds

### API Endpoints

All authenticated endpoints require `X-API-Key` header.

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/health` | No | Health check |
| GET | `/api/status` | Yes | Cluster status, current task, queue depth |
| GET | `/api/workers` | Yes | List workers in cluster |
| GET | `/api/tasks` | Yes | List tasks (filter: `?status=`, `?project=`) |
| POST | `/api/tasks` | Yes | Submit new task |
| POST | `/api/tasks/<id>/cancel` | Yes | Cancel a pending task |
| GET | `/api/metrics` | Yes | Aggregated metrics |
| GET | `/api/daemon/status` | Yes | Check if TARS daemon is running |
| POST | `/api/daemon/start` | Yes | Start TARS daemon |
| POST | `/api/daemon/stop` | Yes | Stop TARS daemon |

### Configuration

Set `TARS_API_KEY` in `tars.conf` or as an environment variable. The same key is set on Railway so the website can send tasks to the controller.

```bash
# In tars.conf
TARS_API_KEY="your-secret-key"
```

---

## Cluster Architecture

```
                    ┌─────────────────────┐
                    │   usetars.dev       │
                    │   (Railway)         │
                    │   Django + Postgres │
                    └────────┬────────────┘
                             │ POST /api/tasks
                             ▼
                    ┌─────────────────────┐
                    │   Mac Mini (Brain)  │
                    │   Controller API    │
                    │   TARS Daemon       │
                    │   Port 8421         │
                    └────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌───────────┐ ┌───────────┐ ┌───────────┐
        │ Worker 1  │ │ Worker 2  │ │ Worker N  │
        │ Mac Mini  │ │ Mac Mini  │ │ Mac Mini  │
        └───────────┘ └───────────┘ └───────────┘
```

The brain Mac Mini:
- Runs the Controller API (Flask on port 8421)
- Runs the TARS daemon (picks tasks, executes via Claude CLI)
- Receives tasks from the website
- Distributes work to additional Mac Minis (when added)

Additional workers poll the brain for tasks and report back via heartbeat.

---

## Website Integration

The Django website at usetars.dev lets users:
- Register and log in
- Add GitHub projects
- Submit tasks (TARS Code, TARS Marketing)
- Track task status and progress
- View project activity

When a user submits a task, the website:
1. Saves it to the Django database
2. Forwards it to the Controller API on the Mac Mini
3. TARS picks it up from the queue and executes it

Set these env vars on Railway:
- `TARS_CONTROLLER_URL` — URL of the Mac Mini controller (e.g. `http://your-ip:8421`)
- `TARS_API_KEY` — shared secret key
- `DATABASE_URL` — PostgreSQL connection string
- `SECRET_KEY` — Django secret key
- `DJANGO_SUPERUSER_EMAIL` — admin email
- `DJANGO_SUPERUSER_PASSWORD` — admin password

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

TARS discovers tasks from four sources, each with a default priority score:

| Source | Priority | Description |
|--------|----------|-------------|
| **Manual queue** | 100 | Tasks in `config/queue.yaml` |
| **GitHub issues** | 80 | Issues with configured labels (e.g. `tars`) |
| **Website** | 70 | Tasks submitted via usetars.dev |
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

## Architecture

```
tars.sh                          Entry point (start/stop/status)
  ├─ bin/tars-daemon.sh          Main loop (the heartbeat)
  │    ├─ bin/tars-scheduler.sh  Pick next task from all sources
  │    │    └─ lib/task_manager.py
  │    ├─ bin/tars-worker.sh     Execute one task end-to-end
  │    │    ├─ lib/claude_runner.py    Claude CLI subprocess wrapper
  │    │    ├─ lib/git_manager.py      Clone, branch, commit, push, PR
  │    │    ├─ lib/error_analyzer.py   Auto-patch loop + circuit breakers
  │    │    ├─ lib/plan_builder.py     Pre-implementation planning
  │    │    └─ lib/xcode_manager.py    Xcode build/test
  │    ├─ lib/token_tracker.py   Budget enforcement
  │    ├─ lib/discord_logger.py  Webhook notifications
  │    └─ lib/metrics.py         Daily summaries
  ├─ bin/tars-health.sh          Watchdog (parallel process)
  ├─ bin/tars-setup.sh           Dependency checker
  └─ controller/
       └─ api.py                 Controller API + Dashboard (Flask)
```

**Design choices:**
- Shell for orchestration (process management, signals, PID files)
- Python for complex logic (task scoring, token math, Discord embeds)
- Claude CLI (`claude -p --output-format json`) for actual coding
- JSON files for state (human-readable, no database needed)
- Discord webhooks for logging (no bot infrastructure)
- Flask controller for cluster management + web dashboard

---

## Directory Layout

```
├── tars.sh              # Entry point
├── tars.conf            # Global config (includes TARS_API_KEY)
├── bin/                 # Shell scripts (orchestration)
├── lib/                 # Python modules (logic)
├── config/              # YAML configuration
│   ├── projects/        # Per-project configs
│   └── queues/          # Per-project task queues
├── controller/          # Controller API + Dashboard
│   ├── api.py           # Flask app
│   ├── requirements.txt
│   └── start.sh
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

**System:** `python3` (3.9+), `claude` CLI (authenticated), `gh` CLI (authenticated), `git`, `jq`

**Python:** `pyyaml`, `requests`, `python-dateutil`

**Controller:** `flask`, `flask-cors`, `pyyaml`, `gunicorn`

**Optional:** `xcodebuild` (macOS, for Xcode projects)
