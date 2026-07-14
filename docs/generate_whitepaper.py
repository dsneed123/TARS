#!/usr/bin/env python3
"""Generate the TARS Technical Whitepaper PDF."""

import os
from fpdf import FPDF

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
DARK_BLUE = (26, 58, 92)       # #1a3a5c  — headings
LIGHT_GRAY = (240, 240, 240)   # code-block bg
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
TABLE_HEADER_BG = (26, 58, 92)
TABLE_HEADER_FG = (255, 255, 255)
TABLE_ALT_ROW = (235, 240, 248)
TABLE_BORDER = (200, 200, 200)
ACCENT_BLUE = (52, 103, 160)

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "TARS-Whitepaper.pdf")

# Font paths (DejaVu Sans — Unicode-capable)
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
SANS = "DejaVuSans"
SANS_BOLD = "DejaVuSans-Bold"
SANS_ITALIC = "DejaVuSans-Oblique"
SANS_BI = "DejaVuSans-BoldOblique"
MONO = "DejaVuSansMono"
MONO_BOLD = "DejaVuSansMono-Bold"

# Logical font family names used throughout
FONT = "DejaVu"       # sans-serif body font
MONO_FONT = "DejaVuM"  # monospace code font


class WhitepaperPDF(FPDF):
    """Custom PDF with header/footer styling."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Register Unicode TTF fonts
        self.add_font(FONT, "",  os.path.join(FONT_DIR, f"{SANS}.ttf"))
        self.add_font(FONT, "B", os.path.join(FONT_DIR, f"{SANS_BOLD}.ttf"))
        self.add_font(FONT, "I", os.path.join(FONT_DIR, f"{SANS_ITALIC}.ttf"))
        self.add_font(FONT, "BI", os.path.join(FONT_DIR, f"{SANS_BI}.ttf"))
        self.add_font(MONO_FONT, "", os.path.join(FONT_DIR, f"{MONO}.ttf"))
        self.add_font(MONO_FONT, "B", os.path.join(FONT_DIR, f"{MONO_BOLD}.ttf"))

    def header(self):
        if self.page_no() == 1:
            return  # cover page — no header
        self.set_font(FONT, "I", 8)
        self.set_text_color(*ACCENT_BLUE)
        self.cell(0, 8, "TARS Technical Whitepaper", align="R")
        self.ln(12)

    def footer(self):
        if self.page_no() == 1:
            return  # cover page — no footer
        self.set_y(-15)
        self.set_font(FONT, "", 8)
        self.set_text_color(140, 140, 140)
        self.cell(0, 10, f"Page {self.page_no() - 1}", align="C")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section_title(pdf: WhitepaperPDF, number: int, title: str):
    pdf.set_font(FONT, "B", 18)
    pdf.set_text_color(*DARK_BLUE)
    pdf.ln(6)
    pdf.cell(0, 12, f"{number}. {title}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    # underline rule
    pdf.set_draw_color(*DARK_BLUE)
    pdf.set_line_width(0.6)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(6)


def subsection(pdf: WhitepaperPDF, title: str):
    pdf.set_font(FONT, "B", 13)
    pdf.set_text_color(*DARK_BLUE)
    pdf.ln(3)
    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def body_text(pdf: WhitepaperPDF, text: str):
    pdf.set_font(FONT, "", 10.5)
    pdf.set_text_color(*BLACK)
    pdf.multi_cell(0, 5.5, text)
    pdf.ln(2)


def bullet_list(pdf: WhitepaperPDF, items: list[str]):
    pdf.set_font(FONT, "", 10.5)
    pdf.set_text_color(*BLACK)
    for item in items:
        pdf.cell(6)  # indent
        pdf.cell(5, 5.5, chr(0x2022))  # bullet
        pdf.multi_cell(0, 5.5, f" {item}")
        pdf.ln(1)
    pdf.ln(2)


def bold_bullet_list(pdf: WhitepaperPDF, items: list[tuple[str, str]]):
    """Bullet list where each item has a bold label and normal description."""
    pdf.set_text_color(*BLACK)
    for label, desc in items:
        x_start = pdf.get_x()
        pdf.cell(6)  # indent
        pdf.cell(5, 5.5, chr(0x2022))  # bullet
        pdf.set_font(FONT, "B", 10.5)
        pdf.write(5.5, f" {label}: ")
        pdf.set_font(FONT, "", 10.5)
        pdf.multi_cell(0, 5.5, desc)
        pdf.ln(1)
    pdf.ln(2)


def code_block(pdf: WhitepaperPDF, text: str):
    pdf.ln(1)
    pdf.set_fill_color(*LIGHT_GRAY)
    pdf.set_font(MONO_FONT, "", 9.5)
    pdf.set_text_color(40, 40, 40)
    lines = text.strip().split("\n")
    block_h = len(lines) * 5.5 + 6
    x = pdf.l_margin
    w = pdf.w - pdf.l_margin - pdf.r_margin
    y_start = pdf.get_y()
    # check if we need a page break
    if y_start + block_h > pdf.h - pdf.b_margin:
        pdf.add_page()
        y_start = pdf.get_y()
    pdf.rect(x, y_start, w, block_h, style="F")
    pdf.set_xy(x + 4, y_start + 3)
    for i, line in enumerate(lines):
        pdf.cell(0, 5.5, line, new_x="LMARGIN", new_y="NEXT")
        if i < len(lines) - 1:
            pdf.set_x(x + 4)
    pdf.ln(5)
    pdf.set_text_color(*BLACK)


def comparison_table(pdf: WhitepaperPDF, headers: list[str],
                     rows: list[list[str]]):
    """Render a comparison table with header row and alternating shading."""
    col_count = len(headers)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    first_col = 42
    other_col = (usable - first_col) / (col_count - 1)
    col_widths = [first_col] + [other_col] * (col_count - 1)
    row_h = 7.5

    # Check space — push to new page if tight
    needed = row_h * (len(rows) + 1) + 10
    if pdf.get_y() + needed > pdf.h - pdf.b_margin:
        pdf.add_page()

    # Header row
    pdf.set_font(FONT, "B", 8)
    pdf.set_fill_color(*TABLE_HEADER_BG)
    pdf.set_text_color(*TABLE_HEADER_FG)
    pdf.set_draw_color(*TABLE_BORDER)
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], row_h, f" {h}", border=1, fill=True)
    pdf.ln(row_h)

    # Data rows
    pdf.set_font(FONT, "", 8)
    pdf.set_text_color(*BLACK)
    for r_idx, row in enumerate(rows):
        if r_idx % 2 == 1:
            pdf.set_fill_color(*TABLE_ALT_ROW)
            fill = True
        else:
            pdf.set_fill_color(*WHITE)
            fill = True
        for i, cell in enumerate(row):
            style = "B" if i == 0 else ""
            pdf.set_font(FONT, style, 8)
            pdf.cell(col_widths[i], row_h, f" {cell}", border=1, fill=fill)
        pdf.ln(row_h)
    pdf.ln(4)


# ---------------------------------------------------------------------------
# Build the PDF
# ---------------------------------------------------------------------------

def build():
    pdf = WhitepaperPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(20, 15, 20)

    # -----------------------------------------------------------------------
    # COVER PAGE
    # -----------------------------------------------------------------------
    pdf.add_page()
    pdf.ln(55)
    pdf.set_font(FONT, "B", 36)
    pdf.set_text_color(*DARK_BLUE)
    pdf.cell(0, 16, "TARS", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font(FONT, "", 16)
    pdf.set_text_color(*ACCENT_BLUE)
    pdf.cell(0, 10, "Task Automation & Repository Steward",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    # Subtitle
    pdf.set_font(FONT, "B", 14)
    pdf.set_text_color(*DARK_BLUE)
    pdf.cell(0, 10, "Introducing the Continuous Development Agent",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font(FONT, "I", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, "A local-first, self-verifying autonomous coding and business tool",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(24)
    # Decorative rule
    pdf.set_draw_color(*DARK_BLUE)
    pdf.set_line_width(0.8)
    cx = pdf.w / 2
    pdf.line(cx - 40, pdf.get_y(), cx + 40, pdf.get_y())
    pdf.ln(10)

    # Author
    pdf.ln(20)
    pdf.set_font(FONT, "B", 11)
    pdf.set_text_color(*DARK_BLUE)
    pdf.cell(0, 7, "Davis Sneed",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Creator & Lead Developer",
             align="C", new_x="LMARGIN", new_y="NEXT")

    # Bottom of cover
    pdf.ln(16)
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 6, "Technical Whitepaper  |  July 2026",
             align="C", new_x="LMARGIN", new_y="NEXT")

    # -----------------------------------------------------------------------
    # SECTION 1 — The Continuous Development Agent
    # -----------------------------------------------------------------------
    pdf.add_page()
    section_title(pdf, 1, "The Continuous Development Agent")

    subsection(pdf, "Defining the CDA")
    body_text(pdf,
        "A Continuous Development Agent (CDA) is an autonomous system that "
        "continuously discovers, implements, tests, and deploys code changes "
        "-- 24/7, without human prompts. It operates as a persistent daemon, "
        "monitoring repositories for work, executing changes through a safe "
        "pipeline, and reporting results. Where traditional AI coding tools "
        "wait for a developer to type, a CDA never stops working."
    )

    subsection(pdf, "Three Generations of AI Coding Tools")
    bold_bullet_list(pdf, [
        ("Generation 1 -- Copilots",
         "Reactive autocomplete tools (GitHub Copilot, Cursor, Cody). They "
         "wait for keystrokes, suggest completions, and vanish when the IDE "
         "closes. No autonomy, entirely session-bound."),
        ("Generation 2 -- Agents",
         "One-shot task executors (Devin, Codex CLI, SWE-Agent). Given a "
         "prompt, they plan and execute a task, then stop. No daemon, no "
         "persistence, no continuous operation."),
        ("Generation 3 -- Continuous Development Agents",
         "Persistent, autonomous systems that run as daemons, discover their "
         "own work, self-heal on failure, manage budgets, and operate "
         "indefinitely. TARS is the first of this generation."),
    ])

    subsection(pdf, "Why \"Continuous\" Matters")
    body_text(pdf,
        "Just as CI/CD transformed deployment from a manual ceremony into an "
        "automated pipeline, CDAs transform development itself. Code changes "
        "flow continuously: tasks are discovered, prioritised, implemented, "
        "tested, reviewed, and merged -- all without a human sitting at a "
        "keyboard. The developer's role shifts from writing every line to "
        "steering strategy, reviewing output, and defining guardrails."
    )

    # -----------------------------------------------------------------------
    # SECTION 2 — The Problem with Current AI Coding Tools
    # -----------------------------------------------------------------------
    pdf.add_page()
    section_title(pdf, 2, "The Problem with Current AI Coding Tools")

    subsection(pdf, "Copilots: Reactive, Session-Bound")
    body_text(pdf,
        "Tools like GitHub Copilot, Cursor, and Sourcegraph Cody are "
        "fundamentally reactive. They watch for keystrokes and offer "
        "suggestions. When the developer closes the IDE, they stop. They "
        "cannot initiate work, discover tasks, or operate independently. "
        "They are powerful typeahead, not agents."
    )

    subsection(pdf, "One-Shot Agents: Single Task, Then Silence")
    body_text(pdf,
        "The next wave -- Devin, OpenAI Codex CLI, SWE-Agent -- brought "
        "genuine autonomy for individual tasks. Give them a prompt and they "
        "plan, code, and test. But when the task is done, they stop. There "
        "is no daemon, no loop, no persistence. Each invocation is a fresh "
        "start."
    )

    subsection(pdf, "The Missing Pieces")
    body_text(pdf,
        "No existing tool provides all of the following, yet each is "
        "essential for truly autonomous development:"
    )
    bullet_list(pdf, [
        "Budget management -- token spend tracking with peak-hour throttling",
        "Self-healing -- automatic patch retries and circuit breakers",
        "Task discovery -- finding work from issues, code analysis, queues",
        "24/7 daemon operation -- persistent, restartable, crash-resilient",
        "Human-out-of-the-loop execution -- no prompts required to begin",
    ])

    # -----------------------------------------------------------------------
    # SECTION 3 — How TARS Works
    # -----------------------------------------------------------------------
    section_title(pdf, 3, "How TARS Works")

    subsection(pdf, "Architecture: Four Layers")
    bold_bullet_list(pdf, [
        ("Shell scripts (bin/)",
         "Orchestration layer. Manages the daemon lifecycle, process "
         "management, lock files, and signal handling."),
        ("Python modules (lib/)",
         "Logic layer. Task queue, budget tracking, Discord notifications, "
         "error classification, circuit breakers, and git operations."),
        ("Local Ollama models",
         "Intelligence layer. TARS runs entirely on locally-hosted open "
         "models -- no Claude API, no per-token billing, no data leaving "
         "the machine. Work is routed by role, not one model doing "
         "everything: a coding-tuned model (qwen2.5-coder:32b) implements "
         "and edits files through a tool-calling agent loop; a larger "
         "reasoning model (deepseek-r1:70b) handles planning, review, and "
         "fixing, where deeper multi-step reasoning matters more than "
         "tool-calling speed."),
        ("Flask controller (controller/)",
         "REST API + dashboard layer. Exposes project management, task "
         "queues, and TARS Go sessions over HTTP with API-key auth, so a "
         "hosted front end (or a friend's browser) can drive a TARS "
         "instance running on someone else's hardware."),
    ])

    subsection(pdf, "Two Ways TARS Works")
    body_text(pdf,
        "TARS operates in two complementary modes, both ending in the same "
        "place: real commits, pushed and visible."
    )
    bold_bullet_list(pdf, [
        ("The daemon loop -- one task at a time",
         "A background process that continuously pulls from a prioritised "
         "queue, implements one task, tests it, self-reviews the diff, and "
         "pushes -- then repeats. Suited to an ongoing backlog of "
         "well-scoped tasks."),
        ("TARS Go -- goal-directed sessions",
         "Give TARS a single high-level goal (\"build X\") instead of a "
         "task list, and it plans its own task breakdown, executes it, "
         "reviews its own completeness against the original goal, and "
         "loops -- adding new tasks and iterating -- until the review "
         "score clears a bar or the iteration budget runs out. This is "
         "closer to how a human would tackle an open-ended project than a "
         "single-shot prompt."),
    ])
    code_block(pdf,
        "TARS Go loop:\n"
        "  plan --> execute --> verify --> review --> (repeat or stop)"
    )

    subsection(pdf, "Task Pipeline (Daemon Mode)")
    body_text(pdf,
        "Tasks flow into the daemon from multiple sources, converging into "
        "a single prioritised queue:"
    )
    bullet_list(pdf, [
        "Manual queue -- developers add tasks via CLI or config files",
        "GitHub issues -- labelled issues are ingested automatically",
        "Auto-discovery -- AI analysis of the codebase surfaces TODOs, "
        "missing tests, code smells, and improvement opportunities",
    ])

    subsection(pdf, "Safety & Verification Systems")
    bold_bullet_list(pdf, [
        ("Circuit breakers",
         "After consecutive failures, TARS pauses the project to prevent "
         "cascading damage, then alerts the team."),
        ("Verify before trusting \"done\"",
         "A task is never accepted as complete just because the model "
         "didn't error -- that only proves it didn't crash. TARS Go runs "
         "an automated syntax check (and the project's own test command, "
         "if configured) after every task, and a failure immediately "
         "queues a fix task in the same iteration rather than moving on."),
        ("Reviewer gets ground truth, not self-reports",
         "The review step that scores a TARS Go session's completeness is "
         "given the actual verification output, not just the tasks' own "
         "summaries -- so a commit message claiming something was fixed "
         "isn't taken on faith."),
        ("Stuck-loop detection",
         "If a proposed fix is a near-duplicate of one already attempted "
         "and failed, TARS refuses to silently retry it forever. After a "
         "small number of repeat attempts, the session stops and flags "
         "itself for human review instead of burning the rest of its "
         "iteration budget circling the same unresolved issue."),
        ("Auto-patch retries",
         "When tests fail, TARS feeds the error back to the model for an "
         "automatic fix attempt before giving up."),
        ("Self-review gates",
         "Every diff is reviewed by a second model pass before merging, "
         "catching regressions the test suite might miss."),
    ])

    # -----------------------------------------------------------------------
    # SECTION 4 — Key Differentiators
    # -----------------------------------------------------------------------
    pdf.add_page()
    section_title(pdf, 4, "Key Differentiators")

    body_text(pdf,
        "TARS introduces capabilities that no other AI coding tool provides "
        "in combination:"
    )

    bold_bullet_list(pdf, [
        ("Local-first -- no API key, no per-token cost",
         "Runs entirely on self-hosted Ollama models. No Claude/OpenAI API "
         "dependency, no data leaving the machine, no per-token bill that "
         "scales with usage -- the only real cost is the hardware."),
        ("Goal-directed autonomous sessions (TARS Go)",
         "Give it a goal, not a task list. It plans its own breakdown, "
         "executes, verifies, reviews its own completeness against the "
         "original goal, and iterates -- closer to how a person tackles "
         "an open-ended project than a single prompt-response cycle."),
        ("Self-verifying, not self-reporting",
         "Task completion is checked against an actual syntax/test run, "
         "not just \"the model said it worked.\" A stuck-loop detector "
         "stops a non-converging session for human review instead of "
         "cycling on the same failing fix indefinitely."),
        ("Autonomous daemon operation",
         "Runs 24/7 as a background process. Survives reboots, handles "
         "signals gracefully, and resumes work automatically."),
        ("Multi-source task discovery",
         "Pulls work from manual queues, GitHub issues, and AI-driven code "
         "analysis -- no human prompt needed."),
        ("Real-time web dashboard + REST API",
         "Live view of daemon status, task progress, and project health "
         "from any browser, plus an API-key-authenticated controller so a "
         "hosted front end can drive a TARS instance remotely."),
        ("Discord notifications",
         "Webhook-based alerts for task completion, failures, and budget "
         "warnings. No bot token required."),
        ("Zero-database architecture",
         "All state lives in JSON files. No PostgreSQL, no Redis, no "
         "migrations. Clone and run."),
        ("Project scaffolding from CLI or API",
         "One command (or API call) sets up a new project config with "
         "repo, branch, tasks, and notification preferences -- including "
         "spinning up a brand-new GitHub repo from a goal description."),
    ])

    # -----------------------------------------------------------------------
    # SECTION 5 — Comparison Matrix
    # -----------------------------------------------------------------------
    section_title(pdf, 5, "Comparison Matrix")

    body_text(pdf,
        "How TARS compares to the leading AI coding tools across key "
        "capabilities:"
    )

    headers = ["Feature", "TARS", "Copilot", "Devin", "Cursor", "Codex CLI"]
    rows = [
        ["Autonomous op.",
         "Yes", "No", "Partial", "No", "Partial"],
        ["Runs as daemon",
         "Yes", "No", "No", "No", "No"],
        ["Local models, no API key",
         "Yes", "No", "No", "No", "No"],
        ["Goal-directed sessions",
         "Yes", "No", "Partial", "No", "Partial"],
        ["Self-verifying (not self-reported)",
         "Yes", "No", "No", "No", "No"],
        ["Self-healing",
         "Yes", "No", "No", "No", "No"],
        ["Task discovery",
         "Yes", "No", "Partial", "No", "No"],
        ["Web dashboard",
         "Yes", "No", "Yes", "No", "No"],
        ["CI/CD integration",
         "Yes", "Partial", "Partial", "No", "Partial"],
        ["Open source",
         "Yes", "No", "No", "No", "Yes"],
    ]
    comparison_table(pdf, headers, rows)

    # -----------------------------------------------------------------------
    # SECTION 6 — Architecture Diagram
    # -----------------------------------------------------------------------
    section_title(pdf, 6, "Architecture Overview")

    body_text(pdf,
        "The following diagram shows the full TARS pipeline, from task "
        "discovery through deployment:"
    )

    code_block(pdf,
        "+-------------------+     +-------------------+     +------------------+\n"
        "|   TASK SOURCES    |     |    TARS DAEMON     |     |     OUTPUTS      |\n"
        "|   or a GOAL       |     |    or TARS GO       |     |                  |\n"
        "+-------------------+     +-------------------+     +------------------+\n"
        "|                   |     |                   |     |                  |\n"
        "| Manual Queue   --------->                   |     |  Git Push        |\n"
        "| GitHub Issues  -------->  Task Prioritiser  |     |  (every task)    |\n"
        "| Auto-Discovery --------->  or Goal Planner  |     |                  |\n"
        "| Controller API --------->                   |     |                  |\n"
        "|                   |     +--------+----------+     +--------+---------+\n"
        "+-------------------+              |                          ^         \n"
        "                                   v                          |         \n"
        "                          +--------+----------+               |         \n"
        "                          | LOCAL OLLAMA MODELS|              |         \n"
        "                          |  (Intelligence)    |              |         \n"
        "                          |                    |              |         \n"
        "                          |  qwen2.5-coder:32b |              |         \n"
        "                          |   Read/Write Code  |              |         \n"
        "                          |  deepseek-r1:70b   |              |         \n"
        "                          |   Plan/Review/Fix  |              |         \n"
        "                          +--------+----------+               |         \n"
        "                                   |                          |         \n"
        "                                   v                          |         \n"
        "                          +--------+----------+               |         \n"
        "                          | VERIFY & REVIEW    |               |         \n"
        "                          |                    +---------------+         \n"
        "                          |  Syntax/Test Gate  |                         \n"
        "                          |  Ground-Truth Rev. |     +------------------+\n"
        "                          |  Stuck-Loop Detect +---->|  Discord Notify  |\n"
        "                          |  Circuit Breaker   |     +------------------+\n"
        "                          +-------------------+                         \n"
    )

    body_text(pdf,
        "State is stored entirely in JSON files under state/. No external "
        "database is required. Configuration lives in YAML files under "
        "config/projects/. The Flask controller (controller/) exposes this "
        "same pipeline over an authenticated REST API, so a hosted front "
        "end can drive it remotely."
    )

    # -----------------------------------------------------------------------
    # SECTION 7 — Successes & Pitfalls
    # -----------------------------------------------------------------------
    pdf.add_page()
    section_title(pdf, 7, "Successes & Pitfalls")

    body_text(pdf,
        "Building TARS has been equal parts breakthrough and hard lesson. "
        "The following is an honest assessment of what has worked, what has "
        "not, and what those outcomes reveal about the current state of "
        "autonomous AI development."
    )

    subsection(pdf, "Successes")

    bold_bullet_list(pdf, [
        ("Fully autonomous commit-to-push pipeline",
         "TARS has successfully discovered tasks, written code, passed "
         "tests, and pushed production-ready commits to GitHub -- all "
         "without a human touching the keyboard. This end-to-end loop "
         "is the core proof of concept and it works."),
        ("Self-healing actually recovers",
         "The auto-patch retry loop is not theoretical. When tests fail, "
         "TARS feeds the error output back to the local model, which "
         "generates a corrected patch. In practice, a meaningful share of "
         "first-attempt failures are resolved within two retries, saving "
         "manual intervention."),
        ("Local models eliminate the billing-liability problem entirely",
         "An autonomous agent with API access and no spend controls is a "
         "billing liability by construction -- the more it works, the more "
         "it costs, with no natural ceiling. Running entirely on "
         "self-hosted Ollama models removes this class of risk outright: "
         "there is no per-token bill that scales with how long a session "
         "runs, so a 30-iteration TARS Go session costs the same as a "
         "5-iteration one -- just time and local compute."),
        ("Zero-database architecture simplified everything",
         "Using JSON state files instead of a database eliminated an "
         "entire class of deployment complexity. There are no migrations, "
         "no connection pools, no ORM. The system can be cloned and run "
         "from a fresh machine in minutes."),
        ("Discord integration creates accountability",
         "Real-time webhook notifications to Discord channels give "
         "visibility into what TARS is doing at all times. This has "
         "been critical for building trust in an autonomous system -- "
         "stakeholders can watch the work happen."),
    ])

    subsection(pdf, "Pitfalls")

    bold_bullet_list(pdf, [
        ("A model reporting success is not proof of success",
         "The clearest lesson from an internal dry run: a local coding "
         "model can report a task as complete and describe plausible "
         "file writes and commands that never actually executed, because "
         "the tool-call format it used wasn't one the agent harness "
         "recognized. The task's own self-report looked identical whether "
         "the work happened or not. This is now treated as the default "
         "assumption, not an edge case -- every layer of TARS Go verifies "
         "against ground truth (does the code parse, do tests pass) "
         "rather than trusting a task's own summary."),
        ("A reviewer that only reads text can be fooled by claims",
         "In the same dry run, an autonomous session plateaued for four "
         "iterations because its self-review step was scoring based on a "
         "file listing and the tasks' own descriptions -- and three "
         "separate commits claimed to have fixed a problem (a duplicated, "
         "unfinished GUI implementation) that was still visibly present "
         "in the repository. The fix was to stop trusting self-reported "
         "text and feed the reviewer actual command output instead."),
        ("A non-converging loop won't stop itself unless told to",
         "The same session re-attempted the identical failing fix across "
         "three separate iterations with nothing noticing the pattern. "
         "An autonomous loop needs an explicit repeat-attempt detector -- "
         "it will not organically recognize that it's stuck."),
        ("Context window limits constrain task complexity",
         "Large refactors that span many files push against a local "
         "model's context window (16K tokens by default). TARS performs "
         "best on focused, well-scoped tasks. Multi-file architectural "
         "changes still require human decomposition into smaller units "
         "of work."),
        ("Test suite quality is the real bottleneck",
         "TARS is only as reliable as the tests it runs against. "
         "Projects with thin or flaky test coverage produce false "
         "positives -- TARS believes its change is correct because "
         "the tests pass, but the tests were not comprehensive enough "
         "to catch the regression. Verification gates that only check "
         "syntax face the same limit: syntactically valid code can still "
         "be semantically broken (wrong imports, mismatched interfaces)."),
        ("AI-generated code can be subtly wrong",
         "Local models produce syntactically correct, well-structured "
         "code that passes tests but occasionally introduces logic that "
         "is plausible rather than correct. Verification and self-review "
         "gates catch many of these, but not all. Human review of merged "
         "PRs remains essential."),
        ("Daemon stability requires defensive engineering",
         "Running 24/7 exposes every edge case: network timeouts, model "
         "load contention, partial writes to state files, zombie child "
         "processes. Each failure mode required its own mitigation. "
         "Building a reliable daemon is significantly harder than "
         "building a tool that runs once."),
        ("Task discovery generates noise",
         "AI-driven auto-discovery of tasks (TODOs, missing tests, code "
         "smells) produces a high volume of low-priority suggestions. "
         "Without careful filtering and prioritisation, TARS can spend "
         "its time on trivial changes while meaningful work sits in "
         "the queue."),
    ])

    subsection(pdf, "Key Takeaway")
    body_text(pdf,
        "The CDA model works. Autonomous, continuous development is not "
        "a theoretical exercise -- TARS proves it can be done today with "
        "existing AI capabilities. But the model is honest about its "
        "boundaries: it excels at well-scoped tasks in well-tested "
        "codebases, and it requires human oversight for architectural "
        "decisions and quality assurance. The right framing is not AI "
        "replacing developers, but AI as an always-on junior engineer "
        "that never sleeps, never forgets, and steadily ships code while "
        "the team focuses on the work that requires human judgement."
    )

    # -----------------------------------------------------------------------
    # Save
    # -----------------------------------------------------------------------
    pdf.output(OUTPUT_PATH)
    print(f"Whitepaper generated: {OUTPUT_PATH}")


if __name__ == "__main__":
    build()
