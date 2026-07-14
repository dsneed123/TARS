# NIGHTLOG — overnight/node-pipeline — 2026-07-14

## TL;DR (read this over coffee)

TARS now runs tasks through a **typed node graph** (intake → plan → implement → verify →
review → integrate) on **qwen3-coder:30b** instead of the linear shell pipeline on
qwen2.5-coder:32b, and the difference is not subtle:

| metric | last night | tonight | delta |
|---|---|---|---|
| trivial task, end-to-end → merged PR | 4.5–25 min | **20.0 s** | **~13–75x** |
| standard task (dark-mode toggle) | 15–25 min typical, 134 min worst | **7.2 min** | ~2–3x |
| generation speed | 10.2 tok/s | **90 tok/s** | 8.8x |
| repo size (code+prompts+config) | 19,974 lines | **13,691** | **−6,283 (−31%)** |
| branch diff | — | 45 files, +1,986 / −7,163 | 12 commits, all pushed |

Real PRs merged tonight by the new pipeline on dsneed123/tars-test: **#3, #4, #5**, plus
the re-measure run — every one planned/implemented/verified/pushed by TARS itself.

The CLI is now one conversational REPL (`./tars`, just type — no subcommands, no mode
flags) with a local router model, streaming chat, and live per-node progress
(`watch <id>` renders `intake ✓ plan ∅ implement ✓11s …` as the graph runs).

**One thing needs you (1 minute):** `sudo` an Ollama upgrade —
`curl -fsSL https://ollama.com/install.sh | sh` — so structured tool calls come from
Ollama's parser instead of my fallback (then delete `parse_text_tool_calls` in
lib/llm.py). The daemon is stopped (as you left it); `./tars.sh start` when ready —
3 pending crypto-bot tasks will run through the new graph.

**Top 3 next steps:**
1. Upgrade Ollama (above), delete both text-tool-call fallback parsers, and re-verify
   an E2E task — that also unlocks trying qwen3-coder at its native 256K context.
2. Port go-sessions (website greenfield builds) onto the graph executor — it's the
   last workflow on the legacy OllamaRunner path, and it would inherit verify/skip/
   per-node state for free (then OllamaRunner shrinks to chat-only).
3. Raise throughput: the graph, gates, and per-task state are already isolated
   per task — the daemon's single-flock synchronous loop is now the ceiling.
   Either loosen it to N concurrent workers on this box (VRAM allows it) or
   start the Mac-mini worker-node distribution.

**Stretch goal: SHIPPED.** The Obsidian-brain view is live at
`http://localhost:8420/graph` (and through the tunnel): force-directed, dark,
zero external deps (hand-rolled physics on a canvas), key-gated like the main
app. Nodes = TARS hub → projects → recent task runs → each run's pipeline
stages; running nodes pulse/glow; hover for timings and call counts; drag to
play. It polls the new `GET /api/graph` every 2s. Verified: JS syntax-checked,
graph-construction + physics logic exercised in Node against the real API
payload (35 nodes / 34 edges / stable / no dupes across poll cycles) —
couldn't screenshot it headless because your Firefox session was in the way,
so the first human to actually SEE the brain will be you. Enjoy.

Details, decisions, and every deletion below.

---

Working unattended on the node-graph rework. Judged against the Milestone 0 baseline below.

## Environment at start
- GX10: NVIDIA GB10, 119 GB unified memory, 20 cores. ~82 GB available at start.
- Ollama models pulled: qwen2.5-coder:32b (19 GB), deepseek-r1:70b (42 GB), qwen2.5:14b, qwen2.5:7b, llama3.1:8b.
- qwen2.5-coder:32b loaded at **16K context** (llama-server `-c 16384 --no-jinja --chat-template chatml`) — duct-taped tool calling, exactly what the mission says to fix.
- Controller (gunicorn :8420) + cloudflared tunnel LIVE — left untouched until changes are tested.
- Note: `curl localhost:8420/health` returns `{"error":"Not found"}` — need to find the real health path before any restart.

## Milestone 0 — Baseline (from existing evidence, per user instruction — no live multi-hour run)

A first live baseline attempt failed in 1s: `repos/tars-test` local main had diverged from
origin and `git_manager.ensure_cloned()`'s bare `git pull` can't reconcile. Reset the clone
(TARS-owned, safe) and filed the fix for the reliability milestone: TARS clones should
`fetch + reset --hard origin/<base>`, never `pull`. The user then asked for a log-derived
baseline instead of a live run, so these numbers come from the last ~5 days of real
`manual-imp-*` worker runs (crypto-trading-bot, full pipeline, qwen2.5-coder:32b):

