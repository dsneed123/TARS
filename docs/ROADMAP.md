# TARS Roadmap & Architecture Notes

Living document. Captures the current direction so the local models (and future
sessions) can reload context fast. Last major update: local-model migration +
multi-user product phase.

## North star
TARS runs **fully on local Ollama models** on the ASUS GX10 (no Claude API),
and becomes a small multi-user product: the owner generates API keys for
friends, friends point TARS at a public repo (or spin up a fresh one), and TARS
autonomously implements queued tasks one at a time.

## LLM provider: local Ollama (DONE ✅)
- Toggle: `TARS_LLM_PROVIDER` in `tars.conf` (`ollama` default | `claude`).
- New code: `lib/ollama_client.py` (stdlib HTTP), `lib/ollama_runner.py` (agent loop).
- `ClaudeRunner` and `ChatEngine` transparently delegate to `OllamaRunner` when
  provider=ollama, so all call sites + the worker's `jq` JSON contract are unchanged.
- **Why an agent loop?** Ollama only returns text/tool-calls — it is NOT an agent.
  `OllamaRunner` gives the model read/write/list/run_command tools so it actually
  edits the working tree, the way `claude -p` did. TARS keeps owning git/commits/PRs.
- Model routing (by prompt template → role):
  | Prompt | Role | Model (env) | Mode |
  |---|---|---|---|
  | implement_task.md | code | qwen2.5-coder:32b (`OLLAMA_CODE_MODEL`) | agent / tool-calling |
  | fix_error.md | fix | deepseek-r1:70b (`OLLAMA_FIX_MODEL`) | text SEARCH/REPLACE loop |
  | review_code.md | review | deepseek-r1:70b (`OLLAMA_REVIEW_MODEL`) | plain text→JSON |
  | discover_improvements.md | plan | deepseek-r1:70b (`OLLAMA_PLAN_MODEL`) | plain text→JSON |
  | chat/default | chat | deepseek-r1:70b (`OLLAMA_CHAT_MODEL`) | plain text |
- r1 is a reasoning model: no reliable tool-calling, emits `<think>` (stripped in
  `ollama_runner.strip_think`). So `fix` uses a text-directive loop (READ/RUN/EDIT
  blocks). qwen sometimes emits tool calls as text → recovered by `_parse_text_tool_calls`.
- Health check: `bin/tars-ollama-check.sh [--smoke]` (wired into `tars-setup.sh`).
- Validated end-to-end on hardware: code path created a file, fix path applied a
  SEARCH/REPLACE, chat path stripped `<think>`. All $0, local.

## Auto-merge toggle (DONE ✅)
- Per-project `git.auto_merge: true` → `config_loader.load_project` forces
  `git.strategy = "direct-main"` → worker merges to base branch, **no PR**.
- Backend already supported it via `GitManager.push_and_pr` strategies.

## Load balancing / resource gate (DONE ✅)
- Concurrency is **1 by design**: daemon runs tasks synchronously + single flock.
  Real parallelism needs worker nodes (future).
- `bin/tars-resource-gate.sh`: before each task the daemon checks 1-min load
  average (`TARS_MAX_LOADAVG`, default = ncores) and optional free VRAM
  (`TARS_MIN_FREE_VRAM_MB`, 0=off). Saturated → wait a poll interval, re-check.

## Product features — ALL DONE ✅ (built on the Flask controller, controller/api.py)
All friend-facing features live on the **controller** (REST API + dashboard, port
8420). The **public web app is the separate tars-survey repo** (Django/Railway,
`dsneed123/tars-survey`) which calls these controller APIs. The orphan static
`website/` folder was removed from this repo.

1. **Admin dashboard + per-friend API keys.** `lib/api_keys.py` (SHA-256 hashed
   store in `state/api_keys.json`, plaintext shown once). `require_api_key` now
   does key→user lookup; `require_admin` gates admin routes; master TARS_API_KEY =
   admin; constant-time compares; identity on `flask.g`. Routes: `/api/whoami`,
   `/api/admin/keys` [GET/POST], `/api/admin/keys/<id>/revoke`. The new SPA
   (`APP_HTML`) key-gates in the browser (localStorage) — **no embedded secret**.
2. **Self-service onboarding.** `POST /api/projects` ({repo_url}) parses owner/repo,
   writes project config, associates it with the caller's key. `GET /api/projects`
   (admin=all, user=own). `POST /api/projects/<name>/settings` toggles auto_merge /
   build / test / enabled. Projects tab in the SPA.
3. **Fresh project from design docs.** `POST /api/projects/fresh` ({name, design_docs})
   → `git_manager.create_repo` + `_tasks_from_docs` (planning model → JSON task list
   → per-project queue). NOTE: repo creation needs `gh` auth + network (not unit-
   tested headless); the route/config/task-gen wiring is verified.
4. **Chat UI (Claude/ChatGPT-style).** `POST /api/chat` with `mode`: `chat` (talks to
   the local model via provider-aware `ChatEngine`, per-conversation history in
   `state/chat/`) vs `task` (queues a task to a project). SPA Chat tab has an explicit
   Chat / Queue-task mode switch + project selector. Verified: r1 reply + task queue.
5. **Persistent per-project context.** `OllamaRunner._load_context` auto-loads
   `<cwd>/.tars/context.md` and prepends it to the system prompt; `generate_context`
   builds the digest (file list + README → planning model). Worker Step 3b generates
   it once per repo. Tunable via `OLLAMA_MAX_CONTEXT_CHARS`.

## Import note (IMPORTANT)
lib cross-imports (ollama_runner/claude_runner/chat_engine) use a try/except so they
work BOTH as bare modules (`PYTHONPATH=lib`) and as `lib.*` (PYTHONPATH=TARS_HOME,
how the worker runs). Keep this pattern when adding lib modules that import siblings.

## Server layout
- Public Flask controller `controller/api.py` → port 8420 (TARS_CONTROLLER_PORT).
  Runs in `controller/.venv`. Serves the SPA + all friend-facing APIs.
- Local FastAPI operator dashboard `lib/dashboard.py` → port 8421 (TARS_DASHBOARD_PORT).

## Key architecture facts (from code map)
- TWO web servers, BOTH on port 8420 — must reconcile:
  - `controller/api.py` (Flask): public, X-API-Key, serves dashboard, talks to website.
  - `lib/dashboard.py` (FastAPI): local operator UI, no auth, WS logs, project/queue create.
- Django website (tarsai.dev) is **external** (Railway+Postgres), not in this repo.
  Contract: website→controller `POST /api/tasks` (X-API-Key); worker→website status
  callbacks to `{TARS_WEBSITE_URL}/api/tasks/{survey_task_id}/status`.
- State is JSON files under gitignored `state/` (atomic `_read_json`/`_write_json`).
- Project config drives everything off `repo: owner/name`. Two queue locations:
  legacy `config/queue.yaml` (API writes) + per-project `config/queues/*.yaml` (daemon prefers).
