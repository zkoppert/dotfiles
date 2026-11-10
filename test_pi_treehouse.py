#!/usr/bin/env python3
"""Behavior tests for the Pi Treehouse launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


WRAPPER = Path(__file__).with_name("bin") / "pi-treehouse"


class PiTreehouseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.worktree = self.root / "treehouse" / "1" / "repo"
        self.worktree.mkdir(parents=True)
        self.treehouse_log = self.root / "treehouse.log"
        self.fake_pi = self.root / "fake-pi"
        self.fake_pi.write_text(
            "#!/bin/bash\nprintf 'cwd=%s\\n' \"$PWD\"\nprintf 'args=%s\\n' \"$*\"\n",
            encoding="utf-8",
        )
        self.fake_pi.chmod(0o755)
        self.write_treehouse(managed=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_treehouse(self, *, managed: bool) -> None:
        status = (
            [{"name": "1", "path": str(self.repo), "status": "you're here"}]
            if managed
            else []
        )
        allocation = {
            "path": str(self.worktree),
            "lease_id": "lease-123",
            "lease_holder": "holder-from-fake",
            "leased_at": "2026-01-01T00:00:00Z",
            "base_branch": "main",
        }
        self.fake_status = json.dumps(status)
        self.fake_allocation = json.dumps(allocation)
        script = self.bin / "treehouse"
        script.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$*\" >> {self.treehouse_log!s}\n"
            "case \"${1:-}\" in\n"
            "  status) printf '%s\\n' \"$TREEHOUSE_FAKE_STATUS\" ;;\n"
            "  get) printf '%s\\n' \"$TREEHOUSE_FAKE_ALLOCATION\" ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    def run_wrapper(self, *args: str, overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("PI_TREEHOUSE_BYPASS", None)
        env.update(
            HOME=str(self.home),
            USER="fixture",
            PI_REAL_PI=str(self.fake_pi),
            TREEHOUSE_FAKE_STATUS=self.fake_status,
            TREEHOUSE_FAKE_ALLOCATION=self.fake_allocation,
        )
        env.update(overrides or {})
        return subprocess.run(
            [str(WRAPPER), *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def test_new_repository_session_runs_in_durable_lease(self) -> None:
        result = self.run_wrapper("implement", "feature")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cwd={self.worktree}", result.stdout)
        self.assertIn("args=implement feature", result.stdout)
        self.assertIn("get --lease --lease-holder", self.treehouse_log.read_text())
        self.assertIn("lease-123", result.stderr)
        self.assertIn("Treehouse lease retained", result.stderr)

    def test_administrative_command_bypasses_treehouse(self) -> None:
        result = self.run_wrapper("--version")

        self.assertEqual(result.returncode, 0)
        self.assertIn(f"cwd={self.repo.resolve()}", result.stdout)
        self.assertFalse(self.treehouse_log.exists())

    def test_explicit_session_resume_bypasses_treehouse(self) -> None:
        result = self.run_wrapper("--resume=session-123")

        self.assertEqual(result.returncode, 0)
        self.assertIn(f"cwd={self.repo.resolve()}", result.stdout)
        self.assertFalse(self.treehouse_log.exists())

    def test_path_lookup_skips_the_shim_and_survives_relocation(self) -> None:
        (self.bin / "pi").symlink_to(WRAPPER.resolve())
        later = self.repo / "npm-bin"
        later.mkdir()
        (later / "pi").symlink_to(self.fake_pi)
        for directory in (str(later), "npm-bin"):
            with self.subTest(directory=directory):
                result = self.run_wrapper("work", overrides={
                    "PI_REAL_PI": "",
                    "PATH": f"{self.bin}{os.pathsep}{directory}{os.pathsep}{os.environ['PATH']}",
                })

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"cwd={self.worktree}", result.stdout)
                self.assertIn("args=work", result.stdout)

    def test_missing_or_recursive_real_pi_stops_before_treehouse(self) -> None:
        shim = self.bin / "pi"
        shim.symlink_to(WRAPPER.resolve())
        for configured in ("", str(shim), str(self.root / "missing-pi")):
            with self.subTest(configured=configured):
                result = self.run_wrapper("work", overrides={
                    "PI_REAL_PI": configured, "PATH": str(self.bin),
                })

                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertIn("real Pi not found", result.stderr)
                self.assertFalse(self.treehouse_log.exists())

    def test_existing_managed_slot_does_not_nest(self) -> None:
        self.write_treehouse(managed=True)

        result = self.run_wrapper("work")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cwd={self.repo.resolve()}", result.stdout)
        log = self.treehouse_log.read_text()
        self.assertIn("status --json", log)
        self.assertNotIn("get --lease", log)


if __name__ == "__main__":
    unittest.main()
