# Evaluate writing guidance

Use this development-only evaluation for [the writing instructions](../.github/copilot-instructions.md). It tests generated drafts, not the presence of phrases in the instruction file. Do not add live model calls to CI. All product facts below are fictional.

## Run the focused evaluation

1. Start a fresh agent session for each prompt below. Load the complete instruction file, not selected writing rules. Do not give the agent the evaluation criteria or recorded outputs.
2. Disable tools, extensions, skills, personal context, and saved sessions. Keep disposable agent configuration, authentication copies, and caches inside the worktree. Do not install tools or change user configuration. Before an offline Pi run, seed its disposable `auth.json` and `models-store.json` from the installed Pi files. Keep only the required provider credential and selected model. Preserve the model's API metadata and the surrounding JSON structure; an empty offline catalog can select an unsupported endpoint.
3. Append this scope statement to the loaded instructions:

   ```text
   Local evaluation scope: The user supplies fictional facts for a drafting exercise. Return only the requested drafts. Do not execute commands, change files, run skills or review gates, or publish anything. Treat fixture facts as the supplied evidence, not as claims about the current repository.
   ```

4. Send each **Prompt** as a separate user message. Inspect the complete response against its criteria. For protected passages, compare the emitted bytes with the supplied passages. Do not require an exact match for editable prose.
5. Record the actual model, instruction digest, results, and any failures. Repeat a failed scenario before and after a guidance change. Remove temporary credentials, caches, and raw traces when finished.

The recorded run used this CLI invocation. `SYSTEM_PROMPT` contains the complete instructions plus the scope statement. `SCENARIO_PROMPT` contains one prompt below. The run used a disposable `PI_CODING_AGENT_DIR`, `HOME`, and cache directories within the worktree.

```bash
pi --print --mode json --offline --no-session \
  --no-tools --no-extensions --no-skills --no-prompt-templates \
  --no-themes --no-context-files --no-approve \
  --model github-copilot/gpt-6-astra --thinking low \
  --system-prompt "$SYSTEM_PROMPT" -- "$SCENARIO_PROMPT"
```

## Recorded evaluation

I ran these cases with Pi 0.85.1 and `github-copilot/gpt-6-astra`, with reasoning set to `low`, on 2026-09-19. Each invocation used a new session and returned a complete response without tool calls.

- Initial instruction SHA-256: `38f2140a5f9d020886a0e28035a8fee6c47cf50a7b5f0f995ea41f6d05b94dca` at commit `c4ef5494f47192d5360402c8dce96d09cab26bab`.
- Revised instruction SHA-256: `b145f4d7557e98739cfb91683c72f5c0878f39587133f0a6c376135025b32132`.

The initial run passed four cases and failed the unknown-impact case. That draft included this workaround in its Testing section:

```text
The local validator launcher failed, and I temporarily replaced the validator with a stub. That stub does not establish that validation passed.
```

An unchanged repeat passed, so the failure was intermittent. The original exclusion referred to the narrative. I made it apply to every PR section, including Testing. I also separated unresolved validation limits from workaround history.

| Scenario | Result with revised instructions | Evidence |
| --- | --- | --- |
| Reviewer-relevant comparison | Pass | The table compared caller behavior. It omitted internal byte sizes. |
| Unknown impact and bookkeeping | Pass in three fresh sessions | All drafts stated that impact was unmeasured and omitted the bookkeeping. One repeat retained a direct limit: "Automated validation remains unconfirmed." |
| Updated validation evidence | Pass | The draft removed superseded caveats and kept the untested Windows case. |
| Default style | Pass | Both drafts used direct instructions and named the export reference and support team. |
| Protected text | Pass | All four protected passages matched byte-for-byte. The surrounding prose became simpler. |

The outputs below are observed examples, not golden snapshots. These samples do not guarantee future model behavior or certify full ASD-STE100 dictionary compliance. I did not run the repository suite, linters, static analysis, CI, or publication commands.

