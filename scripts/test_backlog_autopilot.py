"""Behavioral contracts for the local backlog worker."""

import base64
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("backlog_autopilot.py")
SPEC = importlib.util.spec_from_file_location("backlog_autopilot", MODULE_PATH)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def issue(number=1, title="[Sentry] Example", **fields):
    return {
        "id": number + 100,
        "number": number,
        "title": title,
        "state": "open",
        "comments": 0,
        **fields,
    }


def envelope(value):
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    return f"api_response:\n  body: {encoded}\n  truncated: false\n"


class FakeTailscale:
    def __init__(self):
        self.settings = {
            "executable": "/fixture/tailscale",
            "work_profile": "work",
            "restore_profile": "personal",
        }
        self.selected = "personal"
        self.ids = ["work", "personal", "third"]
        self.events = []
        self.fail_before = set()
        self.fail_after = set()
        self.fail_counts = {}
        self.backend = "Running"
        self.personal_backend = "Running"
        self.connected_profile = None
        self.personal_statuses = []

    def command(self, argv, **kwargs):
        assert argv[0] == self.settings["executable"], argv
        args = argv[1:]
        if args == ["switch", "--list", "--json"]:
            return json.dumps(
                [
                    {
                        "id": profile,
                        "selected": profile == self.selected,
                        "tailnet": profile + ".example.test",
                    }
                    for profile in self.ids
                ]
            )
        if args == ["status", "--json"]:
            if self.selected == "personal" and self.personal_statuses:
                self.personal_backend, self.connected_profile = (
                    self.personal_statuses.pop(0)
                )
            return json.dumps(
                {
                    "BackendState": (
                        self.backend
                        if self.selected == "work"
                        else self.personal_backend
                    ),
                    "CurrentTailnet": {
                        "Name": (self.connected_profile or self.selected)
                        + ".example.test"
                    },
                }
            )
        if len(args) == 2 and args[0] == "switch" and args[1] in self.ids:
            profile = args[1]
            self.events.append(profile)
            if self.fail_counts.get(profile, 0):
                self.fail_counts[profile] -= 1
                raise worker.InfrastructureError("Temporary switch failure")
            if profile in self.fail_before:
                raise worker.InfrastructureError("Simulated switch failure")
            self.selected = profile
            if profile in self.fail_after:
                raise worker.InfrastructureError("Simulated failure after switching")
            return "Success"
        raise AssertionError(argv)


class ScheduleTests(unittest.TestCase):
    def test_daily_boundary_and_host_timezone_independence(self):
        before = datetime.fromisoformat("2026-11-07T10:59:59+00:00")
        after = datetime.fromisoformat("2026-11-07T11:00:00+00:00")
        self.assertEqual(
            worker.latest_slot(before).isoformat(), "2026-11-06T03:00:00-08:00"
        )
        self.assertEqual(
            worker.latest_slot(after).isoformat(), "2026-11-07T03:00:00-08:00"
        )

    def test_spring_offset_changes_without_changing_wall_time(self):
        instant = datetime.fromisoformat("2026-03-14T01:00:00+00:00")
        self.assertEqual(
            worker.latest_slot(instant).isoformat(), "2026-03-13T03:00:00-07:00"
        )

    def test_weekday_and_weekend_slots_advance_daily(self):
        for instant, expected in [
            ("2026-11-08T11:00:00+00:00", "2026-11-08T03:00:00-08:00"),
            ("2026-11-09T10:59:59+00:00", "2026-11-08T03:00:00-08:00"),
            ("2026-03-08T09:59:59+00:00", "2026-03-07T03:00:00-08:00"),
            ("2026-03-08T10:00:00+00:00", "2026-03-08T03:00:00-07:00"),
            ("2026-11-01T10:59:59+00:00", "2026-10-31T03:00:00-07:00"),
            ("2026-11-01T11:00:00+00:00", "2026-11-01T03:00:00-08:00"),
        ]:
            with self.subTest(instant=instant):
                self.assertEqual(
                    worker.latest_slot(datetime.fromisoformat(instant)).isoformat(),
                    expected,
                )


class ApiTests(unittest.TestCase):
    def test_real_axi_base64_envelope_preserves_large_sensitive_strings(self):
        data = [issue(title='[Sentry] "quotes"\n' + "x" * 5000)]
        self.assertEqual(worker.decode_api(envelope(data)), data)
        self.assertEqual(worker.decode_api(envelope([])), [])

    def test_quoted_base64_is_supported(self):
        raw = envelope({"state": "closed"})
        encoded = raw.split("body: ")[1].splitlines()[0]
        self.assertEqual(
            worker.decode_api(raw.replace(encoded, json.dumps(encoded))),
            {"state": "closed"},
        )

    def test_truncation_or_unknown_shape_fails_closed(self):
        for raw in [
            envelope([]).replace("false", "true"),
            "[]",
            "api_response:\n  body: ????\n  truncated: false\n",
        ]:
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                worker.decode_api(raw)

    def test_pagination_deduplicates_and_reads_empty_final_page(self):
        with patch.object(
            worker, "api", side_effect=[[issue(1), issue(2)], [issue(2), issue(3)], []]
        ) as api:
            rows = worker.pages("/repos/example/tracker/issues?state=open")
        self.assertEqual([row["number"] for row in rows], [1, 2, 3])
        self.assertIn("page=3", api.call_args.args[0])

    def test_repeated_page_cannot_loop_forever(self):
        with patch.object(worker, "api", side_effect=[[issue()], [issue()]]):
            with self.assertRaisesRegex(ValueError, "no progress"):
                worker.pages("/issues")

    def test_intermediate_failure_is_not_an_empty_snapshot(self):
        with patch.object(
            worker, "api", side_effect=[[issue()], RuntimeError("rate limit")]
        ):
            with self.assertRaisesRegex(RuntimeError, "rate limit"):
                worker.pages("/issues")

    def test_title_match_is_literal_and_case_sensitive(self):
        self.assertTrue(worker.eligible(issue(title="Prefix [Sentry] problem")))
        for data in [
            issue(title="Sentry problem"),
            issue(title="[sentry] problem"),
            issue(state="closed"),
            issue(pull_request={}),
        ]:
            self.assertFalse(worker.eligible(data))


