#!/usr/bin/env python3
"""Lint text against Zack's writing-style hard rules.

These rules come from ~/.copilot/copilot-instructions.md under
"Writing Style > Hard Rules" and from explicit user feedback captured
in Copilot Memory. The linter catches the mechanical, easy-to-detect
violations so they cannot slip into external-facing text.

Usage:
    lint.py path/to/file.md [path/to/another.md ...]
    cat draft.md | lint.py
    cat draft.md | lint.py -
    lint.py --json path/to/file.md

Exit codes:
    0 - no violations
    1 - one or more violations
    2 - error reading or decoding a file
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache


@dataclass
class Violation:
    rule: str
    line: int
    column: int
    text: str
    message: str


EM_DASH_PATTERN = re.compile(r"\u2014")

# A hyphen-minus or en-dash used as sentence punctuation between two words:
# a single space-flanked dash with a letter on each side, e.g.
# "master drift - they came in". Requiring a letter on both sides keeps the
# rule to prose punctuation and avoids flagging word-joining hyphens
# ("runner-up", no spaces), list bullets ("- item", "  - item", "> - item"),
# thematic breaks ("- - -"), numbered steps and issue refs ("Step 1 - x",
# "Fix #5 - x"), numeric ranges ("3 - 1"), and label lists ("**Term** - desc").
SPACED_DASH_PATTERN = re.compile(r"(?<=[A-Za-z])[ \t][-\u2013][ \t](?=[A-Za-z])")

PER_DETERMINER_PATTERN = re.compile(
    r"\bper\s+(the|a|my|our|your|his|her|their|this|that|last|prior|previous|recent|latest|earlier|above|below|usual)\b",
    re.IGNORECASE,
)

PER_POSSESSIVE_PATTERN = re.compile(r"\b(?i:per)\s+[A-Za-z][A-Za-z'\u2019]*['\u2019]s\b")

PRAYER_HANDS_PATTERN = re.compile(r"\U0001F64F")

CLICK_HERE_PATTERN = re.compile(r"\[\s*(click here|here)\s*\][\(\[]", re.IGNORECASE)

ISP_INCIDENT_PATTERN = re.compile(r"\bISP\s+incidents?\b", re.IGNORECASE)

AGENTIC_PASSIVE_PATTERN = re.compile(
    r"\b(Claude|(?:Chat)?GPT|Copilot|Gemini|the (?:AI|model|assistant|agent))\s+"
    r"(made|wrote|generated|produced|created|drafted|composed|authored)\b",
    re.IGNORECASE,
)

# "This PR adds...", "This change introduces...", etc. Matches when the phrase
# appears at line start or after sentence-ending punctuation, which is where it
# is almost always the subject of a sentence (the violation pattern). Mid-sentence
# uses like "after merging this PR" are rare in PR bodies and are not matched.
THIS_PR_SUBJECT_PATTERN = re.compile(
    r"(?:^|(?<=[.!?])\s+)"
    r"This\s+(?:PR|change|commit|MR|pull\s+request|patch|diff|changeset|revision)\b",
    re.IGNORECASE,
)

# Bullets in PR bodies that lead with a bare past-tense action verb ("Added X",
# "Removed Y", "Inspected Z") instead of first person ("I added X"). Restricted
# to bullet lines because subjectless-verb detection in prose has too many false
# positives ("Updated tests confirm the fix"). The verb list is curated to the
# action verbs Zack actually uses in PR descriptions.
SUBJECTLESS_ACTION_BULLET_PATTERN = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+"
    r"(Added|Removed|Updated|Changed|Fixed|Created|Deleted|Modified|Refactored|"
    r"Renamed|Moved|Implemented|Introduced|Replaced|Wrote|Edited|Applied|Ran|"
    r"Built|Inspected|Verified|Tested|Configured|Installed|Pushed|Committed|"
    r"Rebased|Merged|Reorganized|Cleaned|Simplified|Migrated|Optimized|"
    r"Documented|Wired|Hooked|Integrated|Bumped|Pinned|Upgraded|Reverted|"
    r"Restored|Improved|Reduced|Enabled|Disabled|Drafted|Generated|Switched|"
    r"Swapped|Extracted|Inlined|Cached|Reworked|Reordered|Tagged|Squashed|"
    r"Rendered|Pulled|Pinged|Posted|Sent|Triggered|Closed|Opened)\b",
)


FENCE_OPEN_PATTERN = re.compile(r"^(\s{0,3})(`{3,}|~{3,})")
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")


def _blank_preserving_newline(line: str) -> str:
    """Return a same-length string of spaces, preserving any trailing newline."""
    if line.endswith("\n"):
        return " " * (len(line) - 1) + "\n"
    return " " * len(line)


def mask_code_regions(text: str) -> str:
    """Replace Markdown fenced code blocks and inline code spans with spaces.

    Preserves character offsets so line and column numbers stay accurate.
    Rules quoted inside backticks (e.g., examples in documentation) are
    masked out so the linter does not flag them as violations.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in lines:
        if not in_fence:
            m = FENCE_OPEN_PATTERN.match(line)
            if m:
                in_fence = True
                fence_char = m.group(2)[0]
                fence_len = len(m.group(2))
                out.append(_blank_preserving_newline(line))
                continue
            out.append(INLINE_CODE_PATTERN.sub(
                lambda mo: " " * len(mo.group(0)),
                line,
            ))
        else:
            stripped = line.lstrip()
            if (
                stripped.startswith(fence_char * fence_len)
                and set(stripped.rstrip()) <= {fence_char}
            ):
                in_fence = False
                fence_char = ""
                fence_len = 0
            out.append(_blank_preserving_newline(line))
    return "".join(out)


