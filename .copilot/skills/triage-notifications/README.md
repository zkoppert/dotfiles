# triage-notifications

Classify unread GitHub notifications, auto-drop noise, route actionable
items into `~/repos/zkoppert-todo/todo.yml`, and mark notifications done
on GitHub (removing them from the inbox) once the corresponding todo
is done.

Designed to be safe to re-run: deduped by `notification.thread_id`, so a
second run produces no duplicate todos and no spurious mark-dones.

## What problem this solves

I get a lot of GitHub notifications and miss the important ones. This
tool runs every two hours on weekdays, surfaces the actionable items
into my existing todo workflow, and silently clears the noise so the
notification inbox stops being a wall of red.

## How it classifies

| Reason            | Bucket                                           |
| ----------------- | ------------------------------------------------ |
| `mention`         | Q1 (urgent + important)                          |
| `assign`          | Q1                                               |
| `security_alert`  | Q1                                               |
| `review_requested`| Q1 if from NUX teammate, else INBOX              |
| `manual`          | INBOX (deliberately subscribed)                  |
| `comment`         | DROP if thread closed/merged or super-linter without @mention; Q1 if @mention; INBOX otherwise |
| `ci_activity`     | DROP (always)                                    |
| `subscribed`      | DROP if closed/merged; else INBOX                |
| anything else     | INBOX (safe default - never auto-drop the unknown) |

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
cron cycle doesn't re-fetch the unread thread and re-add it. Pruner
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
- **A new GitHub notification reason appears**: classifier defaults to
  INBOX rather than DROP. Check the inbox bucket for unfamiliar items
  and update `Q1_REASONS` / `CLOSED_STATES` in `triage.py` if needed.
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