- **Wall clock per task: typical 15–25 min; range 9–134 min.** Recent examples:
  4.5 min (tiny), 9.3 min, 19 min, 22 min, 63 min, 65 min, 134 min.
- **LLM calls per task: 4–8 for normal tasks; 45 for a 63-min runaway.**
  (counted as POST /api/chat in the ollama journal during each task's window)
- **Generation speed: ~10.1–10.4 tok/s** (qwen2.5-coder:32b Q4_K_M, dense, on GB10).
  Prompt eval is ~735–780 tok/s. **Generation is the bottleneck** — every call that
  emits ~1000 tokens costs ~100 s, and the pipeline makes 4–45 of them.
- Token I/O for a representative 9.3-min task: 20.6K in / 5.2K out.
- Cost: $0 (local).

Interpretation: three levers dominate everything else —
1. tok/s (dense 32B → MoE coder ≈ 4–6x generation speedup),
2. number of LLM calls (merge/skip pipeline layers),
3. tokens per call (distilled artifacts instead of raw transcripts).
Started `ollama pull qwen3-coder:30b` (MoE, ~3.3B active, tool-calling native) in the
background at 07:2x — it is the intended new coder model.

## Milestone 1 — Design (what I'm building and why)

**New pipeline = a typed node DAG, executed by `lib/graph_executor.py`:**
`intake → plan → implement → verify → review → integrate`, declared in `config/graph.yaml`
(per-project override via a `graph:` key in the project YAML). Each node records timing,
LLM-call count, and tokens into `state/runs/<task_id>.json` as it goes.

Key decisions:
- **intake** (small fast model, 1 call, ~200 tokens out) classifies the task
  (trivial / standard / complex, docs_only) — this drives skip logic: trivial/docs skip
  `plan`, trivial skips `review`. Declared as `skip_for:` lists in YAML, not eval'd
  expressions (safe + simple).
- **verify** is deterministic (0 LLM calls): diff-exists check, syntax check, build, test.
  Failures go to a distilled fix loop (error tail only, not the whole transcript):
  retry coder ×2 → escalate to stronger model ×1 → fail loudly to Discord.
- **Artifacts, not transcripts**: nodes exchange small structured values (task, repo_map,
  plan, diff_stat + diff, test_output tail, verdict). Each node's prompt is built from
  only its declared inputs.
- **`lib/repo_map.py`**: deterministic per-project map (file tree + extracted symbols +
  README head), cached at `<repo>/.tars/repo_map.json`, keyed on git HEAD — replaces the
  old LLM-generated `.tars/context.md` (which cost a full slow LLM call per new repo and
  went stale silently).
- **`lib/llm.py`**: lean Ollama wrapper (text + native tool-calling agent loop) with
  per-role model/num_ctx/num_predict/keep_alive from `config/graph.yaml`. Replaces the
  role logic tangled into OllamaRunner; the bare-JSON tool-call recovery hacks die once
  qwen3-coder's native tool calling is verified.
- **Models**: coder/planner/reviewer = qwen3-coder:30b (MoE — same box, ~4-6x tok/s),
  router/intake = qwen2.5:7b, escalation = deepseek-r1:70b (load-on-demand, short
  keep_alive). Coder + router both stay resident (24 GB total on 119 GB — no more
  auto-swap thrash, which the July logs show cost 40 GB reloads mid-task).
- **Worker slims to a shim**: `bin/tars-worker.sh` becomes ~50 lines calling
  `python3 -m lib.graph_executor`; git prep / verify / push / Discord all live inside
  the graph nodes (kills the shell↔python round-trips and the 3 copies of verify logic).

## Milestone 1 — Results (verified end-to-end, twice)

**Model benchmarks on this GB10 (measured, not guessed):**
| model | role | gen tok/s | prompt tok/s | notes |
|---|---|---|---|---|
| qwen2.5-coder:32b (old) | everything | **10.2** | ~750 | dense — the old bottleneck |
| qwen3-coder:30b (new) | coder/planner/reviewer | **90** | ~3200 | MoE, 65K ctx verified, loads in 13s |
| qwen2.5:7b | router/intake | 46 | ~700 | intake verdict in <1s |
| deepseek-r1:70b | escalation only | ~10 | — | load-on-demand, keep_alive 5m |

**E2E runs through the new graph (real pushes to dsneed123/tars-test):**
- Trivial task (footer): **21.3 s wall**, 7 LLM calls, 12.2K/0.9K tok → PR #3 merged.
  Old pipeline for this class: 4.5–25 min. **~13–70x.**
- Standard task (dark-mode toggle w/ CSS vars + localStorage + OS pref): **7.2 min wall**,
  21 calls, review approved score 95 → PR #4 merged. Old pipeline typical: 15–25 min
  (and it would have made 2 extra LLM calls for context-digest + separate self-review).
- Skip logic verified live: trivial → plan+review skipped; standard → full path.
- Verify node is deterministic (0 LLM calls) and caught nothing to fix (both runs PASS);
  fix-loop and escalation paths exist but weren't exercised by these tasks.

**LLM-call diet vs old pipeline:** deleted the per-repo LLM context digest (replaced by
the deterministic repo map), merged quality-review + self-review into one review node,
trivial tasks now cost 2 LLM node types total (intake + implement).

**Found & fixed along the way:**
- `git_manager.ensure_cloned()` used `git pull` → divergence bricked every task on the
  project (this is what killed the first live baseline attempt at 07:10). Now hard-syncs
  disposable clones with `fetch + reset --hard origin/<base>`.
- Repo-map cache first landed inside the work tree and got committed by the agent
  (visible in tars-test PR #3) — moved to `state/repo_maps/`.
- The 06:09 crypto task log shows a worker that started and died silently — the old
  pipeline's failure modes are exactly why per-node state records now exist.

**Blocked (needs you, 1 minute):** Ollama 0.30.9 doesn't parse qwen3-coder's tool-call
format into structured `tool_calls` (the model omits the opening `<tool_call>` tag).
The fix is upgrading Ollama (≥0.31), which needs sudo:
`curl -fsSL https://ollama.com/install.sh | sh` — I couldn't enter a password.
Until then `lib/llm.py:parse_text_tool_calls()` recovers the calls (tested, works, and
`ollama_runner` falls back to it too); delete that function after the upgrade.

Legacy paths (chat, discovery, go sessions) also moved to qwen3-coder via tars.conf, and
auto-swap is now off by default — coder + router co-reside in 24GB of 119GB.

## Milestone 2 — Repo cleanup

**Line count: 19,974 → 15,058 (−4,916, −25%) — and that's INCLUDING the ~1,100 new
lines of graph pipeline.** Everything below is in git history if ever needed.

Deleted and why:
- `lib/task_executor.py` + `plan_task/implement_task/review_quality/fix_error/review_code.md`
  — the old linear pipeline; fully replaced by the graph. `self_review()` had zero callers.
- `lib/dashboard.py` (1,423) + `bin/tars-dashboard.sh` — the old :8421 operator dashboard;
  the controller on :8420 is the real one, this hadn't been running.
- `lib/discord_bot.py` (1,019) + `discord_modes.py` + `image_handler.py` + `bin/tars-discord.sh`
  — Discord BOT stack. No bot token was ever configured, and the stated convention is
  webhook logging, not a bot. Webhook logger (`discord_logger.py`) stays.
- `lib/options_scanner.py`, `stock_scanner.py`, `xcode_manager.py`, `plan_builder.py`
  — zero imports anywhere; strays from earlier experiments.
- `config/queue.yaml` + its fallback branches in config_loader/task_manager — the legacy
  global queue; only per-project `config/queues/*.yaml` exist now. (Its 2 entries were
  completed/cancelled — nothing migrated.)
- `config/projects/notes-app.yaml` — duplicate of tars-notes-app (both repos created
  within 5 min of each other on Jun 25; TARS only ever cloned tars-notes-app).
- `bin/elephant.sh` — project-specific hack for a disabled project.
- Stale `state/wtmp_*` temp files.

Workflow judgments (mission asked for a verdict on each):
- **daemon / scheduler / worker** — keep; worker is now a ~55-line shim over the graph.
- **self-improve loop** (`improvement_loop.py`) — keep: it's the active task-generator for
  crypto-trading-bot and orthogonal to the graph (it queues tasks; the graph runs them).
- **auto-discover** (`task_manager.get_auto_discovered_tasks`) — keep: the website's
  "discover" button calls it via `POST /api/projects/<name>/discover`. It shares the
  suggestion-parsing with self-improve already.
- **go sessions** (`go_runner.py`) — keep for now: the website's greenfield-build flow
  drives it. Candidate for a later port onto the graph executor.
- **chat engine** — keep (controller chat + soon the new CLI).
- Prompts tightened: `self_improve.md` 92→40 lines, `discover_improvements.md` 54→30;
  `go_*`/`discord_chat`/`create_project` were already tight.

## Milestone 3 — The CLI

`bin/tars-cli.py` rewritten 1,895 → ~440 lines: one conversational REPL, zero
subcommands, zero mode flags. `./tars` opens the loop; `./tars <anything>` is a
one-shot. Routing is layered: regex fast-paths answer `status`/`cancel <id>`
instantly (0 LLM calls), everything else goes to qwen2.5:7b with `format: json`
(~1s), and chat streams token-by-token straight from Ollama with a system prompt
carrying live cluster context (falls back to the controller's `/api/chat/generate`
when Ollama isn't reachable, e.g. a remote laptop).

Verified with real sessions:
- Router test 5/5: "add a contact form … in tars-test" → create_task(tars-test);
  "fix the typo in the README of crypto-trading-bot" → create_task(crypto-trading-bot);
  "cancel imp-7b840a90" → cancel_task; questions → chat/status. Correct every time.
- Mixed live session: chat ("is the coder model warm?") → streamed answer naming the
  actual projects; "add an about section to index.html in tars-test" → queued
  web-b01872dd with a one-line confirm; `status` showed it.
- **Live pipeline progress**: added `GET /api/runs/<task_id>` to the controller
  (serves the graph's per-node state); `watch <id>` renders nodes inline as they run:
  `intake ✓ plan ∅ implement ✓11s verify ✓ review ∅ integrate ✓8s → PR #5` —
  20 s from queue to merged PR. Controller was restarted for the new endpoint and
  health-verified locally + through the tunnel (note: health lives at `/api/health`,
  not `/health`).

The watch demo also caught a real verify bug: tars-test has node_modules COMMITTED
(June PR by the old pipeline), and verify's syntax gate was `node --check`-ing every
tracked .js → unfixable fix-loop → escalation to the 70B. Verify now parse-checks
ONLY the files the task changed, never vendored paths (go_runner's check got the same
guard). The kill-tree trap and the 5m escalation keep_alive both behaved exactly as
designed during the recovery, which was good to see under real fire.

## Milestone 4 — Reliability hardening

Already structural in the graph (verify gate, capped fix loop → escalation → loud
Discord failure, per-task push, one Discord message per task with wall-s/calls/tokens).
Added tonight on top:

- **Per-call wall-clock bounds**: every text node now passes its node timeout down to
  the HTTP call, and each agent-loop call is capped by the node's *remaining* budget —
  one hung generation can no longer overshoot a node by the old 20-min client default.
- **Escalation model decision**: default escalation is now a fresh, temperature-0
  qwen3-coder pass on the distilled failure — NOT deepseek-r1. r1 can't drive tools
  reliably (it's why the old pipeline had a whole parallel "edit-mode" text loop), and
  at ~10 tok/s it costs minutes per attempt. Rationale in config/graph.yaml; point it
  at a bigger tool-calling model when one lands.
- **Ollama gate in tars-resource-gate.sh**: local-model tasks are guaranteed failures
  during an Ollama outage — the daemon now waits instead of burning task retries
  (same philosophy as the existing GitHub network gate).
- **Issue fetch without a clone**: `gh issue list` now targets `-R <repo>` and no
  longer requires `repos/<name>` to exist — this was spamming "Failed to fetch issues
  for tars-survey" into every scheduler tick.
- **Tests**: `tests/test_graph_executor.py` — 9 new tests covering changed-file
  scoping, vendored exclusion, skip logic, the verify fix-loop exhausting
  coder→coder→escalation and failing, empty-diff failure, run-state persistence,
  tool-call/JSON parsers, and DAG deadlock detection. Suite: 21 passed.

**Daemon note:** the daemon has been DOWN since ~06:09 this morning — no OOM, no
reboot, no shutdown trap in the log, and the systemd unit is disabled; it was run
via nohup and evidently killed by hand right before the overnight mission was
written. I left it stopped on purpose. When you start it (`./tars.sh start`), the
3 pending crypto-trading-bot improvement tasks will run through the new graph.

## Milestone 5 — Re-measure

Official post-rework run of the M0-class task ("add a favicon link tag to index.html",
tars-test): **20.0 s wall, 8 LLM calls, 30.6K/0.7K tokens, completed → pushed.**
Node split: intake 0.9s · plan skipped · implement 11.1s (7 calls) · verify 0.0s ·
review skipped · integrate 7.9s. Integration (push + PR + auto-merge) is now the
second-biggest cost at ~8s — GitHub round-trips, not model time.

Baseline equivalents were 4.5–25 min (typical 15–25 min for standard work, 134 min
worst observed). LLM-call count for trivial work: 4–8 before (all against a 10 tok/s
dense model) vs 8 now (7 against a 90 tok/s MoE + 1 sub-second router call) — the
call COUNT is similar for trivial tasks, but the calls are ~9x faster and carry
distilled context instead of raw transcripts. For standard tasks the old pipeline's
context-digest + double-review overhead is simply gone.

Line-count delta for the whole night: 19,974 → 13,691 total (−6,283 net, −31%),
45 files changed, +1,986/−7,163 on the branch, 12 commits, all pushed to
origin/overnight/node-pipeline.
