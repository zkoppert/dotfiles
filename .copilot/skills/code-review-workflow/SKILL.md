---
name: code-review-workflow
description: Use before any plan review, code review, self-review, security review, pull request description review, multi-model review panel, recurring report, retrospective, handoff, or performance feedback.
---

# Code review workflow

Read and follow this skill before starting a review. Pass applicable requirements to review agents because they might not load this skill themselves.

## Code Review Workflow
When asked to review a PR (or conduct a self-review), follow this workflow automatically:

### Multi-model review
- Launch **at least 3 code review agents in parallel** using different models (e.g., Claude Opus, Claude Sonnet, GPT) to get diverse perspectives
- **Never use Gemini models** for plan reviews, code reviews, PR-description reviews, or any other multi-model panel. Choose Claude, GPT, or another available non-Gemini model instead.
- Keep all three reviewers and give at least one an explicit adversarial focus: challenge assumptions, trace failure paths, and try to disprove the proposed behavior. Use a model outside the host model's lineage when one is available.
- For coordinated changes across multiple PRs or repositories, retrieve and review the actual sibling diffs. Check the seams between them only when sibling changes exist; never rely on descriptions as a substitute for code.
- Include a simplification pass that asks what code, configuration, or abstraction can be removed without losing required behavior.
- Synthesize findings across all models. Surface a finding once you independently verify it, even if only one model raised it; multiple models flagging an issue is a strong signal but still requires verification before reporting. Do not downgrade or drop a verified finding solely because it is pre-existing or sits outside the diff; label it instead (see "What to focus on")
- Present a unified, deduplicated report using the **Output format** below: lead with a merge decision, group findings by blocking impact, and name what looks right

#### Convergence loop (code gate only)
Run the **code diff** gate as a convergence loop, not a single pass. Follow-up observations do not reset convergence:
- After addressing a batch of verified blocking or fix-before-merge findings, re-run the full multi-model panel on the updated diff.
- A clean round has no new verified **Blocking** or **fix-before-merge** findings. A verified follow-up can remain when its current value does not justify added scope.
- Every initial review and re-review must use newly launched, session-free reviewers. Do not resume a reviewer's prior conversation, reuse a fixing agent as a reviewer, or let a reviewer certify code written to satisfy its own recommendation. Give the new panel the updated diff and relevant requirements, but treat prior findings, fix summaries, and tests from the fix turn as claims that require independent verification.
- **Cap at 5 rounds.** Stop when a round is clean. If the fifth round still surfaces a verified blocking finding, fix it, say the cap was reached, and let me decide whether to continue.
- Record the final clean round with `pr-marker write code-review --models a,b,c --convergence-rounds N -`. Both `pr-marker` and `gh-guard` reject a code-review marker without valid final-clean-round metadata.
- The **plan** and **PR-description** gates stay single-pass unless I ask otherwise.

### Verification standard
- **Every finding must be verified before reporting it.** Do not report potential issues based on assumptions alone.
- Verify by reading the actual source files, checking call sites, tracing data flow, or running tests/experiments
- Clearly label findings with verification status: **Verified** (confirmed by reading code or testing), **Observation** (plausible but depends on context outside the diff), or **Unverified** (could not confirm; include reasoning)
- When a finding involves runtime behavior, write or run a test to confirm it rather than speculating

### Review passes: scope, reuse, and reliability
Before drilling into line-level correctness, judge the shape of the change. These are first-class findings, not nitpicks.
- **Smallest safe change (MVP scope)** - ask what business problem this solves *now* and what the smallest safe version is. Flag scope that solves a larger imagined problem: speculative extension points, config knobs nothing uses yet, generalization added ahead of a second caller. Recommend what could be follow-up once the pattern proves itself.
- **Evidence-weighted edge cases** - do not turn every reachable corner case into required scope. Before requesting code or tests, assess reachability, observed frequency, failure impact, recovery options, and added implementation cost. Use available traffic, logs, incidents, support reports, contracts, or comparable production evidence. Record the source and measurement window when data exists. Verification proves that a case is real; it does not prove that fixing it now is valuable.
- **Edge-case disposition** - use these outcomes consistently:
  - **Blocking:** security, authorization, privacy, secret exposure, data corruption, destructive actions, irreversible migrations, external contract breaks, broad outages, or failures that defeat rollback. Severe impact overrides low frequency and implementation cost.
  - **Fix before merge:** a reachable case has concrete traffic, incident, support, or contract evidence, and the focused fix has proportionate complexity.
  - **Follow-up:** a reachable, recoverable case has low observed frequency, uncertain value, or a fix that adds meaningful branches, abstractions, fixtures, or maintenance.
  - **No comment:** the case is unreachable, speculative, or low-value, with no blocking safety or contract impact.
