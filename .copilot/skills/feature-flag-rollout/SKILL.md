---
name: feature-flag-rollout
description: This skill should be used when shipping a user-facing or production behavior change, planning a feature-flag rollout, or working on logic in github/github (or any deployed service) that should ship dark and ramp gradually. It encodes GitHub's safe-deployment staged-rollout lifecycle (ramp percentages, per-stage monitoring, flag cleanup) and the no-flag gradual-deploy path. Also triggered when the user asks "how should I roll this out", "what are the rollout stages", "ramp this flag", "is this safe to deploy", or "did I clean up the flag".
---

# Feature Flag Rollout: ship dark, ramp gradually, then clean up

Source: GitHub internal `thehub` `dev-practicals/safe-deployment-practices/`. Feature flags
are the default mechanism for shipping user-facing behavior so a bad change can be disabled
without a redeploy. Deploy the code **disabled**, then roll out in production.

Use this skill to drive the rollout and to make sure the PR and the post-merge plan reflect
each stage. Pair it with the existing PR-description rules: write the "What to watch after
merge" section in first person and link the relevant Datadog/Splunk dashboard.

## The flag lifecycle (the happy path)

1. **Create the flag fully disabled.** New behavior is dark by default.
2. **Test locally with the flag both ON and OFF.** Confirm the old path still works.
3. **Deploy everywhere with the flag OFF.** The code is in production but inert.
4. **Enable for a limited audience first** (yourself / staff / a single org), verify in prod.
5. **Ramp gradually**, pausing to monitor at each stage. Typical ladder:

   | Stage | Audience |
   |-------|----------|
   | 1 | staff / 1 org |
   | 2 | 2% |
   | 3 | 10% |
   | 4 | 30% |
   | 5 | 50% |
   | 6 | 100% |

6. **At every stage, monitor** defects, error rate, latency/performance, and resource use
   before advancing. If a metric regresses, disable the flag (no redeploy needed) and
   investigate.
7. **Fully enable only after you're confident** at 100% for a meaningful bake period.
8. **Remove the conditional code** so only the new path remains.
9. **Delete the flag.** Rollout is not done until the flag and its dead branch are gone.

A rollout is incomplete if it stops at step 7. Steps 8 and 9 are part of the work; leaving
stale flags and dead branches behind is technical debt.

## When a change can't sit behind a flag

Use the no-flag safe-deployment path and still ramp gradually:

`review-lab → single cluster in one site → single site → all sites`

Prepare a rollback for **each** step before you take it: prefer a fast rollback
(e.g. heaven rollback) and/or have a revert PR pre-built and ready to merge.

## What to put in the PR / rollout issue

Drop this checklist into the PR's Rollout section (or a linked rollout tracking issue) and
keep it updated as stages complete:

```markdown
### Rollout
- [ ] Flag created, disabled by default
- [ ] Tested locally with flag ON and OFF
- [ ] Deployed everywhere with flag OFF
- [ ] Enabled for staff / single org, verified
- [ ] 2% → monitored (errors, latency, resources)
- [ ] 10% → monitored
- [ ] 30% → monitored
- [ ] 50% → monitored
- [ ] 100% → baked for <duration>
- [ ] Conditional code removed
- [ ] Flag deleted

**What to watch after merge:** I will watch <dashboard link> for <metric>; if <signal> regresses I will disable the flag and investigate.
**Rollback:** <how to disable / revert at each stage>
```

## Higher-risk changes

If the change touches a high-tier service or could cause data loss/corruption, treat it as a
production change: write it up (a production change record in github/github), schedule
ramp stages during low customer-impact windows, and actively watch dashboards during each
stage rather than ramping and walking away.