class ReliabilityTests(unittest.TestCase):
    def test_api_uses_cfs_and_retries_only_a_bounded_number_of_times(self):
        with patch.object(
            worker,
            "command",
            side_effect=[worker.InfrastructureError("offline"), envelope({"ok": True})],
        ) as command, patch.object(worker.time, "sleep"):
            self.assertEqual(worker.api("/test"), {"ok": True})
        self.assertEqual(command.call_count, 2)
        self.assertEqual(
            command.call_args.args[0][:6], ["gh", "cfs", "--", "npx", "-y", "gh-axi"]
        )
        with patch.object(
            worker, "command", side_effect=worker.InfrastructureError("offline")
        ) as command, patch.object(worker.time, "sleep"):
            with self.assertRaises(worker.InfrastructureError):
                worker.api("/test")
        self.assertEqual(command.call_count, 3)

    def test_real_command_failure_records_bounded_diagnostics_without_common_credentials(
        self,
    ):
        script = "import sys; sys.stderr.write('x'*3000 + ' token=private-value Bearer secret-value ghp_fakecredential'); sys.exit(7)"
        with self.assertRaises(worker.InfrastructureError) as raised:
            worker.command([sys.executable, "-c", script])
        text = str(raised.exception)
        self.assertIn("exited 7", text)
        self.assertIn("[REDACTED]", text)
        self.assertLess(len(text), 2100)
        for secret in ("private-value", "secret-value", "ghp_fakecredential"):
            self.assertNotIn(secret, text)

    def test_command_error_keeps_primary_stdout_cause_and_redacts_both_streams(self):
        stdout = (
            'error: feed access denied; "token": "private value"\ncode: AUTH_FAILED\n'
        )
        stderr = "cfs: command failed; Authorization: Bearer bearer-secret\n"
        script = (
            "import sys; sys.stdout.write("
            + repr(stdout)
            + "); sys.stderr.write("
            + repr(stderr)
            + "); sys.exit(1)"
        )
        with self.assertRaises(worker.InfrastructureError) as raised:
            worker.command([sys.executable, "-c", script])
        message = str(raised.exception)
        self.assertIn("feed access denied", message)
        self.assertIn("AUTH_FAILED", message)
        self.assertIn("cfs: command failed", message)
        self.assertNotIn("private value", message)
        self.assertNotIn("bearer-secret", message)
        self.assertIn("[REDACTED]", message)

    def test_real_command_timeout_has_explicit_infrastructure_outcome(self):
        with self.assertRaisesRegex(worker.InfrastructureError, "could not complete"):
            worker.command(
                [sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.01
            )

    def test_object_check_run_pagination_includes_final_page(self):
        with patch.object(
            worker, "api", side_effect=[{"check_runs": [{"id": 1}]}, {"check_runs": []}]
        ) as api:
            self.assertEqual(
                worker.pages("/checks?filter=latest", key="check_runs"), [{"id": 1}]
            )
        self.assertIn("page=2", api.call_args.args[0])

    def test_ci_requires_completed_checks_and_latest_successful_statuses(self):
        passed = {"id": 1, "status": "completed", "conclusion": "success"}
        cases = [
            ([passed], [], True),
            ([], [], False),
            ([{**passed, "status": "in_progress", "conclusion": None}], [], False),
            ([{**passed, "conclusion": "failure"}], [], False),
            ([passed], [{"id": 2, "context": "build", "state": "failure"}], False),
            (
                [],
                [
                    {"id": 3, "context": "build", "state": "success"},
                    {"id": 2, "context": "build", "state": "failure"},
                ],
                True,
            ),
        ]
        for checks, statuses, expected in cases:
            with self.subTest(checks=checks, statuses=statuses), patch.object(
                worker, "pages", side_effect=[checks, statuses]
            ):
                self.assertEqual(worker.ci_green("example/app", "sha"), expected)

    def test_preflight_saves_dns_warnings_and_capacity_without_exposing_environment(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker.atomic_json(
                root / "mcp.json",
                {
                    "mcpServers": {
                        "Splunk": {
                            "env": {
                                "SPLUNK_HOST": "logs.example.test",
                                "TOKEN": "private-value",
                            }
                        },
                        "Sentry": {"url": "https://sentry.example.test/mcp"},
                    }
                },
            )

            def command(argv, **kwargs):
                if argv[0] == "git":
                    return "https://github.com/example/tracker.git"
                if argv[0] == "treehouse":
                    return json.dumps([{"status": "leased"}])
                if argv[-1] == "logs.example.test":
                    raise worker.InfrastructureError("DNS failed")
                return ""

            config = {
                "repository": "example/tracker",
                "checkout": str(root),
                "agent_dir": str(root),
            }
            with patch.object(
                worker.shutil, "which", return_value="/tool"
            ), patch.object(worker, "api", return_value={}), patch.object(
                worker, "command", side_effect=command
            ), patch.object(
                worker, "treehouse_limit", return_value=24
            ):
                health = worker.preflight(config)
            self.assertEqual(health["worktree_pools"]["example/tracker"]["idle"], 0)
            self.assertEqual(
                health["worktree_pools"]["example/tracker"]["state"], "can_create"
            )
            self.assertEqual(health["telemetry"]["Splunk"]["dns"], "unavailable")
            self.assertEqual(health["telemetry"]["Sentry"]["dns"], "available")
            self.assertEqual(len(health["warnings"]), 1)
            self.assertNotIn("private-value", json.dumps(health))

    def test_preflight_warns_only_for_the_exhausted_repository(self):
        config = {
            "repository": "example/tracker",
            "checkout": "/tracker",
            "agent_dir": "/unconfigured-agent",
        }
        pools = {
            "example/tracker": {
                "state": "can_create",
                "idle": 0,
                "allocated": 2,
                "limit": 24,
            },
            "example/app": {
                "state": "exhausted",
                "idle": 0,
                "allocated": 24,
                "limit": 24,
            },
        }
        with patch.object(worker.shutil, "which", return_value="/tool"), patch.object(
            worker, "api", return_value={}
        ), patch.object(worker, "worktree_pools", return_value=pools):
            health = worker.preflight(config)
        capacity_warnings = [
            warning for warning in health["warnings"] if warning.startswith("Treehouse")
        ]
        self.assertEqual(len(capacity_warnings), 1)
        self.assertIn("example/app", capacity_warnings[0])
        self.assertNotIn("example/tracker", capacity_warnings[0])

    def test_preflight_missing_tool_prevents_github_and_model_work(self):
        with patch.object(worker.shutil, "which", return_value=None), patch.object(
            worker, "api"
        ) as api:
            with self.assertRaisesRegex(worker.InfrastructureError, "executable"):
                worker.preflight({})
        api.assert_not_called()

    def test_progress_records_heartbeat_and_detects_two_hours_without_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
            output = root / "output.log"
            output.write_text("started")
            os.utime(output, (started.timestamp(), started.timestamp()))
            with patch.object(
                worker,
                "now",
                return_value=datetime.fromisoformat("2026-11-07T04:00:00+00:00"),
            ):
                self.assertTrue(
                    worker.progress({"state_dir": str(root)}, root, 123, started)
                )
                output.write_text("new progress")
                self.assertFalse(
                    worker.progress({"state_dir": str(root)}, root, 123, started)
                )
            saved = worker.read_json(root / "progress.json")
            self.assertEqual(saved["child_pid"], 123)
            self.assertEqual(saved["status"], "running")
            self.assertEqual(saved["heartbeat_at"], "2026-11-07T04:00:00+00:00")


class TailscaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vpn = FakeTailscale()
        self.config = {"state_dir": str(self.root), "tailscale": self.vpn.settings}
        self.journal = self.root / "tailscale-session.json"

    def test_background_cli_forces_cli_mode_without_changing_parent_environment(self):
        with patch.dict(os.environ, {"PATH": "/fixture"}, clear=True), patch.object(
            worker, "command", side_effect=self.vpn.command
        ) as command:
            self.assertEqual(
                worker.active_tailscale_profile(self.vpn.settings), "personal"
            )
            env = command.call_args.kwargs["env"]
            self.assertEqual(env["TAILSCALE_BE_CLI"], "1")
            self.assertEqual(env["PATH"], "/fixture")
            self.assertNotIn("TAILSCALE_BE_CLI", os.environ)

    def test_selects_work_before_body_and_restores_personal_after_success_or_failure(
        self,
    ):
        for already_work in (False, True):
            for fail in (False, True):
                with self.subTest(already_work=already_work, fail=fail):
                    self.vpn.selected = "work" if already_work else "personal"
                    self.vpn.events.clear()
                    with patch.object(worker, "command", side_effect=self.vpn.command):
                        try:
                            with worker.tailscale_session(self.config):
                                self.assertEqual(self.vpn.selected, "work")
                                self.assertTrue(self.journal.exists())
                                if fail:
                                    raise ValueError("Work failed")
                        except ValueError as error:
                            self.assertTrue(fail)
                            self.assertEqual(str(error), "Work failed")
                    self.assertEqual(self.vpn.selected, "personal")
                    self.assertFalse(self.journal.exists())
                    self.assertEqual(
                        self.vpn.events,
                        ["personal"] if already_work else ["work", "personal"],
                    )

    def test_missing_profile_aborts_before_switching_or_recording_ownership(self):
        self.vpn.ids.remove("work")
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), self.assertRaises(worker.InfrastructureError):
            with worker.tailscale_session(self.config):
                self.fail("Work ran without the configured profile")
        self.assertEqual(self.vpn.events, [])
        self.assertFalse(self.journal.exists())

    def test_failed_or_disconnected_work_selection_restores_personal(self):
        self.vpn.fail_after.add("work")
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), self.assertRaises(worker.InfrastructureError):
            with worker.tailscale_session(self.config):
                self.fail("Work ran after a switch failure")
        self.assertEqual(self.vpn.selected, "personal")
        self.assertFalse(self.journal.exists())
        self.vpn.fail_after.clear()
        self.vpn.backend = "NeedsLogin"
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), self.assertRaisesRegex(worker.InfrastructureError, "not connected"):
            with worker.tailscale_session(self.config):
                self.fail("Work ran without connectivity")
        self.assertEqual(self.vpn.selected, "personal")

    def test_restore_failure_retains_receipt_for_recovery(self):
        self.vpn.fail_before.add("personal")
        with patch.object(worker, "command", side_effect=self.vpn.command):
            with self.assertRaises(worker.InfrastructureError):
                with worker.tailscale_session(self.config):
                    pass
            self.assertEqual(self.vpn.selected, "work")
            self.assertEqual(worker.read_json(self.journal)["phase"], "restoring")
            self.vpn.fail_before.clear()
            self.assertTrue(worker.restore_tailscale(self.config))
        self.assertEqual(self.vpn.selected, "personal")
        self.assertFalse(self.journal.exists())

    def test_restore_retries_a_transient_switch_failure(self):
        self.vpn.selected = "work"
        self.vpn.fail_counts["personal"] = 1
        worker.atomic_json(self.journal, self.vpn.settings)
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), patch.object(worker.time, "sleep"):
            self.assertTrue(worker.restore_tailscale(self.config))
        self.assertEqual(self.vpn.selected, "personal")
        self.assertEqual(self.vpn.events, ["personal", "personal"])
        self.assertFalse(self.journal.exists())

    def test_restore_waits_for_the_personal_backend_without_repeating_the_switch(self):
        self.vpn.selected = "work"
        self.vpn.connected_profile = "work"
        self.vpn.personal_statuses = [
            ("Running", "work"),
            ("Starting", "personal"),
            ("Running", "personal"),
        ]
        worker.atomic_json(self.journal, self.vpn.settings)
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), patch.object(worker.time, "sleep"):
            self.assertTrue(worker.restore_tailscale(self.config))
        self.assertEqual(self.vpn.connected_profile, "personal")
        self.assertEqual(self.vpn.personal_backend, "Running")
        self.assertEqual(self.vpn.events, ["personal"])
        self.assertFalse(self.journal.exists())

    def test_restore_preserves_recovery_when_personal_is_disconnected_or_wrong_tailnet(
        self,
    ):
        for backend, connected in [("NeedsLogin", "personal"), ("Running", "work")]:
            with self.subTest(backend=backend, connected=connected):
                self.vpn.selected = "personal"
                self.vpn.personal_backend = backend
                self.vpn.connected_profile = connected
                worker.atomic_json(self.journal, self.vpn.settings)
                with patch.object(
                    worker, "command", side_effect=self.vpn.command
                ), patch.object(worker.time, "sleep"), self.assertRaises(
                    worker.InfrastructureError
                ):
                    worker.restore_tailscale(self.config)
                self.assertTrue(self.journal.exists())
                self.assertEqual(self.vpn.selected, "personal")

    def test_restore_accepts_verified_success_after_an_ambiguous_switch_error(self):
        self.vpn.selected = "work"
        self.vpn.fail_after.add("personal")
        worker.atomic_json(self.journal, self.vpn.settings)
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), patch.object(worker.time, "sleep"):
            self.assertTrue(worker.restore_tailscale(self.config))
        self.assertEqual(self.vpn.selected, "personal")
        self.assertEqual(self.vpn.events, ["personal"])
        self.assertFalse(self.journal.exists())

    def test_restore_does_not_override_a_manual_change_between_retries(self):
        self.vpn.selected = "work"
        worker.atomic_json(self.journal, self.vpn.settings)

        def command(argv, **kwargs):
            if argv[1:] == ["switch", "personal"]:
                self.vpn.events.append("personal")
                self.vpn.selected = "third"
                raise worker.InfrastructureError("Switch interrupted")
            return self.vpn.command(argv, **kwargs)

        with patch.object(worker, "command", side_effect=command), patch.object(
            worker.time, "sleep"
        ), self.assertRaisesRegex(worker.InfrastructureError, "changed manually"):
            worker.restore_tailscale(self.config)
        self.assertEqual(self.vpn.selected, "third")
        self.assertEqual(self.vpn.events, ["personal"])
        self.assertTrue(self.journal.exists())

    def test_manual_third_profile_is_not_overridden_on_exit(self):
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), self.assertRaisesRegex(worker.InfrastructureError, "changed manually"):
            with worker.tailscale_session(self.config):
                self.vpn.selected = "third"
        self.assertEqual(self.vpn.selected, "third")
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.vpn.events, ["work"])

    def test_real_sigterm_stops_child_before_restoring_profile(self):
        vpn_state = self.root / "vpn.json"
        worker.atomic_json(vpn_state, {"selected": "personal", "switches": []})
        executable = self.root / "tailscale"
        executable.write_text("#!" + sys.executable + "\n" + f"""
import json,os,pathlib,sys
if not os.environ.get("TERM") and os.environ.get("TAILSCALE_BE_CLI")!="1": sys.exit(0)
path=pathlib.Path({str(vpn_state)!r});state=json.loads(path.read_text());args=sys.argv[1:]
if args==["switch","--list","--json"]:
 print(json.dumps([{{"id":p,"selected":p==state["selected"],"tailnet":p+".example.test"}} for p in ("work","personal")]))
elif args==["status","--json"]: print(json.dumps({{"BackendState":"Running","CurrentTailnet":{{"Name":state["selected"]+".example.test"}}}}))
elif len(args)==2 and args[0]=="switch":
 state["selected"]=args[1];state["switches"].append(args[1]);path.write_text(json.dumps(state))
else: sys.exit(9)
""")
        executable.chmod(0o700)
        started = self.root / "child.pid"
        fake_pi = self.root / "sleeping-pi"
        fake_pi.write_text(
            "#!"
            + sys.executable
            + "\nimport os,pathlib,time\n"
            + f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n"
        )
        fake_pi.chmod(0o700)
        harness = f"""
import importlib.util,pathlib,sys
spec=importlib.util.spec_from_file_location("worker",{str(MODULE_PATH)!r});w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
root=pathlib.Path({str(self.root)!r})
config={{"state_dir":str(root),"pi":{str(fake_pi)!r},"agent_dir":str(root),"model":"fixture","tailscale":{{"executable":{str(executable)!r},"work_profile":"work","restore_profile":"personal"}}}}
try:
 with w.exclusive_lock(root) as fd,w.worker_signals(),w.tailscale_session(config):
  w.run_pi(config,root/"stage",root,"fixture",fd,pathlib.Path({str(MODULE_PATH.with_name('backlog_autopilot_policy.md'))!r}))
except w.WorkerInterrupted as error: sys.exit(128+error.signum)
"""
        background_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TERM", "TERM_PROGRAM", "TAILSCALE_BE_CLI"}
        }
        process = subprocess.Popen(
            [sys.executable, "-c", harness],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=background_env,
        )
        try:
            for _ in range(100):
                if started.exists() or process.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(started.exists(), "Fixture child did not start")
            child_pid = int(started.read_text())
            self.assertEqual(worker.read_json(vpn_state)["selected"], "work")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
            self.assertEqual(
                worker.read_json(vpn_state),
                {"selected": "personal", "switches": ["work", "personal"]},
            )
            self.assertFalse(self.journal.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            if started.exists():
                pid = int(started.read_text())
                check = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                )
                if str(fake_pi) in check.stdout:
                    os.kill(pid, signal.SIGTERM)

    def test_crash_recovery_does_not_accept_changed_profile_configuration(self):
        worker.atomic_json(
            self.journal, {"work_profile": "other-work", "restore_profile": "personal"}
        )
        with patch.object(
            worker, "command", side_effect=self.vpn.command
        ), self.assertRaisesRegex(worker.InfrastructureError, "does not match"):
            worker.restore_tailscale(self.config)
        self.assertEqual(self.vpn.events, [])
        self.assertTrue(self.journal.exists())


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tracker = self.root / "tracker"
        self.app = self.root / "app"
        self.tracker.mkdir()
        self.app.mkdir()
        self.config = {
            "repository": "example/tracker",
            "checkout": str(self.tracker),
            "allowed_repositories": ["example/tracker", "example/app"],
            "implementation_checkouts": {"example/app": str(self.app)},
        }
        settings = self.root / ".config/treehouse/config.toml"
        settings.parent.mkdir(parents=True)
        settings.write_text("max_trees = 24\n")

    def command(self, argv, cwd=None, **kwargs):
        if argv[0] == "git":
            return f"https://github.com/example/{Path(cwd).name}.git\n"
        return json.dumps(
            [{"status": "leased"}] * (2 if Path(cwd) == self.tracker else 17)
            + ([] if Path(cwd) == self.tracker else [{"status": "available"}])
        )

    @unittest.skipIf(
        sys.version_info < (3, 11), "Configured limits require the stdlib TOML parser"
    )
    def test_tracking_pool_without_idle_slots_does_not_hide_implementation_capacity(
        self,
    ):
        with patch.object(worker.Path, "home", return_value=self.root), patch.object(
            worker, "command", side_effect=self.command
        ):
            pools = worker.worktree_pools(self.config)
        self.assertEqual(pools["example/tracker"]["state"], "can_create")
        self.assertEqual(pools["example/tracker"]["limit"], 24)
        self.assertEqual(pools["example/app"]["state"], "available")
        self.assertEqual(pools["example/app"]["idle"], 1)

    @unittest.skipIf(
        sys.version_info < (3, 11), "Configured limits require the stdlib TOML parser"
    )
    def test_repo_override_and_real_exhaustion_are_distinct_from_no_idle_slots(self):
        (self.tracker / "treehouse.toml").write_text("max_trees = 2\n")
        with patch.object(worker.Path, "home", return_value=self.root), patch.object(
            worker, "command", side_effect=self.command
        ):
            pools = worker.worktree_pools(self.config)
        self.assertEqual(pools["example/tracker"]["state"], "exhausted")
        self.assertEqual(pools["example/app"]["limit"], 24)

    def test_unknown_limit_does_not_claim_pool_exhaustion(self):
        with patch.object(worker, "tomllib", None), patch.object(
            worker, "command", side_effect=self.command
        ):
            pools = worker.worktree_pools(self.config)
        self.assertEqual(pools["example/tracker"]["state"], "unknown")
        self.assertIsNone(pools["example/tracker"]["limit"])
        self.assertEqual(pools["example/app"]["state"], "available")

    def test_unmapped_or_wrong_repository_is_unknown_not_full(self):
        config = {**self.config, "implementation_checkouts": {}}
        with patch.object(worker, "command", side_effect=self.command):
            pools = worker.worktree_pools(config)
        self.assertEqual(pools["example/app"]["state"], "unknown")
        self.assertIn("not configured", pools["example/app"]["detail"])
        with patch.object(
            worker, "command", return_value="https://github.com/other/repo.git"
        ):
            pools = worker.worktree_pools(self.config)
        self.assertEqual(pools["example/app"]["state"], "unknown")
        self.assertIn("does not match", pools["example/app"]["detail"])


