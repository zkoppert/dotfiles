# memory-sweep

A local Copilot skill that finds Copilot CLI memory rules that should be
promoted into `copilot-instructions.md`.

Stored memories are great for capturing preferences mid-conversation,
but rules that the user wants applied consistently belong in the
instructions file: visible to reviewers, source-controlled, and resilient
to memory churn. This skill diffs the two and flags the gaps.

## Files

- `sweep.py` - the classifier. Reads a memories dump and instructions
  file, scores each memory by token and quoted-phrase overlap, prints
  findings.
- `tests.py` - unit tests. Run with `python3 tests.py`.
- `SKILL.md` - the agent-facing instructions Copilot loads when it
  discovers this skill.

## Quick start

The skill is designed to be invoked by the Copilot agent (which has
access to the user's memories in its prompt context), but you can also
run it manually:

1. Dump your memories to a markdown file in the format:

   ```markdown
   **subject heading**
   - Fact: <fact text>
   - Citations: <citations text>
   ```

2. Run the sweep:

   ```bash
   python3 ~/.copilot/skills/memory-sweep/sweep.py \
     /tmp/memories.md \
     ~/repos/dotfiles/.github/copilot-instructions.md
   ```

3. Read the report. Findings are sorted by score (lowest first), so
   promotion candidates appear at the top.

To filter to only one verdict:

```bash
python3 sweep.py /tmp/memories.md ~/repos/dotfiles/.github/copilot-instructions.md --only PROMOTE
```

## Verdicts

| Verdict   | Score         | Meaning                                                |
|-----------|---------------|--------------------------------------------------------|
| PROMOTE   | < 0.30        | Almost no overlap. Strong promotion candidate.         |
| AMBIGUOUS | 0.30 to <0.70 | Partial overlap. Eyeball to decide.                    |
| PRESENT   | >= 0.70       | Likely already in the file.                            |

## How the score works

For each memory fact, the script:

1. Extracts distinctive tokens (4+ char words, not in a stopword list).
2. Extracts quoted phrases (strings inside double quotes).
3. Counts how many of those tokens also appear as tokens in the
   instructions file (set intersection, not substring search, so
   `commit` does not match `committed`).
4. Counts how many quoted phrases appear verbatim in the instructions
   text.
5. Score = token-match-ratio + 0.3 * phrase-match-ratio, capped at 1.0.

It's a heuristic, not a semantic match. The goal is triage: shrink a
50-memory list into a 5-candidate review.

## Why no `--write` flag

The skill deliberately does **not** propose changes to the instructions
file or open PRs automatically. Promotion is a writing-style decision
that the human owns. The skill produces findings; the human decides
what to promote and how to phrase it.

## Tests

```bash
cd ~/.copilot/skills/memory-sweep
python3 tests.py
```
