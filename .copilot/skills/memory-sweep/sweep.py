#!/usr/bin/env python3
"""Durable-guidance sweep.

Reads candidate rules extracted from memories and session history, then
reports which are covered by durable global, skill, or repository guidance.

Usage:
    python3 sweep.py <candidates.md> <guidance-path> [<guidance-path> ...]

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

from __future__ import annotations

import argparse
import os
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
PRESENT_THRESHOLD = 0.90
AMBIGUOUS_THRESHOLD = 0.30

GUIDANCE_FILENAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "SKILL.md",
    "copilot-instructions.md",
}
IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
}


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
    guidance_path: Path | None = None


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
    letters, digits, or underscores; and not in STOPWORDS. Hyphen-like
    punctuation is normalized to spaces so ``em-dashes`` and ``em dashes``
    compare consistently.
    """
    normalized = re.sub(r"[-\u2010-\u2015]", " ", text.lower())
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", normalized)
    return {w for w in words if w not in STOPWORDS}


def extract_quoted_phrases(text: str) -> list[str]:
    """Return all double-quoted phrases in `text`, lowercased and stripped."""
    phrases = re.findall(r'"([^"]{2,80})"', text)
    return [p.strip().lower() for p in phrases if p.strip()]


def classify(
    memory: Memory,
    instructions_tokens: set[str],
    instructions_lower: str,
    guidance_path: Path | None = None,
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
        guidance_path=guidance_path,
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
    if f.guidance_path:
        lines.append(f"  closest guidance: {f.guidance_path}")
    if f.memory.citations:
        lines.append(f"  citations: {f.memory.citations}")
    if f.missing_tokens:
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


def is_guidance_file(path: Path) -> bool:
    """Return whether a discovered file contains durable agent guidance."""
    return path.name in GUIDANCE_FILENAMES or path.name.endswith(".instructions.md")


def resolve_path(path: Path) -> Path:
    """Resolve a path or raise a CLI-friendly error."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise IOError(f"could not resolve guidance path {path}: {exc}") from exc


def _is_within(path: Path, directory: Path) -> bool:
    """Return whether ``path`` resolves within ``directory``."""
    try:
        resolve_path(path).relative_to(resolve_path(directory))
    except ValueError:
        return False
    return True


def _scan_guidance_tree(display_root: Path, boundary: Path) -> list[Path]:
    """Scan one guidance tree without crossing nested symlink boundaries."""
    guidance_files: list[Path] = []
    pending = [display_root]
    seen_directories: set[Path] = set()

    while pending:
        current = pending.pop()
        resolved_directory = resolve_path(current)
        if resolved_directory in seen_directories:
            continue
        seen_directories.add(resolved_directory)

        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise IOError(f"could not scan guidance directory {current}: {exc}") from exc

        for entry in entries:
            candidate = Path(entry.path)
            try:
                is_symlink = entry.is_symlink()
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise IOError(f"could not inspect guidance entry {candidate}: {exc}") from exc

            if is_directory:
                if entry.name not in IGNORED_DIRECTORIES:
                    pending.append(candidate)
                continue

            if is_symlink:
                if entry.name in IGNORED_DIRECTORIES or not is_guidance_file(candidate):
                    continue
                try:
                    linked_file = entry.is_file(follow_symlinks=True)
                except OSError as exc:
                    raise IOError(f"could not inspect guidance symlink {candidate}: {exc}") from exc
                if linked_file and is_guidance_file(candidate) and _is_within(candidate, boundary):
                    guidance_files.append(candidate)
                continue

            if is_file and is_guidance_file(candidate):
                guidance_files.append(candidate)

    return guidance_files


def collect_guidance_files(paths: list[Path]) -> list[Path]:
    """Expand explicit files and directories into deduplicated guidance files."""
    discovered: dict[Path, Path] = {}

    for path in paths:
        expanded = path.expanduser().absolute()
        canonical = resolve_path(expanded)
        if expanded.is_file():
            discovered.setdefault(canonical, expanded)
            continue
        if not expanded.is_dir():
            raise IOError(f"guidance path not found: {path}")

        scan_roots: list[tuple[Path, Path]] = [(expanded, expanded)]
        supplied_as_skill_root = expanded.parts[-2:] == (".copilot", "skills")
        resolved_as_skill_root = canonical.parts[-2:] == (".copilot", "skills")
        if supplied_as_skill_root or resolved_as_skill_root:
            scan_roots = []
            try:
                entries = list(os.scandir(expanded))
            except OSError as exc:
                raise IOError(f"could not scan guidance directory {expanded}: {exc}") from exc

            for entry in entries:
                candidate = Path(entry.path)
                if entry.name in IGNORED_DIRECTORIES:
                    continue
                try:
                    is_symlink = entry.is_symlink()
                    linked_directory = entry.is_dir(follow_symlinks=True)
                    linked_file = entry.is_file(follow_symlinks=True)
                except OSError as exc:
                    raise IOError(f"could not inspect guidance entry {candidate}: {exc}") from exc

                if linked_directory:
                    scan_roots.append((candidate, resolve_path(candidate)))
                elif linked_file and is_guidance_file(candidate):
                    discovered.setdefault(resolve_path(candidate), candidate)
                elif is_symlink and not linked_file:
                    raise IOError(f"skill symlink does not resolve to a file or directory: {candidate}")

        for display_root, boundary in scan_roots:
            for candidate in _scan_guidance_tree(display_root, boundary):
                discovered.setdefault(resolve_path(candidate), candidate)

    guidance_files = sorted(discovered.values(), key=lambda item: str(item))
    if not guidance_files:
        raise IOError("no guidance files found in the supplied paths")
    return guidance_files


def run_sweep(
    candidates_path: Path,
    guidance_paths: Path | list[Path],
) -> tuple[list[Finding], dict[str, int]]:
    candidates_text = _read_text(candidates_path, "candidates")
    requested_paths = [guidance_paths] if isinstance(guidance_paths, Path) else guidance_paths
    guidance_files = collect_guidance_files(requested_paths)
    guidance_documents = [
        (path, text, text.lower(), extract_tokens(text))
        for path in guidance_files
        for text in [_read_text(path, "guidance")]
    ]

    candidates = parse_memories(candidates_text)
    findings = [
        max(
            (
                classify(
                    candidate,
                    guidance_tokens,
                    guidance_lower,
                    guidance_path,
                )
                for guidance_path, _, guidance_lower, guidance_tokens in guidance_documents
            ),
            key=lambda finding: (
                finding.score,
                -len(finding.missing_tokens),
                len(finding.matched_tokens),
                len(finding.matched_phrases),
            ),
        )
        for candidate in candidates
    ]

    counts = {"PRESENT": 0, "AMBIGUOUS": 0, "PROMOTE": 0}
    for f in findings:
        counts[f.verdict] += 1

    return findings, counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare candidate rules from memories and session history against "
            "durable global, skill, and repository guidance."
        )
    )
    parser.add_argument(
        "candidates",
        type=Path,
        help="Path to candidate rules extracted from memories and session history.",
    )
    parser.add_argument(
        "guidance",
        type=Path,
        nargs="+",
        help=(
            "Guidance file or directory. Directories are searched for global, "
            "repository, agent, and skill instruction files."
        ),
    )
    parser.add_argument(
        "--only",
        choices=["PRESENT", "AMBIGUOUS", "PROMOTE"],
        help="Show only findings with this verdict.",
    )
    args = parser.parse_args()

    if not args.candidates.is_file():
        print(f"error: candidates file not found: {args.candidates}", file=sys.stderr)
        return 1

    try:
        guidance_files = collect_guidance_files(args.guidance)
        findings, counts = run_sweep(args.candidates, guidance_files)
    except IOError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    findings.sort(key=lambda f: (f.score, f.memory.subject))

    print(
        f"Scanned {len(findings)} candidate rules against "
        f"{len(guidance_files)} guidance files\n"
    )
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
