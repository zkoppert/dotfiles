#!/usr/bin/env python3
"""Regression tests for install.sh."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tarfile
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
            '  [ "$3" = --json ] || exit 90\n'
            '  if [ -n "${FAKE_COPILOT_PLUGINS_JSON+x}" ]; then\n'
            '    printf \'%s\\n\' "$FAKE_COPILOT_PLUGINS_JSON"\n'
            '  elif [ -f "$HOME/.copilot/gho11y-installed" ]; then\n'
            "    enabled=true\n"
            '    [ -f "$HOME/.copilot/gho11y-disabled" ] && enabled=false\n'
            '    printf \'[{"name":"gho11y","enabled":%s}]\\n\' "$enabled"\n'
            "  else\n"
            "    printf '[]\\n'\n"
            "  fi\n"
            '  exit "${FAKE_COPILOT_LIST_STATUS:-0}"\n'
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
        self.env["FAKE_COPILOT_LIST_STATUS"] = "0"
        self.env.pop("FAKE_COPILOT_PLUGINS_JSON", None)

    def run_installer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.installer)],
            cwd=self.repo,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def prepare_node_install(self) -> None:
        scripts = {
            "uname": (
                "#!/bin/sh\n"
                'if [ "$1" = -m ]; then\n'
                "  printf 'x86_64\\n'\n"
                "else\n"
                "  printf 'Linux\\n'\n"
                "fi\n"
            ),
            "node": "#!/bin/sh\nprintf '20\\n'\n",
            "1up": "#!/bin/sh\nexit 0\n",
            "curl": (
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$HOME/curl.log"\n'
                '[ "$1" = -fsSL ] || exit 2\n'
                'case "$2" in\n'
                "  https://nodejs.org/dist/index.json)\n"
                '    printf \'%s\\n\' "$FAKE_NODE_INDEX"\n'
                '    exit "$FAKE_NODE_INDEX_EXIT"\n'
                "    ;;\n"
                "  https://nodejs.org/dist/v22.0.0/node-v22.0.0-linux-x64.tar.xz)\n"
                '    [ "$3" = -o ] && [ "$#" -eq 4 ] || exit 2\n'
                '    printf \'%s\\n\' "$4" > "$HOME/node-archive-path"\n'
                '    cp "$FAKE_NODE_ARCHIVE" "$4" || exit "$?"\n'
                '    exit "${FAKE_NODE_ARCHIVE_EXIT:-0}"\n'
                "    ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n"
            ),
        }
        for name, script in scripts.items():
            command = self.fake_bin / name
            command.write_text(script, encoding="utf-8")
            command.chmod(0o755)
        self.env.update(
            {
                "FAKE_NODE_INDEX": json.dumps(
                    [{"version": "v24.0.0"}, {"version": "v22.0.0"}]
                ),
                "FAKE_NODE_INDEX_EXIT": "0",
                "TMPDIR": str(self.root),
            }
        )
        runtime = self.root / "node-runtime"
        (runtime / "bin").mkdir(parents=True)
        for name in ("node", "npm", "npx", "corepack"):
            command = runtime / "bin" / name
            command.write_text("#!/bin/sh\nprintf 'v22.0.0\\n'\n", encoding="utf-8")
            command.chmod(0o755)
        archive_path = self.root / "node-release.tar.xz"
        with tarfile.open(archive_path, "w:xz") as archive:
            archive.add(runtime, arcname="node-v22.0.0-linux-x64")
        self.env["FAKE_NODE_ARCHIVE"] = str(archive_path)

    def prepare_existing_node_runtime(self) -> Path:
        runtime = self.home / ".local" / "share" / "node-v22"
        (runtime / "bin").mkdir(parents=True)
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        for name in ("node", "npm", "npx", "corepack"):
            command = runtime / "bin" / name
            command.write_text("#!/bin/sh\nprintf 'v20.19.0\\n'\n", encoding="utf-8")
            command.chmod(0o755)
            (local_bin / name).symlink_to(command)
        (runtime / "obsolete.txt").write_text("previous runtime\n", encoding="utf-8")
        return runtime

    def assert_node_commands_version(self, version: str) -> None:
        for name in ("node", "npm", "npx", "corepack"):
            result = subprocess.run(
                [str(self.home / ".local" / "bin" / name), "--version"],
                env=self.env, check=True, capture_output=True, text=True,
            )
            self.assertEqual(result.stdout, version + "\n", name)

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
            "graphql-availability-investigator",
            "milestone-release-tracking",
            "prod-explain",
            "gh-axi",
        )
        self.assertEqual(
            {path.name for path in (self.home / ".copilot" / "skills").iterdir()},
            set(expected_skills),
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

    def test_disabled_plugin_produces_an_enable_instruction(self) -> None:
        plugin_dir = self.home / ".copilot"
        plugin_dir.mkdir()
        (plugin_dir / "gho11y-installed").touch()
        disabled = plugin_dir / "gho11y-disabled"
        disabled.touch()

        result = self.run_installer()

        self.assertIn("Copilot plugin gho11y is not enabled", result.stdout)
        self.assertIn("copilot plugin enable gho11y", result.stdout)
        self.assertNotIn("Copilot plugin gho11y is already installed", result.stdout)
        self.assertNotIn("Installed Copilot plugin gho11y", result.stdout)
        self.assertTrue(disabled.exists())
        self.assertEqual(
            (self.home / "copilot.log").read_text(encoding="utf-8").splitlines(),
            ["plugin list --json"],
        )
        self.assertIn("Dotfiles install complete.", result.stdout)

    def test_plugin_listing_failures_do_not_trigger_installation(self) -> None:
        for payload, status in (
            ("not JSON", 0),
            ("{}", 0),
            ('[{"name":"gho11y","enabled":true}]', 1),
        ):
            with self.subTest(payload=payload, status=status):
                self.env["FAKE_COPILOT_PLUGINS_JSON"] = payload
                self.env["FAKE_COPILOT_LIST_STATUS"] = str(status)

                result = self.run_installer()

                self.assertIn("Cannot list Copilot plugins", result.stdout)
                self.assertIn("copilot plugin list --json", result.stdout)
                self.assertIn("Dotfiles install complete.", result.stdout)
                self.assertFalse((self.home / ".copilot" / "gho11y-installed").exists())

    def test_missing_catalog_source_skips_catalog_tools(self) -> None:
        del self.env["COPILOT_SKILL_CATALOG_REPO"]

        result = self.run_installer()

        self.assertIn(
            "COPILOT_SKILL_CATALOG_REPO is not set",
            result.stdout,
        )
        self.assertFalse((self.home / "gh.log").exists())
        self.assertFalse((self.home / "copilot.log").exists())

    def test_codespace_copilot_commands_are_linked(self) -> None:
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        commands = (
            "copilot2",
            "copilot-codespace-session",
            "bootstrap-copilot-mcp",
            "verify-codespace-copilot-env",
        )
        for command in commands:
            source = bin_dir / command
            source.write_text("#!/bin/sh\n", encoding="utf-8")
            source.chmod(0o755)

        first_run = self.run_installer()
        second_run = self.run_installer()

        for command in commands:
            target = self.home / ".local" / "bin" / command
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), (bin_dir / command).resolve())
            self.assertEqual(first_run.stdout.count(f"Linked {command}"), 1)
            self.assertEqual(second_run.stdout.count(f"Linked {command}"), 1)

    def test_linux_installs_node_22_when_current_version_is_older(self) -> None:
        self.prepare_node_install()

        result = self.run_installer()

        self.assertIn("Installed Node.js v22.0.0", result.stdout)
        for name in ("node", "npm", "npx", "corepack"):
            target = self.home / ".local" / "bin" / name
            self.assertTrue(target.is_symlink())
            self.assertTrue(target.is_file())
            self.assertEqual(
                target.resolve(),
                (self.home / ".local" / "share" / "node-v22" / "bin" / name).resolve(),
            )
        version = subprocess.run(
            [str(self.home / ".local" / "bin" / "node"), "--version"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(version.stdout, "v22.0.0\n")
        downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
        self.assertFalse(downloaded_archive.exists())

    def test_node_update_replaces_the_existing_runtime_after_extraction(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        self.assert_node_commands_version("v20.19.0")

        for _ in range(2):
            result = self.run_installer()

            self.assertIn("Installed Node.js v22.0.0", result.stdout)
            self.assert_node_commands_version("v22.0.0")
            self.assertFalse((runtime / "obsolete.txt").exists())
            self.assertEqual(set(runtime.parent.iterdir()), {runtime})

    def test_partial_node_extraction_preserves_the_existing_runtime(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        self.assert_node_commands_version("v20.19.0")
        (self.root / "node-runtime" / "new-only.txt").write_text("new runtime\n", encoding="utf-8")
        with tarfile.open(self.env["FAKE_NODE_ARCHIVE"], "w:xz") as archive:
            archive.add(self.root / "node-runtime", arcname="node-v22.0.0-linux-x64")
            broken_link = tarfile.TarInfo("node-v22.0.0-linux-x64/broken-link")
            broken_link.type = tarfile.LNKTYPE
            broken_link.linkname = "node-v22.0.0-linux-x64/missing-target"
            archive.addfile(broken_link)

        result = self.run_installer()

        self.assertIn("Failed to install Node.js 22", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)
        self.assert_node_commands_version("v20.19.0")
        self.assertEqual((runtime / "obsolete.txt").read_text(), "previous runtime\n")
        self.assertFalse((runtime / "new-only.txt").exists())
        self.assertEqual(set(runtime.parent.iterdir()), {runtime})
        downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
        self.assertFalse(downloaded_archive.exists())

    def test_failed_node_promotion_restores_the_existing_runtime(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        real_mv = shutil.which("mv")
        if real_mv is None:
            self.fail("mv is required to exercise install.sh")
        mv = self.fake_bin / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = "$HOME/.local/share/node-v22" ] && [ ! -e "$HOME/node-move-failed" ]; then\n'
            '  : > "$HOME/node-move-failed"\n'
            "  exit 1\n"
            "fi\n"
            f'exec {shlex.quote(real_mv)} "$@"\n',
            encoding="utf-8",
        )
        mv.chmod(0o755)

        result = self.run_installer()

        self.assertIn("Failed to install Node.js 22", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)
        self.assertTrue((self.home / "node-move-failed").exists())
        self.assert_node_commands_version("v20.19.0")
        self.assertEqual((runtime / "obsolete.txt").read_text(), "previous runtime\n")
        self.assertEqual(set(runtime.parent.iterdir()), {runtime})

    def test_node_staging_is_cleaned_when_command_linking_fails(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        ln = self.fake_bin / "ln"
        ln.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        ln.chmod(0o755)

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer()

        self.assert_node_commands_version("v22.0.0")
        self.assertEqual(set(runtime.parent.iterdir()), {runtime})
        downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
        self.assertFalse(downloaded_archive.exists())

    def test_node_download_does_not_follow_a_preexisting_archive_symlink(self) -> None:
        self.prepare_node_install()
        owned = self.root / "owned-file"
        owned.write_text("keep me\n", encoding="utf-8")
        predictable = self.root / "node-v22.0.0-linux-x64.tar.xz"
        predictable.symlink_to(owned)

        result = self.run_installer()

        self.assertIn("Installed Node.js v22.0.0", result.stdout)
        self.assertEqual(owned.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue(predictable.is_symlink())
        self.assertEqual(predictable.readlink(), owned)
        downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
        self.assertNotEqual(downloaded_archive, predictable)
        self.assertFalse(downloaded_archive.exists())

    def test_node_archive_is_removed_after_download_or_extraction_failure(self) -> None:
        self.prepare_node_install()
        for failure in ("download", "extraction"):
            with self.subTest(failure=failure):
                self.env["FAKE_NODE_ARCHIVE_EXIT"] = "22" if failure == "download" else "0"
                if failure == "extraction":
                    Path(self.env["FAKE_NODE_ARCHIVE"]).write_bytes(b"not an archive")

                result = self.run_installer()

                self.assertIn("Failed to install Node.js 22", result.stdout)
                self.assertIn("Dotfiles install complete.", result.stdout)
                self.assertFalse((self.home / ".local" / "bin" / "node").exists())
                downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
                self.assertFalse(downloaded_archive.exists())

    def test_node_install_preserves_a_non_directory_runtime_path(self) -> None:
        self.prepare_node_install()
        node_dir = self.home / ".local" / "share" / "node-v22"
        node_dir.parent.mkdir(parents=True)
        node_dir.write_text("keep me\n", encoding="utf-8")

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer()

        self.assertEqual(node_dir.read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse((self.home / ".local" / "bin" / "node").exists())

    def test_node_install_preserves_user_managed_commands(self) -> None:
        self.prepare_node_install()
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        for name in ("node", "npm", "npx", "corepack"):
            command = local_bin / name
            command.write_text(
                f"#!/bin/sh\nprintf 'user-managed {name}\\n'\n", encoding="utf-8"
            )
            command.chmod(0o755)

        for _ in range(2):
            result = self.run_installer()

            self.assertIn("Dotfiles install complete.", result.stdout)
            for name in ("node", "npm", "npx", "corepack"):
                command = local_bin / name
                self.assertFalse(command.is_symlink())
                self.assertEqual(
                    command.read_text(encoding="utf-8"),
                    f"#!/bin/sh\nprintf 'user-managed {name}\\n'\n",
                )
                self.assertIn(
                    f"{command} exists and is not a symlink - skipping", result.stdout
                )

    def test_node_install_preserves_directories_and_refreshes_symlinks(self) -> None:
        self.prepare_node_install()
        local_bin = self.home / ".local" / "bin"
        owned_directory = local_bin / "node"
        owned_directory.mkdir(parents=True)
        (owned_directory / "owned.txt").write_text("keep me\n", encoding="utf-8")
        old_target = self.root / "old-npm"
        old_target.write_text("owned command\n", encoding="utf-8")
        (local_bin / "npm").symlink_to(old_target)
        (local_bin / "npx").symlink_to(self.root / "missing-npx")

        result = self.run_installer()

        self.assertIn(
            f"{owned_directory} exists and is not a symlink - skipping", result.stdout
        )
        self.assertEqual({path.name for path in owned_directory.iterdir()}, {"owned.txt"})
        self.assertEqual(old_target.read_text(encoding="utf-8"), "owned command\n")
        for name in ("npm", "npx", "corepack"):
            target = local_bin / name
            self.assertTrue(target.is_symlink())
            self.assertTrue(target.is_file())
            self.assertEqual(
                target.resolve(),
                (self.home / ".local" / "share" / "node-v22" / "bin" / name).resolve(),
            )

    def test_node_release_lookup_failures_do_not_stop_command_linking(self) -> None:
        self.prepare_node_install()
        source = self.repo / "bin" / "copilot2"
        source.parent.mkdir()
        source.write_text("#!/bin/sh\n", encoding="utf-8")
        source.chmod(0o755)
        target = self.home / ".local" / "bin" / "copilot2"
        curl_log = self.home / "curl.log"
        cases = (
            ("network failure", "", 6),
            ("invalid JSON", "{", 0),
            ("no Node.js 22 release", '[{"version":"v24.0.0"}]', 0),
            ("failed transfer with valid JSON", '[{"version":"v22.0.0"}]', 18),
        )
        for name, index, status in cases:
            with self.subTest(case=name):
                target.unlink(missing_ok=True)
                curl_log.unlink(missing_ok=True)
                self.env["FAKE_NODE_INDEX"] = index
                self.env["FAKE_NODE_INDEX_EXIT"] = str(status)

                result = self.run_installer()

                self.assertIn("Cannot look up Node.js 22", result.stdout)
                self.assertIn("rerun ./install.sh", result.stdout)
                self.assertIn("Dotfiles install complete.", result.stdout)
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.resolve(), source.resolve())
                self.assertFalse((self.home / ".local/share/node-v22").exists())
                self.assertEqual(
                    curl_log.read_text(encoding="utf-8").splitlines(),
                    ["-fsSL https://nodejs.org/dist/index.json"],
                )

    def prepare_1up_install(self) -> None:
        (self.fake_bin / "uname").write_text(
            "#!/bin/sh\nprintf 'Linux\\n'\n",
            encoding="utf-8",
        )
        npx = self.fake_bin / "npx"
        npx.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        npx.chmod(0o755)
        node = self.fake_bin / "node"
        node.write_text("#!/bin/sh\nprintf '22\\n'\n", encoding="utf-8")
        node.chmod(0o755)
        go = self.fake_bin / "go"
        go.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$HOME/go.log\"\n"
            "mkdir -p \"$GOBIN\"\n"
            "printf '#!/bin/sh\\nexit 0\\n' > \"$GOBIN/1up\"\n"
            "chmod +x \"$GOBIN/1up\"\n",
            encoding="utf-8",
        )
        go.chmod(0o755)
        self.env["COPILOT_1UP_MODULE"] = "private.example/one-up@v1.2.3"
        # A host-installed 1up would skip the installation this test exercises.
        for name in ("dirname", "mkdir", "chmod", "python3"):
            command = shutil.which(name)
            self.assertIsNotNone(command, f"{name} is required to exercise install.sh")
            (self.fake_bin / name).symlink_to(command)
        self.env["PATH"] = str(self.fake_bin)

    def test_linux_installs_private_1up_module(self) -> None:
        self.prepare_1up_install()

        result = self.run_installer()

        self.assertIn("Installed 1up", result.stdout)
        target = self.home / ".local" / "bin" / "1up"
        self.assertTrue(target.is_file())
        installed = target.read_bytes()
        repeated = self.run_installer()
        self.assertIn("exists - skipping 1up install", repeated.stdout)
        self.assertEqual(target.read_bytes(), installed)
        self.assertEqual(
            (self.home / "go.log").read_text(encoding="utf-8").splitlines(),
            ["install private.example/one-up@v1.2.3"],
        )

    def test_1up_install_preserves_existing_targets_outside_path(self) -> None:
        self.prepare_1up_install()
        target = self.home / ".local" / "bin" / "1up"
        target.parent.mkdir(parents=True)
        owned = self.home / "owned-1up"
        owned.write_text("keep me\n", encoding="utf-8")
        missing = self.home / "missing-1up"
        content = "#!/bin/sh\nprintf 'user-managed 1up\\n'\n"

        for kind in ("executable", "file", "directory", "symlink", "dangling-symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    target.mkdir()
                    (target / "owned.txt").write_text(content, encoding="utf-8")
                elif kind in ("symlink", "dangling-symlink"):
                    target.symlink_to(owned if kind == "symlink" else missing)
                else:
                    target.write_text(content, encoding="utf-8")
                    target.chmod(0o755 if kind == "executable" else 0o644)
                mode = target.lstat().st_mode

                result = self.run_installer()

                self.assertIn("exists - skipping 1up install", result.stdout)
                self.assertFalse((self.home / "go.log").exists())
                self.assertEqual(target.lstat().st_mode, mode)
                self.assertEqual(owned.read_text(encoding="utf-8"), "keep me\n")
                self.assertFalse(missing.exists())
                if kind == "directory":
                    self.assertEqual((target / "owned.txt").read_text(encoding="utf-8"), content)
                    shutil.rmtree(target)
                elif kind in ("symlink", "dangling-symlink"):
                    self.assertEqual(target.readlink(), owned if kind == "symlink" else missing)
                    target.unlink()
                else:
                    self.assertEqual(target.read_text(encoding="utf-8"), content)
                    target.unlink()

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
            8,
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
        companion.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

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
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n",
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
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n",
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
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n",
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
