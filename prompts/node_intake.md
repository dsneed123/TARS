Classify this coding task. Reply with ONLY a JSON object, no other text.

Task: {{TASK_TITLE}}
Details: {{TASK_DESCRIPTION}}

Repo (summary):
{{REPO_MAP}}

JSON schema:
{"complexity": "trivial|standard|complex", "docs_only": false, "reason": "<10 words"}

- trivial: one small localized change (fix typo, add a flag, tweak text/style)
- standard: a normal feature or fix touching a few files
- complex: multi-file design work, new subsystem, or unclear requirements
- docs_only: true when only documentation/comments/README change
