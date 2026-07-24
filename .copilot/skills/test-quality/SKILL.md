---
name: test-quality
description: This skill should be used whenever authoring, editing, or reviewing tests; changing a coverage threshold; or finalizing a production behavior change. It checks for incidental coverage, shallow assertions, missing boundaries, incorrect test ownership, mock-contract drift, coverage-only test artifacts, and source changes without persistent regression tests. Run it before the multi-model code review and before `pr-marker run-tests`.
---

# Test Quality: prove behavior, not line execution

## When to use this skill

Use this skill when:

- production behavior is added, removed, or changed;
- tests are authored, edited, or reviewed;
- a coverage threshold changes;
- a test-only PR is intended to close coverage gaps;
- the code-review or tests marker is about to be written.

Coverage reports show which code ran. They do not prove that tests asserted the right outcome.

## Run the deterministic preflight

From the repository root:

```bash
python3 ~/.copilot/skills/test-quality/check.py
```

When base inference is ambiguous:

```bash
python3 ~/.copilot/skills/test-quality/check.py --base origin/main
```

For a legitimate executable-source change with no committed test change:

```bash
python3 ~/.copilot/skills/test-quality/check.py \
  --base origin/main \
  --no-test-change-reason "Generated client output is verified by the schema compatibility check."
```

The checker enforces a minimum evidence floor. A passing result does not prove that the tests are sufficient.

## Build a behavior-to-test matrix

List every changed behavior before judging the tests:

| Changed behavior | Test | Observable assertion | Boundary or failure case |
| --- | --- | --- | --- |
| Example: default sort | integration test name | returned IDs are in updated order | explicit sort remains unchanged |

The matrix must cover relevant cases:

- normal behavior;
- empty, nil, or malformed input;
- the exact threshold and threshold plus one;
- the final page, batch, or chunk;
- feature flag disabled and enabled;
- timeout, explicit error, partial success, and missing required fields;
- retry, replay, or repeated execution.

Do not invent irrelevant cases. Explain why an item does not apply.

## Apply the no-op test

For every important assertion, ask:

> Would this test fail if the changed behavior returned the old value, returned the wrong value, or became a no-op?

Strengthen any test that only proves:

- a method exists;
- a value is non-null or has the expected type;
- a mock was called;
- an argument or query object was constructed;
- no exception occurred.

Assert the observable output, state change, emitted command/event, or exact error instead.

For test-hardening or coverage-focused work, introduce at least one deliberate fault for each behavior cluster in a temporary worktree. Invert a condition, return the wrong literal, or remove the side effect, then confirm the relevant test fails. Restore the temporary worktree after the check.

## Verify mock contracts

Before inventing an API, CLI, or library response:

1. Read the official type or implementation, or inspect a real response.
2. Capture a minimal redacted fixture when the shape is not stable or obvious.
3. Prefer a purpose-built fake or fixture over a permissive nested mock.
4. Keep at least one test through the real parser or entry point.

Do not let mocked integration tests be the only evidence that command construction, serialization, or payload parsing works.

## Keep tests owned by behavior

- Put tests in the file that corresponds to the source module.
- Never create catch-all files such as `test_coverage_additions.py`, `coverage_test.rb`, `test_misc.py`, or `test_extra.py`.
- Name tests after scenarios and outcomes, not source lines or coverage branches.
- Preserve regression tests during refactors unless the protected behavior was intentionally removed.
- Test the owning abstraction, not a helper or framework behavior that happens to execute.

## Produce the code-review section

Include this section in the multi-model code-review synthesis:

```markdown
## Test quality

- **Behavior map:** <changed behaviors and matching tests>
- **Observable assertions:** <why the tests fail for wrong behavior>
- **Boundaries and failures:** <cases covered or why they do not apply>
- **Contract evidence:** <real payload/type/fixture or N/A>
- **No-op or deliberate-fault check:** <what was changed and which test failed>
- **Persistent coverage:** <committed regression tests or explicit justification>
- **Preflight:** <checker result and any warnings/waiver>
```

Warnings require review, not automatic suppression. Fix the test when the warning identifies a real weakness.
