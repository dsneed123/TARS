"""TARS Discord mode commands — parse, execute, and pipeline mode operations."""

import asyncio
import hashlib
import io
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("tars.discord_modes")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
CONFIG_DIR = TARS_HOME / "config"
QUEUES_DIR = CONFIG_DIR / "queues"

# All recognized mode prefixes
MODES = {"chat", "implement", "pdf", "list", "research"}


# ---------------------------------------------------------------------------
# Mode Parser
# ---------------------------------------------------------------------------

def parse_modes(text: str) -> tuple[set[str], str]:
    """Parse !mode tokens from anywhere in a message.

    Returns (set_of_modes, clean_prompt_text).
    Examples:
        "!research !list best gear" -> ({"research", "list"}, "best gear")
        "find leads then !list them then !pdf" -> ({"list", "pdf"}, "find leads then them then")
        "!research best diving spots !pdf" -> ({"research", "pdf"}, "best diving spots")
    """
    modes = set()
    remaining = text.strip()

    # Find all !mode tokens anywhere in the message
    def replace_mode(m):
        mode = m.group(1).lower()
        if mode in MODES:
            modes.add(mode)
            return " "  # Replace with space
        return m.group(0)  # Keep unknown !tokens

    remaining = re.sub(r"!(\w+)", replace_mode, remaining)

    # Clean up extra whitespace and filler words left behind
    remaining = re.sub(r"\s+", " ", remaining).strip()
    # Remove dangling connectors at start/end
    remaining = re.sub(r"^(then|and|also|,)\s*", "", remaining, flags=re.IGNORECASE).strip()
    remaining = re.sub(r"\s*(then|and|also|,)\s*$", "", remaining, flags=re.IGNORECASE).strip()
    # Collapse internal "then them then" → "them"
    remaining = re.sub(r"\bthen\b", "", remaining, flags=re.IGNORECASE)
    remaining = re.sub(r"\s+", " ", remaining).strip()

    return modes, remaining


# ---------------------------------------------------------------------------
# Web Researcher (DuckDuckGo)
# ---------------------------------------------------------------------------

class WebResearcher:
    """Search the web via DuckDuckGo and return summarized results."""

    @staticmethod
    async def search(query: str, max_results: int = 5) -> str:
        """Run a DuckDuckGo search and return formatted results.

        Returns a text block of search results for prompt injection.
        """
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(
                None, WebResearcher._search_sync, query, max_results
            )
            return results
        except Exception as e:
            logger.warning("Web search failed: %s", e)
            return f"(Web search failed: {e}. Responding without search results.)"

    @staticmethod
    def _search_sync(query: str, max_results: int) -> str:
        """Synchronous DuckDuckGo search."""
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return "(duckduckgo-search package not installed. Run: pip install duckduckgo-search)"

        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                results.append(f"- **{title}**\n  {body}\n  Source: {href}")

        if not results:
            return "(No search results found.)"

        return "## Web Search Results\n\n" + "\n\n".join(results)


# ---------------------------------------------------------------------------
# PDF Generator (reportlab)
# ---------------------------------------------------------------------------