RULES = [
    (
        "no-em-dash",
        EM_DASH_PATTERN,
        "Em-dash (\u2014) is forbidden. Rephrase using a comma, period, parentheses, "
        "or restructure the sentence. Do not substitute a spaced hyphen, which is also flagged.",
    ),
    (
        "no-spaced-dash",
        SPACED_DASH_PATTERN,
        "A spaced hyphen or en-dash used as sentence punctuation (e.g., 'master drift - they "
        "came in') is forbidden. Rephrase using a comma, period, parentheses, or restructure "
        "the sentence. Word-joining hyphens (e.g., 'runner-up') are fine.",
    ),
    (
        "no-per-as-according-to",
        PER_DETERMINER_PATTERN,
        "'per' used to mean 'according to' is forbidden. Replace with 'based on …'. "
        "Rate/unit uses (e.g., '3 per second') are fine.",
    ),
    (
        "no-per-as-according-to",
        PER_POSSESSIVE_PATTERN,
        "'per <Name>'s …' is the 'according to' sense and is forbidden. "
        "Replace with 'based on <Name>'s …'.",
    ),
    (
        "no-prayer-hands",
        PRAYER_HANDS_PATTERN,
        "Prayer/folded-hands emoji (\U0001F64F) for thanks/please is forbidden. "
        "Use plain 'thanks' / 'please', or omit.",
    ),
    (
        "no-click-here",
        CLICK_HERE_PATTERN,
        "'click here' / '[here]' link text is not descriptive. "
        "Use text that describes the destination, e.g., '[the dashboard](url)'.",
    ),
    (
        "no-isp-incident",
        ISP_INCIDENT_PATTERN,
        "The prefix I-S-P before 'incident' is forbidden - just say 'incident'.",
    ),
    (
        "no-agentic-passive",
        AGENTIC_PASSIVE_PATTERN,
        "Agentic passive voice (model name as subject of verbs like made/wrote/generated) "
        "is forbidden - the human owns the output. Rephrase with the human as subject "
        "(for example: 'I made an error in the writeup').",
    ),
    (
        "no-this-pr-subject",
        THIS_PR_SUBJECT_PATTERN,
        "'This PR' / 'This change' / 'This commit' as the subject of a sentence "
        "is forbidden in PR descriptions and review replies - write in first person "
        "('I added retry logic', not 'This PR adds retry logic').",
    ),
    (
        "no-subjectless-action-bullet",
        SUBJECTLESS_ACTION_BULLET_PATTERN,
        "Bullets that lead with a bare past-tense action verb ('Added X', 'Removed Y') "
        "are forbidden in PR descriptions - rewrite in first person ('I added X', "
        "'I removed Y') so the human is the explicit actor.",
    ),
]


def find_violations(text: str, check_visibility: bool = False) -> list[Violation]:
    masked = mask_code_regions(text)
    violations: list[Violation] = []
    for lineno, line in enumerate(masked.splitlines(), start=1):
        for rule_name, pattern, message in RULES:
            for match in pattern.finditer(line):
                violations.append(
                    Violation(
                        rule=rule_name,
                        line=lineno,
                        column=match.start() + 1,
                        text=match.group(0),
                        message=message,
                    )
                )
    if check_visibility:
        violations.extend(_find_private_repo_refs(masked))
    violations.sort(key=lambda v: (v.line, v.column, v.rule))
    return violations


# Matches repo references with at least one anchoring signal:
# - github.com/owner/repo (URL prefix)
# - owner/repo#123 (issue/PR number suffix)
# - owner/repo/pull/123 or owner/repo/issues/456 (path suffix)
# Bare word/word without any anchor is NOT matched to avoid false positives
# on prose like "input/output", "client/server".
# Case-insensitive so mixed-case domains (GitHub.com) are caught, and the
# leading (?<![A-Za-z0-9.-]) boundary stops "github.com" from matching inside a
# longer host (notgithub.com, mygithub.com, my-github.com) and triggering
# spurious lookups.
_REPO_REF_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.-])(?:https?://)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:#\d+|/(?:pull|issues|actions|blob|tree|commit)/\S*)?",
    re.IGNORECASE,
)
_REPO_REF_SHORTHAND_PATTERN = re.compile(
    r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)"
)

