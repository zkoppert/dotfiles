#!/usr/bin/env python3
"""Behavior tests for the CFS package command guard."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CfsPackageGuardTest(unittest.TestCase):
    """Exercise the installed shim boundary with fake downstream tools."""

    CFS_INDEX = "https://pkgs.dev.azure.com/github-cfs/GitHubCFS/_packaging/github-cfs-pypi/pypi/simple/"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.guard_dir = self.root / "guard"
        self.real_dir = self.root / "real"
        self.guard_dir.mkdir()
        self.real_dir.mkdir()
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir()
        (self.runtime_dir / "python3").symlink_to(sys.executable)
        self.guard = Path(__file__).with_name("bin") / "cfs-package-guard"
        self.log = self.root / "downstream.log"
        self.env = dict(os.environ)
        for name in tuple(self.env):
            if name.startswith(("PIP_", "UV_")):
                self.env.pop(name)
        self.env["PATH"] = os.pathsep.join((str(self.guard_dir), str(self.real_dir), str(self.runtime_dir)))
        self.env["GUARD_TEST_LOG"] = str(self.log)
        for name in ("pip", "pip3", "pip-compile", "pip-sync", "uv", "uvx"):
            (self.guard_dir / name).symlink_to(self.guard)
            downstream = self.real_dir / name
            downstream.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$0|$*\" >> \"$GUARD_TEST_LOG\"\nexit \"${GUARD_TEST_EXIT:-0}\"\n",
                encoding="utf-8",
            )
            downstream.chmod(0o755)

    def run_guard(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command_env = self.env if env is None else env
        return subprocess.run(arguments, env=command_env, capture_output=True, text=True, check=False)

    def cfs_env(self, command: str) -> dict[str, str]:
        env = dict(self.env)
        if command.startswith("pip"):
            env.update(
                {
                    "PIP_CONFIG_FILE": "/dev/null",
                    "PIP_INDEX_URL": self.CFS_INDEX,
                    "PIP_EXTRA_INDEX_URL": "",
                }
            )
        else:
            env.update(
                {
                    "UV_DEFAULT_INDEX": self.CFS_INDEX,
                    "UV_EXTRA_INDEX_URL": "",
                    "UV_INDEX_URL": "",
                    "UV_INDEX": "",
                }
            )
        return env

    def test_unwrapped_resolving_commands_are_blocked(self) -> None:
        for command in ("pip", "pip3", "pip-compile", "pip-sync", "uv", "uvx"):
            arguments = (command, "install", "example") if command.startswith("pip") else (command, "sync")
            with self.subTest(command=command):
                result = self.run_guard(*arguments)
                self.assertEqual(65, result.returncode)
                self.assertIn("blocked unwrapped package resolution", result.stderr)
        self.assertFalse(self.log.exists())

    def test_cfs_environment_allows_resolution_and_preserves_exit_status(self) -> None:
        env = self.cfs_env("pip")
        env["GUARD_TEST_EXIT"] = "23"

        result = self.run_guard("pip", "install", "example", env=env)

        self.assertEqual(23, result.returncode)
        self.assertEqual("pip|install example", self.log.read_text(encoding="utf-8").strip().split("/real/")[-1])

    def test_child_and_non_source_flags_are_allowed_with_cfs(self) -> None:
        cases = (
            ("uv", "run", "python", "-i"),
            ("uv", "sync", "--no-install-project"),
            ("pip", "install", "--no-input", "example"),
            ("pip", "install", "-rrequirements.txt"),
            ("pip", "download", "--python-version", "3.11", "example"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_guard(*arguments, env=self.cfs_env(arguments[0]))
                self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(5, len(self.log.read_text(encoding="utf-8").splitlines()))

    def test_local_commands_reach_downstream_without_cfs(self) -> None:
        for arguments in (
            ("pip", "install", "--help"),
            ("pip", "show", "-f", "example"),
            ("pip", "completion", "-f"),
            ("uv", "cache", "dir"),
            ("uv", "pip", "check"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_guard(*arguments)
                self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(5, len(self.log.read_text(encoding="utf-8").splitlines()))

    def test_local_pip_commands_disable_the_version_check(self) -> None:
        downstream = self.real_dir / "pip"
        downstream.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"${PIP_DISABLE_PIP_VERSION_CHECK:-}\" > \"$GUARD_TEST_LOG\"\n",
            encoding="utf-8",
        )
        downstream.chmod(0o755)

        result = self.run_guard("pip", "show", "example")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("1", self.log.read_text(encoding="utf-8").strip())

    def test_unknown_and_networked_status_commands_fail_closed(self) -> None:
        for arguments in (
            ("pip", "list", "--outdated"),
            ("pip", "list", "--outdate"),
            ("pip", "list", "--ou"),
            ("pip", "list", "--up"),
            ("pip", "list", "-o"),
            ("pip", "list", "-vo"),
            ("pip", "install", "--log", "--help", "example"),
            ("pip", "unknown"),
            ("uv", "version", "--bump", "patch"),
            ("uv", "unknown"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_guard(*arguments)
                self.assertEqual(65, result.returncode)
        self.assertFalse(self.log.exists())

    def test_guard_symlink_is_not_selected_as_downstream(self) -> None:
        (self.real_dir / "pip").unlink()
        (self.real_dir / "pip").symlink_to(self.guard)

        result = self.run_guard("pip", "show", "example")

        self.assertEqual(127, result.returncode)
        self.assertIn("no downstream pip executable found", result.stderr)


if __name__ == "__main__":
    unittest.main()
