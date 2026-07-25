# nux-fr-handoff

Generate a NUX first-responder handoff draft every Friday, save it to a secret
gist, and open the review flow from a clickable macOS notification.

## Workflow

1. Find an open `On-call handoff` issue created this week and assigned to the
   authenticated GitHub user.
2. Give Copilot read-only access to `session_store_sql` and require it to inspect
   every session with activity since Monday.
3. Refresh every linked GitHub issue and pull request through read-only API calls.
4. Validate the Markdown structure and scan for likely credentials or private keys.
   Style, handle, and rendering checkers also run when installed.
5. Create and verify a secret gist.
6. Send a Notification Center alert that opens the gist.

The tool never posts the comment. Review the gist, then post it manually.

## Schedule

`com.zkoppert.nux-fr-handoff.plist` runs at 12:00 local time every Friday.
A same-week wake can catch a missed firing. If the Mac stays asleep into the
next week, issue discovery intentionally finds no current-week handoff and exits.

Logs:

```text
~/Library/Logs/nux-fr-handoff.log
```

State and generated drafts:

```text
~/Library/Application Support/nux-fr-handoff/
```

## Commands

```bash
# Normal run
nux-fr-handoff

# Check issue discovery without generating anything
nux-fr-handoff --dry-run --verbose

# Regenerate the current week's draft
nux-fr-handoff --force

# Validate an existing draft through the gist and notification path
nux-fr-handoff \
  --issue-url https://github.com/acme/on-call/issues/123 \
  --draft-file /path/to/draft.md \
  --force

# Test the clickable notification
nux-fr-handoff --notify-test https://gist.github.com/
```

## Install for another first responder

Clone the public dotfiles repository to a persistent location, then run the
skill-specific installer. It installs only this skill, its command, and its
Friday launchd job.

```bash
brew install terminal-notifier
git clone https://github.com/zkoppert/dotfiles.git ~/repos/zkoppert-dotfiles
~/repos/zkoppert-dotfiles/.copilot/skills/nux-fr-handoff/install.sh
$EDITOR ~/.config/nux-fr-handoff/config.yml
```

Replace the fictitious repository and reference-comment URL in the user config
with your private handoff targets. The committed config intentionally contains
only public placeholders.

Preview the assigned-issue discovery before the first scheduled run:

```bash
nux-fr-handoff --dry-run --verbose
```

The installer also requires GitHub CLI, Copilot CLI, Python 3, and PyYAML. It
prints the exact missing dependency when setup cannot continue.

Secret gists are unlisted, not access-controlled. The runner blocks common
credential and private-key patterns before upload, but the generated draft
should still contain only the operational context needed for the handoff.

## Tests

```bash
cd ~/.copilot/skills/nux-fr-handoff
python3 -m pytest -q tests.py
```
