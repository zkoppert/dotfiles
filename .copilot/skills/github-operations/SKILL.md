---
name: github-operations
description: Use for every GitHub read or write, including issues, pull requests, reviews, workflows, releases, repositories, Projects, gists, search, and API calls. Load gh-axi first.
---

# GitHub operations

Load `gh-axi` first. Use this skill for additional command, output, gist, and external-content rules. `gh-axi` controls command choice if guidance conflicts.

## GitHub CLI & API
- Use `--json` flag with `gh` for structured output that can be parsed programmatically
- When searching across an org, use `gh search prs`, `gh search issues`, etc. with `--owner` filter
- Always disable pagers: `git --no-pager`, `gh --no-pager`, or pipe to `cat`
- **Gists**: I use gists frequently for drafts, sharing, and iteration. **Always create gists as private/secret by default** unless I explicitly ask for a public gist. When editing gists programmatically, use `gh api` to fetch raw file content instead of `gh gist view --raw` (which prepends the description and causes duplication on re-upload)
- **Verify edits to external content**: After modifying any external content (gists, PR descriptions, issue bodies, discussion posts, wiki pages), always fetch the result back and verify the edit applied correctly; check that the content wasn't truncated, corrupted, or partially applied. Do not assume a successful API response means the content is correct. Read it back and confirm.
- **No hard-wrapped prose in GitHub-bound markdown**: Never hard-wrap paragraphs at 80 characters in files destined for `gh pr edit --body-file`, `gh issue create --body-file`, or `gh gist create`. GitHub renders each literal newline as a visible line break. Use single-line paragraphs; let the renderer wrap. Lists, tables, code blocks, and headers are fine on their own lines.
- **Trace the code path before writing rationale**: Before describing *why* a design works in PR descriptions, review comments, or issue replies, trace the actual runtime path in source code. Don't infer behavior from method names or documentation alone; read the implementation. Wrong rationale (even on correct code) erodes reviewer trust and creates confusion when the code is revisited later.

<!-- This boundary keeps the migrated baseline block byte-verifiable. -->
