#!/usr/bin/env python3
"""Regression tests for the personal pull request size guard."""

from __future__ import annotations

import os
import json
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
        self.pre_commit = Path(__file__).with_name("git-hooks") / "pre-commit"
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
            'case "$2" in repos/github/github/pulls*) [ "$1" = api ] && exit 0 ;; esac\n'
            'case "$*" in\n'
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
                "--head-owner",
                "github",
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

    def test_explicit_override_allows_a_check_failure(self) -> None:
        result = self.run_size_guard("missing-commit", override=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("override accepted despite check failure", result.stderr)

    def test_non_target_repository_is_not_gated(self) -> None:
        result = self.run_size_guard("not-a-commit", repository="example/project")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_push_hook_blocks_fork_push_with_target_upstream(self) -> None:
        source = self.commit_added_lines(801)
        self.run_command(["git", "remote", "rename", "origin", "upstream"])
        self.run_command(
            ["git", "remote", "add", "origin", "git@github.com:zkoppert/github.git"]
        )
        hook_input = f"refs/heads/feature {source} refs/heads/feature {'0' * 40}\n"

        result = subprocess.run(
            [str(self.pre_push), "origin", "git@github.com:zkoppert/github.git"],
            cwd=self.repo,
            env=self.env,
            input=hook_input,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)

    def test_pre_push_hook_supports_case_and_port_qualified_ssh_url(self) -> None:
        source = self.commit_added_lines(801)
        hook_input = f"refs/heads/feature {source} refs/heads/feature {'0' * 40}\n"

        result = subprocess.run(
            [
                str(self.pre_push),
                "origin",
                "ssh://git@ssh.github.com:443/GitHub/GitHub.git",
            ],
            cwd=self.repo,
            env=self.env,
            input=hook_input,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)

    def test_pre_push_hook_delegates_to_repository_hook(self) -> None:
        local_hook = self.repo / ".git" / "hooks" / "pre-push"
        local_hook.write_text(
            "#!/bin/sh\nprintf 'repository pre-push ran\\n' >&2\nexit 7\n",
            encoding="utf-8",
        )
        local_hook.chmod(0o755)
        source = self.commit_added_lines(1)
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

        self.assertEqual(result.returncode, 7)
        self.assertIn("repository pre-push ran", result.stderr)

    def test_pre_commit_hook_delegates_to_repository_hook(self) -> None:
        local_hook = self.repo / ".git" / "hooks" / "pre-commit"
        local_hook.write_text(
            "#!/bin/sh\nprintf 'repository pre-commit ran\\n' >&2\nexit 7\n",
            encoding="utf-8",
        )
        local_hook.chmod(0o755)

        result = subprocess.run(
            [str(self.pre_commit)],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 7)
        self.assertIn("repository pre-commit ran", result.stderr)

    def test_repository_hook_forwarder_preserves_other_hook_names(self) -> None:
        local_hook = self.repo / ".git" / "hooks" / "commit-msg"
        local_hook.write_text(
            "#!/bin/sh\nprintf 'repository commit-msg ran: %s\\n' \"$1\" >&2\nexit 7\n",
            encoding="utf-8",
        )
        local_hook.chmod(0o755)
        shim = self.root / "commit-msg"
        shim.symlink_to(Path(__file__).with_name("git-hooks") / "repository-hook-forwarder")

        result = subprocess.run(
            [str(shim), "message.txt"],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 7)
        self.assertIn("repository commit-msg ran: message.txt", result.stderr)

    def test_binary_files_do_not_break_the_count(self) -> None:
        (self.repo / "image.bin").write_bytes(b"\x00\x01\x02")
        self.run_command(["git", "add", "image.bin"])
        self.run_command(["git", "commit", "-m", "add binary"])
        source = self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

        result = self.run_size_guard(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1/800 added lines", result.stdout)

    def test_text_marked_binary_still_counts_added_lines(self) -> None:
        (self.repo / ".gitattributes").write_text(
            "payload.txt binary\n", encoding="utf-8"
        )
        (self.repo / "payload.txt").write_text(
            "".join(f"line {number}\n" for number in range(801)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", ".gitattributes", "payload.txt"])
        self.run_command(["git", "commit", "-m", "add attributed text"])
        source = self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

        result = self.run_size_guard(source)

        self.assertEqual(result.returncode, 1)
        self.assertIn("802 added lines", result.stderr)

    def test_modified_text_marked_binary_counts_only_new_lines(self) -> None:
        (self.repo / ".gitattributes").write_text(
            "payload.txt binary\n", encoding="utf-8"
        )
        (self.repo / "payload.txt").write_text(
            "".join(f"line {number}\n" for number in range(1000)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", ".gitattributes", "payload.txt"])
        self.run_command(["git", "commit", "-m", "add attributed baseline"])
        baseline = self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()
        self.run_command(
            [
                "git",
                f"--git-dir={self.remote}",
                "fetch",
                str(self.repo),
                f"{baseline}:refs/heads/main",
            ]
        )
        (self.repo / "payload.txt").write_text(
            (self.repo / "payload.txt").read_text(encoding="utf-8")
            + "one new line\n",
            encoding="utf-8",
        )
        self.run_command(["git", "add", "payload.txt"])
        self.run_command(["git", "commit", "-m", "change attributed text"])
        source = self.run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

        result = self.run_size_guard(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1/800 added lines", result.stdout)

    def test_repository_matching_is_case_insensitive(self) -> None:
        source = self.commit_added_lines(801)

        result = self.run_size_guard(source, repository="GitHub/GitHub")

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)

    def test_remote_tracking_source_is_resolved(self) -> None:
        source = self.commit_added_lines(1)
        self.run_command(["git", "push", "origin", "feature"])
        self.run_command(["git", "checkout", "main"])
        self.run_command(["git", "branch", "-D", "feature"])

        result = self.run_size_guard("feature")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1/800 added lines", result.stdout)
        self.assertEqual(
            self.run_command(["git", "rev-parse", "origin/feature"]).stdout.strip(),
            source,
        )

    def test_shallow_clone_is_deepened_to_find_merge_base(self) -> None:
        self.commit_added_lines(1)
        self.run_command(["git", "push", "origin", "feature"])
        for branch in ("extra-one", "extra-two"):
            self.run_command(["git", "branch", branch, "main"])
            self.run_command(["git", "push", "origin", branch])
        shallow = self.root / "shallow"
        self.run_command(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--no-single-branch",
                "--branch",
                "feature",
                f"file://{self.remote}",
                str(shallow),
            ],
            cwd=self.root,
        )
        self.run_command(
            ["git", "remote", "set-url", "origin", "git@github.com:github/github.git"],
            cwd=shallow,
        )
        extra_count_before = self.run_command(
            ["git", "rev-list", "--count", "origin/extra-one"],
            cwd=shallow,
        ).stdout.strip()
        self.run_command(
            [
                "git",
                "config",
                f"url.file://{self.remote}.insteadOf",
                "git@github.com:github/github.git",
            ],
            cwd=shallow,
        )

        result = subprocess.run(
            [
                str(self.guard),
                "--repo",
                "github/github",
                "--remote",
                "origin",
                "--base",
                "main",
                "--branch",
                "feature",
                "--head-owner",
                "github",
                "--source",
                "HEAD",
                "--gh",
                str(self.fake_bin / "gh"),
                "--context",
                "push",
            ],
            cwd=shallow,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1/800 added lines", result.stdout)
        self.assertEqual(
            self.run_command(
                ["git", "rev-list", "--count", "origin/extra-one"],
                cwd=shallow,
            ).stdout.strip(),
            extra_count_before,
        )

    def test_first_stacked_push_uses_live_gh_stack_parent(self) -> None:
        stack_a = self.commit_added_lines(500)
        self.run_command(["git", "checkout", "-b", "stack-b"])
        (self.repo / "stack-b.txt").write_text(
            "".join(f"child {number}\n" for number in range(500)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", "stack-b.txt"])
        self.run_command(["git", "commit", "-m", "add stacked child"])
        common_dir = Path(
            self.run_command(["git", "rev-parse", "--git-common-dir"]).stdout.strip()
        )
        if not common_dir.is_absolute():
            common_dir = self.repo / common_dir
        (common_dir / "gh-stack").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "repository": "github/github",
                    "stacks": [
                        {
                            "trunk": {"branch": "main"},
                            "branches": [
                                {"branch": "feature", "base": "HEAD"},
                                {"branch": "stack-b", "base": stack_a},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                str(self.guard),
                "--repo",
                "github/github",
                "--remote",
                "origin",
                "--branch",
                "stack-b",
                "--head-owner",
                "github",
                "--source",
                "HEAD",
                "--gh",
                str(self.fake_bin / "gh"),
                "--context",
                "push",
                "--check-open-pr",
            ],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("500/800 added lines", result.stdout)

        self.run_command(["git", "checkout", "feature"])
        (self.repo / "parent-more.txt").write_text(
            "".join(f"parent {number}\n" for number in range(400)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", "parent-more.txt"])
        self.run_command(["git", "commit", "-m", "grow parent"])
        self.run_command(["git", "checkout", "stack-b"])
        self.run_command(["git", "rebase", "feature"])

        rebased = subprocess.run(
            [
                str(self.guard),
                "--repo",
                "github/github",
                "--remote",
                "origin",
                "--branch",
                "stack-b",
                "--head-owner",
                "github",
                "--source",
                "HEAD",
                "--gh",
                str(self.fake_bin / "gh"),
                "--context",
                "push",
                "--check-open-pr",
            ],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(rebased.returncode, 0, rebased.stderr)
        self.assertIn("500/800 added lines", rebased.stdout)

    def test_draft_creation_ignores_gh_stack_metadata_without_base_flag(self) -> None:
        stack_a = self.commit_added_lines(500)
        self.run_command(["git", "checkout", "-b", "stack-b"])
        (self.repo / "stack-b.txt").write_text(
            "".join(f"child {number}\n" for number in range(500)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", "stack-b.txt"])
        self.run_command(["git", "commit", "-m", "add stacked child"])
        common_dir = Path(
            self.run_command(["git", "rev-parse", "--git-common-dir"]).stdout.strip()
        )
        if not common_dir.is_absolute():
            common_dir = self.repo / common_dir
        (common_dir / "gh-stack").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "repository": "github/github",
                    "stacks": [
                        {
                            "trunk": {"branch": "main"},
                            "branches": [
                                {"branch": "feature", "base": "HEAD"},
                                {"branch": "stack-b", "base": stack_a},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                str(self.guard),
                "--repo",
                "github/github",
                "--remote",
                "origin",
                "--branch",
                "stack-b",
                "--head-owner",
                "github",
                "--source",
                "HEAD",
                "--gh",
                str(self.fake_bin / "gh"),
                "--context",
                "draft creation",
            ],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("1000 added lines", result.stderr)

    def test_linked_worktree_reads_its_gh_stack_metadata(self) -> None:
        self.commit_added_lines(500)
        self.run_command(["git", "checkout", "-b", "stack-b"])
        (self.repo / "stack-b.txt").write_text(
            "".join(f"child {number}\n" for number in range(500)),
            encoding="utf-8",
        )
        self.run_command(["git", "add", "stack-b.txt"])
        self.run_command(["git", "commit", "-m", "add stacked child"])
        self.run_command(["git", "checkout", "main"])
        worktree = self.root / "worktree"
        self.run_command(["git", "worktree", "add", str(worktree), "stack-b"])
        worktree_git_dir = Path(
            self.run_command(
                ["git", "rev-parse", "--git-dir"],
                cwd=worktree,
            ).stdout.strip()
        )
        if not worktree_git_dir.is_absolute():
            worktree_git_dir = worktree / worktree_git_dir
        (worktree_git_dir / "gh-stack").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "repository": "github/github",
                    "stacks": [
                        {
                            "trunk": {"branch": "main"},
                            "branches": [
                                {"branch": "feature", "base": "HEAD"},
                                {"branch": "stack-b", "base": "HEAD"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                str(self.guard),
                "--repo",
                "github/github",
                "--remote",
                "origin",
                "--branch",
                "stack-b",
                "--head-owner",
                "github",
                "--source",
                "HEAD",
                "--gh",
                str(self.fake_bin / "gh"),
                "--context",
                "push",
                "--check-open-pr",
            ],
            cwd=worktree,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("500/800 added lines", result.stdout)

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
        self.run_command(
            ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"]
        )
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

    def test_gh_guard_blocks_native_publication_before_proof_checks(self) -> None:
        self.commit_added_lines(801)

        result = subprocess.run(
            [str(self.gh_guard), "pr", "create", "--draft"],
            cwd=self.repo,
            env={
                **self.env,
                "NO_MISTAKES_PUBLICATION_RUN": "native-run",
                "ZACK_CONFIRMED_PR_CREATE": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("blocked draft creation", result.stderr)
        self.assertNotIn("native publication blocked", result.stderr)

    def test_gh_guard_parses_short_and_host_qualified_flags(self) -> None:
        self.commit_added_lines(801)
        self.run_command(["git", "branch", "large", "HEAD"])
        self.run_command(["git", "checkout", "main"])
        (self.repo / "small.txt").write_text("small\n", encoding="utf-8")
        self.run_command(["git", "add", "small.txt"])
        self.run_command(["git", "commit", "-m", "small change"])

        invocations = (
            ["-Rgithub/github", "-H", "large"],
            ["-RGitHub/GitHub", "-H", "large"],
            ["--repo", "github.com/github/github", "--head", "large"],
            ["--repo", "https://github.com/github/github", "--head", "large"],
            ["-R", "github/github", "-Hlarge"],
        )
        for flags in invocations:
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [str(self.gh_guard), "pr", "create", "--draft", *flags],
                    cwd=self.repo,
                    env={**self.env, "ZACK_CONFIRMED_PR_CREATE": "1"},
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn("801 added lines", result.stderr)

    def test_gh_guard_uses_origin_when_repo_lookup_fails(self) -> None:
        self.commit_added_lines(801)
        self.run_command(["git", "config", "branch.feature.gh-merge-base", "main"])
        failing_bin = self.root / "failing-bin"
        failing_bin.mkdir()
        failing_gh = failing_bin / "gh"
        failing_gh.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        failing_gh.chmod(0o755)

        result = subprocess.run(
            [str(self.gh_guard), "pr", "create", "--draft"],
            cwd=self.repo,
            env={
                **self.env,
                "PATH": f"{failing_bin}{os.pathsep}{os.environ['PATH']}",
                "ZACK_CONFIRMED_PR_CREATE": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)

    def test_gh_guard_finds_target_remote_when_origin_is_a_fork(self) -> None:
        self.commit_added_lines(801)
        self.run_command(
            ["git", "remote", "set-url", "origin", "git@github.com:zkoppert/github.git"]
        )
        self.run_command(
            ["git", "remote", "add", "upstream", "git@github.com:github/github.git"]
        )
        self.run_command(
            [
                "git",
                "config",
                f"url.file://{self.remote}.insteadOf",
                "git@github.com:github/github.git",
            ]
        )
        self.run_command(["git", "config", "branch.feature.gh-merge-base", "main"])
        failing_bin = self.root / "failing-upstream-bin"
        failing_bin.mkdir()
        failing_gh = failing_bin / "gh"
        failing_gh.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        failing_gh.chmod(0o755)

        result = subprocess.run(
            [str(self.gh_guard), "pr", "create", "--draft"],
            cwd=self.repo,
            env={
                **self.env,
                "PATH": f"{failing_bin}{os.pathsep}{os.environ['PATH']}",
                "ZACK_CONFIRMED_PR_CREATE": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)

    def test_gh_guard_blocks_owner_qualified_head(self) -> None:
        self.commit_added_lines(1)

        result = subprocess.run(
            [
                str(self.gh_guard),
                "pr",
                "create",
                "--draft",
                "--repo",
                "github/github",
                "--head",
                "someone:feature",
            ],
            cwd=self.repo,
            env={**self.env, "ZACK_CONFIRMED_PR_CREATE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot verify an owner-qualified --head", result.stderr)

    def test_installed_gh_symlink_finds_size_guard(self) -> None:
        self.commit_added_lines(801)
        shim_dir = self.root / "shim"
        shim_dir.mkdir()
        (shim_dir / "gh").symlink_to(self.gh_guard)
        env = {
            **self.env,
            "PATH": f"{shim_dir}{os.pathsep}{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "ZACK_CONFIRMED_PR_CREATE": "1",
        }

        result = subprocess.run(
            [str(shim_dir / "gh"), "pr", "create", "--draft"],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("801 added lines", result.stderr)
        self.assertNotIn("can't open file", result.stderr)


if __name__ == "__main__":
    unittest.main()
