---
name: validate-style
description: Apply ASD-STE100 and hard writing rules before external text is posted. Also use for linting, draft validation, or ASD-STE100 requests.
---

# Validate Style: apply ASD-STE100 and enforce hard rules

Use this skill **before** finalizing any text that will be posted externally on Zack's behalf. Apply ASD-STE100 principles to editable prose, then run the linter for deterministic rules. Apply the preservation requirement in `~/.copilot/copilot-instructions.md` when protected text conflicts with a rule. Do not claim formal ASD-STE100 compliance without the applicable specification and an approved dictionary.

## ASD-STE100 review

Use these rules for conversations and authored prose:

- Use short, active sentences.
- Put one instruction in each sentence.
- Keep instructions at 20 words or fewer.
- Keep descriptive sentences at 25 words or fewer.
- Use one term for each concept.
- Define an abbreviation at its first use.
- Prefer common words over jargon.
- Use `I'll` instead of `I will` in natural prose.
- Describe an empty quantity with `no`, `none`, `nothing`, or `not any`.
- Combine repeated sentence openings when one natural sentence is clearer.

Technical accuracy takes priority. Preserve exact quotations, code, commands, identifiers, legal text, and required repository-template text.

The linter checks sentence length and other measurable patterns. After it passes, review the draft manually for active voice and undefined abbreviations. Also check terminology, jargon, complex instructions, and repetitive phrasing that a regular expression cannot judge safely.

## What this skill catches

The full list of rules and their descriptions are defined as constants in `lint.py`. To inspect them directly, run:

```bash
python3 ~/.copilot/skills/validate-style/lint.py --help
grep -A1 '"no-' ~/.copilot/skills/validate-style/lint.py | head -40
```

At a high level, the rules cover:

- **no-em-dash** - the em-dash character
- **no-spaced-dash** - a hyphen or en-dash used as sentence punctuation (spaced, e.g., `drift - they came in`) instead of joining words ("runner-up")
- **no-per-as-according-to** - using the word "per" to mean "according to"
- **no-prayer-hands** - the folded-hands emoji for thanks or please
- **no-click-here** - non-descriptive Markdown link text
- **no-isp-incident** - prefixing the word "incident" with extra letters
- **`no-idempotent`** - using `idempotent` or `idempotency` instead of "consistent" or "consistency" (URLs are skipped)
- **no-agentic-passive** - using a model name as the subject of verbs like made, wrote, generated
- **no-this-pr-subject** - using "This PR / This change / This commit" as a sentence subject instead of first person
- **no-subjectless-action-bullet** - bullets that lead with a bare past-tense action verb ("Added X") instead of first person ("I added X")
- **use-ill-contraction** - using `I will` instead of the natural contraction `I'll`
- **no-zero-quantity** - using "zero" or "0" for an empty count instead of "no", "none", "nothing", or "not any"
- **no-repetitive-ill-openings** - starting three or more consecutive sentences with "I'll"
- **ste-sentence-length** - exceeding 20 words in a checkbox instruction or 25 words in a descriptive sentence
- **no-private-repo-ref** (requires `--check-visibility`) - referencing a private or internal GitHub repo in text destined for a public surface. Checks visibility via the `gh` CLI at lint time.

When the linter flags a violation, it prints the exact rule name, file, line, and column. Use that to look up the full message and suggested fix in `lint.py`.

## When to invoke

**Always** invoke the linter when you are about to:

- Open a PR or write a PR description
- Post a comment on an issue, PR, or discussion
- Send a Slack message on Zack's behalf
- Create or edit a gist
- Write an email, memo, or status update
- Finalize any text destined for an external surface

Skip the linter for purely internal artifacts (session plan files, scratch notes, internal command output) - but err on the side of running it. Even ~20 seconds of linter time is cheaper than a follow-up cleanup request.

## How to run

### Lint a single file

```bash
python3 ~/.copilot/skills/validate-style/lint.py path/to/draft.md
```

### Lint piped text

```bash
cat draft.md | python3 ~/.copilot/skills/validate-style/lint.py -
```

### Lint multiple files

```bash
python3 ~/.copilot/skills/validate-style/lint.py file1.md file2.md
```

### Get JSON output (for programmatic handling)

```bash
python3 ~/.copilot/skills/validate-style/lint.py --json path/to/draft.md
```

### Exit codes

- `0` - no violations
- `1` - one or more violations
- `2` - error reading or decoding a file

## How to handle violations

1. **Fix violations in editable prose before posting.** Apply the preservation requirement in `~/.copilot/copilot-instructions.md` under **Writing Style** when a finding affects protected text.
2. **If the linter has a false positive in editable prose**, rephrase rather than disable the rule.
3. **Re-run the linter** after fixing. Report any remaining protected-text findings for resolution. Do not change protected text or bypass a publication gate to make the check pass.

Do **not** silence the linter, comment it out, or skip it because the text "looks fine."

## What this skill does NOT catch

The linter only catches mechanical, regex-detectable rules. It does **not** check:

- The writing standard or protected-text requirement in **Writing Style**
- Tone (additive vs. corrective, warmth for first-time contributors)
- Voice (active vs. passive in general, first person vs. third)
- Whether two sentences with different wording still repeat the same idea
- Undefined abbreviations
- Inconsistent terminology
- Whether technical jargon is necessary
- Whether a sentence contains more than one instruction when it remains within the word limit
- Boastful framing or generic praise
- Internal repo/issue names leaking into public contexts
- @-mentions of people without their confirmation
- BLUF structure for asks vs. problem-first for PR descriptions
- The "verify before flagging" review principle

Those still require human or agent judgment. After running the linter, re-read the text through the lens of the full **Writing Style** and **Pull Requests** sections of `~/.copilot/copilot-instructions.md`.

## Source of the rules

These rules come from `~/.copilot/copilot-instructions.md` under "Writing Style > Hard Rules" and from explicit user feedback captured in Copilot Memory. When a new hard rule is added, update both the instructions and `lint.py` (plus a test case in `tests.py`).
