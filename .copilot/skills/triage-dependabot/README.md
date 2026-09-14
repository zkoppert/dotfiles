# triage-dependabot

Hourly skill that scans GitHub notifications, filters to Dependabot PRs,
and applies one of four discrete outcomes per PR. It is the companion to
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

For each notification from `gh api /notifications?all=true` whose subject is a
`PullRequest` authored by `dependabot[bot]` or `dependabot-preview[bot]`:

| Condition | Outcome |
| --- | --- |
| PR closed or merged | skip and mark notification done |
| Repo owner is not `github`, `github-community-projects`, or `zkoppert` AND notification reason is `review_requested`, `subscribed`, or `ci_activity` | skip and mark notification done |
| Notification reason is `mention` or `assign`, or a `comment` contains a direct mention | hand off to general notification triage before any terminal action |
| Notification reason is `comment` with complete history and no direct mention | continue through the Dependabot decision tree |
| Repo owner is not `github`, `github-community-projects`, or `zkoppert` AND notification reason is not passive | skip, leave notification in inbox for direct response |
| Title or body references an excluded dependency (e.g. `super-linter/super-linter`) AND notification reason is `review_requested`, `subscribed`, or `ci_activity` | skip and mark notification done |
| Title or body references an excluded dependency AND notification reason is not passive | skip, leave notification in inbox for direct response |
| Draft PR | flag-for-review |
| Any non-bot human (other than me) reviewed or commented | flag-for-review |
| Target version is a prerelease (alpha / beta / rc / dev / preview) | close-prerelease (force-close via `gh pr close --delete-branch`) |
| `mergeStateStatus` is `behind` or `dirty` | rebase (suppressed if my last rebase comment is newer than the latest dependabot push) |
| Bump is major / minor / unknown AND repo coverage threshold below 90 | flag-for-review |
| CI status pending | skip this run; next hour retries |
| CI status failing | flag-for-review |
| Security release (Copilot sub-agent or regex on title/body) AND repo defines a `release` label | label-and-merge |
| Otherwise | merge |

The script never auto-merges when uncertainty exists; sub-agent
timeouts, unknown bump kinds, and missing coverage signals all route to
`flag-for-review`.

### Owned repo allowlist

The script only processes repos whose owner is `github`,
`github-community-projects`, or `zkoppert`. The owner comparison is
case-insensitive. Dependabot PRs from every other owner are skipped and no Q1
todo entry is created. Passive notification reasons (`review_requested`,
`subscribed`, `ci_activity`) are marked done so third-party subscription noise
stays out of the inbox. `mention`, `assign`, and directly addressed comment
notifications are handed to general triage before this policy runs. Verified
non-direct comments continue through the Dependabot decision tree; incomplete
comment history is retained. Other non-passive reasons leave the notification
in place so the user can respond. Repo-specific
skips from the private config still apply inside the owned owners.

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

`SKIPPED_DEPENDENCY_PATTERNS` in `triage_dependabot.py` lists package
coordinates the script must never auto-act on (currently
`super-linter/super-linter`). These PRs always skip the action branches,
but the notification handling depends on the reason: passive reasons
(`review_requested`, `subscribed`, `ci_activity`) auto-clear so the
inbox stops accumulating. `mention`, `assign`, and directly addressed comment
notifications are handed to general triage before the exclusion policy.
Verified non-direct comments may clear as irrelevant, while incomplete comment
history and other reasons leave the notification in place.

### Coverage detection

The script reads `pyproject.toml`, `setup.cfg`, `Makefile`, `tox.ini`,
and `.coveragerc` on the repo's default branch via `gh api` and looks
for `--cov-fail-under=N` or `fail_under = N`. When no signal exists, the
threshold is treated as below 90, which means non-patch bumps in repos
without a configured threshold are flagged for review. This is the
conservative default.

## Outputs

- **Auto-merge**: submit an approving review, then
  `gh pr merge --auto --squash --delete-branch`, and the notification is
  marked done only after the shared local notification ledger records the
  terminal decision. The approval comes first because most target repos require
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
  PRs whose target version is an alpha / beta / rc / dev / preview;
  notification is marked done and the cooldown is applied.
- **Flag-for-review**: a Q1 entry in
  `~/repos/zkoppert-todo/todo.yml` under
  `prioritized.q1_do_first`, with `source: dependabot-triage`,
  `notification.thread_id` for dedup, the PR url, and the reason in
  `description`. Each newly added flag also sends one macOS notification
  through `terminal-notifier`. Selecting the notification opens the PR in
  the default browser. Routine merges, rebases, labels, prerelease closes,
  already tracked flags, and no-op runs stay silent.

## Health snapshot

The shared notification ledger also carries a machine-readable health
snapshot for Dependabot runs. It includes last success/error timestamps,
notification totals, actionables without tracker links, clear failures,
and stale dropped items so the hourly job can be inspected without
opening the database manually.

## Per-PR cooldown

`~/Library/Logs/triage-dependabot-state.json` records the last action
timestamp per PR url. Re-runs within `ACTION_COOLDOWN_SECONDS` (3600)
skip the same PR so an unrefreshed notification stream cannot trigger a
duplicate merge. The shared source ledger lives at
`~/Library/Application Support/notification-workers/ledger.sqlite` and
tracks tracker linkage plus notification-clear retries independently of
that cooldown.

