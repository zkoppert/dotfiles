---
name: engineering-practices
description: Use before authoring, editing, or reviewing code, tests, configuration, architecture, system design, React components, or project structure.
---

# Engineering practices

Read and follow this skill before changing or reviewing implementation, tests, configuration, architecture, or project structure.

## Code Style & Languages
- **Python** is the preferred scripting language for automation, data processing, and tooling
- Use `argparse` for CLI argument parsing in Python scripts
- Include proper error handling and logging; don't silently swallow errors
- **Prefer affirmative logic**: Use an affirmative predicate when it preserves the same semantics. Prefer `value.present?` over `!value.nil?`, and prefer named positive predicates over negated absence checks or double negatives. Keep the explicit negative form when the language lacks an equivalent predicate or the affirmative form would change behavior.
- **Comments explain why, not what** - only comment non-obvious design decisions (e.g., why one method was chosen over another, a constraint that isn't visible in the code). If the method name or variable name already communicates the intent, delete the comment. First drafts tend to over-comment; strip redundant comments before committing.
- Prefer ecosystem tools (`pip install`, `npm init`, etc.) over manual configuration
- Run `make lint` and `make test` before committing in repos that have a Makefile
- Write unit tests for new functionality
- **Test file naming, one per source module, no catch-alls**: When a repo follows a per-module test convention (e.g., `test_<module>.py` for each `<module>.py`), put new tests for a module in that module's existing test file (creating it if missing). Never create catch-all aggregator files like `test_coverage_additions.py`, `test_extra.py`, `test_misc.py`, or `test_new_stuff.py` - they hide what is being tested and break the source-to-test mapping a maintainer relies on. If a test exercises multiple modules, file it under the module whose behavior is most central to the assertion. Before adding a new `test_*.py` file, confirm there is no existing per-module test file that would be a better home.
- Document changes to environment variables in the `README.md` file
- **Linting philosophy**: When linting errors arise, **always fix the code to pass the linter** - do not suppress, ignore, or disable lint rules. Only disable a rule as a last resort if fixing the code is truly impossible or would make it significantly worse, and explain why in a comment. This applies to all linters (flake8, pylint, mypy, markdownlint, eslint, etc.).
- **Cross-reference existing patterns**: When adding new code to a file that already has similar blocks (e.g., a new job in a workflow, a new route in a router, a new test in a suite), explicitly compare the new code against the existing code for naming conventions, formatting, and runtime behavior before committing. Don't pattern-match on the name you're defining; check how existing code actually references the same concept.

### React & TypeScript Component Patterns

When writing or reviewing React components, follow these composition principles. AI agents tend to produce monolithic components with big conditionals, one render function that switches on data type, full of `if`/`else` chains and type casts. That pattern works but creates comprehension debt: adding one row type means reading the whole feature.

- **One component per concern, in its own file.** If a component renders different things based on a type discriminator (e.g., event type, action type, row variant), extract each case into a standalone component. Each should be small enough to read in one scroll.
- **Prefer components that fetch their own data from hooks** in variant-driven UIs (lists of heterogeneous items, sidebars with mixed row types). Each component reads what it needs from tanstack-query or a shared hook rather than receiving rich objects via prop drilling. This keeps components self-contained and avoids unnecessary re-renders of siblings when one subscription updates. Pure presentational components that receive already-loaded data are still fine for leaf UI.
- **Pass only primitives as props** (IDs, counts, enum values) when the child will fetch its own rich data. Props are for identity and configuration. If the parent already has the data and the child is purely presentational, passing the object directly is acceptable.
- **Return null when there's nothing to show.** If a component's data means it shouldn't render, let it decide that internally (`if (!hasData) return null`). Don't push visibility logic into the parent.
- **Container components read like a table of contents.** The parent composes children in a list; it should be obvious what the feature does by scanning the container without reading child implementations:
  ```tsx
  function SidebarActivity() {
    return (
      <>
        <CommitsRow />
        <ApprovedRow />
        <ReviewCommentsRow />
        <LabelsRow />
      </>
    )
  }
  ```
- **When to flag in reviews**: see the "React composition check" bullet in the Code Review Workflow above. This is a composability and testability concern, not a style nitpick.

## Platform & System Design
- **Design for safe contribution** - When creating new modules or features, always ask: "Could a PM or junior contributor add to this safely without understanding the whole system?" If no, the abstraction is wrong. Prefer designs where the blast radius of a bad change is small by default.
- **Explicit domain boundaries** - When adding new functionality, identify and document which module owns which concern. Don't let unrelated capabilities leak across module boundaries (e.g., notification logic shouldn't touch payment logic). Flag boundary violations in reviews.
- **Design for 10x** - When adding API calls, pagination, or data processing, always ask what happens if the input is 10x larger than today. Add rate limiting, backoff, batching, or other safeguards when measurements show a likely limit or when failure impact is blocking. Do not add speculative safeguards that create more complexity than the observed risk justifies.
- **Semantic observability** - When adding error handling, include enough context to diagnose *who* is affected and *what change* caused it, not just the raw error. Log the feature, the actor, and the deployment version. Raw stack traces are the equivalent of useless dashboards.
- **Self-healing and automatic rollback** - When designing CI/CD workflows or deployment pipelines, always include an automatic rollback path. Feature flags should be the default mechanism for shipping new behavior, so a bad change can be disabled without a redeploy.
- **Guardrails over guidelines** - Where possible, encode pattern consistency as a lint rule or CI check rather than relying on human review to catch deviations. If a requirement matters, it should be a gate, not a suggestion.
- **Assume imperfect changes** - Design systems so that a single bad commit, a hallucinated function call, or an incorrect configuration value can't cascade into a system-wide failure. Use input validation at boundaries, not just at the entry point. Treat every module boundary as a trust boundary.
- **Conflict detection over conflict prevention** - Instead of relying on contributors to avoid conflicts (e.g., DB migrations, config changes), build detection into the pipeline. Conflicting migrations should be blocked automatically. Overlapping feature flags should raise a warning.

## File & Project Organization
- Store automation scripts in a `scripts/` directory
- Use YAML config files for parameterized values (team rosters, schedules, etc.) rather than hardcoding
- Keep generated output separate from source (e.g., `january-2026/`, `february-2026/` directories)
- Add a `.gitignore` for common artifacts (`__pycache__/`, `*.pyc`, `.DS_Store`)

<!-- This boundary keeps the migrated baseline block byte-verifiable. -->
