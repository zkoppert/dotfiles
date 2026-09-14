# Dotfiles agent guidance

- Read `.github/copilot-instructions.md` for contributor, test-quality, writing, and publication requirements.
- CI commands live in `.github/workflows/`. The notification workers both use a file named `tests.py`: run each complete suite in its own pytest process, as `.github/workflows/notification-worker-tests.yml` does. Bare pytest discovery does not collect these suites, and combining them in one default-import-mode process causes a module-name collision.
- Notification runtime, health, backfill, and attended activation instructions live in `.copilot/skills/triage-notifications/README.md` and `.copilot/skills/triage-dependabot/README.md`. Validate with fixtures, not live workers or the real installer; `test_install.py` provides a fake HOME and launchctl boundary.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
