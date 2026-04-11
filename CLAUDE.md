# TARS - Task Automation & Repository Steward

Autonomous coding system that uses Claude CLI to discover tasks, implement code, run tests, push to GitHub, and log to Discord.

## Architecture
- Shell scripts (`bin/`) = orchestration (daemon, process management)
- Python modules (`lib/`) = complex logic (task queue, tokens, Discord, errors)
- Claude CLI (`claude -p`) = actual coding, invoked as subprocess

## Key Paths
- `tars.sh` — entry point (start/stop/status/run-once)
- `tars.conf` — global config (sourced by shell scripts)
- `config/projects/*.yaml` — per-project configs
- `state/` — runtime state (JSON files, PID, locks)
- `repos/` — TARS's own clones (never user's working trees)

## Conventions
- Python 3.10+, no type stubs needed
- Shell scripts use `set -euo pipefail`
- Claude invoked with `claude -p --bare --output-format json`
- All state is JSON files, not a database
- Discord logging via webhook POST, not bot
