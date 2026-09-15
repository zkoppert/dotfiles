# triage-dependabot

Hourly skill that scans GitHub notifications, filters to Dependabot PRs,
and applies one of five action outcomes per PR, or skips it. It is the companion to
the `triage-notifications` skill, focused entirely on the dependency
upgrade firehose so I stop hand-merging routine bumps and stop letting
risky ones rot.

## What problem this solves

Dependabot generates a notification per PR per push. Most of them are
patch bumps with green CI that I rubber-stamp; that work belongs in
automation. A minority are major version jumps, security advisories with
behavior changes, or builds where my coverage is too low to trust the
green CI. Those still need eyes, but they get buried in the rubber-stamp
queue and slip until they break something. This skill separates the two
piles automatically.

## Decision tree

The worker uses the same [read-state-resilient intake](../triage-notifications/README.md#what-problem-this-solves)
as general triage and filters to Dependabot-authored PRs. Its decision policy
stays separate. Direct mentions, assignments, and directly addressed comments
are left for [general triage's direct-ask routing](../triage-notifications/README.md#how-it-classifies),
not handled by a PR mutation or terminal cleanup here. Verified non-direct
comments can continue through the policy; incomplete history stays accountable
for retry.

After cooldown and ownership checks, the worker follows these routes:

| Condition | Outcome |
| --- | --- |
| Repo archived | skip with terminal notification cleanup |
| Repo owner outside the allowlist | apply the [owned-repo policy](#owned-repo-allowlist) without acting on the PR |
| Repo or dependency excluded | apply the [exclusion policy](#excluded-dependencies) without acting on the PR |
| PR closed or merged | skip with terminal notification cleanup |
| Draft PR | flag-for-review |
| Any non-bot human (other than me) reviewed or commented | flag-for-review |
| Target version is a prerelease (alpha / beta / rc / dev / preview) | close-prerelease |
| `mergeStateStatus` is `behind` or `dirty` | rebase unless a prior request is still waiting on Dependabot |
| Bump kind unknown | flag-for-review regardless of coverage |
| Bump is major / minor AND coverage is unknown or below 90 | flag-for-review |
| CI status pending | skip this run; a later run retries |
| CI status failing | flag-for-review |
| Security release (Copilot sub-agent or regex on title/body) | label-and-merge, adding `release` only if the repo defines it |
| Otherwise | merge |

Before each approval, merge, label, rebase, or close, the guard checks ledger
and tracker ownership, inspects relevant comment history, then makes a final
single-thread read of the notification reason, update timestamp, subject, and
latest-comment URL. New direct asks veto the mutation. A tracker reload failure
also blocks the action. Notifications without comment evidence do not trigger
unrelated comment-collection requests; incomplete relevant history is retained
rather than assumed non-direct.

A security-classification sub-agent failure falls back to the regex, not
a blanket flag-for-review. Unknown bump kinds and insufficient coverage for
non-patch updates follow the conservative branches above.

### Owned repo allowlist

The script only acts on PRs whose repo owner is `github`,
`github-community-projects`, or `zkoppert`, compared case-insensitively.
PRs from other owners do not create a Dependabot Q1 flag. After the direct-ask
handoff above, passive reasons (`review_requested`, `subscribed`, `ci_activity`)
and verified non-direct comments may clear as irrelevant, subject to tracker
ownership and current-notification checks. Other reasons leave the notification
in place. Repo-specific skips from the private config still apply inside the
owned owners.

### Prerelease detection

Catches PRs like
[github-community-projects/stale-repos#520](https://github.com/github-community-projects/stale-repos/pull/520)
(`bump python from 3.14.5-slim to 3.15.0b2-slim`) that target an
unstable release. Recognized prerelease forms:

- PEP 440 short forms glued to the patch digit: `1.0.0a1`, `1.0.0b2`, `1.0.0rc1`, `3.15.0b2`
- Word forms with `-` or `.` separator: `1.0.0-alpha`, `1.0.0-beta.1`, `1.0.0-rc1`, `1.0.0.dev1`, `1.0.0-preview`

Docker build variants (`-slim`, `-alpine`, `-bookworm`) and PEP 440
post-releases (`1.0.0.post1`) are NOT treated as prereleases. The action
calls `gh pr close --delete-branch` to force the PR shut via the API.
The script used to also post `@dependabot close` first, but Dependabot
has historically ignored that comment for hours (see
`github-community-projects/contributors#496`, where the hourly cron
posted the directive 12+ times before the PR actually closed) and once
the direct close lands the comment is pure noise on the PR timeline, so
the comment was dropped. Only a narrow "already closed / not found"
race on the close call is swallowed; any other failure (auth, rate
limit, timeout, branch deletion) propagates so the outer run loop
records it and the next cron tick retries instead of silently treating
the PR as handled. Dependabot may open a new PR if the upstream
releases another prerelease, and the next run closes that one too. For
a permanent skip, add an `ignore` rule in the repo's
`.github/dependabot.yml`.

### Excluded dependencies

`SKIPPED_DEPENDENCY_PATTERNS` in [triage_dependabot.py](triage_dependabot.py)
owns the excluded package coordinates. Those matches and private
`dependabot_skipped_repos` entries never reach PR action branches. Open PRs
use the same passive-reason and verified-comment clearance policy as
[unowned repos](#owned-repo-allowlist); closed or merged PRs can receive terminal
cleanup. Direct asks and incomplete history retain the protections above.

### Coverage detection

`detect_repo_coverage()` in [triage_dependabot.py](triage_dependabot.py)
owns the supported Python and Ruby SimpleCov configuration sources and
threshold parsing. It reads the repo's default branch via `gh api`.
A missing or unreadable coverage signal is unknown, not evidence of safety;
non-patch updates then follow the flag-for-review branch.

## Outputs

- **Auto-merge**: submit an approving review, then
  `gh pr merge --auto --squash --delete-branch`. Enabling auto-merge is not
  treated as completion: the notification stays open until a later run sees
  the PR closed or merged. A successful synchronous merge can clear immediately
  after the ledger records completion and clearance is revalidated.
  The approval comes first because most target repos require
  an approving code-owner review; enabling auto-merge alone would leave
  the PR stuck until a human approved. When the repo doesn't allow
  auto-merge at the repo level, the merge falls back to a synchronous
  merge. The approval is consistent: it's skipped when the current login
  already approved the PR's head SHA.
- **Rebase**: `gh pr comment --body "@dependabot rebase"`; the
  notification stays open so the next push triggers another evaluation.
- **Label-and-merge**: `gh pr edit --add-label release` (only if the
  repo defines a `release` label) followed by the same approve-then-merge
  flow.
- **Close-prerelease**: `gh pr close --delete-branch` (force-close via
  the API, so we do not depend on Dependabot acting on a comment) for
  PRs whose target version is an alpha / beta / rc / dev / preview.
  After a successful close, the worker records irrelevance, applies the
  cooldown, and attempts notification clearance with durable retry state.
- **Flag-for-review**: a Q1 entry in
  `~/repos/zkoppert-todo/todo.yml` under
  `prioritized.q1_do_first`. Ordinary flags keep their GitHub notification
  open for human disposition. A branch-protection handoff can clear after
  the Q1 item is durably linked in the ledger as `tracked_elsewhere`, without
  removing the flag. Each newly added flag also sends one macOS notification
  through `terminal-notifier`. Selecting the notification opens the PR in
  the default browser. Routine merges, rebases, labels, prerelease closes,
  already tracked flags, and no-op runs stay silent.

## Health snapshot

See the shared [health and backfill guide](../triage-notifications/README.md#health-and-backfill-preview)
for the read-only query and the distinction between worker intake counts and
ledger-wide metrics. Dependabot records its snapshot under `triage-dependabot`.
Normal dry runs do not modify the existing ledger, health snapshot, tracker,
or cooldown state.

## Per-PR cooldown

`~/Library/Logs/triage-dependabot-state.json` records the last action
timestamp per PR URL. The action cooldown is one hour; branch-protection
handoffs back off for a day. This limits repeated PR actions while GitHub's
notification stream catches up. Durable clear and tracker-cleanup retries
are separate from that cooldown; see [tracker integration](#integration-with-zkoppert-todo).

## Integration with zkoppert-todo

`build_flag_entry()` in [triage_dependabot.py](triage_dependabot.py) owns
the generated flag fields. Dedup checks the item ID and notification thread
across INBOX, every quadrant, `in_progress`, `blocked`, `in_review`, and `done`.
Ownership and cleanup also compare normalized GitHub artifact URLs so an older
thread can still protect or identify the same PR.

Writes to `todo.yml` use an exclusive `todo.yml.lock`, a fresh read, and
an atomic `os.replace`. Before clearing a terminal notification, the tool
records any matching stale entries and their sections in the shared ledger,
matching thread identity first and normalized GitHub URLs second. It retries
that cleanup even when the notification no longer appears in the inbox.
Only unchanged tracker snapshots are removed. Edits or moves that keep work
active reopen ledger ownership, so another Dependabot run cannot resume the
old cleanup. Renewed direct asks retain the tracker entry and return to intake
without waiting for the old action cooldown, even if the earlier clear
succeeded. General triage does not mistake an unchanged pending cleanup for
a deliberate reopen.

After a successful write, the tool stages `todo.yml` in the todo repo,
skips the commit when there is no staged diff, and otherwise creates a
signed-off local commit with the Copilot co-author trailer. It then tries
`git pull --rebase --autostash` and `git push`. Pull or push failures
are logged as warnings so launchd keeps running, while the local commit
still records the change.

Human completion or elimination of a flag uses general triage's
[terminal-disposition reconciliation](../triage-notifications/README.md#how-it-integrates-with-zkoppert-todo),
including already-absent threads and renewed asks.

## Schedule

Use the shared [schedule and attended-activation instructions](../triage-notifications/README.md#schedule)
for installer safeguards, plist paths, timestamped logs, and enable/disable
commands. Select `triage-dependabot` as the worker in those examples.

## Ad-hoc usage

```bash
# Default run (mutating, uses the pinned dotfiles-owned runtime).
~/repos/dotfiles/bin/triage-dependabot

# Preview only.
~/repos/dotfiles/bin/triage-dependabot --dry-run --verbose

# Restrict new intake to one owned repo; repeat --allowed-repo for more.
~/repos/dotfiles/bin/triage-dependabot --allowed-repo zkoppert/dotfiles

# Skip the Copilot sub-agent (regex-only security classification).
~/repos/dotfiles/bin/triage-dependabot --no-copilot-subagent
```

`--allowed-repo` filters incoming notifications, not recovery of existing
ledger cleanup intents. Use `--dry-run` when you need a non-mutating preview.

## Requirements

- `gh` CLI authenticated with `notifications`, `repo`, and `read:org`
  scopes.
- `copilot` CLI on `PATH` when running with the sub-agent enabled
  (default). The skill falls back to a regex classifier on any sub-agent
  failure, so the `--no-copilot-subagent` flag is for explicit opt-out
  rather than failure recovery.
- The shared [notification runtime and alert prerequisites](../triage-notifications/README.md#requirements).

## Privacy

The script reads only repos accessible to the authenticated `gh` user.
Security-classification prompts send the PR title and the first 4000
characters of the body to Copilot CLI. Normal GitHub operations and tracker
syncing also use the network. The cooldown state contains PR URLs and
timestamps. Follow the shared [ledger and tracker privacy guidance](../triage-notifications/README.md#privacy)
when inspecting or sharing local state.

## Tests

Use the shared [fixture-validation instructions](../triage-notifications/README.md#tests)
and run this worker's complete `tests.py` suite in its own pytest process.

## Failure modes and recovery

- `gh auth` expired: every gh call raises and the run exits with status
  1. Re-authenticate and the next hour's run resumes.
- `copilot` missing or unauthenticated: sub-agent classification returns
  None and the regex fallback runs. No data is lost.
- Coverage detection request fails: treated as unknown coverage, which
  routes non-patch bumps to `flag-for-review` until the request
  recovers.
- Malformed cooldown JSON: load falls back to an empty map, resetting
  action throttling. This does not reset ledger lifecycle decisions or
  tracker ownership; the normal guards and cleanup recovery still apply.

## Known limitations

**Per-PR cooldown state has a read-then-write race across overlapping runs.**
The state file has no run-wide lock, so a second invocation can overwrite
the first run's timestamp updates. Avoid overlapping manual and scheduled
runs; the schedule is not a guarantee about worker duration or manual invocation.
A run-wide cooldown lock remains a possible follow-up.

## Adding new outcomes

The decision tree lives in `decide()` in `triage_dependabot.py`. New
outcomes should be added as `OUTCOME_*` constants, with a matching
executor function and a `stats` counter. Always default the new branch
to `flag-for-review` while the rule is being tuned, then promote once
it proves safe across at least one week of runs.
