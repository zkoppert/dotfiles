#!/usr/bin/env python3
"""Behavior tests for the Dependabot triage launchd wrapper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
            '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$WRAPPER_TEST_LOG"\n'
            'if [ "${WRAPPER_TEST_STARTED:-0}" = "1" ]; then : > "$DEPENDABOT_WORKER_STARTED"; fi\n'
            'if [ "${WRAPPER_TEST_REPORTED:-0}" = "1" ]; then : > "$DEPENDABOT_FAILURE_REPORTED"; fi\n'
            'if [ "${WRAPPER_TEST_EMERGENCY:-0}" = "1" ]; then : > "$DEPENDABOT_FALLBACK_ALERT_SENT"; fi\n'
            'exit "${WRAPPER_TEST_EXIT:-0}"\n',
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

    def test_historical_command_refuses_execution_before_optional_dependencies(self):
        legacy = (
            self.wrapper.parent.parent
            / ".copilot/skills/triage-dependabot/triage_dependabot.py"
        )
        result = subprocess.run(
            [sys.executable, str(legacy), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("Use the guarded runner", result.stderr)

    def test_dynamic_dependencies_are_resolved_through_cfs(self) -> None:
        result = self.run_wrapper("--dry-run")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "cfs -- /opt/homebrew/bin/uv run --no-project "
            "--python /opt/homebrew/bin/python3 --with PyYAML==6.0.3 "
            "--with ruamel.yaml==0.18.16 -- "
            f"{self.wrapper.parent.parent}/scripts/dependabot_automerge.py --dry-run",
            self.log.read_text(encoding="utf-8").strip(),
        )

    def test_cfs_failure_is_preserved(self) -> None:
        self.env["WRAPPER_TEST_EXIT"] = "42"

        result = self.run_wrapper("--dry-run")

        self.assertEqual(42, result.returncode)

    def bootstrap_fixture(self):
        root = self.root / "installation"
        (root / "bin").mkdir(parents=True)
        target = root / "bin/triage-dependabot"
        shutil.copy2(self.wrapper, target)
        self.wrapper = target
        (root / "scripts").mkdir()
        (root / "scripts/dependabot_alerts.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['BOOTSTRAP_TEST_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
        )
        self.bootstrap_log = self.root / "bootstrap.json"
        self.env["BOOTSTRAP_TEST_LOG"] = str(self.bootstrap_log)
        self.env["WRAPPER_TEST_EXIT"] = "42"

    def test_bootstrap_failure_uses_stdlib_alert_helper_and_preserves_exit_code(self):
        self.bootstrap_fixture()
        result = self.run_wrapper("--report", str(self.root / "report.json"))
        self.assertEqual(42, result.returncode)
        self.assertEqual(
            ["--bootstrap-error", "42", "--report", str(self.root / "report.json")],
            json.loads(self.bootstrap_log.read_text()),
        )

    def test_dry_run_bootstrap_failure_never_requests_an_alert(self):
        self.bootstrap_fixture()
        result = self.run_wrapper("--dry-run")
        self.assertEqual(42, result.returncode)
        self.assertFalse(self.bootstrap_log.exists())

    def test_worker_failure_does_not_duplicate_the_workers_own_alert(self):
        self.bootstrap_fixture()
        self.env["WRAPPER_TEST_STARTED"] = "1"
        self.env["WRAPPER_TEST_REPORTED"] = "1"
        result = self.run_wrapper()
        self.assertEqual(42, result.returncode)
        self.assertFalse(self.bootstrap_log.exists())

    def test_receipt_directory_failure_still_requests_help(self):
        self.bootstrap_fixture()
        shell_env = self.root / "shell-env"
        shell_env.write_text("mktemp() { return 1; }\n")
        self.env["BASH_ENV"] = str(shell_env)
        result = self.run_wrapper()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            ["--bootstrap-error", "1"], json.loads(self.bootstrap_log.read_text())
        )
        self.assertFalse(self.log.exists())

    def test_receipt_directory_failure_stays_silent_for_dry_runs(self):
        self.bootstrap_fixture()
        shell_env = self.root / "shell-env"
        shell_env.write_text("mktemp() { return 1; }\n")
        self.env["BASH_ENV"] = str(shell_env)
        result = self.run_wrapper("--dry-run")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.bootstrap_log.exists())
        self.assertFalse(self.log.exists())

    def test_helper_startup_failure_tries_another_interpreter(self):
        self.bootstrap_fixture()
        first = Path("/opt/homebrew/bin/python3")
        later = Path("/usr/bin/python3")
        if (
            not first.exists()
            or not later.exists()
            or first.resolve() == later.resolve()
        ):
            self.skipTest(
                "This contract requires distinct Homebrew and system interpreters"
            )
        helper = self.wrapper.parent.parent / "scripts/dependabot_alerts.py"
        helper.write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "p=Path(os.environ['BOOTSTRAP_TEST_LOG'])\n"
            "seen=json.loads(p.read_text()) if p.exists() else []\n"
            "seen.append(sys.executable)\np.write_text(json.dumps(seen))\n"
            "sys.exit(1 if len(seen)==1 else 0)\n"
        )
        result = self.run_wrapper()
        attempts = json.loads(self.bootstrap_log.read_text())
        self.assertEqual(42, result.returncode)
        self.assertEqual(2, len(attempts))
        self.assertNotEqual(attempts[0], attempts[1])

    def test_emergency_delivery_receipt_suppresses_duplicate_wrapper_help(self):
        self.bootstrap_fixture()
        self.env["WRAPPER_TEST_EMERGENCY"] = "1"
        result = self.run_wrapper()
        self.assertEqual(42, result.returncode)
        self.assertFalse(self.bootstrap_log.exists())

    def test_worker_that_started_but_did_not_report_still_requests_help(self):
        self.bootstrap_fixture()
        self.env["WRAPPER_TEST_STARTED"] = "1"
        result = self.run_wrapper()
        self.assertEqual(42, result.returncode)
        self.assertEqual(
            ["--bootstrap-error", "42", "--worker-started"],
            json.loads(self.bootstrap_log.read_text()),
        )


if __name__ == "__main__":
    unittest.main()
