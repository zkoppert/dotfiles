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

## How are native publication handoffs checked?

`gh-guard` treats either `NO_MISTAKES_PUBLICATION_RUN` or `NO_MISTAKES_PUBLICATION_ATTEMPT` as a required native handoff on a create or body-edit command. Both are opaque locators, not proof or consent. Its adjacent `pr-marker` checks the actual command, Git context, body bytes, and producer-owned evidence through `no-mistakes axi publication verify`. It resolves that CLI from the existing operator-controlled PATH and refuses an older CLI before attempting the verifier. No locator-supplied executable or caller proof file is accepted.

`pr-marker capabilities --json` describes the consumer's requirements, not an installed producer's readiness: all five artifact floors, at least three distinct actual non-Gemini models at each review gate, a plan reviewed before implementation, full machine checks, and a cumulative ceiling of ten code-review panels. Plan and description rounds remain separately required and recorded; failed or incomplete code panels still spend the code budget. Unknown history cannot become zero on a retry.

The normal native proof retains the earlier `plan.run_id`, its baseline checks and reviewed subject, and the later implementation admission. The top-level `preparation_id` names the finalization round, not that earlier run. Final checks must follow admission, cover the same pinned command manifest, and precede final-head code and exact-body description reviews. The native verifier owns immutable run/configuration pins, actual execution provenance, and accumulated history; caller names, files, or old logs cannot supply those facts.

The candidate reader also advertises `planning_exception_support: true` for the producer's proposed run-bound authorization form. This is recognition, not a planning waiver or a verified live producer capability. A matching native verifier must bind the authorization to the same run, source/store/branch, submitted work and intent/policy; its recorded PID is historical attribution, not a live process to inspect. The exception keeps `plan: null`, zero prior-plan admission, truthful plan-history counts, and four actual remaining artifacts. Final checks must follow authorization and match the native pin's exact ordered `required_commands`; all remaining review, body and creation requirements still apply. Matching implemented native evidence and the isolated execution route must be verified before using this path.

On normal native success, `gh-guard` consumes those five verified artifacts directly without requiring a second set of caller-authored marker files. It keeps body/visibility linting and explicit same-call creation confirmation, then verifies the actual command context before publication. A partial locator, older producer, changed head/body/store/branch, replaced plan, or incompatible policy fails closed. The read-only check never imports artifacts, overwrites existing markers, or resets counts. With neither locator, existing non-native behavior and legacy marker metadata remain unchanged. Implementing this reader does not install, enable, or certify a compatible producer.

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
