#!/usr/bin/env python3
"""Regression tests for install.sh."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.skill_metadata import pi_sdk_path


class InstallScriptTest(unittest.TestCase):
    """Exercise install.sh in an isolated home directory."""

    RC_FILES = (".zshrc", ".bashrc", ".bash_profile")
    CATALOG_SKILLS = (
        "validate-pr-with-codespace",
        "session-portability",
        "cleanup-worktrees",
        "remediate-accessibility-audit",
        "graphql-availability-investigator",
        "milestone-release-tracking",
        "prod-explain",
        "gh-axi",
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
        metadata_source = Path(__file__).with_name("scripts") / "skill_metadata.py"
        metadata_target = self.repo / "scripts" / "skill_metadata.py"
        metadata_target.parent.mkdir()
        shutil.copy2(metadata_source, metadata_target)
        self.pi_package = self.root / "npm" / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent"
        (self.pi_package / "dist" / "bundle").mkdir(parents=True)
        (self.pi_package / "package.json").write_text(json.dumps({
            "name": "@earendil-works/pi-coding-agent", "type": "module", "main": "./dist/index.js",
        }), encoding="utf-8")
        (self.pi_package / "dist" / "index.js").write_text("export {};\n", encoding="utf-8")
        cli = self.pi_package / "dist" / "bundle" / "cli.js"
        cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cli.chmod(0o755)
        (self.fake_bin / "pi").symlink_to(cli)
        node = self.fake_bin / "node"
        node.write_text(
            "#!/bin/sh\n"
            'if [ "$*" = --version ]; then\n'
            '  printf \'%s\\n\' "$FAKE_CURRENT_NODE_VERSION"\n'
            '  exit "$FAKE_CURRENT_NODE_EXIT"\n'
            'fi\n'
            'if [ -n "$REAL_NODE" ]; then exec "$REAL_NODE" "$@"; fi\n'
            '[ "$1" = --input-type=module ] || exit 2\n'
            f"exec {shlex.quote(sys.executable)} -c 'import json, sys; "
            "print(json.dumps({path: [] for path in json.load(sys.stdin)}))'\n",
            encoding="utf-8",
        )
        node.chmod(0o755)

        uname = self.fake_bin / "uname"
        uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        uname.chmod(0o755)

        gh = self.fake_bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$HOME/gh.log\"\n"
            "if [ \"$1\" = stack ] && [ \"$2\" = --help ]; then\n"
            "  [ -f \"$HOME/.gh-stack-installed\" ] && exit 0\n"
            "  exit 1\n"
            "elif [ \"$1\" = extension ] && [ \"$2\" = install ] && [ \"$3\" = github/gh-stack ]; then\n"
            "  : > \"$HOME/.gh-stack-installed\"\n"
            "elif [ \"$1\" = skill ] && [ \"$2\" = install ]; then\n"
            "  [ \"${FAKE_GH_FAIL:-0}\" = 1 ] && exit 1\n"
            "  skill_name=${4#skills/}\n"
            "  mkdir -p \"$HOME/.copilot/skills/$skill_name\"\n"
            '  if [ "$skill_name" = gh-axi ] && [ -n "${FAKE_CATALOG_CONTENT+x}" ]; then\n'
            '    printf \'%s\' "$FAKE_CATALOG_CONTENT" > "$HOME/.copilot/skills/$skill_name/SKILL.md"\n'
            '    exit 0\n'
            '  fi\n'
            "  printf '%s\\n' '---' 'description: Catalog fixture' 'metadata:' "
            '"  github-path: skills/$skill_name" '
            "'  github-ref: refs/heads/main' '  github-repo: https://example.invalid/catalog' "
            "'  hermes:' '    tags: [git, automation]' "
            '"name: $skill_name" \'---\' "# $skill_name" > "$HOME/.copilot/skills/$skill_name/SKILL.md"\n'
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
        for name in ("PI_CODING_AGENT_DIR", "PI_REAL_PI", "REAL_NODE", "FAKE_CATALOG_CONTENT"):
            self.env.pop(name, None)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.fake_bin}{os.pathsep}{self.env['PATH']}"
        self.env["GIT_CONFIG_GLOBAL"] = str(self.home / ".gitconfig")
        self.env["GIT_CONFIG_SYSTEM"] = os.devnull
        self.env["COPILOT_SKILL_CATALOG_REPO"] = "private/catalog"
        self.env["FAKE_CURRENT_NODE_VERSION"] = "v22.19.0"
        self.env["FAKE_CURRENT_NODE_EXIT"] = "0"
        self.env["FAKE_COPILOT_LIST_STATUS"] = "0"
        self.env.pop("FAKE_COPILOT_PLUGINS_JSON", None)

    def use_real_pi_sdk(self) -> None:
        if shutil.which("pi") is None or shutil.which("node") is None:
            self.fail("Pi and Node.js are required for catalog metadata regressions")
        sdk = pi_sdk_path()
        self.env["REAL_NODE"] = shutil.which("node")
        (self.pi_package / "dist" / "index.js").write_text(
            f"export {{ DefaultResourceLoader, SettingsManager, parseFrontmatter }} from {json.dumps(sdk.as_uri())};\n",
            encoding="utf-8",
        )

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
            "node": (
                "#!/bin/sh\n"
                '[ "$*" = --version ] || exit 90\n'
                'printf \'%s\\n\' "$FAKE_CURRENT_NODE_VERSION"\n'
                'exit "$FAKE_CURRENT_NODE_EXIT"\n'
            ),
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
                "  https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-x64.tar.xz)\n"
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
                "FAKE_CURRENT_NODE_VERSION": "v22.18.12",
                "FAKE_CURRENT_NODE_EXIT": "0",
                "FAKE_NODE_INDEX": json.dumps(
                    [{"version": "v24.0.0"}, {"version": "v22.19.0"}]
                ),
                "FAKE_NODE_INDEX_EXIT": "0",
                "TMPDIR": str(self.root),
            }
        )
        runtime = self.root / "node-runtime"
        (runtime / "bin").mkdir(parents=True)
        for name in ("node", "npm", "npx", "corepack"):
            command = runtime / "bin" / name
            command.write_text(
                "#!/bin/sh\n"
                'if [ "$*" = --version ]; then printf \'v22.19.0\\n\'; exit 0; fi\n'
                '[ "$1" = --input-type=module ] || exit 2\n'
                f"exec {shlex.quote(sys.executable)} -c 'import json, sys; "
                "print(json.dumps({path: [] for path in json.load(sys.stdin)}))'\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
        archive_path = self.root / "node-release.tar.xz"
        with tarfile.open(archive_path, "w:xz") as archive:
            archive.add(runtime, arcname="node-v22.19.0-linux-x64")
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

    def install_routed_skills(self) -> set[str]:
        source_root = Path(__file__).parent
        manifest = json.loads(
            (source_root / "tests/fixtures/instruction-migration/manifest.json").read_text(encoding="utf-8")
        )
        routed_skills = {name for names in manifest["pre_action_routes"].values() for name in names}
        for name in routed_skills:
            if name not in manifest["external_skills"]:
                shutil.copytree(
                    source_root / ".copilot" / "skills" / name,
                    self.repo / ".copilot" / "skills" / name,
                )
        source_instructions = source_root / ".github" / "copilot-instructions.md"
        (self.repo / ".github").mkdir()
        shutil.copy2(source_instructions, self.repo / ".github" / "copilot-instructions.md")

        self.run_installer()
        return routed_skills

    def test_routed_skills_are_discoverable_in_both_clients(self) -> None:
        routed_skills = self.install_routed_skills()
        for root in (self.home / ".copilot" / "skills", self.home / ".agents" / "skills"):
            for name in routed_skills:
                with self.subTest(root=root, skill=name):
                    installed = root / name
                    owner = self.home if name == "gh-axi" else self.repo
                    expected = owner / ".copilot" / "skills" / name
                    self.assertTrue((installed / "SKILL.md").is_file())
                    self.assertEqual(installed.resolve(), expected.resolve())
        copilot_instructions = self.home / ".copilot" / "copilot-instructions.md"
        pi_instructions = self.home / ".pi" / "agent" / "AGENTS.md"
        self.assertTrue(copilot_instructions.is_symlink())
        self.assertTrue(pi_instructions.is_symlink())
        installed_source = self.repo / ".github" / "copilot-instructions.md"
        self.assertEqual(copilot_instructions.resolve(), installed_source.resolve())
        self.assertEqual(pi_instructions.resolve(), installed_source.resolve())
        self.assertFalse((self.home / "tests" / "fixtures" / "instruction-migration").exists())

    def test_installed_routes_reach_the_real_pi_sdk(self) -> None:
        self.use_real_pi_sdk()
        routed_skills = self.install_routed_skills()
        verifier = Path(__file__).with_name("bin") / "verify-codespace-copilot-env"
        program = (
            "import json, runpy, sys; from pathlib import Path; "
            "discover = runpy.run_path(sys.argv[1])['effective_pi_skills']; "
            "print(json.dumps(discover(Path.home() / '.pi' / 'agent')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(verifier)],
            cwd=self.repo, env=self.env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        winners = json.loads(result.stdout)
        self.assertTrue(routed_skills <= winners.keys())
        for name in routed_skills:
            expected = (self.home if name == "gh-axi" else self.repo) / ".copilot" / "skills" / name / "SKILL.md"
            self.assertEqual(Path(winners[name]["filePath"]).resolve(), expected.resolve())
            self.assertFalse(winners[name]["disableModelInvocation"])

    def test_instruction_links_follow_the_active_pi_agent_directory(self) -> None:
        instructions = self.repo / ".github" / "copilot-instructions.md"
        instructions.parent.mkdir()
        instructions.write_text("# Shared guidance\n", encoding="utf-8")
        default = self.home / ".pi" / "agent" / "AGENTS.md"
        for setting, directory in (
            (None, default.parent),
            ("", default.parent),
            (str(self.home / "absolute agent"), self.home / "absolute agent"),
            ("~/custom agent", self.home / "custom agent"),
            ("~", self.home),
        ):
            with self.subTest(setting=setting):
                default.unlink(missing_ok=True)
                if setting is None:
                    self.env.pop("PI_CODING_AGENT_DIR", None)
                else:
                    self.env["PI_CODING_AGENT_DIR"] = setting

                self.run_installer()

                for target in (
                    self.home / ".copilot" / "copilot-instructions.md",
                    directory / "AGENTS.md",
                ):
                    self.assertTrue(target.is_symlink())
                    self.assertEqual(target.resolve(), instructions.resolve())
                    self.assertEqual(target.read_bytes(), b"# Shared guidance\n")
                if directory != default.parent:
                    self.assertFalse(default.exists())

    def test_relative_pi_agent_directory_is_rejected(self) -> None:
        instructions = self.repo / ".github" / "copilot-instructions.md"
        instructions.parent.mkdir()
        instructions.write_text("# Shared guidance\n", encoding="utf-8")
        self.env["PI_CODING_AGENT_DIR"] = "relative-agent"

        result = subprocess.run(
            [str(self.installer)],
            cwd=self.repo,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("PI_CODING_AGENT_DIR must be absolute or start with ~/", result.stderr)
        self.assertFalse((self.repo / "relative-agent").exists())

    def test_custom_pi_instruction_files_are_preserved(self) -> None:
        instructions = self.repo / ".github" / "copilot-instructions.md"
        instructions.parent.mkdir()
        instructions.write_text("# Shared guidance\n", encoding="utf-8")
        self.env["PI_CODING_AGENT_DIR"] = "~/custom agent"
        target = self.home / "custom agent" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text("Keep my guidance.\n", encoding="utf-8")

        for _ in range(2):
            result = self.run_installer()

            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), b"Keep my guidance.\n")
            self.assertIn(f"{target} exists and is not a symlink", result.stdout)
            self.assertFalse((self.home / ".pi" / "agent" / "AGENTS.md").exists())

    def test_existing_instruction_and_skill_files_are_preserved(self) -> None:
        source_skill = Path(__file__).with_name(".copilot") / "skills" / "actions-security"
        shutil.copytree(source_skill, self.repo / ".copilot" / "skills" / "actions-security")
        source_instructions = Path(__file__).with_name(".github") / "copilot-instructions.md"
        (self.repo / ".github").mkdir()
        shutil.copy2(source_instructions, self.repo / ".github" / "copilot-instructions.md")
        sentinels = {
            self.home / ".copilot" / "copilot-instructions.md": self.home / ".copilot" / "copilot-instructions.md",
            self.home / ".pi" / "agent" / "AGENTS.md": self.home / ".pi" / "agent" / "AGENTS.md",
            self.home / ".copilot" / "skills" / "actions-security" / "SKILL.md": self.home / ".copilot" / "skills" / "actions-security",
            self.home / ".agents" / "skills" / "actions-security" / "SKILL.md": self.home / ".agents" / "skills" / "actions-security",
        }
        pi_shadows = (
            self.home / ".pi" / "agent" / "AGENTS.override.md",
            self.home / ".pi" / "agent" / "skills" / "actions-security" / "SKILL.md",
        )
        for path in (*sentinels, *pi_shadows):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep me\n", encoding="utf-8")

        result = self.run_installer()

        for path in (*sentinels, *pi_shadows):
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_text(encoding="utf-8"), "keep me\n")
        for warning_target in sentinels.values():
            self.assertIn(f"{warning_target} exists and is not a symlink", result.stdout)

    def test_review_lab_alias_is_added_once_on_macos(self) -> None:
        self.run_installer()
        self.run_installer()

        alias_line = "alias deploy='gh review-lab deploy'  # dotfiles: review lab"
        for rc_name in self.RC_FILES:
            content = (self.home / rc_name).read_text(encoding="utf-8")
            self.assertEqual(content.count(alias_line), 1)

    def test_cfs_package_guard_is_installed_once_on_macos(self) -> None:
        source_guard = Path(__file__).with_name("bin") / "cfs-package-guard"
        guard_dir = self.repo / "bin"
        guard_dir.mkdir()
        guard = guard_dir / "cfs-package-guard"
        guard.write_bytes(source_guard.read_bytes())
        guard.chmod(0o755)

        self.run_installer()
        self.run_installer()

        installed_dir = self.home / ".local" / "libexec" / "cfs-package-guard" / "bin"
        for command in ("pip", "pip3", "pip-compile", "pip-sync", "uv", "uvx"):
            self.assertEqual(guard.resolve(), (installed_dir / command).resolve())
        path_line = 'export PATH="$HOME/.local/libexec/cfs-package-guard/bin:$PATH"  # dotfiles: CFS package guard'
        for rc_name in (".zprofile", ".zshrc", ".profile", ".bash_profile", ".bashrc"):
            self.assertEqual(1, (self.home / rc_name).read_text(encoding="utf-8").count(path_line))

    def test_catalog_tools_are_installed_once(self) -> None:
        self.run_installer()
        second_run = self.run_installer()

        for root in (self.home / ".copilot" / "skills", self.home / ".agents" / "skills"):
            self.assertEqual({path.name for path in root.iterdir()}, set(self.CATALOG_SKILLS))
            for name in self.CATALOG_SKILLS:
                self.assertEqual(
                    (root / name / "SKILL.md").read_text(encoding="utf-8"),
                    "---\ndescription: Catalog fixture\nmetadata:\n"
                    f"  github-path: skills/{name}\n"
                    "  github-ref: refs/heads/main\n"
                    "  github-repo: https://example.invalid/catalog\n"
                    "  hermes:\n    tags: [git, automation]\n"
                    f"name: {name}\n---\n# {name}\n",
                )
                self.assertEqual(
                    (root / name).resolve(),
                    (self.home / ".copilot" / "skills" / name).resolve(),
                )
        gh_calls = (self.home / "gh.log").read_text(encoding="utf-8").splitlines()
        for name in self.CATALOG_SKILLS:
            expected_call = (
                f"skill install private/catalog skills/{name} "
                "--agent github-copilot --scope user"
            )
            self.assertEqual(gh_calls.count(expected_call), 1)
            self.assertIn(f"Copilot skill {name} is already installed", second_run.stdout)

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

    def test_macos_catalog_validation_requires_supported_node(self) -> None:
        self.use_real_pi_sdk()
        for version, supported in (("v22.18.0", False), ("v22.18.12", False), ("v22.19.0", True)):
            with self.subTest(version=version):
                self.env["FAKE_CURRENT_NODE_VERSION"] = version

                result = self.run_installer()

                if supported:
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertIn(f"Node.js 22.19.0 or later is required (found {version})", result.stderr)
                for name in self.CATALOG_SKILLS:
                    source = self.home / ".copilot" / "skills" / name
                    mirror = self.home / ".agents" / "skills" / name
                    self.assertTrue((source / "SKILL.md").is_file())
                    if supported:
                        self.assertTrue(mirror.is_symlink())
                        self.assertEqual(mirror.resolve(), source.resolve())
                    else:
                        self.assertFalse(mirror.exists())
                self.assertFalse((self.home / ".local" / "share" / "node-v22").exists())
                self.assertFalse((self.home / ".local" / "bin" / "node").exists())

    def test_unavailable_validation_preserves_existing_catalog_skills(self) -> None:
        originals = {}
        for name in self.CATALOG_SKILLS:
            source = self.home / ".copilot" / "skills" / name
            source.mkdir(parents=True)
            originals[source / "SKILL.md"] = f"---\nname: {name}\ndescription: Existing catalog skill\n---\n# Retained {name}\n"
            originals[source / "reference.txt"] = f"Keep the existing {name} reference.\n"
            mirror = self.home / ".agents" / "skills" / name
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.symlink_to(source)
        for path, content in originals.items():
            path.write_text(content, encoding="utf-8")

        for failure, version, error in (
            ("runtime", "v22.18.12", "Node.js 22.19.0 or later is required"),
            ("SDK", "v22.19.0", "cannot locate the installed Pi SDK"),
        ):
            self.env["FAKE_CURRENT_NODE_VERSION"] = version
            if failure == "SDK":
                (self.pi_package / "dist" / "index.js").unlink()
            for attempt in range(2):
                with self.subTest(failure=failure, attempt=attempt):
                    result = self.run_installer()

                    calls = [shlex.split(line) for line in (self.home / "gh.log").read_text(encoding="utf-8").splitlines()]
                    self.assertFalse(any(call[:2] == ["skill", "install"] for call in calls), calls)
                    self.assertIn(error, result.stderr)
                    for path, content in originals.items():
                        self.assertEqual(path.read_text(encoding="utf-8"), content)
                    for name in self.CATALOG_SKILLS:
                        self.assertIn(f"Cannot validate existing catalog skill {name}; skipping installation", result.stdout)
                        mirror = self.home / ".agents" / "skills" / name
                        self.assertEqual(mirror.readlink(), self.home / ".copilot" / "skills" / name)

    def test_nested_catalog_metadata_is_preserved_on_reinstall(self) -> None:
        self.use_real_pi_sdk()
        source = self.home / ".copilot" / "skills" / "gh-axi"
        source.mkdir(parents=True)
        content = (
            "---\ndescription: >-\n  Existing catalog guidance.\n"
            "metadata:\n  github-path: skills/gh-axi\n  hermes:\n    tags: [github, git]\n"
            "name: gh-axi\n---\n# GitHub operations\n"
        )
        (source / "SKILL.md").write_text(content, encoding="utf-8")
        asset = source / "reference.txt"
        asset.write_text("Keep this asset.\n", encoding="utf-8")

        self.run_installer()
        repeated = self.run_installer()

        self.assertIn("Copilot skill gh-axi is already installed", repeated.stdout)
        self.assertEqual((source / "SKILL.md").read_text(encoding="utf-8"), content)
        self.assertEqual(asset.read_text(encoding="utf-8"), "Keep this asset.\n")
        calls = (self.home / "gh.log").read_text(encoding="utf-8").splitlines()
        for name in self.CATALOG_SKILLS:
            expected_calls = 0 if name == "gh-axi" else 1
            self.assertEqual(calls.count(
                f"skill install private/catalog skills/{name} --agent github-copilot --scope user"
            ), expected_calls)
            self.assertEqual(
                (self.home / ".agents" / "skills" / name).resolve(),
                (self.home / ".copilot" / "skills" / name).resolve(),
            )

    def test_invalid_catalog_metadata_is_not_mirrored_without_catalog_configuration(self) -> None:
        self.use_real_pi_sdk()
        del self.env["COPILOT_SKILL_CATALOG_REPO"]
        source = self.home / ".copilot" / "skills" / "gh-axi" / "SKILL.md"
        source.parent.mkdir(parents=True)
        mirror = self.home / ".agents" / "skills" / "gh-axi"
        for content in (
            "---\nmetadata:\n  name: gh-axi\ndescription: Nested name only\n---\n# Skill\n",
            "---\nname: gh-axi\ndescription: Disabled\ndisable-model-invocation: true\n---\n# Skill\n",
            "---\nname: gh-axi\ndescription: [invalid\n---\n# Skill\n",
        ):
            with self.subTest(content=content):
                source.write_text(content, encoding="utf-8")

                result = self.run_installer()

                self.assertIn("Cannot validate catalog skill gh-axi; skipping Pi mirror", result.stdout)
                self.assertFalse(mirror.exists())
                self.assertEqual(source.read_text(encoding="utf-8"), content)
        source.write_text(
            "---\nname: gh-axi\ndescription: Valid\nmetadata:\n  tags: [git]\n---\n# Skill\n", encoding="utf-8",
        )

        self.run_installer()

        self.assertEqual(mirror.resolve(), source.parent.resolve())
        self.assertFalse((self.home / "copilot.log").exists())

    def test_invalid_catalog_install_is_rejected_then_repaired(self) -> None:
        self.use_real_pi_sdk()
        self.env["FAKE_CATALOG_CONTENT"] = "---\nname: gh-axi\ndescription: Disabled\ndisable-model-invocation: true\n---\n"
        mirror = self.home / ".agents" / "skills" / "gh-axi"

        failed = self.run_installer()

        self.assertIn("Failed to install Copilot skill gh-axi", failed.stdout)
        self.assertFalse(mirror.exists())
        self.assertTrue((self.home / ".agents" / "skills" / "cleanup-worktrees" / "SKILL.md").is_file())
        del self.env["FAKE_CATALOG_CONTENT"]

        repaired = self.run_installer()

        self.assertIn("Installed Copilot skill gh-axi", repaired.stdout)
        self.assertTrue((mirror / "SKILL.md").is_file())
        self.assertEqual(mirror.resolve(), (self.home / ".copilot" / "skills" / "gh-axi").resolve())

    def test_existing_catalog_skills_are_mirrored_without_catalog_configuration(self) -> None:
        self.run_installer()
        del self.env["COPILOT_SKILL_CATALOG_REPO"]
        for name in self.CATALOG_SKILLS:
            (self.home / ".agents" / "skills" / name).unlink()

        result = self.run_installer()

        self.assertIn("COPILOT_SKILL_CATALOG_REPO is not set", result.stdout)
        for name in self.CATALOG_SKILLS:
            mirror = self.home / ".agents" / "skills" / name
            source = self.home / ".copilot" / "skills" / name
            self.assertTrue(mirror.is_symlink())
            self.assertEqual(mirror.resolve(), source.resolve())
            self.assertTrue((mirror / "SKILL.md").is_file())

    def test_catalog_mirrors_preserve_owned_files_and_directories(self) -> None:
        target = self.home / ".agents" / "skills" / "gh-axi"
        target.parent.mkdir(parents=True)
        for kind in ("file", "directory"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    target.mkdir()
                    sentinel = target / "SKILL.md"
                else:
                    sentinel = target
                sentinel.write_text("keep me\n", encoding="utf-8")

                result = self.run_installer()

                self.assertFalse(target.is_symlink())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")
                self.assertIn(f"{target} exists and is not a symlink", result.stdout)
                sentinel.unlink()
                if kind == "directory":
                    target.rmdir()

    def test_catalog_mirrors_refresh_existing_and_broken_symlinks(self) -> None:
        owned = self.root / "owned-skill"
        owned.mkdir()
        sentinel = owned / "SKILL.md"
        sentinel.write_text("keep me\n", encoding="utf-8")
        mirror_root = self.home / ".agents" / "skills"
        mirror_root.mkdir(parents=True)
        (mirror_root / "gh-axi").symlink_to(owned)
        (mirror_root / "prod-explain").symlink_to(self.root / "missing-skill")

        self.run_installer()

        for name in ("gh-axi", "prod-explain"):
            self.assertTrue((mirror_root / name).is_symlink())
            self.assertEqual(
                (mirror_root / name).resolve(),
                (self.home / ".copilot" / "skills" / name).resolve(),
            )
            self.assertTrue((mirror_root / name / "SKILL.md").is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")

    def test_catalog_mirrors_follow_content_and_asset_updates(self) -> None:
        self.run_installer()
        source = self.home / ".copilot" / "skills" / "gh-axi"
        mirror = self.home / ".agents" / "skills" / "gh-axi"
        content = "---\nname: gh-axi\ndescription: Updated fixture\n---\n# Updated skill\n"
        (source / "SKILL.md").write_text(content, encoding="utf-8")
        (source / "reference.txt").write_text("updated reference\n", encoding="utf-8")

        self.assertEqual((mirror / "SKILL.md").read_text(encoding="utf-8"), content)
        self.assertEqual((mirror / "reference.txt").read_text(encoding="utf-8"), "updated reference\n")

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
        self.assertEqual(
            (self.home / "gh.log").read_text(encoding="utf-8").splitlines(),
            ["stack --help", "extension install github/gh-stack"],
        )
        self.assertFalse((self.home / "copilot.log").exists())

    def test_codespace_copilot_commands_are_linked(self) -> None:
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        commands = (
            "copilot2",
            "copilot-codespace-session",
            "setup-copilot2-codespace",
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

    def test_personal_git_hooks_are_configured(self) -> None:
        hooks_dir = self.repo / "git-hooks"
        hooks_dir.mkdir()
        pre_push = hooks_dir / "pre-push"
        pre_push.write_text("#!/bin/sh\n", encoding="utf-8")
        pre_push.chmod(0o755)
        pre_commit = hooks_dir / "pre-commit"
        pre_commit.write_text("#!/bin/sh\n", encoding="utf-8")
        pre_commit.chmod(0o755)
        forwarder = hooks_dir / "repository-hook-forwarder"
        forwarder.write_text("#!/bin/sh\n", encoding="utf-8")
        forwarder.chmod(0o755)

        first_run = self.run_installer()
        second_run = self.run_installer()

        result = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        managed_hooks = self.home / ".local" / "share" / "dotfiles-git-hooks"
        self.assertEqual(result.stdout.strip(), str(managed_hooks))
        self.assertEqual((managed_hooks / "pre-push").resolve(), pre_push.resolve())
        self.assertEqual(
            (managed_hooks / "commit-msg").resolve(),
            forwarder.resolve(),
        )
        self.assertIn("Configured personal Git hooks", first_run.stdout)
        self.assertIn("Configured personal Git hooks", second_run.stdout)

    def test_target_repo_local_hooks_override_blocks_install(self) -> None:
        hooks_dir = self.repo / "git-hooks"
        hooks_dir.mkdir()
        pre_push = hooks_dir / "pre-push"
        pre_push.write_text("#!/bin/sh\n", encoding="utf-8")
        pre_push.chmod(0o755)
        forwarder = hooks_dir / "repository-hook-forwarder"
        forwarder.write_text("#!/bin/sh\n", encoding="utf-8")
        forwarder.chmod(0o755)
        target_repo = self.home / "repos" / "github"
        target_repo.mkdir()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=target_repo,
            env=self.env,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                "ssh://git@github.com:22/GitHub/GitHub.git",
            ],
            cwd=target_repo,
            env=self.env,
            check=True,
        )
        subprocess.run(
            ["git", "config", "core.hooksPath", ".custom-hooks"],
            cwd=target_repo,
            env=self.env,
            check=True,
        )

        result = subprocess.run(
            [str(self.installer)],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Personal pre-push guard is inactive", result.stderr)

    def test_repository_guard_install_preserves_private_assignments_and_owned_command(self) -> None:
        command = self.repo / "bin" / "repo-guards"
        command.parent.mkdir()
        shutil.copy2(Path(__file__).with_name("bin") / "repo-guards", command)
        private = self.home / ".config/repo-guards/config.yaml"
        private.parent.mkdir(parents=True)
        private.write_text("version: 1\nrepositories:\n  example/personal: relaxed\n", encoding="utf-8")
        private.chmod(0o600)
        original = private.read_bytes()
        self.assertEqual(self.run_installer().returncode, 0)
        installed = self.home / ".local/bin/repo-guards"
        self.assertEqual(installed.resolve(), command.resolve())
        self.assertEqual(private.read_bytes(), original)
        installed.unlink()
        installed.write_text("user-owned command\n", encoding="utf-8")
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(installed.read_text(encoding="utf-8"), "user-owned command\n")
        self.assertEqual(private.read_bytes(), original)

    def test_stack_extension_is_installed_once(self) -> None:
        first_run = self.run_installer()
        second_run = self.run_installer()

        gh_calls = (self.home / "gh.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            gh_calls.count("extension install github/gh-stack"),
            1,
        )
        self.assertIn("Installed GitHub stack extension", first_run.stdout)
        self.assertIn(
            "GitHub stack extension is already installed",
            second_run.stdout,
        )

    def test_linux_installs_node_22_when_current_version_is_older(self) -> None:
        self.prepare_node_install()

        result = self.run_installer()

        self.assertIn("Installed Node.js v22.19.0", result.stdout)
        self.assertTrue((self.home / ".agents" / "skills" / "gh-axi" / "SKILL.md").is_file())
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
        self.assertEqual(version.stdout, "v22.19.0\n")
        downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
        self.assertFalse(downloaded_archive.exists())

    def test_node_versions_below_the_sdk_floor_are_upgraded(self) -> None:
        self.prepare_node_install()
        for version, exit_status in (
            ("", "127"),
            ("invalid", "0"),
            ("v20.19.0", "0"),
            ("v22.0.0", "0"),
            ("v22.18.0", "0"),
            ("v22.18.12", "0"),
            ("v22.19.0-rc.1", "0"),
            ("v22.19.0", "1"),
        ):
            with self.subTest(version=version, exit_status=exit_status):
                self.env["FAKE_CURRENT_NODE_VERSION"] = version
                self.env["FAKE_CURRENT_NODE_EXIT"] = exit_status

                result = self.run_installer()

                self.assertIn("Installed Node.js v22.19.0", result.stdout)
                self.assert_node_commands_version("v22.19.0")

    def test_supported_node_versions_do_not_trigger_downloads(self) -> None:
        self.prepare_node_install()
        original = (self.fake_bin / "node").read_bytes()
        for version in ("v22.19.0", "v22.19.1", "v22.20.0", "v23.0.0", "v24.1.0"):
            with self.subTest(version=version):
                self.env["FAKE_CURRENT_NODE_VERSION"] = version

                self.run_installer()

                self.assertFalse((self.home / "curl.log").exists())
                self.assertFalse((self.home / ".local" / "share" / "node-v22").exists())
                self.assertEqual((self.fake_bin / "node").read_bytes(), original)

    def test_node_release_selection_excludes_older_node_22_releases(self) -> None:
        self.prepare_node_install()
        self.env["FAKE_NODE_INDEX"] = json.dumps([
            {"version": "v22.18.12"}, {"version": "v22.19.0"},
        ])

        result = self.run_installer()

        self.assertIn("Installed Node.js v22.19.0", result.stdout)
        self.assert_node_commands_version("v22.19.0")

    def test_node_update_replaces_the_existing_runtime_after_extraction(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        self.assert_node_commands_version("v20.19.0")

        for _ in range(2):
            result = self.run_installer()

            self.assertIn("Installed Node.js v22.19.0", result.stdout)
            self.assert_node_commands_version("v22.19.0")
            self.assertFalse((runtime / "obsolete.txt").exists())
            self.assertEqual(set(runtime.parent.iterdir()), {runtime})

    def test_partial_node_extraction_preserves_the_existing_runtime(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        self.assert_node_commands_version("v20.19.0")
        (self.root / "node-runtime" / "new-only.txt").write_text("new runtime\n", encoding="utf-8")
        with tarfile.open(self.env["FAKE_NODE_ARCHIVE"], "w:xz") as archive:
            archive.add(self.root / "node-runtime", arcname="node-v22.19.0-linux-x64")
            broken_link = tarfile.TarInfo("node-v22.19.0-linux-x64/broken-link")
            broken_link.type = tarfile.LNKTYPE
            broken_link.linkname = "node-v22.19.0-linux-x64/missing-target"
            archive.addfile(broken_link)

        result = self.run_installer()

        self.assertIn("Failed to install Node.js 22.19.0 or later", result.stdout)
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

        self.assertIn("Failed to install Node.js 22.19.0 or later", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)
        self.assertTrue((self.home / "node-move-failed").exists())
        self.assert_node_commands_version("v20.19.0")
        self.assertEqual((runtime / "obsolete.txt").read_text(), "previous runtime\n")
        self.assertEqual(set(runtime.parent.iterdir()), {runtime})

    def test_node_staging_is_cleaned_when_command_linking_fails(self) -> None:
        self.prepare_node_install()
        runtime = self.prepare_existing_node_runtime()
        real_ln = shutil.which("ln")
        if real_ln is None:
            self.fail("ln is required to exercise install.sh")
        ln = self.fake_bin / "ln"
        ln.write_text(
            "#!/bin/sh\n"
            '[ "$3" = "$HOME/.local/bin/node" ] && exit 1\n'
            f'exec {shlex.quote(real_ln)} "$@"\n',
            encoding="utf-8",
        )
        ln.chmod(0o755)

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer()

        self.assert_node_commands_version("v22.19.0")
        self.assertEqual(set(runtime.parent.iterdir()), {runtime})
        downloaded_archive = Path((self.home / "node-archive-path").read_text().strip())
        self.assertFalse(downloaded_archive.exists())

    def test_node_download_does_not_follow_a_preexisting_archive_symlink(self) -> None:
        self.prepare_node_install()
        owned = self.root / "owned-file"
        owned.write_text("keep me\n", encoding="utf-8")
        predictable = self.root / "node-v22.19.0-linux-x64.tar.xz"
        predictable.symlink_to(owned)

        result = self.run_installer()

        self.assertIn("Installed Node.js v22.19.0", result.stdout)
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

                self.assertIn("Failed to install Node.js 22.19.0 or later", result.stdout)
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
            ("only unsupported Node.js 22 releases", '[{"version":"v22.18.12"}]', 0),
            ("failed transfer with valid JSON", '[{"version":"v22.19.0"}]', 18),
        )
        for name, index, status in cases:
            with self.subTest(case=name):
                target.unlink(missing_ok=True)
                curl_log.unlink(missing_ok=True)
                self.env["FAKE_NODE_INDEX"] = index
                self.env["FAKE_NODE_INDEX_EXIT"] = str(status)

                result = self.run_installer()

                self.assertIn("Cannot look up Node.js 22.19.0 or later", result.stdout)
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
        node.write_text("#!/bin/sh\nprintf 'v22.19.0\\n'\n", encoding="utf-8")
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
        for name in ("dirname", "mkdir", "chmod", "ln", "python3"):
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
        for name in ("dirname", "mkdir", "ln", "python3"):
            command = shutil.which(name)
            if command is None:
                self.fail(f"{name} is required to exercise install.sh")
            (self.fake_bin / name).symlink_to(command)
        self.env["PATH"] = str(self.fake_bin)
        source = self.home / ".copilot" / "skills" / "gh-axi"
        source.mkdir(parents=True)
        content = "---\nname: gh-axi\ndescription: Existing fixture\n---\n# GitHub operations\n"
        (source / "SKILL.md").write_text(content, encoding="utf-8")

        result = self.run_installer()

        self.assertIn("gh is missing", result.stdout)
        self.assertIn("copilot is missing", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)
        mirror = self.home / ".agents" / "skills" / "gh-axi"
        self.assertEqual(mirror.resolve(), source.resolve())
        self.assertEqual((mirror / "SKILL.md").read_text(encoding="utf-8"), content)

    def test_catalog_install_failures_are_nonfatal(self) -> None:
        self.env["FAKE_GH_FAIL"] = "1"
        self.env["FAKE_COPILOT_FAIL"] = "1"

        result = self.run_installer()

        self.assertEqual(
            result.stdout.count("Failed to install Copilot skill"),
            len(self.CATALOG_SKILLS),
        )
        self.assertIn("Failed to install Copilot plugin gho11y", result.stdout)
        self.assertIn("Dotfiles install complete.", result.stdout)
        self.assertFalse((self.home / ".agents" / "skills").exists())

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
