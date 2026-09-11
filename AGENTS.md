# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- CI-equivalent test commands and the Python version floor live in `.github/workflows/pr-marker-tests.yml`; run them before delivery.
- `bin/pr-marker` owns private review-waiver validation. See README.md for the lifecycle; never commit real consent records or private target identifiers.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
