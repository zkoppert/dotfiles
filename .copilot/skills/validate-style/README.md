# validate-style skill

Text linter for Zack's writing-style hard rules. Used by the Copilot CLI to catch
mechanical violations before any text ships to GitHub, Slack, email, gists, or
other external surfaces.

See [`SKILL.md`](./SKILL.md) for the description that drives skill discovery and
[`lint.py`](./lint.py) for the linter itself.

## Install

This skill is auto-installed by the dotfiles `install.sh`, which symlinks
`.copilot/skills/validate-style/` into `~/.copilot/skills/validate-style/`. The
Copilot CLI picks up everything in `~/.copilot/skills/` on every session.

## Run manually

```bash
python3 ~/.copilot/skills/validate-style/lint.py path/to/file.md
cat draft.md | python3 ~/.copilot/skills/validate-style/lint.py
python3 ~/.copilot/skills/validate-style/lint.py --json file.md
```

## Run the tests

```bash
python3 ~/.copilot/skills/validate-style/tests.py
```

## Add a new rule

1. Add the regex and message to `RULES` in `lint.py`.
2. Add at least one positive and one negative test in `tests.py`.
3. Document the rule in the table at the top of `SKILL.md`.
4. If the rule comes from a new user directive, also add it to the **Writing
   Style > Hard Rules** section of `.github/copilot-instructions.md`.

Keep regexes narrow. The skill loses value if it produces false positives that
the agent learns to ignore.
