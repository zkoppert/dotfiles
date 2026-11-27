# Automate selected Dependabot updates
Notification-based discovery can miss dependency updates after someone clears the notification.
The scheduled runner scans configured repositories directly and reuses the existing version parser.

## Configure the scope
Create `~/.config/dependabot-automerge/config.yml` outside Git:

```yaml
# Keep disabled until the preview and activation receive approval.
enabled: false
organizations: [example-org]  # Include current and future repositories.
repositories: [another-owner/tools]  # Include this exact repository only.
exclude_repositories: []  # Exclusions override both inclusion lists.
```

Missing, invalid, or empty scope stops processing.

## Understand the decisions
- Stable patch, minor, and major upgrades use the same checks, without a coverage-setting threshold.
- At least one check must succeed, and all reported checks must pass or have a neutral or skipped conclusion.
- Pending, missing, or failed checks prevent approval.
- Human comments and reviews, unknown versions, prereleases, and existing deferred auto-merge prevent action.
- Your manual comments and reviews also stop automation; only marked machine-generated activity is exempt.
- The existing dependency exclusion still applies.
- The runner requests a Dependabot rebase once for each head revision that needs one.
- Rebase recovery takes precedence over stale CI failures; passing checks remain mandatory before approval or merge.
- Approvals reference the evaluated commit; merge requests require that same commit.
- The runner requires a clean merge state after approval and never requests a branch-policy bypass.
- Only a successful merge response counts as a completed merge.

Passing checks cannot prove compatibility that the tests don't cover.
The runner doesn't edit code, close PRs, change release labels, or modify GitHub notifications or the todo repository.
Merge queues and unsupported merge policies remain exceptions; the runner doesn't bypass them.

## Review dependency security
The approval gate compares dependency graph snapshots and resolves old and new stable versions to upstream GitHub commits.
When both graph entries omit a Python source URL, it checks version-specific PyPI metadata for both releases.
Both releases must identify the same GitHub source repository and match the requested package and exact version.
Missing, conflicting, redirected, oversized, or unavailable metadata still prevents approval.
Consumer patches also come from an immutable base/head comparison, not the live PR-files endpoint.
Immutable Git trees supply file modes. Mode changes, symlinks, submodules, and incomplete tree data require manual review.
It checks reviewed, unreviewed, and malware advisories for each target version, independently of CI.
A tool-free Copilot reviewer examines the consuming PR patches and upstream code patches before approval.
All model tools, including shell, file, and network tools, are disabled through an explicit nonmatching catalog selector.
An empty `--available-tools=` restores this CLI's default catalog and must not replace the nonmatching selector.
The reviewer uses temporary configuration directories and an explicit environment allowlist, without approval-token environment variables.

Missing source links, new or removed packages, unresolved tags, divergent histories, incomplete patches, and oversized changes require manual review.
Every consumer file must have matching dependency evidence. Mixed or partially mapped updates require manual review.
Exact dependency replacements in `pyproject.toml` can use the adjacent `uv.lock` graph entries.
Commit-pinned Actions require exact version comments, matching upstream tags, and complete graph coverage.
The runner checks each changed workflow reference, including repeated references across files.
Other workflow edits and Action references missing from the graph still require manual review.
The graph supplies dependency mappings; the model cross-checks the full covered manifest patch against that list.
This is not an independent deterministic parser for every manifest format.
Findings, invalid model responses, and reviewer failures also prevent approval.
Code verdicts use an evidence-bound cache; advisory queries still run before cached verdicts can authorize approval.
Cache directories and entries must belong to the current user and must not permit group or public writes.
On macOS, extended ACLs on cache directories or entries require manual repair before reuse.
Dry runs can query APIs, invoke Copilot, and populate this local cache, but cannot approve or merge.

The review adds evidence, not a security guarantee.
It doesn't verify published package archives, signatures, or package-to-source provenance.
It checks the PR author, but it doesn't verify who pushed each commit or validate commit signatures.
The base branch can advance after revalidation. GitHub's configured branch rules decide whether that requires new checks.
Source patches enter the authenticated Copilot service. Reports keep review identities, revision IDs, and findings, not source patches.
Don't publish private reports or Copilot session logs.
The GitHub API adapter pins `gh-axi@0.1.35` through Central Feed Services (CFS).
Run `bin/triage-dependabot --prepare-runtime` once in each fixed release before activation.
Preparation installs Python dependencies and the GitHub CLI into the ignored `.dependabot-runtime` directory through CFS.
Scheduled runs use those installed executables without package resolution or CFS authentication.
A missing runtime stops the worker and requests help. Preparation does not approve or merge updates.
The standalone alert helper retains CFS-based CLI resolution when no prepared CLI exists.