class PrCertificationTests(unittest.TestCase):
    def verify(
        self,
        *,
        remote_sha="certified-sha",
        local_remote="git@github.com:example/app.git",
        dirty="",
        green=True,
        second_sha=None,
    ):
        pr = {
            "state": "open",
            "head": {"sha": remote_sha},
            "assignees": [{"login": "zkoppert"}],
            "requested_reviewers": [{"login": "copilot-pull-request-reviewer[bot]"}],
        }
        receipt = {
            "outcome": "awaiting_review",
            "pr_urls": ["https://github.com/example/app/pull/1"],
            "worktrees": ["/worktree"],
        }
        outputs = [
            json.dumps([{"path": "/worktree", "status": "leased"}]),
            dirty,
            local_remote,
            "certified-sha",
            "passed",
        ]
        with patch.object(
            worker,
            "api",
            side_effect=[pr, {**pr, "head": {"sha": second_sha or remote_sha}}],
        ), patch.object(worker, "pages", return_value=[]), patch.object(
            worker, "command", side_effect=outputs
        ), patch.object(
            worker, "ci_green", return_value=green
        ):
            worker.verify_pull_requests(
                {"allowed_repositories": ["example/app"]}, receipt
            )
        return receipt

    def test_certified_remote_commit_and_green_ci_allow_review(self):
        self.assertEqual(self.verify()["outcome"], "awaiting_review")
        self.assertEqual(
            self.verify(local_remote="https://github.com/example/app.git")["outcome"],
            "awaiting_review",
        )

    def test_pending_or_failed_ci_downgrades_review_handoff(self):
        result = self.verify(green=False)
        self.assertEqual(result["outcome"], "awaiting_ci")
        self.assertIn("GitHub checks", result["next_action"])

    def test_remote_commit_repository_and_dirty_tree_cannot_borrow_certification(self):
        for options, message in [
            ({"remote_sha": "uncertified-sha"}, "does not match"),
            ({"local_remote": "git@github.com:other/repo.git"}, "does not match"),
            ({"dirty": " M source.py"}, "uncommitted"),
            ({"second_sha": "new-remote-sha"}, "changed during"),
        ]:
            with self.subTest(options=options), self.assertRaisesRegex(
                ValueError, message
            ):
                self.verify(**options)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.batch = [{"number": 1, "sha256": "a"}, {"number": 2, "sha256": "b"}]
        self.receipt = {
            "snapshot_sha256": "snapshot",
            "notes": [
                {
                    "number": 1,
                    "sha256": "a",
                    "note": "An existing fix needs deployment.",
                },
                {
                    "number": 2,
                    "sha256": "b",
                    "note": "The first issue shares this dependency.",
                },
            ],
        }

    def test_analysis_requires_exact_coverage_and_content_hashes(self):
        worker.validate_analysis(self.receipt, self.batch, "snapshot")
        for change in [
            {"snapshot_sha256": "foreign"},
            {"notes": self.receipt["notes"][:1]},
            {"notes": [self.receipt["notes"][0]] * 2},
            {"notes": [{**row, "sha256": "wrong"} for row in self.receipt["notes"]]},
            {"notes": [{**row, "note": ""} for row in self.receipt["notes"]]},
        ]:
            with self.assertRaises(ValueError):
                worker.validate_analysis(
                    {**self.receipt, **change}, self.batch, "snapshot"
                )

    def test_draft_does_not_count_as_resolution(self):
        receipt = {
            "number": 1,
            "outcome": "resolved",
            "summary": "Fix proposed",
            "evidence": ["test.log"],
        }
        with self.assertRaisesRegex(ValueError, "confirmed GitHub closure"):
            worker.validate_outcome(receipt, 1, issue())
        receipt["outcome"] = "awaiting_review"
        with self.assertRaisesRegex(ValueError, "pull request URL"):
            worker.validate_outcome(receipt, 1, issue())
        receipt["pr_urls"] = ["https://github.com/example/app/pull/1"]
        worker.validate_outcome(receipt, 1, issue())

    def test_closed_issue_still_requires_evidence(self):
        receipt = {
            "number": 1,
            "outcome": "resolved",
            "summary": "No longer occurs",
            "evidence": [],
        }
        with self.assertRaises(ValueError):
            worker.validate_outcome(receipt, 1, issue(state="closed"))
        receipt["evidence"] = ["sentry-query.json"]
        worker.validate_outcome(receipt, 1, issue(state="closed"))


