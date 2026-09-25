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
| Additional private skills | Installs `gh-axi`, investigation, release, and production database helpers. |
| `gho11y` plugin | Supports GitHub observability investigations and production-signal analysis. |

`record-demo` still owns pre-PR before/after evidence and the required `demo` marker. `validate-pr-with-codespace` validates an already-open PR and does not satisfy that marker. The `gho11y` plugin covers observability, while accessibility findings use `remediate-accessibility-audit`. `session-portability` handles general development handoffs, while specialized handoff skills keep their narrower workflows.

## How do I install it?

The public repository does not contain the private catalog identifier. Configure `COPILOT_SKILL_CATALOG_REPO` as a user-level Codespaces secret or export it locally before running the installer:

```bash
export COPILOT_SKILL_CATALOG_REPO="OWNER/REPOSITORY"
./install.sh
```

For existing catalog tools, see [the update instructions](#how-do-i-update-catalog-tools). Missing CLIs, authentication failures, or unavailable catalog tools produce warnings without stopping the rest of dotfiles setup.

Open a new shell after installation to load the `~/.local/bin` PATH change.

## How do I run Copilot in a durable Codespace session?

Install dotfiles on the laptop and [configure the Codespace](#how-do-i-configure-copilot-tools-in-a-codespace). The laptop needs Python 3.9 or later, OpenSSH, and an authenticated GitHub CLI. Then run:

```bash
copilot2
```

The command uses the exact Codespace selected by `setup-copilot2-codespace`. It falls back to finding `gummyworm` by display name when no default configuration exists. It runs `copilot --allow-all` inside `tmux` and reconnects after SSH transport failures. The `--allow-all` flag permits all Copilot tools without individual approval. Use this command only in a trusted workspace.

Use `--codespace NAME` or `COPILOT2_CODESPACE` to override the selected default. Use `--repo OWNER/REPOSITORY` or `COPILOT2_REPOSITORY` to validate or filter the repository. Set `COPILOT2_DISPLAY_NAME` or pass `--display-name NAME` to search by display name when no exact Codespace is selected.

## How do I replace the default Copilot Codespace?

Run one command on the laptop:

```bash
setup-copilot2-codespace
```

The command copies the repository, machine type, devcontainer path, and region from `gummyworm`. It creates `copilot2-default` with a four-hour idle timeout and 30-day retention. It waits for dotfiles, reruns `install.sh`, refreshes the managed MCP definitions, and runs `verify-codespace-copilot-env`.

The command copies required local-only skills from `~/.copilot/skills`. It excludes Git metadata, Python caches, and test caches. It does not copy Copilot sessions, logs, MCP credentials, or OAuth state.

The command forwards the local GitHub CLI token through encrypted SSH during bootstrap. It does not write the token to the Codespace. The temporary token lets the installer fetch catalog skills, the plugin, and `1up`.

The command updates `~/.config/copilot2/default.json` only after tool verification succeeds. Tailscale and Azure CLI authentication can remain pending because they require interactive login. The command does not delete or modify the source Codespace. Use `--source-codespace NAME` when multiple source Codespaces share the same display name.

Use explicit values when the source Codespace is unavailable:

```bash
setup-copilot2-codespace \
  --repo OWNER/REPOSITORY \
  --machine MACHINE \
  --devcontainer-path PATH
```

OAuth services still require interactive authentication. After creation, run `copilot2`, open `/mcp`, and authenticate DataDog, Sentry, and Slack. Run `az login` for Kusto.

The launcher passes `/workspaces/<repository-name>` to the remote helper from the resolved Codespace repository. Set `COPILOT2_REMOTE_CWD` inside the Codespace only when the checkout uses another path.

The helper stores continuity state in `/workspaces/.copilot2`, outside repository checkouts. This Codespace-local directory survives rebuilds and is shared by all launcher devices. Keep it between connections and rebuilds.

Stop old launcher clients before upgrading the helper. Run `copilot2` with the updated helper before rebuilding an existing Codespace. The helper migrates session records from `$XDG_STATE_HOME/copilot2`, or `~/.local/state/copilot2` when `XDG_STATE_HOME` is unset or empty. Migration validates records and uses atomic writes under source and destination locks. Existing persistent records take precedence; the helper keeps legacy files as backups. Invalid or inaccessible records that need migration stop startup instead of resetting continuity. Migration cannot recover state that an earlier rebuild already removed.

The reconnect window defaults to 600 seconds of consecutive connection failures. Set `COPILOT2_MAX_RECONNECT_SECONDS` to a positive integer to change it. Each confirmed interactive SSH connection resets this window. Time spent working in Copilot does not consume it.

Set `COPILOT2_RETRY_DELAYS` to comma-separated nonnegative integers to change the retry delays in seconds. The default is `2,4,8,15`; subsequent failures repeat the last delay until the reconnect window expires.

Use a different name to run another Copilot process. Session names can contain only letters, numbers, underscores, and hyphens.

```bash
copilot2 review-one
copilot2 review-two
```

Use the same session name and Codespace options on another device to attach to the live process. Each attachment detaches other tmux clients from that session.

The wrapper preserves work across SSH and network loss. A Codespace stop or rebuild ends the remote process. The wrapper reports that event and does not silently start a replacement process.

Each launcher invocation stays bound to its original Copilot process. The helper retains those bindings after a session ends, so old retries cannot create or adopt a replacement.

An interrupted startup requires an explicit replacement if its process did not survive.

When the tmux session no longer exists, start a replacement after that warning:

```bash
copilot2 --new
```

Keep the same session name and Codespace options when you run `--new`. If that Copilot process is still live, the command attaches without replacing it. If the tmux session remains without its original Copilot process, use a different session name. Use Copilot's `/resume` command if you need the previous conversation context.

Set the Codespaces idle timeout to the maximum value your organization allows. GitHub supports values up to four hours.

## How do I configure Copilot tools in a Codespace?

Set `COPILOT_SKILL_CATALOG_REPO` and `COPILOT_1UP_MODULE` as user-level Codespaces secrets before running the installer. Restrict their repository access to the target repository. Use the approved versioned Go module for `COPILOT_1UP_MODULE`.

Inside the Codespace, run:

```bash
cd /workspaces/.codespaces/.persistedshare/dotfiles
./install.sh
export PATH="$HOME/.local/bin:$PATH"
bootstrap-copilot-mcp
```

On Linux x86_64 and ARM64, the installer adds Node.js 22 when Node.js is missing or older. Installation requires `curl`, Python 3, and `tar` with xz support. Azure MCP requires Node.js 22 or later. The installer preserves existing files that are not symlinks in `~/.local/bin`; resolve any reported command conflicts before continuing.

The Linux installer places a missing `1up` in `~/.local/bin`. This step requires Go and access to the private module.

The [MCP bootstrap](bin/bootstrap-copilot-mcp) owns the managed server definitions. It refreshes them in `~/.copilot/mcp-config.json` on every run and preserves unrelated server names. Use different names for custom definitions. The bootstrap writes the file with mode `0600` and does not copy credentials from another machine.

Open Copilot and use `/mcp` to authenticate DataDog, Sentry, and Slack. Complete DataDog's OAuth flow in the Codespace. Run `az login` for Kusto. Splunk and PagerDuty remain configured but intentionally unauthenticated; do not add Tailscale or a PagerDuty credential workaround.

After setup, check the environment:

```bash
verify-codespace-copilot-env
```

The [verifier](bin/verify-codespace-copilot-env) owns the prerequisite checks. The installer does not provision every required tool. The verifier requires managed MCP entries to match the bootstrap definitions, but it does not authenticate OAuth services or test MCP connections.

Splunk checks still require Docker, connected Tailscale, `COPILOT_MCP_SPLUNK_BEARER_TOKEN` (bearer token), and `COPILOT_MCP_SPLUNK_HOST` (hostname). The verifier reports an incomplete environment when those prerequisites are absent, even though Splunk is intentionally unauthenticated. Address any unrelated failures in the report.

Never copy `~/.copilot/mcp-config.json`, its backup files, `mcp-oauth-config`, session databases, or logs between machines. These files can contain credentials and machine-specific state.

## How do I update catalog tools?

The install-once checks do not update existing catalog tools. Update a skill by name, using the `CATALOG_SKILLS` list in [the installer](install.sh) for other names:

```bash
gh skill update validate-pr-with-codespace
copilot plugin update gho11y
```

The installer leaves disabled plugins disabled. Run `copilot plugin enable gho11y` if the installer reports that the plugin is disabled.

To retry a failed or interrupted setup, confirm that `gh auth status` and `copilot --version` succeed, set `COPILOT_SKILL_CATALOG_REPO`, and run `./install.sh` again.

## How are native publication handoffs checked?

`gh-guard` treats either `NO_MISTAKES_PUBLICATION_RUN` or `NO_MISTAKES_PUBLICATION_ATTEMPT` as a required native handoff on any create or edit command. Both are opaque locators, not proof or consent. Its adjacent `pr-marker` checks the actual command, Git context, body bytes, and producer-owned evidence through `no-mistakes axi publication verify`. After complete validation, `check --print-publication-lint-context --publication <gh argv>` exposes JSON containing the verified `repository` and parsed absolute `body_file`. The guard lints that file directly rather than scanning the arguments again. An option-looking title cannot replace the body or trigger a read of another file. Native visibility reads use the verified target with `gh repo view`, including host-qualified repositories. The earlier repository-only output remains available through `--print-publication-repository`. The helper resolves the native CLI from the existing operator-controlled PATH and refuses an older CLI before attempting the verifier. No locator-supplied executable or caller proof file is accepted. Native mutations require the full body handoff; ordinary non-body edits remain unchanged.

The four exact read-only help calls (`gh pr create --help`, `gh pr create -h`, `gh pr edit --help`, and `gh pr edit -h`) go directly to the CLI, even with native locators. Extra arguments and help-looking title values do not take that path; native publication still requires the complete proof.

`pr-marker capabilities --json` describes the consumer's requirements, not an installed producer's readiness. It requires all five artifact floors and at least three distinct actual non-Gemini models at each review gate. It also requires a plan reviewed before implementation, full machine checks, and a cumulative ceiling of ten code-review panels. Plan and description rounds remain separately required and recorded; failed or incomplete code panels still spend the code budget. Unknown history cannot become zero on a retry.

The normal native proof retains the earlier `plan.run_id`, its baseline checks and reviewed subject, and the later implementation admission. The top-level `preparation_id` names the finalization round, not that earlier run. Final checks must follow admission, cover the same pinned command manifest, and precede final-head code and exact-body description reviews. The native verifier owns immutable run/configuration pins, actual execution provenance, and accumulated history; caller names, files, or old logs cannot supply those facts.

The candidate reader also advertises `planning_exception_support: true` for the producer's implemented run-bound authorization form. This is recognition, not a planning waiver or a verified live producer capability. A matching native verifier must bind the authorization to the same run, source/store/branch, submitted work and intent/policy. Its recorded PID is historical attribution, not a live process to inspect. The exception keeps `plan: null`, zero prior-plan admission, truthful plan-history counts, and four actual remaining artifacts. Final checks must follow authorization and match the native pin's exact ordered `required_commands`; all remaining review, body and creation requirements still apply. A compatible isolated runtime and genuine producer evidence must still be verified before using this path.

On normal native success, `gh-guard` consumes those five verified artifacts directly without requiring a second set of caller-authored marker files. It requires completed body/visibility linting and explicit same-call creation confirmation, then verifies the actual command context before publication. Native publication refuses unavailable lint tooling or a target-visibility lookup that fails or returns an unknown value; successful proof alone cannot replace those checks. A partial locator, older producer, changed head/body/store/branch, replaced plan, or incompatible policy fails closed. The read-only check never imports artifacts, overwrites existing markers, or resets counts. With neither locator, existing non-native behavior and legacy marker metadata remain unchanged. Implementing this reader does not install, enable, or certify a compatible producer.

## How does the accessibility issue picker work?

The macOS installer can schedule one accessibility remediation attempt each hour. The picker claims one eligible unassigned issue and starts a credential-restricted Copilot session in the local command sandbox. It saves the handoff and sends a notification that can prepare the saved session in iTerm without executing it.

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
