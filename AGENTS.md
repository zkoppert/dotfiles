# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.
- GitHub notification workers use the pinned runtime provisioned by `install.sh` at `~/.local/share/dotfiles/notification-workers/venv`; requirements live in `python/notification-worker-requirements.txt`, wrappers live in `bin/{notification-triage,triage-dependabot,run-notification-worker}`.
- Shared notification ledger/health plumbing lives in `.copilot/skills/notification_worker_common.py`; GitHub worker health files are `~/Library/Logs/{notification-triage-health,triage-dependabot-health}.json`.
- Relevant regression suites run separately because both test modules are named `tests.py`: `python -m pytest .copilot/skills/triage-notifications/tests.py -q` and `python -m pytest .copilot/skills/triage-dependabot/tests.py -q`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
