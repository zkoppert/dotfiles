#!/usr/bin/env python3
"""Exercise profiles through real CLI, marker, PR and Git push entry points."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/repo-guards"
MARKER = ROOT / "bin/pr-marker"
GUARD = ROOT / "bin/gh-guard"


class RepoGuardsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.config = self.home / ".config/repo-guards/config.yaml"
        self.config.parent.mkdir(parents=True)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        }
        for name in (
            "GH_REPO",
            "GH_HOST",
            "NO_MISTAKES_PUBLICATION_RUN",
            "NO_MISTAKES_PUBLICATION_ATTEMPT",
            "ZACK_CONFIRMED_PR_CREATE",
        ):
            self.env.pop(name, None)
        self.run_cmd(["git", "init", "-b", "main"])
        self.run_cmd(["git", "config", "user.name", "Test User"])
        self.run_cmd(["git", "config", "user.email", "test@example.test"])
        self.run_cmd(["git", "commit", "--allow-empty", "-m", "base"])
        self.run_cmd(
            ["git", "remote", "add", "origin", "git@github.com:example/personal.git"]
        )
        fake_gh = self.bin / "gh"
        fake_gh.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            ' "repo view --json nameWithOwner --jq .nameWithOwner") echo example/personal ;;\n'
            ' "repo view --json parent --jq "*) exit 0 ;;\n'
            ' "repo view --json url,parent --jq "*)\n'
            "   target=$(git config --get remote.origin.gh-resolved || true)\n"
            '   [ "$target" = base ] && target=""\n'
            "   target=${TEST_PR_BASE:-${target:-example/personal}}\n"
            '   case "$target" in *://*) printf "%s\\n" "$target" ;;\n'
            '   *) printf "https://github.com/%s\\n" "$target" ;; esac ;;\n'
            ' "api repos/"*" --jq .visibility") echo public ;;\n'
            ' "pr create"*) echo PUBLISHED ;;\n'
            " *) exit 2 ;;\nesac\n"
        )
        fake_gh.chmod(0o755)

    def run_cmd(self, args, *, input=None, check=True, cwd=None):
        result = subprocess.run(
            [str(arg) for arg in args],
            cwd=cwd or self.repo,
            env=self.env,
            input=input,
            text=True,
            capture_output=True,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def assign(self, profile="relaxed", extra=""):
        self.config.write_text(
            f"version: 1\nrepositories:\n  example/personal: {profile}\n{extra}"
        )
        self.config.chmod(0o600)

    def profile(self, *args):
        return json.loads(
            self.run_cmd([sys.executable, CLI, "show", "--json", *args]).stdout
        )

    def gh_profile(self, *args):
        return self.run_cmd(
            [sys.executable, CLI, "gh-context", "--", "pr", "create", *args]
        ).stdout.split("\t")[0]

    def test_strict_default_and_private_assignment(self):
        self.assertEqual(self.profile()["profile"], "strict")
        self.assign()
        output = self.profile()
        self.assertEqual(output["profile"], "relaxed")
        self.assertEqual(output["required_markers"], [])
        self.assertNotIn("example/personal", json.dumps(output))
        self.assertEqual(self.profile("--repo", "example/work")["profile"], "strict")
        self.config.unlink()
        self.assertEqual(self.profile()["profile"], "strict")

    def test_invalid_configuration_never_partially_relaxes_or_leaks(self):
        malformed = [
            "",
            "version: 2\nrepositories:\n  example/personal: relaxed\n",
            "version: 1\nrepositories:\n  example/personal: relaxed\n  confidential/service: typo\n",
            "version: 1\nrepositories:\n  example/personal: relaxed\n  github.com/EXAMPLE/PERSONAL: strict\n",
            "version: 1\nrepositories:\n  example/personal: relaxed\ndefault: relaxed\n",
            "version: 1\nrepositories:\n  example/personal: relaxed\n  example/other: !!python/object bad\n",
        ]
        for text in malformed:
            with self.subTest(text=text):
                self.config.write_text(text)
                self.config.chmod(0o600)
                output = self.profile()
                self.assertEqual(output["profile"], "strict")
                self.assertEqual(output["reason"], "invalid private configuration")
                self.assertNotIn("confidential", json.dumps(output))

    def test_config_symlink_and_public_permissions_are_strict(self):
        self.assign()
        self.config.chmod(0o644)
        self.assertEqual(self.profile()["profile"], "strict")
        target = self.root / "private.yaml"
        self.config.rename(target)
        target.chmod(0o600)
        self.config.symlink_to(target)
        self.assertEqual(self.profile()["profile"], "strict")

    def test_url_normalization_host_boundary_and_unresolved_origin(self):
        self.assign()
        for url in (
            "https://github.com/Example/Personal.git",
            "https://github.com/Example/Personal/",
            "ssh://git@github.com:22/example/personal.git",
            "ssh://git@ssh.github.com:443/example/personal.git",
            "git@GitHub.com:EXAMPLE/PERSONAL.git",
        ):
            self.run_cmd(["git", "remote", "set-url", "origin", url])
            self.assertEqual(self.profile()["profile"], "relaxed", url)
        for url in (
            "https://elsewhere.test/example/personal.git",
            "ssh://git@github.com:2222/example/personal.git",
            "/tmp/unresolved",
        ):
            self.run_cmd(["git", "remote", "set-url", "origin", url])
            self.assertEqual(self.profile()["profile"], "strict", url)

    def test_scp_remotes_without_user_and_aliases_cannot_be_ignored(self):
        self.assign()
        self.run_cmd(
            ["git", "remote", "set-url", "origin", "github.com:example/personal.git"]
        )
        self.assertEqual(self.profile()["profile"], "relaxed")
        self.run_cmd(["git", "remote", "add", "upstream", "work-gh:example/work.git"])
        self.assertEqual(self.profile()["profile"], "strict")
        self.assertEqual(self.gh_profile(), "strict")
        self.run_cmd(["git", "remote", "remove", "upstream"])
        self.run_cmd(["git", "remote", "set-url", "origin", "example/personal"])
        self.assertEqual(self.profile()["profile"], "strict")

    def test_implicit_pr_target_uses_gh_default_and_parent_not_only_remotes(self):
        self.assign()
        self.run_cmd(["git", "config", "remote.origin.gh-resolved", "example/work"])
        self.assertEqual(self.gh_profile(), "strict")
        self.run_cmd(["git", "config", "remote.origin.gh-resolved", "base"])
        self.env["TEST_PR_BASE"] = "example/parent"
        self.assertEqual(self.gh_profile(), "strict")
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        refused = self.run_cmd(
            [GUARD, "pr", "create", "--title", "Example"], check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("PUBLISHED", refused.stdout)
        self.assertEqual(self.gh_profile("--repo", "example/personal"), "relaxed")
        self.env["TEST_PR_BASE"] = "not-a-repository"
        self.assertEqual(self.gh_profile(), "strict")

    def test_implicit_pr_target_preserves_enterprise_host(self):
        self.assign(
            extra="  code.example.test/example/personal: relaxed\n  example/parent: relaxed\n"
        )
        self.run_cmd(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                "https://code.example.test/example/personal.git",
            ]
        )
        self.env["TEST_PR_BASE"] = "https://code.example.test/example/parent"
        output = self.run_cmd(
            [sys.executable, CLI, "gh-context", "--", "pr", "create"]
        ).stdout
        profile, repository, _ = output.split("\t")
        self.assertEqual(profile, "strict")
        self.assertEqual(repository, "code.example.test/example/parent")

    def test_multiple_remotes_use_strictest_and_local_gate_is_not_identity(self):
        self.assign()
        self.run_cmd(["git", "remote", "add", "pipeline", "/tmp/gate.git"])
        self.assertEqual(self.profile()["profile"], "relaxed")
        self.run_cmd(
            ["git", "remote", "add", "upstream", "https://github.com/example/work.git"]
        )
        self.assertEqual(self.profile()["profile"], "strict")

    def test_linked_checkout_uses_shared_remote_identity(self):
        self.assign("standard")
        linked = self.root / "linked"
        self.run_cmd(["git", "worktree", "add", "-b", "linked", linked])
        output = self.run_cmd([sys.executable, CLI, "show", "--json"], cwd=linked)
        self.assertEqual(json.loads(output.stdout)["profile"], "standard")

    def test_gh_target_parser_consumes_values_and_honors_target_precedence(self):
        self.assign()
        for args in (
            ["-Rexample/personal"],
            ["--repo=example/personal"],
            ["-R", "github.com/example/personal"],
            ["--title", "--repo", "--body", "example/work"],
        ):
            self.assertEqual(self.gh_profile(*args), "relaxed", args)
        self.env["GH_REPO"] = "example/work"
        self.assertEqual(self.gh_profile(), "strict")
        for selector in (["--repo="], ["-R", ""], ["-R="]):
            context = self.run_cmd(
                [sys.executable, CLI, "gh-context", "--", "pr", "create", *selector]
            ).stdout
            profile, repository, _ = context.split("\t")
            self.assertEqual(profile, "strict", selector)
            self.assertEqual(repository, "github.com/example/work", selector)
        self.assertEqual(self.gh_profile("--repo", "example/personal"), "relaxed")
        self.assertEqual(
            self.gh_profile("-Rexample/personal", "-Rexample/work"), "strict"
        )
        self.assertEqual(self.gh_profile("--unknown"), "strict")
        self.env["GH_HOST"] = "elsewhere.test"
        self.assertEqual(self.gh_profile("--repo", "example/personal"), "strict")

    def test_relaxed_pr_skips_markers_but_still_requires_authorization(self):
        self.assign()
        self.assertEqual(self.run_cmd([sys.executable, MARKER, "check"]).returncode, 0)
        refused = self.run_cmd(
            [GUARD, "pr", "create", "--title", "Example"], check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("PUBLISHED", refused.stdout)
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        published = self.run_cmd([GUARD, "pr", "create", "--title", "Example"])
        self.assertEqual(published.stdout.strip(), "PUBLISHED")
        strict_target = self.run_cmd(
            [
                sys.executable,
                MARKER,
                "check",
                "--gh-args",
                "pr",
                "create",
                "-Rexample/work",
            ],
            check=False,
        )
        self.assertNotEqual(strict_target.returncode, 0)

    def test_standard_requires_pinned_self_review_and_machine_tests(self):
        self.assign("standard")
        refused = self.run_cmd([sys.executable, MARKER, "check"], check=False)
        self.assertIn("code-review", refused.stderr)
        self.assertIn("tests", refused.stderr)
        review = (
            "I reviewed the final diff and checked normal behavior and failure paths. "
            * 5
        )
        self.run_cmd(
            [sys.executable, MARKER, "write", "code-review", "-"], input=review
        )
        self.assertNotIn(
            "code-review",
            self.run_cmd([sys.executable, MARKER, "check"], check=False).stderr,
        )
        self.run_cmd([sys.executable, MARKER, "run-tests", "--cmd", "true"])
        self.run_cmd([sys.executable, MARKER, "check"])
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        refused = self.run_cmd(
            [GUARD, "pr", "create", "--title", "Example"], check=False
        )
        self.assertIn("requires --draft", refused.stderr)
        self.run_cmd([GUARD, "pr", "create", "--draft", "--title", "Example"])
        self.assign("strict")
        self.assertIn(
            "needs models",
            self.run_cmd([sys.executable, MARKER, "check"], check=False).stderr,
        )
        refused = self.run_cmd(
            [GUARD, "pr", "create", "--draft", "--title", "Example"], check=False
        )
        self.assertIn("FEW-MODELS", refused.stderr)
        self.assertNotIn("PUBLISHED", refused.stdout)
        self.assign("standard")
        self.run_cmd(["git", "commit", "--allow-empty", "-m", "next"])
        self.assertIn(
            "stale", self.run_cmd([sys.executable, MARKER, "check"], check=False).stderr
        )

    def test_standard_target_can_write_self_review_from_relaxed_checkout(self):
        self.assign(extra="  example/library: standard\n")
        review = "I reviewed the final target diff and its failure paths. " * 6
        self.run_cmd(
            [
                sys.executable,
                MARKER,
                "write",
                "code-review",
                "--repo",
                "example/library",
                "-",
            ],
            input=review,
        )
        refused = self.run_cmd(
            [
                sys.executable,
                MARKER,
                "check",
                "--gh-args",
                "pr",
                "create",
                "--repo",
                "example/library",
            ],
            check=False,
        )
        self.assertIn("tests", refused.stderr)
        self.assertNotIn("code-review", refused.stderr)
        self.run_cmd([sys.executable, MARKER, "run-tests", "--cmd", "true"])
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        published = self.run_cmd(
            [
                GUARD,
                "pr",
                "create",
                "--draft",
                "--repo",
                "example/library",
                "--title",
                "Example",
            ]
        )
        self.assertEqual(published.stdout.strip(), "PUBLISHED")

    def test_relaxed_pr_keeps_body_lint_and_ready_confirmation(self):
        self.assign()
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        linter = self.home / ".copilot/skills/validate-style/lint.py"
        linter.parent.mkdir(parents=True)
        linter.write_bytes(
            (ROOT / ".copilot/skills/validate-style/lint.py").read_bytes()
        )
        refused = self.run_cmd(
            [
                GUARD,
                "pr",
                "create",
                "--title",
                "Example",
                "--body",
                "This PR adds behavior.",
            ],
            check=False,
        )
        self.assertIn("blocked by validate-style", refused.stderr)
        self.assertNotIn("PUBLISHED", refused.stdout)
        refused = self.run_cmd([GUARD, "pr", "ready"], check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("ZACK_CONFIRMED_PR_READY", refused.stderr)

    def test_unresolved_target_cannot_use_another_repository_visibility(self):
        self.assign()
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        linter = self.home / ".copilot/skills/validate-style/lint.py"
        linter.parent.mkdir(parents=True)
        linter.write_bytes(
            (ROOT / ".copilot/skills/validate-style/lint.py").read_bytes()
        )
        refused = self.run_cmd(
            [
                GUARD,
                "pr",
                "create",
                "--repo",
                "not-a-repository",
                "--body",
                "I added behavior.",
            ],
            check=False,
        )
        self.assertIn("cannot resolve PR target for body visibility", refused.stderr)
        self.assertNotIn("PUBLISHED", refused.stdout)

    def test_strict_publication_without_checkout_or_attached_branch_is_blocked(self):
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        outside = self.run_cmd(
            [GUARD, "pr", "create", "--title", "Example"], cwd=self.root, check=False
        )
        self.assertIn("requires a checkout and attached branch", outside.stderr)
        self.run_cmd(["git", "checkout", "--detach"])
        detached = self.run_cmd(
            [GUARD, "pr", "create", "--title", "Example"], check=False
        )
        self.assertIn("requires a checkout and attached branch", detached.stderr)
        self.assertNotIn("PUBLISHED", outside.stdout + detached.stdout)

    def test_strict_and_native_requirements_survive_relaxed_assignment(self):
        self.assign()
        self.env["NO_MISTAKES_PUBLICATION_RUN"] = "invalid-run"
        refused = self.run_cmd([sys.executable, MARKER, "check"], check=False)
        self.assertIn("native handoff requires", refused.stderr)
        self.env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        refused = self.run_cmd(
            [GUARD, "pr", "create", "--title", "Example"], check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("PUBLISHED", refused.stdout)
        self.env.pop("NO_MISTAKES_PUBLICATION_RUN")
        self.config.unlink()
        refused = self.run_cmd(
            [sys.executable, MARKER, "write", "code-review", "-"],
            input="Reviewed. " * 50,
            check=False,
        )
        self.assertIn("requires >= 3", refused.stderr)

    def setup_remote(self):
        self.remote = self.root / "remote.git"
        self.run_cmd(["git", "init", "--bare", "-b", "main", self.remote])
        self.run_cmd(["git", "push", str(self.remote), "main"])
        ssh = self.bin / "test-ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            f" *git-upload-pack*) exec git-upload-pack {shlex.quote(str(self.remote))} ;;\n"
            f" *git-receive-pack*) exec git-receive-pack {shlex.quote(str(self.remote))} ;;\n"
            " *) exit 2 ;;\nesac\n"
        )
        ssh.chmod(0o755)
        self.env["GIT_SSH_COMMAND"] = str(ssh)
        self.env["GIT_SSH_VARIANT"] = "ssh"
        self.run_cmd(["git", "config", "core.hooksPath", ROOT / "git-hooks"])
        self.run_cmd(["git", "commit", "--allow-empty", "-m", "next"])

    def test_real_push_relaxed_default_passes_without_testing_strict_blocks(self):
        self.setup_remote()
        self.assign()
        result = self.run_cmd(["git", "push", "origin", "main"])
        self.assertIn("push policy relaxed", result.stderr)
        head = self.run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()
        remote_head = self.run_cmd(
            ["git", "--git-dir", self.remote, "rev-parse", "main"]
        ).stdout.strip()
        self.assertEqual(head, remote_head)
        self.config.unlink()
        self.run_cmd(["git", "commit", "--allow-empty", "-m", "strict next"])
        result = self.run_cmd(["git", "push", "origin", "main"], check=False)
        self.assertIn("strict blocks direct default-branch", result.stderr)
        self.assertEqual(
            remote_head,
            self.run_cmd(
                ["git", "--git-dir", self.remote, "rev-parse", "main"]
            ).stdout.strip(),
        )

    def test_real_userless_scp_push_blocks_default_but_unknown_hosts_allow_features(
        self,
    ):
        self.setup_remote()
        refused = self.run_cmd(
            ["git", "push", "github.com:example/personal.git", "main"], check=False
        )
        self.assertIn("strict blocks direct default-branch", refused.stderr)
        self.run_cmd(["git", "push", "work-gh:group/sub/project.git", "HEAD:feature"])
        refused = self.run_cmd(
            ["git", "push", "work-gh:group/sub/project.git", "HEAD:main"], check=False
        )
        self.assertIn("strict blocks direct default-branch", refused.stderr)

    def test_relaxed_can_initialize_an_empty_remote_but_missing_head_is_not_empty(self):
        self.setup_remote()
        self.run_cmd(
            ["git", "--git-dir", self.remote, "update-ref", "-d", "refs/heads/main"]
        )
        self.assign()
        result = self.run_cmd(["git", "push", "origin", "main"])
        self.assertIn("relaxed permits initial branch creation", result.stderr)
        self.run_cmd(
            [
                "git",
                "--git-dir",
                self.remote,
                "symbolic-ref",
                "HEAD",
                "refs/heads/missing",
            ]
        )
        refused = self.run_cmd(["git", "push", "origin", "HEAD:feature"], check=False)
        self.assertIn("default branch is unavailable", refused.stderr)

    def test_up_to_date_network_push_is_a_successful_no_op(self):
        self.setup_remote()
        self.assign()
        self.run_cmd(["git", "push", "origin", "main"])
        self.config.unlink()
        result = self.run_cmd(["git", "push", "origin", "main"])
        self.assertIn("Everything up-to-date", result.stderr)
        for updates in ("", "\n"):
            self.run_cmd(
                [sys.executable, CLI, "check-push", "github.com:example/personal.git"],
                input=updates,
            )

    def test_push_respects_actual_default_and_repository_hook(self):
        self.setup_remote()
        self.assign("standard")
        self.run_cmd(
            [
                "git",
                "--git-dir",
                self.remote,
                "symbolic-ref",
                "HEAD",
                "refs/heads/release",
            ]
        )
        self.run_cmd(
            [
                "git",
                "--git-dir",
                self.remote,
                "update-ref",
                "refs/heads/release",
                self.run_cmd(
                    ["git", "--git-dir", self.remote, "rev-parse", "main"]
                ).stdout.strip(),
            ]
        )
        self.run_cmd(["git", "push", "origin", "main"])
        refused = self.run_cmd(["git", "push", "origin", "HEAD:release"], check=False)
        self.assertIn("standard blocks", refused.stderr)
        local_hook = self.repo / ".git/hooks/pre-push"
        local_hook.write_text(
            '#!/bin/sh\nprintf "repository hook rejected\\n" >&2\nexit 7\n'
        )
        local_hook.chmod(0o755)
        refused = self.run_cmd(["git", "push", "origin", "HEAD:feature"], check=False)
        self.assertIn("repository hook rejected", refused.stderr)

    def test_relaxed_cannot_delete_default_and_multiref_push_is_atomic(self):
        self.setup_remote()
        self.assign()
        refused = self.run_cmd(["git", "push", "origin", ":main"], check=False)
        self.assertIn("blocked default-branch deletion", refused.stderr)
        self.assign("standard")
        refused = self.run_cmd(
            ["git", "push", "origin", "HEAD:feature", "HEAD:main"], check=False
        )
        self.assertIn("standard blocks", refused.stderr)
        absent = self.run_cmd(
            [
                "git",
                "--git-dir",
                self.remote,
                "rev-parse",
                "--verify",
                "refs/heads/feature",
            ],
            check=False,
        )
        self.assertNotEqual(absent.returncode, 0)

    def test_missing_default_evidence_blocks_and_relaxed_cannot_rewrite(self):
        self.setup_remote()
        self.assign()
        self.run_cmd(["git", "push", "origin", "main"])
        previous = self.run_cmd(["git", "rev-parse", "HEAD^"]).stdout.strip()
        refused = self.run_cmd(
            ["git", "push", "--force", "origin", f"{previous}:main"], check=False
        )
        self.assertIn("blocked default-branch history rewrite", refused.stderr)
        self.run_cmd(
            [
                "git",
                "--git-dir",
                self.remote,
                "symbolic-ref",
                "HEAD",
                "refs/heads/missing",
            ]
        )
        refused = self.run_cmd(["git", "push", "origin", "HEAD:feature"], check=False)
        self.assertIn("default branch is unavailable", refused.stderr)


if __name__ == "__main__":
    unittest.main()
