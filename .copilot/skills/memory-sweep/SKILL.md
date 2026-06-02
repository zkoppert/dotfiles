---
name: memory-sweep
description: This skill should be used when the user asks to "audit my memories", "sweep memories", "find memory-only rules", "what memories should be promoted to instructions", "review my Copilot memories", or any similar request to identify rules that live only in stored Copilot memories but are not yet documented in their personal copilot-instructions.md. It dumps the current memories from the agent's prompt context to a tempfile and runs a Python classifier that scores each memory against the instructions file for keyword and phrase overlap. Output is a list of memories classified as PROMOTE (memory-only, candidate for promotion), AMBIGUOUS (partial overlap, worth eyeballing), or PRESENT (already covered).
---

# Memory Sweep - find memory-only rules that should be in copilot-instructions.md

## When to use this skill

Trigger this skill whenever the user asks any of:

- "audit my memories"
- "sweep my memories"
- "any memory-only rules?"
- "review my Copilot memories"
- "what memories should be promoted to instructions"
- "is anything in memory that should be in the file?"

Also proactively offer to run this skill if you notice during a session
that a user-stated rule lives only in memory and not in the instructions
file (e.g., when a recent multi-model PR review flags a "smuggled rule"
finding like the gratitude-first case).

## Why this matters

Memories are great for capturing preferences mid-conversation, but they
have downsides:

- They're personal to one user account and don't transfer to teammates.
- They can be down-voted into oblivion accidentally.
- They're invisible during code review or onboarding.
- They can drift out of sync with the canonical instructions file.

Rules that the user wants to apply consistently belong in
`copilot-instructions.md`, where they're visible, source-controlled,
and survive any memory churn. The memory remains useful as a quick
reference but the file is authoritative.

## How to run the sweep

Follow these steps **in order**:

### Step 1: Dump the current memories block

You have the user's stored memories in your prompt context inside a
`<memories>` block. Write that block verbatim to a tempfile in a
private per-invocation directory. Memories can contain personal
preferences and context you do not want on a predictable shared path.

```bash
SWEEP_DIR=$(mktemp -d -t memory-sweep.XXXXXX)
chmod 700 "$SWEEP_DIR"
```

Then use your file-write tool to create
`"$SWEEP_DIR/memories.md"` containing **only** the memory entries
from the `<memories>` block - one entry per memory, in this exact
markdown format:

```markdown
**subject heading**
- Fact: <fact text>
- Citations: <citations text>

**next subject**
- Fact: <fact text>
- Citations: <citations text>
```

Do **not** include the surrounding instructional prose ("Be sure to
consider these stored facts carefully...", "If you come across a
memory you can verify...", etc.). Only the memory entries themselves.

### Step 2: Run the classifier

```bash
python3 ~/.copilot/skills/memory-sweep/sweep.py \
  "$SWEEP_DIR/memories.md" \
  ~/repos/dotfiles/.github/copilot-instructions.md
```

To focus on only the promotion candidates:

```bash
python3 ~/.copilot/skills/memory-sweep/sweep.py \
  "$SWEEP_DIR/memories.md" \
  ~/repos/dotfiles/.github/copilot-instructions.md \
  --only PROMOTE
```

When the review is finished, clean up the temp directory:

```bash
rm -rf "$SWEEP_DIR"
```

### Step 3: Review the output with the user

The output sorts findings by score (lowest first), so the strongest
PROMOTE candidates appear at the top. For each candidate:

1. Read the fact text aloud (or summarize it).
2. Confirm with the user whether it's a rule they want documented in
   the instructions file.
3. If yes, ask which section of the file it belongs under (e.g.,
   Writing Style, Pull Requests, GitHub Actions).
4. If the user says "skip" or "leave in memory only", move on.

Do **not** silently file a PR for everything flagged as PROMOTE. The
classifier is a heuristic and the user gets the final say on what
becomes documented policy.

### Step 4: Promote agreed-upon rules

For each rule the user approves for promotion:

1. Draft the change to `~/repos/dotfiles/.github/copilot-instructions.md`.
   Match the surrounding section's tone and structure.
2. Self-lint with the validate-style skill before opening a PR.
3. Follow the user's PR workflow from their personal instructions:
   multi-model review, draft PR, sign-off, Co-authored-by Copilot trailer,
   full repo PR template.

## Verdict guide

| Verdict   | Score range  | What it means                                                                              |
|-----------|--------------|--------------------------------------------------------------------------------------------|
| PROMOTE   | < 0.30       | Almost no overlap with the file. Strong candidate for promotion.                           |
| AMBIGUOUS | 0.30 to <0.70| Partial overlap. Eyeball the matched/missing tokens to decide.                             |
| PRESENT   | >= 0.70      | Significant token overlap with the file. Likely already documented. Exact quoted-phrase matches add weight (up to +0.3) but cannot reach PRESENT on their own. |

PRESENT findings are not always perfect matches. If the user has
recently added a rule to the file and the memory was already there, the
two should agree. If they don't, that's a different problem (drift)
worth surfacing.

## Caveats

- This is a keyword and phrase classifier, not a semantic one. False
  positives and false negatives both happen. Treat output as a triage
  aid, not as gospel.
- The classifier only checks the instructions file passed as the second
  argument. Repo-level `.github/copilot-instructions.md` files are not
  considered.
- The `<memories>` block in your prompt context already excludes
  memories outside the current scope. Whatever you dump is the working
  set.
