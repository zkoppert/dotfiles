---
name: subagent-monitor
description: Use whenever launching, delegating to, resuming, or checking background coding agents or parallel subagents. Register automatic launchd monitoring before reporting agents as running. Deliver blocked, stalled, stopped, PR-created, and completion updates without waiting for the user to ask. Do not use Pi cron.
compatibility: macOS launchd, Python 3, terminal-notifier, and the Pi subagent-monitor extension.
---
<!-- Managed by pi-subagent-monitor. -->

# Monitor background agents automatically

Background agents can stop silently. Register monitoring during every background-agent launch, not after the user asks for an update.

## Check the runtime

Run `pi-subagent-monitor --help` and inspect `launchctl print gui/$(id -u)/com.zkoppert.pi-subagent-monitor`.

If installation is missing, locate the user's dotfiles checkout. Read its instructions and run its repository safeguards first.

Install from that checkout:

```bash
# Install a user LaunchAgent, command, skill, and Pi extension.
python3 scripts/pi_subagent_monitor.py install
```

The LaunchAgent polls every minute while the user is logged in. Sleeping computers resume checks after waking.

Desktop notifications work without an open Pi session. A loaded Pi extension delivers pending updates when the parent conversation is idle.

New sessions load the extension automatically. Existing sessions need `/reload` once after installation. Never claim chat delivery before verifying extension loading.

Do not edit an active session file or open a second writer to deliver messages. Do not inject terminal keystrokes.

## Register every batch

Save an absolute artifact directory and a durable `session.jsonl` for each agent. Include the session path in its launch arguments.

Keep the launch manifest outside Git. Each entry must include `id` or `issue`, `name`, `pid`, and `artifacts`.

```json
[
  {
    "id": "task-one",
    "name": "Validate a bounded fix",
    "pid": 12345,
    "artifacts": "/absolute/path/to/task-one"
  }
]
```

Replace the example PID with the actual launched process. Never copy example identity values into a real registration.

```bash
# Register the real launch manifest against the actual parent session.
pi-subagent-monitor register --manifest "$BATCH_DIR/launches.json" --parent-session "$PI_SESSION_FILE"
# Check the registered batch without sending a desktop notification.
pi-subagent-monitor poll --no-notify
```

Verify the task count and parent-session path. Check process states before telling the user that monitoring is active.

If registration fails, report the failure immediately. Do not leave an unmonitored background batch without disclosure.

## Give each agent a reporting contract

Require `progress.md` during work and `result.md` before stopping. Keep reports inside the task artifact directory.

Require structured status updates through the monitor command:

```bash
# Report a real blocker without granting new authority.
pi-subagent-monitor report --artifacts "$TASK_DIR" --state blocked --summary "Required validation environment is unavailable."
# Report a draft PR separately from task completion.
pi-subagent-monitor report --artifacts "$TASK_DIR" --state pr_opened --summary "The draft PR is open; checks are pending." --pr-url "$PR_URL"
# Report completion only after recording the actual validation and CI outcomes.
pi-subagent-monitor report --artifacts "$TASK_DIR" --state complete --summary "Required validation passed." --validation passed --ci passed --evidence "$TASK_DIR/validation.md"
```

Use `--ci not_applicable` only when CI genuinely doesn't apply. Record the reason in the evidence file.

Use `needs_review` when investigation finds no justified patch or requires a human decision.

Before resuming an agent, report `running` to replace any old blocked or terminal report. Preserve the existing task session and lease.

For the legacy publication supervisor, the watcher also reads `publication-continuation.json`. Register other supervisor layouts as separate tracked agents.

## Interpret alerts conservatively

- `running`: the recorded process identity matches.
- `stalled`: no recorded activity for 30 minutes. This isn't proof that the process is stuck.
- `continuation_pending`: a supervisor is waiting to resume the task.
- `blocked`: an agent or parent reported a blocker.
- `stopped`: no tracked agent remains. Process exit isn't proof of success.
- `pr_opened`: the agent reported a PR. CI can still be pending.
- `complete_reported`: the agent supplied a completion report and evidence path. The parent must verify the evidence.
- `needs_review`: a human decision or investigation review is required.
- `monitor_error`: monitoring couldn't classify the task safely.

On an alert, inspect the task evidence and report the next action. Never equate a successful process exit with successful delivery.

Monitoring doesn't authorize agent restarts, Codespace deletion, pushes, PR creation, readiness changes, merges, or deployments.

Do not broaden an agent's authority because the watcher found a blocker. Preserve the original approval and quality gates.

## Inspect and recover

```bash
# Read the last durable status snapshot.
pi-subagent-monitor status
# Check the watcher and its last exit result.
launchctl print "gui/$(id -u)/com.zkoppert.pi-subagent-monitor"
```

State lives in `~/.local/state/pi-subagent-monitor`. Logs live in `~/Library/Logs/pi-subagent-monitor`.

The watcher retries desktop notification failures. Session inbox files persist while Pi is closed and across watcher restarts.

Identical observations don't create new alerts. Delivery can repeat after a crash between sending a message and recording delivery.

No network service, paid model call, or Pi cron job runs during the periodic check. Parent-session responses can use the configured model.
