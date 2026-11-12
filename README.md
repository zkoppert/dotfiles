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

`record-demo` still owns pre-PR before/after evidence and the required `demo` marker. `ui-screenshot-presentation` defines how to present authentic UI captures in PR descriptions. `validate-pr-with-codespace` validates an already-open PR and does not satisfy the demo marker. The `gho11y` plugin covers observability, while accessibility findings use `remediate-accessibility-audit`. `session-portability` handles general development handoffs, while specialized handoff skills keep their narrower workflows.

## How are global instructions organized?

The global file keeps rules that apply to every session. Task-specific guidance lives in skills that load before the related action.

`install.sh` links the same global file for Copilot and Pi. It links local skills into `~/.copilot/skills` and `~/.agents/skills`, then mirrors installed catalog skills into `~/.agents/skills`. The linking steps preserve existing non-symlink paths.

`verify-codespace-copilot-env` requires repository skills to resolve to this checkout and Pi catalog mirrors to resolve to their Copilot source. It checks Pi's first available context file, including `AGENTS.override.md`, against the shared instructions. Both the installer and verifier honor `PI_CODING_AGENT_DIR`; the default is `~/.pi/agent`. Use an absolute path, `~`, or a path starting with `~/`.

The verifier uses Pi's installed software development kit (SDK) to resolve user skills, installed packages, and settings filters. Each required skill must remain enabled and resolve to its managed source. Native discovery follows Pi's Markdown, symlink, ignore, directory-name fallback, and traversal rules. Unrelated user skills remain unchanged.

SDK discovery and catalog metadata validation require a working Pi executable and Node.js 22.19.0 or later. Both support npm installations and Homebrew's `libexec/lib/node_modules` layout, including executables behind the Treehouse wrapper. The wrapper honors `PI_REAL_PI`; otherwise, it selects the next Pi executable on `PATH`.

The installer and environment verifier share the SDK-based metadata validator in `scripts/skill_metadata.py`. Its SDK resolver rejects unsupported or unverifiable Node.js versions before metadata parsing or discovery on every platform. Node.js provisioning remains Linux-only. Valid nested metadata remains unchanged. Incorrect names, empty descriptions, malformed YAML, and disabled model invocation still fail validation. If validation is unavailable, setup warns, preserves existing catalog skills, and leaves their Pi mirrors unchanged.

The metadata CLI returns `0` for valid metadata, `1` for invalid metadata, and `2` when validation is unavailable.

Discovery runs offline without loading extensions or installing packages. Project resources, extension-added skills, and command-line overrides remain outside this check.

The `pr-marker-tests` continuous integration (CI) workflow installs Node.js 22.19.0 and Pi 1.3.0 to test the minimum supported runtime. CI runs the metadata and Treehouse launcher suites alongside installer and environment tests. SDK integration tests fail when Pi or Node.js is unavailable instead of skipping.

Run the preservation check after changing global instructions or moving guidance:

```bash
python3 scripts/verify_instruction_migration.py
```

The check compares each original section with its runtime owner. It also validates skill metadata, routing entries, compatibility headings, and the migration baseline. The routing manifest binds each action trigger to an ordered list of explicitly read skills. Fenced examples and HTML comments cannot satisfy a route, including during acknowledged rebaselining. Only repository migration contracts require flat, single-line frontmatter fields, including plain or quoted strings and boolean invocation flags. The preservation check rejects unsupported YAML forms rather than guessing their meaning. Installed catalog and client skills use Pi's YAML parser, including nested metadata and multiline descriptions.

After an intentional guidance edit, review the content diff and refresh the hashes:

```bash
python3 scripts/verify_instruction_migration.py --update-baseline --acknowledge-guidance-changes
```

## How do I choose repository safeguards?

Small personal changes don't always need the same checks as production changes. `repo-guards` selects quality requirements from private, user-owned assignments.

| Profile | Required evidence | Direct default-branch pushes |
| --- | --- | --- |
| `relaxed` | None | Allowed for ordinary fast-forward updates |
| `standard` | HEAD-pinned self-review and successful local checks | Blocked |
| `strict` | Current plan, code, demo, PR-description, and test markers | Blocked |

Unlisted repositories stay strict. Invalid configuration also selects strict. The resolver never prints the assignment inventory.

Create `~/.config/repo-guards/config.yaml` outside any Git checkout. Use this fictional example as a starting point:

