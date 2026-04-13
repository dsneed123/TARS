# TARS - Task Automation & Repository Steward

Autonomous coding system that uses Claude CLI to discover tasks, implement code, run tests, push to GitHub, and log to Discord. Runs on a brain node (any computer) with a Flask controller; receives tasks from the Railway-hosted **usetars.dev** website and (eventually) distributes work to Mac mini worker nodes.

## Architecture
- Shell scripts (`bin/`) = orchestration (daemon, process management)
- Python modules (`lib/`) = complex logic (task queue, tokens, Discord, errors)
- Flask controller (`controller/api.py`) = REST API + dashboard for cluster and website
- Django website on Railway (`usetars.dev`) = user-facing task submission, forwards to controller
- Claude CLI (`claude -p`) = actual coding, invoked as subprocess

## Key Paths
- `tars.sh` — entry point (start/stop/status/run-once)
- `tars.conf` — global config (sourced by shell scripts; holds `TARS_API_KEY`)
- `config/projects/*.yaml` — per-project configs
- `config/queues/*.yaml` — per-project task queues
- `controller/` — Flask API + dashboard (brain node only)
- `state/` — runtime state (JSON files, PID, locks)
- `repos/` — TARS's own clones (never user's working trees)

## Conventions
- Python 3.9+, no type stubs needed
- Shell scripts use `set -euo pipefail`
- Claude invoked with `claude -p --bare --output-format json`
- All state is JSON files, not a database (controller included)
- Discord logging via webhook POST, not bot
- Controller auth via `X-API-Key` header matching `TARS_API_KEY`
- Controller listens on port `8420` (override with `TARS_CONTROLLER_PORT`)
