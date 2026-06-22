---
name: actions-security
description: This skill should be used WHENEVER authoring, editing, or reviewing GitHub Actions workflow files (.github/workflows/*.yml or *.yaml, composite actions, reusable workflows). It encodes GitHub's internal secure-Actions authoring rules (script-injection hardening, SHA pinning, least-privilege tokens, runner safety, secret handling) and runs a local zizmor + actionlint harness. Also triggered when the user asks to "harden this workflow", "check this action for security issues", "review my workflow", "run zizmor", or "is this Action injection-safe".
---

# Actions Security: harden GitHub Actions workflows before they ship

Zack maintains a suite of open-source GitHub Actions (`stale-repos`, `issue-metrics`,
`contributors`, `evergreen`, `cleanowners`, `measure-innersource`, `pr-conflict-detector`,
`validate-style-action`) plus internal automation. These rules come from GitHub's internal
secure-coding guidance (`thehub` `dev-practicals/secure-coding/secure-coding-general/actions-best-practices.md`).
Apply them at **authoring time**, not just in review, and run the local harness before
considering a workflow done.

## The non-negotiable authoring rules

### 1. Treat all event data as untrusted (script-injection defense)

These fields are attacker-controllable and must NEVER be interpolated inline inside a
`run:` block with `${{ ... }}`:

- `github.event.issue.title`, `github.event.issue.body`
- `github.event.pull_request.title`, `github.event.pull_request.body`
- `github.event.comment.body`, `github.event.review.body`, `github.event.review_comment.body`
- `github.event.commits.*.message`, `github.event.head_commit.message`
- `github.event.pull_request.head.ref` (and `github.head_ref`), `*.head.label`, `*.head.repo.*`
- `github.event.discussion.title`, `github.event.discussion.body`

**Wrong (template injection):**
```yaml
- run: echo "Title: ${{ github.event.issue.title }}"
```

**Right (bind to an env var first, then expand the shell variable, always quoted):**
```yaml
- env:
    TITLE: ${{ github.event.issue.title }}
  run: echo "Title: $TITLE"
```

The `${{ }}` expansion happens before the shell runs, so a title like
`"; rm -rf / #` executes as code. Binding to `env:` makes it a plain string the shell reads.

### 2. Pin third-party actions to a full commit SHA + version comment

```yaml
uses: actions/checkout@<full-40-char-sha> # v4.2.2
```

Tags are mutable; SHAs are not. The comment must show the **full** version tag
(`# v4.2.2`, not `# v4`). Prefer first-party/local actions when possible.

### 3. Least-privilege `GITHUB_TOKEN`

Set `permissions:` to read-only at the top of every workflow and grant only the specific
scopes a job needs (e.g. `contents: read`, `issues: write`). Never rely on the default
broad token.

### 4. Dangerous triggers

Be extremely careful with `pull_request_target` and `workflow_run`: they run with write
tokens and secrets in the context of the base repo. Do NOT check out and execute untrusted
PR head code in those jobs. Use `pull_request` (no secrets, fork-isolated) for anything
that runs PR-author code.

### 5. Runner safety

Use GitHub-hosted runners for anything that can run untrusted/fork code. Avoid self-hosted
runners for public-PR-triggered jobs.

### 6. Secrets hygiene

Keep secrets out of workflow YAML and out of logs. Pass them via `secrets`/`env`, use a
dedicated token per workflow (don't reuse across workflows), don't pass structured data as
a single secret, and verify nothing sensitive lands in logs.

## Run the harness before you call it done

Both `zizmor` (security) and `actionlint` (correctness) are installed locally. Run:

```bash
~/.copilot/skills/actions-security/check.sh [path ...]
```

With no argument it scans `.github/workflows` in the current repo. It runs `zizmor`
(authoritative for injection / dangerous-triggers / unpinned actions), `actionlint`,
and a fast heuristic grep for inline untrusted interpolation. It exits non-zero if either
tool reports findings, so it doubles as a pre-commit gate. Fix findings rather than
suppressing them; only add a `# zizmor: ignore[rule]` comment as a documented last resort.

## When reviewing someone else's workflow

Run the harness, then walk each `run:` block and each `pull_request_target`/`workflow_run`
trigger by hand to confirm no untrusted field reaches a shell. Report only verified issues,
and prefer a `suggestion` block showing the `env:`-binding fix.
