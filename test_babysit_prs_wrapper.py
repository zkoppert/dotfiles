#!/usr/bin/env python3
"""Regression tests for the babysit-prs launchd wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class BabysitPrsWrapperTest(unittest.TestCase):
    """Exercise the wrapper with an isolated home directory."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()

        source = Path(__file__).with_name("bin") / "babysit-prs"
        self.wrapper = self.root / "babysit-prs"
        self.wrapper.write_bytes(source.read_bytes())
        self.wrapper.chmod(0o755)

        companion = self.home / "repos" / "babysit-prs" / "babysit_prs.py"
        companion.parent.mkdir(parents=True)
        companion.write_text(
            "import json, os, sys\n"
            "if '--help' in sys.argv:\n"
            "    print('--review-lab-repo OWNER/REPO --preview-repo OWNER/REPO')\n"
            "    raise SystemExit(0)\n"
            "with open(os.path.join(os.environ['HOME'], 'args.json'), 'w', encoding='utf-8') as output:\n"
            "    json.dump(sys.argv[1:], output)\n",
            encoding="utf-8",
        )
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)

    def test_config_maps_repositories_to_explicit_targets(self) -> None:
        config = self.home / ".config" / "babysit-prs" / "review-environments"
        config.parent.mkdir(parents=True)
        config.write_text(
            "# target owner/repo\n"
            "review-lab example-org/backend\n"
            "preview example-org/frontend\n",
            encoding="utf-8",
        )

        subprocess.run(
            [str(self.wrapper), "--dry-run"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        args = json.loads((self.home / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(
            args,
            [
                "--review-lab-repo",
                "example-org/backend",
                "--preview-repo",
                "example-org/frontend",
                "--dry-run",
            ],
        )

    def test_missing_config_runs_without_target_arguments(self) -> None:
        subprocess.run(
            [str(self.wrapper), "--dry-run"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        args = json.loads((self.home / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args, ["--dry-run"])

    def test_final_config_line_without_newline_is_loaded(self) -> None:
        config = self.home / ".config" / "babysit-prs" / "review-environments"
        config.parent.mkdir(parents=True)
        config.write_text("preview example-org/frontend", encoding="utf-8")

        subprocess.run(
            [str(self.wrapper)],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        args = json.loads((self.home / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args, ["--preview-repo", "example-org/frontend"])

    def test_invalid_config_fails_before_running_companion(self) -> None:
        config = self.home / ".config" / "babysit-prs" / "review-environments"
        config.parent.mkdir(parents=True)
        config.write_text("unknown example-org/backend\n", encoding="utf-8")

        result = subprocess.run(
            [str(self.wrapper)],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unsupported review environment target", result.stderr)
        self.assertFalse((self.home / "args.json").exists())

    def test_incompatible_companion_skips_config_but_still_runs(self) -> None:
        config = self.home / ".config" / "babysit-prs" / "review-environments"
        config.parent.mkdir(parents=True)
        config.write_text("preview example-org/frontend\n", encoding="utf-8")
        companion = self.home / "repos" / "babysit-prs" / "babysit_prs.py"
        companion.write_text(
            "import json, os, sys\n"
            "if '--help' in sys.argv:\n"
            "    print('legacy help')\n"
            "    raise SystemExit(0)\n"
            "with open(os.path.join(os.environ['HOME'], 'args.json'), 'w', encoding='utf-8') as output:\n"
            "    json.dump(sys.argv[1:], output)\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [str(self.wrapper), "--dry-run"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Skipping review environment config", result.stderr)
        args = json.loads((self.home / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args, ["--dry-run"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
