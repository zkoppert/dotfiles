# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Repository-wide guidance lives in `.github/copilot-instructions.md`.
- `bin/pr-marker` owns readiness formats and validation; `bin/gh-guard` consumes it, with behavioral regressions in `bin/test_pr_marker.py`.
- `bin/pr-size-guard` owns the personal 800-addition limit; `bin/gh-guard` and `git-hooks/pre-push` consume it.
- `.github/workflows/pr-marker-tests.yml` owns the declared CI test commands and Python runtime floor.
- `setup-copilot2-codespace --refresh-default` deploys merged Copilot guidance to the laptop and selected Codespace from the exact `origin/main` commit.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
