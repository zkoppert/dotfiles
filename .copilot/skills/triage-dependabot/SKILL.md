---
name: triage-dependabot
description: Triggers when the user says "triage dependabot", "review my dependabot PRs", "what dependency updates are waiting", "merge safe dependabot bumps", or any similar request to process Dependabot PRs surfaced via GitHub notifications. Use the dotfiles Dependabot worker and its README for the current decision policy and human-ownership safeguards.
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

## How to run

Read [README.md](README.md) before running the worker. It owns the
[decision policy and guards](README.md#decision-tree),
[tracker cleanup lifecycle](README.md#integration-with-zkoppert-todo), and
[ad-hoc commands](README.md#ad-hoc-usage), including previews, repository
filtering, and the security-classification opt-out. Use those wrapper commands
rather than a system-Python invocation or manual PR and tracker mutations.

For development validation, use the [fixture instructions](README.md#tests),
not a live worker or the real installer. Follow the separate
[attended-activation guide](README.md#schedule) only when scheduling is
explicitly authorized.

## After running

1. Read the worker's printed summary and distinguish completed actions from
   retained notifications or pending auto-merges.
2. If any PRs were flagged, tell the user which repos and why so they
   know what awaits review.
3. Surface errors from stderr so the user can investigate, such as an
   expired `gh auth` token or a failed tracker update.

## What this skill must NOT do

- Do not modify `todo.yml` or the ledger directly; use the worker's locked
  reconciliation path.
- Do not fail the triage run just because the best-effort git pull or push
  could not complete.
- Do not widen the worker's decision policy or bypass its ownership guard
  without explicit approval. Do not manually mutate PRs it leaves for human
  attention or hands to general notification triage.
- Do not bypass the worker's rebase-comment suppression or retry cooldown.
