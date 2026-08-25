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
        self.home.mkdir()
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
            "if '--validate-config' in sys.argv:\n"
            "    required = ('ACCESSIBILITY_ISSUE_REPO', 'ACCESSIBILITY_AUDIT_REPO', "
            "'ACCESSIBILITY_LABELS', 'ACCESSIBILITY_ASSIGNEE', "
            "'ACCESSIBILITY_GITHUB_TOKEN')\n"
            "    if any(not os.environ.get(name) for name in required):\n"
            "        raise SystemExit(1)\n"
            "    raise SystemExit(0)\n"
            "print(os.environ['ACCESSIBILITY_ISSUE_REPO'])\n"
            "print(os.environ['ACCESSIBILITY_LABELS'])\n"
            "print(os.environ['GH_TOKEN'])\n"
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
            'ACCESSIBILITY_AUDIT_REPO="example/audits"\n'
            'ACCESSIBILITY_LABELS="accessibility,label two"\n'
            'ACCESSIBILITY_ASSIGNEE="zkoppert"\n'
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
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
            [
                "example/project",
                "accessibility,label two",
                "repository-scoped-token",
                "--dry-run",
            ],
        )

    def test_partial_configuration_fails_validation(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            'ACCESSIBILITY_ISSUE_REPO="example/project"\n'
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "--validate-config"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)

    def test_configuration_output_is_suppressed(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            "set -x\n"
            "printf 'github_pat_SECRET\\n' >&2\n"
            'ACCESSIBILITY_ISSUE_REPO="example/project"\n'
            'ACCESSIBILITY_AUDIT_REPO="example/audits"\n'
            'ACCESSIBILITY_LABELS="accessibility"\n'
            'ACCESSIBILITY_ASSIGNEE="zkoppert"\n'
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "--validate-config"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertNotIn("github_pat_SECRET", result.stderr)
        self.assertNotIn("repository-scoped-token", result.stderr)

    def test_malformed_configuration_does_not_echo_contents(self) -> None:
        config = self.root / "picker.env"
        config.write_text("github_pat_SECRET\n", encoding="utf-8")
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "--validate-config"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("github_pat_SECRET", result.stderr)
        self.assertIn("failed to load config file", result.stderr)

    def test_intermediate_configuration_failure_is_not_masked(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            "false\n"
            'ACCESSIBILITY_ISSUE_REPO="example/project"\n'
            'ACCESSIBILITY_AUDIT_REPO="example/audits"\n'
            'ACCESSIBILITY_LABELS="accessibility"\n'
            'ACCESSIBILITY_ASSIGNEE="zkoppert"\n'
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "--validate-config"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("failed to load config file", result.stderr)

    def test_readable_configuration_is_not_sourced(self) -> None:
        config = self.root / "picker.env"
        sentinel = self.home / "config-executed"
        config.write_text(f'touch "{sentinel}"\n', encoding="utf-8")
        config.chmod(0o644)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "--validate-config"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(sentinel.exists())
        self.assertIn("config file must have mode 0600", result.stderr)


class ResumeAccessibilitySessionWrapperTest(unittest.TestCase):
    """Exercise scoped configuration loading before a session resumes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin_dir = self.root / "repo" / "bin"
        self.scripts_dir = self.root / "repo" / "scripts"
        self.bin_dir.mkdir(parents=True)
        self.scripts_dir.mkdir()
        source = Path(__file__).with_name("bin") / "resume-accessibility-session"
        self.wrapper = self.bin_dir / "resume-accessibility-session"
        shutil.copy2(source, self.wrapper)
        self.wrapper.chmod(0o755)
        validator = self.bin_dir / "accessibility-issue-picker"
        validator.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = --validate-config ]\n"
            "[ -n \"${ACCESSIBILITY_GITHUB_TOKEN:-}\" ]\n",
            encoding="utf-8",
        )
        validator.chmod(0o755)
        (self.scripts_dir / "resume_accessibility_session.py").write_text(
            "import os, sys\n"
            "print(os.environ['ACCESSIBILITY_PICKER_CONFIG'])\n"
            "print(os.environ['ACCESSIBILITY_GITHUB_TOKEN'])\n"
            "print(' '.join(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)

    def test_configuration_is_validated_and_arguments_are_forwarded(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "42", "--print-command"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.stdout.splitlines(),
            [str(config), "repository-scoped-token", "42 --print-command"],
        )

    def test_invalid_configuration_stops_before_python(self) -> None:
        config = self.root / "picker.env"
        config.write_text("", encoding="utf-8")
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "42"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_configuration_output_is_suppressed(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            "set -x\n"
            "printf 'github_pat_SECRET\\n' >&2\n"
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "42", "--print-command"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertNotIn("github_pat_SECRET", result.stderr)
        self.assertNotIn("repository-scoped-token", result.stderr)

    def test_intermediate_configuration_failure_is_not_masked(self) -> None:
        config = self.root / "picker.env"
        config.write_text(
            "false\n"
            'ACCESSIBILITY_GITHUB_TOKEN="repository-scoped-token"\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "42"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("failed to load config file", result.stderr)

    def test_readable_configuration_is_not_sourced(self) -> None:
        config = self.root / "picker.env"
        sentinel = self.home / "config-executed"
        config.write_text(f'touch "{sentinel}"\n', encoding="utf-8")
        config.chmod(0o644)
        self.env["ACCESSIBILITY_PICKER_CONFIG"] = str(config)

        result = subprocess.run(
            [str(self.wrapper), "42"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(sentinel.exists())
        self.assertIn("config file must have mode 0600", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
