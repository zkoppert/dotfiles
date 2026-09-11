# dotfiles

My personal configuration for macOS and GitHub Codespaces.

## What does the installer configure?

`install.sh` links the repository's Copilot instructions, local skills, CLI wrappers, and macOS launch agents into their user-level locations. It can also install these tools from a private skill catalog:

| Tool | Problem it addresses |
| --- | --- |
| `validate-pr-with-codespace` | Rechecks an open PR in a clean Codespace so local state cannot hide setup or integration failures. |
| `session-portability` | Carries general development context between local sessions and Codespaces without rebuilding the handoff manually. |
| `cleanup-worktrees` | Removes stale worktrees safely while preserving worktrees with uncommitted changes. |
| `remediate-accessibility-audit` | Guides accessibility audit validation and remediation. |
| `gho11y` plugin | Supports GitHub observability investigations and production-signal analysis. |

`record-demo` still owns pre-PR before/after evidence and the required `demo` marker. `validate-pr-with-codespace` validates an already-open PR and does not satisfy that marker. The `gho11y` plugin covers observability, while accessibility findings use `remediate-accessibility-audit`. `session-portability` handles general development handoffs, while specialized handoff skills keep their narrower workflows.

## How do I install it?

The public repository does not contain the private catalog identifier. Configure `COPILOT_SKILL_CATALOG_REPO` as a user-level Codespaces secret or export it locally before running the installer:

```bash
export COPILOT_SKILL_CATALOG_REPO="OWNER/REPOSITORY"
./install.sh
```

The installer skips tools that are already present. Missing CLIs, authentication failures, or unavailable catalog tools produce warnings without stopping the rest of dotfiles setup.

## How do I update catalog tools?

The install-once checks do not update existing tools. Refresh them explicitly:

```bash
gh skill update validate-pr-with-codespace session-portability cleanup-worktrees remediate-accessibility-audit
copilot plugin update gho11y
```

To retry a failed or interrupted setup, confirm that `gh auth status` and `copilot --version` succeed, set `COPILOT_SKILL_CATALOG_REPO`, and run `./install.sh` again.

## How do exact-head review exceptions work?

`bin/pr-marker` owns a private, explicit exception for an authorized continuation of an **existing** PR. It waives only the `plan`, `code-review`, and `pr-review` requirements, including model counts and clean convergence. It reports **WAIVED (not clean approval)** and leaves all original reviewer files and findings intact. Without a waiver record, existing CLI behavior is unchanged.

### Approval and application

Obtain explicit owner approval for that PR, source branch, full commit, and the one-time review exception. Approval to implement this mechanism does not authorize applying it to another change. Keep the approval, reason, and provenance in private storage, outside tracked files. The record is an honest attestation of that approval, not a signature verifier; like existing marker evidence, it does not defend against deliberate forgery by the checkout owner.

Create a private JSON file with exactly these fields (the values below are placeholders):

```json
{
  "schema": "pr-marker-review-waiver.v1",
  "repo": "OWNER/REPOSITORY",
  "pr": 42,
  "source_branch": "author/existing-pr-source",
  "local_branch": "task/local-checkout-branch",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "approved_by": "Owner who authorized the exception",
  "reason": "One-time continuation without further reviews or clean convergence",
  "provenance": "Private references to the explicit exception and approval messages"
}
```

`repo` is the exact canonical GitHub.com owner/name, without a URL or `.git` suffix. `pr` must be a positive JSON integer. `commit` must be the complete lowercase 40-character SHA. All text fields must be nonempty, trimmed strings without control characters. Missing, extra, or duplicate fields are rejected. Both branch names must be literal valid Git branch names. `local_branch` is separate because an isolated task branch can publish to an existing PR's differently named `source_branch`.

From the approved, clean checkout at that exact commit:

```bash
pr-marker review-waiver apply /private/path/approval.json
pr-marker status --repo OWNER/REPOSITORY --pr 42 --source-branch author/existing-pr-source
pr-marker check --repo OWNER/REPOSITORY --pr 42 --source-branch author/existing-pr-source
```

Application validates the local branch, HEAD, cleanliness, and single canonical `origin` URL (HTTPS, SCP-style SSH, or `ssh://git@github.com/`). It stores `review-waiver.json` with mode `0600` beside the existing per-branch markers in the **per-checkout Git directory**, never in the tracked tree or a global exemption list. Applying again refuses to replace an existing record. Symlink records are rejected.