```bash
mkdir -p "$HOME/.config/repo-guards"
chmod 700 "$HOME/.config/repo-guards"
# Keep real repository assignments outside this public repository.
(umask 077; printf 'version: 1\nrepositories:\n  example/personal: relaxed\n  example/library: standard\n' > "$HOME/.config/repo-guards/config.yaml")
```

The supported YAML subset has `version: 1`, followed by `repositories:` and two-space-indented `repository: profile` entries. Blank lines and comments are allowed. Use unquoted scalar values, exact names, and one space after each colon. Other YAML features are rejected rather than interpreted.

Keys accept `owner/repository` for GitHub.com or `host/owner/repository`. Names are case-insensitive; duplicate normalized identities invalidate the whole file. Wildcards and configurable defaults aren't supported. The file must have mode `0600`, belong to the current user, and be a regular file. Symlinks and files larger than 64 KiB are rejected.

Inspect the current checkout or an additional target:

```bash
repo-guards show --json
repo-guards show --repo example/library --json
pr-marker status
```

Multiple repository identities use the strictest applicable profile. Worktrees share remote identity but keep separate marker evidence. `gh pr create` also checks explicit `--repo` selectors and `GH_REPO`. Explicit selectors take precedence over `GH_REPO`; `GH_HOST` qualifies unqualified selectors. Uncertain arguments cannot grant reduced safeguards. Implicit PR targets use GitHub CLI resolution, including fork parents and configured default repositories. Lookup failures select strict.

For standard work, record an actual self-review and local checks:

```bash
# Supply your review file, not placeholder evidence.
pr-marker write code-review review.md
pr-marker run-tests --cmd 'python3 -m unittest discover'
```

For a different PR target, add `--repo OWNER/REPOSITORY` to `pr-marker write`. The writer applies the stricter checkout or target profile.

Standard PR creation requires `--draft`. Strict keeps its existing marker and native proof contracts. Relaxed marker checks are optional, not fabricated passes. Agents still need approval to push or create PRs. Explicitly requested checks still apply.

The pre-push hook checks the actual network destination's live default branch. Missing default-branch evidence blocks branch pushes. Relaxed permits initial branch creation only after a successful lookup confirms no remote branches exist. Default-branch deletion and history rewriting remain blocked even for relaxed repositories. Feature-branch pushes retain existing size, privacy, and repository-hook checks. Marker certification stays at PR creation, not in the push hook. Tags and local pipeline transports retain their existing handling.

Existing automation that pushes directly to a default branch needs an explicit relaxed assignment before installation. Review those assignments locally; don't publish the inventory. Native pipeline publication still requires its original strict proof and custody checks.

Installation links the read-only resolver but never creates or changes assignments. Removing an assignment restores strict behavior. Copy private configuration separately between machines; the installer doesn't synchronize it.

These tools prevent accidental shortcuts. They aren't a security boundary against edited policy files, bypassed Git hooks, or direct executable paths.

## How do I install it?

The public repository does not contain the private catalog identifier. Configure `COPILOT_SKILL_CATALOG_REPO` as a user-level Codespaces secret or export it locally before running the installer:

```bash
export COPILOT_SKILL_CATALOG_REPO="OWNER/REPOSITORY"
./install.sh
```

