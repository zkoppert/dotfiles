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