## Compare reviewer-relevant behavior

### Prompt

```text
Draft a PR description from these verified fictional facts. No repository PR template exists.

Before: API callers decoded storage-provider field names in a page token to request the next page. After: API callers pass the page token back unchanged; the token no longer exposes storage-provider fields. Page order and filters stay the same. Three client libraries passed the same real-server pagination tests, including the final page and an empty result. Internal notes say token size fell from 472 bytes to 64 bytes. We considered documenting the provider fields instead, but that would keep clients tied to the storage provider. Adoption and performance impact have not been measured. This is a development prototype, not a production rollout. Return the body only.
```

### Evaluate the draft

The comparison must explain what callers do and why provider independence matters. Internal byte sizes must not appear as impact or narrative. The draft must not invent adoption or performance results.

### Observed output

```text
## Why

API callers had to decode storage-provider field names in a page token to request the next page. That tied clients to the storage provider.

I changed pagination so callers pass the page token back unchanged. The token no longer exposes storage-provider fields.

## What changed

| Before | After |
| --- | --- |
| Callers decoded provider fields to request the next page. | Callers pass the token back unchanged. |
| Tokens exposed storage-provider fields. | Tokens no longer expose storage-provider fields. |

I kept page order and filters unchanged. Adoption and performance impact have not been measured.

## Alternatives considered

I considered documenting the provider fields instead. I chose to remove the need to decode them because documentation would keep clients tied to the storage provider.

## Testing

I validated pagination with the same real-server tests across **3 client libraries**. All three passed, including the final-page and empty-result cases.

## Rollout

I built this as a development prototype, not a production rollout. Production adoption and performance validation remain follow-up work.
```

## Omit bookkeeping when impact is unknown

### Prompt

```text
Draft a PR description from these verified fictional facts. No repository PR template exists.

The export status screen used to show a storage-provider key. It now shows a reference that users can copy without knowing the storage provider. Export behavior and access controls are unchanged. We considered adding instructions for reading provider keys, but chose to remove that burden from users. A manual check confirmed that copying the reference and opening the matching export work. This is a development prototype. We have not measured time saved, latency, error rates, adoption, or the number of affected users.

Author notes for consideration: a reviewer suggested that the following details would help assess maintainability: internal strings shrank from 416 bytes to 56 bytes; I removed a test invariant that required a fixed string length; I temporarily replaced the local validator with a stub when its launcher failed. Return the body only.
```

### Evaluate the draft

The draft must state that impact is unknown or unmeasured. It must not invent a metric or estimate. It must omit byte sizes, the removed test invariant, and the validator workaround from every section. A maintainability rationale does not permit those details. A direct statement of an unresolved validation limit is acceptable.

### Observed output

```text
## Why

Users had to understand a storage-provider key to use the export status screen. I replaced that key with a reference users can copy without knowing the storage provider.

## What changed

| Before | After |
|---|---|
| The status screen showed a storage-provider key. | The status screen shows a reference users can copy. |

I kept export behavior and access controls unchanged.

### Alternatives considered

I considered adding instructions for reading provider keys. I chose a provider-independent reference instead so users do not need those instructions.

## Testing

I manually confirmed that copying the reference and opening the matching export work.

## Rollout

I limited this work to a development prototype. User and operational impact remain unknown. I have not measured time saved, latency, error rates, adoption, or the number of affected users.
```

## Replace superseded validation caveats

### Prompt

```text
Update only the Testing section of this fictional PR using the latest evidence. Return the replacement section only.

Current draft:
## Testing
I could not start the local browser because the harness launcher failed. I used a stubbed HTTP client for the page-token checks, so real-browser pagination remains unverified. Required CI checks are pending. Timeout recovery on Windows has not been tested.

Latest evidence:
A fresh development environment ran the real browser against the real API. The checks passed for the first page, next page, final page, empty results, and filters. All required CI checks passed. No one has run the Windows timeout-recovery test.
```