For existing catalog tools, see [the update instructions](#how-do-i-update-catalog-tools). Missing CLIs, authentication failures, or unavailable catalog tools produce warnings without stopping the rest of dotfiles setup.

Open a new shell after installation to load the `~/.local/bin` PATH change.

## How are Python package downloads guarded?

On macOS, the installer puts guarded `pip`, pip-tools, `uv`, and `uvx` shims before normal package tools on interactive shell `PATH`. Package-resolving commands fail unless GitHub Central Feed Services (CFS) supplies its feed environment. Use `gh cfs -- <command>` for package downloads.

The guard prevents common accidental bypasses. It is not egress enforcement. After CFS configures a command, explicit source options, project settings, requirements directives, and direct URLs can still select another source. The conservative allowlist requires CFS for status commands such as `pip list` and `uv pip list`. It also does not intercept absolute paths, virtual-environment executables, `python -m pip`, or LaunchAgents. Persistent automation must call CFS explicitly.

## How do I start a named local Copilot session?

On macOS, the installer adds a `copilot` alias to Zsh. Open a new iTerm tab, enter a Git checkout, and run `copilot`. The launcher asks for a session name and uses that name for Copilot and the terminal title. Copilot's automatic title updates are disabled for that session.

You can supply `copilot --name "Review checkout"` to skip the prompt. Repository subdirectories and linked worktrees are supported. Use `copilot -C PATH` to select a checkout.

Resume options (`--resume`, `--continue`, `--session-id`, and `--connect`) require a checkout but skip naming and leave title management unchanged. Because `--session-id` can also create a session, supply `--name` yourself when you use it for a new session.

Help, version, administrative subcommands, prompt mode, protocol servers, and invocations without terminal input pass through unchanged. Scripts still resolve the original executable because the alias exists only in interactive Zsh. Use `command copilot` to bypass the alias. The installer preserves an existing custom `copilot` definition. The `copilot2` launcher is unchanged.

## How does Pi use Treehouse?

The installer links `bin/pi-treehouse` to `~/.local/bin/pi` and adds an interactive Zsh alias so new Pi coding sessions started inside Git repositories acquire a durable [Treehouse](https://github.com/kunchenguid/treehouse) lease first. Pi runs from the isolated worktree instead of the main checkout. The wrapper prints the exact path, lease ID, holder, and guarded `treehouse return` command. It retains the lease after Pi exits so the Pi session can be resumed safely; return it explicitly when the work is finished.

Pi help/version, package/configuration commands, and session resume/fork/export options bypass acquisition to preserve their normal lookup behavior. Invocations outside Git repositories also pass through. Set `PI_TREEHOUSE_BYPASS=1` only for an intentional main-checkout run. Starting Pi from an existing managed Treehouse slot does not acquire a nested worktree.

Treehouse must be installed at `~/.local/bin/treehouse`. The wrapper fails closed when a repository session needs isolation and Treehouse is unavailable.

## How do dotfiles and skills stay updated?

The `com.zkoppert.dotfiles-sync` LaunchAgent checks this laptop's `~/repos/dotfiles` checkout daily at 09:00 and when loaded. It extends the existing job rather than adding another updater.

The scheduled runner uses an approved copy outside the checkout, with isolated Python startup. It never imports Python packages from the working tree. Dirty checkouts, another active branch, or unfinished Git operations cause a skipped update and a clickable Mac notification. Local edits remain untouched; nothing is stashed or restored.

The updater requires a clean `main` checkout before updating. It fetches `origin main` and merges its explicit remote-tracking ref instead of shared `FETCH_HEAD`. Fast-forward flags prevent rebasing, automatic stashing, and overwriting ignored files. Authentication must work without interactive prompts. Git commands have a two-minute timeout, and timed-out process groups are terminated.

The job requires Git, Pi, Node.js 22.19.0 or later, and Homebrew's `terminal-notifier`. The plist supplies the notifier path explicitly. After updating, it validates tracked skill metadata with the existing Pi SDK parser. It creates missing links in `~/.copilot/skills` and `~/.agents/skills` and ensures the shared instruction links. `PI_CODING_AGENT_DIR` selects the Pi instruction directory; `--pi-agent-dir` overrides it. The default is `~/.pi/agent`.

Existing files, directories, and conflicting symlinks remain unchanged and cause a reported failure. Broken links require manual resolution. Private catalog skills and removed repository skills aren't deleted. The updater doesn't run the broad installer or restore Git history after a failed validation. Existing links follow trusted merged checkout contents, as they do after a manual pull.

The runner and metadata helper are copied from committed Git blobs into a versioned private directory. Updating dotfiles doesn't automatically replace that approved runner. Reinstall it from reviewed, clean `main` when either file changes. Old versions remain available for rollback.

Inspect an installed runner without fetching, creating links, taking the update lock, or sending notifications:

```bash
runner="$HOME/Library/Application Support/dotfiles-sync/runtime/current/dotfiles_sync.py"
python3 -I -S -B "$runner" --repository "$HOME/repos/dotfiles" --branch main --check
```

Check mode makes no remote-freshness claim. Exit status `2` means links are missing; status `1` reports a refusal or failure. A scheduled skip returns success only if the notification command accepts the request. Failed notification requests return status `1`. JSON distinguishes `skipped` from a completed update. Repeated alerts replace the same notification group, and clicking opens the checkout. A normal run creates missing links. Its nonblocking lock coordinates manual and scheduled runs; it doesn't lock out unrelated Git commands. Use Treehouse worktrees for feature work. Avoid other Git mutations in this managed checkout while an update runs.

### Upgrade the existing laptop agent

Merge and pull the reviewed installer before deploying the plist. Run the installer by its explicit path with `-I -S`, not through a checkout package. This plist uses absolute laptop paths and a Homebrew Python interpreter. Adjust those paths for another machine or a custom Pi agent directory. Confirm the current agent isn't running before reloading it. Loading the agent starts one immediate sync.

The following upgrade preserves the original plist and restores it if loading the replacement fails:

```bash
(
set -eu
repo="$HOME/repos/dotfiles"
target="$HOME/Library/LaunchAgents/com.zkoppert.dotfiles-sync.plist"
backup_dir="$HOME/Library/Application Support/dotfiles-sync"
backup="$backup_dir/agent.$(date -u +%Y%m%dT%H%M%SZ).plist"
test -f "$target"
test ! -L "$target"
test ! -e "$backup"
plutil -lint "$repo/LaunchAgents/com.zkoppert.dotfiles-sync.plist"
mkdir -p "$HOME/Library/Logs" "$backup_dir"
cp -p "$target" "$backup"
current="$HOME/Library/Application Support/dotfiles-sync/runtime/current"
previous_runtime=""
if [ -L "$current" ]; then previous_runtime="$(readlink "$current")"; fi
launchctl bootout "gui/$(id -u)/com.zkoppert.dotfiles-sync"
if /opt/homebrew/bin/python3 -I -S -B "$repo/scripts/dotfiles_sync.py" \
     --repository "$repo" --branch main --install-runtime &&
   cp -p "$repo/LaunchAgents/com.zkoppert.dotfiles-sync.plist" "$target" &&
   launchctl bootstrap "gui/$(id -u)" "$target"; then
  launchctl print "gui/$(id -u)/com.zkoppert.dotfiles-sync"
else
  if [ -n "$previous_runtime" ]; then ln -sfn "$previous_runtime" "$current"; fi
  cp -p "$backup" "$target"
  launchctl bootstrap "gui/$(id -u)" "$target"
  exit 1
fi
)
```

Results are timestamped JSON lines in `~/Library/Logs/dotfiles-sync.log`. Repeated runs create no duplicate links. No packages, hooks, shell startup files, or unrelated agents are installed. Rerun the upgrade when the runner, metadata helper, or plist changes. Updating the checkout alone doesn't replace the trusted runtime or reload launchd configuration.

Disable future runs without changing the checkout:

```bash
launchctl bootout "gui/$(id -u)/com.zkoppert.dotfiles-sync"
```

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

## How do I deploy updated Copilot guidance?

Merge the dotfiles change to `main`, update the local checkout to `origin/main`, then run:

```bash
setup-copilot2-codespace --refresh-default
```

The command requires a clean local checkout at the exact `origin/main` commit. It runs `install.sh` on the laptop, verifies the instruction and `test-quality` links, then refreshes the configured `copilot2` Codespace.

The remote refresh requires a clean persisted dotfiles checkout that can fast-forward to the same commit. It runs the installer and environment verifier before reporting success. The command does not change `~/.config/copilot2/default.json`.

If the remote refresh fails after local installation, the command reports a partial deployment. Resolve the remote checkout or environment error, then run the same command again.

## How do I configure Copilot tools in a Codespace?

Set `COPILOT_SKILL_CATALOG_REPO` and `COPILOT_1UP_MODULE` as user-level Codespaces secrets before running the installer. Restrict their repository access to the target repository. Use the approved versioned Go module for `COPILOT_1UP_MODULE`.

Inside the Codespace, run:

```bash
cd /workspaces/.codespaces/.persistedshare/dotfiles
./install.sh
export PATH="$HOME/.local/bin:$PATH"
bootstrap-copilot-mcp
```

On Linux x86_64 and ARM64, the installer adds a supported Node.js 22 release when Node.js is missing or older than 22.19.0. Node setup runs before SDK-backed catalog validation. Installation requires `curl`, Python 3, and `tar` with xz support. Pi `1.3.0` requires Node.js `22.19.0` or later. The installer preserves existing files that are not symlinks in `~/.local/bin`; resolve any reported command conflicts before continuing.

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

The install-once checks leave valid catalog skills unchanged. Only confirmed metadata errors trigger automatic reinstallation. Update a skill by name, using the `CATALOG_SKILLS` list in [the installer](install.sh) for other names:

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

The personal `pr-size-guard` keeps PRs in `github/github` and `github/github-ui` at 800 added lines or fewer. `gh-guard` checks draft creation, and the global `git-hooks/pre-push` hook checks every later branch push. The count includes tests, generated files, lockfiles, and snapshots. Split larger work with `npx -y gh-axi stack`; use `ZACK_CONFIRMED_LARGE_PR=1` only after Zack approves an exception.

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
