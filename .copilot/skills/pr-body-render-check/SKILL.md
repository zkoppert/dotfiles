---
name: pr-body-render-check
description: This skill should be used AFTER updating a PR description, issue body, or gist via `gh pr edit --body-file`, `gh issue edit --body-file`, or `gh gist create/edit` to verify the body renders cleanly on GitHub. It runs a Python checker that flags hard-wrapped paragraphs (orphaned line breaks), markdown links split across lines, split table rows, and bodies approaching GitHub's 65,535-char limit. Also triggered when the user asks to "check PR body rendering", "verify the PR description renders", or "did the markdown wrap correctly".
---

# pr-body-render-check: verify PR/issue body rendering after edit

Use this skill **after** updating a PR description, issue body, or gist via `gh pr edit --body-file`, `gh issue edit --body-file`, or `gh gist edit`.

## When to invoke

After any `gh pr edit`, `gh issue edit`, or `gh gist create/edit` that sets a body from a file. The check catches:

- **Orphaned line breaks** - paragraphs that were hard-wrapped at 80 chars and now render as choppy short lines on GitHub
- **Broken table alignment** - pipes that got split across lines
- **Malformed links** - `[text](url)` split by a newline between `]` and `(`
- **Truncation** - body was silently cut off (e.g., exceeded GitHub's 65,535 char limit)

## How to run

```bash
# Check a PR after editing
python3 ~/.copilot/skills/pr-body-render-check/check.py --pr 123 --repo owner/repo

# Check an issue
python3 ~/.copilot/skills/pr-body-render-check/check.py --issue 123 --repo owner/repo

# Check a local file (dry-run, no fetch)
python3 ~/.copilot/skills/pr-body-render-check/check.py --file /tmp/pr-body.md
```

## What it checks

1. **Short-line detection** - flags sequences of 3+ consecutive non-blank, non-structural lines under 90 chars (likely hard-wrapped prose).
2. **Split link detection** - finds `]\n(` patterns that break markdown links.
3. **Split table detection** - finds `|` lines followed by non-`|` continuation.
4. **Length check** - warns if body exceeds 60,000 chars (approaching GitHub's limit).

## How to handle violations

1. Run `gh-unwrap-body --in-place <file>` to fix hard-wrapped prose.
2. Re-upload with `gh pr edit <number> --repo <repo> --body-file <file>`.
3. Re-run this check to confirm clean.
