# TARS Go dry run — findings and fixes (2026-07-03)

## Context

Ran TARS Go (the autonomous plan → execute → review loop in `lib/go_runner.py`)
end-to-end on a fresh project (`crypto-trading-bot`, a from-scratch build) as a
deliberate stress test of the harness itself. The trading bot's actual quality
was never the point — the goal was to find out where the autonomous loop lies
to itself, and fix the harness so it can't.

Four real problems surfaced, in the order they were hit. All four are fixed.

---

## Problem 1: work never left the machine

**Symptom**: after a full session, the GitHub repo had nothing but the
bootstrap README — no matter what the task list claimed got done.

**Cause**: `lib/go_runner.py`'s `_go_loop` committed inside the local clone
(the coding model runs `git commit` itself, on instruction) but never called
`git push` anywhere. `lib/git_manager.py` has full push/PR machinery, but
`go_runner.py` never used it.

**Fix**: added `_push()`, called after every completed task. Pushes straight
to `origin/<base_branch>` so progress is visible on GitHub as the loop runs,
not just (never) at the end.

---

## Problem 2: tasks reported "done" while doing nothing

**Symptom**: three tasks in a row reported plausible one-line summaries and
`status: done`. The working tree was completely empty except the bootstrap
README — no files, no commits beyond bootstrap.