class PDFGenerator:
    """Generate PDF documents from text/markdown content."""

    @staticmethod
    async def generate(content: str, title: str = "TARS Report") -> Optional[bytes]:
        """Generate a PDF from text content.

        Returns PDF bytes or None on failure.
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, PDFGenerator._generate_sync, content, title
            )
        except Exception as e:
            logger.error("PDF generation failed: %s", e)
            return None

    @staticmethod
    def _generate_sync(content: str, title: str) -> bytes:
        """Synchronous PDF generation via reportlab."""
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import inch
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
            )
            from reportlab.lib.enums import TA_LEFT
            from reportlab.lib.colors import HexColor
        except ImportError:
            raise RuntimeError("reportlab not installed. Run: pip install reportlab")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=0.75 * inch,
            bottomMargin=0.75 * inch,
        )

        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle(
            "CustomTitle",
            parent=styles["Title"],
            fontSize=18,
            spaceAfter=20,
            textColor=HexColor("#1a1a2e"),
        )
        heading_style = ParagraphStyle(
            "CustomHeading",
            parent=styles["Heading2"],
            fontSize=14,
            spaceBefore=16,
            spaceAfter=8,
            textColor=HexColor("#16213e"),
        )
        body_style = ParagraphStyle(
            "CustomBody",
            parent=styles["Normal"],
            fontSize=11,
            leading=16,
            spaceAfter=8,
        )
        bullet_style = ParagraphStyle(
            "CustomBullet",
            parent=styles["Normal"],
            fontSize=11,
            leading=16,
            leftIndent=20,
            spaceAfter=4,
        )
        footer_style = ParagraphStyle(
            "Footer",
            parent=styles["Normal"],
            fontSize=8,
            textColor=HexColor("#888888"),
        )

        story = []

        # Title
        story.append(Paragraph(title, title_style))
        story.append(Paragraph(
            f"Generated by TARS | {datetime.now().strftime('%B %d, %Y')}",
            footer_style,
        ))
        story.append(Spacer(1, 12))

        # Parse content into paragraphs
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                story.append(Spacer(1, 6))
                continue

            # Heading markers
            if line.startswith("### "):
                story.append(Paragraph(PDFGenerator._escape(line[4:]), heading_style))
            elif line.startswith("## "):
                story.append(Paragraph(PDFGenerator._escape(line[3:]), heading_style))
            elif line.startswith("# "):
                story.append(Paragraph(PDFGenerator._escape(line[2:]), title_style))
            # Bullet points
            elif line.startswith("- ") or line.startswith("* "):
                text = line[2:]
                # Handle bold markers
                text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
                story.append(Paragraph(f"&bull; {text}", bullet_style))
            # Numbered items
            elif re.match(r"^\d+\.\s", line):
                text = re.sub(r"^\d+\.\s*", "", line)
                text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
                story.append(Paragraph(text, bullet_style))
            # Regular text
            else:
                text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
                text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
                story.append(Paragraph(PDFGenerator._escape_minimal(text), body_style))

        doc.build(story)
        return buf.getvalue()

    @staticmethod
    def _escape(text: str) -> str:
        """Escape text for reportlab XML."""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        return text

    @staticmethod
    def _escape_minimal(text: str) -> str:
        """Escape ampersands but preserve <b>, <i> tags."""
        text = text.replace("&", "&amp;")
        return text


# ---------------------------------------------------------------------------
# Task Creator (for !implement mode)
# ---------------------------------------------------------------------------

def create_implementation_task(
    project: str,
    title: str,
    description: str,
    priority: int = 150,
) -> dict:
    """Create a TARS task to implement a website change.

    Appends to config/queues/<project>.yaml.
    Returns the task dict.
    """
    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    queue_path = QUEUES_DIR / f"{project}.yaml"

    if queue_path.exists():
        with open(queue_path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    tasks = data.setdefault("tasks", [])

    task_hash = hashlib.md5(
        f"{project}{title}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:8]

    task = {
        "id": f"discord-{task_hash}",
        "title": title,
        "description": description,
        "project": project,
        "priority": priority,
        "status": "pending",
        "source": "discord",
        "created": datetime.now().isoformat(),
    }
    tasks.append(task)

    with open(queue_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    logger.info("Created implementation task: %s for %s", task["id"], project)
    return task


# ---------------------------------------------------------------------------
# Mode Pipeline
# ---------------------------------------------------------------------------

async def run_mode_pipeline(
    modes: set[str],
    prompt: str,
    chat_engine,
    channel_id: str,
    server_config: dict,
    system_prompt: str,
    username: str = "User",
) -> dict:
    """Execute the mode pipeline and return results.

    Returns dict with:
        text: str — the response text
        pdf_bytes: Optional[bytes] — PDF if !pdf mode was used
        task: Optional[dict] — task dict if !implement mode was used
    """
    result = {
        "text": "",
        "pdf_bytes": None,
        "task": None,
    }

    # If no modes specified, default to chat
    if not modes:
        modes = {"chat"}

    # --- Step 1: Research (web search) ---
    research_context = ""
    if "research" in modes:
        research_context = await WebResearcher.search(prompt)

    # --- Step 2: Build mode-specific instructions ---
    mode_instructions = []

    if "list" in modes:
        mode_instructions.append(
            "FORMAT YOUR RESPONSE AS A STRUCTURED LIST with clear categories, "
            "bullet points, and organized sections. Use markdown formatting."
        )

    if "implement" in modes:
        mode_instructions.append(
            "The user wants to make a change to their website. Analyze the request "
            "and provide a clear, detailed description of what should be implemented. "
            "Include specific page locations, components, content, and any technical details. "
            "This description will be used as a task specification for implementation."
        )

    if "pdf" in modes:
        mode_instructions.append(
            "Structure your response with clear headings (##), bullet points, "
            "and organized sections suitable for a professional PDF document."
        )

    combined_instructions = "\n\n".join(mode_instructions)

    # --- Step 3: Chat with Claude ---
    full_prompt = prompt
    if research_context:
        full_prompt = f"{research_context}\n\n---\n\nBased on the above research, respond to: {prompt}"

    response_text = await chat_engine.chat(
        channel_id=channel_id,
        user_message=full_prompt,
        system_prompt=system_prompt,
        username=username,
        mode_instructions=combined_instructions,
    )
    result["text"] = response_text

    # --- Step 4: Create implementation task ---
    if "implement" in modes:
        project = server_config.get("project")
        is_error = response_text.startswith("Sorry, I encountered an error")
        if project and not is_error:
            # Use the prompt as title, response as detailed description
            task_title = prompt[:100] if len(prompt) <= 100 else prompt[:97] + "..."
            task = create_implementation_task(
                project=project,
                title=task_title,
                description=f"Requested via Discord by {username}.\n\n{response_text}",
            )
            result["task"] = task
        elif project and is_error:
            # Still create the task but with the original prompt as description
            task_title = prompt[:100] if len(prompt) <= 100 else prompt[:97] + "..."
            task = create_implementation_task(
                project=project,
                title=task_title,
                description=f"Requested via Discord by {username}.\n\n{prompt}",
            )
            result["task"] = task

    # --- Step 5: Generate PDF ---
    if "pdf" in modes:
        pdf_title = prompt[:80] if len(prompt) <= 80 else prompt[:77] + "..."
        pdf_bytes = await PDFGenerator.generate(response_text, title=pdf_title)
        result["pdf_bytes"] = pdf_bytes

    return result
