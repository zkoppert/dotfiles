---
name: open-source-actions-maintenance
description: Use for maintenance in contributors, evergreen, issue-metrics, stale-repos, cleanowners, measure-innersource, pr-conflict-detector, or ospo-reusable-workflows.
---

# Open source Actions maintenance

Read and follow this skill for the named GitHub Actions repositories. Load `actions-security` before changing workflow files.

## My Open Source GitHub Actions: Shared Patterns

The following conventions apply to my suite of GitHub Actions in the `github-community-projects` org: **contributors**, **evergreen**, **issue-metrics**, **stale-repos**, **cleanowners**, **measure-innersource**, **pr-conflict-detector**, and **ospo-reusable-workflows**. These repos share a consistent architecture; follow these patterns when working in any of them.

### Project Structure
- **Flat module layout** - all Python source files live at the repo root (no `src/` directory)
- Common module split: `{main}.py`, `auth.py`, `env.py`, `markdown_writer.py` (or `markdown.py`)
- Test files: `test_*.py` at root, one per source module, never catch-all aggregator files like `test_coverage_additions.py`
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
1. **flake8** - strict errors (E9, F63, F7, F82), then warnings with exit-zero; config at `.github/linters/.flake8`
2. **isort** - import sorting; config at `.github/linters/.isort.cfg`
3. **pylint** - minimum score 9.0; config at `.github/linters/.python-lint`
4. **mypy** - type checking; config at `.github/linters/.mypy.ini`
5. **black** - code formatting (no custom config)

### Testing
- **pytest** with **pytest-cov** - minimum 80% code coverage (`--cov-fail-under=80`)
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
- **python-ci.yml** - lint + test on push/PR to main (Python version matrix)
- **docker-ci.yml** - build Docker image (linux/amd64)
- **super-linter.yaml** - GitHub Super-Linter for markdown, YAML, shell
- **release.yml** - automated semantic versioning releases to GHCR
- **scorecard.yml** - OpenSSF security scorecard
- **stale.yaml** - auto-close stale issues/PRs
- **auto-labeler.yml** - PR label automation
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
<!-- This boundary keeps the migrated baseline block byte-verifiable. -->
