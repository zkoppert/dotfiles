---
name: triage-notifications
description: Triggers when the user says "triage my notifications", "run notification triage", "what GitHub notifications need attention", "clear my notifications", or any similar request to process their unread GitHub notifications. Runs the dotfiles triage tool which aggressively bulk-triages each notification, dropping passive noise (subscribed, team_mention, comments, CI runs, super-linter posts, state changes) and clearing those GitHub notifications, while keeping only personal-action items (mentions, assignments, security alerts, review requests, my own PRs) plus AoR-matched items, routing them into ~/repos/zkoppert-todo/todo.yml. Dependabot bumps are dropped from the inbox but left unread for triage-dependabot. Safe to re-run (deduped by thread_id).
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

This is an aggressive "bulk triage": passive subscription noise is
dropped and cleared from GitHub, and only personal-action items survive.

1. Fetches all notifications via `gh api /notifications?all=true --paginate`.
2. Classifies each one into DROP / Q1 / INBOX based on the rules in
   `triage.py`:
   - **KEEP_REASONS** (`review_requested`, `assign`, `author`, `mention`,
     `security_alert`) survive: mention/assign/security_alert → Q1,
     review_requested from a NUX teammate → Q1, otherwise → INBOX.
   - **Everything else** (`subscribed`, `team_mention`, `comment`,
     `state_change`, `ci_activity`, `manual`, ...) is passive noise and
     **drops** (marked done on GitHub).
   - **Repo overrides** run first and can be stricter. A safety carve-out
     applies: a direct `mention`/`assign` or a `security_alert` always
     survives these gates (except on fully tuned-out repos). Otherwise:
     `github/.github` plus private config entries always drop everything;
     `github/curated-data` keeps only the carve-out reasons;
     `github/markup` keeps security titles plus the carve-out;
     private AoR config entries keep only matching titles plus the
     carve-out; any `*/super-linter` keeps only the carve-out reasons.
   - **Dependabot bumps** drop from the inbox but are **left unread on
     GitHub** (never marked done) so the separate `triage-dependabot`
     tool can consume them.
3. Adds Q1 and INBOX entries to `~/repos/zkoppert-todo/todo.yml`
   (deduped by `notification.thread_id`).
4. Marks DROP threads done on GitHub (no human confirmation), which
   removes them from the inbox (except Dependabot bumps, which stay
   unread).
5. Scans the todo file for items previously created by this tool that
   have moved to `status: done` and marks those notifications done.

A launchd job (`com.zkoppert.notification-triage.plist`) runs this every
two hours on weekdays at 8/10/12/14/16/18. This skill is for ad-hoc
runs in between.

## How to run

Default (writes to `~/repos/zkoppert-todo/todo.yml`, sends a macOS
notification if anything actionable was added):

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py
```

Preview without writing or calling DELETE:

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py \
  --dry-run --verbose
```

## After running

1. Read the printed summary line (`fetched=N added_q2=N added_inbox=N
   dropped=N ... left_for_dependabot=N pruned_stale=N`).
2. If anything landed in Q1, tell the user the count and the titles so
   they know what they're being asked to do.
3. If `errors` lines appear on stderr, surface them so the user can
   investigate (most commonly an expired `gh auth` token).

## What this skill must NOT do

- Don't modify `todo.yml` directly. The script handles atomic writes.
- Don't commit changes to `zkoppert-todo` automatically. The user
  reviews and commits manually (matches existing workflow).
- Don't mark Dependabot bump notifications done on GitHub. They are left
  unread on purpose for `triage-dependabot`; the classifier already
  enforces this via `skip_mark_done`.
