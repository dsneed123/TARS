Review this diff against the task. Reply with ONLY a JSON object.

Task: {{TASK_TITLE}}
Details: {{TASK_DESCRIPTION}}

Plan (if any):
{{PLAN}}

Verification results (ground truth — already executed):
{{VERIFY_REPORT}}

Diff:
```diff
{{DIFF}}
```

JSON schema:
{"approved": true, "score": 0-100, "gaps": ["missing or broken things"], "feedback": "what the next pass must fix"}

Judge: does the diff fully implement the task? Real content (no stubs/TODOs)?
Any obvious bugs, security issues, or breakage? Approve unless something is
genuinely wrong or missing — style nitpicks are not rejection reasons.
