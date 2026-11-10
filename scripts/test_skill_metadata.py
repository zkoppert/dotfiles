#!/usr/bin/env python3
"""Exercise installed catalog YAML and invocation contracts with Pi's SDK."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.skill_metadata import skill_metadata_errors


class SkillMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("pi") is None or shutil.which("node") is None:
            self.fail("Pi and Node.js are required for client metadata parsing")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_skill(self, directory: str, content: str) -> Path:
        path = self.root / directory / "SKILL.md"
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")
        return path

    def test_catalog_metadata_accepts_nested_maps_and_yaml_scalars(self) -> None:
        catalog = (
            "---\ndescription: >-\n  Handle GitHub operations\n  with the catalog CLI.\n"
            "metadata:\n  github-path: skills/gh-axi\n  github-ref: refs/heads/main\n"
            "  github-repo: https://example.invalid/catalog\n"
            "  hermes:\n    tags: [github, git, ci]\n    category: devops\n"
            "name: gh-axi\ndisable-model-invocation: false # Available to agents\n---\n# Skill\n"
        )
        quoted = (
            "---\nname: 'gh-axi'\ndescription: \"A description: with punctuation.\"\n"
            "metadata: {catalog: {tags: [git]}}\n---\n# Skill\n"
        )
        paths = [
            self.write_skill("nested", catalog),
            self.write_skill("quoted", quoted),
            self.write_skill("crlf", "\ufeff" + catalog.replace("\n", "\r\n")),
        ]
        originals = {path: path.read_bytes() for path in paths}

        errors = skill_metadata_errors({path: "gh-axi" for path in paths})

        self.assertEqual(errors, {str(path): [] for path in paths})
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_nested_fields_cannot_replace_required_top_level_fields(self) -> None:
        cases = (
            ("metadata:\n  name: gh-axi\ndescription: Valid", ["declares the wrong skill name"]),
            ("name: gh-axi\nmetadata:\n  description: Nested only", ["has no skill description"]),
            ("name: gh-axi\ndescription: 42", ["has no skill description"]),
            ("name: gh-axi\ndescription: '   '", ["has no skill description"]),
            (
                "name: gh-axi\ndescription: Valid\ndisable-model-invocation: true\nmetadata:\n  disable-model-invocation: false",
                ["is unavailable for model invocation"],
            ),
            (
                "name: gh-axi\ndescription: Valid\ndisable-model-invocation: 'false'",
                ["is unavailable for model invocation"],
            ),
            ("name: gh-axi\ndescription: [invalid", ["has invalid skill metadata"]),
            ("name: gh-axi\nname: gh-axi\ndescription: Valid", ["has invalid skill metadata"]),
            ("- gh-axi\n- description", ["has invalid skill metadata"]),
        )
        expected = {}
        inputs = {}
        for index, (header, errors) in enumerate(cases):
            path = self.write_skill(str(index), f"---\n{header}\n---\n# Skill\n")
            inputs[path] = "gh-axi"
            expected[str(path)] = errors
        missing = self.root / "missing" / "SKILL.md"
        inputs[missing] = "gh-axi"
        expected[str(missing)] = ["has invalid skill metadata"]

        self.assertEqual(skill_metadata_errors(inputs), expected)
        self.assertFalse(missing.exists())

    def test_cli_reports_invalid_metadata_without_disclosing_values(self) -> None:
        content = "---\nname: gh-axi\ndescription: Valid\nmetadata: [fixture-secret\n---\n"
        path = self.write_skill("invalid", content)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("skill_metadata.py")), str(path), "gh-axi"],
            capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gh-axi has invalid skill metadata", result.stderr)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_suite_fails_when_required_tools_are_unavailable(self) -> None:
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        result = subprocess.run(
            [
                sys.executable, "-m", "unittest",
                "scripts.test_skill_metadata.SkillMetadataTest.test_catalog_metadata_accepts_nested_maps_and_yaml_scalars",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PATH": str(empty_bin)},
            capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Pi and Node.js are required for client metadata parsing", result.stderr)
        self.assertNotIn("skipped=", result.stderr)

    def test_unavailable_parser_does_not_accept_catalog_metadata(self) -> None:
        path = self.write_skill("valid", "---\nname: gh-axi\ndescription: Valid\n---\n")
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("skill_metadata.py")), str(path), "gh-axi"],
            env={**os.environ, "PATH": str(empty_bin)},
            capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Pi executable is missing from PATH", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