# Exclude obvious false positives (file paths, known non-repo patterns)
_REPO_REF_EXCLUDE = re.compile(
    r"^(?:actions/|features/|refs/|docs/|src/|bin/|lib/|test/|spec/|pkg/|cmd/|"
    r"app/|config/|packages/|node_modules/|\.\w)"
)


@lru_cache(maxsize=256)
def _check_repo_visibility(owner_repo: str) -> str | None:
    """Query GitHub API for repo visibility. Returns 'private', 'internal', 'public', or None on error."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}", "--jq", ".visibility"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _find_private_repo_refs(masked_text: str) -> list[Violation]:
    """Find references to private/internal repos in the text."""
    violations: list[Violation] = []
    seen_repos: set[str] = set()

    for lineno, line in enumerate(masked_text.splitlines(), start=1):
        # Check URL-style references (github.com/owner/repo...)
        for match in _REPO_REF_URL_PATTERN.finditer(line):
            owner_repo = match.group(1).rstrip(".,;:!?)")
            parts = owner_repo.split("/")
            if len(parts) > 2:
                owner_repo = "/".join(parts[:2])
            if _REPO_REF_EXCLUDE.match(owner_repo):
                continue
            if owner_repo in seen_repos:
                continue
            seen_repos.add(owner_repo)

            visibility = _check_repo_visibility(owner_repo)
            if visibility in ("private", "internal"):
                violations.append(
                    Violation(
                        rule="no-private-repo-ref",
                        line=lineno,
                        column=match.start() + 1,
                        text=match.group(0),
                        message=f"Reference to {visibility} repo '{owner_repo}' in text destined for "
                        f"a public context. Anonymize it (e.g., 'an internal service repo') or "
                        f"remove the reference entirely.",
                    )
                )

        # Check shorthand references (owner/repo#123)
        for match in _REPO_REF_SHORTHAND_PATTERN.finditer(line):
            owner_repo = match.group(1)
            if _REPO_REF_EXCLUDE.match(owner_repo):
                continue
            if owner_repo in seen_repos:
                continue
            seen_repos.add(owner_repo)

            visibility = _check_repo_visibility(owner_repo)
            if visibility in ("private", "internal"):
                violations.append(
                    Violation(
                        rule="no-private-repo-ref",
                        line=lineno,
                        column=match.start() + 1,
                        text=match.group(0),
                        message=f"Reference to {visibility} repo '{owner_repo}' in text destined for "
                        f"a public context. Anonymize it (e.g., 'an internal service repo') or "
                        f"remove the reference entirely.",
                    )
                )
    return violations


def format_text(source: str, violations: list[Violation]) -> str:
    if not violations:
        return f"\u2713 {source}: no style violations"
    out = [f"\u2717 {source}: {len(violations)} violation(s)"]
    for v in violations:
        out.append(f"  [{v.rule}] line {v.line}:{v.column}  {v.text!r}")
        out.append(f"    {v.message}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint text against Zack's writing-style hard rules.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files to lint. If none provided, reads from stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output.",
    )
    parser.add_argument(
        "--check-visibility",
        action="store_true",
        help="Check repo references against the GitHub API and flag private/internal repos. "
        "Requires `gh` CLI authenticated. Use when the target surface is public.",
    )
    args = parser.parse_args(argv)

    results: list[tuple[str, list[Violation]]] = []
    exit_code = 0

    if args.paths:
        for path in args.paths:
            if path == "-":
                text = sys.stdin.read()
                source = "<stdin>"
            else:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                except OSError as exc:
                    print(f"Error reading {path}: {exc}", file=sys.stderr)
                    exit_code = max(exit_code, 2)
                    continue
                except UnicodeDecodeError as exc:
                    print(
                        f"Error decoding {path} as UTF-8: {exc}",
                        file=sys.stderr,
                    )
                    exit_code = max(exit_code, 2)
                    continue
                source = path
            violations = find_violations(text, check_visibility=args.check_visibility)
            results.append((source, violations))
            if violations:
                exit_code = max(exit_code, 1)
    else:
        text = sys.stdin.read()
        violations = find_violations(text, check_visibility=args.check_visibility)
        results.append(("<stdin>", violations))
        if violations:
            exit_code = max(exit_code, 1)

    if args.json:
        payload = {
            "sources": [
                {
                    "source": source,
                    "violation_count": len(violations),
                    "violations": [asdict(v) for v in violations],
                }
                for source, violations in results
            ],
            "total_violations": sum(len(v) for _, v in results),
        }
        print(json.dumps(payload, indent=2))
    else:
        for source, violations in results:
            print(format_text(source, violations))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
