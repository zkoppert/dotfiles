# triage-notifications

Classify GitHub notifications, auto-drop noise, route actionable
items into `~/repos/zkoppert-todo/todo.yml`, archive shipped work to
the `done` section for biannual reflection, and mark notifications
done on GitHub (removing them from the inbox) once handled.

Designed to be safe to re-run: deduped by `notification.thread_id`, so a
second run produces no duplicate todos and no spurious mark-dones.

## What problem this solves

I get a lot of GitHub notifications and miss the important ones. This
tool runs every two hours on weekdays and does an aggressive "bulk
triage": it drops the passive subscription noise (and clears those
GitHub notifications), keeps only the personal-action items in my
existing todo workflow, and leaves Dependabot bumps for a separate
handler. The notification inbox stops being a wall of red.

The fetch uses `?all=true` so the cron also sees notifications I've
viewed on github.com (marked read) but never deleted. Without that,
PRs I'd already clicked on would sit in the inbox forever even after
they merged; the cron would never see them again to clean them up.

## How it classifies

The core policy is **KEEP_REASONS default-drop**: only directed,
personal-action reasons survive. Everything else is passive subscription
noise that drops and is marked done on GitHub.

`KEEP_REASONS = {review_requested, assign, author, mention, security_alert}`

A series of early-exit drops fire before reason routing, in this order:

1. **Title-pattern drop** - repetitive system-generated noise and routine
   config PRs (regex match on `subject.title`). Catches flaky-test report
   titles (`Intermittent test failure: ...`, `Flaky test: ...`, `test
   flake: ...`) and `Enable Dependabot` config PRs. Overridden when
   `reason` is `mention` or `assign` so a direct human ping always
   reaches the inbox. Edit `TITLE_DROP_PATTERNS` in `triage.py` to add
   patterns.
2. **Dependabot version-bump drop** - PRs whose title looks like a
   Dependabot bump (`build(deps): ...`, `chore(deps-dev): ...`, or
   `Bump <pkg> from <x> to <y>`) drop from the inbox but are **never
   marked done on GitHub** (`skip_mark_done`). A separate
   `triage-dependabot` tool consumes those threads, so they must stay
   unread. A direct `mention`/`assign` overrides this so a human ping on
   a bump still reaches me.
3. **Closed-subject drop** - if a KEEP-reason PR or issue is already
   closed/merged when the notification arrives, drop instead of routing
   anywhere. If the subject is a PR I authored, also append an entry to
   todo.yml's `done` section (source `github-notification-auto-archive`)
   so the shipped work is captured for biannual reflection.
4. **Repo-level overrides** (`repo_override`) - per-repo policies, often
   stricter than the global KEEP_REASONS. A safety carve-out runs first:
   a direct `mention`, a direct `assign`, or a `security_alert` always
   survives these gates (except on the fully tuned-out repos), because a
   personal ping or a vulnerability alert is too important to silently
   drop on a title or subscription miss.
   - `github/.github` plus private config entries (`ALWAYS_DROP_REPOS`):
     fully tuned out, dropping **every** notification, including direct
     pings and security alerts.
   - `github/curated-data`: drop everything except the carve-out (direct
     pings and security alerts).
   - `github/markup`: keep security-related titles (security / vuln /
     CVE, routed to INBOX) and the carve-out reasons; drop the rest as
     low priority.
   - Private config entries in `title_aor_required_repos`: keep only
     titles about NUX's area of responsibility plus the carve-out reasons;
     drop anything else.
   - `*/super-linter` (any owner, matched by `^[^/]+/super-linter$`):
     keep only the carve-out reasons. This covers both
     `super-linter/super-linter` and the `github/super-linter` fork (an
     exact-match list missed the fork, so its Dependabot PRs slipped
     through).
   - Private config entries in `subscription_filtered_repos`: keep the
     listed reasons plus the carve-out reasons; drop everything else.

After the drops, surviving KEEP_REASONS notifications route by reason.
Both read and unread notifications classify through the same table; the
`already_tracked` short-circuit in `run()` prevents re-adding a tracked
notification.

The reason table (after the early-exit drops above):

| Reason             | Bucket                                          |
| ------------------ | ----------------------------------------------- |
| `mention`          | Q1 (urgent + important)                         |
| `assign`           | Q1                                              |
| `security_alert`   | Q1                                              |
| `review_requested` | Q1 if from NUX teammate, else INBOX             |
| `author`           | INBOX (my own open PR/issue, status item)       |
| `comment`          | Q1 if the body @-mentions me; else DROP         |
| anything else      | DROP (passive subscription noise)               |

The NUX teammate allowlist is hardcoded in `triage.py` as
`NUX_TEAM_LOGINS_Q1`. Edit there to add or remove people.

## How it integrates with zkoppert-todo

Each new entry carries a `notification` block:

```yaml
- id: notif-github-pull-1234-fix-the-thing
  title: "Fix the thing (org/repo)"
  description: "GitHub notification - reason: mention. @mention → Q1."
  category: process
  source: github-notification
  added: 2026-01-15
  urgency: high
  importance: high
  quadrant: q1_do_first
  status: pending
  notes: ""
  notification:
    thread_id: "1234567"
    url: "https://github.com/org/repo/pull/1234"
    reason: mention
    repo: org/repo
```

When you move the todo to `status: done`, the next triage run will:

1. DELETE `/notifications/threads/{thread_id}` to mark it done on GitHub
   (removes it from the inbox and moves it to the Done tab).
2. Add `marked_done: true` and `marked_done_at: <today>` to the
   `notification` block so it isn't marked-done twice.

## Schedule

A launchd plist runs the tool every two hours on weekdays at 8, 10,
12, 14, 16, and 18 local time. Logs land in
`~/Library/Logs/notification-triage.log`.

To pause: `launchctl unload ~/Library/LaunchAgents/com.zkoppert.notification-triage.plist`
To resume: `launchctl load ~/Library/LaunchAgents/com.zkoppert.notification-triage.plist`

## Pruning stale notifications

After classifying new notifications, the script walks the inbox **and
all four quadrants** (`q1_do_first`, `q2_schedule`, `q3_delegate`,
`q4_eliminate`) and drops anything whose underlying GitHub subject is
now stale:

- closed or merged PRs
- closed issues
- locked discussions
- answered Q&A discussions
- subjects that 404 (deleted)

The classifier also performs the same closed/merged check at intake
time, so a notification that arrives on an already-closed subject (e.g.
a `review_requested` review that landed before the cron ran) is dropped
immediately instead of being routed to a quadrant.

A closed-but-unlocked regular discussion is kept because it can still
receive activity. Only entries with `source: github-notification` are
touched, so manually added items are left alone. Errors other than 404
(timeout, 5xx, parse failure) keep the entry to avoid dropping things
during transient issues. When the pruner drops an entry, it also marks
the underlying GitHub notification thread done (DELETE) so the next
cron cycle doesn't re-fetch the unread thread and re-add it. If the
dropped entry was a self-authored PR (`notification.reason == "author"`
on a PR URL), it is also copied into `done` with
`source: github-notification-auto-archive` before being removed, so
PRs first tracked while open and merged later still land in the
biannual reflection archive instead of vanishing silently. Pruner
stats land in the final summary line as `pruned_stale=N` plus a
`pruned_breakdown:` line when anything was dropped.

## Ad-hoc usage

```bash
# Default run
~/repos/dotfiles/bin/notification-triage

# Preview without writing
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py \
  --dry-run --verbose

# Skip the macOS notification (useful during testing)
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py \
  --no-notify

# Skip the inbox pruner (still classifies new notifications)
python3 ~/repos/dotfiles/.copilot/skills/triage-notifications/triage.py \
  --no-prune
```

## Requirements

Runtime dependencies (all installed via `pip`):

- `PyYAML` - used by tests for fixture setup
- `ruamel.yaml` - used in production to round-trip `todo.yml` while
  preserving the manually maintained section header comments. Plain
  `yaml.safe_dump` would silently strip every `#` comment in the file.

GitHub access:

- `gh` CLI authenticated as you (`gh auth status` should show your login).

## Privacy

`todo.yml` is private, so real notification titles and URLs land in it.
If you ever make that file public, redact entries with
`source: github-notification` first.

## Tests

```bash
cd ~/repos/dotfiles/.copilot/skills/triage-notifications
python3 -m pytest tests.py -v
```

## Failure modes

- **`gh auth` expired**: classifier prints `ERROR: failed to fetch /user`
  and exits with code 1. The launchd job will surface this in
  `~/Library/Logs/notification-triage.log`.
- **`todo.yml` missing**: script exits with code 1. Re-create the file
  (or check that `~/repos/zkoppert-todo` is still cloned).
- **A new GitHub notification reason appears**: under the aggressive
  policy the classifier **drops** any reason that isn't in
  `KEEP_REASONS`. If a new directed-ping reason shows up that I care
  about, add it to `KEEP_REASONS` (and route it in `_classify_internal`)
  in `triage.py`. Run with `--dry-run --verbose` to see how unfamiliar
  reasons are being classified before they're cleared.
- **Pruner dropped something I wanted to keep**: the pruner only drops
  on a hard "stale" signal (closed PR, closed issue, locked discussion,
  answered Q&A discussion, or 404). If a subject reopens after being
  pruned and you still care about it, run triage again and the
  notification will come back through the inbox. To audit what was
  dropped, check the cron log
  (`~/Library/Logs/notification-triage.log`) or run with `--verbose` -
  each drop logs `pruned inbox item <id> (<reason>)`. To disable the
  pruner entirely for a run, pass `--no-prune`. To recover a specific
  entry, the previous version of `todo.yml` lives in
  `~/repos/zkoppert-todo`'s git history.