## Integration with zkoppert-todo

Flagged PRs are written using the same notification schema as
`triage-notifications` (`thread_id`, `url`, `reason`, `repo`) plus
`pr_number` and `bump`. Dedup keys off `notification.thread_id` and is
checked against `inbox`, every `prioritized` quadrant, `in_progress`,
`blocked`, `in_review`, and `done`.

Writes to `todo.yml` are race-safe. The tool completes GitHub API work
first, then takes an exclusive `todo.yml.lock`, re-reads the file from
disk, applies only the planned flag and stale-removal deltas, and writes
with an atomic `os.replace`. That keeps manual edits made during a run
instead of replaying a stale in-memory snapshot.

After a successful write, the tool stages `todo.yml` in the todo repo,
skips the commit when there is no staged diff, and otherwise creates a
signed-off local commit with the Copilot co-author trailer. It then tries
`git pull --rebase --autostash` and `git push`. Pull or push failures
are logged as warnings so launchd keeps running, while the local commit
still records the change.

When the user moves a flagged todo to `done`, `status: dropped`, or Q4,
the existing `triage-notifications` reconciliation loop will catch it on its
next run, record the terminal disposition in the shared ledger, and then
DELETE the underlying notification thread.

## Schedule

`com.zkoppert.triage-dependabot.plist` runs hourly on the hour, 24x7.
The `RunAtLoad` key is false so loading the plist does not trigger an
immediate run.

`./install.sh` removes any existing dotfiles-owned notification plist
symlink from `~/Library/LaunchAgents`; it does not start the hourly job.
When you're ready to activate it in a separate attended step, recreate the
symlink and run:

```bash
launchctl load -w "$HOME/Library/LaunchAgents/com.zkoppert.triage-dependabot.plist"
```

To unload:

```bash
launchctl unload -w "$HOME/Library/LaunchAgents/com.zkoppert.triage-dependabot.plist"
```

Logs go to `~/Library/Logs/triage-dependabot.log`.

## Ad-hoc usage

```bash
# Default run (mutating, uses the pinned dotfiles-owned runtime).
~/repos/dotfiles/bin/triage-dependabot

# Preview only.
~/repos/dotfiles/bin/triage-dependabot --dry-run --verbose

# Process a single repo while testing rule changes.
~/repos/dotfiles/bin/triage-dependabot --allowed-repo zkoppert/dotfiles

# Skip the Copilot sub-agent (regex-only security classification).
~/repos/dotfiles/bin/triage-dependabot --no-copilot-subagent
```

## Requirements

- `gh` CLI authenticated with `notifications`, `repo`, and `read:org`
  scopes.
- `copilot` CLI on `PATH` when running with the sub-agent enabled
  (default). The skill falls back to a regex classifier on any sub-agent
  failure, so the `--no-copilot-subagent` flag is for explicit opt-out
  rather than failure recovery.
- The pinned notification-worker runtime provisioned by `./install.sh` at
  `~/.local/share/dotfiles/notification-workers/venv`, using
  `python/notification-worker-requirements.txt` (`PyYAML` and
  `ruamel.yaml`). The wrapper fails fast when that runtime is missing
  dependencies.
- `terminal-notifier` on `PATH` for clickable macOS alerts. Install it
  with `brew install terminal-notifier`.

## Privacy

The script reads only repos accessible to the authenticated `gh` user.
Sub-agent invocations send the PR title and the first 4000 characters of
the body to Copilot CLI; nothing else leaves the local machine. The
state file in `~/Library/Logs` is a flat JSON map of PR url to
timestamp.

## Tests

`tests.py` covers the decision tree branches, semver detection (single
and grouped bumps), coverage parsing, human-activity detection, rebase
suppression, and the cooldown state file. Run with:

```bash
cd ~/repos/dotfiles/.copilot/skills/triage-dependabot
python3 -m pytest tests.py -v
```

## Failure modes and recovery

- `gh auth` expired: every gh call raises and the run exits with status
  1. Re-authenticate and the next hour's run resumes.
- `copilot` missing or unauthenticated: sub-agent classification returns
  None and the regex fallback runs. No data is lost.
- Coverage detection request fails: treated as unknown coverage, which
  routes non-patch bumps to `flag-for-review` until the request
  recovers.
- State file corruption: load returns `{}` so every PR is re-evaluated
  on the next run. The worst case is one duplicate merge attempt which
  `gh` will reject as a no-op once the PR is auto-merging.

## Known limitations

This is tracked for a follow-up PR rather than blocking this one. The
remaining item has a narrow blast radius given the hourly cron with a
10-30 second run window, but it is worth surfacing.

- **Per-PR cooldown state has a read-then-write race across overlapping
  runs.** The launchd schedule runs hourly and a single invocation
  finishes well under a minute, so two runs should not overlap in
  practice. If they ever do, the second run can clobber the first run's
  state updates. Mitigation under consideration: lock the state file for
  the duration of each run.

## Adding new outcomes

The decision tree lives in `decide()` in `triage_dependabot.py`. New
outcomes should be added as `OUTCOME_*` constants, with a matching
executor function and a `stats` counter. Always default the new branch
to `flag-for-review` while the rule is being tuned, then promote once
it proves safe across at least one week of runs.
