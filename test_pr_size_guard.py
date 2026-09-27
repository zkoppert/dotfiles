#!/usr/bin/env python3
"""Regression tests for the personal pull request size guard."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional


class PrSizeGuardTest(unittest.TestCase):
    """Exercise the helper and both publication gates."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.guard = Path(__file__).with_name("bin") / "pr-size-guard"
        self.pre_push = Path(__file__).with_name("git-hooks") / "pre-push"
        self.gh_guard = Path(__file__).with_name("bin") / "gh-guard"

        self.run_command(["git", "init", "--bare", str(self.remote)], cwd=self.root)
        self.run_command(["git", "init", "-b", "main", str(self.repo)], cwd=self.root)
        self.run_command(["git", "config", "user.email", "test@example.com"])
        self.run_command(["git", "config", "user.name", "Test User"])
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.run_command(["git", "add", "base.txt"])
        self.run_command(["git", "commit", "-m", "base"])
        self.run_command(["git", "remote", "add", "origin", str(self.remote)])
        self.run_command(["git", "push", "-u", "origin", "main"])
        self.run_command(
            ["git", "remote", "set-url", "origin", "git@github.com:github/github.git"]
        )
        self.run_command(
            [
                "git",
                "config",
                f"url.file://{self.remote}.insteadOf",
                "git@github.com:github/github.git",
            ]
        )
        self.run_command(["git", "checkout", "-b", "feature"])

        fake_gh = self.fake_bin / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  "pr list --repo github/github --head feature --state open --limit 1 --json baseRefName --jq .[0].baseRefName // \\\"\\\"") exit 0 ;;\n'
            '  "repo view github/github --json defaultBranchRef --jq .defaultBranchRef.name") printf "main\\n" ;;\n'
            '  "repo view --json nameWithOwner --jq .nameWithOwner") printf "github/github\\n" ;;\n'
            '  "api repos/github/github --jq .visibility") printf "internal\\n" ;;\n'
            '  *) printf "fake gh: unsupported: %s\\n" "$*" >&2; exit 2 ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
        }

    def run_command(
        self,
        command: list[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        input_text: Optional[str] = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd or self.repo,
            env=env,
            input=input_text,
            check=True,
            capture_output=True,
            text=True,
        )

    def commit_added_lines(self, count: int) -> str:
        """Commit a file containing the requested number of added lines."""
        (self.repo / "added.txt").write_text(
            "".join(f"line {number}\n" for number in range(count)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", "added.txt"])
        self.run_command(["git", "commit", "-m", f"add {count} lines"])
        return self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

    def run_size_guard(
        self, source: str, *, repository: str = "github/github", override: bool = False
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if override:
            env["ZACK_CONFIRMED_LARGE_PR"] = "1"
        return subprocess.run(
            [
                str(self.guard),
                "--repo",
                repository,
                "--remote",
                "origin",
                "--base",
                "main",
                "--branch",
                "feature",
                "--source",
                source,
                "--gh",
                str(self.fake_bin / "gh"),
                "--context",
                "push",
            ],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exact_limit_passes_and_limit_plus_one_blocks(self) -> None:
        source = self.commit_added_lines(800)

        result = self.run_size_guard(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("800/800 added lines", result.stdout)

        (self.repo / "added.txt").write_text(
            (self.repo / "added.txt").read_text(encoding="utf-8") + "line 801\n",
            encoding="utf-8",
        )
        self.run_command(["git", "add", "added.txt"])
        self.run_command(["git", "commit", "-m", "cross size limit"])
        oversized_source = self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

        blocked = self.run_size_guard(oversized_source)

        self.assertEqual(blocked.returncode, 1)
        self.assertIn("801 added lines", blocked.stderr)
        self.assertIn("gh-axi stack", blocked.stderr)

    def test_explicit_override_allows_oversized_diff(self) -> None:
        source = self.commit_added_lines(801)

        result = self.run_size_guard(source, override=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("override accepted", result.stdout)

    def test_non_target_repository_is_not_gated(self) -> None:
        result = self.run_size_guard("not-a-commit", repository="example/project")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_binary_files_do_not_break_the_count(self) -> None:
        (self.repo / "image.bin").write_bytes(b"\x00\x01\x02")
        self.run_command(["git", "add", "image.bin"])
        self.run_command(["git", "commit", "-m", "add binary"])
        source = self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

        result = self.run_size_guard(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0/800 added lines", result.stdout)

    def test_pre_push_hook_blocks_target_branch_update(self) -> None:
        source = self.commit_added_lines(801)
        hook_input = f"refs/heads/feature {source} refs/heads/feature {'0' * 40}\n"

        result = subprocess.run(
            [str(self.pre_push), "origin", "git@github.com:github/github.git"],
            cwd=self.repo,
            env=self.env,
            input=hook_input,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)

    def test_pre_push_hook_skips_non_target_repository(self) -> None:
        source = self.commit_added_lines(801)
        hook_input = f"refs/heads/feature {source} refs/heads/feature {'0' * 40}\n"

        result = subprocess.run(
            [str(self.pre_push), "origin", "git@github.com:example/project.git"],
            cwd=self.repo,
            env=self.env,
            input=hook_input,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gh_guard_blocks_oversized_draft_before_marker_checks(self) -> None:
        self.commit_added_lines(801)

        result = subprocess.run(
            [str(self.gh_guard), "pr", "create", "--draft"],
            cwd=self.repo,
            env={**self.env, "ZACK_CONFIRMED_PR_CREATE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("blocked draft creation", result.stderr)
        self.assertIn("801 added lines", result.stderr)


if __name__ == "__main__":
    unittest.main()
