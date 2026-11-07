#!/usr/bin/env python3
"""Behavior tests for the Dependabot triage launchd wrapper."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TriageDependabotWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.log = self.root / "gh.log"
        self.gh = self.root / "gh"
        self.gh.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$WRAPPER_TEST_LOG\"\nexit \"${WRAPPER_TEST_EXIT:-0}\"\n",
            encoding="utf-8",
        )
        self.gh.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.root / "home"),
                "TRIAGE_DEPENDABOT_GH": str(self.gh),
                "WRAPPER_TEST_LOG": str(self.log),
            }
        )
        self.wrapper = Path(__file__).with_name("bin") / "triage-dependabot"

    def run_wrapper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.wrapper), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dynamic_dependencies_are_resolved_through_cfs(self) -> None:
        result = self.run_wrapper("--dry-run")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "cfs -- /opt/homebrew/bin/uv run --no-project "
            "--python /opt/homebrew/bin/python3 --with PyYAML==6.0.3 "
            "--with ruamel.yaml==0.18.16 -- "
            f"{self.env['HOME']}/repos/dotfiles/.copilot/skills/triage-dependabot/triage_dependabot.py --dry-run",
            self.log.read_text(encoding="utf-8").strip(),
        )

    def test_cfs_failure_is_preserved(self) -> None:
        self.env["WRAPPER_TEST_EXIT"] = "42"

        result = self.run_wrapper()

        self.assertEqual(42, result.returncode)


if __name__ == "__main__":
    unittest.main()
