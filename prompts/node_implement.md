Implement this task completely. Build the real thing — no stubs, no TODOs, no
placeholder text.

Task: {{TASK_TITLE}}
Details: {{TASK_DESCRIPTION}}

Plan:
{{PLAN}}

{{FEEDBACK_SECTION}}Repo map (paths + symbols — read files before editing them):
{{REPO_MAP}}

How to work:
- Read the files you'll touch first; follow the project's existing conventions.
- Prefer edit_file for changes to existing files (cheaper than rewriting).
- Commit each coherent unit of work: `git add <files> && git commit -m "<imperative summary>"`.
  Never `git add -A` blindly, never commit caches, never `git push` (the pipeline pushes).
- Verify before finishing: run the build/tests, or open what you changed and check it.
- Call finish with a one-paragraph summary when the task is fully done.
