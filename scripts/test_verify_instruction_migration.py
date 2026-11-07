#!/usr/bin/env python3
"""Tests for instruction migration preservation."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.verify_instruction_migration import parse_h2_blocks, validate, verify_source


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

    def test_changed_baseline_is_rejected(self) -> None:
        baseline = self.root / self.load_manifest()["baseline"]["path"]
        baseline.write_text(baseline.read_text() + "unexpected\n")

        self.assertIn("baseline SHA-256 does not match the manifest", validate(self.root))

    def test_deleted_destination_block_is_rejected(self) -> None:
        destination = self.root / ".copilot/skills/pull-request-workflow/SKILL.md"
        destination.write_text(destination.read_text().replace("## Pull Requests", "## PR rules", 1))

        errors = validate(self.root)

        self.assertTrue(any("Pull Requests appears 0 times" in error for error in errors))

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

    def test_fake_h2_inside_fence_does_not_split_blocks(self) -> None:
        blocks = parse_h2_blocks(
            "# Header\n\n## Real\n\n```markdown\n## Example\n```\n\nText\n"
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
