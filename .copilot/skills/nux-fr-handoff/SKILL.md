---
name: nux-fr-handoff
description: Triggers when the user asks to prepare, draft, generate, or review a NUX first-responder on-call handoff, asks what they worked on during their NUX FR shift, or asks to run the Friday handoff automation. The skill finds the current assigned handoff issue, synthesizes all Copilot sessions active since Monday, refreshes linked GitHub states, creates a secret gist for review, and sends a clickable macOS notification. It never posts the issue comment automatically.
---

# Prepare the NUX first-responder handoff

Use this skill when Zack asks for a NUX FR handoff draft or asks to run the
Friday automation.

## Run it

```bash
nux-fr-handoff
```

Preview issue discovery without invoking Copilot or creating a gist:

```bash
nux-fr-handoff --dry-run --verbose
```

Force a replacement draft for the current handoff issue:

```bash
nux-fr-handoff --force
```

## Safety boundaries

- The Copilot synthesis subprocess can only use `session_store_sql`.
- Python performs every GitHub read and the single intentional secret-gist write.
- The tool never posts, edits, assigns, closes, or comments on the handoff issue.
- The draft must pass structural, size, and secret checks before upload. Installed
  style, handle, and rendering checkers run automatically when available.
- Repeated runs reuse the current week's verified gist unless `--force` is provided.

## After running

Report the gist URL from the command output. If no current handoff issue is
assigned, say so and stop. Do not recreate the workflow manually unless the
runner reports a failure that requires investigation.
