# triage-notifications

Classify GitHub notifications, auto-drop noise, route actionable
items into `~/repos/zkoppert-todo/todo.yml`, archive shipped work to
the `done` section for biannual reflection, and mark notifications
done on GitHub (removing them from the inbox) once handled.

Re-runs reconcile existing work rather than starting a second queue. See
[tracker integration](#how-it-integrates-with-zkoppert-todo) for identity matching
and notification lifecycle handling.

## What problem this solves

I get a lot of GitHub notifications and miss the important ones. Each run
separates personal work from passive subscription noise, while
[triage-dependabot](../triage-dependabot/README.md) handles dependency updates.
The notification inbox stops being a wall of red without replacing my todo workflow.

The fetch uses `?all=true` so the cron also sees notifications I've
viewed on github.com (marked read) but never deleted. Without that,
PRs I'd already clicked on would sit in the inbox forever even after
they merged; the cron would never see them again to clean them up.

Each newly added direct mention sends one macOS notification through
`terminal-notifier`. Selecting the notification opens the PR, issue, or
discussion in the default browser. Alerts follow the classifier's direct-mention
result, including a mention found in comment history when GitHub reports a
different reason. Assignments, reviews, security alerts, and author updates
without such a mention stay silent, as do already tracked items, dropped
noise, and no-op runs.

## How it classifies

The classifier protects personal requests before applying noise filters:

1. **Direct asks**: `mention`, `assign`, and exact, case-insensitive mentions
   of the authenticated login in relevant comment history route to Q1. A
   closed PR or issue does not by itself resolve a direct ask.
2. **Security alerts**: `security_alert` routes to Q1.
3. **Review requests**: ordinary `review_requested` notifications route to
   Q2, regardless of the author's team. Closed or merged subjects drop instead.
   Recognized Dependabot-authored bump reviews are handed to the companion
   worker; an unverified author falls back to scheduled review work.

Comment inspection includes issue comments, PR review comments, and reviews
when the notification carries comment evidence. The ledger keeps a separate
cursor for each stream so a later bot reply does not hide an earlier direct
mention. Incomplete history is retained for retry and reported as an error;
a direct ask already established by the reason or available history still
reaches Q1. A comment with no usable history can remain in INBOX for triage.

The remaining notifications follow the noise policy in `classify()` and
`repo_override()` in [triage.py](triage.py):

- **Dependabot handoff**: bump-title patterns identify candidates, and an
  author lookup confirms bot ownership. Handoffs remain in GitHub's inbox
  without changing read state; an unavailable lookup also retains the thread.
  Private `watch_only_dependabot_mark_done_repos` entries instead clear
  passive bump notifications. Protected direct asks and security alerts still
  reach Q1, and watch-only review requests remain scheduled work.
- **Title-pattern drops**: repetitive flaky-test reports and routine
  `Enable Dependabot` config PRs are noise. `TITLE_DROP_PATTERNS` owns the
  matching patterns; these do not override the protected routes above.
- **Closed author updates**: closed or merged self-authored PRs can be
  archived to `done` with source `github-notification-auto-archive`, retaining
  shipped work for biannual reflection. Closed issues are not archived as PR work.
- **Repository filters**: the public defaults and private
  `~/.copilot/private/triage-repos.yml` settings govern the remaining reasons.
  Tuned-out and subscription-filtered repos drop noise; area-of-responsibility
  filters require matching titles; security-title exceptions can retain an
  INBOX item. The owner-agnostic super-linter filter also covers forks.
  None of these filters overrides a protected request.
- Surviving `author` status items enter INBOX. Other unprotected reasons,
  including non-direct comments, default to DROP unless a repository exception
  keeps them.

An ordinary review escalates to Q1 after one business day while it remains
in INBOX or Q2 with an empty, `pending`, or `not_started` status. The deadline
uses the original `notification.captured_at` (initially GitHub's `updated_at`,
or intake time if unavailable); legacy entries can fall back to `added`.
Business days skip weekends, not holidays, and preserve the UTC time of day.
Editing notes alone does not count as starting work. Items moved to
`in_progress`, `blocked`, or `in_review`, or given a started status, are not
moved by age escalation.

## How it integrates with zkoppert-todo

`todo.yml` remains the human work queue. The shared local ledger records
source identity, canonical artifact, classification, tracker linkage,
terminal decisions, comment cursors, and clear/retry state before a GitHub
DELETE. `NotificationLedger` in [notification_worker_common.py](../notification_worker_common.py)
owns the persisted schema and its writable upgrades; see
[health inspection](#health-and-backfill-preview) for the database location.

The general worker matches `notification.thread_id` first, then canonical
PR/issue URLs in tracker `link` or `artifacts` references. Matching spans
INBOX, all quadrants, `in_progress`, `blocked`, `in_review`, and `done`.
Q1/Q2 requests can attach notification metadata to a matching item without
a thread ID, preserving its title, notes, and started-work section instead
of adding another todo. URL-deduped INBOX candidates do not add another item;
clearing their GitHub threads still requires a current clearability check.

`build_todo_entry()` and the archive builders in [triage.py](triage.py) own
the generated tracker fields. Use the worker rather than copying an older
notification block by hand.

Writes take an exclusive `todo.yml.lock`, re-read the file, apply planned
deltas, and use an atomic `os.replace`. Round-trip YAML preserves comments,
key order, and quoting. Terminal-clear checks and git operations also use
the shared lock so cooperating writers cannot invalidate tracker ownership
between its check and a clear.

After a successful write, the tool stages `todo.yml` in the todo repo,
skips the commit when there is no staged diff, and otherwise creates a
signed-off local commit with the Copilot co-author trailer. It then tries
`git pull --rebase --autostash` and `git push`. Pull or push failures
are logged as warnings so launchd keeps running, while the local commit
still records the change.

When you move the todo to `status: done`, or move a GitHub-backed item to
`prioritized.q4_eliminate` / `status: dropped`, the next triage run will:

1. Record the terminal disposition in the local ledger (`completed` or
   `irrelevant`) before any GitHub mutation.
2. Re-read tracker ownership under its lock, check the current notification,
   and DELETE `/notifications/threads/{thread_id}` only if it is still clearable.
   A confirmed HTTP 404 settles an already-absent thread without DELETE;
   timeouts, API errors, and malformed responses never count as absence.
3. Add `marked_done: true`, `marked_done_at: <today>`, and the terminal
   disposition to the `notification` block. If the remote clear succeeded but
   the tracker write failed, the next run repairs this metadata without a
   second DELETE.

An unchanged direct mention or assignment can clear after explicit Q4/dropped
or completed disposition, including an unchanged event at the recorded
boundary. A renewed reason, timestamp, or comment requires revalidation rather
than reusing the earlier clear decision. Failed clears stay in the ledger for
retry. Each run lists the inbox once; clearance refreshes use the single-thread
endpoint rather than repeatedly paginating the whole inbox.

## Schedule

Both the [general triage plist](../../../LaunchAgents/com.zkoppert.notification-triage.plist)
and the [Dependabot plist](../../../LaunchAgents/com.zkoppert.triage-dependabot.plist)
define hourly runs on the hour, 24x7, with no immediate run at load.
Their `--log-output` argument timestamps summaries, prune breakdowns, and
errors in `~/Library/Logs/<worker>.log`. Ad-hoc runs keep the plain stdout
summary; runtime preflight errors are always timestamped.

On macOS, `./install.sh` requires both notification services and their
LaunchAgents filesystem entries (including dangling symlinks) to be absent
before provisioning the runtime or changing worker links. From
`~/repos/dotfiles`, it unloads owned plist symlinks, verifies service absence,
then removes those links. It tries `bootout` when `unload` leaves an owned
service registered. A loaded label without a plist is stopped only when its
program points to the expected checkout's worker. Foreign entries and services
are preserved; nonstandard checkouts do not stop services or remove agent entries.
If either path remains or service absence cannot be verified, installation
stops with a nonzero exit. The installer never runs these workers or loads
their schedules.

Activation is a separate attended step, not part of fixture validation.
First provision the [runtime](#requirements) and inspect the
[backfill preview](#health-and-backfill-preview). The checked-in plists use
absolute paths for Zack's macOS account; inspect them before loading on another
machine. For the configured account, choose one worker and recreate its missing
symlink before loading it:

```bash
worker=notification-triage # Choose triage-dependabot to activate that worker instead.
agent="$HOME/Library/LaunchAgents/com.zkoppert.$worker.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" &&
  ln -s "$HOME/repos/dotfiles/LaunchAgents/com.zkoppert.$worker.plist" "$agent" &&
  launchctl load -w "$agent"
```

Do not force-replace an existing agent path; inspect its ownership first.
To disable the selected worker, including at future logins:

```bash
worker=notification-triage # Choose the worker to disable.
launchctl unload -w "$HOME/Library/LaunchAgents/com.zkoppert.$worker.plist"
```

## Health and backfill preview

The workers write separate machine-readable health snapshots into the shared
local ledger at `~/Library/Application Support/notification-workers/ledger.sqlite`.
Each snapshot records the worker's last success/error timestamps and retains
the previous error when a later run succeeds. Its `current_notification_count`
and `current_unread_count` describe that run's intake, not a post-clear GitHub
inventory. `notification_counts` and the actionable-without-tracker,
clear-failure, and stale-dropped lists summarize the shared ledger across
both workers, including retained historical rows. Stale-dropped items are
irrelevant rows not yet cleared, not an age threshold.

`record_worker_health_snapshot()` in [notification_worker_common.py](../notification_worker_common.py)
owns the JSON fields. Runtime preflight failures happen before that function
can run, so check the timestamped log as well as the stored health timestamp.
Read the stored JSON without modifying the database:

```bash
sqlite3 -readonly "$HOME/Library/Application Support/notification-workers/ledger.sqlite" \
  "SELECT json_object('worker', worker, 'updated_at', updated_at, 'health', json(snapshot_json)) FROM notification_health;"
```

To preview a ledger backfill without mutating GitHub or `todo.yml`, run:

```bash
~/repos/dotfiles/bin/notification-triage --dry-run --backfill \
  --backfill-ledger /tmp/notification-backfill.sqlite
```

The destination must be fresh and cannot be the production ledger or a symlink.
The preview reconciles by thread ID first, then canonical GitHub URL, and
records the projected tracker links and terminal decisions in that new file.
It does not modify the current tracker or existing ledger, clear notifications,
or activate schedules. Normal `--dry-run` does not write health or ledger state.
A backfill preview is not applied to production automatically. Ordinary mutating
worker runs create or upgrade their ledger schema when needed; the installer
does not perform that migration.

## Pruning stale notifications

After classifying new notifications, the script walks INBOX and all four
quadrants for stale subjects. Direct asks are not auto-pruned just because
the subject closes; they follow the explicit terminal-disposition lifecycle
above. Other `source: github-notification` entries are candidates when their
subjects are:

- closed or merged PRs
- closed issues
- locked discussions
- answered Q&A discussions
- subjects that 404 (deleted)

Ordinary review and author-status notifications also receive a closed/merged
check at intake; this does not override the protected direct-ask route.

A closed-but-unlocked regular discussion is kept because it can still
receive activity. Manually added entries are left alone. Errors other than
404 (timeout, 5xx, parse failure) keep the entry to avoid dropping work during
transient issues. Before pruning, the worker rechecks the tracker and current
notification and durably records the terminal decision. A clear failure stays
in the ledger for retry. If the dropped entry was a self-authored PR (`notification.reason == "author"`
on a PR URL), it is also copied into `done` with
`source: github-notification-auto-archive` before being removed, so
PRs first tracked while open and merged later still land in the
biannual reflection archive instead of vanishing silently. Pruner
stats land in the final summary line as `pruned_stale=N` plus a
`pruned_breakdown:` line when anything was dropped. The summary uses the
deltas that actually applied to the fresh `todo.yml` load under the
lock. In `--dry-run`, the tool applies the same deltas to a fresh
in-memory load and reports that preview without writing.

## Ad-hoc usage

```bash
# Default run (uses the pinned dotfiles-owned runtime)
~/repos/dotfiles/bin/notification-triage

# Preview without writing to todo.yml, the ledger, or GitHub
~/repos/dotfiles/bin/notification-triage --dry-run --verbose

# Skip the macOS notification (useful during testing)
~/repos/dotfiles/bin/notification-triage --no-notify

# Skip the inbox pruner (still classifies new notifications)
~/repos/dotfiles/bin/notification-triage --no-prune
```

## Requirements

Both workers use a dotfiles-owned virtualenv at
`~/.local/share/dotfiles/notification-workers/venv`. `./install.sh` needs an
available Python 3.11+ interpreter to create it, installs the dependency
versions in [notification-worker-requirements.txt](../../../python/notification-worker-requirements.txt),
and verifies that `PyYAML` and `ruamel.yaml` import successfully. The Python
interpreter comes from the local bootstrap selection, not an exact version pin.
A healthy runtime is reused when the requirements stamp matches.

The wrappers resolve their checkout through symlinks and use only this
virtualenv, not whichever Python happens to be on `PATH`. They fail before
starting a worker if its Python executable is missing or the YAML import
preflight fails. Runtime provisioning failures warn without stopping the rest
of dotfiles setup, but do not activate a notification schedule. Notification
wrapper symlinks in `~/.local/bin` are installed only on macOS from
`~/repos/dotfiles`; elsewhere, invoke the checkout's `bin/` wrappers directly.

GitHub access:

- `gh` CLI authenticated as you (`gh auth status` should show your login).
- `terminal-notifier` on `PATH` for clickable macOS alerts. Install it
  with `brew install terminal-notifier`.

## Privacy

Both `todo.yml` and the local ledger contain notification titles, repository
names, and URLs. The default ledger directory uses mode `0700`; the database
and its SQLite sidecars use mode `0600`. Health snapshots contain the same
private metadata. Do not publish the ledger or raw health output. If you make
`todo.yml` public, redact notification-backed entries first.

## Tests

Use fixtures, not live workers or the real installer. The
[notification worker CI workflow](../../../.github/workflows/notification-worker-tests.yml)
owns the locked test dependencies and explicit full-suite commands. Each
worker's `tests.py` must run in its own pytest process; bare discovery does
not collect these suites. The installer boundary is exercised by
[test_install.py](../../../test_install.py) with a fake HOME and launchctl.

## Failure modes

- **Pinned runtime missing modules**: the wrapper exits before loading
  `triage.py` and prints the preflight failure.
- **`gh auth` expired**: classifier prints `ERROR: failed to fetch /user`
  and exits with code 1. The launchd job will surface this in
  `~/Library/Logs/notification-triage.log`.
- **`todo.yml` missing**: script exits with code 1. Re-create the file
  (or check that `~/repos/zkoppert-todo` is still cloned).
- **A new GitHub notification reason appears**: use `--dry-run --verbose`
  to inspect its result under the [classification policy](#how-it-classifies)
  before allowing clears. Add an explicit route in `classify()` with fixture
  coverage if the new reason should receive personal attention.
- **Pruner dropped something I wanted to keep**: the pruner only drops
  on a hard "stale" signal (closed PR, closed issue, locked discussion,
  answered Q&A discussion, or 404), subject to the direct-ask protection
  above. If GitHub sends renewed activity after a subject reopens, the next
  triage run can route that notification again. To audit what was
  dropped, check the cron log
  (`~/Library/Logs/notification-triage.log`) or run with `--verbose`;
  each drop logs `planned prune of <section> item <id> (<reason>)`. To disable the
  pruner entirely for a run, pass `--no-prune`. To recover a specific
  entry, the previous version of `todo.yml` lives in
  `~/repos/zkoppert-todo`'s git history.
