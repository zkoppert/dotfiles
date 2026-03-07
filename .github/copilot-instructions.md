# Zack Koppert — Global Copilot Instructions

These preferences apply across all repositories and sessions.

## General Preferences
- Be direct and concise — skip preamble and get to the point
- When uncertain, ask clarifying questions rather than assuming
- Prefer practical, working solutions over theoretical explanations
- When automating repetitive work, build reusable tools (scripts, actions) not one-off fixes
- Use parallel execution when possible to save time (e.g., multiple API calls, concurrent agents)

## Pull Requests
- **Always create PRs as draft** unless I explicitly say otherwise
- Always check a PR's status (open/merged/closed) before pushing commits to it
- PR descriptions should be kept up to date with the actual changes — verify before finalizing
- When reviewing PRs, focus on critical issues (bugs, security, logic errors) not style nitpicks

## Code Style & Languages
- **Python** is the preferred scripting language for automation, data processing, and tooling
- Use `argparse` for CLI argument parsing in Python scripts
- Include proper error handling and logging — don't silently swallow errors
- Only add comments where code needs clarification; don't over-comment obvious logic
- Prefer ecosystem tools (`pip install`, `npm init`, etc.) over manual configuration
- Run `make lint` and `make test` before committing in repos that have a Makefile
- Write unit tests for new functionality
- Document changes to environment variables in the `README.md` file

## GitHub CLI & API
- Prefer the **`gh` CLI** over raw API calls or curl when interacting with GitHub
- Use `--json` flag with `gh` for structured output that can be parsed programmatically
- When searching across an org, use `gh search prs`, `gh search issues`, etc. with `--owner` filter
- Always disable pagers: `git --no-pager`, `gh --no-pager`, or pipe to `cat`

## GitHub Actions Best Practices
When creating or modifying GitHub Actions workflows:
1. Always use the **latest release** of each action
2. Pin actions to their **full commit SHA** (not tags) with a comment showing the human-readable version: `uses: actions/checkout@<sha> # v6.0.2`
3. Validate workflow syntax before committing
4. Use **dedicated tokens/secrets** for each workflow — do not reuse tokens across different workflows
5. When filtering activity data for reports, exclude Dependabot PRs from summaries (they add noise)

## Git & Version Control
- Commit messages should be clear and descriptive with a summary line and body when needed
- Always include the Co-authored-by trailer for Copilot: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- Use `--ff-only` for pulls to avoid unexpected merge commits
- Don't commit secrets, credentials, or tokens into source code

## Excel & Report Generation
- Use **openpyxl** for Excel file creation in Python
- Excel can only support one hyperlink per cell — use a separate References sheet for multiple links
- For display in cells, use clean text (e.g., `github#418801`) without raw URLs
- Match existing format/templates when extending reports — don't invent new layouts without asking

## Writing Style (for reports, evaluations, documentation authored on my behalf)
- Use a conversational, direct tone — not corporate or stiff
- Vary greetings and openings — don't default to "Hey team" every time
- Use real names, not handles, when referring to people in narrative text
- Back up qualitative assessments with specific evidence (links to PRs, issues, etc.)
- Use `[PLACEHOLDER]` tags for subjective items that only I can fill in
- Rating language: "Above expectations" / "Meets expectations" / "Below expectations" — not "Exceeds" or "Does not meet"
- When referencing GitHub artifacts, always include a clickable link

## File & Project Organization
- Store automation scripts in a `scripts/` directory
- Use YAML config files for parameterized values (team rosters, schedules, etc.) rather than hardcoding
- Keep generated output separate from source (e.g., `january-2026/`, `february-2026/` directories)
- Add a `.gitignore` for common artifacts (`__pycache__/`, `*.pyc`, `.DS_Store`)

## macOS Environment
- I run macOS — use macOS-compatible commands (e.g., `open` not `xdg-open`, `pbcopy` for clipboard)
- For scheduled tasks, prefer **launchd** plist over crontab
- My repos live in `~/repos/`

---

## My Open Source GitHub Actions — Shared Patterns

