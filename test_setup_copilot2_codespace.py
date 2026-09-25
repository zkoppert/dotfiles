#!/usr/bin/env python3
"""Regression tests for default Copilot Codespace provisioning."""

from __future__ import annotations

import json
import os
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
        self.script = Path(__file__).with_name("bin") / "setup-copilot2-codespace"
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
            "  *) printf 'unexpected gh args: %s\\n' \"$*\" >&2; exit 90 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        self.gh.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
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


if __name__ == "__main__":
    unittest.main()
