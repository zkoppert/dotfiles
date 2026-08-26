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
            "with open(os.path.join(os.environ['HOME'], 'args.json'), 'w', encoding='utf-8') as output:\n"
            "    json.dump(sys.argv[1:], output)\n",
            encoding="utf-8",
        )
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)

    def test_forwards_arguments_without_adding_deployment_flags(self) -> None:
        subprocess.run(
            [str(self.wrapper), "--dry-run", "--owner", "example-org"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        args = json.loads((self.home / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args, ["--dry-run", "--owner", "example-org"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
