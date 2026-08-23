#!/usr/bin/env python3
"""Regression tests for install.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallScriptTest(unittest.TestCase):
    """Exercise install.sh in an isolated home directory."""

    RC_FILES = (".zshrc", ".bashrc", ".bash_profile")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.repo = self.home / "repos" / "dotfiles"
        self.fake_bin = self.root / "bin"
        self.repo.mkdir(parents=True)
        self.fake_bin.mkdir()

        source = Path(__file__).with_name("install.sh")
        self.installer = self.repo / "install.sh"
        self.installer.write_bytes(source.read_bytes())
        self.installer.chmod(0o755)

        uname = self.fake_bin / "uname"
        uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        uname.chmod(0o755)

        gh = self.fake_bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$HOME/gh.log\"\n"
            "if [ \"$1\" = skill ] && [ \"$2\" = install ]; then\n"
            "  [ \"${FAKE_GH_FAIL:-0}\" = 1 ] && exit 1\n"
            "  skill_name=${4#skills/}\n"
            "  mkdir -p \"$HOME/.copilot/skills/$skill_name\"\n"
            "  printf '# installed\\n' > \"$HOME/.copilot/skills/$skill_name/SKILL.md\"\n"
            "fi\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

        copilot = self.fake_bin / "copilot"
        copilot.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$HOME/copilot.log\"\n"
            "if [ \"$1\" = plugin ] && [ \"$2\" = list ]; then\n"
            "  [ -f \"$HOME/.copilot/gho11y-installed\" ] && printf '  \\342\\200\\242 gho11y (v1.0.0)\\n'\n"
            "elif [ \"$1\" = plugin ] && [ \"$2\" = install ]; then\n"
            "  [ \"${FAKE_COPILOT_FAIL:-0}\" = 1 ] && exit 1\n"
            "  mkdir -p \"$HOME/.copilot\"\n"
            "  : > \"$HOME/.copilot/gho11y-installed\"\n"
            "fi\n",
            encoding="utf-8",
        )
        copilot.chmod(0o755)

        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.fake_bin}{os.pathsep}{self.env['PATH']}"
        self.env["COPILOT_SKILL_CATALOG_REPO"] = "private/catalog"

    def run_installer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.installer)],
            cwd=self.repo,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_review_lab_alias_is_added_once_on_macos(self) -> None:
        self.run_installer()
        self.run_installer()

        alias_line = "alias deploy='gh review-lab deploy'  # dotfiles: review lab"
        for rc_name in self.RC_FILES:
            content = (self.home / rc_name).read_text(encoding="utf-8")
            self.assertEqual(content.count(alias_line), 1)

    def test_catalog_tools_are_installed_once(self) -> None:
        self.run_installer()
        second_run = self.run_installer()

        expected_skills = (
            "validate-pr-with-codespace",
            "session-portability",
            "cleanup-worktrees",
            "remediate-accessibility-audit",
        )
        gh_calls = (self.home / "gh.log").read_text(encoding="utf-8").splitlines()
        for skill_name in expected_skills:
            expected_call = (
                f"skill install private/catalog skills/{skill_name} "
                "--agent github-copilot --scope user"
            )
            self.assertEqual(gh_calls.count(expected_call), 1)
            self.assertEqual(
                (
                    self.home
                    / ".copilot"
                    / "skills"
                    / skill_name
                    / "SKILL.md"
                ).read_text(encoding="utf-8"),
                "# installed\n",
            )
            self.assertIn(
                f"Copilot skill {skill_name} is already installed",
                second_run.stdout,
            )

        copilot_calls = (
            self.home / "copilot.log"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            copilot_calls.count("plugin install private/catalog:plugins/gho11y"),
            1,
        )
        self.assertIn(
            "Copilot plugin gho11y is already installed",
            second_run.stdout,
        )

    def test_missing_catalog_source_skips_catalog_tools(self) -> None:
        del self.env["COPILOT_SKILL_CATALOG_REPO"]

        result = self.run_installer()

        self.assertIn(
            "COPILOT_SKILL_CATALOG_REPO is not set",
            result.stdout,
        )
        self.assertFalse((self.home / "gh.log").exists())
        self.assertFalse((self.home / "copilot.log").exists())

    def test_missing_catalog_commands_are_nonfatal(self) -> None:
        (self.fake_bin / "gh").unlink()
        (self.fake_bin / "copilot").unlink()
        self.env["PATH"] = f"{self.fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"

        result = self.run_installer()

        self.assertIn("gh is missing", result.stdout)
        self.assertIn("copilot is missing", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)

    def test_catalog_install_failures_are_nonfatal(self) -> None:
        self.env["FAKE_GH_FAIL"] = "1"
        self.env["FAKE_COPILOT_FAIL"] = "1"

        result = self.run_installer()

        self.assertEqual(
            result.stdout.count("Failed to install Copilot skill"),
            4,
        )
        self.assertIn("Failed to install Copilot plugin gho11y", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)

    def test_existing_deploy_alias_is_preserved(self) -> None:
        definitions = (
            "alias deploy='echo existing'\n",
            "alias 'deploy=echo existing'\n",
            'alias "deploy=echo existing"\n',
            "alias 'deploy'='echo existing'\n",
            'alias "deploy"="echo existing"\n',
        )
        for definition in definitions:
            with self.subTest(definition=definition):
                for rc_name in self.RC_FILES:
                    (self.home / rc_name).write_text(definition, encoding="utf-8")

                result = self.run_installer()

                self.assertEqual(result.stdout.count("already defines deploy"), 3)
                for rc_name in self.RC_FILES:
                    self.assertEqual(
                        (self.home / rc_name).read_text(encoding="utf-8"),
                        definition,
                    )

    def test_existing_deploy_function_is_preserved(self) -> None:
        definitions = (
            "deploy() { echo existing; }\n",
            "function deploy() { echo existing; }\n",
            "function deploy { echo existing; }\n",
        )
        for definition in definitions:
            with self.subTest(definition=definition):
                for rc_name in self.RC_FILES:
                    (self.home / rc_name).write_text(definition, encoding="utf-8")

                result = self.run_installer()

                self.assertEqual(result.stdout.count("already defines deploy"), 3)
                for rc_name in self.RC_FILES:
                    self.assertEqual(
                        (self.home / rc_name).read_text(encoding="utf-8"),
                        definition,
                    )

    def test_babysit_launch_agent_is_linked_and_reloaded(self) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        source_plist = launch_agents / "com.zkoppert.babysit-prs.plist"
        source_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        wrapper = self.repo / "bin" / "babysit-prs"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        wrapper.chmod(0o755)
        companion = self.home / "repos" / "babysit-prs" / "babysit_prs.py"
        companion.parent.mkdir()
        companion.write_text(
            "import sys\n"
            "if '--help' in sys.argv:\n"
            "    print('--review-lab-repo OWNER/REPO --preview-repo OWNER/REPO')\n",
            encoding="utf-8",
        )

        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

        self.run_installer()

        target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.zkoppert.babysit-prs.plist"
        )
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), source_plist.resolve())
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertIn(f"unload {target}", calls)
        self.assertIn(f"load {target}", calls)

    def test_babysit_launch_agent_requires_compatible_companion(self) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        (launch_agents / "com.zkoppert.babysit-prs.plist").write_text(
            "<plist version=\"1.0\"></plist>\n",
            encoding="utf-8",
        )
        wrapper = self.repo / "bin" / "babysit-prs"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        wrapper.chmod(0o755)
        companion = self.home / "repos" / "babysit-prs" / "babysit_prs.py"
        companion.parent.mkdir()
        companion.write_text("print('legacy help')\n", encoding="utf-8")

        result = self.run_installer()

        self.assertIn("update", result.stdout)
        self.assertIn("before enabling review-lab deployment", result.stdout)
        target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.zkoppert.babysit-prs.plist"
        )
        self.assertFalse(target.exists())

    def test_babysit_launch_agent_skips_nonstandard_checkout(self) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        (launch_agents / "com.zkoppert.babysit-prs.plist").write_text(
            "<plist version=\"1.0\"></plist>\n",
            encoding="utf-8",
        )
        relocated = self.root / "relocated"
        relocated.mkdir()
        relocated_launch_agents = relocated / "LaunchAgents"
        relocated_launch_agents.mkdir()
        (relocated_launch_agents / "com.zkoppert.babysit-prs.plist").write_text(
            "<plist version=\"1.0\"></plist>\n",
            encoding="utf-8",
        )
        relocated_installer = relocated / "install.sh"
        relocated_installer.write_bytes(self.installer.read_bytes())
        relocated_installer.chmod(0o755)

        result = subprocess.run(
            [str(relocated_installer)],
            cwd=relocated,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Skipping babysit-prs launchd agent", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
