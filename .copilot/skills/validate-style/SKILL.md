---
name: validate-style
description: This skill should be used BEFORE posting any text to GitHub (PR descriptions, issues, comments, discussions, gists), Slack, email, or any other external-facing surface, to catch hard-rule writing-style violations. Also triggered when the user asks to "lint this text", "check this for style violations", "validate this draft", or "run the writing style linter".
---

# Validate Style - enforce Zack's writing-style hard rules

Use this skill **before** finalizing any text that will be posted externally on Zack's behalf. The linter catches the rules that are easiest to forget under pressure and the ones Zack has explicitly flagged as hard rules in his Copilot instructions.

## What this skill catches

The full list of rules and their descriptions are defined as constants in `lint.py`. To inspect them directly, run:

```bash
python3 ~/.copilot/skills/validate-style/lint.py --help
grep -A1 '"no-' ~/.copilot/skills/validate-style/lint.py | head -40
```

At a high level, the rules cover:

- **no-em-dash** - the em-dash character
- **no-spaced-dash** - a hyphen or en-dash used as sentence punctuation (spaced, e.g., "drift - they came in") instead of joining words ("runner-up")
- **no-per-as-according-to** - using the word "per" to mean "according to"
- **no-prayer-hands** - the folded-hands emoji for thanks or please
- **no-click-here** - non-descriptive Markdown link text
- **no-isp-incident** - prefixing the word "incident" with extra letters
- **no-agentic-passive** - using a model name as the subject of verbs like made, wrote, generated
- **no-this-pr-subject** - using "This PR / This change / This commit" as a sentence subject instead of first person
- **no-subjectless-action-bullet** - bullets that lead with a bare past-tense action verb ("Added X") instead of first person ("I added X")
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

1. **Fix every violation before posting.** Hard rules are hard rules - do not ship text with violations.
2. **If the linter has a false positive**, rephrase rather than disable the rule. The rules are intentionally narrow to keep false positives low; if you trip one, the wording probably is unclear.
3. **Re-run the linter** after fixing to confirm the text is clean.

Do **not** silence the linter, comment it out, or skip it because the text "looks fine." If the linter flags something, treat it as a real bug.

## What this skill does NOT catch

The linter only catches mechanical, regex-detectable rules. It does **not** check:

- Tone (additive vs. corrective, warmth for first-time contributors)
- Voice (active vs. passive in general, first person vs. third)
- Boastful framing or generic praise
- Internal repo/issue names leaking into public contexts
- @-mentions of people without their confirmation
- BLUF structure for asks vs. problem-first for PR descriptions
- The "verify before flagging" review principle

Those still require human or agent judgment. After running the linter, re-read the text through the lens of the full **Writing Style** and **Pull Requests** sections of `~/.copilot/copilot-instructions.md`.

## Source of the rules

These rules come from `~/.copilot/copilot-instructions.md` under "Writing Style > Hard Rules" and from explicit user feedback captured in Copilot Memory. When a new hard rule is added, update both the instructions and `lint.py` (plus a test case in `tests.py`).

