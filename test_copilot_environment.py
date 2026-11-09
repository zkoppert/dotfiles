#!/usr/bin/env python3
"""Regression tests for Codespace Copilot bootstrap and verification."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class CopilotEnvironmentTest(unittest.TestCase):
    """Exercise MCP bootstrap and environment verification safely."""

    DISCOVERY_ROOTS = {"Copilot": ".copilot/skills", "Pi": ".agents/skills"}
    REPOSITORY_SKILLS = (
        "actions-security",
        "code-review-workflow",
        "engineering-practices",
        "feature-flag-rollout",
        "github-operations",
        "memory-sweep",
        "nux-fr-handoff",
        "open-source-actions-maintenance",
        "pr-body-render-check",
        "proxima-review-lab",
        "pull-request-workflow",
        "record-demo",
        "spreadsheet-report-generation",
        "test-quality",
        "treehouse-workflow",
        "triage-dependabot",
        "triage-notifications",
        "ui-screenshot-presentation",
        "validate-style",
    )
    CATALOG_SKILLS = (
        "cleanup-worktrees",
        "gh-axi",
        "graphql-availability-investigator",
        "milestone-release-tracking",
        "prod-explain",
        "remediate-accessibility-audit",
        "session-portability",
        "validate-pr-with-codespace",
    )
    EXPECTED_SKILLS = REPOSITORY_SKILLS + CATALOG_SKILLS

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.bin_dir = self.root / "bin"
        self.home.mkdir()
        self.bin_dir.mkdir()
        self.bootstrap = Path(__file__).with_name("bin") / "bootstrap-copilot-mcp"
        self.verifier = Path(__file__).with_name("bin") / "verify-codespace-copilot-env"
        self.env = {**os.environ, "HOME": str(self.home)}

    def run_bootstrap(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.bootstrap), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_bootstrap_writes_all_servers_without_secret_values(self) -> None:
        output = self.home / ".copilot" / "mcp-config.json"
        self.env["COPILOT_MCP_SPLUNK_BEARER_TOKEN"] = "not-printed"

        result = self.run_bootstrap()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload["mcpServers"]),
            {
                "1up",
                "DataDog",
                "Kusto",
                "Sentry",
                "Slack",
                "Splunk",
                "pagerduty",
                "playwright",
            },
        )
        self.assertEqual(
            payload["mcpServers"]["DataDog"]["url"],
            "https://mcp.datadoghq.com/v1/mcp",
        )
        serialized = output.read_text(encoding="utf-8")
        self.assertNotIn("not-printed", serialized)
        splunk_environment = payload["mcpServers"]["Splunk"]["env"]
        self.assertEqual(
            splunk_environment["SPLUNK_BEARER_TOKEN"],
            "${COPILOT_MCP_SPLUNK_BEARER_TOKEN}",
        )
        self.assertEqual(
            splunk_environment["SPLUNK_HOST"],
            "${COPILOT_MCP_SPLUNK_HOST}",
        )
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_bootstrap_emits_authentication_guidance_for_approved_services(self) -> None:
        result = self.run_bootstrap()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[1:],
            [
                "Authenticate DataDog, Sentry, and Slack inside Copilot with /mcp.",
                "Splunk and PagerDuty remain configured but intentionally unauthenticated.",
                "Set catalog values as Codespaces secrets.",
            ],
        )

    def test_bootstrap_refreshes_all_managed_definitions_and_preserves_unrelated_servers(self) -> None:
        output = self.home / ".copilot" / "mcp-config.json"
        initial = self.run_bootstrap()
        self.assertEqual(initial.returncode, 0, initial.stderr)
        defaults = json.loads(output.read_text(encoding="utf-8"))["mcpServers"]
        customized = {
            name: {"type": "http", "url": f"https://custom.example/{name}", "tools": ["read"]}
            for name in defaults
        }
        unrelated = {
            "type": "http",
            "url": "https://example.com/custom",
            "tools": ["query"],
            "headers": {"Authorization": "Bearer fixture-token"},
        }
        customized["KustoCustom"] = unrelated
        output.write_text(json.dumps({"mcpServers": customized}), encoding="utf-8")

        for _ in range(2):
            result = self.run_bootstrap()

            self.assertEqual(result.returncode, 0, result.stderr)
            servers = json.loads(output.read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(servers, {**defaults, "KustoCustom": unrelated})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_bootstrap_replaces_stale_kusto_with_azure_mcp_without_flags(self) -> None:
        output = self.home / ".copilot" / "mcp-config.json"
        output.parent.mkdir(parents=True)
        unrelated = {"type": "http", "url": "https://example.com", "tools": ["read"]}
        output.write_text(
            json.dumps(
                {"mcpServers": {"Kusto": {"type": "local", "command": "node"}, "existing": unrelated}}
            ),
            encoding="utf-8",
        )

        result = self.run_bootstrap()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["mcpServers"]["Kusto"],
            {
                "type": "local",
                "command": "npx",
                "args": ["-y", "@azure/mcp@latest", "server", "start"],
                "source": "user",
                "tools": ["*"],
            },
        )
        self.assertEqual(payload["mcpServers"]["existing"], unrelated)

    def test_bootstrap_refreshes_datadog_oauth_definition(self) -> None:
        output = self.home / ".copilot" / "mcp-config.json"
        output.parent.mkdir(parents=True)
        output.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "DataDog": {
                            "type": "http",
                            "url": "https://legacy.example/mcp",
                            "headers": {"Authorization": "Bearer fixture-token"},
                            "oauthClientId": "obsolete-client",
                            "tools": ["read"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        result = self.run_bootstrap()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["mcpServers"]["DataDog"],
            {
                "type": "http",
                "url": "https://mcp.datadoghq.com/v1/mcp",
                "headers": {},
                "source": "user",
                "tools": ["*"],
            },
        )

    def test_bootstrap_reports_non_file_output_without_traceback(self) -> None:
        output = self.home / ".copilot" / "mcp-config.json"
        output.mkdir(parents=True)

        result = self.run_bootstrap()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_bootstrap_rejects_alternate_output_paths(self) -> None:
        alternate = self.home / "alternate-mcp-config.json"

        result = self.run_bootstrap("--output", str(alternate))

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unrecognized arguments: --output", result.stderr)
        self.assertFalse(alternate.exists())
        self.assertFalse((self.home / ".copilot" / "mcp-config.json").exists())

    def prepare_complete_environment(self) -> dict[str, str]:
        source_root = Path(__file__).resolve().parent
        for client, relative_path in self.DISCOVERY_ROOTS.items():
            skills_dir = self.home / relative_path
            skills_dir.mkdir(parents=True)
            for name in self.EXPECTED_SKILLS:
                skill = skills_dir / name
                if name in self.REPOSITORY_SKILLS:
                    skill.symlink_to(source_root / ".copilot" / "skills" / name)
                elif client == "Pi":
                    skill.symlink_to(self.home / ".copilot" / "skills" / name)
                else:
                    skill.mkdir()
                    (skill / "SKILL.md").write_text(
                        f"---\nname: {name}\ndescription: Test skill\n---\n# {name}\n",
                        encoding="utf-8",
                    )

        remote_command = self.bin_dir / "copilot-codespace-session"
        remote_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        remote_command.chmod(0o755)
        remote_helper = self.home / ".local" / "bin" / "copilot-codespace-session"
        remote_helper.parent.mkdir(parents=True)
        remote_helper.symlink_to(remote_command)
        for relative_path in (".copilot/copilot-instructions.md", ".pi/agent/AGENTS.md"):
            instructions = self.home / relative_path
            instructions.parent.mkdir(parents=True, exist_ok=True)
            instructions.symlink_to(source_root / ".github" / "copilot-instructions.md")
        gh_guard = Path(__file__).resolve().parent / "bin" / "gh-guard"
        (remote_helper.parent / "gh").symlink_to(gh_guard)
        pr_marker = Path(__file__).resolve().parent / "bin" / "pr-marker"
        (remote_helper.parent / "pr-marker").symlink_to(pr_marker)
        hooks_source = Path(__file__).resolve().parent / "git-hooks"
        hooks_path = self.home / ".local" / "share" / "dotfiles-git-hooks"
        hooks_path.mkdir(parents=True)
        (hooks_path / "pre-push").symlink_to(hooks_source / "pre-push")
        (hooks_path / "pre-commit").symlink_to(hooks_source / "pre-commit")
        for hook_name in (
            "applypatch-msg",
            "pre-applypatch",
            "post-applypatch",
            "pre-merge-commit",
            "prepare-commit-msg",
            "commit-msg",
            "post-commit",
            "pre-rebase",
            "post-checkout",
            "post-merge",
            "post-rewrite",
            "post-index-change",
            "pre-auto-gc",
            "reference-transaction",
            "push-to-checkout",
            "sendemail-validate",
            "fsmonitor-watchman",
            "p4-changelist",
            "p4-prepare-changelist",
            "p4-post-changelist",
            "p4-pre-submit",
        ):
            (hooks_path / hook_name).symlink_to(
                hooks_source / "repository-hook-forwarder"
            )

        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)

        for command in (
            "1up",
            "az",
            "docker",
            "gh",
            "git",
            "npx",
            "tailscale",
            "tmux",
        ):
            path = self.bin_dir / command
            if command == "git":
                path.write_text(
                    "#!/bin/sh\n"
                    'if [ "$*" = "config --get core.hooksPath" ]; then\n'
                    f"  printf '%s\\n' {str(hooks_path)!r}\n"
                    "fi\n",
                    encoding="utf-8",
                )
            else:
                if command == "gh":
                    path.write_text(
                        "#!/bin/sh\n"
                        '[ "$*" = "stack --help" ] && exit 0\n'
                        "exit 0\n",
                        encoding="utf-8",
                    )
                else:
                    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        node = self.bin_dir / "node"
        node.write_text(
            "#!/bin/sh\n"
            '[ "$#" -eq 1 ] && [ "$1" = --version ] || exit 2\n'
            'printf \'%s\\n\' "$FAKE_NODE_VERSION"\n'
            'exit "$FAKE_NODE_EXIT"\n',
            encoding="utf-8",
        )
        node.chmod(0o755)
        copilot = self.bin_dir / "copilot"
        copilot.write_text(
            "#!/bin/sh\n"
            '[ "$*" = "plugin list --json" ] || exit 90\n'
            'printf \'%s\\n\' "$FAKE_PLUGINS_JSON"\n'
            'exit "${FAKE_PLUGIN_STATUS:-0}"\n',
            encoding="utf-8",
        )
        copilot.chmod(0o755)

        env = dict(os.environ)
        env.pop("PI_CODING_AGENT_DIR", None)
        env.update(
            {
                "HOME": str(self.home),
                "PATH": (
                    f"{self.home / '.local' / 'bin'}{os.pathsep}"
                    f"{self.bin_dir}{os.pathsep}{env['PATH']}"
                ),
                "CODESPACES": "true",
                "COPILOT_SKILL_CATALOG_REPO": "private/catalog",
                "COPILOT_MCP_SPLUNK_BEARER_TOKEN": "not-printed",
                "COPILOT_MCP_SPLUNK_HOST": "internal.example",
                "FAKE_NODE_VERSION": "v22.0.0",
                "FAKE_NODE_EXIT": "0",
                "FAKE_PLUGINS_JSON": json.dumps(
                    [{"name": "gho11y", "version": "1.0.0", "enabled": True}]
                ),
                "FAKE_PLUGIN_STATUS": "0",
            }
        )
        return env

    def test_verifier_reports_complete_environment(self) -> None:
        env = self.prepare_complete_environment()
        for version in ("v22.0.0", "v23.0.0", "v24.1.0"):
            with self.subTest(version=version):
                env["FAKE_NODE_VERSION"] = version

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("environment is ready", result.stdout)
                for client in self.DISCOVERY_ROOTS:
                    self.assertIn(
                        f"{client} installed skills: {len(self.EXPECTED_SKILLS)}",
                        result.stdout,
                    )
                self.assertNotIn("not-printed", result.stdout)
                self.assertNotIn("internal.example", result.stdout)

    def test_verifier_emits_oauth_guidance_for_approved_services(self) -> None:
        env = self.prepare_complete_environment()
        for missing_secrets in (False, True):
            with self.subTest(missing_secrets=missing_secrets):
                if missing_secrets:
                    del env["COPILOT_MCP_SPLUNK_BEARER_TOKEN"]
                    del env["COPILOT_MCP_SPLUNK_HOST"]

                result = subprocess.run(
                    [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                )

                self.assertEqual(result.returncode, int(missing_secrets), result.stdout + result.stderr)
                guidance = [line for line in result.stdout.splitlines() if line.startswith("OAuth status:")]
                self.assertEqual(guidance, ["OAuth status: verify DataDog, Sentry, and Slack with /mcp"])

    def test_verifier_rejects_inactive_personal_git_hooks(self) -> None:
        env = self.prepare_complete_environment()
        git = self.bin_dir / "git"
        git.write_text(
            "#!/bin/sh\n"
            'if [ "$*" = "config --get core.hooksPath" ]; then\n'
            "  printf '/wrong/hooks\\n'\n"
            "fi\n",
            encoding="utf-8",
        )
        git.chmod(0o755)

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("core.hooksPath must point to", result.stdout)
        self.assertNotIn("environment is ready", result.stdout)

    def test_verifier_rejects_inactive_gh_wrapper(self) -> None:
        env = self.prepare_complete_environment()
        installed_gh = self.home / ".local" / "bin" / "gh"
        installed_gh.unlink()
        installed_gh.symlink_to(self.bin_dir / "gh")

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gh must resolve to the personal wrapper", result.stdout)
        self.assertNotIn("environment is ready", result.stdout)

    def test_verifier_rejects_missing_pr_marker(self) -> None:
        env = self.prepare_complete_environment()
        (self.home / ".local" / "bin" / "pr-marker").unlink()

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pr-marker must resolve to the personal helper", result.stdout)

    def test_verifier_rejects_inactive_pr_marker(self) -> None:
        env = self.prepare_complete_environment()
        fake_bin = self.root / "earlier-bin"
        fake_bin.mkdir()
        fake_pr_marker = fake_bin / "pr-marker"
        fake_pr_marker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_pr_marker.chmod(0o755)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pr-marker must resolve to the personal helper", result.stdout)

    def test_verifier_rejects_missing_stack_extension(self) -> None:
        env = self.prepare_complete_environment()
        real_gh = self.bin_dir / "gh"
        real_gh.write_text(
            "#!/bin/sh\n"
            '[ "$*" = "stack --help" ] && exit 1\n'
            "exit 0\n",
            encoding="utf-8",
        )
        real_gh.chmod(0o755)

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing GitHub extension: github/gh-stack", result.stdout)

    def test_verifier_rejects_unsupported_node_versions(self) -> None:
        env = self.prepare_complete_environment()
        for version in ("v20.19.0", "v21.7.3"):
            with self.subTest(version=version):
                env["FAKE_NODE_VERSION"] = version

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(
                    f"Node.js 22 or later is required (found {version})",
                    result.stdout,
                )
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_verifier_reports_unverifiable_node_versions(self) -> None:
        env = self.prepare_complete_environment()
        for version, status in (("", 0), ("invalid", 0), ("v22.0.0", 1)):
            with self.subTest(version=version, status=status):
                env["FAKE_NODE_VERSION"] = version
                env["FAKE_NODE_EXIT"] = str(status)

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("cannot verify Node.js version", result.stdout)
                self.assertIn("install Node.js 22 or later", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_verifier_requires_an_executable_helper_at_the_remote_path(self) -> None:
        env = self.prepare_complete_environment()
        helper = self.home / ".local" / "bin" / "copilot-codespace-session"
        helper.unlink()
        for condition in ("missing", "nonexecutable", "directory", "broken symlink"):
            with self.subTest(condition=condition):
                if condition == "nonexecutable":
                    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    helper.chmod(0o644)
                elif condition == "directory":
                    helper.mkdir()
                elif condition == "broken symlink":
                    helper.symlink_to(self.root / "missing-helper")

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(
                    f"remote helper is missing or not executable: {helper}",
                    result.stdout,
                )
                self.assertNotIn("environment is ready", result.stdout)
                self.assertTrue((self.bin_dir / "copilot-codespace-session").is_file())
                if helper.is_dir():
                    helper.rmdir()
                else:
                    helper.unlink(missing_ok=True)

    def test_verifier_requires_instructions_from_the_dotfiles_checkout(self) -> None:
        env = self.prepare_complete_environment()
        for client, relative_path in (
            ("Copilot", ".copilot/copilot-instructions.md"),
            ("Pi", ".pi/agent/AGENTS.md"),
        ):
            with self.subTest(client=client):
                instructions = self.home / relative_path
                source = instructions.readlink()
                instructions.unlink()
                instructions.write_text("# stale instructions\n", encoding="utf-8")
                try:
                    result = subprocess.run(
                        [str(self.verifier)],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(
                        f"{client} instructions do not resolve to the dotfiles checkout",
                        result.stdout,
                    )
                    self.assertNotIn("environment is ready", result.stdout)
                finally:
                    instructions.unlink()
                    instructions.symlink_to(source)

    def test_verifier_requires_ui_screenshot_presentation(self) -> None:
        env = self.prepare_complete_environment()
        skill = self.home / ".copilot" / "skills" / "ui-screenshot-presentation"
        skill.unlink()
        for condition in ("missing", "empty directory"):
            with self.subTest(condition=condition):
                if condition == "empty directory":
                    skill.mkdir()

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(
                    "  - Copilot missing skills: ui-screenshot-presentation",
                    result.stdout.splitlines(),
                )
                self.assertNotIn("environment is ready", result.stdout)

    def test_verifier_rejects_pi_context_overrides_without_mutation(self) -> None:
        env = self.prepare_complete_environment()
        agent_dir = self.home / ".pi" / "agent"
        instructions = agent_dir / "AGENTS.md"
        original_link = instructions.readlink()
        override = agent_dir / "AGENTS.override.md"
        owned = self.root / "owned-override.md"
        owned.write_text("Keep my custom instructions.\n", encoding="utf-8")
        for kind in ("empty file", "stale file", "symlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    override.symlink_to(owned)
                else:
                    override.write_text("" if kind == "empty file" else "Old guidance.\n", encoding="utf-8")
                before = override.read_bytes()
                try:
                    result = subprocess.run(
                        [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(f"Pi instructions do not resolve to the dotfiles checkout: {override}", result.stdout)
                    self.assertNotIn("environment is ready", result.stdout)
                    self.assertEqual(override.read_bytes(), before)
                    self.assertEqual(instructions.readlink(), original_link)
                    if kind == "symlink":
                        self.assertEqual(override.readlink(), owned)
                finally:
                    override.unlink()
        self.assertEqual(owned.read_text(encoding="utf-8"), "Keep my custom instructions.\n")

    def test_verifier_uses_the_first_available_pi_context_file(self) -> None:
        env = self.prepare_complete_environment()
        agent_dir = self.home / ".pi" / "agent"
        primary = agent_dir / "AGENTS.md"
        source = primary.readlink()
        primary.unlink()
        for filename in ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"):
            with self.subTest(filename=filename):
                candidate = agent_dir / filename
                candidate.symlink_to(source)
                if filename == "AGENTS.override.md":
                    primary.write_text("Unused old guidance.\n", encoding="utf-8")
                try:
                    result = subprocess.run(
                        [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("environment is ready", result.stdout)
                    self.assertEqual(candidate.readlink(), source)
                    if filename == "AGENTS.override.md":
                        self.assertEqual(primary.read_text(encoding="utf-8"), "Unused old guidance.\n")
                finally:
                    candidate.unlink()
                    if filename == "AGENTS.override.md":
                        primary.unlink()

    def test_verifier_ignores_non_file_pi_overrides(self) -> None:
        env = self.prepare_complete_environment()
        override = self.home / ".pi" / "agent" / "AGENTS.override.md"
        for kind in ("directory", "broken symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    override.mkdir()
                else:
                    override.symlink_to(self.root / "missing-override")
                try:
                    result = subprocess.run(
                        [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("environment is ready", result.stdout)
                    if kind == "directory":
                        self.assertTrue(override.is_dir())
                    else:
                        self.assertEqual(override.readlink(), self.root / "missing-override")
                finally:
                    if override.is_symlink():
                        override.unlink()
                    else:
                        override.rmdir()

    def test_verifier_rejects_native_pi_skill_files_without_mutation(self) -> None:
        env = self.prepare_complete_environment()
        native = self.home / ".pi" / "agent" / "skills"
        for name in ("engineering-practices", "gh-axi"):
            for relative_path in (f"{name}/SKILL.md", "renamed/SKILL.md", "bundle/nested/SKILL.md", "other.md", "SKILL.md"):
                with self.subTest(skill=name, path=relative_path):
                    skill = native / relative_path
                    skill.parent.mkdir(parents=True)
                    content = f"---\nname: {name}\ndescription: Older native skill\n---\n# Old guidance\n".encode()
                    skill.write_bytes(content)
                    try:
                        result = subprocess.run(
                            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                        )

                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn(f"Pi native skill root must be empty to verify managed guidance: {native}", result.stdout)
                        self.assertNotIn("environment is ready", result.stdout)
                        self.assertEqual(skill.read_bytes(), content)
                        self.assertFalse(native.is_symlink())
                    finally:
                        shutil.rmtree(native)

    def test_verifier_rejects_native_pi_skill_symlinks_without_mutation(self) -> None:
        env = self.prepare_complete_environment()
        native = self.home / ".pi" / "agent" / "skills"
        owned = self.root / "owned-native-skill"
        owned.mkdir()
        content = b"---\nname: gh-axi\ndescription: Older native skill\n---\n# Old guidance\n"
        (owned / "SKILL.md").write_bytes(content)
        for kind in ("root", "directory", "file", "shared root"):
            with self.subTest(kind=kind):
                if kind in ("root", "shared root"):
                    link = native
                    target = owned if kind == "root" else self.home / ".agents" / "skills"
                else:
                    native.mkdir()
                    link = native / ("other.md" if kind == "file" else "renamed")
                    target = owned / "SKILL.md" if kind == "file" else owned
                link.symlink_to(target)
                try:
                    result = subprocess.run(
                        [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(f"Pi native skill root must be empty to verify managed guidance: {native}", result.stdout)
                    self.assertNotIn("environment is ready", result.stdout)
                    self.assertEqual(link.readlink(), target)
                    self.assertEqual((owned / "SKILL.md").read_bytes(), content)
                finally:
                    link.unlink()
                    if kind not in ("root", "shared root"):
                        native.rmdir()

    def test_verifier_accepts_an_empty_native_pi_skill_root(self) -> None:
        env = self.prepare_complete_environment()
        native = self.home / ".pi" / "agent" / "skills"
        native.mkdir()

        result = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("environment is ready", result.stdout)
        self.assertEqual(list(native.iterdir()), [])

    def test_verifier_checks_the_configured_pi_agent_directory(self) -> None:
        env = self.prepare_complete_environment()
        default = self.home / ".pi" / "agent"
        source = (default / "AGENTS.md").readlink()
        (default / "AGENTS.override.md").write_text("Unused default override.\n", encoding="utf-8")
        (default / "skills").mkdir()
        (default / "skills" / "SKILL.md").write_text("Unused default skill.\n", encoding="utf-8")
        active = self.home / "custom-agent"
        active.mkdir()
        env["PI_CODING_AGENT_DIR"] = "~/custom-agent"

        missing = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn(f"Pi instructions do not resolve to the dotfiles checkout: {active / 'AGENTS.md'}", missing.stdout)

        (active / "AGENTS.md").symlink_to(source)
        for setting in (str(active), "~/custom-agent", "custom-agent"):
            with self.subTest(setting=setting):
                env["PI_CODING_AGENT_DIR"] = setting
                complete = subprocess.run(
                    [str(self.verifier)], cwd=self.home, env=env, capture_output=True, text=True, check=False,
                )

                self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)
        env["PI_CODING_AGENT_DIR"] = "~/custom-agent"
        for relative_path in ("AGENTS.override.md", "skills/gh-axi/SKILL.md"):
            with self.subTest(path=relative_path):
                shadow = active / relative_path
                shadow.parent.mkdir(parents=True, exist_ok=True)
                content = b"---\nname: gh-axi\ndescription: Stale active guidance\n---\n# Old guidance\n"
                shadow.write_bytes(content)
                try:
                    result = subprocess.run(
                        [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn("environment is ready", result.stdout)
                    self.assertIn(str(active), result.stdout)
                    self.assertEqual(shadow.read_bytes(), content)
                finally:
                    shadow.unlink()

    def test_verifier_requires_every_globally_routed_skill(self) -> None:
        env = self.prepare_complete_environment()
        manifest_path = (
            Path(__file__).with_name("tests")
            / "fixtures"
            / "instruction-migration"
            / "manifest.json"
        )
        routed_skills = json.loads(manifest_path.read_text(encoding="utf-8"))["routed_skills"]
        for client, relative_path in self.DISCOVERY_ROOTS.items():
            for name in routed_skills:
                with self.subTest(client=client, skill=name):
                    skill = self.home / relative_path / name
                    saved = self.root / "removed-skill"
                    skill.rename(saved)
                    try:
                        result = subprocess.run(
                            [str(self.verifier)],
                            env=env,
                            capture_output=True,
                            text=True,
                            check=False,
                        )

                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn(f"{client} missing skills: {name}", result.stdout)
                        self.assertNotIn("environment is ready", result.stdout)
                    finally:
                        saved.rename(skill)

        restored = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)

    def test_verifier_rejects_missing_discovery_roots(self) -> None:
        env = self.prepare_complete_environment()
        for client, relative_path in self.DISCOVERY_ROOTS.items():
            with self.subTest(client=client):
                skills = self.home / relative_path
                saved = self.root / "removed-skills"
                skills.rename(saved)
                try:
                    result = subprocess.run(
                        [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(
                        f"{client} missing skills: {', '.join(sorted(self.EXPECTED_SKILLS))}",
                        result.stdout,
                    )
                    self.assertNotIn("environment is ready", result.stdout)
                finally:
                    saved.rename(skills)

    def test_verifier_rejects_skills_without_skill_files(self) -> None:
        env = self.prepare_complete_environment()
        for client, relative_path in self.DISCOVERY_ROOTS.items():
            skill = self.home / relative_path / "gh-axi"
            for condition in ("empty directory", "broken symlink", "directory artifact"):
                with self.subTest(client=client, condition=condition):
                    saved = self.root / "removed-skill"
                    skill.rename(saved)
                    if condition == "broken symlink":
                        skill.symlink_to(self.root / "missing-skill")
                    else:
                        skill.mkdir()
                        if condition == "directory artifact":
                            (skill / "SKILL.md").mkdir()
                    try:
                        result = subprocess.run(
                            [str(self.verifier)],
                            env=env,
                            capture_output=True,
                            text=True,
                            check=False,
                        )

                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn(f"{client} missing skills: gh-axi", result.stdout)
                        self.assertNotIn("environment is ready", result.stdout)
                    finally:
                        if skill.is_symlink():
                            skill.unlink()
                        else:
                            if (skill / "SKILL.md").is_dir():
                                (skill / "SKILL.md").rmdir()
                            skill.rmdir()
                        saved.rename(skill)

    def test_verifier_requires_managed_skills_from_their_sources(self) -> None:
        env = self.prepare_complete_environment()
        targets = [
            (client, relative_path, name, "the dotfiles checkout")
            for client, relative_path in self.DISCOVERY_ROOTS.items()
            for name in self.REPOSITORY_SKILLS
        ] + [
            ("Pi", self.DISCOVERY_ROOTS["Pi"], name, "the Copilot source")
            for name in self.CATALOG_SKILLS
        ]
        for client, relative_path, name, owner in targets:
            for kind in ("directory copy", "unrelated symlink"):
                with self.subTest(client=client, skill=name, kind=kind):
                    skill = self.home / relative_path / name
                    saved = self.root / "removed-skill"
                    skill.rename(saved)
                    replacement = skill if kind == "directory copy" else self.root / "stale-skill"
                    replacement.mkdir()
                    (replacement / "SKILL.md").write_text(
                        f"---\nname: {name}\ndescription: Stale copy\n---\n# Old guidance\n",
                        encoding="utf-8",
                    )
                    original = (replacement / "SKILL.md").read_bytes()
                    if kind == "unrelated symlink":
                        skill.symlink_to(replacement)
                    try:
                        result = subprocess.run(
                            [str(self.verifier)],
                            env=env,
                            capture_output=True,
                            text=True,
                            check=False,
                        )

                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn(f"{client} {name} does not resolve to {owner}", result.stdout)
                        self.assertNotIn("environment is ready", result.stdout)
                        self.assertEqual((replacement / "SKILL.md").read_bytes(), original)
                        if kind == "unrelated symlink":
                            self.assertEqual(skill.readlink(), replacement)
                        else:
                            self.assertFalse(skill.is_symlink())
                    finally:
                        if skill.is_symlink():
                            skill.unlink()
                        (replacement / "SKILL.md").unlink()
                        replacement.rmdir()
                        saved.rename(skill)

        restored = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)

    def test_verifier_accepts_catalog_sources_installed_through_symlinks(self) -> None:
        env = self.prepare_complete_environment()
        skill = self.home / ".copilot" / "skills" / "gh-axi"
        source = self.root / "catalog-source"
        skill.rename(source)
        skill.symlink_to(source)

        result = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("environment is ready", result.stdout)

    def test_verifier_accepts_a_shared_skill_root(self) -> None:
        env = self.prepare_complete_environment()
        pi_skills = self.home / ".agents" / "skills"
        pi_skills.rename(self.root / "pi-skill-links")
        pi_skills.symlink_to(self.home / ".copilot" / "skills")

        result = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("environment is ready", result.stdout)

    def test_verifier_requires_the_exact_enabled_plugin(self) -> None:
        env = self.prepare_complete_environment()
        for plugins in (
            [],
            [{"name": "gho11y", "enabled": False}],
            [{"name": "gho11y", "enabled": "true"}],
            [{"name": "gho11y", "enabled": 1}],
            [{"name": "gho11y"}],
            [{"name": "other-gho11y", "enabled": True}],
        ):
            with self.subTest(plugins=plugins):
                env["FAKE_PLUGINS_JSON"] = json.dumps(plugins)

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("missing or disabled Copilot plugin: gho11y", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)

    def test_verifier_reports_plugin_listing_failures_without_tracebacks(self) -> None:
        env = self.prepare_complete_environment()
        for payload, status in (
            ("not JSON", 0),
            ("{}", 0),
            (env["FAKE_PLUGINS_JSON"], 1),
        ):
            with self.subTest(payload=payload, status=status):
                env["FAKE_PLUGINS_JSON"] = payload
                env["FAKE_PLUGIN_STATUS"] = str(status)

                result = subprocess.run(
                    [str(self.verifier)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("cannot list Copilot plugins", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def verify_definition(
        self,
        env: dict[str, str],
        name: str,
        definition: object,
    ) -> subprocess.CompletedProcess[str]:
        config = self.home / ".copilot" / "mcp-config.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["mcpServers"][name] = definition
        config.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_verifier_rejects_malformed_local_mcp_definitions(self) -> None:
        env = self.prepare_complete_environment()
        for definition in (
            None,
            [],
            "not an object",
            {},
            {"type": "local"},
            {"type": "local", "command": "   "},
            {"type": "local", "command": 123},
            {"type": "local", "command": "node" + chr(0)},
            {"type": "unknown", "command": "node"},
            {"type": "local", "command": "node", "args": "--read-only"},
            {"type": "local", "command": "node", "args": [1]},
            {"type": "local", "command": "node", "args": [chr(0)]},
            {"type": "local", "command": "node", "env": []},
            {"type": "local", "command": "node", "env": {"TOKEN": None}},
            {"type": "local", "command": "node", "env": {"": "fixture-secret"}},
            {"type": "local", "command": "node", "tools": "*"},
        ):
            with self.subTest(definition=definition):
                result = self.verify_definition(env, "Kusto", definition)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("invalid MCP server Kusto:", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_verifier_rejects_malformed_remote_mcp_definitions(self) -> None:
        env = self.prepare_complete_environment()
        for definition in (
            {"type": "http"},
            {"type": "http", "url": []},
            {"type": "http", "url": ""},
            {"type": "http", "url": "https://"},
            {"type": "http", "url": "ftp://example.com/mcp"},
            {"type": "http", "url": "https://example.com:invalid/mcp"},
            {"type": "http", "url": "https://example.com:99999/mcp"},
            {"type": "http", "url": "https://[invalid]/mcp"},
            {"type": "http", "url": "https://example.com/" + chr(0)},
            {"type": "http", "url": "https://example.com/with space"},
            {"type": "http", "url": "https://example.com/mcp", "headers": []},
            {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": 42}},
            {"type": "sse", "url": None},
        ):
            with self.subTest(definition=definition):
                result = self.verify_definition(env, "Sentry", definition)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("invalid MCP server Sentry:", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_verifier_rejects_custom_definitions_under_every_managed_name(self) -> None:
        env = self.prepare_complete_environment()
        config = self.home / ".copilot" / "mcp-config.json"
        original = config.read_text(encoding="utf-8")
        for name in json.loads(original)["mcpServers"]:
            with self.subTest(name=name):
                config.write_text(original, encoding="utf-8")

                result = self.verify_definition(env, name, {"type": "local", "command": "node"})

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"invalid MCP server {name}: does not match the managed definition", result.stdout)
                self.assertIn("run bootstrap-copilot-mcp", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(
                    json.loads(config.read_text(encoding="utf-8"))["mcpServers"][name],
                    {"type": "local", "command": "node"},
                )

        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertIn("environment is ready", restored.stdout)

    def test_verifier_rejects_changed_managed_fields(self) -> None:
        env = self.prepare_complete_environment()
        config = self.home / ".copilot" / "mcp-config.json"
        original = config.read_text(encoding="utf-8")
        definitions = json.loads(original)["mcpServers"]
        for name, field, value in (
            ("Kusto", "command", "node"),
            ("Kusto", "args", ["-y", "different-mcp", "server", "start"]),
            ("Kusto", "tools", ["query"]),
            ("Kusto", "env", {"TOKEN": "fixture-secret"}),
            ("Kusto", "disabled", True),
            ("DataDog", "url", "https://custom.example/mcp"),
            ("Slack", "oauthPublicClient", 1),
            ("Slack", "oauthClientId", "custom-client"),
            ("Sentry", "headers", {"Authorization": "Bearer fixture-secret"}),
        ):
            with self.subTest(name=name, field=field):
                config.write_text(original, encoding="utf-8")
                modified = {**definitions[name], field: value}

                result = self.verify_definition(env, name, modified)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"invalid MCP server {name}: does not match the managed definition", result.stdout)
                self.assertNotIn("environment is ready", result.stdout)
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_verifier_requires_all_managed_names(self) -> None:
        env = self.prepare_complete_environment()
        config = self.home / ".copilot" / "mcp-config.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        del payload["mcpServers"]["Kusto"]
        config.write_text(json.dumps(payload), encoding="utf-8")

        result = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing MCP servers: Kusto", result.stdout)
        self.assertNotIn("environment is ready", result.stdout)

    def test_verifier_accepts_reordered_managed_entries_and_leaves_custom_names_untouched(self) -> None:
        env = self.prepare_complete_environment()
        config = self.home / ".copilot" / "mcp-config.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["mcpServers"] = {
            name: dict(reversed(list(definition.items())))
            for name, definition in payload["mcpServers"].items()
        }
        custom = self.bin_dir / "custom-kusto"
        custom.write_text(
            "#!/bin/sh\n"
            'printf \'executed\\n\' > "$HOME/custom-command-executed"\n',
            encoding="utf-8",
        )
        custom.chmod(0o755)
        payload["mcpServers"]["KustoCustom"] = {
            "type": "local", "command": str(custom), "args": ["--read-only"],
            "tools": ["custom_query"], "env": {"TOKEN": "fixture-secret"},
        }
        config.write_text(json.dumps(payload), encoding="utf-8")
        before = config.read_bytes()

        result = subprocess.run(
            [str(self.verifier)], env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("environment is ready", result.stdout)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((self.home / "custom-command-executed").exists())
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_installed_verifier_symlink_uses_the_bootstrap_definitions(self) -> None:
        env = self.prepare_complete_environment()
        link = self.home / ".local" / "bin" / "verify-codespace-copilot-env"
        link.symlink_to(self.verifier)
        config = self.home / ".copilot" / "mcp-config.json"
        before = config.read_bytes()

        result = subprocess.run(
            [str(link)], cwd=self.home, env=env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("environment is ready", result.stdout)
        self.assertEqual(config.read_bytes(), before)

    def test_verifier_reports_missing_copilot_without_traceback(self) -> None:
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "CODESPACES": "true",
        }

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing tools:", result.stdout)
        self.assertIn("copilot", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_verifier_reports_non_object_mcp_config(self) -> None:
        config = self.home / ".copilot" / "mcp-config.json"
        config.parent.mkdir(parents=True)
        config.write_text("[]\n", encoding="utf-8")
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "CODESPACES": "true",
        }

        result = subprocess.run(
            [str(self.verifier)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read MCP configuration", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
