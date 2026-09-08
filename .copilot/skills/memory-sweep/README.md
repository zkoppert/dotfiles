# memory-sweep

`memory-sweep` finds explicit user rules in memories and session history that are missing from durable Copilot guidance.

Durable guidance can live in:

- Global `copilot-instructions.md`
- Skill `SKILL.md` files
- Repository `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and `.github` instruction files

The Copilot skill gathers candidate rules from memories and user-authored session messages. `sweep.py` compares those candidates with one or more guidance files or directories.

## Usage

```bash
python3 sweep.py candidates.md \
  ~/.copilot/copilot-instructions.md \
  ~/.copilot/skills \
  /path/to/repository
```

Directory arguments are searched for recognized instruction and skill files. Explicit files are always accepted.

Filter to one verdict:

```bash
python3 sweep.py candidates.md ~/.copilot/skills --only PROMOTE
```

## Candidate format

```markdown
**subject heading**
- Fact: <explicit user rule>
- Citations: <memory citation or session ID and timestamp>
```

## Verdicts

| Verdict | Score | Meaning |
|---------|-------|---------|
| PROMOTE | `< 0.30` | Little overlap with durable guidance |
| AMBIGUOUS | `0.30` to `< 0.90` | Partial overlap requiring review |
| PRESENT | `>= 0.90` | Strongly represented in one durable guidance file |

The score combines distinctive-token overlap with exact quoted-phrase matches. It is a triage heuristic, not a semantic equivalence check.

## Tests

```bash
cd ~/.copilot/skills/memory-sweep
python3 tests.py
```
