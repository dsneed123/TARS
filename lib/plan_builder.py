"""TARS plan builder — autonomous implementation planning before coding."""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from lib.claude_runner import ClaudeRunner

logger = logging.getLogger("tars.plan_builder")

PLAN_PROMPT = """You are TARS, an autonomous coding agent. Before implementing a task, create a detailed plan.

## Task
{title}

## Description
{description}

## Repository
{repo}

## Instructions
Analyze the codebase and create an implementation plan. Return a JSON object:

```json
{{
  "approach": "Brief description of the overall approach",
  "complexity": "low|medium|high",
  "estimated_files": ["list", "of", "files", "to", "modify"],
  "steps": [
    {{
      "step": 1,
      "description": "What to do",
      "files": ["affected/files.py"],
      "risk": "low|medium|high"
    }}
  ],
  "dependencies": ["any external dependencies needed"],
  "risks": ["potential risks or issues"],
  "model_recommendation": "sonnet|opus"
}}
```

## Guidelines
- Read relevant files before planning
- Consider existing patterns and conventions
- Identify all files that need changes
- Flag any risky operations
- Recommend Opus model only for genuinely complex tasks (architectural changes, complex algorithms)
- Keep the plan focused and minimal
"""


class PlanBuilder:
    """Generates implementation plans before Claude writes code."""

    def __init__(self, model: str = "sonnet"):
        self.runner = ClaudeRunner(model=model, max_turns=10)

    def create_plan(
        self,
        title: str,
        description: str,
        repo: str,
        cwd: str,
    ) -> dict:
        """Generate an implementation plan for a task.

        Returns dict with: approach, steps, complexity, model_recommendation, etc.
        """
        prompt = PLAN_PROMPT.format(
            title=title,
            description=description,
            repo=repo,
        )

        result = self.runner.run(prompt, cwd=cwd, max_turns=10, timeout=300)
        plan = self._parse_plan(result.get("result", ""))

        # Add metadata
        plan["_tokens_used"] = result.get("tokens_in", 0) + result.get("tokens_out", 0)
        plan["_cost_usd"] = result.get("cost_usd", 0.0)

        logger.info(
            "Plan created: complexity=%s, files=%d, steps=%d, recommended_model=%s",
            plan.get("complexity", "unknown"),
            len(plan.get("estimated_files", [])),
            len(plan.get("steps", [])),
            plan.get("model_recommendation", "sonnet"),
        )

        return plan

    def _parse_plan(self, text: str) -> dict:
        """Parse a plan JSON from Claude's response."""
        import re

        # Try to find JSON object in the response
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # Fallback: return a minimal plan
        logger.warning("Could not parse plan from Claude response, using fallback")
        return {
            "approach": "Direct implementation",
            "complexity": "medium",
            "estimated_files": [],
            "steps": [{"step": 1, "description": text[:500], "files": [], "risk": "medium"}],
            "dependencies": [],
            "risks": ["Plan parsing failed, proceeding with direct implementation"],
            "model_recommendation": "sonnet",
        }

    def should_use_opus(self, plan: dict) -> bool:
        """Determine if Opus should be used based on the plan."""
        if plan.get("model_recommendation") == "opus":
            return True
        if plan.get("complexity") == "high":
            return True
        if len(plan.get("estimated_files", [])) > 10:
            return True
        high_risk_steps = [s for s in plan.get("steps", []) if s.get("risk") == "high"]
        if len(high_risk_steps) > 2:
            return True
        return False

    def format_plan_for_prompt(self, plan: dict) -> str:
        """Format a plan as context to include in the implementation prompt."""
        lines = [f"## Implementation Plan\n"]
        lines.append(f"**Approach:** {plan.get('approach', 'N/A')}\n")
        lines.append(f"**Complexity:** {plan.get('complexity', 'N/A')}\n")

        if plan.get("estimated_files"):
            lines.append("**Files to modify:**")
            for f in plan["estimated_files"]:
                lines.append(f"- {f}")
            lines.append("")

        if plan.get("steps"):
            lines.append("**Steps:**")
            for step in plan["steps"]:
                risk_marker = " ⚠️" if step.get("risk") == "high" else ""
                lines.append(f"{step.get('step', '?')}. {step.get('description', '')}{risk_marker}")
            lines.append("")

        if plan.get("risks"):
            lines.append("**Risks to watch for:**")
            for risk in plan["risks"]:
                lines.append(f"- {risk}")

        return "\n".join(lines)
