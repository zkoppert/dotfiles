#!/usr/bin/env python3
"""Regression tests for default Copilot Codespace provisioning."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class SetupCopilot2CodespaceTest(unittest.TestCase):
    """Exercise setup-copilot2-codespace with a fake GitHub CLI."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.bin_dir = self.root / "bin"
        self.home.mkdir()
        self.bin_dir.mkdir()
        self.repo = self.root / "dotfiles"
        (self.repo / "bin").mkdir(parents=True)
        source_script = Path(__file__).with_name("bin") / "setup-copilot2-codespace"
        self.script = self.repo / "bin" / "setup-copilot2-codespace"
        shutil.copy2(source_script, self.script)
        install = self.repo / "install.sh"
        install.write_text(
            "#!/bin/sh\n"
            'dotfiles="$(cd "$(dirname "$0")" && pwd)"\n'
            'mkdir -p "$HOME/.copilot/skills"\n'
            'ln -sfn "$dotfiles/.github/copilot-instructions.md" '
            '"$HOME/.copilot/copilot-instructions.md"\n'
            'ln -sfn "$dotfiles/.copilot/skills/test-quality" '
            '"$HOME/.copilot/skills/test-quality"\n',
            encoding="utf-8",
        )
        install.chmod(0o755)
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "copilot-instructions.md").write_text(
            "# fixture instructions\n",
            encoding="utf-8",
        )
        (self.repo / ".copilot" / "skills" / "test-quality").mkdir(parents=True)
        (self.repo / ".copilot" / "skills" / "test-quality" / "SKILL.md").write_text(
            "# fixture test-quality\n",
            encoding="utf-8",
        )
        for skill in (
            "gh-axi",
            "graphql-availability-investigator",
            "milestone-release-tracking",
            "prod-explain",
        ):
            skill_dir = self.home / ".copilot" / "skills" / skill
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {skill}\n---\n",
                encoding="utf-8",
            )
        self.gh = self.bin_dir / "gh"
        self.gh.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$HOME/gh.log"\n'
            'case "$*" in\n'
            "  'auth status --hostname github.com') exit 0 ;;\n"
            "  'auth token') printf 'fixture-token\\n'; exit 0 ;;\n"
            "  'codespace list --limit 100 --json name,displayName')\n"
            "    printf '%s\\n' \"$FAKE_SOURCE_JSON\"\n"
            "    ;;\n"
            "  'codespace view --codespace old-generated --json name,repository,machineName,devcontainerPath,location')\n"
            "    printf '%s\\n' \"$FAKE_SOURCE_DETAILS_JSON\"\n"
            "    ;;\n"
            "  'codespace view --codespace source-exact --json name,repository,machineName,devcontainerPath,location')\n"
            "    printf '%s\\n' \"$FAKE_EXACT_SOURCE_JSON\"\n"
            "    ;;\n"
            "  'codespace view --codespace configured-generated --json name,repository,state')\n"
            "    printf '%s\\n' \"$FAKE_DEFAULT_DETAILS_JSON\"\n"
            "    ;;\n"
            "  codespace\\ create*)\n"
            '    touch "$HOME/created"\n'
            '    exit "${FAKE_CREATE_STATUS:-0}"\n'
            "    ;;\n"
            "  'codespace list --limit 100 --repo example/project --json name,displayName,state')\n"
            '    if [ -f "$HOME/created" ]; then\n'
            "      printf '%s\\n' \"$FAKE_CREATED_JSON\"\n"
            "    else\n"
            "      printf '[]\\n'\n"
            "    fi\n"
            "    ;;\n"
            "  codespace\\ ssh\\ --codespace\\ new-generated*tar\\ -xzf\\ -*)\n"
            '    [ "$#" -eq 6 ] || exit 91\n'
            "    exit 0\n"
            "    ;;\n"
            "  codespace\\ ssh\\ --codespace\\ new-generated*)\n"
            '    [ "$#" -eq 6 ] || exit 91\n'
            '    exit "${FAKE_REMOTE_STATUS:-0}"\n'
            "    ;;\n"
            "  codespace\\ ssh\\ --codespace\\ configured-generated*)\n"
            '    [ "$#" -eq 6 ] || exit 91\n'
            '    exit "${FAKE_REMOTE_STATUS:-0}"\n'
            "    ;;\n"
            "  *) printf 'unexpected gh args: %s\\n' \"$*\" >&2; exit 90 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        self.gh.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SOURCE_JSON": json.dumps(
                [
                    {
                        "name": "old-generated",
                        "displayName": "gummyworm",
                    }
                ]
            ),
            "FAKE_SOURCE_DETAILS_JSON": json.dumps(
                {
                    "name": "old-generated",
                    "repository": "example/project",
                    "machineName": "largeLinux",
                    "devcontainerPath": ".devcontainer/devcontainer.json",
                    "location": "WestUs2",
                }
            ),
            "FAKE_EXACT_SOURCE_JSON": json.dumps(
                {
                    "name": "source-exact",
                    "repository": "example/project",
                    "machineName": "largeLinux",
                    "devcontainerPath": ".devcontainer/devcontainer.json",
                    "location": "WestUs2",
                }
            ),
            "FAKE_CREATED_JSON": json.dumps(
                [
                    {
                        "name": "new-generated",
                        "displayName": "copilot2-default",
                        "state": "Available",
                    }
                ]
            ),
            "FAKE_DEFAULT_DETAILS_JSON": json.dumps(
                {
                    "name": "configured-generated",
                    "repository": "example/project",
                    "state": "Available",
                }
            ),
        }

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_copies_source_settings_configures_remote_and_selects_default(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertIn(
            "codespace create --repo example/project --machine largeLinux "
            "--display-name copilot2-default --idle-timeout 4h "
            "--retention-period 720h --default-permissions --status "
            "--devcontainer-path .devcontainer/devcontainer.json --location WestUs2",
            calls,
        )
        self.assertIn("codespace ssh --codespace new-generated", calls)
        self.assertIn("tar -xzf -", calls)
        self.assertIn("bootstrap-copilot-mcp", calls)
        self.assertIn("verify-codespace-copilot-env", calls)
        self.assertNotIn("fixture-token", calls + result.stdout + result.stderr)
        destination = self.home / ".config" / "copilot2" / "default.json"
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8")),
            {
                "codespace": "new-generated",
                "displayName": "copilot2-default",
                "machine": "largeLinux",
                "repository": "example/project",
            },
        )
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_remote_verification_failure_preserves_existing_default(self) -> None:
        destination = self.home / ".config" / "copilot2" / "default.json"
        destination.parent.mkdir(parents=True)
        destination.write_text('{"codespace":"working"}\n', encoding="utf-8")
        self.env["FAKE_REMOTE_STATUS"] = "1"

        result = self.run_script()

        self.assertEqual(result.returncode, 2)
        self.assertIn("setup or verification failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            '{"codespace":"working"}\n',
        )

    def test_reuses_an_available_codespace_after_an_interrupted_setup(self) -> None:
        (self.home / "created").touch()

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertNotIn("codespace create", calls)
        self.assertIn("codespace ssh --codespace new-generated", calls)
        self.assertIn("Reusing existing Codespace new-generated.", result.stdout)

    def test_continues_when_status_poll_fails_after_creation(self) -> None:
        self.env["FAKE_CREATE_STATUS"] = "1"

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("became available; continuing", result.stderr)
        destination = self.home / ".config" / "copilot2" / "default.json"
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8"))["codespace"],
            "new-generated",
        )

    def test_explicit_repository_and_machine_skip_source_lookup(self) -> None:
        result = self.run_script(
            "--repo",
            "example/project",
            "--machine",
            "largeLinux",
            "--branch",
            "main",
            "--location",
            "WestUs2",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertNotIn("machineName", calls)
        self.assertIn("--branch main --location WestUs2", calls)

    def test_exact_source_codespace_avoids_ambiguous_display_lookup(self) -> None:
        result = self.run_script("--source-codespace", "source-exact")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertIn("codespace view --codespace source-exact", calls)
        self.assertNotIn(
            "codespace list --limit 100 --json name,displayName",
            calls,
        )

    def prepare_refresh_checkout(self) -> str:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.repo, check=True)
        remote = self.root / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "fetch", "-q", "origin", "main"],
            cwd=self.repo,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def write_default(self) -> Path:
        destination = self.home / ".config" / "copilot2" / "default.json"
        destination.parent.mkdir(parents=True)
        destination.write_text(
            json.dumps(
                {
                    "codespace": "configured-generated",
                    "repository": "example/project",
                }
            ),
            encoding="utf-8",
        )
        return destination

    def test_refresh_default_deploys_the_exact_main_commit(self) -> None:
        expected_commit = self.prepare_refresh_checkout()
        self.write_default()

        result = self.run_script("--refresh-default")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertIn(
            "codespace view --codespace configured-generated --json name,repository,state",
            calls,
        )
        self.assertIn("codespace ssh --codespace configured-generated", calls)
        self.assertIn(expected_commit, calls)
        self.assertIn("git status --porcelain", calls)
        origin_guard = 'test "$(git rev-parse origin/main)" = "$expected_commit"'
        self.assertIn(origin_guard, calls)
        self.assertIn("git fetch origin main:refs/remotes/origin/main", calls)
        self.assertLess(calls.index(origin_guard), calls.index("git merge --ff-only origin/main"))
        self.assertIn("git merge-base --is-ancestor HEAD origin/main", calls)
        self.assertIn("git merge --ff-only origin/main", calls)
        self.assertIn("verify-codespace-copilot-env", calls)
        self.assertIn(
            f"Deployed dotfiles commit {expected_commit}",
            result.stdout,
        )
        instructions = self.home / ".copilot" / "copilot-instructions.md"
        self.assertEqual(
            instructions.resolve(),
            (self.repo / ".github" / "copilot-instructions.md").resolve(),
        )

    def test_refresh_default_rejects_a_dirty_local_checkout(self) -> None:
        self.prepare_refresh_checkout()
        self.write_default()
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        result = self.run_script("--refresh-default")

        self.assertEqual(result.returncode, 2)
        self.assertIn("local dotfiles checkout has uncommitted changes", result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertEqual(calls.splitlines(), ["auth status --hostname github.com"])

    def test_refresh_default_rejects_a_commit_that_is_not_on_origin_main(self) -> None:
        self.prepare_refresh_checkout()
        self.write_default()
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        subprocess.run(["git", "add", "local.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "local"], cwd=self.repo, check=True)

        result = self.run_script("--refresh-default")

        self.assertEqual(result.returncode, 2)
        self.assertIn("local dotfiles checkout is not aligned with origin/main", result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertEqual(calls.splitlines(), ["auth status --hostname github.com"])

    def test_remote_git_guard_refuses_to_advance_past_the_expected_commit(self) -> None:
        expected_commit = self.prepare_refresh_checkout()
        remote = self.root / "origin.git"
        checkout = self.root / "remote-checkout"
        advancer = self.root / "advancer"
        subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(advancer)], check=True)
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            expected_commit,
        )
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=advancer, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=advancer,
            check=True,
        )
        (advancer / "advanced.txt").write_text("advanced\n", encoding="utf-8")
        subprocess.run(["git", "add", "advanced.txt"], cwd=advancer, check=True)
        subprocess.run(["git", "commit", "-qm", "advance main"], cwd=advancer, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=advancer, check=True)

        module = runpy.run_path(str(self.script))
        result = subprocess.run(
            [
                "bash",
                "-lc",
                f"set -euo pipefail; {module['refresh_git_shell'](expected_commit)}",
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            expected_commit,
        )
        self.assertNotEqual(
            subprocess.run(
                ["git", "rev-parse", "origin/main"],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            expected_commit,
        )

    def test_refresh_failure_reports_partial_deployment_without_changing_default(self) -> None:
        self.prepare_refresh_checkout()
        destination = self.write_default()
        original = destination.read_text(encoding="utf-8")
        self.env["FAKE_REMOTE_STATUS"] = "1"

        result = self.run_script("--refresh-default")

        self.assertEqual(result.returncode, 2)
        self.assertIn("local guidance is updated", result.stderr)
        self.assertIn("default selection was unchanged", result.stderr)
        self.assertEqual(destination.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
