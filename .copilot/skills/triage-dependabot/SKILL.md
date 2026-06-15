---
name: triage-dependabot
description: Triggers when the user says "triage dependabot", "review my dependabot PRs", "what dependency updates are waiting", "merge safe dependabot bumps", or any similar request to process Dependabot PRs surfaced via GitHub notifications. Runs the dotfiles tool which filters notifications to Dependabot-authored PRs, evaluates each PR against a five-outcome decision tree (auto-merge, request rebase, label-and-merge for security releases, close-prerelease for alpha/beta/rc/dev target versions, or flag for human review in ~/repos/zkoppert-todo/todo.yml), and skips PRs that another human is already engaged on or whose CI is still pending. Safe to re-run; per-PR cooldown prevents double-acting within an hour.
---

# Triage Dependabot PRs

## When to use this skill

Use whenever the user asks any of:

- "triage dependabot"
- "review my dependabot PRs"
- "what dependency updates are waiting?"
- "merge safe dependabot bumps"
- "any dependabot PRs ready to ship?"

Also offer this when the user mentions being behind on dependency
upgrades or seeing a backlog of dependabot notifications.

## What it does

1. Fetches all unread notifications via `gh api /notifications --paginate`.
2. Filters to notifications whose subject is a PullRequest authored by
   `dependabot[bot]` (or `dependabot-preview[bot]`).
3. For each PR, fetches metadata (`gh pr view --json`) and decides one
   of five outcomes:
   - `merge` - enable `gh pr merge --auto --squash --delete-branch`.
   - `rebase` - comment `@dependabot rebase` (suppressed when a prior
     rebase request is newer than the most recent dependabot push).
   - `label-and-merge` - add the `release` label (if the repo defines
     one) and enable auto-merge, for changes the Copilot sub-agent or
     fallback regex classifies as security-related.
   - `close-prerelease` - force-close via
     `gh pr close --delete-branch` when the target version is a
     prerelease (alpha / beta / rc / dev / preview). Catches PRs like
     `bump python from 3.14.5-slim to 3.15.0b2-slim` that should
     never auto-merge. The direct API close exists because Dependabot
     has historically ignored `@dependabot close` comments for hours.
   - `flag-for-review` - write a Q1 entry to
     `~/repos/zkoppert-todo/todo.yml` for human attention.
4. Marks the notification done on GitHub when an action runs.
5. Persists a per-PR cooldown timestamp in
   `~/Library/Logs/triage-dependabot-state.json` so re-runs within an
   hour do not double-act.

A launchd job (`com.zkoppert.triage-dependabot.plist`) runs this every
hour on weekdays from 08:00 through 18:00. This skill is for ad-hoc runs
in between.

## How to run

Default run (writes to `~/repos/zkoppert-todo/todo.yml`, calls
mutating gh endpoints, sends a macOS digest notification):

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-dependabot/triage_dependabot.py
```

Preview without mutations (no merges, no comments, no labels, no todo
writes, no DELETE on the notification):

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-dependabot/triage_dependabot.py \
  --dry-run --verbose
```

Restrict to specific repos while expanding the allowlist:

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-dependabot/triage_dependabot.py \
  --allowed-repo zkoppert/dotfiles \
  --allowed-repo github-community-projects/contributors
```

Disable the Copilot CLI sub-agent (use regex-only security
classification):

```bash
python3 ~/repos/dotfiles/.copilot/skills/triage-dependabot/triage_dependabot.py \
  --no-copilot-subagent
```

## After running

1. Read the printed summary
   (`fetched=N dependabot=N merged=N labeled=N rebased=N flagged=N
   skipped=N cooldown=N already_tracked=N`).
2. If any PRs were flagged, tell the user which repos and why so they
   know what awaits review.
3. If `ERROR:` lines appear on stderr, surface them (most commonly an
   expired `gh auth` token or `copilot` not on `PATH`).

## What this skill must NOT do

- Do not modify `todo.yml` directly. The script handles atomic writes.
- Do not commit changes to `zkoppert-todo` automatically. The user
  reviews and commits manually (matches existing workflow).
- Do not relax the five-outcome decision tree without explicit approval.
  Routing to `flag-for-review` is the safe default whenever any
  uncertainty exists (sub-agent failure, unknown bump kind, missing
  coverage signal).
- Do not auto-merge PRs that show any human review or comment activity.
- Do not spam `@dependabot rebase` comments. The script suppresses the
  comment when a prior rebase request is newer than the latest
  dependabot push.
