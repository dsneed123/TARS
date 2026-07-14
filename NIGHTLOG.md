# NIGHTLOG — overnight/node-pipeline — 2026-07-14

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
