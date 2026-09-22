#!/usr/bin/env python3
"""Regression tests for the durable Codespace Copilot launcher."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_copilot_codespace_session import write_isolated_helper


class Copilot2Test(unittest.TestCase):
    """Exercise copilot2 with fake GitHub CLI and SSH commands."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.bin_dir = self.root / "bin"
        self.home.mkdir()
        self.bin_dir.mkdir()
        self.wrapper = Path(__file__).with_name("bin") / "copilot2"

        self.gh = self.bin_dir / "gh"
        self.gh.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$HOME/gh.log"\n'
            'case "$*" in\n'
            "  'auth status --hostname github.com') exit 0 ;;\n"
            "  'codespace list --limit 100 --json name,displayName,state')\n"
            "    printf '%s\\n' \"${FAKE_CODESPACES_JSON}\"\n"
            "    ;;\n"
            "  'codespace list --limit 100 --json name,displayName,state --repo example/project')\n"
            "    printf '%s\\n' \"${FAKE_FILTERED_CODESPACES_JSON}\"\n"
            "    ;;\n"
            "  codespace\\ view\\ --codespace*)\n"
            '    if [ -n "${FAKE_VIEW_SLEEP_SECONDS:-}" ]; then\n'
            '      sleep "$FAKE_VIEW_SLEEP_SECONDS"\n'
            "    fi\n"
            '    if [ -n "${FAKE_VIEW_ERROR:-}" ]; then\n'
            "      printf '%s\\n' \"$FAKE_VIEW_ERROR\" >&2\n"
            "      exit 1\n"
            "    fi\n"
            '    view_count=0\n'
            '    [ -f "$HOME/view-count" ] && view_count=$(cat "$HOME/view-count")\n'
            '    view_count=$((view_count + 1))\n'
            '    printf \'%s\\n\' "$view_count" > "$HOME/view-count"\n'
            '    state="${FAKE_CODESPACE_STATE:-Available}"\n'
            '    if [ -n "${FAKE_CODESPACE_STATES:-}" ]; then\n'
            '      state=$(printf \'%s\\n\' "$FAKE_CODESPACE_STATES" | awk -F, -v n="$view_count" \'{print ($n == "" ? $NF : $n)}\')\n'
            "    fi\n"
            '    printf \'{"name":"%s","state":"%s","repository":"%s"}\\n\' '
            '"${FAKE_CODESPACE_NAME:-$4}" "$state" "${FAKE_CODESPACE_REPOSITORY:-example/project}"\n'
            "    ;;\n"
            "  codespace\\ ssh\\ --codespace*'--config')\n"
            "    printf 'Host fake-codespace\\n  ProxyCommand false\\n'\n"
            "    ;;\n"
            "  *) printf 'unexpected gh args: %s\\n' \"$*\" >&2; exit 90 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        self.gh.chmod(0o755)

        self.ssh = self.bin_dir / "ssh"
        self.ssh.write_text(
            r"""#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
home = Path(os.environ["HOME"])
with (home / "ssh.log").open("a", encoding="utf-8") as log:
    log.write(" ".join(arguments) + "\n")
count_file = home / "ssh-count"
count = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(count), encoding="utf-8")
action = arguments[-1].split()[-1]
results = os.environ.get("FAKE_SSH_RESULTS", "0").split(",")
result = int(results[min(count - 1, len(results) - 1)])
if os.environ.get("FAKE_SSH_FAIL_ATTACH") == "1":
    result = 255 if action == "attach" else 0
connected_calls = os.environ.get("FAKE_SSH_CONNECTED_CALLS", "").split(",")
connected = result == 0 or str(count) in connected_calls
if connected:
    options = {}
    for index, argument in enumerate(arguments[:-1]):
        if argument == "-o":
            key, _, value = arguments[index + 1].partition("=")
            options[key.lower()] = value
    reader = os.environ.get("FAKE_SSH_CONFIG_READER")
    if reader:
        config = subprocess.run([reader, "-G", *arguments], capture_output=True, text=True, check=True)
        options = dict(line.split(" ", 1) for line in config.stdout.splitlines() if " " in line)
    local_command = options.get("localcommand")
    if options.get("permitlocalcommand") == "yes" and local_command:
        if "%" in local_command.replace("%%", ""):
            sys.exit(90)
        subprocess.run(["/bin/sh", "-c", local_command.replace("%%", "%")], check=True)
        with (home / "connected-calls").open("a", encoding="utf-8") as log:
            log.write(str(count) + "\n")
    if action == "attach":
        time.sleep(float(os.environ.get("FAKE_SSH_CONNECTED_SLEEP_SECONDS", "0")))
if str(count) == os.environ.get("FAKE_SSH_SLEEP_CALL"):
    time.sleep(float(os.environ.get("FAKE_SSH_SLEEP_SECONDS", "0")))
sys.exit(result)
""",
            encoding="utf-8",
        )
        self.ssh.chmod(0o755)

        self.env = dict(os.environ)
        self.env.pop("COPILOT2_REPOSITORY", None)
        self.env.pop("COPILOT2_DISPLAY_NAME", None)
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin_dir}{os.pathsep}{self.env['PATH']}",
                "COPILOT2_RETRY_DELAYS": "0",
                "COPILOT2_MAX_RECONNECT_SECONDS": "10",
                "FAKE_SSH_FAIL_ATTACH": "0",
                "FAKE_SSH_CONNECTED_CALLS": "",
                "FAKE_SSH_CONNECTED_SLEEP_SECONDS": "0",
                "FAKE_CODESPACES_JSON": json.dumps(
                    [
                        {
                            "name": "generated-name",
                            "displayName": "gummyworm",
                            "state": "Available",
                        }
                    ]
                ),
            }
        )

    def run_wrapper(
        self, *arguments: str, timeout: float = 15
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.wrapper), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def test_connects_with_keepalives_and_remote_helper(self) -> None:
        result = self.run_wrapper("review-one")

        self.assertEqual(result.returncode, 0, result.stderr)
        ssh_args = (self.home / "ssh.log").read_text(encoding="utf-8")
        self.assertIn("ConnectTimeout=", ssh_args)
        self.assertIn("ConnectionAttempts=1", ssh_args)
        self.assertIn("ServerAliveInterval=15", ssh_args)
        self.assertIn("ServerAliveCountMax=3", ssh_args)
        self.assertIn("copilot-codespace-session", ssh_args)
        self.assertIn("review-one", ssh_args)
        self.assertIn("resume", ssh_args)
        self.assertIn("prepare", ssh_args)
        self.assertIn("attach", ssh_args)

    def test_resolves_clients_from_path(self) -> None:
        self.env["COPILOT2_GH"] = str(self.root / "unused-gh")
        self.env["COPILOT2_SSH"] = str(self.root / "unused-ssh")

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "gh.log").is_file())
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "2")

    def test_retries_transport_failure_while_codespace_is_available(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,0"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.home / "ssh-count").read_text(encoding="utf-8").strip(),
            "4",
        )
        self.assertIn("retrying in 0 seconds", result.stderr)

    def test_reconnect_budget_starts_when_a_long_session_drops(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,0"
        self.env["FAKE_SSH_SLEEP_CALL"] = "2"
        self.env["FAKE_SSH_SLEEP_SECONDS"] = "2"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "2"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.home / "ssh-count").read_text(encoding="utf-8").strip(),
            "4",
        )

    def test_repeated_live_connections_do_not_consume_the_outage_budget(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,255,0,255,0,0"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "2,4,6"
        self.env["FAKE_SSH_CONNECTED_SLEEP_SECONDS"] = "0.6"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "8")
        self.assertEqual((self.home / "view-count").read_text().strip(), "3")
        self.assertNotIn("reconnect deadline reached", result.stderr)

    def test_stop_after_repeated_live_connections_reports_process_loss(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,255,0,255"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "2,4,6"
        self.env["FAKE_SSH_CONNECTED_SLEEP_SECONDS"] = "0.6"
        self.env["FAKE_CODESPACE_STATES"] = "Available,Available,Shutdown"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("is Shutdown", result.stderr)
        self.assertIn("did not survive", result.stderr)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "6")
        self.assertEqual((self.home / "view-count").read_text().strip(), "3")
        self.assertNotIn("reconnect deadline reached", result.stderr)

    def test_connection_confirmation_does_not_reset_later_failed_attempts(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,255"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "2"
        self.env["FAKE_SSH_CONNECTED_SLEEP_SECONDS"] = "0.2"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        started = time.monotonic()
        result = self.run_wrapper(timeout=5)

        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("reconnect deadline reached", result.stderr)
        self.assertEqual((self.home / "connected-calls").read_text().splitlines(), ["2"])

    def test_connection_confirmation_uses_valid_openssh_configuration(self) -> None:
        ssh = shutil.which("ssh")
        if ssh is None:
            self.skipTest("OpenSSH is required for the configuration regression")
        scratch = self.root / "ssh scratch's %q"
        scratch.mkdir()
        self.env["TMPDIR"] = str(scratch)
        self.env["FAKE_SSH_CONFIG_READER"] = ssh
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,0"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "2"
        self.env["FAKE_SSH_SLEEP_CALL"] = "2"
        self.env["FAKE_SSH_SLEEP_SECONDS"] = "1.2"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "4")
        self.assertEqual((self.home / "connected-calls").read_text().splitlines(), ["2", "4"])

    def test_repeated_transport_failures_reach_the_deadline(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "255"
        self.env["FAKE_SSH_SLEEP_CALL"] = "1"
        self.env["FAKE_SSH_SLEEP_SECONDS"] = "2"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 124)
        self.assertIn("reconnect deadline reached", result.stderr)

    def test_repeated_attach_failures_reach_the_deadline(self) -> None:
        self.env["FAKE_SSH_FAIL_ATTACH"] = "1"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        started = time.monotonic()
        result = self.run_wrapper(timeout=5)
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124)
        self.assertIn("reconnect deadline reached", result.stderr)
        self.assertLess(elapsed, 3)
        calls = (self.home / "ssh.log").read_text(encoding="utf-8").splitlines()
        actions = [call.split()[-1] for call in calls]
        self.assertGreaterEqual(actions.count("attach"), 2)
        self.assertEqual(
            actions,
            ["prepare" if index % 2 == 0 else "attach" for index in range(len(actions))],
        )

    def test_retry_sleep_does_not_exceed_remaining_budget(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "255"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"
        self.env["COPILOT2_RETRY_DELAYS"] = "2"

        started = time.monotonic()
        result = self.run_wrapper()
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124)
        self.assertLess(elapsed, 1.75)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "1")

    def test_initial_prepare_cannot_block_past_deadline(self) -> None:
        self.env["FAKE_SSH_SLEEP_CALL"] = "1"
        self.env["FAKE_SSH_SLEEP_SECONDS"] = "5"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        started = time.monotonic()
        result = self.run_wrapper()
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("reconnect deadline reached", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertLess(elapsed, 2.5)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "1")
        self.assertNotIn(" attach", (self.home / "ssh.log").read_text())

    def test_retry_prepare_uses_only_the_remaining_budget(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,0"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "2"
        self.env["FAKE_SSH_SLEEP_CALL"] = "3"
        self.env["FAKE_SSH_SLEEP_SECONDS"] = "5"
        self.env["FAKE_VIEW_SLEEP_SECONDS"] = "0.5"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "2"
        self.env["COPILOT2_RETRY_DELAYS"] = "1"

        started = time.monotonic()
        result = self.run_wrapper()
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("reconnect deadline reached", result.stderr)
        self.assertLess(elapsed, 3)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "3")

    def test_state_lookup_cannot_block_past_deadline(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "255"
        self.env["FAKE_VIEW_SLEEP_SECONDS"] = "5"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "1"

        started = time.monotonic()
        result = self.run_wrapper()
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("reconnect deadline reached", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertLess(elapsed, 2.5)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "1")

    def test_state_lookup_time_reduces_retry_sleep(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "255"
        self.env["FAKE_VIEW_SLEEP_SECONDS"] = "1"
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "2"
        self.env["COPILOT2_RETRY_DELAYS"] = "2"

        started = time.monotonic()
        result = self.run_wrapper()
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(elapsed, 3)
        self.assertEqual((self.home / "ssh-count").read_text().strip(), "1")

    def test_stops_when_codespace_is_not_available(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255"
        self.env["FAKE_CODESPACE_STATE"] = "Shutdown"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 12)
        self.assertIn("did not survive", result.stderr)
        self.assertIn("copilot2 --new", result.stderr)
        self.assertEqual(
            (self.home / "ssh-count").read_text(encoding="utf-8").strip(),
            "2",
        )

    def test_rejects_duplicate_display_names(self) -> None:
        self.env["FAKE_CODESPACES_JSON"] = json.dumps(
            [
                {
                    "name": "one",
                    "displayName": "gummyworm",
                    "state": "Available",
                },
                {
                    "name": "two",
                    "displayName": "gummyworm",
                    "state": "Available",
                },
            ]
        )

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 2)
        self.assertIn("multiple Codespaces", result.stderr)
        self.assertFalse((self.home / "ssh.log").exists())

    def test_exact_codespace_override_skips_list(self) -> None:
        self.env["FAKE_CODESPACE_REPOSITORY"] = "example/another-project"

        result = self.run_wrapper("--codespace", "exact-name")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "gh.log").read_text(encoding="utf-8")
        self.assertNotIn("codespace list", calls)
        self.assertIn("codespace view --codespace exact-name", calls)
        self.assertIn("codespace ssh --codespace exact-name --config", calls)

    def test_repository_filter_is_optional_and_supports_cli_and_environment(self) -> None:
        self.env["FAKE_CODESPACES_JSON"] = json.dumps(
            [
                {"name": "matching-project", "displayName": "gummyworm", "state": "Available"},
                {"name": "other-project", "displayName": "gummyworm", "state": "Available"},
            ]
        )
        self.env["FAKE_FILTERED_CODESPACES_JSON"] = json.dumps(
            [{"name": "matching-project", "displayName": "gummyworm", "state": "Available"}]
        )
        for arguments in (("--repo", "example/project"), ()):
            with self.subTest(arguments=arguments):
                if not arguments:
                    self.env["COPILOT2_REPOSITORY"] = "example/project"
                (self.home / "gh.log").unlink(missing_ok=True)

                result = self.run_wrapper(*arguments)

                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (self.home / "gh.log").read_text(encoding="utf-8")
                self.assertIn("--repo example/project", calls)
                self.assertIn("codespace ssh --codespace matching-project --config", calls)
                self.assertNotIn("codespace ssh --codespace other-project", calls)

    def test_exact_codespace_rejects_an_explicit_repository_mismatch(self) -> None:
        self.env["FAKE_CODESPACE_REPOSITORY"] = "example/another-project"

        result = self.run_wrapper("--codespace", "exact-name", "--repo", "example/project")

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("belongs to example/another-project, not example/project", result.stderr)
        self.assertFalse((self.home / "ssh.log").exists())

    def test_rejects_unsafe_session_name(self) -> None:
        for session in ("bad session", "review.with.period", ".review", "review.", "."):
            with self.subTest(session=session):
                result = self.run_wrapper(session)

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(
                    "session names can contain only letters, numbers, underscores, and hyphens",
                    result.stderr,
                )
                self.assertFalse((self.home / "gh.log").exists())
                self.assertFalse((self.home / "ssh.log").exists())

    def test_creation_permission_is_consumed_after_confirmed_prepare(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255,0,0"
        for arguments, initial_mode in (((), "start"), (("--new",), "new")):
            with self.subTest(arguments=arguments):
                (self.home / "ssh.log").unlink(missing_ok=True)
                (self.home / "ssh-count").unlink(missing_ok=True)

                result = self.run_wrapper(*arguments)

                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (self.home / "ssh.log").read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    [call.split()[-3] for call in calls], [initial_mode, "resume", "resume", "resume"]
                )
                self.assertEqual([call.split()[-1] for call in calls], ["prepare", "attach", "prepare", "attach"])
                self.assertEqual(len({call.split()[-2] for call in calls}), 1)

    def test_creation_permission_is_retained_until_prepare_succeeds(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "255,0,255,0,0"
        self.env["FAKE_SSH_CONNECTED_CALLS"] = "1"
        for arguments, initial_mode in (((), "start"), (("--new",), "new")):
            with self.subTest(arguments=arguments):
                (self.home / "ssh.log").unlink(missing_ok=True)
                (self.home / "ssh-count").unlink(missing_ok=True)

                result = self.run_wrapper(*arguments)

                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (self.home / "ssh.log").read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    [call.split()[-3] for call in calls], [initial_mode, initial_mode, "resume", "resume", "resume"]
                )
                self.assertEqual(
                    [call.split()[-1] for call in calls], ["prepare", "prepare", "attach", "prepare", "attach"]
                )
                self.assertEqual(len({call.split()[-2] for call in calls}), 1)

    def use_real_remote(self) -> Path:
        real_tmux = shutil.which("tmux")
        if real_tmux is None:
            self.skipTest("tmux is required for the shared-continuity regression")
        socket_dir = tempfile.TemporaryDirectory(prefix=".t-", dir=Path(__file__).parent)
        self.addCleanup(socket_dir.cleanup)
        socket = Path(socket_dir.name) / "s"
        tmux = self.bin_dir / "tmux"
        tmux.write_text(
            f"#!/bin/sh\nexec {shlex.quote(real_tmux)} -S {shlex.quote(str(socket))} -f /dev/null \"$@\"\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        copilot = self.bin_dir / "copilot"
        copilot.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$$" >> "$HOME/copilot-starts"\n'
            "exec sleep 60\n",
            encoding="utf-8",
        )
        copilot.chmod(0o755)
        self.workspaces = self.root / "workspaces"
        workspace = self.workspaces / "project"
        workspace.mkdir(parents=True)
        helper = self.home / ".local" / "bin" / "copilot-codespace-session"
        helper.parent.mkdir(parents=True)
        write_isolated_helper(helper, self.workspaces)
        self.env.update(
            {
                "CODESPACES": "true",
                "COPILOT2_REMOTE_CWD": str(workspace),
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "SHELL": "/bin/sh",
            }
        )
        self.env.pop("TMUX", None)
        self.addCleanup(
            subprocess.run,
            [str(tmux), "kill-server"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return tmux

    def test_another_launcher_cannot_rearm_replacement_after_confirmation(self) -> None:
        tmux = self.use_real_remote()
        self.ssh.write_text(
            r"""#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

home = Path(os.environ["HOME"])
remote_command = sys.argv[-1]
session, mode, invocation, action = shlex.split(remote_command)[-4:]
if action == "prepare":
    result = subprocess.run(["/bin/sh", "-c", remote_command], check=False)
    sys.exit(result.returncode)
if action == "attach":
    dropped = home / "transport-dropped"
    if not dropped.exists():
        deadline = time.monotonic() + 5
        while not (home / "copilot-starts").exists():
            if time.monotonic() >= deadline:
                sys.exit(91)
            time.sleep(0.02)
        helper = home / ".local" / "bin" / "copilot-codespace-session"
        subprocess.run([str(helper), session, "start", "other-launcher", "prepare"], check=True)
        subprocess.run(["tmux", "new-session", "-d", "-s", "test-anchor", "exec sleep 60"], check=True)
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"], check=True)
        dropped.touch()
        sys.exit(255)
    sys.exit(0)
sys.exit(90)
""",
            encoding="utf-8",
        )

        result = self.run_wrapper("--new")

        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("did not survive", result.stderr)
        self.assertEqual(len((self.home / "copilot-starts").read_text().splitlines()), 1)
        marker = self.workspaces / ".copilot2" / "copilot2.identity"
        state = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(state["invocations"]["other-launcher"], state["identity"])
        self.assertEqual(state["invocations"][state["identity"]], state["identity"])
        remaining = subprocess.run(
            [str(tmux), "has-session", "-t", "=copilot2"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(remaining.returncode, 0)

    def test_transport_retries_preserve_creation_and_process_continuity(self) -> None:
        tmux = self.use_real_remote()
        self.ssh.write_text(
            r'''#!/usr/bin/env python3
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

home = Path(os.environ["HOME"])
remote_command = sys.argv[-1]
session, mode, invocation, action = shlex.split(remote_command)[-4:]
dropped = home / "transport-dropped"
if not dropped.exists() and os.environ["FAKE_DROP_ACTION"] == "connect":
    dropped.touch()
    sys.exit(255)
options = {}
for index, argument in enumerate(sys.argv[1:-1], start=1):
    if argument == "-o":
        key, _, value = sys.argv[index + 1].partition("=")
        options[key.lower()] = value
local_command = options.get("localcommand")
if options.get("permitlocalcommand") == "yes" and local_command:
    subprocess.run(["/bin/sh", "-c", local_command.replace("%%", "%")], check=True)
if not dropped.exists() and os.environ["FAKE_DROP_ACTION"] == "local-command":
    config = Path(sys.argv[sys.argv.index("-F") + 1])
    config.with_name("connected").write_text("connected", encoding="utf-8")
    dropped.touch()
    sys.exit(255)
if action == "prepare":
    result = subprocess.run(["/bin/sh", "-c", remote_command], check=False)
    if result.returncode:
        sys.exit(result.returncode)
    deadline = time.monotonic() + 5
    while not (home / "copilot-starts").exists():
        if time.monotonic() >= deadline:
            sys.exit(91)
        time.sleep(0.02)
    if dropped.exists() or os.environ["FAKE_DROP_ACTION"] != "prepare":
        sys.exit(0)
elif action != "attach":
    sys.exit(90)
if not dropped.exists():
    if os.environ["FAKE_LOSE_PROCESS"] == "1":
        subprocess.run(["tmux", "new-session", "-d", "-s", f"anchor-{session}", "exec sleep 60"], check=True)
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"], check=True)
        if os.environ["FAKE_DROP_ACTION"] == "attach":
            shutil.rmtree(home.parent / "workspaces" / ".copilot2")
    dropped.touch()
    sys.exit(255)
sys.exit(0)
''',
            encoding="utf-8",
        )
        for drop_action in ("connect", "local-command", "prepare", "attach"):
            for lose_process in ((False, True) if drop_action in ("prepare", "attach") else (False,)):
                for initial_mode, arguments in (("start", ()), ("new", ("--new",))):
                    with self.subTest(action=drop_action, loss=lose_process, arguments=arguments):
                        session = f"{drop_action}-{int(lose_process)}-{initial_mode}"
                        self.env["FAKE_DROP_ACTION"] = drop_action
                        self.env["FAKE_LOSE_PROCESS"] = str(int(lose_process))
                        (self.home / "transport-dropped").unlink(missing_ok=True)
                        (self.home / "copilot-starts").unlink(missing_ok=True)

                        result = self.run_wrapper(session, *arguments)

                        self.assertEqual(result.returncode, 12 if lose_process else 0, result.stderr)
                        self.assertTrue((self.home / "transport-dropped").is_file())
                        starts = (self.home / "copilot-starts").read_text().splitlines()
                        self.assertEqual(len(starts), 1)
                        if lose_process:
                            self.assertIn("did not survive", result.stderr)
                            marker = self.workspaces / ".copilot2" / f"{session}.identity"
                            if drop_action == "attach":
                                self.assertFalse(marker.exists())
                            else:
                                state = json.loads(marker.read_text(encoding="utf-8"))
                                self.assertEqual(state["invocations"], {state["identity"]: state["identity"]})
                            remaining = subprocess.run(
                                [str(tmux), "has-session", "-t", f"={session}"],
                                env=self.env, capture_output=True, text=True, check=False,
                            )
                            self.assertNotEqual(remaining.returncode, 0)
                        else:
                            current = subprocess.run(
                                [str(tmux), "display-message", "-p", "-t", f"={session}:", "#{pane_pid}"],
                                env=self.env, capture_output=True, text=True, check=True,
                            )
                            self.assertEqual(current.stdout.strip(), starts[0])

    def test_rejects_zero_reconnect_window_before_ssh(self) -> None:
        self.env["COPILOT2_MAX_RECONNECT_SECONDS"] = "0"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "COPILOT2_MAX_RECONNECT_SECONDS must be a positive integer", result.stderr
        )
        self.assertNotIn("reconnect deadline reached", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.home / "ssh.log").exists())

    def test_invalid_retry_settings_have_actionable_errors(self) -> None:
        for variable in (
            "COPILOT2_MAX_RECONNECT_SECONDS",
            "COPILOT2_RETRY_DELAYS",
        ):
            with self.subTest(variable=variable):
                self.env[variable] = "invalid"
                result = self.run_wrapper()
                self.assertEqual(result.returncode, 2)
                self.assertIn(variable, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.env.pop(variable)

    def test_remote_validation_failure_is_not_retried(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "13"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 13)
        self.assertEqual(
            (self.home / "ssh-count").read_text(encoding="utf-8").strip(),
            "1",
        )

    def test_authentication_loss_stops_retries(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255"
        self.env["FAKE_VIEW_ERROR"] = "authentication expired"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 2)
        self.assertIn("authentication expired", result.stderr)

    def test_deleted_codespace_stops_retries(self) -> None:
        self.env["FAKE_SSH_RESULTS"] = "0,255"
        self.env["FAKE_VIEW_ERROR"] = "HTTP 404: codespace not found"

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 2)
        self.assertIn("not found", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
