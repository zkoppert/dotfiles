# Handle selected Dependabot updates

Notification-based discovery can miss dependency updates after someone clears the notification.
The scheduled runner scans the configured repositories directly.
Read [the operating guide](../../../docs/dependabot-automerge.md) for policy, security boundaries, alerts, and activation.

## Use the guarded runner

Use `~/.local/bin/triage-dependabot --dry-run` after installation.
For source-checkout validation, use `bin/triage-dependabot --dry-run`.
The private configuration must explicitly select organizations or exact repositories.
Missing, invalid, or empty scope stops processing.

The historical `triage_dependabot.py` module supplies shared parsing helpers and legacy regression tests.
Its command entrypoint refuses execution, so normal callers cannot bypass the repository-mode policy through the installed legacy command.

## Understand reviewer isolation

The reviewer uses fresh `COPILOT_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and `XDG_DATA_HOME` directories.
It doesn't pass `GH_TOKEN`, `GITHUB_TOKEN`, or parent tool-permission settings to the reviewer.
If configured, `COPILOT_GITHUB_TOKEN` remains available for Copilot authentication.
Standard proxy and certificate variables also remain available.
The wrapper uses `DEPENDABOT_WORKER_STARTED` to record startup and `DEPENDABOT_FAILURE_REPORTED` to confirm failure-report persistence.
`DEPENDABOT_FALLBACK_ALERT_SENT` records an emergency notification when report storage fails.
It removes all internal receipts when the invocation ends.
A standard-library helper can report startup failures before third-party Python packages become available.
The reviewer invokes `/opt/homebrew/bin/copilot` with a fixed system tool path, not the parent's executable search path.
All model tools are disabled, and untrusted dependency code never executes locally.

## Run the regression tests

From the source checkout:

```bash
gh cfs -- uv run --no-project --python /opt/homebrew/bin/python3 \
  --with PyYAML==6.0.3 --with ruamel.yaml==0.18.16 --with pytest -- \
  python -m pytest scripts/test_dependabot_automerge.py \
  scripts/test_dependency_review.py scripts/test_dependabot_alerts.py \
  scripts/test_dependabot_api.py scripts/test_dependabot_scope.py \
  test_triage_dependabot_wrapper.py .copilot/skills/triage-dependabot/tests.py
```

The operating guide also documents the opt-in real-CLI security test.
Run that test before installation or activation and after Copilot CLI upgrades.
The local release installer enforces it and records the validated CLI version.