- **Missing evidence** - do not assume a recoverable edge case is common because production data is unavailable. Label the evidence gap. Use impact, reachability, contracts, reversibility, and detection speed to choose a disposition.
- **Reuse existing primitives** - before accepting a new helper, service, dependency, queue, table, or abstraction, check whether an existing helper, policy, schema, job, pattern, or owner already covers it. Duplicating or bypassing an existing primitive creates drift; name the primitive the change should reuse. Generalizes the library-migration redundancy check below.
- **Over-optimization and premature abstraction** - flag caching without evidence of a bottleneck, batching without need, complex config, or abstractions built for imagined future cases. Optimize only what a measurement shows is the bottleneck.
- **Reliability and operations** - for changes to production behavior, check retries, timeouts, consistency on retry/replay, migrations, partial-failure handling, deploy order, the rollback path, and whether the failure mode would surprise on-call. Tie this to the "Include monitoring context in code PRs" rule. Use "consistency", never `idempotency`.
- **Performance footprint** - judge the change's overall cost, not just its correctness: added database queries or N+1s, extra allocations or memory growth, larger response or client-bundle size, new synchronous or blocking calls, and added latency across the whole request, render, or job. For a change on a request path or inside a large loop, estimate the added cost (queries, calls, bytes) and say whether it warrants a measurement or a benchmark before shipping. This is the holistic companion to the narrower "New hot-path cost" check below and to "Design for 10x".
- **New hot-path cost** - when the diff adds per-request work on a hot path (a new feature-flag or config lookup, an extra query, a cache or network call, an N+1), confirm it reuses the batching, caching, or preloading the surrounding path already relies on. Such a regression is invisible to functional tests, so it has to be caught in review; for example, a feature-flag read added to a private application's controller action but not to that action's feature preloader costs a separate cache call every request. Name the batch or cache to reuse.