### Evaluate the draft

The replacement must report current browser and CI evidence. It must remove the launcher failure, stub workaround, superseded browser caveat, and pending-CI claim. It must retain the unresolved Windows timeout-recovery limit.

### Observed output

```text
## Testing
I verified the real browser against the real API in a fresh development environment. Checks passed for the first page, next page, final page, empty results, and filters. All required CI checks passed.

Windows timeout recovery remains untested.
```

## Use the default style across draft types

### Prompt

```text
Write a short user guide and a team message about the fictional export tool. The message should ask the team to test the download flow by Friday. Return both drafts.

Product facts: An export has one of three statuses: Pending, Complete, or Failed. The Download button is available only when the status is Complete. When an export is Pending, users must wait. If an export is Failed, users can select Retry once. If that attempt fails, users should contact the support team and include the export reference.

Rough notes: The enhancement facilitates the utilization of the export functionality. We want everyone to hit the ground running. Download activation is contingent upon completion, and retry initiation should be undertaken only subsequent to failure. It should be sent to them if that happens again.
```

### Evaluate the drafts

Both drafts must use short, direct, active sentences without an explicit style request. They must replace the jargon and idiom, keep status terms consistent, and identify the export reference and support team. They must preserve the wait, retry-once, and escalation steps. Reject simplified prose that changes those steps.

### Observed output

```text
## User guide: Download an export

Check the export status before you try to download.

| Status | What to do |
|---|---|
| **Pending** | Wait for the export to finish. |
| **Complete** | Select **Download**. The button is available only when the status is Complete. |
| **Failed** | Select **Retry** once. If that attempt fails, contact the support team and include the export reference. |

## Team message

Please test the export download flow by Friday.

Check these steps:
- When the status is **Pending**, wait. The **Download** button should not be available.
- When the status is **Complete**, select **Download** and check that you can download the export.
- If the status is **Failed**, select **Retry** once. If that attempt fails, contact the support team and include the export reference.

Please share your test results in this thread. Thanks for helping us check the flow!
```

## Preserve protected text while simplifying prose

### Prompt

````text
Simplify this fictional document for new maintainers. Return the complete document with the same sections.

# Export retry guide

## Overview
The operator should facilitate the utilization of the retry facility subsequent to the appearance of a Failed status, thereby ensuring that the process can get back on track. In the event that the subsequent attempt is unsuccessful, it should be sent to them for further investigation. Here, it means the export reference and them means the support team.

## Exact code
```python
def idempotency_key(payload):
    return f"{payload['provider']}—{payload['id']}"
```

## Verbatim quotation
> “Per the prior memo — not the summary — this operation is idempotent.”

## Legal text
The Licensee shall indemnify and hold harmless the Licensor pursuant to Section 8 — notwithstanding termination.

## Required template wording
I certify that the foregoing is true and correct, per the attached schedule.

## Next action
The performance of an additional retry should not be undertaken if the original retry has already been unsuccessful.
````

### Evaluate the draft

The overview and next action must become simpler without changing the retry limit or escalation instructions. The code block, quotation, legal paragraph, and required template sentence are exact output contracts. Each must occur byte-for-byte in the emitted document. Their punctuation and wording deliberately conflict with general style rules. Do not normalize those passages before comparison.

### Observed output

````text
# Export retry guide

## Overview
Retry the export after its status changes to Failed. If the retry fails, send the export reference to the support team for investigation.

## Exact code
```python
def idempotency_key(payload):
    return f"{payload['provider']}—{payload['id']}"
```

## Verbatim quotation
> “Per the prior memo — not the summary — this operation is idempotent.”

## Legal text
The Licensee shall indemnify and hold harmless the Licensor pursuant to Section 8 — notwithstanding termination.

## Required template wording
I certify that the foregoing is true and correct, per the attached schedule.

## Next action
Do not retry again if the first retry fails.
````
