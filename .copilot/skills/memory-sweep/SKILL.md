---
name: memory-sweep
description: Use when the user asks to audit memories, find memory-only rules, review Copilot memories, or identify durable guidance that exists only in session history. Compare explicit user rules from memories and sessions against global instructions, skill definitions, and repository rules, then recommend the correct durable owner for uncovered guidance.
---

# Audit durable Copilot guidance

Use this skill to find explicit user rules that appear in memories or session history but are missing from durable guidance.

## Sources of truth

- Universal preferences belong in `~/.copilot/copilot-instructions.md`.
- Task-specific behavior belongs in the relevant skill's `SKILL.md`, which is authoritative for that skill.
- Repository conventions belong in that repository's `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`, or `.github/instructions/**/*.instructions.md`.
- Session history and memories are evidence for candidate rules, not durable policy by themselves.

## Run the sweep

### 1. Create a private workspace

```bash
SWEEP_DIR=$(mktemp -d -t memory-sweep.XXXXXX)
chmod 700 "$SWEEP_DIR"
```

Create `"$SWEEP_DIR/candidates.md"` using this format:

```markdown
**subject**
- Fact: <explicit user rule>
- Citations: <memory citation or session ID and timestamp>
```

### 2. Collect candidate rules

Add the current `<memories>` block without its surrounding instructional prose. Then search session history with `session_store_sql`, starting with the last 7 days and widening only when needed. Inspect user messages, not assistant responses, for explicit durable language such as "always," "never," "prefer," "don't," or "should." Use a time-bounded query and a finite limit, for example:

```sql
SELECT session_id, timestamp, user_message
FROM turns
WHERE timestamp >= now() - INTERVAL '7 days'
  AND (
    user_message ILIKE '%always%'
    OR user_message ILIKE '%never%'
    OR user_message ILIKE '%prefer%'
    OR user_message ILIKE '%don''t%'
    OR user_message ILIKE '%should%'
  )
ORDER BY timestamp DESC
LIMIT 200
```

Extract only rules the user stated or explicitly approved. Do not treat assistant suggestions as user policy. Add each session-derived rule to `candidates.md` with its session ID and timestamp.

### 3. Compare against durable guidance

Build the guidance argument list, adding the current repository only when the command is running inside one:

```bash
GUIDANCE=(
  ~/.copilot/copilot-instructions.md
  ~/.copilot/skills
)
if REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  GUIDANCE+=("$REPO_ROOT")
fi
```

Run:

```bash
python3 ~/.copilot/skills/memory-sweep/sweep.py \
  "$SWEEP_DIR/candidates.md" \
  "${GUIDANCE[@]}"
```

Directory arguments are searched only for recognized instruction and skill files. Use `--only PROMOTE` to show only likely gaps.

### 4. Review findings

The classifier sorts findings from least to most overlap:

| Verdict | Score | Meaning |
|---------|-------|---------|
| PROMOTE | `< 0.30` | Little overlap with durable guidance |
| AMBIGUOUS | `0.30` to `< 0.90` | Partial overlap that needs review |
| PRESENT | `>= 0.90` | Strongly represented in one durable guidance file |

For each `PROMOTE` or relevant `AMBIGUOUS` finding:

1. Verify the candidate against its memory or session citation.
2. Ask the user whether it should become durable guidance.
3. Recommend the correct owner: global instructions, a skill, or repository rules.
4. Check for conflicting existing guidance before editing.

Do not promote findings automatically. The classifier is a keyword and phrase triage aid, not a semantic policy engine.

### 5. Clean up

```bash
rm -rf "$SWEEP_DIR"
```

## Caveats

- Session searches are best-effort and must remain time-bounded.
- Repository guidance outside the current checkout is not included unless passed explicitly.
- User-level skill discovery follows each immediate skill symlink under `~/.copilot/skills`, but does not follow nested symlinks inside a skill. Repository scans do not follow directory symlinks or file symlinks that resolve outside the repository.
- A `PRESENT` result can still hide contradictory wording, so inspect likely conflicts directly.
