#!/usr/bin/env python3
"""Regression tests for install.sh."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallScriptTest(unittest.TestCase):
    """Exercise install.sh in an isolated home directory."""

    RC_FILES = (".zshrc", ".bashrc", ".bash_profile")
    NO_SERVICES = (
        'if [ "$1" = "print" ]; then\n'
        '  printf \'Could not find service "%s"\\n\' "$2" >&2\n'
        '  exit 1\n'
        'fi\n'
    )

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

        python3 = self.fake_bin / "python3"
        python3.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
            "  target=$3\n"
            "  mkdir -p \"$target/bin\"\n"
            "  cat > \"$target/bin/python3\" <<'EOF'\n"
            "#!/bin/sh\n"
            "set -eu\n"
            'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "-" ] || [ "$1" = "-c" ]; then\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
            "EOF\n"
            "  chmod +x \"$target/bin/python3\"\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "-" ] || [ "$1" = "-c" ]; then\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python3.chmod(0o755)

        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "print" ]; then\n'
            "  printf '%s\\n' 'Could not find service \"$2\"'\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

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
        dirname = shutil.which("dirname")
        if dirname is None:
            self.fail("dirname is required to exercise install.sh")
        (self.fake_bin / "dirname").symlink_to(dirname)
        self.env["PATH"] = str(self.fake_bin)

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

    def test_notification_worker_runtime_is_provisioned_once(self) -> None:
        requirements = self.repo / "python" / "notification-worker-requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("PyYAML==6.0.2\nruamel.yaml==0.18.6\n", encoding="utf-8")
        expected_hash = hashlib.sha256(requirements.read_bytes()).hexdigest()

        first_run = self.run_installer()
        second_run = self.run_installer()

        runtime_python = self.home / ".local/share/dotfiles/notification-workers/venv/bin/python3"
        stamp = self.home / ".local/share/dotfiles/notification-workers/requirements.sha256"

        self.assertTrue(runtime_python.exists())
        self.assertTrue(stamp.exists())
        self.assertEqual(stamp.read_text(encoding="utf-8").strip(), expected_hash)
        self.assertIn("Provisioned notification worker runtime", first_run.stdout)
        self.assertIn("Notification worker runtime already provisioned", second_run.stdout)

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
        companion.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n" + self.NO_SERVICES,
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

    def test_notification_jobs_are_unloaded_before_activation(self) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        triage_plist = launch_agents / "com.zkoppert.notification-triage.plist"
        triage_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        dependabot_plist = launch_agents / "com.zkoppert.triage-dependabot.plist"
        dependabot_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in ("notification-triage", "triage-dependabot"):
            wrapper = bin_dir / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
        activity_log = self.home / "install.log"
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "unload" ]; then\n'
            "  : > \"$HOME/notification-teardown-ready\"\n"
            "fi\n"
            "printf '%s\\n' \"launchctl $*\" >> \"$HOME/install.log\"\n"
            'if [ "$1" = "print" ]; then\n'
            '  if [ -e "$HOME/notification-teardown-ready" ]; then\n'
            '    printf "%s\\n" "Could not find service \"$2\""\n'
            "    exit 1\n"
            "  fi\n"
            "  printf '%s\\n' 'State = running'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "bootout" ]; then\n'
            "  : > \"$HOME/notification-teardown-ready\"\n"
            "  exit 0\n"
            "fi\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        gh = self.fake_bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "marker=no\n"
            'if [ -e "$HOME/notification-teardown-ready" ]; then marker=yes; fi\n'
            "printf '%s\\n' \"gh $* marker=$marker\" >> \"$HOME/install.log\"\n"
            "if [ \"$1\" = skill ] && [ \"$2\" = install ]; then\n"
            "  skill_name=${4#skills/}\n"
            "  mkdir -p \"$HOME/.copilot/skills/$skill_name\"\n"
            "  printf '# installed\\n' > \"$HOME/.copilot/skills/$skill_name/SKILL.md\"\n"
            "fi\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        triage_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.notification-triage.plist"
        triage_target.parent.mkdir(parents=True)
        triage_target.symlink_to(triage_plist)
        dependabot_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.triage-dependabot.plist"
        dependabot_target.symlink_to(dependabot_plist)

        self.run_installer()

        calls = activity_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"launchctl unload {triage_target}", calls)
        self.assertIn(f"launchctl unload {dependabot_target}", calls)
        self.assertNotIn(f"launchctl load {triage_target}", calls)
        self.assertNotIn(f"launchctl load {dependabot_target}", calls)
        gh_skill_lines = [
            entry for entry in calls if entry.startswith("gh skill install ")
        ]
        self.assertTrue(gh_skill_lines)
        self.assertTrue(all("marker=yes" in entry for entry in gh_skill_lines))
        self.assertLess(
            calls.index(f"launchctl unload {triage_target}"),
            calls.index(gh_skill_lines[0]),
        )
        self.assertFalse(triage_target.exists())
        self.assertFalse(dependabot_target.exists())

    def test_notification_jobs_only_remove_expected_symlinks(self) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        triage_plist = launch_agents / "com.zkoppert.notification-triage.plist"
        triage_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        dependabot_plist = launch_agents / "com.zkoppert.triage-dependabot.plist"
        dependabot_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in ("notification-triage", "triage-dependabot"):
            wrapper = bin_dir / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
        activity_log = self.home / "install.log"
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"launchctl $*\" >> \"$HOME/install.log\"\n" + self.NO_SERVICES,
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        foreign_target = self.root / "foreign" / "com.zkoppert.notification-triage.plist"
        foreign_target.parent.mkdir(parents=True)
        foreign_target.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        triage_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.notification-triage.plist"
        triage_target.parent.mkdir(parents=True)
        triage_target.symlink_to(foreign_target)
        dependabot_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.triage-dependabot.plist"
        dependabot_target.symlink_to(dependabot_plist)

        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_installer()
        self.assertIn("Installation stopped", error.exception.stderr)

        calls = activity_log.read_text(encoding="utf-8").splitlines()
        self.assertNotIn(f"launchctl bootout gui/{os.getuid()}/com.zkoppert.notification-triage", calls)
        self.assertNotIn(f"launchctl unload {triage_target}", calls)
        self.assertIn(f"launchctl unload {dependabot_target}", calls)
        self.assertTrue(triage_target.is_symlink())
        self.assertEqual(triage_target.resolve(), foreign_target.resolve())
        self.assertFalse(dependabot_target.exists())

    def test_notification_jobs_remove_when_absent_service_is_confirmed(self) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        triage_plist = launch_agents / "com.zkoppert.notification-triage.plist"
        triage_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        dependabot_plist = launch_agents / "com.zkoppert.triage-dependabot.plist"
        dependabot_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in ("notification-triage", "triage-dependabot"):
            wrapper = bin_dir / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "unload" ]; then\n'
            "  exit 1\n"
            "fi\n"
            'if [ "$1" = "print" ]; then\n'
            "  printf '%s\\n' \"Could not find service \\\"$2\\\"\"\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        triage_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.notification-triage.plist"
        triage_target.parent.mkdir(parents=True)
        triage_target.symlink_to(triage_plist)
        dependabot_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.triage-dependabot.plist"
        dependabot_target.symlink_to(dependabot_plist)

        self.run_installer()

        self.assertFalse(triage_target.exists())
        self.assertFalse(dependabot_target.exists())

    def exercise_owned_notification_bootout(
        self, *, unload_status: int = 1, bootout_status: int = 0, removes: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], list[Path], list[str]]:
        requirements = self.repo / "python/notification-worker-requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("PyYAML==6.0.2\nruamel.yaml==0.18.10\n", encoding="utf-8")
        targets = []
        for worker in ("notification-triage", "triage-dependabot"):
            name = f"com.zkoppert.{worker}.plist"
            source = self.repo / "LaunchAgents" / name
            source.parent.mkdir(exist_ok=True)
            source.write_text('<plist version="1.0"></plist>\n', encoding="utf-8")
            target = self.home / "Library/LaunchAgents" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
            targets.append(target)
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$HOME/launchctl.log"\n'
            'marker="$HOME/bootedout-${2##*/}"\n'
            'case "$1" in\n'
            f'  unload) exit {unload_status} ;;\n'
            '  print)\n'
            '    if [ -e "$marker" ]; then\n'
            '      printf \'Could not find service "%s"\\n\' "$2" >&2\n'
            '      exit 1\n'
            '    fi\n'
            "    printf 'state = running\\n'\n"
            '    exit 0 ;;\n'
            '  bootout)\n'
            + ('    : > "$marker"\n' if removes else '')
            + f'    exit {bootout_status} ;;\n'
            + 'esac\n',
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        result = subprocess.run(
            [str(self.installer)], cwd=self.repo, env=self.env,
            capture_output=True, text=True, check=False,
        )
        calls = (self.home / "launchctl.log").read_text().splitlines()
        return result, targets, calls

    def test_owned_running_notification_jobs_boot_out_when_unload_fails(self) -> None:
        result, targets, calls = self.exercise_owned_notification_bootout()
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in targets:
            service = f"gui/{os.getuid()}/{target.stem}"
            self.assertIn(f"bootout {service}", calls)
            self.assertGreaterEqual(calls.count(f"print {service}"), 2)
            self.assertFalse(target.is_symlink())
        self.assertFalse(any(call.startswith("load ") for call in calls))
        self.assertIn("Provisioned notification worker runtime", result.stdout)

    def test_owned_notification_unload_success_still_requires_absence(self) -> None:
        result, targets, calls = self.exercise_owned_notification_bootout(unload_status=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call.startswith("bootout ") for call in calls), 2)
        self.assertTrue(all(not target.is_symlink() for target in targets))

    def test_owned_notification_bootout_failure_stops_installation(self) -> None:
        result, targets, calls = self.exercise_owned_notification_bootout(bootout_status=1, removes=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Installation stopped", result.stderr)
        self.assertTrue(all(target.is_symlink() for target in targets))
        self.assertEqual(sum(call.startswith("bootout ") for call in calls), 2)
        self.assertFalse((self.home / ".local/share/dotfiles/notification-workers/venv").exists())
        self.assertFalse((self.home / "gh.log").exists())

    def test_owned_notification_bootout_success_without_absence_stops_installation(self) -> None:
        result, targets, _calls = self.exercise_owned_notification_bootout(removes=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Installation stopped", result.stderr)
        self.assertTrue(all(target.is_symlink() for target in targets))
        self.assertFalse((self.home / ".local/share/dotfiles/notification-workers/venv").exists())

    def test_notification_jobs_stay_linked_when_unload_cannot_be_verified(self) -> None:
        requirements = self.repo / "python" / "notification-worker-requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("PyYAML==6.0.2\nruamel.yaml==0.18.6\n", encoding="utf-8")

        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        triage_plist = launch_agents / "com.zkoppert.notification-triage.plist"
        triage_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        dependabot_plist = launch_agents / "com.zkoppert.triage-dependabot.plist"
        dependabot_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in ("notification-triage", "triage-dependabot"):
            wrapper = bin_dir / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
        activity_log = self.home / "install.log"
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"launchctl $*\" >> \"$HOME/install.log\"\n"
            'if [ "$1" = "unload" ]; then\n'
            "  exit 1\n"
            "fi\n"
            'if [ "$1" = "print" ]; then\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        triage_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.notification-triage.plist"
        triage_target.parent.mkdir(parents=True)
        triage_target.symlink_to(triage_plist)
        dependabot_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.triage-dependabot.plist"
        dependabot_target.symlink_to(dependabot_plist)

        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_installer()
        self.assertIn("Installation stopped", error.exception.stderr)

        calls = activity_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"launchctl unload {triage_target}", calls)
        self.assertIn(f"launchctl unload {dependabot_target}", calls)
        self.assertTrue(triage_target.is_symlink())
        self.assertTrue(dependabot_target.is_symlink())
        self.assertFalse((self.home / ".local/share/dotfiles/notification-workers/venv").exists())
        self.assertFalse((self.home / ".local/share/dotfiles/notification-workers/requirements.sha256").exists())

    def test_notification_jobs_skip_runtime_provisioning_when_launch_agents_still_exist_in_nonstandard_checkout(self) -> None:
        relocated = self.root / "relocated"
        relocated.mkdir()
        launch_agents = relocated / "LaunchAgents"
        launch_agents.mkdir()
        triage_plist = launch_agents / "com.zkoppert.notification-triage.plist"
        dependabot_plist = launch_agents / "com.zkoppert.triage-dependabot.plist"
        (relocated / "python").mkdir()
        (relocated / "python" / "notification-worker-requirements.txt").write_text(
            "PyYAML==6.0.2\nruamel.yaml==0.18.6\n",
            encoding="utf-8",
        )
        relocated_installer = relocated / "install.sh"
        relocated_installer.write_bytes((self.repo / "install.sh").read_bytes())
        relocated_installer.chmod(0o755)

        activity_log = self.home / "install.log"
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"launchctl $*\" >> \"$HOME/install.log\"\n"
            'if [ "$1" = "print" ]; then\n'
            "  printf '%s\\n' 'Could not find service \"$2\"'\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        triage_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.notification-triage.plist"
        triage_target.parent.mkdir(parents=True)
        triage_target.symlink_to(triage_plist)
        dependabot_target = self.home / "Library" / "LaunchAgents" / "com.zkoppert.triage-dependabot.plist"
        dependabot_target.symlink_to(dependabot_plist)

        result = subprocess.run(
            [str(relocated_installer)],
            cwd=relocated,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(activity_log.exists())
        self.assertIn("Installation stopped", result.stderr)
        self.assertTrue(triage_target.is_symlink())
        self.assertTrue(dependabot_target.is_symlink())
        self.assertFalse((self.home / ".local/share/dotfiles/notification-workers/venv").exists())
        self.assertFalse((self.home / ".local/share/dotfiles/notification-workers/requirements.sha256").exists())

    def test_notification_jobs_boot_out_targetless_loaded_services_before_provisioning(self) -> None:
        requirements = self.repo / "python" / "notification-worker-requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("PyYAML==6.0.2\nruamel.yaml==0.18.6\n", encoding="utf-8")

        activity_log = self.home / "install.log"
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"launchctl $*\" >> \"$HOME/install.log\"\n"
            'label_key=$(printf "%s" "$2" | tr "/" "_")\n'
            'marker="$HOME/bootedout-$label_key"\n'
            'worker=${2##*/com.zkoppert.}\n'
            'if [ "$1" = "print" ]; then\n'
            '  printf "program = %s\\n" "$HOME/repos/dotfiles/bin/$worker"\n'
            '  if [ -e "$marker" ]; then\n'
            "    printf '%s\\n' 'Could not find service \"$2\"'\n"
            "    exit 1\n"
            "  fi\n"
            "  printf '%s\\n' 'State = running'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "bootout" ]; then\n'
            "  : > \"$marker\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

        result = subprocess.run(
            [str(self.installer)],
            cwd=self.repo,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

        calls = activity_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"launchctl print gui/{os.getuid()}/com.zkoppert.notification-triage", calls)
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/com.zkoppert.notification-triage", calls)
        self.assertIn(f"launchctl print gui/{os.getuid()}/com.zkoppert.triage-dependabot", calls)
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/com.zkoppert.triage-dependabot", calls)
        self.assertIn("Provisioned notification worker runtime", result.stdout)
        self.assertTrue((self.home / ".local/share/dotfiles/notification-workers/venv/bin/python3").exists())
        self.assertTrue((self.home / ".local/share/dotfiles/notification-workers/requirements.sha256").exists())

    def test_accessibility_picker_is_linked_and_loaded_with_private_config(
        self,
    ) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        source_plist = (
            launch_agents / "com.zkoppert.accessibility-issue-picker.plist"
        )
        source_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in (
            "accessibility-issue-picker",
            "resume-accessibility-session",
        ):
            wrapper = bin_dir / command
            script = "#!/bin/sh\n"
            if command == "accessibility-issue-picker":
                script += "printf '%s\\n' \"$*\" >> \"$HOME/accessibility-picker.log\"\n"
            wrapper.write_text(script, encoding="utf-8")
            wrapper.chmod(0o755)
        config = self.home / ".config" / "accessibility-issue-picker.env"
        config.parent.mkdir()
        config.write_text(
            "ACCESSIBILITY_ISSUE_REPO=example/project\n"
            "ACCESSIBILITY_AUDIT_REPO=example/audits\n"
            "ACCESSIBILITY_LABELS=accessibility\n"
            "ACCESSIBILITY_ASSIGNEE=zkoppert\n"
            "ACCESSIBILITY_GITHUB_TOKEN=repository-scoped-token\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n" + self.NO_SERVICES,
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

        self.run_installer()

        for command in (
            "accessibility-issue-picker",
            "resume-accessibility-session",
        ):
            target = self.home / ".local" / "bin" / command
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), (bin_dir / command).resolve())
        plist_target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.zkoppert.accessibility-issue-picker.plist"
        )
        self.assertTrue(plist_target.is_symlink())
        self.assertEqual(plist_target.resolve(), source_plist.resolve())
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertIn(f"unload {plist_target}", calls)
        self.assertIn(f"load {plist_target}", calls)
        validation = (self.home / "accessibility-picker.log").read_text(
            encoding="utf-8"
        )
        self.assertEqual(validation, "--validate-schedule\n")

    def test_accessibility_picker_does_not_load_without_private_config(
        self,
    ) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        (launch_agents / "com.zkoppert.accessibility-issue-picker.plist").write_text(
            "<plist version=\"1.0\"></plist>\n",
            encoding="utf-8",
        )
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in (
            "accessibility-issue-picker",
            "resume-accessibility-session",
        ):
            wrapper = bin_dir / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)

        result = self.run_installer()

        self.assertIn("create", result.stdout)
        self.assertIn("accessibility-issue-picker.env", result.stdout)
        plist_target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.zkoppert.accessibility-issue-picker.plist"
        )
        self.assertFalse(plist_target.exists())

    def test_accessibility_picker_unloads_job_when_schedule_validation_fails(
        self,
    ) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        source_plist = (
            launch_agents / "com.zkoppert.accessibility-issue-picker.plist"
        )
        source_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        picker_wrapper = bin_dir / "accessibility-issue-picker"
        picker_wrapper.write_text(
            "#!/bin/sh\n"
            "printf 'accessibility-issue-picker: Remediation workdir does not exist: github_pat_SECRET\\n' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        picker_wrapper.chmod(0o755)
        resume_wrapper = bin_dir / "resume-accessibility-session"
        resume_wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        resume_wrapper.chmod(0o755)
        config = self.home / ".config" / "accessibility-issue-picker.env"
        config.parent.mkdir()
        config.write_text(
            "ACCESSIBILITY_ISSUE_REPO=example/project\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        plist_target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.zkoppert.accessibility-issue-picker.plist"
        )
        plist_target.parent.mkdir(parents=True)
        plist_target.symlink_to(source_plist)
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n" + self.NO_SERVICES,
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

        result = self.run_installer()

        self.assertFalse(plist_target.exists())
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertIn(f"unload {plist_target}", calls)
        self.assertIn(
            "accessibility remediation workdir does not exist",
            result.stdout,
        )
        self.assertNotIn("github_pat_SECRET", result.stdout)

    def test_accessibility_picker_hides_unstructured_validation_stderr(
        self,
    ) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        source_plist = (
            launch_agents / "com.zkoppert.accessibility-issue-picker.plist"
        )
        source_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        picker_wrapper = bin_dir / "accessibility-issue-picker"
        picker_wrapper.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '/config.env: line 2: github_pat_SECRET: command not found' >&2\n"
            "printf '%s\\n' 'accessibility-issue-picker: Schedule validation failed' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        picker_wrapper.chmod(0o755)
        resume_wrapper = bin_dir / "resume-accessibility-session"
        resume_wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        resume_wrapper.chmod(0o755)
        config = self.home / ".config" / "accessibility-issue-picker.env"
        config.parent.mkdir()
        config.write_text(
            "ACCESSIBILITY_ISSUE_REPO=example/project\n",
            encoding="utf-8",
        )
        config.chmod(0o600)

        result = self.run_installer()

        self.assertIn("accessibility picker schedule validation failed", result.stdout)
        self.assertNotIn("github_pat_SECRET", result.stdout)

    def test_accessibility_picker_unloads_existing_job_without_private_config(
        self,
    ) -> None:
        launch_agents = self.repo / "LaunchAgents"
        launch_agents.mkdir()
        source_plist = (
            launch_agents / "com.zkoppert.accessibility-issue-picker.plist"
        )
        source_plist.write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        for command in (
            "accessibility-issue-picker",
            "resume-accessibility-session",
        ):
            wrapper = bin_dir / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
        plist_target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.zkoppert.accessibility-issue-picker.plist"
        )
        plist_target.parent.mkdir(parents=True)
        plist_target.symlink_to(source_plist)
        launchctl = self.fake_bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n" + self.NO_SERVICES,
            encoding="utf-8",
        )
        launchctl.chmod(0o755)

        result = self.run_installer()

        self.assertFalse(plist_target.exists())
        calls = (self.home / "launchctl.log").read_text(encoding="utf-8")
        self.assertIn(f"unload {plist_target}", calls)
        self.assertIn("Unloaded accessibility issue picker", result.stdout)

    def test_accessibility_picker_skips_nonstandard_checkout(self) -> None:
        relocated = self.root / "relocated"
        (relocated / "LaunchAgents").mkdir(parents=True)
        (
            relocated
            / "LaunchAgents"
            / "com.zkoppert.accessibility-issue-picker.plist"
        ).write_text("<plist version=\"1.0\"></plist>\n", encoding="utf-8")
        (relocated / "bin").mkdir()
        for command in (
            "accessibility-issue-picker",
            "resume-accessibility-session",
        ):
            wrapper = relocated / "bin" / command
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
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

        self.assertIn(
            "Skipping accessibility issue picker launchd agent",
            result.stdout,
        )
        self.assertFalse(
            (self.home / ".local/bin/accessibility-issue-picker").exists()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
