#!/usr/bin/env python3
"""Memory-to-instructions sweep.

Reads a dump of the agent's stored memories and the personal
copilot-instructions.md file, then reports which memory facts are likely
already covered in the instructions and which are memory-only rules that
should be promoted into the file.

Usage:
    python3 sweep.py <memories.md> <instructions.md>

Exit codes:
    0  Run completed (regardless of findings).
    1  Bad arguments or unreadable files.

The report is printed to stdout. Each fact is classified as:
    PRESENT    - significant keyword overlap with the instructions file
    AMBIGUOUS  - partial overlap; human should eyeball it
    PROMOTE    - little to no overlap; likely memory-only rule

This is a heuristic, not a semantic match. The goal is to shrink a long
memory list down to a small set of candidates that the human can review
in one sitting.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Common English stopwords plus a few domain-specific filler terms that
# show up in nearly every memory and add no signal.
STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "being",
    "but", "by", "can", "do", "does", "for", "from", "has", "have",
    "having", "if", "in", "into", "is", "it", "its", "may", "must",
    "not", "of", "on", "or", "should", "so", "such", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "use", "uses", "used", "using", "via", "was", "were",
    "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your", "yours",
    "fact", "citations", "input", "user", "remember", "always", "never",
    "also", "even", "just", "rather", "really", "very", "ever",
    "rule", "strict", "hard", "preference", "convention",
}

# Verdict thresholds.
PRESENT_THRESHOLD = 0.70
AMBIGUOUS_THRESHOLD = 0.30


@dataclass
class Memory:
    """A single memory entry parsed from the dump file."""

    subject: str
    fact: str
    citations: str


@dataclass
class Finding:
    """The sweep result for one memory."""

    memory: Memory
    verdict: str
    score: float
    matched_tokens: list[str]
    missing_tokens: list[str]
    matched_phrases: list[str]


def parse_memories(text: str) -> list[Memory]:
    """Parse the memory dump into structured records.

    Tolerates the markdown format used in the agent prompt's <memories>
    block:

        **subject heading**
        - Fact: <fact text>
        - Citations: <citation text>

    A new entry starts whenever a `**bold subject**` line or an `##`,
    `###`, or `####` heading is seen. A new `- Fact:` line under the
    same subject also starts a new entry (so multi-fact subjects do not
    silently lose their earlier facts). Single-`#` headings, blank
    lines, and other markdown noise are ignored.
    """
    memories: list[Memory] = []
    current_subject: str | None = None
    current_fact: list[str] = []
    current_citations: list[str] = []
    collecting: str | None = None

    def flush() -> None:
        nonlocal current_subject, current_fact, current_citations, collecting
        fact_text = " ".join(current_fact).strip()
        if current_subject and fact_text:
            memories.append(
                Memory(
                    subject=current_subject.strip(),
                    fact=fact_text,
                    citations=" ".join(current_citations).strip(),
                )
            )
        current_subject = None
        current_fact = []
        current_citations = []
        collecting = None

    # Subject markers (in order of preference):
    #   **bold subject**           - canonical form used in the agent prompt
    #   ## subject / ### subject   - markdown heading form some dumps use
    bold_subject_re = re.compile(r"^\*\*(.+?)\*\*\s*$")
    heading_subject_re = re.compile(r"^#{2,4}\s+(.+?)\s*$")
    # Any "- Foo:" field-style line, used to detect when a non-canonical field
    # (e.g. "- Note:") follows a fact and should stop fact collection.
    field_line_re = re.compile(r"^- ?[A-Za-z][A-Za-z0-9_-]*\s*:")
    fact_line_re = re.compile(r"^- ?Fact:\s*(.*)$", re.IGNORECASE)
    citations_line_re = re.compile(r"^- ?Citations?:\s*(.*)$", re.IGNORECASE)

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        bold_match = bold_subject_re.match(line)
        if bold_match:
            flush()
            current_subject = bold_match.group(1)
            continue
        heading_match = heading_subject_re.match(line)
        if heading_match:
            flush()
            current_subject = heading_match.group(1)
            continue

        fact_match = fact_line_re.match(line)
        if fact_match:
            if current_subject and current_fact:
                saved_subject = current_subject
                flush()
                current_subject = saved_subject
            current_fact = [fact_match.group(1)]
            collecting = "fact"
            continue
        citations_match = citations_line_re.match(line)
        if citations_match:
            current_citations = [citations_match.group(1)]
            collecting = "citations"
            continue

        if not line.strip():
            continue

        # An unrecognized "- Foo:" line (e.g. "- Note: extra prose") ends the
        # current collection rather than getting silently appended to the fact
        # or citations body.
        if field_line_re.match(line):
            collecting = None
            continue

        # Only indented lines count as continuations of the previous
        # fact or citations body. Non-indented prose ends collection so it
        # cannot pollute the fact text.
        is_indented = raw_line.startswith((" ", "\t"))
        if collecting == "fact" and is_indented:
            current_fact.append(line.strip())
        elif collecting == "citations" and is_indented:
            current_citations.append(line.strip())
        elif collecting is not None:
            collecting = None

    flush()
    return memories


def extract_tokens(text: str) -> set[str]:
    """Return the set of distinctive lowercase tokens in `text`.

    A token is 4+ characters, starting with a letter, followed by
    letters, digits, hyphens, or underscores; and not in STOPWORDS.
    Hyphens and underscores are kept on purpose so domain terms like
    `co-authored` and `foo_bar` survive tokenization intact.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def extract_quoted_phrases(text: str) -> list[str]:
    """Return all double-quoted phrases in `text`, lowercased and stripped."""
    phrases = re.findall(r'"([^"]{2,80})"', text)
    return [p.strip().lower() for p in phrases if p.strip()]


