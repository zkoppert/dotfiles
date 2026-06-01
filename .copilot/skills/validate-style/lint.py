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
import sys
from dataclasses import asdict, dataclass


@dataclass
class Violation:
    rule: str
    line: int
    column: int
    text: str
    message: str


EM_DASH_PATTERN = re.compile(r"\u2014")

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
        "Em-dash (\u2014) is forbidden. Use a hyphen with spaces ( - ), comma, period, or rephrase.",
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
]


def find_violations(text: str) -> list[Violation]:
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
    violations.sort(key=lambda v: (v.line, v.column, v.rule))
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
            violations = find_violations(text)
            results.append((source, violations))
            if violations:
                exit_code = max(exit_code, 1)
    else:
        text = sys.stdin.read()
        violations = find_violations(text)
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
