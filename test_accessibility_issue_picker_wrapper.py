#!/usr/bin/env python3
"""Regression tests for the accessibility issue picker wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class AccessibilityIssuePickerWrapperTest(unittest.TestCase):
    """Exercise private configuration loading before Python starts."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.bin_dir = self.root / "repo" / "bin"
        self.scripts_dir = self.root / "repo" / "scripts"
        self.bin_dir.mkdir(parents=True)
        self.scripts_dir.mkdir()
        source = Path(__file__).with_name("bin") / "accessibility-issue-picker"
        self.wrapper = self.bin_dir / "accessibility-issue-picker"
        shutil.copy2(source, self.wrapper)
        self.wrapper.chmod(0o755)
        (self.scripts_dir / "accessibility_issue_picker.py").write_text(
            "import os, sys\n"
            "print(os.environ['ACCESSIBILITY_ISSUE_REPO'])\n"
            "print(os.environ['ACCESSIBILITY_LABELS'])\n"
            "print(' '.join(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)

    def test_missing_configuration_fails_before_python_starts(self) -> None:
        result = subprocess.run(
            [str(self.wrapper), "--dry-run"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing config file", result.stderr)

    def test_configuration_is_exported_and_arguments_are_forwarded(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            'ACCESSIBILITY_ISSUE_REPO="example/project"\n'
            'ACCESSIBILITY_LABELS="accessibility,label two"\n',
            encoding="utf-8",
        )
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "--dry-run"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.stdout.splitlines(),
            ["example/project", "accessibility,label two", "--dry-run"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