**Cause**: `lib/ollama_runner.py`'s agent loop (`_run_agent`) expects tool
calls either via Ollama's structured `tool_calls` field, or — as a fallback —
recovered from plain text via `_parse_text_tool_calls()`. That fallback only
recognized `<tool_call>` tags, ` ```json ` fences, or a single bare JSON
object. `qwen2.5-coder:32b` was actually emitting **multiple bare JSON
objects back-to-back with no wrapper at all** — a shape the old regex-based
recovery didn't match. Every one of those "tool calls" was silently ignored,
the loop saw no tool calls and concluded the model was "done," and the raw
JSON text became the task's plausible-looking summary.

**Fix**: replaced the regex-based recovery with a balanced-brace JSON scanner
(`json.JSONDecoder().raw_decode` walked across the text) that finds every
top-level `{...}` object regardless of wrapper, handling both the multi-object
case and a second latent bug — the old lazy regex (`\{.*?\}`) truncated at the
first `}` inside a `write_file` call's own code content (e.g. a dict literal),
so any generated code containing braces would have broken parsing too.
Verified against the exact broken model output before deploying.

---

## Problem 3: the review loop couldn't tell truth from claims

**Symptom**: review score plateaued at 85 for four straight iterations
(needs 90 to auto-finish). The repo held **three separate, mutually
unwired GUI implementations** — two different Electron entry points, two
different PyQt5 windows, and a Tkinter one, none connected to the real
trading logic — while three separate commits *claimed* to have "unified the
GUI" / "removed the redundant Electron setup." The Electron files were still
there every time.

**Cause**: the review step (`prompts/go_review.md`) only ever saw a repo file
listing plus the tasks' own self-reported summaries. It had no way to check
whether a "done" task's claim was true, and no instruction to be skeptical of
suspiciously self-congratulatory commit messages.

**Fix, two parts**:
- `go_runner.py` now runs an actual verification pass before every review —
  a syntax check across the repo (see Problem 4) plus the project's
  `test.command` if configured — and feeds the real pass/fail output to the
  reviewer as `{{VERIFICATION}}`, explicitly labeled as ground truth over the
  tasks' self-reports.
- `prompts/go_review.md` now explicitly instructs the reviewer to watch for
  parallel/duplicate implementations and to not trust a commit message's
  claim of "unified"/"removed" as proof — check the snapshot.

**Known limitation**: a syntax check only catches literally-broken code, not
semantically-broken-but-valid code (e.g. an entry point that imports a class
that doesn't exist under that name — which is exactly what happened in the
Python GUI files here). Catching that reliably needs either a configured
`test.command` that actually exercises imports, or a deeper "try to run the
entry point" check — noted as a good next increment, not implemented yet.

---

## Problem 4: tasks were never actually blocked on verification, and the
existing test-failure fix mechanism was dead code

**Symptom/cause**: `go_runner.py` never verified a task's own output at all
— `status: done` only ever meant "the model didn't crash." Separately, there
was already a mechanism (`_run_tests`) that, on test failure, built a
"fix this" task — but it only appended that task to `state["tasks"]` tagged
with the **current** iteration number. The execution loop only re-scans for
tasks tagged with the **next** iteration once it moves on. The fix task sat
as `status: queued` forever and never ran. This was a real, pre-existing bug,
independent of anything in this dry run's model behavior.

**Fix**:
- Added `_syntax_check()` — a language-aware syntax check (`python3 -m
  py_compile` for `.py` files; `node --check` for `.js` files if `node` is
  installed, otherwise skipped with a log note) run after **every** task,
  regardless of whether a `test.command` is configured. A failure demotes the
  task from "done" to "failed" and immediately queues a fix task.
- Added `_inject_fix_task()`, a shared helper used by both the new syntax
  gate and the existing `_run_tests`, which appends the fix task to **both**
  `state["tasks"]` (persistence) and the `queued` list the current
  iteration's execution loop is actively iterating over — Python's `for`
  loop over a list does pick up items appended to that same list mid-iteration,
  so the fix task now actually executes immediately instead of being
  orphaned.

---

## Problem 5 (design gap, also fixed): no detection of a non-converging loop

**Symptom**: "Unify GUI Implementation" was queued as a fresh task by the
reviewer in three separate iterations, each time failing the same way,
with nothing noticing the loop wasn't converging — it would have kept
retrying the same failing approach for the full iteration budget (default 5,
raised to 30 earlier this session).

**Fix**: added a stuck-loop detector. Before queueing the reviewer's proposed
tasks for the next iteration, each proposed title is checked against all
already-attempted (`done` or `failed`) task titles in the session
(`_title_attempt_count`, using `difflib.SequenceMatcher` on lowercased titles,
threshold 0.6 — tuned to the verbatim-repeat pattern actually observed, not
a hypothetical paraphrase). A title attempted 2+ times already is refused as
a repeat rather than requeued. If **every** proposed task for the next
iteration is a repeat, the session stops immediately with `status: "stuck"`
and a clear `error` message, instead of burning the rest of the iteration
budget cycling the same unresolved issue.

---

## Also fixed this session (smaller, related)

- **Commit identity is now a project config option.** Autonomous commits were
  hardcoded to `TARS Bot <tars@usetars.dev>`. Added optional
  `git.author_name` / `git.author_email` to project YAML
  (`lib/git_manager.py`, `controller/api.py`); unset keeps the bot default.
  Applied on every `ensure_cloned()`, not just first bootstrap, so a config
  change takes effect on the next run against an existing clone.
- Raised `TARS_GO_MAX_ITERATIONS` default 5 → 30 (`tars.conf`) so a session
  has room to actually converge instead of stopping early — this is now
  safer with the stuck-loop detector in place to prevent runaway cycling.

## Verification performed

- Unit-tested the new helpers directly (no live model calls needed):
  near-duplicate title matching, attempt counting, fix-task loop pickup
  (confirmed a mid-iteration-injected task is visited by the same `for`
  loop), and the syntax checker against both a synthetic broken `.py` file
  and the actual (autonomously-generated) crypto-trading-bot repo.
- Confirmed the tool-call parser fix against the exact broken multi-object
  output the model had produced.
- Ran three full sessions against `crypto-trading-bot` to isolate each bug
  in turn (session 1: caught mid-planning, superseded; session 2: exposed
  the tool-call parsing bug; session 3: confirmed real files + real commits
  + real pushes, then exposed the review/convergence bugs at iteration 7-8).

## Known gaps / good next increments

- Syntax-only verification can't catch semantically-broken-but-valid code
  (wrong imports, mismatched class names). Needs either a real
  `test.command` per project or an "attempt to import/run the entry point"
  check.
- No environment-capability probe before planning — the model chose
  Electron/Node in a sandbox with no `node`/`npm` installed, and there's
  nothing steering it away from an unverifiable stack.
- `test.command` is opt-in per project; nothing auto-detects and sets a
  sensible default from the repo's own structure.
