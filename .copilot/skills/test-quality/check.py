#!/usr/bin/env python3
"""Check a git diff for minimum test evidence and test-design warning signs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


SOURCE_EXTENSIONS = {
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
    ".zsh",
}
TEST_DIRECTORIES = {"__tests__", "spec", "specs", "test", "tests"}
CATCH_ALL_TEST_STEMS = {
    "coverage_test",
    "test_coverage",
    "test_coverage_additions",
    "test_extra",
    "test_misc",
    "test_new_stuff",
}
PLACEHOLDER_WAIVERS = {
    "n/a",
    "na",
    "no",
    "none",
    "not needed",
    "skip",
    "test not needed",
    "tests not needed",
}
ASSERTION_RE = re.compile(
    r"\b(assert|expect|refute|should|pytest\.raises|assert_raises|raise_error)\b"
    r"|\.to(Have|Equal|Be|Match|Contain)",
    re.IGNORECASE,
)
UNITTEST_ASSERTION_RE = re.compile(r"\bassert[A-Z]\w*\s*\(")
WEAK_ASSERTION_RE = re.compile(
    r"\b(assertIsNotNone|assert_not_nil|assert_respond_to|assert_kind_of|"
    r"assertIsInstance|toBeDefined|toBeTruthy)\b|"
    r"\bassertTrue\s*\(|\bassert\s+isinstance\s*\(|"
    r"\bassert\s+.+\s+is\s+not\s+None\b",
    re.IGNORECASE,
)
COVERAGE_LANGUAGE_RE = re.compile(
    r"\b(covers?|coverage gap|raise coverage|reach 100%|previously uncovered)\b"
    r".{0,80}\b[\w./-]+\.(?:py|rb|go|js|jsx|ts|tsx):\d+",
    re.IGNORECASE,
)
SUPPRESSION_RE = re.compile(
    r"pragma:\s*no cover|:nocov:|pylint:\s*disable|eslint-disable|"
    r"rubocop:\s*disable|#\s*noqa\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FileChange:
    status: str
    path: str
    old_path: str | None = None


@dataclass
class DiffHunk:
    path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    message: str
    paths: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    status: str
    evidence: str
    base_ref: str | None
    base_commit: str | None
    head_commit: str | None
    source_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    waiver: str | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["error_count"] = len(self.errors)
        data["warning_count"] = len(self.warnings)
        return data


class GitError(RuntimeError):
    """Raised when git cannot provide required repository data."""


def run_git(
    args: list[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {stderr}")
    return result


def git_text(args: list[str], *, cwd: Path, check: bool = True) -> str:
    return run_git(args, cwd=cwd, check=check).stdout.decode(
        "utf-8", errors="replace"
    ).strip()


def resolve_commit(ref: str, *, cwd: Path) -> str | None:
    result = run_git(
        ["rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip()


def empty_tree(cwd: Path) -> str:
    return run_git(
        ["hash-object", "-t", "tree", "--stdin"],
        cwd=cwd,
        input_bytes=b"",
    ).stdout.decode("ascii").strip()


def is_root_commit(commit: str, *, cwd: Path) -> bool:
    parents = git_text(["rev-list", "--parents", "-n", "1", commit], cwd=cwd)
    return len(parents.split()) == 1


def infer_base(head_commit: str, *, cwd: Path) -> tuple[str, str] | None:
    env_ref = os.environ.get("TEST_QUALITY_BASE_REF") or os.environ.get(
        "GITHUB_BASE_REF"
    )
    candidates: list[str] = []
    if env_ref:
        candidates.append(env_ref)

    origin_head = git_text(
        ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=cwd,
        check=False,
    )
    if origin_head:
        candidates.append(origin_head)
    candidates.extend(["origin/main", "origin/master", "main", "master"])

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        candidate_commit = resolve_commit(candidate, cwd=cwd)
        if not candidate_commit:
            continue
        merge_base = git_text(
            ["merge-base", candidate_commit, head_commit],
            cwd=cwd,
            check=False,
        )
        if merge_base:
            return candidate, merge_base

    if is_root_commit(head_commit, cwd=cwd):
        return "(empty tree)", empty_tree(cwd)
    return None


def parse_name_status(raw: bytes) -> list[FileChange]:
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    changes: list[FileChange] = []
    index = 0
    while index < len(tokens):
        status = tokens[index].decode("ascii", errors="replace")
        index += 1
        if status.startswith(("R", "C")):
            old_path = tokens[index].decode("utf-8", errors="surrogateescape")
            path = tokens[index + 1].decode("utf-8", errors="surrogateescape")
            index += 2
            changes.append(FileChange(status=status, path=path, old_path=old_path))
            continue
        path = tokens[index].decode("utf-8", errors="surrogateescape")
        index += 1
        changes.append(FileChange(status=status, path=path))
    return changes


def changed_files(base_commit: str, head_commit: str, *, cwd: Path) -> list[FileChange]:
    result = run_git(
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base_commit,
            head_commit,
            "--",
        ],
        cwd=cwd,
    )
    return parse_name_status(result.stdout)


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = {part.lower() for part in pure.parts[:-1]}
    if parts & TEST_DIRECTORIES:
        return True
    name = pure.name.lower()
    stem = pure.stem.lower()
    return bool(
        stem in {"test", "tests"}
        or stem.startswith("test_")
        or stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
        or name.endswith(("_test.go", "_test.rb", "_spec.rb"))
    )


def tree_entry(commit: str, path: str, *, cwd: Path) -> tuple[str, str] | None:
    result = run_git(
        ["ls-tree", "-z", commit, "--", path],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    metadata, _separator, _path = result.stdout.partition(b"\t")
    fields = metadata.decode("ascii", errors="replace").split()
    if len(fields) < 3:
        return None
    return fields[0], fields[2]


def has_shebang(commit: str, path: str, *, cwd: Path) -> bool:
    result = run_git(["show", f"{commit}:{path}"], cwd=cwd, check=False)
    if result.returncode != 0:
        return False
    first_line = result.stdout.splitlines()[0] if result.stdout else b""
    return first_line.startswith(b"#!")


def is_source_path(
    change: FileChange,
    *,
    base_commit: str,
    head_commit: str,
    cwd: Path,
) -> bool:
    if is_test_path(change.path):
        return False
    path = PurePosixPath(change.path)
    if path.suffix.lower() in SOURCE_EXTENSIONS:
        return True

    commit = base_commit if change.status.startswith("D") else head_commit
    target = change.old_path if change.status.startswith("D") and change.old_path else change.path
    entry = tree_entry(commit, target, cwd=cwd)
    if entry and entry[0] in {"100755", "100775"}:
        return True
    return has_shebang(commit, target, cwd=cwd)


def added_lines_by_path(
    base_commit: str, head_commit: str, *, cwd: Path
) -> dict[str, list[str]]:
    patch = git_text(
        ["diff", "--unified=0", "--no-color", base_commit, head_commit, "--"],
        cwd=cwd,
    )
    added: dict[str, list[str]] = {}
    current_path: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            added.setdefault(current_path, [])
        elif line.startswith("+++ /dev/null"):
            current_path = None
        elif current_path and line.startswith("+") and not line.startswith("+++"):
            added[current_path].append(line[1:])
    return added


def diff_hunks(base_commit: str, head_commit: str, *, cwd: Path) -> list[DiffHunk]:
    patch = git_text(
        ["diff", "--unified=0", "--no-color", base_commit, head_commit, "--"],
        cwd=cwd,
    )
    hunks: list[DiffHunk] = []
    old_path: str | None = None
    current_path: str | None = None
    current_hunk: DiffHunk | None = None
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            old_path = line[6:]
        elif line.startswith("+++ b/"):
            current_path = line[6:]
        elif line == "+++ /dev/null":
            current_path = old_path
        elif line.startswith("@@") and current_path:
            current_hunk = DiffHunk(path=current_path)
            hunks.append(current_hunk)
        elif current_hunk and line.startswith("+") and not line.startswith("+++"):
            current_hunk.added.append(line[1:])
        elif current_hunk and line.startswith("-") and not line.startswith("---"):
            current_hunk.removed.append(line[1:])
    return hunks


def paired_threshold_changes(
    hunks: list[DiffHunk],
    pattern: re.Pattern[str],
) -> list[tuple[str, int, int]]:
    changes: list[tuple[str, int, int]] = []
    for hunk in hunks:
        old_values = [
            int(match.group(1))
            for line in hunk.removed
            if (match := pattern.search(line))
        ]
        new_values = [
            int(match.group(1))
            for line in hunk.added
            if (match := pattern.search(line))
        ]
        changes.extend(
            (hunk.path, old_value, new_value)
            for old_value, new_value in zip(old_values, new_values)
        )
    return changes


def valid_waiver(reason: str | None) -> bool:
    if not reason:
        return False
    normalized = " ".join(reason.split()).strip()
    return len(normalized) >= 20 and normalized.lower() not in PLACEHOLDER_WAIVERS


def analyze(
    *,
    cwd: Path,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
    no_test_change_reason: str | None = None,
) -> Analysis:
    root = Path(git_text(["rev-parse", "--show-toplevel"], cwd=cwd))
    head_commit = resolve_commit(head_ref, cwd=root)
    if not head_commit:
        return Analysis(
            status="error",
            evidence="unavailable",
            base_ref=base_ref,
            base_commit=None,
            head_commit=None,
            findings=[
                Finding("error", "invalid-head", f"Cannot resolve head ref '{head_ref}'.")
            ],
        )

    if base_ref:
        base_candidate = resolve_commit(base_ref, cwd=root)
        if not base_candidate:
            return Analysis(
                status="error",
                evidence="unavailable",
                base_ref=base_ref,
                base_commit=None,
                head_commit=head_commit,
                findings=[
                    Finding(
                        "error",
                        "invalid-base",
                        f"Cannot resolve explicit base ref '{base_ref}'.",
                    )
                ],
            )
        merge_base = git_text(
            ["merge-base", base_candidate, head_commit],
            cwd=root,
            check=False,
        )
        if not merge_base:
            return Analysis(
                status="error",
                evidence="unavailable",
                base_ref=base_ref,
                base_commit=None,
                head_commit=head_commit,
                findings=[
                    Finding(
                        "error",
                        "no-merge-base",
                        f"Explicit base ref '{base_ref}' has no merge base with {head_ref}.",
                    )
                ],
            )
        resolved_base_ref, base_commit = base_ref, merge_base
    else:
        inferred = infer_base(head_commit, cwd=root)
        if not inferred:
            return Analysis(
                status="skipped",
                evidence="unavailable",
                base_ref=None,
                base_commit=None,
                head_commit=head_commit,
                findings=[
                    Finding(
                        "warning",
                        "base-not-inferred",
                        "No default branch could be inferred; pass --base to analyze the branch diff.",
                    )
                ],
            )
        resolved_base_ref, base_commit = inferred

    changes = changed_files(base_commit, head_commit, cwd=root)
    source_changes = [
        change
        for change in changes
        if is_source_path(
            change,
            base_commit=base_commit,
            head_commit=head_commit,
            cwd=root,
        )
    ]
    test_changes = [change for change in changes if is_test_path(change.path)]
    active_test_changes = [
        change for change in test_changes if not change.status.startswith("D")
    ]
    source_files = sorted({change.path for change in source_changes})
    test_files = sorted({change.path for change in active_test_changes})
    findings: list[Finding] = []
    waiver = " ".join(no_test_change_reason.split()).strip() if no_test_change_reason else None

    if source_files and not test_files:
        if waiver and not valid_waiver(waiver):
            findings.append(
                Finding(
                    "error",
                    "invalid-no-test-waiver",
                    "The no-test-change reason must be specific, at least 20 characters, and not a placeholder.",
                    source_files,
                )
            )
        elif not waiver:
            findings.append(
                Finding(
                    "error",
                    "source-without-test",
                    "Executable source changed without a committed test change. Add regression tests or provide --no-test-change-reason.",
                    source_files,
                )
            )

    new_catch_all = sorted(
        {
            change.path
            for change in active_test_changes
            if change.status.startswith(("A", "C", "R"))
            and PurePosixPath(change.path).stem.lower() in CATCH_ALL_TEST_STEMS
        }
    )
    if new_catch_all:
        findings.append(
            Finding(
                "error",
                "new-catch-all-test",
                "New catch-all test files hide source ownership. Put each test in the owning module's test file.",
                new_catch_all,
            )
        )

    deleted_tests = sorted(
        change.old_path or change.path
        for change in test_changes
        if change.status.startswith("D")
    )
    if deleted_tests and not active_test_changes:
        findings.append(
            Finding(
                "warning",
                "deleted-tests-without-replacement",
                "Tests were deleted without an added or modified replacement test.",
                deleted_tests,
            )
        )

    added_by_path = added_lines_by_path(base_commit, head_commit, cwd=root)
    test_added_lines = {
        path: lines for path, lines in added_by_path.items() if is_test_path(path)
    }
    if test_files and not any(
        ASSERTION_RE.search(line) or UNITTEST_ASSERTION_RE.search(line)
        for lines in test_added_lines.values()
        for line in lines
    ):
        findings.append(
            Finding(
                "warning",
                "test-change-without-assertion",
                "Test files changed without adding an assertion or expectation. Confirm the change pins observable behavior.",
                test_files,
            )
        )

    coverage_language_paths = sorted(
        path
        for path, lines in test_added_lines.items()
        if any(COVERAGE_LANGUAGE_RE.search(line) for line in lines)
    )
    if coverage_language_paths:
        findings.append(
            Finding(
                "warning",
                "coverage-shaped-test",
                "Test prose refers to coverage or source line numbers. Describe the behavior and outcome instead.",
                coverage_language_paths,
            )
        )

    weak_assertion_paths = sorted(
        path
        for path, lines in test_added_lines.items()
        if any(WEAK_ASSERTION_RE.search(line) for line in lines)
    )
    if weak_assertion_paths:
        findings.append(
            Finding(
                "warning",
                "weak-assertion",
                "A weak assertion may only prove that code ran. Confirm the test fails for a wrong or no-op implementation.",
                weak_assertion_paths,
            )
        )

    hunks = diff_hunks(base_commit, head_commit, cwd=root)
    coverage_patterns = [
        re.compile(r"cov-fail-under(?:=|\s+)(\d+)", re.IGNORECASE),
        re.compile(r"fail_under\s*=\s*(\d+)", re.IGNORECASE),
        re.compile(r"minimum_coverage(?:\s+\w+:)?\s*(\d+)", re.IGNORECASE),
    ]
    coverage_decreases: list[tuple[str, int, int]] = []
    for pattern in coverage_patterns:
        coverage_decreases.extend(
            change
            for change in paired_threshold_changes(hunks, pattern)
            if change[2] < change[1]
        )
    if coverage_decreases:
        details = ", ".join(
            f"{path}: {old} to {new}"
            for path, old, new in coverage_decreases
        )
        findings.append(
            Finding(
                "warning",
                "coverage-threshold-lowered",
                f"Coverage threshold decreased ({details}). Verify this is an intentional policy change.",
                sorted({path for path, _old, _new in coverage_decreases}),
            )
        )

    module_pattern = re.compile(r"max-module-lines[^0-9]*(\d+)", re.IGNORECASE)
    module_increases = [
        change
        for change in paired_threshold_changes(hunks, module_pattern)
        if change[2] > change[1]
    ]
    if module_increases:
        details = ", ".join(
            f"{path}: {old} to {new}"
            for path, old, new in module_increases
        )
        findings.append(
            Finding(
                "warning",
                "module-size-threshold-raised",
                f"Module-size threshold increased ({details}). Prefer splitting the file when practical.",
                sorted({path for path, _old, _new in module_increases}),
            )
        )

    suppression_paths = sorted(
        path
        for path, lines in added_by_path.items()
        if any(SUPPRESSION_RE.search(line) for line in lines)
    )
    if suppression_paths:
        findings.append(
            Finding(
                "warning",
                "new-test-or-lint-suppression",
                "New coverage or lint suppressions need a specific justification.",
                suppression_paths,
            )
        )

    errors = [finding for finding in findings if finding.severity == "error"]
    if errors:
        status = "failed"
        evidence = "missing"
    elif not source_files:
        status = "passed"
        evidence = "not-applicable"
    elif test_files:
        status = "passed"
        evidence = "present"
    else:
        status = "passed"
        evidence = "waived"

    return Analysis(
        status=status,
        evidence=evidence,
        base_ref=resolved_base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        source_files=source_files,
        test_files=test_files,
        waiver=waiver,
        findings=findings,
    )


def print_text(analysis: Analysis) -> None:
    print("Test-quality evidence preflight")
    print(f"Status: {analysis.status}")
    print(f"Evidence: {analysis.evidence}")
    print(f"Base: {analysis.base_ref or 'unavailable'}")
    print(f"Base commit: {analysis.base_commit or 'unavailable'}")
    print(f"Head commit: {analysis.head_commit or 'unavailable'}")
    print(f"Executable source files changed: {len(analysis.source_files)}")
    print(f"Test files changed: {len(analysis.test_files)}")
    if analysis.waiver:
        print(f"No-test-change reason: {analysis.waiver}")
    for finding in analysis.findings:
        label = finding.severity.upper()
        paths = f" [{', '.join(finding.paths)}]" if finding.paths else ""
        print(f"{label} [{finding.rule}] {finding.message}{paths}")
    print(
        f"Findings: {len(analysis.errors)} error(s), "
        f"{len(analysis.warnings)} warning(s)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="base branch/ref to compare")
    parser.add_argument("--head", default="HEAD", help="head ref to analyze")
    parser.add_argument(
        "--no-test-change-reason",
        default=None,
        help="specific reason executable source has no committed test change",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        analysis = analyze(
            cwd=Path.cwd(),
            base_ref=args.base,
            head_ref=args.head,
            no_test_change_reason=args.no_test_change_reason,
        )
    except GitError as exc:
        analysis = Analysis(
            status="error",
            evidence="unavailable",
            base_ref=args.base,
            base_commit=None,
            head_commit=None,
            findings=[Finding("error", "git-error", str(exc))],
        )

    if args.json:
        json.dump(analysis.to_dict(), sys.stdout, indent=2)
        print()
    else:
        print_text(analysis)

    if analysis.status in {"failed", "skipped"}:
        return 1
    if analysis.status == "error":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
