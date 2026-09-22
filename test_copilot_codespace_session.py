#!/usr/bin/env python3
"""Regression tests for the remote Copilot tmux helper."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable


def write_isolated_helper(destination: Path, workspaces: Path) -> None:
    source = Path(__file__).with_name("bin") / "copilot-codespace-session"
    destination.write_text(
        f'''#!/usr/bin/env python3
import pathlib
import runpy
from unittest.mock import patch

native_path = type(pathlib.Path())

def isolated_path(*parts):
    path = native_path(*parts)
    try:
        relative = path.relative_to("/workspaces")
    except ValueError:
        return path
    return native_path({str(workspaces)!r}) / relative

with patch("pathlib.Path", wraps=native_path, side_effect=isolated_path):
    runpy.run_path({str(source)!r}, run_name="__main__")
''',
        encoding="utf-8",
    )
    destination.chmod(0o755)


class CopilotCodespaceSessionTest(unittest.TestCase):
    """Exercise the helper with isolated tmux sessions and continuity state."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.workspaces = self.root / "workspaces"
        self.workspace = self.workspaces / "project"
        self.bin_dir = self.root / "bin"
        self.home.mkdir()
        self.workspace.mkdir(parents=True)
        self.bin_dir.mkdir()
        self.helper = self.bin_dir / "copilot-codespace-session"
        write_isolated_helper(self.helper, self.workspaces)

        tmux = self.bin_dir / "tmux"
        tmux.write_text(
            r"""#!/bin/bash
for argument do
  if [ "$argument" = ';' ]; then
    batch=()
    for part do
      if [ "$part" = ';' ]; then
        "$0" "${batch[@]}" || exit "$?"
        batch=()
      else
        batch+=("$part")
      fi
    done
    "$0" "${batch[@]}"
    exit "$?"
  fi
done
printf '%s\n' "$*" >> "$HOME/tmux.log"
session_file="$HOME/tmux-session"
identity_file="$HOME/tmux-identity"
origin_file="$HOME/tmux-origin"
pane_file="$HOME/tmux-pane-state"
case "$1" in
  has-session) [ -f "$session_file" ] ;;
  new-session)
    [ "${FAKE_TMUX_STATUS:-0}" != 0 ] && exit "$FAKE_TMUX_STATUS"
    [ -f "$session_file" ] && exit 1
    : > "$session_file"
    rm -f "$identity_file" "$origin_file"
    printf '$0 1234 0\n' > "$pane_file"
    [ "${FAKE_TMUX_EMPTY_ID:-0}" = 1 ] && exit 0
    for argument do
      case "$argument" in
        '#{session_id} #{pane_id} #{pane_pid}') printf '$0 %%0 1234\n' ;;
        '#{session_id}') printf '$0\n' ;;
      esac
    done
    exit 0
    ;;
  display-message)
    [ "$2" = -p ] && [ "$3" = -t ] || exit 90
    [ -f "$session_file" ] || exit 1
    [ "${FAKE_TMUX_EMPTY_ID:-0}" = 1 ] && exit 0
    case "$5" in
      '#{session_id}')
        case "$4" in =*:) printf '$0\n' ;; *) printf '\n' ;; esac
        ;;
      '#{session_id} #{pane_pid} #{pane_dead}')
        [ "$4" = '%0' ] || exit 90
        if [ -f "$pane_file" ]; then cat "$pane_file"; else printf '  \n'; fi
        ;;
      *) exit 90 ;;
    esac
    ;;
  set-option)
    shift
    expand=0
    if [ "$1" = -F ]; then expand=1; shift; fi
    [ "$1" = -t ] || exit 90
    case "$2" in '$0'|=*:) ;; *) exit 90 ;; esac
    [ "${FAKE_TMUX_IDENTITY_FAIL:-0}" = 1 ] && exit 1
    option="$3"
    value="$4"
    if [ "$expand" = 1 ]; then
      [ "$option" = @copilot2_origin ] && [ "$value" = '#{pane_id} #{pane_pid}' ] || exit 90
      value='%0 1234'
    fi
    case "$option" in
      @copilot2_identity) printf '%s\n' "$value" > "$identity_file" ;;
      @copilot2_origin) printf '%s\n' "$value" > "$origin_file" ;;
      *) exit 90 ;;
    esac
    ;;
  show-options)
    [ "$2" = -qv ] && [ "$3" = -t ] && [ "$4" = '$0' ] || exit 90
    [ "${FAKE_TMUX_IDENTITY_FAIL:-0}" = 1 ] && exit 1
    case "$5" in
      @copilot2_identity) option_file="$identity_file" ;;
      @copilot2_origin) option_file="$origin_file" ;;
      *) exit 90 ;;
    esac
    if [ -f "$option_file" ]; then cat "$option_file"; fi
    ;;
  select-window|select-pane)
    [ "$2" = -t ] && [ "$3" = '%0' ] || exit 90
    printf '%s\n' "$3" > "$HOME/$1"
    ;;
  kill-session)
    [ "$2" = -t ] && [ "$3" = '$0' ] || exit 90
    [ "${FAKE_TMUX_KILL_FAIL:-0}" = 1 ] && exit 1
    rm -f "$session_file" "$identity_file" "$origin_file" "$pane_file"
    ;;
  if-shell)
    [ "$2" = -F ] && [ "$3" = -t ] && [ "$4" = '%0' ] || exit 90
    [ "$6" = 'attach-session -d -t %0' ] && [ "$7" = "run-shell 'exit 12'" ] || exit 90
    [ "${FAKE_TMUX_LOSE_ON_ATTACH:-0}" = 1 ] && rm -f "$pane_file"
    [ -f "$pane_file" ] && [ "$(cat "$pane_file")" = '$0 1234 0' ] || exit 12
    printf '%%0\n' > "$HOME/attached-pane"
    [ "${FAKE_TMUX_END_SESSION:-0}" = 1 ] && rm -f "$session_file" "$identity_file" "$origin_file" "$pane_file"
    exit "${FAKE_TMUX_STATUS:-0}"
    ;;
  attach-session)
    [ "$2" = -d ] && [ "$3" = -t ] && [ "$4" = '$0' ] || exit 90
    [ "${FAKE_TMUX_LOSE_ON_ATTACH:-0}" = 1 ] && rm -f "$pane_file"
    printf 'session-default\n' > "$HOME/attached-pane"
    [ "${FAKE_TMUX_END_SESSION:-0}" = 1 ] && rm -f "$session_file" "$identity_file" "$origin_file" "$pane_file"
    exit "${FAKE_TMUX_STATUS:-0}"
    ;;
  *) exit 90 ;;
esac
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)

        copilot = self.bin_dir / "copilot"
        copilot.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        copilot.chmod(0o755)

        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin_dir}{os.pathsep}{self.env['PATH']}",
                "CODESPACES": "true",
                "COPILOT2_REMOTE_CWD": str(self.workspace),
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            }
        )

    def run_helper(
        self,
        session: str = "copilot2",
        mode: str = "start",
        invocation_id: str = "test-invocation",
        action: str = "prepare",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.helper), session, mode, invocation_id, action],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def use_real_tmux(self) -> None:
        real_tmux = shutil.which("tmux")
        if real_tmux is None:
            self.skipTest("tmux is required for the real-session regressions")
        socket_dir = tempfile.TemporaryDirectory(prefix=".t-", dir=Path(__file__).parent)
        self.addCleanup(socket_dir.cleanup)
        socket = Path(socket_dir.name) / "s"
        tmux = self.bin_dir / "tmux"
        native = f"{shlex.quote(real_tmux)} -S {shlex.quote(str(socket))} -f /dev/null"
        tmux.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = new-session ] && [ -n "${RACE_ID:-}" ]; then\n'
            '  : > "$HOME/race-ready-$RACE_ID"\n'
            '  while [ ! -f "$HOME/race-release-$RACE_ID" ]; do sleep 0.02; done\n'
            "fi\n"
            'if [ "$1" = new-session ] && [ -n "${STARTUP_PAUSE:-}" ]; then\n'
            '  if [ "$STARTUP_PAUSE" = after-create ]; then\n'
            f'    {native} "$@" || exit "$?"\n'
            "  fi\n"
            '  : > "$HOME/startup-paused"\n'
            '  while [ ! -f "$HOME/startup-release" ]; do sleep 0.02; done\n'
            '  [ "$STARTUP_PAUSE" = after-create ] && exit 0\n'
            "fi\n"
            'if [ "${CONTROL_ATTACH:-0}" = 1 ] && { [ "$1" = if-shell ] || [ "$1" = attach-session ]; }; then\n'
            '  case "${ATTACH_MUTATION:-}" in\n'
            f'    removed) {native} kill-pane -t "$TEST_COPILOT_PANE" ;;\n'
            "    dead)\n"
            f'      {native} set-option -p -t "$TEST_COPILOT_PANE" remain-on-exit on\n'
            '      kill "$TEST_COPILOT_PID"\n'
            f'      while [ "$({native} display-message -p -t "$TEST_COPILOT_PANE" \'#{{pane_dead}}\')" != 1 ]; do sleep 0.02; done\n'
            "      ;;\n"
            f'    respawned) {native} respawn-pane -k -t "$TEST_COPILOT_PANE" "exec sleep 60" ;;\n'
            "  esac\n"
            f'  exec {native} -C "$@"\n'
            "fi\n"
            f'exec {native} "$@"\n',
            encoding="utf-8",
        )
        (self.bin_dir / "copilot").write_text(
            "#!/bin/sh\nexec sleep 60\n", encoding="utf-8"
        )
        self.env["SHELL"] = "/bin/sh"
        self.env.pop("TMUX", None)
        self.addCleanup(
            subprocess.run,
            [str(tmux), "kill-server"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def tmux_output(self, *arguments: str) -> str:
        return subprocess.run(
            [str(self.bin_dir / "tmux"), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    def wait_for(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("timed out waiting for the tmux test boundary")
            time.sleep(0.02)

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)

    def run_control_attach(
        self, session: str, pane: str, pid: str, mutation: str = ""
    ) -> tuple[int, str, str]:
        process = subprocess.Popen(
            [str(self.helper), session, "resume", "first", "attach"],
            env=dict(
                self.env,
                CONTROL_ATTACH="1",
                ATTACH_MUTATION=mutation,
                TEST_COPILOT_PANE=pane,
                TEST_COPILOT_PID=pid,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        self.wait_for(
            lambda: process.poll() is not None
            or bool(self.tmux_output("list-clients", "-F", "#{client_session}"))
        )
        attached_pane = ""
        if self.tmux_output("list-clients", "-F", "#{client_session}"):
            attached_pane = self.tmux_output(
                "display-message", "-p", "-t", f"={session}:", "#{pane_id}"
            )
            self.tmux_output("detach-client", "-s", f"={session}")
        process.wait(timeout=10)
        _, stderr = process.communicate(timeout=10)
        return process.returncode, attached_pane, stderr

    def marker(self, session: str = "copilot2") -> Path:
        return self.workspaces / ".copilot2" / f"{session}.identity"

    def legacy_marker(self, session: str = "copilot2") -> Path:
        state_home = Path(self.env.get("XDG_STATE_HOME") or self.home / ".local" / "state")
        return state_home / "copilot2" / f"{session}.identity"

    def read_state(self, session: str = "copilot2") -> dict:
        return json.loads(self.marker(session).read_text(encoding="utf-8"))

    def seed_state(self, identity: str) -> None:
        marker = self.marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"identity": identity, "invocations": {identity: identity}}),
            encoding="utf-8",
        )

    def control_state_publication(self) -> None:
        controls = self.root / "controls"
        controls.mkdir()
        (controls / "sitecustomize.py").write_text(
            r'''import fcntl
import os
import stat
import time
from pathlib import Path

home = Path(os.environ["HOME"])
role = os.environ.get("STATE_ROLE", "")
original_flock = fcntl.flock
original_replace = os.replace
original_fsync = os.fsync

def fsync(descriptor):
    if os.environ.get("STATE_DIRECTORY_SYNC_FAIL") == "1" and stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise OSError("directory sync denied")
    return original_fsync(descriptor)

def flock(descriptor, operation):
    if role and operation & fcntl.LOCK_EX:
        (home / f"lock-wait-{role}").touch()
    result = original_flock(descriptor, operation)
    if role and operation & fcntl.LOCK_EX:
        (home / f"lock-held-{role}").touch()
    return result

def replace(source, destination):
    if os.fspath(destination) == os.environ["TEST_STATE_PATH"]:
        if os.environ.get("STATE_PUBLISH_FAIL") == "1":
            raise PermissionError("publication denied")
        if role and os.environ.get("STATE_PAUSE_PUBLISH", "1") == "1":
            (home / f"publish-ready-{role}").touch()
            while not (home / f"publish-release-{role}").exists():
                time.sleep(0.02)
    return original_replace(source, destination)

fcntl.flock = flock
os.replace = replace
os.fsync = fsync
''',
            encoding="utf-8",
        )
        self.env["PYTHONPATH"] = str(controls)
        self.env["TEST_STATE_PATH"] = str(self.marker())

    def start_helper(
        self, invocation: str, role: str, mode: str = "start", action: str = "prepare"
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [str(self.helper), "copilot2", mode, invocation, action],
            env=dict(self.env, STATE_ROLE=role, CONTROL_ATTACH="1"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        return process

    def interrupt_startup(self, phase: str) -> None:
        process = subprocess.Popen(
            [str(self.helper), "copilot2", "new", "first", "prepare"],
            env=dict(self.env, STARTUP_PAUSE=phase),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        self.wait_for((self.home / "startup-paused").exists)
        os.killpg(process.pid, signal.SIGHUP)
        process.wait(timeout=10)
        self.assertEqual(process.returncode, -signal.SIGHUP)
        process.communicate(timeout=10)

    def finish_helper(self, process: subprocess.Popen[str]) -> None:
        process.wait(timeout=10)
        _, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error)

    def finish_competing_publications(
        self, earlier: subprocess.Popen[str], role: str, replacement: subprocess.Popen[str]
    ) -> None:
        waiting = self.home / "lock-wait-replacement"
        ready = self.home / "publish-ready-replacement"
        self.wait_for(lambda: waiting.exists() or ready.exists())
        if ready.exists():
            (self.home / "publish-release-replacement").touch()
            self.finish_helper(replacement)
            (self.home / f"publish-release-{role}").touch()
            self.finish_helper(earlier)
        else:
            self.assertFalse((self.home / "lock-held-replacement").exists())
            (self.home / f"publish-release-{role}").touch()
            self.finish_helper(earlier)
            self.wait_for(ready.exists)
            (self.home / "publish-release-replacement").touch()
            self.finish_helper(replacement)
        self.assertEqual(self.read_state()["identity"], "replacement")
        self.assertEqual(
            self.tmux_output("show-options", "-qv", "-t", "=copilot2:", "@copilot2_identity"),
            "replacement",
        )

    def test_creates_marked_copilot_session(self) -> None:
        result = self.run_helper("review-one")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / "tmux.log").read_text(encoding="utf-8")
        self.assertIn("new-session -d -s review-one", calls)
        self.assertIn("exec copilot --allow-all", calls)
        self.assertTrue((self.home / "tmux-session").is_file())
        self.assertEqual(
            self.read_state("review-one"),
            {"identity": "test-invocation", "invocations": {"test-invocation": "test-invocation"}},
        )
        self.assertEqual(
            (self.home / "tmux-identity").read_text(encoding="utf-8").strip(),
            "test-invocation",
        )

    def test_new_launcher_after_rebuild_requires_explicit_replacement(self) -> None:
        self.use_real_tmux()
        session = "review-one"
        prepared = self.run_helper(session=session, invocation_id="first-device")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        original_pid = self.tmux_output("display-message", "-p", "-t", f"={session}:", "#{pane_pid}")
        joined = self.run_helper(session=session, invocation_id="second-device")
        self.assertEqual(joined.returncode, 0, joined.stderr)
        self.assertEqual(
            self.tmux_output("display-message", "-p", "-t", f"={session}:", "#{pane_pid}"), original_pid
        )
        before = self.marker(session).read_bytes()
        self.tmux_output("kill-server")
        shutil.rmtree(self.home)
        self.home.mkdir()
        checkout = self.workspaces / "another-project" / "src"
        checkout.mkdir(parents=True)
        self.env["COPILOT2_REMOTE_CWD"] = str(checkout)
        self.env["XDG_STATE_HOME"] = str(self.home / "new-state")

        result = self.run_helper(session=session, invocation_id="after-rebuild")

        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("did not survive", result.stderr)
        self.assertIn("copilot2 --new", result.stderr)
        self.assertEqual(self.marker(session).read_bytes(), before)
        remaining = subprocess.run(
            [str(self.bin_dir / "tmux"), "has-session", "-t", f"={session}"],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(remaining.returncode, 0)
        replacement = self.run_helper(session=session, mode="new", invocation_id="replacement")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        self.assertEqual(
            self.read_state(session),
            {
                "identity": "replacement",
                "invocations": {
                    "first-device": "first-device",
                    "second-device": "first-device",
                    "replacement": "replacement",
                },
            },
        )
        replay = self.run_helper(session=session, mode="resume", invocation_id="first-device")
        self.assertEqual(replay.returncode, 12, replay.stderr)

    def test_migrates_all_legacy_sessions_from_default_or_xdg_state(self) -> None:
        for index, setting in enumerate((None, "", str(self.root / "custom-state"))):
            with self.subTest(setting=setting):
                if setting is None:
                    self.env.pop("XDG_STATE_HOME", None)
                else:
                    self.env["XDG_STATE_HOME"] = setting
                sessions = {
                    f"active-{index}": {"identity": "first", "invocations": {"first": "first", "other": "first"}},
                    f"retired-{index}": {"identity": None, "invocations": {"old": "old"}},
                }
                for session, state in sessions.items():
                    legacy = self.legacy_marker(session)
                    legacy.parent.mkdir(parents=True, exist_ok=True)
                    legacy.write_text(json.dumps(state), encoding="utf-8")

                result = self.run_helper(session=f"active-{index}")

                self.assertEqual(result.returncode, 12, result.stderr)
                self.assertFalse((self.home / "tmux-session").exists())
                for session, state in sessions.items():
                    self.assertEqual(self.read_state(session), state)
                    self.assertEqual(self.legacy_marker(session).read_text(), json.dumps(state))
                    self.assertEqual(stat.S_IMODE(self.marker(session).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.marker().parent.stat().st_mode), 0o700)

    def test_migration_preserves_a_live_process_and_its_invocations(self) -> None:
        self.use_real_tmux()
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        original_pid = self.tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}")
        legacy = self.legacy_marker()
        legacy.parent.mkdir(parents=True)
        self.marker().replace(legacy)
        before = legacy.read_bytes()

        migrated = self.run_helper(invocation_id="second")

        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertEqual(
            self.tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}"), original_pid
        )
        self.assertEqual(self.read_state(), {"identity": "first", "invocations": {"first": "first", "second": "first"}})
        self.assertEqual(legacy.read_bytes(), before)
        resumed = self.run_helper(mode="resume", invocation_id="first")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_persistent_state_takes_precedence_over_legacy_backups(self) -> None:
        self.seed_state("current")
        before = self.marker().read_bytes()
        legacy = self.legacy_marker()
        legacy.parent.mkdir(parents=True)
        for contents in (
            json.dumps({"identity": "previous", "invocations": {"previous": "previous"}}),
            "invalid legacy backup",
        ):
            with self.subTest(contents=contents):
                legacy.write_text(contents, encoding="utf-8")

                result = self.run_helper()

                self.assertEqual(result.returncode, 12, result.stderr)
                self.assertEqual(self.marker().read_bytes(), before)
                self.assertEqual(legacy.read_text(), contents)
                self.assertFalse((self.home / "tmux-session").exists())

    def test_invalid_legacy_state_blocks_creation_without_overwriting_the_source(self) -> None:
        legacy = self.legacy_marker()
        legacy.parent.mkdir(parents=True)
        for contents in ("", "not JSON", "[]", '{"identity":"first","invocations":{}}'):
            with self.subTest(contents=contents):
                legacy.write_text(contents, encoding="utf-8")

                result = self.run_helper(mode="new")

                self.assertEqual(result.returncode, 13, result.stderr)
                self.assertFalse(self.marker().exists())
                self.assertEqual(legacy.read_text(), contents)
                self.assertFalse((self.home / "tmux.log").exists())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permission checks")
    def test_unreadable_legacy_state_blocks_migration(self) -> None:
        legacy = self.legacy_marker()
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"identity": "first", "invocations": {"first": "first"}}), encoding="utf-8")
        before = legacy.read_bytes()
        legacy.chmod(0o000)
        self.addCleanup(legacy.chmod, 0o600)

        result = self.run_helper(mode="new")

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertFalse(self.marker().exists())
        self.assertFalse((self.home / "tmux.log").exists())
        legacy.chmod(0o600)
        self.assertEqual(legacy.read_bytes(), before)

    def test_migration_publication_failure_preserves_the_source_for_retry(self) -> None:
        legacy = self.legacy_marker()
        legacy.parent.mkdir(parents=True)
        state = {"identity": "first", "invocations": {"first": "first"}}
        legacy.write_text(json.dumps(state), encoding="utf-8")
        before = legacy.read_bytes()
        self.control_state_publication()
        self.env["STATE_PUBLISH_FAIL"] = "1"

        result = self.run_helper()

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn("publication denied", result.stderr)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(self.marker().exists())
        self.assertEqual(list(self.marker().parent.glob(".copilot2.*")), [])
        self.assertFalse((self.home / "tmux.log").exists())
        del self.env["STATE_PUBLISH_FAIL"]
        retry = self.run_helper()
        self.assertEqual(retry.returncode, 12, retry.stderr)
        self.assertEqual(self.read_state(), state)
        self.assertEqual(legacy.read_bytes(), before)

    def test_concurrent_migration_does_not_overwrite_new_invocation_grants(self) -> None:
        self.use_real_tmux()
        legacy = self.legacy_marker()
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"identity": None, "invocations": {"old": "old"}}), encoding="utf-8")
        self.control_state_publication()
        first = self.start_helper("first", "migration")
        self.wait_for((self.home / "publish-ready-migration").exists)
        second = self.start_helper("second", "follower")
        self.wait_for((self.home / "lock-wait-follower").exists)
        self.assertFalse((self.home / "lock-held-follower").exists())
        (self.home / "publish-release-migration").touch()
        (self.home / "publish-release-follower").touch()
        self.finish_helper(first)
        self.finish_helper(second)

        state = self.read_state()
        self.assertIn(state["identity"], ("first", "second"))
        self.assertEqual(
            state["invocations"], {"old": "old", "first": state["identity"], "second": state["identity"]}
        )
        self.assertEqual(self.tmux_output("list-sessions", "-F", "#{session_name}"), "copilot2")
        self.assertEqual(json.loads(legacy.read_text()), {"identity": None, "invocations": {"old": "old"}})

    def test_reports_missing_session_after_previous_connection(self) -> None:
        self.seed_state("previous-boot")
        before = self.marker().read_bytes()

        result = self.run_helper(action="prepare")

        self.assertEqual(result.returncode, 12)
        self.assertIn("did not survive", result.stderr)
        self.assertIn("copilot2 --new", result.stderr)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertNotIn("new-session", (self.home / "tmux.log").read_text(encoding="utf-8"))
        self.assertEqual(self.marker().read_bytes(), before)

    def test_resume_without_continuity_never_recreates_the_session(self) -> None:
        prepared = self.run_helper(mode="new")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        for name in ("tmux-session", "tmux-identity", "tmux-origin", "tmux-pane-state"):
            (self.home / name).unlink()
        self.marker().unlink()

        for contents in (None, '{"identity":null,"invocations":{}}'):
            if contents is not None:
                self.marker().write_text(contents, encoding="utf-8")
            for action in ("prepare", "attach"):
                with self.subTest(contents=contents, action=action):
                    result = self.run_helper(mode="resume", action=action)

                    self.assertEqual(result.returncode, 12, result.stderr)
                    self.assertIn("did not survive", result.stderr)
                    self.assertFalse((self.home / "tmux-session").exists())
                    if contents is None:
                        self.assertFalse(self.marker().exists())
                    else:
                        self.assertEqual(self.marker().read_text(encoding="utf-8"), contents)
        calls = (self.home / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(call.startswith("new-session ") for call in calls), 1)

    def test_resume_without_a_binding_does_not_adopt_a_replacement(self) -> None:
        prepared = self.run_helper(mode="new", invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        (self.home / "tmux-session").unlink()
        self.marker().unlink()
        replacement = self.run_helper(mode="new", invocation_id="replacement")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        before = self.marker().read_bytes()

        for action in ("prepare", "attach"):
            with self.subTest(action=action):
                result = self.run_helper(mode="resume", invocation_id="first", action=action)

                self.assertEqual(result.returncode, 12, result.stderr)
                self.assertIn("did not survive", result.stderr)
                self.assertEqual(self.marker().read_bytes(), before)
                self.assertTrue((self.home / "tmux-session").exists())
                self.assertFalse((self.home / "attached-pane").exists())

    def test_new_recovery_for_named_session_creates_or_reuses_the_expected_process(self) -> None:
        session = "review-one"
        marker = self.marker(session)
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({"identity": "previous", "invocations": {"previous": "previous"}}),
            encoding="utf-8",
        )

        lost = self.run_helper(session=session, invocation_id="observing")

        self.assertEqual(lost.returncode, 12, lost.stderr)
        self.assertIn("copilot2 --new", lost.stderr)
        self.assertIn(session, lost.stderr)
        replacement = self.run_helper(session=session, mode="new", invocation_id="replacement")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        joined = self.run_helper(session=session, mode="new", invocation_id="joining")
        self.assertEqual(joined.returncode, 0, joined.stderr)
        self.assertEqual(
            self.read_state(session),
            {
                "identity": "replacement",
                "invocations": {
                    "previous": "previous",
                    "replacement": "replacement",
                    "joining": "replacement",
                },
            },
        )
        calls = (self.home / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(call.startswith("new-session ") for call in calls), 1)

    def test_attach_reports_missing_session(self) -> None:
        result = self.run_helper(action="attach")

        self.assertEqual(result.returncode, 12)
        self.assertIn("did not survive", result.stderr)
        self.assertFalse((self.home / "tmux-session").exists())

    def test_invalid_legacy_state_directory_blocks_session_start(self) -> None:
        invalid_state = self.root / "state-file"
        invalid_state.write_text("not a directory\n", encoding="utf-8")
        self.env["XDG_STATE_HOME"] = str(invalid_state)

        result = self.run_helper()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertEqual(invalid_state.read_text(encoding="utf-8"), "not a directory\n")

    def test_unavailable_persistent_state_does_not_fall_back_to_home(self) -> None:
        state_dir = self.marker().parent
        state_dir.write_text("not a directory\n", encoding="utf-8")

        result = self.run_helper()

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertEqual(state_dir.read_text(), "not a directory\n")
        self.assertFalse(self.legacy_marker().exists())
        self.assertFalse((self.home / "tmux.log").exists())

    def test_unwritable_marker_blocks_session_start(self) -> None:
        marker = self.marker()
        marker.mkdir(parents=True)

        result = self.run_helper()

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.is_dir())
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertFalse((self.home / "tmux.log").exists())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permission checks")
    def test_unwritable_marker_blocks_refresh(self) -> None:
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        marker = self.marker()
        marker.chmod(0o400)

        result = self.run_helper(invocation_id="second")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_state(), {"identity": "first", "invocations": {"first": "first"}})
        self.assertTrue((self.home / "tmux-session").exists())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permission checks")
    def test_unreadable_marker_blocks_new_session(self) -> None:
        self.seed_state("test-invocation")
        before = self.marker().read_bytes()
        self.marker().chmod(0o000)

        result = self.run_helper(mode="new")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "tmux-session").exists())
        self.marker().chmod(0o600)
        self.assertEqual(self.marker().read_bytes(), before)

    def test_reservation_failure_does_not_start_or_replace_session(self) -> None:
        self.seed_state("previous-boot")
        before = self.marker().read_bytes()
        self.control_state_publication()
        self.env["STATE_PUBLISH_FAIL"] = "1"

        result = self.run_helper(mode="new")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication denied", result.stderr)
        self.assertEqual(self.marker().read_bytes(), before)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertNotIn("new-session", (self.home / "tmux.log").read_text(encoding="utf-8"))
        self.assertEqual(list(self.marker().parent.glob(".copilot2.*")), [])

    def test_directory_sync_failure_prevents_process_creation(self) -> None:
        self.control_state_publication()
        self.env["STATE_DIRECTORY_SYNC_FAIL"] = "1"

        result = self.run_helper()

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn("directory sync denied", result.stderr)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertNotIn("new-session", (self.home / "tmux.log").read_text(encoding="utf-8"))
        self.assertEqual(self.read_state()["identity"], "test-invocation")

    def test_cleanup_publication_failure_is_reported_after_session_exit(self) -> None:
        prepared = self.run_helper()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        before = self.marker().read_bytes()
        self.env["FAKE_TMUX_END_SESSION"] = "1"
        self.control_state_publication()
        self.env["STATE_PUBLISH_FAIL"] = "1"

        result = self.run_helper(action="attach")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication denied", result.stderr)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertEqual(self.marker().read_bytes(), before)

    def test_handled_creation_failure_restores_previous_continuity(self) -> None:
        self.env["FAKE_TMUX_STATUS"] = "13"
        for prior in (None, "previous"):
            with self.subTest(prior=prior):
                if prior is not None:
                    self.seed_state(prior)
                expected = {"identity": prior, "invocations": {prior: prior} if prior else {}}

                result = self.run_helper(mode="new")

                self.assertEqual(result.returncode, 13, result.stderr)
                self.assertFalse((self.home / "tmux-session").exists())
                self.assertEqual(self.read_state(), expected)
        del self.env["FAKE_TMUX_STATUS"]
        retry = self.run_helper(mode="new")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue((self.home / "tmux-session").exists())

    def test_unmarked_session_is_not_adopted(self) -> None:
        existing = self.home / "tmux-session"
        existing.write_text("unrelated shell\n", encoding="utf-8")
        marker = self.marker()
        log = self.home / "tmux.log"
        for action in ("prepare", "attach"):
            for mode in ("start", "new"):
                with self.subTest(action=action, mode=mode):
                    marker.unlink(missing_ok=True)
                    log.unlink(missing_ok=True)

                    result = self.run_helper(action=action, mode=mode)

                    self.assertEqual(result.returncode, 13)
                    self.assertIn("session-name collision", result.stderr)
                    self.assertFalse(marker.exists())
                    self.assertEqual(existing.read_text(encoding="utf-8"), "unrelated shell\n")
                    calls = log.read_text(encoding="utf-8")
                    self.assertNotIn("new-session", calls)
                    self.assertNotIn("attach-session", calls)

    def test_stale_or_unbound_marker_does_not_adopt_an_unrelated_session(self) -> None:
        existing = self.home / "tmux-session"
        existing.write_text("unrelated shell\n", encoding="utf-8")
        marker = self.marker()
        marker.parent.mkdir(parents=True)
        identity_file = self.home / "tmux-identity"
        log = self.home / "tmux.log"
        prior = json.dumps({"identity": "previous-session", "invocations": {"previous-session": "previous-session"}})
        empty = json.dumps({"identity": None, "invocations": {}})
        for contents, identity in ((prior, None), (prior, "other-session"), (empty, "previous-session")):
            for action in ("prepare", "attach"):
                for mode in ("start", "new"):
                    with self.subTest(contents=contents, identity=identity, action=action, mode=mode):
                        marker.write_text(contents, encoding="utf-8")
                        identity_file.unlink(missing_ok=True)
                        if identity is not None:
                            identity_file.write_text(f"{identity}\n", encoding="utf-8")
                        log.unlink(missing_ok=True)

                        result = self.run_helper(action=action, mode=mode)

                        self.assertEqual(result.returncode, 13, result.stderr)
                        self.assertIn("session-name collision", result.stderr)
                        self.assertEqual(marker.read_text(encoding="utf-8"), contents)
                        self.assertEqual(existing.read_text(encoding="utf-8"), "unrelated shell\n")
                        calls = log.read_text(encoding="utf-8")
                        self.assertNotIn("attach-session", calls)
                        self.assertNotIn("new-session", calls)
                        self.assertNotIn("set-option", calls)

    def test_identity_initialization_failure_does_not_report_readiness(self) -> None:
        self.env["FAKE_TMUX_IDENTITY_FAIL"] = "1"

        result = self.run_helper()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertFalse((self.home / "tmux-identity").exists())
        marker = self.marker()
        self.assertEqual(self.read_state(), {"identity": None, "invocations": {}})
        self.assertEqual(list(marker.parent.glob(".copilot2.*")), [])
        self.env["FAKE_TMUX_IDENTITY_FAIL"] = "0"
        retry = self.run_helper()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue((self.home / "tmux-session").exists())

    def test_failed_cleanup_keeps_the_generation_reserved(self) -> None:
        self.env["FAKE_TMUX_IDENTITY_FAIL"] = "1"
        self.env["FAKE_TMUX_KILL_FAIL"] = "1"

        result = self.run_helper()

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn("could not remove incomplete session", result.stderr)
        self.assertTrue((self.home / "tmux-session").exists())
        self.assertEqual(self.read_state()["identity"], "test-invocation")
        (self.home / "tmux-session").unlink()
        self.env["FAKE_TMUX_IDENTITY_FAIL"] = "0"
        self.env["FAKE_TMUX_KILL_FAIL"] = "0"
        retry = self.run_helper(invocation_id="fresh")
        self.assertEqual(retry.returncode, 12, retry.stderr)
        self.assertFalse((self.home / "tmux-session").exists())

    def test_identity_lookup_failure_preserves_marker(self) -> None:
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.env["FAKE_TMUX_IDENTITY_FAIL"] = "1"

        result = self.run_helper(invocation_id="second")

        self.assertEqual(result.returncode, 13)
        self.assertIn("session-name collision", result.stderr)
        self.assertEqual(self.read_state(), {"identity": "first", "invocations": {"first": "first"}})

    def test_missing_session_id_never_uses_the_default_context(self) -> None:
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.env["FAKE_TMUX_EMPTY_ID"] = "1"
        log = self.home / "tmux.log"
        log.unlink()

        result = self.run_helper(invocation_id="second", action="attach")

        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("did not survive", result.stderr)
        self.assertNotIn("attach-session", log.read_text(encoding="utf-8"))
        self.assertNotIn("show-options", log.read_text(encoding="utf-8"))
        self.assertEqual(self.read_state(), {"identity": "first", "invocations": {"first": "first"}})

    def test_missing_creation_response_retains_reservation_for_recovery(self) -> None:
        self.env["FAKE_TMUX_EMPTY_ID"] = "1"

        result = self.run_helper()

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn("could not identify new session", result.stderr)
        self.assertEqual(
            self.read_state(),
            {"identity": "test-invocation", "invocations": {"test-invocation": "test-invocation"}},
        )
        self.assertEqual((self.home / "tmux-identity").read_text().strip(), "test-invocation")
        self.env["FAKE_TMUX_EMPTY_ID"] = "0"
        (self.home / "tmux.log").unlink()
        recovered = self.run_helper(mode="new")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertNotIn("new-session", (self.home / "tmux.log").read_text(encoding="utf-8"))

    def test_interruption_before_creation_retains_a_durable_reservation(self) -> None:
        self.use_real_tmux()

        self.interrupt_startup("before-create")

        for mode, invocation in (("new", "first"), ("resume", "fresh")):
            result = self.run_helper(mode=mode, invocation_id=invocation)
            self.assertEqual(result.returncode, 12, result.stderr)
            self.assertIn("did not survive", result.stderr)
        self.assertEqual(self.read_state(), {"identity": "first", "invocations": {"first": "first"}})

    def test_surviving_startup_recovers_after_helper_sighup(self) -> None:
        self.use_real_tmux()

        self.interrupt_startup("after-create")
        pane, pid = self.tmux_output(
            "display-message", "-p", "-t", "=copilot2:", "#{pane_id} #{pane_pid}"
        ).split()
        result = self.run_helper(mode="new", invocation_id="first")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_state(), {"identity": "first", "invocations": {"first": "first"}})
        self.assertEqual(
            self.tmux_output("display-message", "-p", "-t", pane, "#{pane_pid}"), pid
        )
        status, attached_pane, stderr = self.run_control_attach("copilot2", pane, pid)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(attached_pane, pane)

    def test_stop_after_interrupted_startup_requires_explicit_replacement(self) -> None:
        self.use_real_tmux()
        self.interrupt_startup("after-create")
        self.tmux_output("new-session", "-d", "-s", "anchor", "exec sleep 60")
        self.tmux_output("kill-session", "-t", "=copilot2")

        result = self.run_helper(invocation_id="fresh")

        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("did not survive", result.stderr)
        replay = self.run_helper(mode="new", invocation_id="first")
        self.assertEqual(replay.returncode, 12, replay.stderr)
        replacement = self.run_helper(mode="new", invocation_id="fresh")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        self.assertEqual(
            self.read_state(),
            {"identity": "fresh", "invocations": {"first": "first", "fresh": "fresh"}},
        )

    def test_interrupted_replacement_keeps_its_identity_and_prior_grants(self) -> None:
        self.use_real_tmux()
        self.seed_state("previous")

        self.interrupt_startup("after-create")
        pid = self.tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}")
        recovered = self.run_helper(mode="new", invocation_id="first")

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            self.read_state(),
            {"identity": "first", "invocations": {"previous": "previous", "first": "first"}},
        )
        self.assertEqual(
            self.tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}"), pid
        )

    def test_preexisting_tmux_server_uses_current_path_for_copilot_session(self) -> None:
        self.use_real_tmux()
        old_bin = self.root / "old runtime"
        new_bin = self.home / ".local" / "bin"
        for directory, version, label in (
            (old_bin, "v20.0.0", "old-npx"),
            (new_bin, "v22.0.0", "new-npx"),
        ):
            directory.mkdir(parents=True)
            node = directory / "node"
            node.write_text(f"#!/bin/sh\nprintf '{version}\\n'\n", encoding="utf-8")
            node.chmod(0o755)
            npx = directory / "npx"
            npx.write_text(
                f"#!/bin/sh\nprintf '{label}\\n'\nexec node --version\n",
                encoding="utf-8",
            )
            npx.chmod(0o755)
        (self.bin_dir / "copilot").write_text(
            "#!/bin/sh\n"
            'node --version > "$HOME/launched-node"\n'
            'npx --version > "$HOME/launched-npx"\n'
            'printf \'%s\\n\' "$PATH" > "$HOME/launched-path"\n'
            ': > "$HOME/runtime-ready"\n'
            "exec sleep 60\n",
            encoding="utf-8",
        )
        stale_path = f"{old_bin}{os.pathsep}{self.env['PATH']}"
        self.env["PATH"] = stale_path
        self.tmux_output("new-session", "-d", "-s", "anchor", "exec sleep 60")
        anchor_pid = self.tmux_output("display-message", "-p", "-t", "=anchor:", "#{pane_pid}")
        self.assertEqual(self.tmux_output("show-environment", "-g", "PATH"), f"PATH={stale_path}")
        self.env["PATH"] = f"{new_bin}{os.pathsep}{stale_path}"

        result = self.run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for((self.home / "runtime-ready").exists)
        self.assertEqual((self.home / "launched-node").read_text(encoding="utf-8"), "v22.0.0\n")
        self.assertEqual((self.home / "launched-npx").read_text(encoding="utf-8"), "new-npx\nv22.0.0\n")
        self.assertEqual((self.home / "launched-path").read_text(encoding="utf-8").strip(), self.env["PATH"])
        self.tmux_output("run-shell", "-t", "=copilot2:", 'npx --version > "$HOME/session-npx"')
        self.assertEqual((self.home / "session-npx").read_text(encoding="utf-8"), "new-npx\nv22.0.0\n")
        self.assertEqual(self.tmux_output("show-environment", "-t", "=copilot2", "PATH"), f"PATH={self.env['PATH']}")
        self.assertEqual(self.tmux_output("show-environment", "-g", "PATH"), f"PATH={stale_path}")
        self.assertEqual(
            self.tmux_output("display-message", "-p", "-t", "=anchor:", "#{pane_pid}"), anchor_pid
        )

    def test_real_tmux_preserves_identity_and_rejects_a_reused_name(self) -> None:
        self.use_real_tmux()
        tmux_output = self.tmux_output

        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        original_pid = tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}")
        tmux_output("new-session", "-d", "-s", "anchor", "exec sleep 60")
        tmux_output("set-option", "-t", "=anchor:", "@copilot2_identity", "anchor")
        refreshed = self.run_helper(invocation_id="second")
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        resumed = self.run_helper(mode="resume", invocation_id="second")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}"),
            original_pid,
        )
        self.assertEqual(
            tmux_output("show-options", "-qv", "-t", "=copilot2:", "@copilot2_identity"),
            "first",
        )
        expected = {"identity": "first", "invocations": {"first": "first", "second": "first"}}
        self.assertEqual(self.read_state(), expected)

        tmux_output("kill-session", "-t", "=copilot2")
        tmux_output("new-session", "-d", "-s", "copilot2", "exec sleep 60")
        tmux_output("set-option", "-t", "=copilot2:", "@copilot2_identity", "unrelated")
        unrelated_pid = tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}")
        for action in ("prepare", "attach"):
            with self.subTest(action=action):
                result = self.run_helper(invocation_id="third", action=action)

                self.assertEqual(result.returncode, 13, result.stderr)
                self.assertIn("session-name collision", result.stderr)
                self.assertEqual(self.read_state(), expected)
                self.assertEqual(
                    tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}"),
                    unrelated_pid,
                )
                self.assertEqual(
                    tmux_output("show-options", "-qv", "-t", "=copilot2:", "@copilot2_identity"),
                    "unrelated",
                )

    def test_real_tmux_detects_original_process_loss_with_other_windows_alive(self) -> None:
        self.use_real_tmux()
        for loss in ("removed", "dead", "respawned"):
            with self.subTest(loss=loss):
                prepared = self.run_helper(session=loss, invocation_id="first")
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                pane, pid = self.tmux_output(
                    "display-message", "-p", "-t", f"={loss}:", "#{pane_id} #{pane_pid}"
                ).split()
                other_pane = self.tmux_output(
                    "new-window", "-t", f"={loss}:", "-P", "-F", "#{pane_id}", "exec sleep 60"
                )
                alive = self.run_helper(session=loss, invocation_id="still-alive")
                self.assertEqual(alive.returncode, 0, alive.stderr)
                self.assertEqual(
                    self.tmux_output("display-message", "-p", "-t", pane, "#{pane_pid}"), pid
                )
                marker = self.marker(loss)
                before = marker.read_bytes()
                if loss == "removed":
                    self.tmux_output("kill-pane", "-t", pane)
                elif loss == "dead":
                    self.tmux_output("set-option", "-p", "-t", pane, "remain-on-exit", "on")
                    os.kill(int(pid), signal.SIGTERM)
                    self.wait_for(lambda: self.tmux_output("display-message", "-p", "-t", pane, "#{pane_dead}") == "1")
                else:
                    self.tmux_output("respawn-pane", "-k", "-t", pane, "exec sleep 60")
                    self.assertNotEqual(
                        self.tmux_output("display-message", "-p", "-t", pane, "#{pane_pid}"), pid
                    )
                for action in ("prepare", "attach"):
                    result = self.run_helper(session=loss, invocation_id="second", action=action)
                    self.assertEqual(result.returncode, 12, result.stderr)
                    self.assertIn("original Copilot process", result.stderr)
                    self.assertEqual(marker.read_bytes(), before)
                    self.assertEqual(
                        self.tmux_output("display-message", "-p", "-t", other_pane, "#{pane_dead}"), "0"
                    )

    def test_real_tmux_concurrent_prepares_share_one_process_and_retain_both_grants(self) -> None:
        self.use_real_tmux()
        self.control_state_publication()
        processes = []
        for invocation in ("winner", "follower"):
            env = dict(self.env, STATE_ROLE=invocation, STATE_PAUSE_PUBLISH="0")
            if invocation == "winner":
                env["RACE_ID"] = invocation
            process = subprocess.Popen(
                [str(self.helper), "copilot2", "new", invocation, "prepare"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            processes.append(process)
            self.addCleanup(self.stop_process, process)
            if invocation == "winner":
                self.wait_for((self.home / "race-ready-winner").exists)
            else:
                self.wait_for((self.home / "lock-wait-follower").exists)
        self.assertFalse((self.home / "lock-held-follower").exists())
        self.assertEqual(
            self.read_state(), {"identity": "winner", "invocations": {"winner": "winner"}}
        )
        (self.home / "race-release-winner").touch()
        for process in processes:
            self.finish_helper(process)
        self.assertEqual(
            self.read_state(),
            {"identity": "winner", "invocations": {"winner": "winner", "follower": "winner"}},
        )
        self.assertEqual(self.tmux_output("list-sessions", "-F", "#{session_name}"), "copilot2")
        self.assertEqual(list(self.marker().parent.glob(".copilot2.*")), [])
        pid = self.tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}")
        resumed = self.run_helper(invocation_id="later")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.read_state()["invocations"]["later"], "winner")
        self.assertEqual(self.tmux_output("display-message", "-p", "-t", "=copilot2:", "#{pane_pid}"), pid)

    def test_refresh_cannot_overwrite_a_concurrent_replacement(self) -> None:
        self.use_real_tmux()
        self.control_state_publication()
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        refresh = self.start_helper("refresher", "refresh")
        self.wait_for((self.home / "publish-ready-refresh").exists)
        parallel = self.run_helper(session="parallel", invocation_id="parallel")
        self.assertEqual(parallel.returncode, 0, parallel.stderr)
        self.tmux_output("kill-session", "-t", "=copilot2")
        replacement = self.start_helper("replacement", "replacement", mode="new")

        self.finish_competing_publications(refresh, "refresh", replacement)

        self.assertEqual(
            self.read_state()["invocations"],
            {"first": "first", "refresher": "first", "replacement": "replacement"},
        )
        replay = self.run_helper(mode="new", invocation_id="refresher")
        self.assertEqual(replay.returncode, 12, replay.stderr)
        observed = self.run_helper(invocation_id="observer")
        self.assertEqual(observed.returncode, 0, observed.stderr)

    def test_cleanup_cannot_retire_a_concurrent_replacement(self) -> None:
        self.use_real_tmux()
        self.control_state_publication()
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.tmux_output("new-session", "-d", "-s", "anchor", "exec sleep 60")
        cleanup = self.start_helper("first", "cleanup", action="attach")
        self.wait_for(lambda: bool(self.tmux_output("list-clients", "-F", "#{client_session}")))
        self.tmux_output("kill-session", "-t", "=copilot2")
        self.wait_for((self.home / "publish-ready-cleanup").exists)
        replacement = self.start_helper("replacement", "replacement", mode="new")

        self.finish_competing_publications(cleanup, "cleanup", replacement)

        self.assertEqual(
            self.read_state()["invocations"], {"first": "first", "replacement": "replacement"}
        )
        replay = self.run_helper(mode="new", invocation_id="first")
        self.assertEqual(replay.returncode, 12, replay.stderr)

    def test_attachment_does_not_hold_the_session_lock(self) -> None:
        self.use_real_tmux()
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        attached = self.start_helper("first", "", action="attach")
        self.wait_for(lambda: bool(self.tmux_output("list-clients", "-F", "#{client_session}")))

        observer = self.run_helper(invocation_id="observer")

        self.assertEqual(observer.returncode, 0, observer.stderr)
        self.assertEqual(self.read_state()["invocations"], {"first": "first", "observer": "first"})
        self.tmux_output("detach-client", "-s", "=copilot2")
        self.finish_helper(attached)

    def test_every_ambiguous_new_invocation_stays_bound_after_other_prepares(self) -> None:
        authorized = ("creator", "ambiguous-one", "ambiguous-two")
        for invocation in (*authorized, "other-launcher"):
            prepared = self.run_helper(mode="new", invocation_id=invocation)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
        (self.home / "tmux-session").unlink()
        (self.home / "tmux.log").unlink()
        for invocation in authorized:
            with self.subTest(invocation=invocation):
                result = self.run_helper(mode="new", invocation_id=invocation)
                self.assertEqual(result.returncode, 12, result.stderr)
                self.assertIn("did not survive", result.stderr)
                self.assertFalse((self.home / "tmux-session").exists())
        self.assertNotIn("new-session", (self.home / "tmux.log").read_text(encoding="utf-8"))
        self.assertEqual(
            self.read_state(),
            {
                "identity": "creator",
                "invocations": {name: "creator" for name in (*authorized, "other-launcher")},
            },
        )

    def test_old_invocations_cannot_adopt_or_replace_later_generations(self) -> None:
        for invocation in ("creator", "ambiguous", "other"):
            result = self.run_helper(mode="new", invocation_id=invocation)
            self.assertEqual(result.returncode, 0, result.stderr)
        (self.home / "tmux-session").unlink()
        replacement = self.run_helper(mode="new", invocation_id="replacement")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        before = self.marker().read_bytes()
        for action in ("prepare", "attach"):
            result = self.run_helper(mode="new", invocation_id="ambiguous", action=action)
            self.assertEqual(result.returncode, 12, result.stderr)
            self.assertEqual(self.marker().read_bytes(), before)
        (self.home / "tmux-session").unlink()
        result = self.run_helper(mode="new", invocation_id="ambiguous")
        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertEqual(self.marker().read_bytes(), before)

    def test_attach_requires_a_prepared_invocation(self) -> None:
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        before = self.marker().read_bytes()

        result = self.run_helper(invocation_id="unprepared", action="attach")

        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn("prepare this invocation", result.stderr)
        self.assertFalse((self.home / "attached-pane").exists())
        self.assertEqual(self.marker().read_bytes(), before)

    def test_malformed_continuity_state_is_not_reinitialized(self) -> None:
        marker = self.marker()
        marker.parent.mkdir(parents=True)
        for contents in (
            "",
            "legacy-invocation legacy-identity\n",
            "[]",
            '{"identity":"first","invocations":{}}',
            '{"identity":null,"invocations":{"caller":"unknown-generation"}}',
        ):
            with self.subTest(contents=contents):
                marker.write_text(contents, encoding="utf-8")
                result = self.run_helper(mode="new")
                self.assertEqual(result.returncode, 13, result.stderr)
                self.assertFalse((self.home / "tmux-session").exists())
                self.assertEqual(marker.read_text(encoding="utf-8"), contents)
                self.assertNotIn("Traceback", result.stderr)

    def test_malformed_origin_is_rejected_before_attachment(self) -> None:
        prepared = self.run_helper()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        origin = self.home / "tmux-origin"
        log = self.home / "tmux.log"
        for value in ("%0;invalid 1234", "%0 0", "%0 1234,invalid", "not-a-pane 1234"):
            with self.subTest(origin=value):
                origin.write_text(value + "\n", encoding="utf-8")
                log.unlink(missing_ok=True)

                result = self.run_helper(action="attach")

                self.assertEqual(result.returncode, 12, result.stderr)
                self.assertIn("original Copilot process", result.stderr)
                self.assertNotIn("if-shell", log.read_text(encoding="utf-8"))
                self.assertFalse((self.home / "attached-pane").exists())

    def test_pane_loss_during_attachment_does_not_attach_to_the_session(self) -> None:
        prepared = self.run_helper()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        marker = self.marker()
        before = marker.read_bytes()
        self.env["FAKE_TMUX_LOSE_ON_ATTACH"] = "1"

        result = self.run_helper(action="attach")

        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("original Copilot pane", result.stderr)
        self.assertTrue((self.home / "tmux-session").exists())
        self.assertFalse((self.home / "attached-pane").exists())
        self.assertEqual(marker.read_bytes(), before)

    def test_real_tmux_attaches_only_to_the_original_live_process(self) -> None:
        self.use_real_tmux()
        for mutation in ("", "removed", "dead", "respawned"):
            with self.subTest(mutation=mutation):
                session = mutation or "live"
                prepared = self.run_helper(session=session, invocation_id="first")
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                pane, pid = self.tmux_output(
                    "display-message", "-p", "-t", f"={session}:", "#{pane_id} #{pane_pid}"
                ).split()
                other_pane = self.tmux_output(
                    "new-window", "-t", f"={session}:", "-P", "-F", "#{pane_id}", "exec sleep 60"
                )
                marker = self.marker(session)
                before = marker.read_bytes()

                status, attached_pane, stderr = self.run_control_attach(session, pane, pid, mutation)

                if mutation:
                    self.assertEqual(status, 12, stderr)
                    self.assertEqual(attached_pane, "")
                    self.assertIn("original Copilot pane", stderr)
                else:
                    self.assertEqual(status, 0, stderr)
                    self.assertEqual(attached_pane, pane)
                    self.assertEqual(
                        self.tmux_output("display-message", "-p", "-t", pane, "#{pane_pid}"), pid
                    )
                self.assertEqual(marker.read_bytes(), before)
                self.assertEqual(
                    self.tmux_output("display-message", "-p", "-t", other_pane, "#{pane_dead}"), "0"
                )

    def test_workspace_default_comes_from_runtime_repository_metadata(self) -> None:
        self.env.pop("COPILOT2_REMOTE_CWD")
        repository_name = self.root.name + "-not-a-workspace"
        self.env["GITHUB_REPOSITORY"] = f"example/{repository_name}"

        result = self.run_helper()

        self.assertEqual(result.returncode, 11)
        self.assertIn(f"missing workspace: /workspaces/{repository_name}", result.stderr)
        self.assertFalse((self.home / "tmux.log").exists())

    def test_workspace_without_runtime_metadata_requires_an_override(self) -> None:
        self.env.pop("COPILOT2_REMOTE_CWD")
        self.env.pop("GITHUB_REPOSITORY", None)

        result = self.run_helper()

        self.assertEqual(result.returncode, 11)
        self.assertIn("set COPILOT2_REMOTE_CWD", result.stderr)
        self.assertFalse((self.home / "tmux.log").exists())

    def test_normal_exit_retires_identity_without_forgetting_invocations(self) -> None:
        prepared = self.run_helper(action="prepare")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.env["FAKE_TMUX_END_SESSION"] = "1"

        result = self.run_helper(action="attach")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.read_state(),
            {"identity": None, "invocations": {"test-invocation": "test-invocation"}},
        )
        replay = self.run_helper(mode="new")
        self.assertEqual(replay.returncode, 12, replay.stderr)
        fresh = self.run_helper(invocation_id="fresh")
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(
            self.read_state(),
            {"identity": "fresh", "invocations": {"test-invocation": "test-invocation", "fresh": "fresh"}},
        )

    def test_rejects_unsafe_session_name(self) -> None:
        for session in ("bad session", "review.with.period", ".review", "review.", "."):
            with self.subTest(session=session):
                result = self.run_helper(session)

                self.assertEqual(result.returncode, 11, result.stderr)
                self.assertIn("invalid tmux session name", result.stderr)
                self.assertFalse((self.home / "tmux.log").exists())
                self.assertFalse(self.marker().parent.exists())

    def test_real_tmux_rejects_dotted_session_names_before_creation(self) -> None:
        self.use_real_tmux()
        self.tmux_output("new-session", "-d", "-s", "anchor", "exec sleep 60")

        result = self.run_helper(session="review.with.period", invocation_id="boundary-check")

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("invalid tmux session name", result.stderr)
        self.assertEqual(self.tmux_output("list-sessions", "-F", "#{session_name}"), "anchor")
        self.assertFalse(self.marker().parent.exists())

    def test_real_tmux_preserves_supported_session_name_on_resume(self) -> None:
        self.use_real_tmux()
        session = "Review_2-safe"
        prepared = self.run_helper(session=session)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(self.tmux_output("list-sessions", "-F", "#{session_name}"), session)
        original = self.tmux_output(
            "display-message", "-p", "-t", f"={session}:", "#{pane_id} #{pane_pid}"
        )

        resumed = self.run_helper(session=session, mode="resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            self.tmux_output("display-message", "-p", "-t", f"={session}:", "#{pane_id} #{pane_pid}"),
            original,
        )

    def test_new_mode_replaces_a_stale_marker(self) -> None:
        self.seed_state("previous-boot")

        result = self.run_helper(mode="new")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.read_state(),
            {
                "identity": "test-invocation",
                "invocations": {"previous-boot": "previous-boot", "test-invocation": "test-invocation"},
            },
        )

    def test_new_mode_does_not_replace_a_session_lost_during_this_invocation(
        self,
    ) -> None:
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        refreshed = self.run_helper(mode="new", invocation_id="second")
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        (self.home / "tmux-session").unlink()
        (self.home / "tmux-identity").unlink()
        (self.home / "tmux.log").unlink()

        result = self.run_helper(mode="new", invocation_id="second")

        self.assertEqual(result.returncode, 12)
        self.assertIn("did not survive", result.stderr)
        self.assertFalse((self.home / "tmux-session").exists())
        self.assertNotIn("new-session", (self.home / "tmux.log").read_text(encoding="utf-8"))
        self.assertEqual(
            self.read_state(),
            {"identity": "first", "invocations": {"first": "first", "second": "first"}},
        )

    def test_prepare_refreshes_marker_for_an_existing_session(self) -> None:
        prepared = self.run_helper(invocation_id="first")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)

        refreshed = self.run_helper(invocation_id="second")

        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertEqual(
            self.read_state(),
            {"identity": "first", "invocations": {"first": "first", "second": "first"}},
        )
        self.assertEqual(
            (self.home / "tmux-identity").read_text(encoding="utf-8").strip(), "first"
        )
        attached = self.run_helper(mode="resume", invocation_id="second", action="attach")
        self.assertEqual(attached.returncode, 0, attached.stderr)
        self.assertTrue((self.home / "tmux-session").exists())
        self.assertEqual((self.home / "attached-pane").read_text().strip(), "%0")

    def test_remaps_remote_status_255(self) -> None:
        prepared = self.run_helper(action="prepare")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.env["FAKE_TMUX_STATUS"] = "255"

        result = self.run_helper(action="attach")

        self.assertEqual(result.returncode, 13)


if __name__ == "__main__":
    unittest.main(verbosity=2)