def classify(
    memory: Memory,
    instructions_tokens: set[str],
    instructions_lower: str,
) -> Finding:
    """Score a single memory against the instructions corpus.

    Token matching uses set membership against the pre-extracted token set
    of the instructions file, not substring containment, so a fact token
    like ``commit`` does not match the substring inside ``committed``.

    Phrase matching uses substring search against the lowercased file text,
    which is intentional - quoted phrases are short, specific strings the
    user wants located verbatim.
    """
    fact_tokens = extract_tokens(memory.fact)
    quoted_phrases = extract_quoted_phrases(memory.fact)

    matched_tokens = sorted(fact_tokens & instructions_tokens)
    missing_tokens = sorted(fact_tokens - instructions_tokens)
    matched_phrases = sorted(p for p in quoted_phrases if p in instructions_lower)

    token_score = 0.0
    if fact_tokens:
        token_score = len(matched_tokens) / len(fact_tokens)
    phrase_bonus = 0.0
    if quoted_phrases:
        phrase_bonus = (len(matched_phrases) / len(quoted_phrases)) * 0.3
    score = min(1.0, token_score + phrase_bonus)

    if score >= PRESENT_THRESHOLD:
        verdict = "PRESENT"
    elif score >= AMBIGUOUS_THRESHOLD:
        verdict = "AMBIGUOUS"
    else:
        verdict = "PROMOTE"

    return Finding(
        memory=memory,
        verdict=verdict,
        score=score,
        matched_tokens=matched_tokens,
        missing_tokens=missing_tokens,
        matched_phrases=matched_phrases,
    )


def truncate(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


def format_finding(f: Finding) -> str:
    """Render a single finding as a multi-line human-readable block."""
    lines = [
        f"[{f.verdict:9s}] score={f.score:.2f}  subject={f.memory.subject}",
        f"  fact: {truncate(f.memory.fact, 140)}",
    ]
    if f.matched_phrases:
        lines.append(f"  matched phrases: {', '.join(repr(p) for p in f.matched_phrases[:3])}")
    if f.verdict != "PRESENT" and f.missing_tokens:
        sample = ", ".join(f.missing_tokens[:8])
        lines.append(f"  missing tokens (sample): {sample}")
    return "\n".join(lines)


def _read_text(path: Path, label: str) -> str:
    """Read a UTF-8 text file or raise a clear ``IOError`` for the CLI."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IOError(f"could not read {label} file {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise IOError(f"could not decode {label} file {path} as UTF-8: {exc}") from exc


def run_sweep(memories_path: Path, instructions_path: Path) -> tuple[list[Finding], dict[str, int]]:
    memories_text = _read_text(memories_path, "memories")
    instructions_text = _read_text(instructions_path, "instructions")
    instructions_lower = instructions_text.lower()
    instructions_tokens = extract_tokens(instructions_text)

    memories = parse_memories(memories_text)
    findings = [classify(m, instructions_tokens, instructions_lower) for m in memories]

    counts = {"PRESENT": 0, "AMBIGUOUS": 0, "PROMOTE": 0}
    for f in findings:
        counts[f.verdict] += 1

    return findings, counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a dump of stored Copilot memories against the personal "
            "copilot-instructions.md file and flag memory-only rules that "
            "should be promoted into the file."
        )
    )
    parser.add_argument(
        "memories",
        type=Path,
        help="Path to the memories dump (markdown).",
    )
    parser.add_argument(
        "instructions",
        type=Path,
        help="Path to the personal copilot-instructions.md.",
    )
    parser.add_argument(
        "--only",
        choices=["PRESENT", "AMBIGUOUS", "PROMOTE"],
        help="Show only findings with this verdict.",
    )
    args = parser.parse_args()

    if not args.memories.is_file():
        print(f"error: memories file not found: {args.memories}", file=sys.stderr)
        return 1
    if not args.instructions.is_file():
        print(f"error: instructions file not found: {args.instructions}", file=sys.stderr)
        return 1

    try:
        findings, counts = run_sweep(args.memories, args.instructions)
    except IOError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    findings.sort(key=lambda f: (f.score, f.memory.subject))

    print(f"Scanned {len(findings)} memories against {args.instructions.name}\n")
    print(
        f"  PROMOTE: {counts['PROMOTE']:3d}   "
        f"AMBIGUOUS: {counts['AMBIGUOUS']:3d}   "
        f"PRESENT: {counts['PRESENT']:3d}\n"
    )

    shown = 0
    for f in findings:
        if args.only and f.verdict != args.only:
            continue
        print(format_finding(f))
        print()
        shown += 1

    if args.only and shown == 0:
        print(f"(no findings with verdict {args.only})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