## Recover failed lookups
A failed API command or timeout gets up to three attempts for `GET` requests only.
The adapter waits one second before the second attempt and two seconds before the third.
Each attempt has a 90-second timeout, so one lookup can take about 273 seconds before process cleanup overhead.
Timeout cleanup kills the command's process group so descendants cannot keep output pipes open indefinitely.
The bounded retry policy also covers persistent access failures.
Successful commands with invalid or truncated responses still stop processing without a retry.
Write requests never retry, including approval, merge, rebase, and gist publication requests.
A lost write response can leave the action completed on GitHub, so a retry could repeat that action.

Exhausted requests retain attempt numbers, exit codes or timeouts, and both output streams in private diagnostics.
Help gists continue to show generic failure summaries, not raw command output.

## Receive help alerts
Normal runs send one clickable macOS summary when new or changed holds or worker errors need attention.
Every actionable alert opens a secret GitHub gist with blocked updates, concise reasons, next steps, and validated PR links.
Secret gists are unlisted, not access-restricted. Anyone with the URL can read them.
Reports include affected repository names, PR numbers, and links, including private repositories.
The handler reuses one managed gist for each report path, so older notifications open the last published alert.
The gist shows its report time; it isn't a live view of GitHub.
GitHub retains earlier gist revisions. Anyone with the URL can read those revisions, including superseded references.
Private JSON snapshots still retain the original details after later scans replace the latest report.
The full repository scope, raw logs, review findings, and source patches stay local.
Startup failure reports withhold PR references until a scan confirms the current scope.
Unconfirmed holds stay queued locally and aren't acknowledged as delivered.
Unchanged issues stay silent. A new revision, changed reason, or recurring issue after resolution can alert again.
Pending CI, mergeability calculations, and rebase waits don't trigger alerts.
Disabled configuration and overlapping worker invocations stay silent after startup.
A separate helper reports bootstrap failures before the worker starts; this helper needs no third-party Python packages.
Dry-run and help requests suppress bootstrap alerts too.

Alert history lives beside the report in `dependabot-automerge.alerts.json`.
The managed gist identity lives in the private `dependabot-automerge.gist.json` file beside that history.
The publisher checks ownership, secret visibility, and full content after each write.
It recovers an ambiguous creation response through the saved unique marker before creating another gist.
Generated Markdown must pass the local `validate-style` skill before publication.
Failed uploads and notification attempts remain eligible for retry, and the report records delivery errors.
If publication or storage fails, a diagnostic notification has no file-opening action, so it doesn't launch a JSON editor.
History-storage failures also request help; warnings can repeat until that storage works again.
Undelivered worker errors survive later successful scans until the notifier accepts delivery.
Undelivered holds remain queued through waits on the same revision; a newer revision or confirmed resolution clears them.
Earlier versions classified an existing rebase request as a hold.
A later wait or rebase on the same revision clears that legacy hold and its alert history.
Other unresolved holds remain queued, even when the wait reason changes.
The wrapper also reports unexpected worker exits when no failure report reached disk.
If report storage fails, a generic private notification points to the diagnostic log without recording durable delivery.
Receipt-directory failures use the same fallback, and helper-startup failures try another available Python interpreter.
The notifier uses the native Homebrew executable when available, rather than a potentially incompatible Ruby shim.
Dry runs never send alerts. macOS notification settings and Focus mode still control presentation.

## Test the review boundary
Run `python scripts/test_dependency_review.py --integration` in the wrapper's Python environment to probe the actual Copilot CLI.
The opt-in test checks malicious patch instructions, synthetic file reads, and unauthorized writes without executing dependency code.
A separate positive control confirms that the canary file becomes readable only when the read restrictions are removed.

## Preview and activate
Run `bin/triage-dependabot --prepare-runtime`, then `bin/triage-dependabot --dry-run` from the reviewed checkout.
The preview permits disabled configuration and prints a JSON report without GitHub mutations or a saved report.
A local lock prevents overlapping scans. Preview mode also uses the lock.
After approval, install a fixed release snapshot and prepare its runtime with `bin/triage-dependabot --prepare-runtime`.
Run that snapshot's `bin/triage-dependabot --dry-run` before changing either launcher.
Then point both the LaunchAgent and local launcher at that version.
Route the installed triage skill to the same snapshot; don't fall back to the historical notification runner.
Set `enabled: true` and enable `com.zkoppert.triage-dependabot` with `launchctl`.
The existing LaunchAgent runs hourly, including weekends, and loading it doesn't start an immediate scan.
Set `enabled: false` to block later scans. Stop the LaunchAgent to interrupt an active scan.
Normal runs save the latest decisions and errors to `~/Library/Logs/dependabot-automerge.json`.
The LaunchAgent writes diagnostic output to `~/Library/Logs/triage-dependabot.log`.
Repository errors produce an unsuccessful exit but don't stop other repositories from processing.
The report and logs contain private repository details; don't publish them.
