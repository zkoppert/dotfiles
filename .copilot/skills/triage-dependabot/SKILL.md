---
name: triage-dependabot
description: Preview or handle Dependabot PRs using the private repository policy, dependency security review, and deduplicated macOS help alerts. Use for Dependabot triage, upgrade review, and routine dependency approval requests.
---

# Handle Dependabot updates safely

## Use the guarded entrypoint

Use `~/.local/bin/triage-dependabot --dry-run` for inspection and preview requests.
If the launcher is missing, stop and report the installation gap.
Never fall back to the historical notification runner for mutating operations.

Read [the repository-mode guide](../../../docs/dependabot-automerge.md) before installation, policy changes, or activation.
Private scope lives in `~/.config/dependabot-automerge/config.yml`, outside Git.
Don't copy private repository names into public files or drafts.

The `launchd` job runs hourly from a fixed release snapshot.
Don't resume a paused job without new approval.
Don't set `enabled: true` or request GitHub mutations without the required approval.

## Preserve the security gate

All stable patch, minor, and major upgrades use the same gate.
The runner requires passing checks, complete dependency evidence, current advisory checks, and an evidence-bound source review.
Incomplete evidence, security findings, invalid reviewer output, and reviewer failures must prevent approval.
Don't bypass review because an update looks small or CI passes.

Approvals and merge requests must reference the evaluated commit.
Human comments and reviews, including the operator's manual feedback, stop automation.
Only recognized machine-generated activity is exempt.
Don't resolve human feedback or bypass branch protection.

## Report the result

Read the printed JSON and diagnostic output.
Distinguish completed merges from proposed actions in dry runs.
Report actionable holds and worker errors without claiming that pending work succeeded.

Normal runs send one macOS summary that opens a secret gist for new or changed actionable problems.
Secret gists are unlisted; anyone with the URL can read them.
Affected repository names and PR links can include private repositories.
Anyone with the URL can also read earlier gist revisions.
The report shows reasons, next steps, and PR links without uploading the full scope, raw logs, or source patches.
Routine waits and unchanged problems stay silent.
Dry runs never send alerts or mutate GitHub, but can invoke Copilot and populate the review cache.