### Checking and publication

Every check with a stored waiver requires all three explicit target arguments and revalidates the checkout. A wrong repository, PR, source branch, local branch, or HEAD refuses; malformed records refuse even if the review markers themselves pass. A future head cannot inherit consent. Plain `check`/`status` refuse while a waiver exists because they cannot establish the intended PR. Reports retain each marker's actual evidence status alongside the waived requirement and include the approval reason and provenance; keep those reports private too.

The `demo` and machine-produced, HEAD-pinned `tests` markers must still pass. Failed tests cannot be waived, and dirty checkouts refuse. Continue to use `pr-marker run-tests` for the full applicable checks. The exception adds no push, merge, readiness, force-push, branch-protection, or CI authority. Publication must still verify the live PR's canonical repository and source branch, remote predecessor, readiness and body, fast-forward safety, required CI, and all existing protections. `pr-marker` checks local evidence and explicit target context; it does not query GitHub or publish anything.

`gh-guard` delegates to the same canonical reader when it encounters a waiver during `pr create`. It refuses creation even when old review markers are clean: an existing-PR authorization cannot justify a new PR. Its usual confirmations, body linting, ready/draft guards, and default marker checks remain in force. Existing-PR continuation uses the explicit `pr-marker check` above before the already-authorized publication workflow.

### Revocation and activation

After the authorized continuation, revoke the local record:

```bash
pr-marker review-waiver revoke
```

Revocation removes only the current branch's exception, including a malformed or stale record. It never rewrites review evidence. Repeated checks on the exact approved revision are allowed until revocation, so status inspection does not consume consent; “one-time” means one explicitly approved PR revision, not one CLI invocation. A new revision needs new explicit authorization and a new record, or the normal review workflow after revocation.

The supported activation path is the existing `./install.sh` from a validated, durable dotfiles checkout, followed by a new shell (or `export PATH="$HOME/.local/bin:$PATH"` in the current shell). It links both `pr-marker` and `gh-guard` from the same checkout. It also updates the other documented user-level dotfiles integrations: obtain authorization for that installation scope before running it in a managed environment. Do not install from a disposable worktree that will be removed. Both helpers require the existing Python 3.9+ runtime. Implementation/validation alone does not install the helpers or mint a live waiver.

## How does the accessibility issue picker work?

The macOS installer can schedule one accessibility remediation attempt each hour. The picker claims one eligible unassigned issue, starts a credential-restricted Copilot session in the local command sandbox, saves the handoff, and sends a notification that can prepare the saved session in iTerm without executing it.

Keep private repository names and labels outside this public repository in `~/.config/accessibility-issue-picker.env`:

```bash
ACCESSIBILITY_ISSUE_REPO="OWNER/REPOSITORY"
ACCESSIBILITY_AUDIT_REPO="OWNER/AUDIT-REPOSITORY"
ACCESSIBILITY_LABELS="accessibility,label-two"
ACCESSIBILITY_ASSIGNEE="YOUR-LOGIN"
ACCESSIBILITY_WORKDIR="$HOME/repos/accessibility-remediation"
ACCESSIBILITY_GITHUB_TOKEN="FINE-GRAINED-TOKEN"
```

Create a dedicated fine-grained token that can access only the configured tracking, audit, and remediation repositories. Grant issue read/write access for assignment management and read access to repository contents for remote verification. The picker uses this token instead of your general `gh` credential, including for Copilot's allowed GitHub tools.

Set the configuration file to mode `0600`. Populate `ACCESSIBILITY_WORKDIR` with at least one local Git checkout and install its test dependencies before enabling the schedule. The unattended shell cannot read login credentials, access Keychain, or use outbound networking.

Run `chmod 0600 ~/.config/accessibility-issue-picker.env` and `./install.sh` after creating the file. Both wrappers reject broader permissions before sourcing the file and suppress output from the sourced shell content. Mode `0600` protects confidentiality from other local users; the file remains trusted executable shell configuration that you own and control. The installer loads the hourly job only after no-network validation confirms the configuration and workspace are ready. Use `accessibility-issue-picker --dry-run` to verify discovery without claiming an issue or starting Copilot. To stop the hourly schedule, unload the agent before removing its symlink:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.zkoppert.accessibility-issue-picker.plist"
rm "$HOME/Library/LaunchAgents/com.zkoppert.accessibility-issue-picker.plist"
```