### What to focus on
- **Correctness over style** - only report bugs, logic errors, security issues, race conditions, type mismatches, and missing high-value or blocking edge cases. Do not flag subjective style, formatting, naming conventions, or personal preferences. This suppression does not extend to codified conventions documented in these instructions (such as the comment-minimalism rule below); enforce those as first-class findings, not nitpicks.
- **Comment minimalism (codified, not a nitpick)** - flag comments that restate what the code or the method/variable name already says, or that explain "what" instead of "why". This enforces the "Comments explain why, not what" rule from Code Style, so surface redundant or over-verbose comments for removal even though generic comment style is otherwise suppressed. Apply it when authoring too: strip redundant comments before committing.
- **Unreachable code and error-path analysis** - do a focused pass to verify that every code path is actually reachable, especially fallback/else branches. Check that commands which can exit non-zero (e.g., `grep` with no matches, failed pipes under `set -eo pipefail`, empty globs) don't silently abort before reaching intended fallback logic. Trace each branch, not just the happy path, to confirm it can actually execute.
- **Security-focused review** - do a dedicated pass specifically for security concerns. In particular, check for: script injection via unsanitized interpolation (e.g., `${{ }}` in GitHub Actions expanding attacker-controlled input before bash runs), command injection through variable expansion, secrets leaked into logs, and unsafe handling of user-controlled or PR-controlled data. Flag any path where external input flows into command execution without proper sanitization.
- **Check whether the author has addressed existing review feedback** - read through all review threads and comments before reporting. Note unresolved threads.
- **Check for unintended behavioral changes** - compare new code against the existing patterns in the same file or module
- **Removed safety nets** - when a refactor deletes a defensive guard (a `None`/empty check, validation, `try/except`, `if not connection:` fallback, or an explicit `raise`), do not assume the new library or path replaces it. Ask explicitly: what happens now on that failure case? Flag any removed guard whose failure mode is no longer handled, naming the old check and the new unguarded path.
- **Library-migration redundancy** - when a PR swaps libraries, check whether scaffolding the old library needed is now dead weight the new one provides natively: custom retry/backoff, timeout wrappers, session monkey-patching, manual JWT/token construction, pagination helpers. Flag custom code that duplicates a built-in default and recommend dropping it.
- **Redundant round-trips** - flag values decomposed and then immediately recomposed (e.g. `owner, name = x.split("/")` followed by `get_repo(f"{owner}/{name}")`) when the split can drop or mangle data the recompose assumes intact (`split("/")` vs `split("/", 1)`, lost trailing slashes). Skip it when the transform is purely cosmetic; report it as a low-priority simplification when collapsing removes a real failure surface.
- **Check docstring/comment accuracy** - verify that docstrings, comments, and commit messages accurately describe what the code actually does. Flag cases where stated behavior differs from implemented behavior. Also verify type hints and function signatures match how the function is actually called (e.g., a parameter typed `str` but always passed an `int`).
- **Test quality pass** - run the `test-quality` skill before synthesizing the code review. Map each accepted changed behavior to a specific test and observable assertion. Check boundaries and failures that the evidence-weighted edge-case rubric classifies as blocking or fix-before-merge. Flag tests that only assert mock calls, constructed arguments, non-null/type checks, or framework behavior instead of the owning abstraction's result. Coverage is a clue, not proof that these scenarios are specified.
- **Persistent test coverage gaps** - when the diff adds accepted behavior, a contract, or a blocking safety path, check whether a committed regression test exists. Do not require code or tests for speculative behavior that the evidence-weighted rubric classifies as follow-up or no comment. Preserve existing feature-flag-off and failure-path regression tests during refactors unless the protected behavior was intentionally removed.
- **Dependency hygiene** - when a PR adds, removes, or swaps a library, verify the dependency manifests and lockfiles agree with actual imports. Flag dependencies left in `pyproject.toml`/`requirements.txt`/lockfile that are no longer imported anywhere, and a dependency someone proposes removing that is in fact still imported (name the file and line). Surface the "is this still needed?" question with the answer, not just silence.
- **Surface high-value pre-existing issues** - tight scoping to the diff is the default, but do not silently drop a verified latent bug, dead code path, or comment/code mismatch you find in a file the PR touches just because the PR did not introduce it. Report it labeled **pre-existing**, with a recommendation to fix-while-here or file a follow-up issue (see "Capture side quests as issues").
- **Idiomatic language check (self-review only)** - when conducting a self-review, do a pass for language idiom violations that would signal unfamiliarity to a reviewer. Examples: `filter` vs `select` in Ruby, `list()` vs list comprehension in Python, `.forEach` vs `.map` in JS when the return value matters. This is not about style nitpicking; it's about using the language the way its community expects. Only flag during self-review, not when reviewing others' PRs.
- **React composition check** - when reviewing React code, flag components that build a large derived data shape and then walk it with conditionals. The canonical smell: a `.map()` containing a multi-branch `if`/`switch` with type casts handling 3 or more variants inline. Suggest extraction into per-variant components; this improves testability (each component is independently unit-testable) and readability (the container reads like a table of contents). When the extracted components also co-locate their data subscriptions via hooks, siblings avoid unnecessary re-renders.

### Visible reasoning bar
A diff can look finished before the author has shown why it is safe. This matters most for agent-assisted changes. After reading the diff and surrounding code, check whether the PR (body, comments, tests) makes enough reasoning visible to review without reconstructing the author's thinking:
- Why does this change exist now?
- Why this approach instead of the obvious alternatives?
- What validation is trusted, and what did it prove?
- What is least certain or intentionally left as follow-up?

