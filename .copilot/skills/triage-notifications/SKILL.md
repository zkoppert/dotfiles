---
name: triage-notifications
description: Triggers when the user says "triage my notifications", "run notification triage", "what GitHub notifications need attention", "clear my notifications", or any similar request to process their GitHub notifications. Use the dotfiles notification worker and its README for current classification, tracker reconciliation, and clearance policy.
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

## How to run

Read [README.md](README.md) before running the worker. It owns the
[classification policy](README.md#how-it-classifies),
[tracker lifecycle](README.md#how-it-integrates-with-zkoppert-todo), and
[ad-hoc commands](README.md#ad-hoc-usage), including the non-mutating preview.
Use those wrapper commands rather than a system-Python invocation or manual
GitHub and tracker edits.

For development validation, follow the [fixture instructions](README.md#tests),
not a live worker or the real installer. Scheduling requires the separate
[attended activation](README.md#schedule); an ad-hoc triage request is not
permission to install or enable a schedule.

## After running

1. Read the worker's printed summary rather than assuming every fetched
   notification was cleared.
2. If anything landed in Q1, tell the user the count and the titles so
   they know what they're being asked to do.
3. Surface errors from stderr so the user can investigate, such as an
   expired `gh auth` token or a failed tracker update.

## What this skill must NOT do

- Don't modify `todo.yml` or the ledger directly; use the worker's locked
  reconciliation path.
- Don't fail the triage run just because the best-effort git pull or push
  could not complete.
- Don't bypass the worker's handoff or clearance decisions with manual
  DELETE calls. Use [triage-dependabot](../triage-dependabot/SKILL.md) for the
  dependency actions it owns.