The following conventions apply to my suite of GitHub Actions in the `github-community-projects` org: **contributors**, **evergreen**, **issue-metrics**, **stale-repos**, **cleanowners**, and **measure-innersource**. These repos share a consistent architecture — follow these patterns when working in any of them.

### Project Structure
- **Flat module layout** — all Python source files live at the repo root (no `src/` directory)
- Common module split: `{main}.py`, `auth.py`, `env.py`, `markdown_writer.py` (or `markdown.py`)
- Test files: `test_*.py` at root, one per module
- Linter configs live in `.github/linters/` (not repo root)
- Action definition: `action.yml` (Docker-based action)

### Python Conventions
- **Python 3.11+** with modern type hints (`str | None`, not `Optional[str]`)
- **Max line length: 150 characters** (configured in `.flake8`)
- Environment variables managed through a centralized `env.py` module with `get_env_vars()` function
- Helper functions: `get_bool_env_var()`, `get_int_env_var()` for typed env var access
- **python-dotenv** for `.env` file support
- **github3-py** (v4.0.1) as the primary GitHub API client library
- Dual auth pattern: GitHub App credentials take priority over PAT (`auth.py`)
- GitHub Enterprise support via `GH_ENTERPRISE_URL` env var
- Docstrings with Args/Returns/Raises format

### Linting & Formatting Stack
All repos use the same 5-tool linting chain run via `make lint`:
1. **flake8** — strict errors (E9, F63, F7, F82), then warnings with exit-zero; config at `.github/linters/.flake8`
2. **isort** — import sorting; config at `.github/linters/.isort.cfg`
3. **pylint** — minimum score 9.0; config at `.github/linters/.python-lint`
4. **mypy** — type checking; config at `.github/linters/.mypy.ini`
5. **black** — code formatting (no custom config)

### Testing
- **pytest** with **pytest-cov** — minimum 80% code coverage (`--cov-fail-under=80`)
- `.coveragerc` omits test files from coverage
- Heavy use of `unittest.mock` (`MagicMock`, `@patch`) for GitHub API mocking
- CI tests against Python 3.11 and 3.12 matrix (some repos also 3.13)
- Run via `make test`

### Makefile Targets
Every repo has the same three core targets:
```
make lint    # Run all 5 linters in sequence
make test    # pytest with coverage
make clean   # Remove __pycache__ and .pyc files
```

### Dockerfile Patterns
- Base image: `python:3.14-slim` pinned by SHA digest
- `pip install --no-cache-dir --no-deps` for reproducible builds
- `apt-get install git` with cache cleanup
- `HEALTHCHECK` for container scanner compliance
- `ENTRYPOINT ["python3", "-u"]` with `CMD ["/action/workspace/{main}.py"]`
- Checkov/Trivy skip annotations documented inline
- OCI-compliant labels (maintainer, description, documentation URL)

### CI/CD Workflow Patterns
Standard workflow set across all repos:
- **python-ci.yml** — lint + test on push/PR to main (Python version matrix)
- **docker-ci.yml** — build Docker image (linux/amd64)
- **super-linter.yaml** — GitHub Super-Linter for markdown, YAML, shell
- **release.yml** — automated semantic versioning releases to GHCR
- **scorecard.yml** — OpenSSF security scorecard
- **stale.yaml** — auto-close stale issues/PRs
- **auto-labeler.yml** — PR label automation
- All actions pinned to SHA with version comments
- Default permissions: read-only (`persist-credentials: false`)

### Dependencies (shared across repos)
**Production**: `github3-py`, `python-dotenv`, `requests`, `cryptography` (for GitHub App JWT)
**Test**: `pytest`, `pytest-cov`, `black`, `flake8`, `pylint`, `mypy`, `isort`, `types-requests`

### Key Design Principles
- **Fail-fast validation**: `get_env_vars()` validates all required config upfront
- **Graceful degradation**: API failures log warnings and continue, don't crash
- **Dual output**: Most actions produce both Markdown (for GitHub issue/summary) and JSON
- **GitHub Actions Step Summary**: Reports auto-injected into workflow summary
- **65,535 char limit**: Markdown reports handle GitHub's issue body size limit
- **Conventional commits**: PR titles use conventional commit prefixes for changelog generation