Missing reasoning is not automatically blocking; it becomes blocking when a reviewer cannot judge correctness, safety, rollback, or scope without guessing. Ask for the smallest useful explanation, not a transcript dump: prefer "I can't tell whether this handles the empty-result case because the PR only says tests pass; add the case you validated or a focused test" over "add more context".

### Comment quality bar
Only raise a comment that clears at least one of these bars:
- The change can ship wrong behavior, break an important path, or make rollback hard.
- It introduces a credible security, privacy, or data-integrity risk.
- It duplicates or bypasses an existing primitive in a way that creates drift.
- It adds scope, abstraction, dependency, or optimization the business problem doesn't need.
- It makes future maintenance materially harder without a clear tradeoff.
- The tests miss a blocking or fix-before-merge case tied to accepted behavior.

For an edge-case comment, include the disposition, evidence source and window, expected impact, and complexity tradeoff. Do not request code when the evidence supports follow-up or no comment.

Do not leave bare "consider..." comments: if the issue is real, say what can fail and what simpler or safer shape fixes it. Reinforces "one point per comment" below.

### Tone and voice
- All review feedback must match the tone and voice described in the **Writing Style** section of these instructions
- Use additive, curious framing, not corrective or prescriptive
- For **first-time contributors**, lead with what was done well, be warm and specific about how to fix issues, and provide step-by-step guidance rather than terse criticism
- For established contributors or teammates, be concise and direct

### Drafting comments
- If findings warrant PR comments, draft them in my voice and **show me the draft before posting**
- When specific code changes are needed, use GitHub suggestion blocks
- One actionable point per comment; do not bundle multiple concerns

### Output format
Lead with a merge decision, group findings by blocking impact, and name what's right. Keep each finding's evidence tag (Verified / Observation / Unverified from the Verification standard) so the format adds a verdict without dropping the evidence bar. Decide each concern's disposition first: blocking issues go under **Blocking**; fix-before-merge and follow-up items go under **Non-blocking** (name which it is); concerns you decide to ignore don't get a comment at all.

```markdown
**Decision:** Needs changes before merge.  (or: Approve, or: Approve with follow-ups)

**Blocking**
1. `path/file.py:123` [Verified] - Authorization runs after the lookup, so a 404-vs-403 split leaks which private IDs exist. Move the permission check before the lookup, or reuse `ExistingPolicy.check`.

**Non-blocking**
1. `path/other.py:45` [Observation] - This helper duplicates `normalize_account_id`; reusing it reduces drift, but it can be follow-up if the behavior is intentionally different.

**What looks right**
The change keeps the data model unchanged and avoids a worker for a synchronous path, which is a reasonable MVP scope.
```

For a small review, a short paragraph with one or two findings beats a padded template. When composing from the code-review or security-review agents, re-rank findings by real impact and drop low-signal items; don't forward raw tool output.

### Durable context loop
When a review surfaces a repeated miss or hidden knowledge a future reviewer would have to rediscover, recommend the smallest durable artifact that prevents the same miss: a focused test, a short instruction in the repo's `.github/copilot-instructions.md` or `AGENTS.md`, adoption of an existing helper, a short decision note, or a lint/CI check when the failure is mechanical and likely to repeat (matches "Guardrails over guidelines"). Don't turn every finding into process; only do this when the miss is likely to recur. If it's a convention worth remembering across sessions, prompt me to store it as a memory.

### Evidence synthesis
For recurring reports, retrospectives, handoffs, and performance feedback:
- Collect source evidence before interpreting it, then extract themes before reading editorial configuration that could bias the synthesis.
- Tag claims with provenance and flag any conclusion supported by only one source.
- Pause at an evidence gate before drafting high-stakes or sensitive output. Confirm that the evidence is sufficient, current, and relevant.
- Structure findings as what happened, why it matters, and the impact.
- Resolve uncertainty when the source can answer it. Disclose the few remaining low-confidence claims directly without weakening ownership of the output.
- Preserve progress, source references, and recovery state so an interrupted session can resume without reconstructing the evidence set.

<!-- This boundary keeps the migrated baseline block byte-verifiable. -->
