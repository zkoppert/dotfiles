#!/usr/bin/env python3
"""Check PR/issue body for rendering issues after upload to GitHub.

Detects hard-wrapped paragraphs, split links, split tables, and truncation.
"""

import argparse
import json
import re
import subprocess
import sys


def fetch_body(args: argparse.Namespace) -> str:
    if args.file:
        with open(args.file) as f:
            return f.read()

    gh = "gh"
    if args.pr:
        cmd = [gh, "pr", "view", str(args.pr), "--repo", args.repo, "--json", "body", "--jq", ".body"]
    elif args.issue:
        cmd = [gh, "issue", "view", str(args.issue), "--repo", args.repo, "--json", "body", "--jq", ".body"]
    else:
        print("Error: provide --pr, --issue, or --file", file=sys.stderr)
        sys.exit(2)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching body: {result.stderr}", file=sys.stderr)
        sys.exit(2)
    return result.stdout


def check_short_lines(body: str) -> list[str]:
    """Flag sequences of short lines that look like hard-wrapped paragraphs."""
    issues = []
    lines = body.split("\n")
    in_code = False
    run_start = None
    run_count = 0

    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            in_code = not in_code
            run_count = 0
            continue

        if in_code:
            continue

        # Skip structural lines
        if (
            line.strip() == ""
            or re.match(r"^(\s*[-*]\s|\s*\d+\.\s|#{1,6}\s|\||>)", line)
        ):
            if run_count >= 3:
                issues.append(
                    f"lines {run_start}-{run_start + run_count - 1}: "
                    f"{run_count} consecutive short lines (likely hard-wrapped paragraph)"
                )
            run_count = 0
            continue

        if 20 < len(line) < 90:
            if run_count == 0:
                run_start = i
            run_count += 1
        else:
            if run_count >= 3:
                issues.append(
                    f"lines {run_start}-{run_start + run_count - 1}: "
                    f"{run_count} consecutive short lines (likely hard-wrapped paragraph)"
                )
            run_count = 0

    if run_count >= 3:
        issues.append(
            f"lines {run_start}-{run_start + run_count - 1}: "
            f"{run_count} consecutive short lines (likely hard-wrapped paragraph)"
        )

    return issues


def check_split_links(body: str) -> list[str]:
    """Find markdown links broken across lines."""
    issues = []
    for m in re.finditer(r"\]\s*\n\s*\(", body):
        line_num = body[: m.start()].count("\n") + 1
        issues.append(f"line {line_num}: markdown link split across lines")
    return issues


def check_split_tables(body: str) -> list[str]:
    """Find table rows that continue on the next line without a pipe."""
    issues = []
    lines = body.split("\n")
    for i, line in enumerate(lines[:-1]):
        if line.strip().startswith("|") and not line.strip().endswith("|"):
            next_line = lines[i + 1]
            if next_line.strip() and not next_line.strip().startswith("|"):
                issues.append(f"line {i + 1}: table row may be split")
    return issues


def check_length(body: str) -> list[str]:
    """Warn if body is approaching GitHub's 65,535 char limit."""
    issues = []
    if len(body) > 60000:
        issues.append(
            f"body is {len(body)} chars (GitHub limit is 65,535 - risk of truncation)"
        )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=str, help="PR number to check")
    parser.add_argument("--issue", type=str, help="Issue number to check")
    parser.add_argument("--repo", type=str, help="owner/repo")
    parser.add_argument("--file", type=str, help="Local file to check (no fetch)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    body = fetch_body(args)
    all_issues = []
    all_issues.extend(check_short_lines(body))
    all_issues.extend(check_split_links(body))
    all_issues.extend(check_split_tables(body))
    all_issues.extend(check_length(body))

    if args.json:
        json.dump({"issues": all_issues, "clean": len(all_issues) == 0}, sys.stdout)
        print()
    elif all_issues:
        print(f"✗ {len(all_issues)} rendering issue(s) found:")
        for issue in all_issues:
            print(f"  {issue}")
        sys.exit(1)
    else:
        print("✓ No rendering issues detected")


if __name__ == "__main__":
    main()
