#!/usr/bin/env bash
# Local harness for the actions-security skill.
# Runs zizmor (security) + actionlint (correctness) over GitHub Actions workflow
# files, plus a fast heuristic grep for inline interpolation of untrusted event
# data. Exits non-zero if any check reports a finding, so it works as a gate.
#
# Usage:
#   check.sh [path ...]      # defaults to .github/workflows
set -uo pipefail

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
  if [ -d ".github/workflows" ]; then
    targets=(".github/workflows")
  else
    echo "No paths given and no .github/workflows directory found." >&2
    exit 2
  fi
fi

# Collect workflow files from the targets (files passed through as-is).
workflow_files=()
for t in "${targets[@]}"; do
  if [ -d "$t" ]; then
    while IFS= read -r f; do workflow_files+=("$f"); done < <(find "$t" -type f \( -name '*.yml' -o -name '*.yaml' \))
  elif [ -e "$t" ]; then
    workflow_files+=("$t")
  else
    echo "warning: path not found: $t" >&2
  fi
done

if [ ${#workflow_files[@]} -eq 0 ]; then
  echo "No workflow files found under: ${targets[*]}" >&2
  exit 2
fi

status=0

echo "==> Files in scope:"
printf '    %s\n' "${workflow_files[@]}"
echo

# 1. zizmor (authoritative security scanner)
echo "==> zizmor (security)"
if command -v zizmor >/dev/null 2>&1; then
  if ! zizmor "${targets[@]}"; then
    status=1
  fi
else
  echo "    zizmor not installed. Install with one of:"
  echo "      brew install zizmor   |   uvx zizmor   |   pipx install zizmor"
  status=1
fi
echo

# 2. actionlint (correctness; also catches some shellcheck issues in run blocks)
echo "==> actionlint (correctness)"
if command -v actionlint >/dev/null 2>&1; then
  if ! actionlint "${workflow_files[@]}"; then
    status=1
  fi
else
  echo "    actionlint not installed. Install with: brew install actionlint"
fi
echo

# 3. Fast heuristic: inline ${{ ... }} referencing untrusted event data.
#    zizmor is authoritative; this is a quick human-readable pre-check.
echo "==> heuristic: inline interpolation of untrusted event data"
untrusted='\$\{\{[^}]*(github\.event\.(issue|pull_request|comment|review|review_comment|discussion|commits|head_commit)|github\.head_ref|github\.event\.workflow_run)[^}]*\}\}'
if grep -RInE "$untrusted" "${workflow_files[@]}" 2>/dev/null; then
  echo
  echo "    ^ Review each hit: if any of these sit inside a run: block, bind the value"
  echo "      to an env: var first and expand it as a quoted shell variable."
  echo "      (zizmor's template-injection rule is the authoritative check.)"
else
  echo "    No obvious inline untrusted interpolation found."
fi
echo

if [ "$status" -eq 0 ]; then
  echo "All checks passed."
else
  echo "Findings present. Fix them (don't suppress) before shipping the workflow."
fi
exit "$status"
