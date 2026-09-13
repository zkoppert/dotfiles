---
name: triage-notifications
description: Triggers when the user says "triage my notifications", "run notification triage", "what GitHub notifications need attention", "clear my notifications", or any similar request to process their GitHub notifications. Runs the dotfiles triage tool which aggressively bulk-triages each notification, dropping passive noise (subscribed, team_mention, comments, CI runs, super-linter posts, state changes) and clearing those GitHub notifications only after the local notification ledger records the decision, while keeping direct asks urgent in Q1, ordinary review requests scheduled in Q2 with age escalation, and my own PR status items in the tracker. Dependabot bumps are dropped from the inbox but left unread for triage-dependabot. Safe to re-run (deduped by thread_id / canonical URL).
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
2. Classifies each one into DROP / Q1 / Q2 / INBOX based on the rules in
   `triage.py`:
   - **KEEP_REASONS** (`review_requested`, `assign`, `author`, `mention`,
     `security_alert`) survive: mention/assign → Q1,
     review_requested → Q2 with one-business-day escalation if it remains
     untouched, security_alert → Q2, `author` → INBOX.
   - **Everything else** (`subscribed`, `team_mention`, `comment`,
     `state_change`, `ci_activity`, `manual`, ...) is passive noise and
     **drops** (marked done on GitHub).
   - Comments, direct `mention`/`assign` reasons, security alerts, and
     ordinary review requests route before noise-only title and repository
     filters. Incomplete comment history is retained rather than cleared.
   - **Repo overrides** can be stricter for the remaining reasons. They do
     not override the protected routes above. For unprotected reasons:
     `github/.github` plus private config entries always drop everything;
     `github/curated-data` keeps only the carve-out reasons;
     `github/markup` keeps security titles plus the carve-out;
     private AoR config entries keep only matching titles plus the
     carve-out; any `*/super-linter` keeps only the carve-out reasons.
   - **Dependabot bumps** drop from the inbox but are **left unread on
     GitHub** (never marked done) so the separate `triage-dependabot`
     tool can consume them.
3. Adds Q1, Q2, and INBOX entries to `~/repos/zkoppert-todo/todo.yml`
   (deduped by `notification.thread_id`; an INBOX entry is also
   suppressed when the notification's PR/issue URL is already tracked as
   a `link` or `artifact` on another item, and the suppressed thread is
   marked done on GitHub so it stops re-adding). Writes take an exclusive
   `todo.yml.lock`, re-read the file, apply only the computed deltas, and
   use an atomic replace so concurrent manual edits are preserved.
4. Records DROP decisions in the local ledger, then marks DROP threads
   done on GitHub (no human confirmation), which removes them from the
   inbox (except Dependabot bumps, which stay unread).
5. Scans the todo file for items previously created by this tool that
   have moved to `status: done`, `status: dropped`, or Q4 and marks those
   notifications done only after the ledger records the terminal
   disposition.
6. Commits any resulting `todo.yml` change in the todo repo, then tries
   a best-effort pull and push. Git failures are logged as warnings.
7. Sends one clickable macOS notification for each newly added direct
   mention. Selecting it opens the GitHub subject URL. Other actionable
   reasons, already tracked items, no-op runs, and dry runs stay silent.

A launchd job (`com.zkoppert.notification-triage.plist`) runs this hourly,
24x7. This skill is for ad-hoc runs in between.

## How to run

Default writes to `~/repos/zkoppert-todo/todo.yml` and sends clickable
macOS alerts only for newly added direct mentions:

```bash
~/repos/dotfiles/bin/notification-triage
```

Preview without writing or calling DELETE:

```bash
~/repos/dotfiles/bin/notification-triage --dry-run --verbose
```

## After running

1. Read the printed summary line (`fetched=N unread=N added_q1=N
   added_q2=N added_inbox=N dropped=N ... left_for_dependabot=N
   pruned_stale=N`).
2. If anything landed in Q1, tell the user the count and the titles so
   they know what they're being asked to do.
3. If `errors` lines appear on stderr, surface them so the user can
   investigate (most commonly an expired `gh auth` token).

## What this skill must NOT do

- Don't modify `todo.yml` directly. The script handles atomic writes.
- Don't edit `todo.yml` outside the script's lock plus fresh re-read path.
- Don't fail the triage run just because the best-effort git pull or push
  could not complete.
- Don't mark Dependabot bump notifications done on GitHub. They are left
  unread on purpose for `triage-dependabot`; the classifier already
  enforces this via `skip_mark_done`.
