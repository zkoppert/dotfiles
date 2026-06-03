---
name: triage-notifications
description: Triggers when the user says "triage my notifications", "run notification triage", "what GitHub notifications need attention", "clear my notifications", or any similar request to process their unread GitHub notifications. Runs the dotfiles triage tool which classifies each unread notification, drops noise (CI runs, comments on closed threads, super-linter posts without @mentions), routes high-confidence items (mentions, assignments, security alerts, review requests from NUX teammates) straight to Q1 in ~/repos/zkoppert-todo/todo.yml, sends everything else to inbox, and marks notifications done on GitHub (removing them from the inbox) once the corresponding todo moves to done. Safe to re-run (deduped by thread_id).
---

# Triage GitHub Notifications

## When to use this skill

Use whenever the user asks any of:

- "triage my notifications"
- "run notification triage"
- "what GitHub notifications need attention?"
- "clear my notifications"
- "any new GitHub stuff I need to look at?"

Also offer to run this if they mention being behind on notifications or
buried in GitHub noise.

## What it does

1. Fetches all unread notifications via `gh api /notifications --paginate`.
2. Classifies each one into DROP / Q1 / INBOX based on the rules in
   `triage.py` (mention, assign, security_alert → Q1; CI activity and
   super-linter without @mention → DROP; review_requested from NUX
   teammates → Q1; everything else → INBOX).
3. Adds Q1 and INBOX entries to `~/repos/zkoppert-todo/todo.yml`
   (deduped by `notification.thread_id`).
4. Marks DROP threads done on GitHub (no human confirmation), which removes them from the inbox.
5. Scans the todo file for items previously created by this tool that
   have moved to `status: done` and marks those notifications read.

A launchd job (`com.zkoppert.notification-triage.plist`) runs this every
two hours on weekdays at 8/10/12/14/16/18. This skill is for ad-hoc
runs in between.

## How to run

Default (writes to `~/repos/zkoppert-todo/todo.yml`, sends a macOS
notification if anything actionable was added):

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py
```

Preview without writing or PATCHing:

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py \
  --dry-run --verbose
```

## After running

1. Read the printed summary line (`fetched=N added_q1=N added_inbox=N
   dropped=N already_tracked=N marked_done=N`).
2. If anything landed in Q1, tell the user the count and the titles so
   they know what they're being asked to do.
3. If `errors` lines appear on stderr, surface them so the user can
   investigate (most commonly an expired `gh auth` token).

## What this skill must NOT do

- Don't modify `todo.yml` directly. The script handles atomic writes.
- Don't commit changes to `zkoppert-todo` automatically. The user
  reviews and commits manually (matches existing workflow).
- Don't change the NUX teammate allowlist or add new auto-drop rules
  without explicit approval. The classifier is conservative on purpose.
