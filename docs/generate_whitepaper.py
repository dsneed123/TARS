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
    pdf.cell(0, 8, "A new category of autonomous AI coding tool",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(24)
    # Decorative rule
    pdf.set_draw_color(*DARK_BLUE)
    pdf.set_line_width(0.8)
    cx = pdf.w / 2
    pdf.line(cx - 40, pdf.get_y(), cx + 40, pdf.get_y())
    pdf.ln(10)
    # TARS quote
    pdf.set_font(FONT, "I", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 7,
             '"I have a cue light I can use to show you when I\'m joking,',
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7,
             'if you want."',
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font(FONT, "", 9)
    pdf.cell(0, 6, "-- TARS, Interstellar (2014)",
             align="C", new_x="LMARGIN", new_y="NEXT")

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
    pdf.cell(0, 6, "Technical Whitepaper  |  March 2026",
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

    subsection(pdf, "Architecture: Three Layers")
    bold_bullet_list(pdf, [
        ("Shell scripts (bin/)",
         "Orchestration layer. Manages the daemon lifecycle, process "
         "management, lock files, and signal handling."),
        ("Python modules (lib/)",
         "Logic layer. Task queue, token budget management, Discord "
         "notifications, error classification, and circuit breakers."),
        ("Claude CLI",
         "Intelligence layer. Invoked as a subprocess for each coding task. "
         "Reads code, writes patches, runs tests -- the actual AI brain."),
    ])

    subsection(pdf, "The Daemon Loop")
    body_text(pdf,
        "TARS runs as a background daemon with a continuous work loop:"
    )
    code_block(pdf,
        "discover --> prioritise --> implement --> test -->\n"
        "self-review --> deploy --> notify --> repeat"
    )
    body_text(pdf,
        "Each iteration picks the highest-priority task, invokes Claude CLI "
        "to implement it, runs the project's test suite, performs an "
        "AI-powered self-review of the diff, and (if all gates pass) pushes "
        "the change and notifies via Discord. If any step fails, the "
        "self-healing pipeline retries with diagnostic context."
    )

    subsection(pdf, "Task Pipeline")
    body_text(pdf,
        "Tasks flow into TARS from multiple sources, converging into a "
        "single prioritised queue:"
    )
    bullet_list(pdf, [
        "Manual queue -- developers add tasks via CLI or config files",
        "GitHub issues -- labelled issues are ingested automatically",
        "Auto-discovery -- AI analysis of the codebase surfaces TODOs, "
        "missing tests, code smells, and improvement opportunities",
    ])

    subsection(pdf, "Safety Systems")
    bold_bullet_list(pdf, [
        ("Circuit breakers",
         "After consecutive failures, TARS pauses the project to prevent "
         "cascading damage, then alerts the team."),
        ("Token budgets",
         "Configurable daily/hourly spend limits with peak-hour throttling "
         "to control costs."),
        ("Auto-patch retries",
         "When tests fail, TARS feeds the error back to Claude for an "
         "automatic fix attempt before giving up."),
        ("Self-review gates",
         "Every diff is reviewed by a second AI pass before merging, "
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
        ("Autonomous daemon operation",
         "Runs 24/7 as a background process. Survives reboots, handles "
         "signals gracefully, and resumes work automatically."),
        ("Self-healing pipeline",
         "Auto-patch retry loop feeds test failures back to the AI. Circuit "
         "breakers halt work before damage spreads."),
        ("Token-aware budget management",
         "Tracks spend per-project with configurable daily limits and "
         "peak-hour throttling to optimise cost."),
        ("Multi-source task discovery",
         "Pulls work from manual queues, GitHub issues, and AI-driven code "
         "analysis -- no human prompt needed."),
        ("Real-time web dashboard",
         "Live view of daemon status, task progress, token spend, and "
         "project health from any browser."),
        ("Discord notifications",
         "Webhook-based alerts for task completion, failures, and budget "
         "warnings. No bot token required."),
        ("Zero-database architecture",
         "All state lives in JSON files. No PostgreSQL, no Redis, no "
         "migrations. Clone and run."),
        ("Project scaffolding from CLI",
         "One command sets up a new project config with repo, branch, "
         "tasks, and notification preferences."),
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
        ["Self-healing",
         "Yes", "No", "No", "No", "No"],
        ["Token budgeting",
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
        "+-------------------+     +-------------------+     +------------------+\n"
        "|                   |     |                   |     |                  |\n"
        "| Manual Queue   --------->                   |     |  Git Push        |\n"
        "| GitHub Issues  -------->  Task Prioritiser  |     |  (auto-merge)    |\n"
        "| Auto-Discovery --------->                   |     |                  |\n"
        "|                   |     +--------+----------+     +--------+---------+\n"
        "+-------------------+              |                          ^         \n"
        "                                   v                          |         \n"
        "                          +--------+----------+               |         \n"
        "                          |   Claude CLI      |               |         \n"
        "                          |   (Intelligence)  |               |         \n"
        "                          |                   |               |         \n"
        "                          |  Read Code        |               |         \n"
        "                          |  Write Patches    |               |         \n"
        "                          |  Run Tests        |               |         \n"
        "                          +--------+----------+               |         \n"
        "                                   |                          |         \n"
        "                                   v                          |         \n"
        "                          +--------+----------+               |         \n"
        "                          |  SAFETY PIPELINE  |               |         \n"
        "                          |                   +---------------+         \n"
        "                          |  Test Gate        |                         \n"
        "                          |  Self-Review Gate |     +------------------+\n"
        "                          |  Circuit Breaker  +---->|  Discord Notify  |\n"
        "                          |  Auto-Patch Retry |     +------------------+\n"
        "                          +-------------------+                         \n"
    )

    body_text(pdf,
        "State is stored entirely in JSON files under state/. No external "
        "database is required. Configuration lives in YAML files under "
        "config/projects/."
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
         "TARS feeds the error output back to Claude, which generates a "
         "corrected patch. In practice, roughly 60-70% of first-attempt "
         "failures are resolved within two retries, saving manual "
         "intervention."),
        ("Token budgets prevent runaway spend",
         "Without guardrails, an autonomous agent with API access is a "
         "billing liability. TARS's per-project daily limits and "
         "peak-hour throttling have kept costs predictable across "
         "multi-day unattended runs."),
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
        ("Context window limits constrain task complexity",
         "Large refactors that span many files push against Claude's "
         "context window. TARS performs best on focused, well-scoped "
         "tasks. Multi-file architectural changes still require human "
         "decomposition into smaller units of work."),
        ("Test suite quality is the real bottleneck",
         "TARS is only as reliable as the tests it runs against. "
         "Projects with thin or flaky test coverage produce false "
         "positives -- TARS believes its change is correct because "
         "the tests pass, but the tests were not comprehensive enough "
         "to catch the regression."),
        ("AI-generated code can be subtly wrong",
         "Claude produces syntactically correct, well-structured code "
         "that passes tests but occasionally introduces logic that is "
         "plausible rather than correct. The self-review gate catches "
         "many of these, but not all. Human review of merged PRs "
         "remains essential."),
        ("Daemon stability requires defensive engineering",
         "Running 24/7 exposes every edge case: network timeouts, API "
         "rate limits, partial writes to state files, zombie child "
         "processes. Each failure mode required its own mitigation. "
         "Building a reliable daemon is significantly harder than "
         "building a tool that runs once."),
        ("Task discovery generates noise",
         "AI-driven auto-discovery of tasks (TODOs, missing tests, code "
         "smells) produces a high volume of low-priority suggestions. "
         "Without careful filtering and prioritisation, TARS can spend "
         "its budget on trivial changes while meaningful work sits in "
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
