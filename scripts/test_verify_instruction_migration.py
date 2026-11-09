#!/usr/bin/env python3
"""Tests for instruction migration preservation."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.verify_instruction_migration import (
    parse_h2_blocks,
    update_baseline,
    validate,
    verify_source,
)


class InstructionMigrationTest(unittest.TestCase):
    """Reject content loss and broken skill routing."""

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

        self.assertEqual(validate(self.root), [])

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
            "global routing table does not name skill: engineering-practices",
            validate(self.root),
        )

    def test_invalid_skill_frontmatter_is_rejected(self) -> None:
        skill = self.root / ".copilot/skills/engineering-practices/SKILL.md"
        skill.write_text(skill.read_text().replace("name: engineering-practices", "name: wrong-name", 1))

        self.assertTrue(
            any("does not match directory" in error for error in validate(self.root))
        )

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
        with self.assertRaisesRegex(ValueError, "explicit guidance-change acknowledgement"):
            update_baseline(self.root, self.manifest_path)

    def test_update_baseline_accepts_intentional_guidance_edit(self) -> None:
        skill = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        skill.write_text(
            skill.read_text().replace(
                "Always create PRs as draft",
                "Always open PRs as draft",
                1,
            )
        )

        changed = update_baseline(self.root, self.manifest_path, acknowledged=True)

        self.assertIn("baseline:Pull Requests", changed)

        self.assertEqual(validate(self.root), [])
        baseline = self.root / self.load_manifest()["baseline"]["path"]
        baseline_text = baseline.read_text()
        self.assertIn("Always open PRs as draft", baseline_text)
        self.assertNotIn("migrated baseline block byte-verifiable", baseline_text)

    def test_fake_h2_inside_fence_does_not_split_blocks(self) -> None:
        blocks = parse_h2_blocks(
            "# Header\n\n## Real\n\n```markdown\n## Example\n```\n\nText\n"
        )

        self.assertEqual([name for name, _ in blocks], ["preamble", "Real"])

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

    def test_changed_external_source_is_rejected(self) -> None:
        source = self.root / "source.md"
        source.write_text("changed\n")

        errors = verify_source(self.load_manifest(), source)

        self.assertEqual(len(errors), 1)
        self.assertIn("source SHA-256 changed", errors[0])


if __name__ == "__main__":
    unittest.main()
