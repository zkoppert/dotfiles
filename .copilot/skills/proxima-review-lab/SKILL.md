---
name: proxima-review-lab
description: Deploy, update, inspect, troubleshoot, and demo github/github branches in Proxima New Review Lab. Use for requests involving `proxima-review-lab`, `proxima-me`, `test-cnc-01`, Proxima test deployments, Heaven duplicate-deployment warnings, or recording a demo in a Proxima lab. Prevents duplicate ChatOps commands and distinguishes a sent command from a failed reply listener.
---

# Proxima Review Lab

Use New Review Lab (NRL) to validate unmerged `github/github` changes on the synthetic Proxima test stamp.

## Environment

- Deployment target: `proxima-review-lab` or the personal alias `proxima-me`
- Stamp: `test-cnc-01`
- Tenant: `proxima-review-lab-testing`
- ChatOps channel: `#dotcom-environment-ops`
- Personal lab URL: `https://proxima-review-lab-<user>.octoca.ts.net`
- Lifetime: approximately 24 hours

Do not confuse NRL with Staffship. Staffship is a production-class deployment stage. Use NRL for unmerged branch validation.

## Initial deployment

1. Confirm the PR head SHA and that required deployment artifacts are available.
2. Choose one lab name. Prefer `proxima-me` for a personal monolith-only lab.
3. Send exactly one command in `#dotcom-environment-ops`:

   ```text
   .deploy https://github.com/github/github/pull/<number> to proxima-me
   ```

   A branch name is also valid:

   ```text
   .deploy github/<branch> to proxima-me
   ```

4. Save the Slack message permalink and the Heaven or DevPortal deployment URL.
5. Wait for Heaven to report a terminal state: `done` or `failed`.
6. Confirm the completed message names the expected commit SHA before testing.

## Duplicate-deployment prevention

Treat command submission and reply monitoring as separate operations.

- A returned Slack permalink proves that ChatOps accepted the message.
- A listener error such as `EOF`, timeout, or lost WebSocket means monitoring failed. It does not mean submission failed.
- After a permalink is returned, never submit the command again without checking the original message and DevPortal first.
- Heaven serializes deployments for `github/proxima-review-lab`. It rejects overlapping attempts with “Another deployment attempt ... is currently being processed.”
- The monolith NRL target has `auto_deploy: true`. Once the lab is locked to a branch, a later branch push can deploy automatically.
- Do not send a manual `.deploy` immediately after pushing a new commit to an existing locked lab. Watch for the automatic deployment first.
- Do not submit both a PR deploy and a branch deploy for the same revision.
- Do not treat a delayed bot reply as permission to retry.

Use this decision sequence after a push:

1. Read the original Slack deployment message.
2. Check DevPortal for an active deployment.
3. Check whether Heaven has started an automatic update for the new SHA.
4. If a deployment is active, wait.
5. If it completed on the expected SHA, test it.
6. If it failed, inspect the failure before retrying once.
7. Only send another `.deploy` when no active attempt exists and the lab is not already on the expected SHA.

For managed multi-application labs, use the documented command:

```text
.proxima-review-lab update <lab-name>
```

Do not use that command for a normal monolith-only `proxima-me` lab.

## Parallel work

Prefer one active deployment for an application. Use a different explicit lab name only when isolated parallel testing is necessary. A different lab name does not guarantee that application-level deployment processing can run concurrently.

## Feature flags

1. Deploy the branch before changing targeting.
2. In DevPortal, select stamp `test-cnc-01` and tenant `proxima-review-lab-testing`.
3. Target an actor from that tenant, not a dotcom production actor.
4. Validate the flag-off state first.
5. Enable the flag for the lab actor and wait for propagation.
6. Remove URL or cookie overrides before final evidence so the demo proves DevPortal targeting.
7. Remove the test actor after validation when continued access is unnecessary.

## Demo workflow

Load and follow the `record-demo` skill after the expected SHA is deployed.

1. Authenticate through the lab enterprise SSO flow.
2. Record the lab URL, stamp, tenant, branch, commit SHA, actor, and feature targeting.
3. Capture matching before and after screenshots at the same viewport.
4. Record the complete user flow, not only the final static state.
5. Check browser console errors, failed functional requests, and broken visible images.
6. Inspect every screenshot and the final video before publishing.
7. Record known environment-only warnings separately from product failures.
8. Update the PR description with the Proxima scope, rollout, rollback, and demo evidence.

## Troubleshooting

### Duplicate deployment warning

Do not retry. Find the active attempt and wait for its terminal state. Verify its SHA before deciding whether another deployment is necessary.

### No bot response

If the send command returned a permalink, read that message directly. Check DevPortal. Assume the command was submitted until both sources prove otherwise.

### Site unavailable after a successful deploy

Check Tailscale routing and DNS first. The expected domain is `proxima-review-lab-<lab-name>.octoca.ts.net`.

### Feature remains hidden

Confirm the deployed SHA, then verify the DevPortal stamp, tenant, and actor. Query-string overrides do not prove that the production targeting rule is correct.

### Deployment failure

Inspect the Heaven logs. Retry once only after the attempt reaches `failed`. If the second attempt fails for the environment, ask in `#new-review-lab` or `#deploy-support`.

## Safety rules

- Never deploy an unmerged branch to Staffship as a substitute for NRL.
- Never use force or skip-check options for routine validation.
- Never claim validation against a SHA that Heaven did not report as deployed.
- Never record a misleading demo when the target feature is unavailable or the page is unhealthy.
- Never issue a duplicate command because the local reply listener failed.

## Tools

| Tool | Purpose |
| --- | --- |
| `gh-axi pr checks` | Confirm CI and deployment artifacts |
| `gh slack send` | Submit one ChatOps deployment command |
| `gh slack read` | Read the original command and Heaven replies |
| DevPortal | Inspect deployment state and target feature flags |
| Playwright | Validate and record the deployed user flow |
| `pr-marker` | Record exact test, review, and demo evidence |