class NotificationTests(unittest.TestCase):
    def test_native_notification_passes_message_as_data_and_reports_delivery_failure(
        self,
    ):
        message = 'Draft PR ready: https://github.com/example/app/pull/1 "quoted"'
        with patch.object(worker.sys, "platform", "darwin"), patch.object(
            worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as send:
            self.assertTrue(worker.notify(message))
        self.assertEqual(send.call_args.args[0][-1], message)
        self.assertIn("item 1 of argv", send.call_args.args[0][-2])
        self.assertIn('with title "Sentry issue worker"', send.call_args.args[0][-2])
        with patch.object(worker.sys, "platform", "darwin"), patch.object(
            worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
        ), self.assertLogs(worker.LOG, level="ERROR"):
            self.assertFalse(worker.notify(message))

    def test_ready_notifications_persist_across_runs_and_ignore_other_outcomes(self):
        url = "https://github.com/example/app/pull/1"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = {
                "1": {"outcome": "awaiting_ci", "pr_urls": [url]},
                "2": {"outcome": "blocked", "pr_urls": [url]},
                "3": {"outcome": "resolved", "pr_urls": [url]},
            }
            with patch.object(worker, "notify", return_value=True) as send:
                worker.notify_ready_pull_requests(root, items)
                send.assert_not_called()
                items["1"]["outcome"] = "awaiting_review"
                worker.notify_ready_pull_requests(root, items)
                worker.notify_ready_pull_requests(root, {"99": items["1"]})
            self.assertEqual(
                [call.args[0] for call in send.call_args_list],
                [f"Draft PR ready for your review: {url}"],
            )
            self.assertEqual(
                set(worker.read_json(root / "pr-notifications.json")), {url}
            )

    def test_failed_notification_is_not_recorded_and_can_retry(self):
        urls = [f"https://github.com/example/app/pull/{n}" for n in (1, 2)]
        items = {"1": {"outcome": "awaiting_review", "pr_urls": urls}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(worker, "notify", side_effect=[True, False]):
                worker.notify_ready_pull_requests(root, items)
            self.assertEqual(
                set(worker.read_json(root / "pr-notifications.json")), {urls[0]}
            )
            with patch.object(worker, "notify", return_value=True) as send:
                worker.notify_ready_pull_requests(root, items)
            self.assertEqual(
                send.call_args.args[0], f"Draft PR ready for your review: {urls[1]}"
            )
            self.assertEqual(
                set(worker.read_json(root / "pr-notifications.json")), set(urls)
            )

    def test_completion_delivery_waits_for_current_personal_connectivity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "runs" / "completed"
            run_path = directory / "run.json"
            worker.atomic_json(
                run_path,
                {
                    "finished_at": "2026-11-07T02:00:00+00:00",
                    "completion_notification_pending": True,
                },
            )
            vpn = FakeTailscale()
            config = {"state_dir": str(root), "tailscale": vpn.settings}
            state = {"last_run": str(directory)}
            vpn.selected = "work"
            with patch.object(worker, "command", side_effect=vpn.command), patch.object(
                worker, "notify", return_value=False
            ) as send:
                self.assertFalse(worker.notify_completed_run(config, state))
                self.assertTrue(
                    worker.read_json(run_path)["completion_notification_pending"]
                )
                self.assertEqual(vpn.selected, "work")
                self.assertEqual(vpn.events, [])
                send.assert_not_called()
                vpn.selected = "personal"
                self.assertFalse(worker.notify_completed_run(config, state))
                self.assertTrue(
                    worker.read_json(run_path)["completion_notification_pending"]
                )
                send.return_value = True
                self.assertTrue(worker.notify_completed_run(config, state))
                self.assertIn("completion_notified_at", worker.read_json(run_path))
                self.assertNotIn(
                    "completion_notification_pending", worker.read_json(run_path)
                )
                self.assertFalse(worker.notify_completed_run(config, state))
                self.assertEqual(send.call_count, 2)

    def test_saved_ready_notification_requires_fresh_certification(self):
        items = {
            "1": {
                "outcome": "awaiting_review",
                "pr_urls": ["https://github.com/example/app/pull/1"],
            }
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            worker,
            "verify_pull_requests",
            side_effect=ValueError("Remote commit changed"),
        ), patch.object(worker, "notify") as send:
            worker.notify_ready_pull_requests(Path(temporary), items, config={})
        send.assert_not_called()

    def test_notification_exception_does_not_discard_work_or_mark_delivery(self):
        items = {
            "1": {
                "outcome": "awaiting_review",
                "pr_urls": ["https://github.com/example/app/pull/1"],
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                worker, "notify", side_effect=OSError("unavailable")
            ), self.assertLogs(worker.LOG, level="ERROR"):
                worker.notify_ready_pull_requests(root, items)
            self.assertFalse((root / "pr-notifications.json").exists())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config = {
            "repository": "example/tracker",
            "allowed_repositories": ["example/tracker", "example/app"],
            "checkout": str(self.root),
            "agent_dir": str(self.root / "agent"),
            "state_dir": str(self.root / "state"),
            "model": "github-copilot/test",
        }
        worker.atomic_json(self.config_path, self.config)
        self.state_path = Path(self.config["state_dir"]) / "state.json"
        self.state = {"last_slot": "2026-10-30T18:00:00-07:00", "active_run": None}
        worker.atomic_json(self.state_path, self.state)
        preflight = patch.object(
            worker, "preflight", return_value={"warnings": [], "worktree_pools": {}}
        )
        preflight.start()
        self.addCleanup(preflight.stop)

    def test_tailscale_work_wraps_execution_and_restores_after_failure(self):
        vpn = FakeTailscale()
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})

        def execute(*args):
            self.assertEqual(vpn.selected, "work")
            vpn.events.append("execute")
            raise RuntimeError("Work failed")

        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "execute", side_effect=execute
        ), patch.object(worker, "notify"):
            self.assertEqual(worker.main(["--config", str(self.config_path), "run"]), 1)
        self.assertEqual(vpn.events, ["work", "execute", "personal"])
        self.assertFalse(
            (Path(self.config["state_dir"]) / "tailscale-session.json").exists()
        )

    def test_completion_is_announced_only_after_verified_personal_restoration(self):
        vpn = FakeTailscale()
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        observations = []

        def observe(message):
            if message.startswith("Pass finished:"):
                observations.append(
                    (
                        vpn.selected,
                        (
                            Path(self.config["state_dir"]) / "tailscale-session.json"
                        ).exists(),
                    )
                )
            return True

        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "pages", return_value=[]
        ), patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "notify", side_effect=observe
        ):
            self.assertEqual(worker.main(["--config", str(self.config_path), "run"]), 0)
        self.assertEqual(observations, [("personal", False)])

    def test_failed_restoration_defers_completion_and_next_tick_notifies_once(self):
        vpn = FakeTailscale()
        vpn.fail_before.add("personal")
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        messages = []
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "pages", return_value=[]
        ), patch.object(worker, "lease", return_value=self.root) as lease, patch.object(
            worker,
            "notify",
            side_effect=lambda message: messages.append((message, vpn.selected))
            or True,
        ), patch.object(
            worker.time, "sleep"
        ), patch.object(
            worker, "now", return_value=instant
        ):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 1
            )
            self.assertFalse([m for m, _ in messages if m.startswith("Pass finished:")])
            saved = worker.read_json(self.state_path)
            self.assertIsNone(saved["active_run"])
            self.assertTrue(
                (Path(self.config["state_dir"]) / "tailscale-session.json").exists()
            )
            vpn.fail_before.clear()
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        self.assertEqual(
            [
                (m.startswith("Pass finished:"), p)
                for m, p in messages
                if m.startswith("Pass finished:")
            ],
            [(True, "personal")],
        )
        self.assertEqual(lease.call_count, 1)
        completed = worker.read_json(Path(saved["last_run"]) / "run.json")
        self.assertIn("completion_notified_at", completed)
        self.assertNotIn("completion_notification_pending", completed)

    def test_deferred_completion_does_not_reacquire_work_in_the_same_tick(self):
        vpn = FakeTailscale()
        vpn.selected = "work"
        root = Path(self.config["state_dir"])
        directory = root / "runs" / "completed"
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        worker.atomic_json(
            directory / "run.json",
            {
                "finished_at": "2026-11-06T14:00:00+00:00",
                "completion_notification_pending": True,
            },
        )
        worker.atomic_json(self.state_path, {**self.state, "last_run": str(directory)})
        worker.atomic_json(root / "tailscale-session.json", vpn.settings)
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "execute"
        ) as execute, patch.object(worker, "notify", return_value=True), patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-07T12:00:00+00:00"),
        ):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
            self.assertEqual(vpn.events, ["personal"])
            self.assertEqual(vpn.selected, "personal")
            execute.assert_not_called()
            self.assertEqual(
                worker.read_json(self.state_path)["last_slot"], self.state["last_slot"]
            )
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        self.assertEqual(execute.call_count, 1)

    def test_failed_network_start_does_not_consume_daily_slot(self):
        vpn = FakeTailscale()
        vpn.fail_before.add("work")
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "execute"
        ) as execute, patch.object(worker, "notify"), patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-07T02:00:00+00:00"),
        ):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 1
            )
        saved = worker.read_json(self.state_path)
        self.assertEqual(saved["last_slot"], self.state["last_slot"])
        self.assertIn("retry_after", saved)
        self.assertEqual(vpn.selected, "personal")
        execute.assert_not_called()

    def test_pending_network_restores_even_when_paused_or_not_due(self):
        vpn = FakeTailscale()
        root = Path(self.config["state_dir"])
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        worker.atomic_json(
            self.state_path,
            {**self.state, "last_slot": worker.latest_slot(instant).isoformat()},
        )
        for paused in (False, True):
            vpn.selected = "work"
            worker.atomic_json(
                root / "tailscale-session.json",
                {"work_profile": "work", "restore_profile": "personal"},
            )
            if paused:
                (root / "paused").touch()
            with patch.object(worker, "command", side_effect=vpn.command), patch.object(
                worker, "execute"
            ) as execute, patch.object(worker, "now", return_value=instant):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 0
                )
            self.assertEqual(vpn.selected, "personal")
            self.assertFalse((root / "tailscale-session.json").exists())
            execute.assert_not_called()

    def test_idle_tick_and_read_only_controls_do_not_switch_profiles(self):
        vpn = FakeTailscale()
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        worker.atomic_json(
            self.state_path,
            {**self.state, "last_slot": worker.latest_slot(instant).isoformat()},
        )
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "now", return_value=instant
        ):
            for action in ("scheduled", "status", "preflight"):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), action]), 0
                )
        self.assertEqual(vpn.events, [])
        self.assertEqual(vpn.selected, "personal")

    def test_restore_retry_does_not_repeat_a_completed_daily_pass(self):
        vpn = FakeTailscale()
        vpn.fail_before.add("personal")
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "execute"
        ) as execute, patch.object(worker, "notify"), patch.object(
            worker, "now", return_value=instant
        ):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 1
            )
            vpn.fail_before.clear()
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(vpn.selected, "personal")
        self.assertNotIn("retry_after", worker.read_json(self.state_path))
        self.assertFalse(
            (Path(self.config["state_dir"]) / "tailscale-session.json").exists()
        )

    def test_restoration_failures_back_off_without_blocking_operator_recovery(self):
        vpn = FakeTailscale()
        vpn.selected = "work"
        vpn.fail_before.add("personal")
        root = Path(self.config["state_dir"])
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        worker.atomic_json(
            root / "tailscale-session.json",
            {"work_profile": "work", "restore_profile": "personal"},
        )
        worker.atomic_json(
            self.state_path,
            {
                **self.state,
                "active_run": "interrupted",
                "retry_after": "2030-01-01T00:00:00+00:00",
            },
        )
        (root / "paused").touch()
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        for attempt, delay in enumerate((60, 120, 300, 300), 1):
            with patch.object(worker, "command", side_effect=vpn.command), patch.object(
                worker, "notify"
            ), patch.object(worker, "now", return_value=instant):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 1
                )
                saved = worker.read_json(self.state_path)
                due = datetime.fromisoformat(saved["tailscale_restore_retry_after"])
                self.assertEqual((due - instant).total_seconds(), delay)
                self.assertEqual(saved["retry_after"], "2030-01-01T00:00:00+00:00")
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 0
                )
            self.assertEqual(len(vpn.events), attempt * 3)
            self.assertEqual(vpn.selected, "work")
            instant = due
        vpn.fail_before.clear()
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-07T02:00:01+00:00"),
        ), patch.object(worker, "execute") as execute:
            self.assertEqual(worker.main(["--config", str(self.config_path), "run"]), 0)
        self.assertEqual(vpn.selected, "personal")
        self.assertFalse((root / "tailscale-session.json").exists())
        self.assertNotIn(
            "tailscale_restore_retry_after", worker.read_json(self.state_path)
        )
        self.assertNotIn(
            "tailscale_restore_failures", worker.read_json(self.state_path)
        )
        execute.assert_not_called()

    def test_held_worker_lock_prevents_network_restoration(self):
        vpn = FakeTailscale()
        vpn.selected = "work"
        root = Path(self.config["state_dir"])
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        worker.atomic_json(
            root / "tailscale-session.json",
            {"work_profile": "work", "restore_profile": "personal"},
        )
        with worker.exclusive_lock(root), patch.object(
            worker, "command", side_effect=vpn.command
        ), patch.object(worker, "notify"):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        self.assertEqual(vpn.selected, "work")
        self.assertTrue((root / "tailscale-session.json").exists())
        self.assertEqual(vpn.events, [])

    def test_interrupt_restores_network_and_original_signal_handlers(self):
        vpn = FakeTailscale()
        worker.atomic_json(self.config_path, {**self.config, "tailscale": vpn.settings})
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(worker, "command", side_effect=vpn.command), patch.object(
            worker, "execute", side_effect=worker.WorkerInterrupted(signal.SIGTERM)
        ):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "run"]),
                128 + signal.SIGTERM,
            )
        self.assertEqual(vpn.selected, "personal")
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertFalse(
            (Path(self.config["state_dir"]) / "tailscale-session.json").exists()
        )

    def test_config_rejects_naive_or_invalid_schedule_cutover(self):
        for changed_at in ("2026-11-14T09:00:00", "not-a-date", 3):
            worker.atomic_json(
                self.config_path, {**self.config, "schedule_changed_at": changed_at}
            )
            with self.subTest(changed_at=changed_at), self.assertRaises(ValueError):
                worker.load_config(self.config_path)

    def test_config_rejects_invalid_tailscale_profile_settings(self):
        for settings in (
            {},
            {
                "executable": "relative",
                "work_profile": "work",
                "restore_profile": "personal",
            },
            {
                "executable": "/tailscale",
                "work_profile": "same",
                "restore_profile": "same",
            },
        ):
            worker.atomic_json(self.config_path, {**self.config, "tailscale": settings})
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                worker.load_config(self.config_path)

    def test_config_rejects_broad_permissions_symlinks_and_unauthorized_tracker(self):
        self.assertEqual(worker.load_config(self.config_path), self.config)
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            worker.load_config(self.config_path)
        self.config_path.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(self.config_path)
        with self.assertRaisesRegex(ValueError, "regular file"):
            worker.load_config(link)
        worker.atomic_json(
            self.config_path, {**self.config, "allowed_repositories": ["example/app"]}
        )
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            worker.load_config(self.config_path)

    def test_config_rejects_untrusted_or_relative_implementation_checkouts(self):
        for mapping in (
            {"outsider/app": "/checkout"},
            {"example/app": "relative"},
            {"example/tracker": "/different-tracker"},
            [],
        ):
            worker.atomic_json(
                self.config_path, {**self.config, "implementation_checkouts": mapping}
            )
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                worker.load_config(self.config_path)

    def test_atomic_write_leaves_complete_json_and_no_temporary_files(self):
        worker.atomic_json(self.state_path, {"new": [1, 2, 3]})
        self.assertEqual(worker.read_json(self.state_path), {"new": [1, 2, 3]})
        self.assertEqual(list(self.state_path.parent.glob(".pending-*")), [])

    def test_lock_excludes_another_process_and_is_inherited(self):
        with worker.exclusive_lock(self.root) as fd:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(2)"], pass_fds=(fd,)
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())
        with self.assertRaisesRegex(RuntimeError, "holds the lock"):
            with worker.exclusive_lock(self.root):
                self.fail("Overlapping work was admitted")
        process.wait()
        with worker.exclusive_lock(self.root):
            pass

    def test_snapshot_includes_unmatched_context_and_all_comments(self):
        rows = [
            issue(1, comments=2),
            issue(2, title="A related feature"),
            issue(3, pull_request={}),
        ]
        comments = [
            {"id": 80, "body": "Shared infrastructure failure"},
            {"id": 81, "body": "A later correction"},
        ]
        target = self.root / "snapshot"
        with patch.object(worker, "pages", side_effect=[rows, comments]):
            manifest = worker.snapshot(self.config, target)
        self.assertEqual([row["number"] for row in manifest["issues"]], [1, 2])
        self.assertEqual(
            worker.read_json(target / "issue-1.json")["comments"], comments
        )
        self.assertFalse(manifest["issues"][1]["eligible"])
        self.assertTrue((target / "manifest.json").exists())

    def test_failed_comment_fetch_does_not_publish_manifest(self):
        with patch.object(
            worker,
            "pages",
            side_effect=[[issue(comments=1)], RuntimeError("unavailable")],
        ):
            with self.assertRaises(RuntimeError):
                worker.snapshot(self.config, self.root / "snapshot")
        self.assertFalse((self.root / "snapshot/manifest.json").exists())

    def test_batches_bound_large_context_without_omitting_an_issue(self):
        target = self.root / "snapshot"
        target.mkdir()
        for number in [1, 2, 3]:
            (target / f"issue-{number}.json").write_text("x" * 10)
        batches = worker.analysis_batches(
            {"issues": [{"number": n} for n in [1, 2, 3]]}, target, byte_limit=20
        )
        self.assertEqual(batches, [[{"number": 1}, {"number": 2}], [{"number": 3}]])

    def test_dry_run_does_not_consume_slot_launch_models_or_acquire_worktree(self):
        with patch.object(
            worker, "snapshot", return_value={"issues": []}
        ), patch.object(worker, "execute") as execute:
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "dry-run"]), 0
            )
        execute.assert_not_called()
        self.assertEqual(worker.read_json(self.state_path), self.state)

    def test_scheduled_slot_is_claimed_before_execution_and_not_repeated(self):
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        observed = []
        with patch.object(worker, "now", return_value=instant), patch.object(
            worker,
            "execute",
            side_effect=lambda *a: observed.append(
                worker.read_json(self.state_path)["last_slot"]
            ),
        ):
            for _ in range(2):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 0
                )
        self.assertEqual(observed, ["2026-11-06T03:00:00-08:00"])

    def test_interrupted_run_retries_after_backoff_without_waiting_for_new_slot(self):
        instant = datetime.fromisoformat("2026-11-07T02:00:00+00:00")

        def interrupt(config, state, state_path, *args):
            state["active_run"] = "interrupted"
            worker.atomic_json(state_path, state)
            raise RuntimeError("Network unavailable")

        with patch.object(worker, "now", return_value=instant), patch.object(
            worker, "execute", side_effect=interrupt
        ) as execute, patch.object(worker, "notify"):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 1
            )
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
            self.assertEqual(execute.call_count, 1)
        retry = worker.read_json(self.state_path)["retry_after"]
        with patch.object(
            worker, "now", return_value=datetime.fromisoformat(retry)
        ), patch.object(worker, "execute") as execute:
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        self.assertEqual(execute.call_count, 1)
        self.assertNotIn("retry_after", worker.read_json(self.state_path))

    def test_schedule_cutover_skips_past_slot_but_runs_at_next_three_am(self):
        worker.atomic_json(
            self.config_path,
            {**self.config, "schedule_changed_at": "2026-11-14T17:00:00+00:00"},
        )
        with patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-14T17:01:00+00:00"),
        ), patch.object(worker, "execute") as execute:
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        execute.assert_not_called()
        with patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-15T11:00:00+00:00"),
        ), patch.object(worker, "execute") as execute:
            for _ in range(2):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 0
                )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(
            worker.read_json(self.state_path)["last_slot"], "2026-11-15T03:00:00-08:00"
        )

    def test_schedule_cutover_does_not_block_recovery_of_an_active_run(self):
        worker.atomic_json(
            self.config_path,
            {**self.config, "schedule_changed_at": "2026-11-14T17:00:00+00:00"},
        )
        worker.atomic_json(self.state_path, {**self.state, "active_run": "ongoing"})
        with patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-14T17:01:00+00:00"),
        ), patch.object(worker, "execute") as execute:
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 0
            )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(worker.read_json(self.state_path)["active_run"], "ongoing")

    def test_manual_run_does_not_consume_daily_slot(self):
        with patch.object(worker, "execute") as execute:
            self.assertEqual(worker.main(["--config", str(self.config_path), "run"]), 0)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(worker.read_json(self.state_path), self.state)

    def test_pause_prevents_execution_and_resume_preserves_schedule(self):
        with patch.object(worker, "execute") as execute:
            for action in ["pause", "run", "scheduled", "resume"]:
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), action]), 0
                )
        execute.assert_not_called()
        self.assertEqual(worker.read_json(self.state_path), self.state)

    def test_missing_state_fails_closed(self):
        self.state_path.unlink()
        with patch.object(worker, "execute") as execute, patch.object(worker, "notify"):
            self.assertEqual(
                worker.main(["--config", str(self.config_path), "scheduled"]), 1
            )
        execute.assert_not_called()

    def test_child_cannot_start_a_nested_worker(self):
        with patch.dict(os.environ, {"BACKLOG_AUTOPILOT_CHILD": "1"}), patch.object(
            worker, "execute"
        ) as execute, patch.object(worker, "notify"):
            self.assertEqual(worker.main(["--config", str(self.config_path), "run"]), 1)
        execute.assert_not_called()

    def test_cli_real_entry_point_reports_saved_state(self):
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--config",
                str(self.config_path),
                "status",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["last_slot"], self.state["last_slot"])
        self.assertFalse(data["paused"])

    def test_pending_pr_rejects_foreign_target_and_missing_certification(self):
        receipt = {
            "outcome": "awaiting_review",
            "pr_urls": ["https://github.com/outsider/app/pull/1"],
        }
        with patch.object(worker, "api") as api:
            with self.assertRaisesRegex(ValueError, "allowlist"):
                worker.verify_pull_requests(self.config, receipt)
        api.assert_not_called()
        receipt["pr_urls"] = ["https://github.com/example/app/pull/1"]
        pr = {
            "state": "open",
            "head": {"sha": "certified-sha"},
            "assignees": [{"login": "zkoppert"}],
            "requested_reviewers": [{"login": "copilot-pull-request-reviewer[bot]"}],
        }
        with patch.object(worker, "api", return_value=pr), patch.object(
            worker, "pages", return_value=[]
        ):
            with self.assertRaisesRegex(ValueError, "certified worktree"):
                worker.verify_pull_requests(self.config, receipt)

    def test_real_child_entry_point_saves_receipts_and_continues_after_item_failure(
        self,
    ):
        fake = self.root / "fake-pi"
        fake.write_text("#!" + sys.executable + "\n" + """
import json, pathlib, sys
stage = pathlib.Path(sys.argv[sys.argv.index("--session") + 1]).parent
if stage.parent.name == "analysis":
    prompt = json.loads((stage / "invocation.json").read_text())["prompt"]
    batch = json.loads(prompt.split("Issues: ", 1)[1])
    manifest = json.loads((stage.parent.parent / "snapshot/manifest.json").read_text())
    result = {"snapshot_sha256": manifest["sha256"], "notes": [dict(row, note="Verified shared context") for row in batch]}
else:
    task = json.loads((stage / "task.json").read_text())
    context = json.loads(pathlib.Path(task["context_file"]).read_text())
    assert len(context["notes"]) == 3
    if task["number"] == 1:
        sys.exit(7)
    result = {"number": task["number"], "outcome": "blocked", "summary": "Waiting for access", "evidence": []}
(stage / "receipt.json").write_text(json.dumps(result))
""")
        fake.chmod(0o700)
        self.config["pi"] = str(fake)
        rows = [issue(1), issue(2), issue(3, title="Context only")]
        with patch.object(worker, "pages", return_value=rows), patch.object(
            worker,
            "api",
            side_effect=lambda endpoint: issue(int(endpoint.rsplit("/", 1)[1])),
        ), patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "notify"
        ):
            with worker.exclusive_lock(Path(self.config["state_dir"])) as fd:
                worker.execute(
                    self.config, self.state, self.state_path, fd, MODULE_PATH.parent
                )
        saved = worker.read_json(self.state_path)
        run = worker.read_json(Path(saved["last_run"]) / "run.json")
        self.assertEqual(set(run["items"]), {"1", "2"})
        self.assertEqual(
            run["outcome_counts"], {"infrastructure_error": 1, "blocked": 1}
        )
        report = (Path(saved["last_run"]) / "report.md").read_text()
        self.assertIn("infrastructure_error: 1", report)
        self.assertIn("Waiting for access", report)
        self.assertTrue((Path(saved["last_run"]) / "items/1/error.json").exists())
        self.assertIn("Pi exited 7", run["items"]["1"]["summary"])
        self.assertEqual(run["items"]["2"]["summary"], "Waiting for access")
        self.assertIsNone(saved["active_run"])
        self.assertTrue((Path(saved["last_run"]) / "items/2/output.log").exists())

    def test_install_initializes_current_slot_without_running_and_preserves_existing_state(
        self,
    ):
        instant = datetime.fromisoformat("2026-11-04T06:00:00+00:00")
        self.state_path.unlink()
        home = self.root / "home"
        with patch.object(worker.Path, "home", return_value=home), patch.object(
            worker, "now", return_value=instant
        ):
            worker.install(self.config_path, self.config, MODULE_PATH.parent)
            saved = worker.read_json(self.state_path)
            self.assertEqual(saved["last_slot"], "2026-11-03T03:00:00-08:00")
            release = worker.read_json(
                Path(self.config["state_dir"]) / "current/release.json"
            )
            self.assertEqual(release["source_checkout"], str(MODULE_PATH.parent.parent))
            self.assertEqual(
                set(release["sha256"]),
                {
                    "backlog_autopilot.py",
                    "backlog_autopilot_policy.md",
                    "backlog_autopilot_extension.ts",
                },
            )
            plist = home / "Library/LaunchAgents" / (worker.LABEL + ".plist")
            import plistlib

            settings = plistlib.loads(plist.read_bytes())
            self.assertEqual(settings["StartInterval"], 60)
            self.assertFalse(settings["RunAtLoad"])
            self.assertEqual(settings["ProgramArguments"][-1], "scheduled")
            worker.install(self.config_path, self.config, MODULE_PATH.parent)
        self.assertEqual(worker.read_json(self.state_path), saved)

    def saved_run(self, count=1):
        self.state["active_run"] = "resume"
        directory = Path(self.config["state_dir"]) / "runs/resume"
        rows = [
            {"number": n, "sha256": str(n), "eligible": True}
            for n in range(1, count + 1)
        ]
        for row in rows:
            worker.atomic_json(
                directory / f"snapshot/issue-{row['number']}.json",
                {"issue": issue(row["number"])},
            )
        worker.atomic_json(
            directory / "snapshot/manifest.json", {"sha256": "snapshot", "issues": rows}
        )
        worker.atomic_json(
            directory / "analysis/0/receipt.json",
            {
                "snapshot_sha256": "snapshot",
                "notes": [
                    {**row, "note": "An existing incident provides context"}
                    for row in rows
                ],
            },
        )
        return directory

    def test_ready_pr_notifies_before_next_issue_and_survives_resume(self):
        directory = self.saved_run(count=2)
        url = "https://github.com/example/app/pull/1"
        result = {
            "number": 1,
            "outcome": "awaiting_review",
            "summary": "Certified draft is ready",
            "evidence": ["tests.log"],
            "pr_urls": [url],
            "worktrees": [str(self.root)],
        }
        worker.atomic_json(directory / "items/1/receipt.json", result)
        messages = []

        def fetch(endpoint):
            number = int(endpoint.rsplit("/", 1)[1])
            if number == 2:
                self.assertEqual(messages, [f"Draft PR ready for your review: {url}"])
                self.assertEqual(
                    worker.read_json(directory / "run.json")["items"]["1"], result
                )
            return issue(number, state="open" if number == 1 else "closed")

        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", side_effect=fetch
        ), patch.object(worker, "verify_pull_requests"), patch.object(
            worker,
            "notify",
            side_effect=lambda message: messages.append(message) or True,
        ):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
            self.state["active_run"] = "resume"
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        self.assertEqual(messages.count(f"Draft PR ready for your review: {url}"), 1)
        saved = worker.read_json(directory / "run.json")
        self.assertEqual(saved["items"]["1"], result)
        self.assertEqual(saved["items"]["2"]["outcome"], "skipped")

    def test_resume_retries_saved_ready_notification_before_leasing(self):
        directory = self.saved_run()
        url = "https://github.com/example/app/pull/1"
        worker.atomic_json(
            directory / "run.json",
            {
                "id": "resume",
                "items": {"1": {"outcome": "awaiting_review", "pr_urls": [url]}},
            },
        )
        with patch.object(
            worker, "lease", side_effect=RuntimeError("No slot")
        ), patch.object(worker, "verify_pull_requests") as verify, patch.object(
            worker, "notify", return_value=True
        ) as send:
            with self.assertRaisesRegex(RuntimeError, "No slot"):
                worker.execute(
                    self.config, self.state, self.state_path, 1, MODULE_PATH.parent
                )
        self.assertEqual(
            send.call_args.args[0], f"Draft PR ready for your review: {url}"
        )
        self.assertEqual(
            set(worker.read_json(directory.parent.parent / "pr-notifications.json")),
            {url},
        )
        self.assertEqual(verify.call_count, 1)

    def test_unverified_pr_does_not_send_ready_notification(self):
        directory = self.saved_run()
        worker.atomic_json(
            directory / "items/1/receipt.json",
            {
                "number": 1,
                "outcome": "awaiting_review",
                "summary": "Not certified",
                "evidence": [],
                "pr_urls": ["https://github.com/example/app/pull/1"],
            },
        )
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", return_value=issue()
        ), patch.object(
            worker, "verify_pull_requests", side_effect=ValueError("Uncertified")
        ), patch.object(
            worker, "notify", return_value=True
        ) as send:
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        self.assertEqual(
            worker.read_json(directory / "run.json")["items"]["1"]["outcome"],
            "infrastructure_error",
        )
        send.assert_not_called()
        self.assertTrue(
            worker.read_json(directory / "run.json")["completion_notification_pending"]
        )

    def test_resume_reconciles_a_saved_closure_receipt_before_eligibility(self):
        directory = self.saved_run()
        result = {
            "number": 1,
            "outcome": "resolved",
            "summary": "Verified deployed fix",
            "evidence": ["sentry.json"],
        }
        worker.atomic_json(directory / "items/1/receipt.json", result)
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", return_value=issue(state="closed")
        ), patch.object(worker, "run_pi") as model, patch.object(worker, "notify"):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        saved = worker.read_json(directory / "run.json")
        self.assertEqual(saved["items"]["1"], result)
        self.assertEqual(saved["outcome_counts"], {"resolved": 1})
        self.assertEqual(
            worker.read_json(directory.parent.parent / "ledger.json")["1"]["evidence"],
            ["sentry.json"],
        )
        model.assert_not_called()

    def test_invalid_saved_analysis_is_regenerated_and_run_can_finish(self):
        directory = self.saved_run()
        receipt = directory / "analysis/0/receipt.json"
        valid = worker.read_json(receipt)
        worker.atomic_json(receipt, {"snapshot_sha256": "wrong", "notes": []})
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", return_value=issue(state="closed")
        ), patch.object(
            worker,
            "run_pi",
            side_effect=lambda *a, **k: worker.atomic_json(receipt, valid),
        ) as model, patch.object(
            worker, "notify"
        ):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(list(receipt.parent.glob("invalid-receipt-*.json"))), 1)
        self.assertIsNone(self.state["active_run"])

    def test_pause_inside_execution_preserves_completed_items_for_resume(self):
        directory = self.saved_run(count=2)
        first = {
            "number": 1,
            "outcome": "blocked",
            "summary": "Needs owner",
            "evidence": [],
        }
        worker.atomic_json(
            directory / "run.json", {"id": "resume", "items": {"1": first}}
        )
        pause = Path(self.config["state_dir"]) / "paused"
        pause.touch()
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", return_value=issue(2, state="closed")
        ) as api, patch.object(worker, "run_pi") as model, patch.object(
            worker, "notify"
        ):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
            self.assertEqual(self.state["active_run"], "resume")
            api.assert_not_called()
            pause.unlink()
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        saved = worker.read_json(directory / "run.json")
        self.assertEqual(saved["items"]["1"], first)
        self.assertEqual(saved["items"]["2"]["outcome"], "skipped")
        model.assert_not_called()

    def test_saved_lease_is_reused_and_missing_lease_is_replaced_without_deletion(self):
        old = {"path": "/old/worktree", "lease_id": "old", "lease_holder": "old-holder"}
        new = {"path": "/new/worktree", "lease_id": "new", "lease_holder": "new-holder"}
        run = {"id": "resume", "lease": old}
        with patch.object(
            worker, "command", return_value=json.dumps([{**old, "status": "leased"}])
        ) as command:
            self.assertEqual(worker.lease(self.config, run), Path(old["path"]))
        self.assertEqual(command.call_count, 1)
        with patch.object(
            worker,
            "command",
            side_effect=[
                "[]",
                json.dumps(new),
                json.dumps([{**new, "status": "leased"}]),
            ],
        ):
            self.assertEqual(worker.lease(self.config, run), Path(new["path"]))
        self.assertEqual(run["unavailable_leases"], [old])
        self.assertEqual(run["lease"], new)

    def test_clean_coordinator_is_returned_with_identity_guards_and_dirty_work_is_preserved(
        self,
    ):
        allocation = {
            "path": "/worktree",
            "lease_id": "lease",
            "lease_holder": "holder",
        }
        run = {"lease": allocation}
        rows = [{**allocation, "status": "leased", "processes": []}]
        with patch.object(
            worker, "command", side_effect=[json.dumps(rows), "", "", "returned"]
        ) as command:
            worker.release_coordinator(self.config, run)
        self.assertEqual(
            command.call_args.args[0],
            [
                "treehouse",
                "return",
                "--if-lease-id",
                "lease",
                "--if-lease-holder",
                "holder",
                "/worktree",
            ],
        )
        self.assertEqual(run["returned_lease"], allocation)
        self.assertNotIn("lease", run)
        run = {"lease": allocation}
        with patch.object(
            worker, "command", side_effect=[json.dumps(rows), " M source.py"]
        ) as command:
            worker.release_coordinator(self.config, run)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(run["lease"], allocation)

    def test_scheduled_lock_contention_is_quiet_and_does_not_consume_slot(self):
        with worker.exclusive_lock(Path(self.config["state_dir"])), patch.object(
            worker,
            "now",
            return_value=datetime.fromisoformat("2026-11-07T02:00:00+00:00"),
        ), patch.object(worker, "notify") as notify:
            for _ in range(3):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 0
                )
        notify.assert_not_called()
        self.assertEqual(worker.read_json(self.state_path), self.state)

    def test_repeated_configuration_failure_notifies_once(self):
        self.config_path.chmod(0o644)
        with patch.object(worker, "notify") as notify:
            for _ in range(3):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 1
                )
        self.assertEqual(notify.call_count, 1)

    def test_malformed_outcome_is_a_validation_error(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            worker.validate_outcome([], 1, issue())

    def test_certified_pending_pr_accepts_existing_copilot_review(self):
        receipt = {
            "outcome": "awaiting_review",
            "pr_urls": ["https://github.com/example/app/pull/1"],
            "worktrees": ["/worktree"],
        }
        pr = {
            "state": "open",
            "head": {"sha": "certified-sha"},
            "assignees": [{"login": "zkoppert"}],
            "requested_reviewers": [],
        }
        reviews = [{"user": {"login": "copilot-pull-request-reviewer[bot]"}}]
        with patch.object(worker, "api", return_value=pr), patch.object(
            worker, "pages", return_value=reviews
        ), patch.object(
            worker,
            "command",
            side_effect=[
                json.dumps([{"path": "/worktree", "status": "leased"}]),
                "",
                "git@github.com:example/app.git",
                "certified-sha",
                "passed",
            ],
        ) as command, patch.object(
            worker, "ci_green", return_value=True
        ):
            worker.verify_pull_requests(self.config, receipt)
        self.assertEqual(command.call_args.args[0], ["pr-marker", "check"])
        self.assertEqual(command.call_args.kwargs["cwd"], "/worktree")

    def test_lease_identity_is_saved_before_verification_can_fail(self):
        run = {"id": "persistence"}
        run_path = self.root / "run.json"
        allocation = {"path": "/worktree", "lease_id": "new", "lease_holder": "holder"}
        with patch.object(
            worker, "command", side_effect=["[]", json.dumps(allocation), "[]"]
        ):
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                worker.lease(self.config, run, run_path)
        self.assertEqual(worker.read_json(run_path)["lease"], allocation)

    def test_unpushed_coordinator_commit_prevents_destructive_return(self):
        allocation = {
            "path": "/worktree",
            "lease_id": "lease",
            "lease_holder": "holder",
        }
        run = {"lease": allocation}
        rows = [{**allocation, "status": "leased", "processes": []}]
        with patch.object(
            worker, "command", side_effect=[json.dumps(rows), "", "unpublished-commit"]
        ) as command:
            worker.release_coordinator(self.config, run)
        self.assertEqual(command.call_count, 3)
        self.assertEqual(
            command.call_args.args[0], ["git", "rev-list", "HEAD", "--not", "--remotes"]
        )
        self.assertEqual(run["lease"], allocation)
        self.assertNotIn("returned_lease", run)

    def test_replacement_coordinator_archives_old_cwd_session_without_rewriting_history(
        self,
    ):
        stage = self.root / "stage"
        stage.mkdir()
        old = self.root / "old-coordinator"
        new = self.root / "new-coordinator"
        new.mkdir()
        header = {
            "type": "session",
            "version": 3,
            "id": "9fc71603-2180-4b6b-b9cf-7957390daf63",
            "timestamp": "2026-11-03T00:00:00.000Z",
            "cwd": str(old),
        }
        original = json.dumps(header) + "\n"
        (stage / "session.jsonl").write_text(original)
        session, previous = worker.prepare_session(stage, new)
        self.assertFalse(session.exists())
        self.assertEqual(len(previous), 1)
        self.assertEqual(Path(previous[0]).read_text(), original)
        (stage / "session.jsonl").write_text(
            json.dumps({**header, "cwd": str(new)}) + "\n"
        )
        same, history = worker.prepare_session(stage, new)
        self.assertEqual(same, session)
        self.assertEqual(history, previous)
        self.assertTrue(same.exists())

    def test_prior_attempt_closed_without_receipt_resumes_read_only_reconciliation(
        self,
    ):
        directory = self.saved_run()
        stage = directory / "items/1"
        worker.atomic_json(stage / "task.json", {"number": 1})
        result = {
            "number": 1,
            "outcome": "resolved",
            "summary": "Verified prior closure from session and comment",
            "evidence": ["sentry.json"],
        }
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", return_value=issue(state="closed")
        ), patch.object(
            worker,
            "pages",
            return_value=[{"id": 80, "body": "Verified closure evidence"}],
        ), patch.object(
            worker,
            "run_pi",
            side_effect=lambda *a, **k: worker.atomic_json(
                stage / "receipt.json", result
            ),
        ) as model, patch.object(
            worker, "notify"
        ):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        self.assertTrue(model.call_args.kwargs["analysis"])
        self.assertTrue(worker.read_json(stage / "task.json")["reconciliation_only"])
        self.assertEqual(worker.read_json(directory / "run.json")["items"]["1"], result)

    def test_child_failure_after_receipt_does_not_discard_verified_outcome(self):
        fake = self.root / "receipt-then-failure"
        fake.write_text(
            "#!"
            + sys.executable
            + "\nimport pathlib,sys\nstage=pathlib.Path(sys.argv[sys.argv.index('--session')+1]).parent\n(stage/'receipt.json').write_text('{}')\nsys.exit(7)\n"
        )
        fake.chmod(0o700)
        config = {**self.config, "pi": str(fake)}
        stage = self.root / "child"
        with worker.exclusive_lock(Path(self.config["state_dir"])) as fd:
            worker.run_pi(
                config,
                stage,
                self.root,
                "Write receipt",
                fd,
                MODULE_PATH.with_name("backlog_autopilot_policy.md"),
            )
        self.assertEqual(worker.read_json(stage / "receipt.json"), {})
        with self.assertRaises(ValueError):
            worker.validate_outcome(
                worker.read_json(stage / "receipt.json"), 1, issue()
            )

    def test_future_install_preserves_running_release_state_and_launchagent(self):
        home = self.root / "home"
        with patch.object(worker.Path, "home", return_value=home):
            worker.install(self.config_path, self.config, MODULE_PATH.parent)
            root = Path(self.config["state_dir"])
            previous = (root / "current").resolve()
            files = {path.name: path.read_bytes() for path in previous.iterdir()}
            worker.atomic_json(self.state_path, {**self.state, "active_run": "ongoing"})
            state = self.state_path.read_bytes()
            plist = home / "Library/LaunchAgents" / (worker.LABEL + ".plist")
            plist_bytes = plist.read_bytes()
            config_bytes = self.config_path.read_bytes()
            with worker.exclusive_lock(root):
                worker.install(
                    self.config_path, self.config, MODULE_PATH.parent, future_only=True
                )
            self.assertNotEqual((root / "current").resolve(), previous)
            self.assertEqual(self.state_path.read_bytes(), state)
            self.assertEqual(plist.read_bytes(), plist_bytes)
            self.assertEqual(self.config_path.read_bytes(), config_bytes)
            self.assertEqual(
                {path.name: path.read_bytes() for path in previous.iterdir()}, files
            )

    def test_future_install_rejects_policy_changes_without_replacing_current(self):
        home = self.root / "home"
        with patch.object(worker.Path, "home", return_value=home):
            worker.install(self.config_path, self.config, MODULE_PATH.parent)
            current = (Path(self.config["state_dir"]) / "current").resolve()
            candidate = self.root / "candidate/scripts"
            candidate.mkdir(parents=True)
            for name in (
                "backlog_autopilot.py",
                "backlog_autopilot_policy.md",
                "backlog_autopilot_extension.ts",
            ):
                (candidate / name).write_bytes((MODULE_PATH.parent / name).read_bytes())
            (candidate / "backlog_autopilot_policy.md").write_text(
                "Different worker policy"
            )
            with self.assertRaisesRegex(ValueError, "cannot change the active policy"):
                worker.install(
                    self.config_path, self.config, candidate, future_only=True
                )
            self.assertEqual(
                (Path(self.config["state_dir"]) / "current").resolve(), current
            )

    def test_install_rejects_another_source_before_changing_release(self):
        config = {**self.config, "source_checkout": str(self.root / "other-source")}
        with self.assertRaisesRegex(ValueError, "configured source_checkout"):
            worker.install(self.config_path, config, MODULE_PATH.parent)
        self.assertFalse((Path(self.config["state_dir"]) / "current").exists())

    def test_real_scheduled_cli_replay_writes_report_and_does_not_repeat_slot(self):
        tools = self.root / "tools"
        tools.mkdir()
        fixture = r"""
import base64,json,os,pathlib,sys
root=pathlib.Path(os.environ["WORKER_REPLAY_ROOT"])
name=pathlib.Path(sys.argv[0]).name
allocation={"path":str(root),"lease_id":"replay","lease_holder":"replay"}
if name=="treehouse":
    print(json.dumps([{**allocation,"status":"leased","processes":[]}] if sys.argv[1]=="status" else allocation))
elif name=="git":
    if sys.argv[1:4]==["remote","get-url","origin"]: print("https://github.com/example/tracker.git")
    elif sys.argv[1]=="rev-list": print("unpublished-replay")
    elif sys.argv[1]!="status": sys.exit(9)
elif name=="gh":
    assert sys.argv[1:7]==["cfs","--","npx","-y","gh-axi","api"]
    endpoint=sys.argv[7]
    issue={"id":101,"number":1,"title":"[Sentry] Replay","state":"open","comments":0}
    if "/issues?" in endpoint: data=[issue] if endpoint.endswith("&page=1") else []
    elif endpoint.endswith("/issues/1"): data=issue
    elif endpoint=="/repos/example/tracker": data={"name":"tracker"}
    else: raise RuntimeError(endpoint)
    print("api_response:\n  body: "+base64.b64encode(json.dumps(data).encode()).decode()+"\n  truncated: false")
elif name=="replay-pi":
    stage=pathlib.Path(sys.argv[sys.argv.index("--session")+1]).parent
    with (root/"model-calls.log").open("a") as log: log.write(str(stage)+"\n")
    if stage.parent.name=="analysis":
        prompt=json.loads((stage/"invocation.json").read_text())["prompt"]
        batch=json.loads(prompt.split("Issues: ",1)[1])
        manifest=json.loads((stage.parent.parent/"snapshot/manifest.json").read_text())
        data={"snapshot_sha256":manifest["sha256"],"notes":[dict(row,note="Existing owner controls repair") for row in batch]}
    else:
        data={"number":1,"outcome":"blocked","summary":"Owner decision needed","evidence":[],"next_action":"Contact the owner"}
    (stage/"receipt.json").write_text(json.dumps(data))
else: sys.exit(9)
"""
        for name in ("treehouse", "git", "gh", "replay-pi"):
            path = tools / name
            path.write_text("#!" + sys.executable + "\n" + fixture)
            path.chmod(0o700)
        worker.atomic_json(
            self.config_path, {**self.config, "pi": str(tools / "replay-pi")}
        )
        env = {
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "WORKER_REPLAY_ROOT": str(self.root),
            "HOME": str(self.root),
        }
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import runpy,sys; scope=runpy.run_path(sys.argv.pop(1)); scope['main'].__globals__['notify']=lambda message: False; sys.exit(scope['main']())",
                    str(MODULE_PATH),
                    "--config",
                    str(self.config_path),
                    "scheduled",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        state = worker.read_json(self.state_path)
        self.assertIsNone(state["active_run"])
        self.assertEqual(
            len((self.root / "model-calls.log").read_text().splitlines()), 2
        )
        run = worker.read_json(Path(state["last_run"]) / "run.json")
        self.assertEqual(run["outcome_counts"], {"blocked": 1})
        self.assertIn(
            "Owner decision needed", (Path(state["last_run"]) / "report.md").read_text()
        )
        progress = worker.read_json(Path(self.config["state_dir"]) / "progress.json")
        self.assertEqual(progress["status"], "finished")

    def test_each_new_issue_gets_refreshed_dependency_health(self):
        directory = self.saved_run(count=2)
        health = [
            {"warnings": [], "checked_at": label, "worktree_pools": {}}
            for label in ("startup", "before-1", "before-2")
        ]
        observed = []

        def model(config, stage, *args, **kwargs):
            task = worker.read_json(stage / "task.json")
            observed.append(task["dependency_health"]["checked_at"])
            worker.atomic_json(
                stage / "receipt.json",
                {
                    "number": task["number"],
                    "outcome": "blocked",
                    "summary": "Needs owner",
                    "evidence": [],
                },
            )

        with patch.object(worker, "preflight", side_effect=health), patch.object(
            worker, "lease", return_value=self.root
        ), patch.object(
            worker,
            "api",
            side_effect=lambda endpoint: issue(int(endpoint.rsplit("/", 1)[1])),
        ), patch.object(
            worker, "run_pi", side_effect=model
        ), patch.object(
            worker, "notify"
        ):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        self.assertEqual(observed, ["before-1", "before-2"])
        self.assertEqual(
            worker.read_json(directory / "run.json")["preflight"]["checked_at"],
            "before-2",
        )

    def test_dependency_outage_checkpoints_and_retry_revisits_only_failed_items(self):
        directory = self.saved_run(count=4)
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "api", side_effect=worker.InfrastructureError("offline")
        ), patch.object(worker, "notify"):
            with self.assertRaisesRegex(
                worker.InfrastructureError, "Three consecutive"
            ):
                worker.execute(
                    self.config, self.state, self.state_path, 1, MODULE_PATH.parent
                )
        saved = worker.read_json(directory / "run.json")
        self.assertEqual(set(saved["items"]), {"1", "2", "3"})
        self.assertTrue(all(item["retryable"] for item in saved["items"].values()))
        self.assertEqual(self.state["active_run"], "resume")
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker,
            "api",
            side_effect=lambda endpoint: issue(
                int(endpoint.rsplit("/", 1)[1]), state="closed"
            ),
        ), patch.object(worker, "notify"):
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        saved = worker.read_json(directory / "run.json")
        self.assertEqual(saved["outcome_counts"], {"skipped": 4})
        self.assertIsNone(self.state["active_run"])

    def test_stalled_child_notifies_once_and_keeps_waiting_for_completion(self):
        class Child:
            pid = 123
            waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits < 3:
                    raise subprocess.TimeoutExpired("pi", timeout)
                return 0

        child = Child()
        with patch.object(worker.subprocess, "Popen", return_value=child), patch.object(
            worker, "progress", side_effect=[False, True, True, False]
        ), patch.object(worker, "notify", return_value=True) as send:
            worker.run_pi(
                self.config,
                self.root / "stage",
                self.root,
                "work",
                1,
                MODULE_PATH.with_name("backlog_autopilot_policy.md"),
            )
        self.assertEqual(child.waits, 3)
        self.assertEqual(send.call_count, 1)
        self.assertIn("Work is still running", send.call_args.args[0])

    def test_completed_long_run_consumes_missed_daily_slots_without_starting_an_overlap(
        self,
    ):
        self.state["active_run"] = "long-run"
        worker.atomic_json(self.state_path, self.state)
        instant = datetime.fromisoformat("2026-11-10T02:00:00+00:00")

        def finish(config, state, state_path, *args):
            state["active_run"] = None

        with patch.object(worker, "now", return_value=instant), patch.object(
            worker, "execute", side_effect=finish
        ) as execute:
            for _ in range(2):
                self.assertEqual(
                    worker.main(["--config", str(self.config_path), "scheduled"]), 0
                )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(
            worker.read_json(self.state_path)["last_slot"], "2026-11-09T03:00:00-08:00"
        )

    def test_pause_between_analysis_batches_preserves_receipt_without_starting_next_batch(
        self,
    ):
        directory = self.saved_run(count=2)
        rows = worker.read_json(directory / "snapshot/manifest.json")["issues"]
        (directory / "analysis/0/receipt.json").unlink()

        def analyze(config, stage, *args, **kwargs):
            worker.atomic_json(
                stage / "receipt.json",
                {
                    "snapshot_sha256": "snapshot",
                    "notes": [{**rows[0], "note": "Verified first batch"}],
                },
            )
            (Path(self.config["state_dir"]) / "paused").touch()

        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker, "analysis_batches", return_value=[[rows[0]], [rows[1]]]
        ), patch.object(worker, "run_pi", side_effect=analyze) as model, patch.object(
            worker, "api"
        ) as api:
            worker.execute(
                self.config, self.state, self.state_path, 1, MODULE_PATH.parent
            )
        self.assertEqual(model.call_count, 1)
        self.assertTrue((directory / "analysis/0/receipt.json").exists())
        self.assertFalse((directory / "analysis/1").exists())
        self.assertEqual(self.state["active_run"], "resume")
        self.assertEqual(worker.read_json(directory / "run.json")["items"], {})
        api.assert_not_called()

    def test_bad_analysis_prevents_any_remediation(self):
        snapshot_dir = Path(self.config["state_dir"]) / "runs/test/snapshot"
        snapshot_dir.mkdir(parents=True)
        worker.atomic_json(snapshot_dir / "issue-1.json", {"issue": issue()})
        worker.atomic_json(
            snapshot_dir / "manifest.json",
            {
                "sha256": "snapshot",
                "issues": [{"number": 1, "sha256": "a", "eligible": True}],
            },
        )
        receipt = snapshot_dir.parent / "analysis/0/receipt.json"
        worker.atomic_json(receipt, {"snapshot_sha256": "snapshot", "notes": []})
        self.state["active_run"] = "test"
        with patch.object(worker, "lease", return_value=self.root), patch.object(
            worker,
            "run_pi",
            side_effect=lambda *a, **k: worker.atomic_json(
                receipt, {"snapshot_sha256": "snapshot", "notes": []}
            ),
        ) as model, patch.object(worker, "api") as api:
            with self.assertRaisesRegex(ValueError, "every issue"):
                worker.execute(
                    self.config, self.state, self.state_path, 1, MODULE_PATH.parent
                )
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(list(receipt.parent.glob("invalid-receipt-*.json"))), 1)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
