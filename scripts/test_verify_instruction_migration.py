#!/usr/bin/env python3
"""Tests for instruction migration preservation."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_instruction_migration import parse_h2_blocks, update_baseline, validate


class InstructionMigrationTest(unittest.TestCase):
    """Reject content loss and broken skill routing."""

    APPROVED_ROUTES = {
        "any file mutation or code, test, configuration, architecture, or system-design review": ["engineering-practices"],
        "any plan, code, security, self, or PR-description review": ["code-review-workflow"],
        "any PR operation or PR-bound draft": ["pull-request-workflow"],
        "any GitHub operation": ["gh-axi", "github-operations"],
        "any workflow or action-metadata change or review": ["actions-security"],
        "work in the named open-source Action repositories": ["open-source-actions-maintenance"],
        "recurring reports, retrospectives, handoffs, or performance feedback": ["code-review-workflow"],
        "filtering activity data for a report": ["actions-security"],
        "spreadsheet or generated-report work": ["spreadsheet-report-generation"],
    }

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        source_root = Path(__file__).resolve().parents[1]
        for relative in (
            ".github/copilot-instructions.md",
            ".copilot/skills",
            "tests/fixtures/instruction-migration",
        ):
            source = source_root / relative
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        self.manifest_path = (
            self.root / "tests/fixtures/instruction-migration/manifest.json"
        )

    def load_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text())

    def save_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    def migration_state(self) -> tuple[bytes, bytes]:
        baseline = self.root / self.load_manifest()["baseline"]["path"]
        return baseline.read_bytes(), self.manifest_path.read_bytes()

    def edit_pull_request_guidance(self) -> None:
        skill = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        skill.write_text(
            skill.read_text().replace("Always create PRs as draft", "Always open PRs as draft", 1)
        )

    def run_rebaseline(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("verify_instruction_migration.py")),
                "--root", str(self.root),
                "--update-baseline", "--acknowledge-guidance-changes",
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_current_migration_is_complete(self) -> None:
        self.assertEqual(validate(self.root), [])

    def test_unrelated_h1_only_skill_is_accepted(self) -> None:
        skill = self.root / ".copilot/skills/direct-instructions/SKILL.md"
        skill.parent.mkdir()
        skill.write_text(
            "---\nname: direct-instructions\n"
            "description: Use for direct instruction fixtures.\n---\n"
            "# Direct instructions\n\nRead the input before editing.\n",
            encoding="utf-8",
        )

        before = self.migration_state()
        self.assertEqual(validate(self.root), [])

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.migration_state(), before)

    def test_h1_only_baseline_is_rejected(self) -> None:
        manifest = self.load_manifest()
        baseline = self.root / manifest["baseline"]["path"]
        content = "# Baseline\n\nRead the input before editing.\n"
        baseline.write_text(content, encoding="utf-8")
        manifest["baseline"]["sha256"] = hashlib.sha256(content.encode()).hexdigest()
        self.save_manifest(manifest)

        self.assertEqual(validate(self.root), ["baseline contains no H2 headings"])
        with self.assertRaisesRegex(ValueError, "contains no H2 headings"):
            update_baseline(self.root, self.manifest_path, acknowledged=True)

    def test_h1_only_migration_destination_is_rejected(self) -> None:
        destination = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        destination.write_text(
            destination.read_text().replace("## Pull Requests", "# Pull Requests", 1)
        )

        self.assertIn(
            "runtime instructions cannot be parsed in "
            ".copilot/skills/pull-request-workflow/SKILL.md: baseline contains no H2 headings",
            validate(self.root),
        )
        with self.assertRaisesRegex(ValueError, "contains no H2 headings"):
            update_baseline(self.root, self.manifest_path, acknowledged=True)

    def test_h1_only_runtime_contract_is_rejected(self) -> None:
        relative_path = ".copilot/skills/direct-instructions/SKILL.md"
        skill = self.root / relative_path
        skill.parent.mkdir()
        content = (
            "---\nname: direct-instructions\n"
            "description: Use for direct instruction fixtures.\n---\n"
            "# Direct instructions\n\nRead the input before editing.\n"
        )
        skill.write_text(content, encoding="utf-8")
        manifest = self.load_manifest()
        manifest["runtime_contracts"].append(
            {
                "id": "Direct instructions",
                "destination": relative_path,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
        self.save_manifest(manifest)

        errors = validate(self.root)

        self.assertIn(
            f"runtime instructions cannot be parsed in {relative_path}: baseline contains no H2 headings",
            errors,
        )
        self.assertIn(
            f"runtime contracts cannot be parsed in {relative_path}: baseline contains no H2 headings",
            errors,
        )

    def test_changed_baseline_is_rejected(self) -> None:
        baseline = self.root / self.load_manifest()["baseline"]["path"]
        baseline.write_text(baseline.read_text() + "unexpected\n")

        self.assertIn("baseline SHA-256 does not match the manifest", validate(self.root))

    def test_deleted_destination_block_is_rejected(self) -> None:
        destination = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        destination.write_text(destination.read_text().replace("## Pull Requests", "## PR rules", 1))

        errors = validate(self.root)

        self.assertTrue(any("Pull Requests has 0 structural matches" in error for error in errors))

    def test_missing_global_route_is_rejected(self) -> None:
        global_path = self.root / ".github/copilot-instructions.md"
        global_path.write_text(global_path.read_text().replace("`engineering-practices`", "engineering guidance"))

        self.assertIn(
            "pre-action routing must read engineering-practices before any file mutation or code, "
            "test, configuration, architecture, or system-design review",
            validate(self.root),
        )

    def test_rebaseline_cannot_ratify_a_missing_pre_action_route(self) -> None:
        before = self.migration_state()
        global_path = self.root / ".github/copilot-instructions.md"
        global_text = global_path.read_text(encoding="utf-8")
        global_path.write_text(
            global_text.replace(
                "- Before any file mutation or code, test, configuration, architecture, "
                "or system-design review, read `engineering-practices` in full.\n",
                "",
                1,
            ),
            encoding="utf-8",
        )

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "pre-action routing must read engineering-practices before any file mutation or code, "
            "test, configuration, architecture, or system-design review",
            result.stderr,
        )
        self.assertEqual(self.migration_state(), before)

    def test_manifest_preserves_the_approved_action_contract(self) -> None:
        self.assertEqual(self.load_manifest()["pre_action_routes"], self.APPROVED_ROUTES)

    def test_rebaseline_cannot_swap_review_and_mutation_routes(self) -> None:
        before = self.migration_state()
        global_path = self.root / ".github/copilot-instructions.md"
        global_path.write_text(
            global_path.read_text().replace(
                "review, read `engineering-practices` in full.",
                "review, read `code-review-workflow` in full.",
                1,
            ).replace(
                "PR-description review, read `code-review-workflow` in full.",
                "PR-description review, read `engineering-practices` in full.",
                1,
            )
        )

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("must read engineering-practices before any file mutation", result.stderr)
        self.assertIn("must read code-review-workflow before any plan", result.stderr)
        self.assertEqual(self.migration_state(), before)

    def test_each_skill_requires_its_own_read_in_the_required_order(self) -> None:
        before = self.migration_state()
        global_path = self.root / ".github/copilot-instructions.md"
        original = global_path.read_text()
        for replacement in (
            "read `gh-axi` first and use `github-operations` second",
            "use `gh-axi` first and read `github-operations` second",
            "read `github-operations` first and read `gh-axi` second",
            "do not read `gh-axi` first and read `github-operations` second",
        ):
            with self.subTest(instruction=replacement):
                global_path.write_text(original.replace(
                    "read `gh-axi` first and read `github-operations` second", replacement, 1,
                ))

                result = self.run_rebaseline()

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("must read gh-axi, github-operations before any GitHub operation", result.stderr)
                self.assertEqual(self.migration_state(), before)

    def test_rebaseline_cannot_drop_workflow_reviews(self) -> None:
        before = self.migration_state()
        global_path = self.root / ".github/copilot-instructions.md"
        global_path.write_text(global_path.read_text().replace(
            "workflow or action-metadata change or review", "workflow or action-metadata change", 1,
        ).replace("changing or reviewing workflow files", "changing workflow files", 1))

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("must read actions-security before any workflow or action-metadata change or review", result.stderr)
        self.assertEqual(self.migration_state(), before)

    def test_inactive_bullets_cannot_satisfy_any_action_contract(self) -> None:
        before = self.migration_state()
        global_path = self.root / ".github/copilot-instructions.md"
        original = global_path.read_text()
        for trigger, names in self.APPROVED_ROUTES.items():
            instruction = (
                f"read `{names[0]}` first and read `{names[1]}` second"
                if len(names) == 2 else f"read `{names[0]}` in full"
            )
            if trigger == "filtering activity data for a report":
                instruction = "also " + instruction
            bullet = f"- Before {trigger}, {instruction}."
            for opening, closing in (
                ("```markdown\n", "\n```"),
                ("~~~markdown\n", "\n~~~"),
                ("````markdown\n```\n", "\n```\n````"),
                ("<!--\n", "\n-->"),
                ("<!-- ", " -->"),
            ):
                with self.subTest(trigger=trigger, opening=opening):
                    global_path.write_text(original.replace(bullet, opening + bullet + closing, 1))

                    errors = validate(self.root)
                    result = self.run_rebaseline()

                    diagnostic = f"pre-action routing must read {', '.join(names)} before {trigger}"
                    self.assertIn(diagnostic, errors)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertEqual(self.migration_state(), before)

    def test_rebaseline_accepts_equivalent_route_spacing_and_case(self) -> None:
        global_path = self.root / ".github/copilot-instructions.md"
        global_path.write_text(global_path.read_text().replace(
            "any GitHub operation, read `gh-axi` first and read `github-operations` second",
            "any  GITHUB  operation, READ `gh-axi` first  and  READ `github-operations` second",
            1,
        ))

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(validate(self.root), [])

    def test_invalid_skill_frontmatter_is_rejected(self) -> None:
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        skill.write_text(skill.read_text().replace("name: engineering-practices", "name: wrong-name", 1))

        self.assertTrue(
            any("does not match directory" in error for error in validate(self.root))
        )

    def test_quoted_frontmatter_preserves_scalar_meaning(self) -> None:
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        body = skill.read_text(encoding="utf-8").split("\n---\n", 1)[1]
        for name, description, disabled in (
            (
                '"\\u0065ngineering-practices"',
                '"Use for authoring, reviewing code, configuration, \\u0061rchitecture: # literal text."',
                "false",
            ),
            (
                "'engineering-practices'",
                "'Use for authoring, reviewing code, configuration, architecture, and Zack''s reports.'",
                "False",
            ),
            (
                "engineering-practices",
                "Use for authoring, reviewing code, configuration, architecture.",
                "FALSE",
            ),
        ):
            with self.subTest(name=name, disabled=disabled):
                skill.write_text(
                    "---\n# Skill routing metadata\n"
                    f"name: {name}\ndescription: {description}\ndisable-model-invocation: {disabled}\n---\n{body}",
                    encoding="utf-8",
                )

                self.assertEqual(validate(self.root), [])

    def test_comments_cannot_supply_triggers_or_hide_disabled_invocation(self) -> None:
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        body = skill.read_text(encoding="utf-8").split("\n---\n", 1)[1]
        before = self.migration_state()
        for field, scalar in (
            ("description", "# authoring reviewing code configuration architecture"),
            ("disable-model-invocation", "true # rationale"),
        ):
            with self.subTest(field=field):
                fields = {
                    "name": "engineering-practices",
                    "description": "Use for authoring, reviewing code, configuration, architecture.",
                    "disable-model-invocation": "false",
                }
                fields[field] = scalar
                header = "\n".join(f"{key}: {value}" for key, value in fields.items())
                skill.write_text(f"---\n{header}\n---\n{body}", encoding="utf-8")

                self.assertIn(
                    f".copilot/skills/engineering-practices/SKILL.md: unsupported frontmatter scalar for {field}",
                    validate(self.root),
                )
                result = self.run_rebaseline()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.migration_state(), before)

    def test_frontmatter_requires_string_names_and_descriptions(self) -> None:
        skill = self.root / ".copilot/skills/scalar-fixture/SKILL.md"
        skill.parent.mkdir()
        for field, scalar, expected in (
            ("name", "true", "invalid skill name: True"),
            ("name", "null", "invalid skill name: None"),
            ("name", "42", ".copilot/skills/scalar-fixture/SKILL.md: unsupported frontmatter scalar for name"),
            ("description", "true", "invalid skill description for 'scalar-fixture'"),
            ("description", "FALSE", "invalid skill description for 'scalar-fixture'"),
            ("description", "null", "invalid skill description for 'scalar-fixture'"),
            ("description", "~", "invalid skill description for 'scalar-fixture'"),
            ("description", "''", "invalid skill description for 'scalar-fixture'"),
            ("description", '"   "', "invalid skill description for 'scalar-fixture'"),
            ("description", "42", ".copilot/skills/scalar-fixture/SKILL.md: unsupported frontmatter scalar for description"),
            ("description", "[valid, words]", ".copilot/skills/scalar-fixture/SKILL.md: unsupported frontmatter scalar for description"),
            ("description", "{value: words}", ".copilot/skills/scalar-fixture/SKILL.md: unsupported frontmatter scalar for description"),
        ):
            with self.subTest(field=field, scalar=scalar):
                fields = {"name": "scalar-fixture", "description": "Use for scalar fixtures."}
                fields[field] = scalar
                header = "\n".join(f"{key}: {value}" for key, value in fields.items())
                skill.write_text(f"---\n{header}\n---\n# Scalar fixture\n", encoding="utf-8")

                self.assertIn(expected, validate(self.root))

    def test_frontmatter_requires_boolean_invocation_flags(self) -> None:
        skill = self.root / ".copilot/skills/scalar-fixture/SKILL.md"
        skill.parent.mkdir()
        for scalar, expected in (
            ("false", None),
            ("False", None),
            ("FALSE", None),
            ("true", "routed skill disables model invocation: scalar-fixture"),
            ("True", "routed skill disables model invocation: scalar-fixture"),
            ("TRUE", "routed skill disables model invocation: scalar-fixture"),
            ('"true"', "invalid model invocation flag for 'scalar-fixture'"),
            ("'false'", "invalid model invocation flag for 'scalar-fixture'"),
            ("null", "invalid model invocation flag for 'scalar-fixture'"),
            ("yes", "invalid model invocation flag for 'scalar-fixture'"),
            ("0", ".copilot/skills/scalar-fixture/SKILL.md: unsupported frontmatter scalar for disable-model-invocation"),
        ):
            with self.subTest(scalar=scalar):
                skill.write_text(
                    "---\nname: scalar-fixture\ndescription: Use for scalar fixtures.\n"
                    f"disable-model-invocation: {scalar}\n---\n# Scalar fixture\n",
                    encoding="utf-8",
                )

                errors = validate(self.root)

                if expected is None:
                    self.assertEqual(errors, [])
                else:
                    self.assertIn(expected, errors)

    def test_frontmatter_rejects_duplicate_keys_and_unsupported_forms(self) -> None:
        skill = self.root / ".copilot/skills/scalar-fixture/SKILL.md"
        skill.parent.mkdir()
        base = "name: scalar-fixture\ndescription: Use for scalar fixtures.\n"
        for header, expected in (
            (base + "name: scalar-fixture\n", "duplicate frontmatter key: name"),
            (base + "description: Use for scalar fixtures.\n", "duplicate frontmatter key: description"),
            (base + "disable-model-invocation: true\ndisable-model-invocation: false\n", "duplicate frontmatter key: disable-model-invocation"),
            (base + "metadata:\n  name: ignored\n", "unsupported frontmatter mapping entry"),
            (base + "<<: *defaults\n", "unsupported frontmatter mapping entry"),
            ('"name": scalar-fixture\ndescription: Valid text\n', "unsupported frontmatter mapping entry"),
            ("name: scalar-fixture\ndescription: |\n  Valid text\n", "unsupported frontmatter scalar for description"),
            ("name: scalar-fixture\ndescription: &label Valid text\n", "unsupported frontmatter scalar for description"),
            ("name: scalar-fixture\ndescription: !!str Valid text\n", "unsupported frontmatter scalar for description"),
            ("name: scalar-fixture\ndescription: Valid text\x00\n", "unsupported frontmatter control character"),
        ):
            with self.subTest(header=header):
                skill.write_text(f"---\n{header}---\n# Scalar fixture\n", encoding="utf-8")

                self.assertIn(f".copilot/skills/scalar-fixture/SKILL.md: {expected}", validate(self.root))

    def test_missing_skill_trigger_term_is_rejected(self) -> None:
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        skill.write_text(skill.read_text().replace("architecture, system design", "system design", 1))

        self.assertIn(
            "skill description for engineering-practices lacks trigger term 'architecture'",
            validate(self.root),
        )

    def test_reordered_destination_blocks_are_rejected(self) -> None:
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        text = skill.read_text()
        blocks = dict(parse_h2_blocks(text))
        original = blocks["Code Style & Languages"] + blocks["Platform & System Design"]
        reordered = blocks["Platform & System Design"] + blocks["Code Style & Languages"]
        skill.write_text(text.replace(original, reordered, 1))

        self.assertIn(
            "baseline block order changed in .copilot/skills/engineering-practices/SKILL.md",
            validate(self.root),
        )

    def test_duplicate_block_in_another_skill_is_rejected(self) -> None:
        source = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        duplicate = dict(parse_h2_blocks(source.read_text()))["Pull Requests"]
        destination = self.root / ".copilot/skills/spreadsheet-report-generation/SKILL.md"
        destination.write_text(destination.read_text() + duplicate)

        self.assertIn(
            "Pull Requests has 2 structural matches across runtime instructions, expected 1",
            validate(self.root),
        )

    def test_changed_pre_action_routing_is_rejected(self) -> None:
        global_path = self.root / ".github/copilot-instructions.md"
        global_path.write_text(
            global_path.read_text().replace(
                "If a required skill cannot load, stop and tell me.",
                "Continue when a required skill cannot load.",
                1,
            )
        )

        self.assertIn(
            "runtime contract changed: Pre-action skill routing",
            validate(self.root),
        )

    def test_changed_global_safety_contract_is_rejected(self) -> None:
        global_path = self.root / ".github/copilot-instructions.md"
        original = global_path.read_text(encoding="utf-8")
        for rule in (
            "Never convert a ready PR to draft without my explicit request for that transition.",
            "Never approve a PR without my explicit confirmation.",
            "Create gists as private/secret unless I explicitly request a public gist.",
            "Use dedicated tokens or secrets for each workflow; never reuse them across workflows.",
        ):
            with self.subTest(rule=rule):
                global_path.write_text(original.replace(rule, "", 1), encoding="utf-8")

                self.assertIn(
                    "runtime contract changed: Pre-action skill routing",
                    validate(self.root),
                )

    def test_changed_compatibility_stub_is_rejected(self) -> None:
        global_path = self.root / ".github/copilot-instructions.md"
        global_path.write_text(
            global_path.read_text().replace(
                "Load that skill before any PR operation or PR-bound draft.",
                "Load that skill when convenient.",
                1,
            )
        )

        self.assertIn("runtime contract changed: Pull Requests", validate(self.root))

    def test_duplicate_runtime_contract_heading_is_rejected(self) -> None:
        global_path = self.root / ".github/copilot-instructions.md"
        text = global_path.read_text()
        pull_requests = dict(parse_h2_blocks(text))["Pull Requests"]
        global_path.write_text(text + "\n" + pull_requests)

        self.assertIn(
            "runtime destination has duplicate H2 identifiers: .github/copilot-instructions.md",
            validate(self.root),
        )

    def test_fenced_migrated_section_is_rejected(self) -> None:
        skill = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        text = skill.read_text()
        skill.write_text(text.replace("## Pull Requests\n", "```markdown\n## Pull Requests\n", 1) + "```\n")

        self.assertTrue(
            any("Pull Requests has 0 structural matches" in error for error in validate(self.root))
        )

    def test_update_baseline_requires_explicit_acknowledgement(self) -> None:
        before = self.migration_state()
        with self.assertRaisesRegex(ValueError, "explicit guidance-change acknowledgement"):
            update_baseline(self.root, self.manifest_path)
        self.assertEqual(self.migration_state(), before)

    def test_update_baseline_accepts_intentional_guidance_edit(self) -> None:
        self.edit_pull_request_guidance()

        changed = update_baseline(self.root, self.manifest_path, acknowledged=True)

        self.assertIn("baseline:Pull Requests", changed)

        self.assertEqual(validate(self.root), [])
        baseline = self.root / self.load_manifest()["baseline"]["path"]
        baseline_text = baseline.read_text()
        self.assertIn("Always open PRs as draft", baseline_text)
        self.assertNotIn("migrated baseline block byte-verifiable", baseline_text)

    def test_missing_contract_preserves_baseline_and_retry_reports_the_edit(self) -> None:
        before = self.migration_state()
        self.edit_pull_request_guidance()
        global_path = self.root / ".github/copilot-instructions.md"
        original = global_path.read_text(encoding="utf-8")
        global_path.write_text(original.replace("## Pull Requests\n", "## Missing contract\n", 1))

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cannot rebaseline missing runtime contract 'Pull Requests'", result.stderr)
        self.assertEqual(self.migration_state(), before)
        self.assertNotIn("Updated guidance contracts", result.stdout)

        global_path.write_text(original, encoding="utf-8")
        retried = self.run_rebaseline()

        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn("baseline:Pull Requests", retried.stdout)
        self.assertEqual(validate(self.root), [])
        self.assertIn(b"Always open PRs as draft", self.migration_state()[0])

    def test_rebaseline_validation_failure_preserves_both_files(self) -> None:
        before = self.migration_state()
        self.edit_pull_request_guidance()
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        skill.write_text(skill.read_text().replace("name: engineering-practices", "name: wrong-name", 1))

        result = self.run_rebaseline()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not match directory", result.stderr)
        self.assertEqual(self.migration_state(), before)
        self.assertNotIn("Updated guidance contracts", result.stdout)

    def test_rebaseline_replacement_failures_restore_both_files(self) -> None:
        before = self.migration_state()
        self.edit_pull_request_guidance()
        baseline = self.root / self.load_manifest()["baseline"]["path"]
        replace = Path.replace
        for failed_path in (baseline, self.manifest_path):
            for interrupted in (False, True):
                with self.subTest(path=failed_path, interrupted=interrupted):
                    injected = False

                    def fail_once(path: Path, destination: Path) -> Path:
                        nonlocal injected
                        if destination == failed_path and not injected:
                            injected = True
                            if interrupted:
                                replace(path, destination)
                                raise KeyboardInterrupt()
                            raise OSError("fixture replacement failed")
                        return replace(path, destination)

                    with patch("scripts.verify_instruction_migration.Path.replace", new=fail_once):
                        with self.assertRaises(KeyboardInterrupt if interrupted else OSError):
                            update_baseline(self.root, self.manifest_path, acknowledged=True)

                    self.assertEqual(self.migration_state(), before)
                    self.assertEqual(list(self.manifest_path.parent.glob(".instruction-migration-*")), [])

        changed = update_baseline(self.root, self.manifest_path, acknowledged=True)

        self.assertIn("baseline:Pull Requests", changed)
        self.assertEqual(validate(self.root), [])

    def test_fake_h2_inside_fence_does_not_split_blocks(self) -> None:
        blocks = parse_h2_blocks(
            "# Header\n\n## Real\n\n```markdown\n## Example\n```\n\nText\n"
        )

        self.assertEqual([name for name, _ in blocks], ["preamble", "Real"])

    def test_commented_h2_and_fences_do_not_split_active_blocks(self) -> None:
        blocks = parse_h2_blocks(
            "# Header\n\n## Real\n\n<!--\n```\n## Example\n-->\n\n## Next\nText\n"
        )

        self.assertEqual([name for name, _ in blocks], ["preamble", "Real", "Next"])

    def test_shorter_fence_does_not_close_longer_fence(self) -> None:
        blocks = parse_h2_blocks(
            "# Header\n\n## Real\n\n````markdown\n```\n## Example\n```\n````\n"
        )

        self.assertEqual([name for name, _ in blocks], ["preamble", "Real"])

    def test_closing_fence_rejects_info_string(self) -> None:
        blocks = parse_h2_blocks(
            "# Header\n\n## Real\n\n```markdown\n```not-a-close\n## Example\n```\n"
        )

        self.assertEqual([name for name, _ in blocks], ["preamble", "Real"])


if __name__ == "__main__":
    unittest.main()
