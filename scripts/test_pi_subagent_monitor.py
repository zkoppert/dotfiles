"""Exercise durable monitoring with real processes, files, and CLI entry points."""

import datetime
import json
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pi_subagent_monitor as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.artifacts = self.root / "task"
        self.artifacts.mkdir()
        self.session = self.artifacts / "session.jsonl"
        self.session.write_text("session fixture\n")
        self.parent = self.root / "parent.jsonl"
        self.parent.touch()
        self.manifest = self.root / "launches.json"
        self.manifest.write_text(json.dumps([{
            "issue": 123, "name": "Fixture task", "pid": 99999999,
            "artifacts": str(self.artifacts),
        }]))

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def register(self):
        return monitor.register(self.state, self.manifest, self.parent)

    def poll(self, now=None):
        return monitor.poll(self.state, now=now, notifications=False)

    def events(self):
        return list((self.state / "inbox").rglob("*.json"))

    def test_exit_with_result_is_not_success_and_alerts_once(self):
        (self.artifacts / "result.md").write_text("All done!\n")
        self.register()
        self.assertEqual(self.poll()[0]["state"], "stopped")
        self.assertEqual(len(self.events()), 1)
        self.poll()
        self.assertEqual(len(self.events()), 1)
        event = json.loads(self.events()[0].read_text())
        self.assertIn("not proof", event["updates"][0]["summary"])
        self.assertEqual(event["parent_session"], str(self.parent))

    def test_reported_blocker_and_recovery_emit_distinct_events(self):
        self.register()
        monitor.report(self.artifacts, "blocked", "Validation capacity unavailable")
        self.assertEqual(self.poll()[0]["state"], "blocked")
        monitor.report(self.artifacts, "needs_review", "Original failure no longer reproduces")
        self.assertEqual(self.poll()[0]["state"], "needs_review")
        self.assertEqual(len(self.events()), 2)

    def test_completion_requires_evidence_and_passed_checks(self):
        with self.assertRaises(ValueError):
            monitor.report(self.artifacts, "complete", "Done")
        evidence = self.artifacts / "validation.md"
        evidence.write_text("Focused integration checks passed. CI is not applicable.\n")
        with self.assertRaises(ValueError):
            monitor.report(self.artifacts, "complete", "Done", evidence=evidence, validation="pending")
        monitor.report(self.artifacts, "complete", "Validated fixture", evidence=evidence,
                       validation="passed", ci="not_applicable")
        self.register()
        item = self.poll()[0]
        self.assertEqual(item["state"], "complete_reported")
        self.assertIn("not independently verified", item["summary"])

    def test_pr_opened_requires_safe_github_pull_url(self):
        for url in ("", "file:///etc/passwd", "https://github.com.evil.test/o/r/pull/1", "https://github.com/o/r/issues/1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                monitor.report(self.artifacts, "pr_opened", "Draft exists", pr_url=url)
        monitor.report(self.artifacts, "pr_opened", "Draft exists", pr_url="https://github.com/example/project/pull/7")
        self.register()
        self.assertEqual(self.poll()[0]["pr_url"], "https://github.com/example/project/pull/7")

    def test_malformed_report_does_not_hide_other_tasks(self):
        self.register()
        (self.artifacts / "agent-status.json").write_text("{")
        self.assertEqual(self.poll()[0]["state"], "monitor_error")
        self.assertEqual(len(self.events()), 1)

    def test_real_process_becomes_stalled_then_stopped(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(self.session)])
        self.addCleanup(self.stop_process, process)
        items = json.loads(self.manifest.read_text())
        items[0]["pid"] = process.pid
        self.manifest.write_text(json.dumps(items))
        self.register()
        self.assertEqual(self.poll()[0]["state"], "running")
        self.assertEqual(self.poll(now=time.time() + 1801)[0]["state"], "stalled")
        process.terminate()
        process.wait(timeout=10)
        self.assertEqual(self.poll()[0]["state"], "stopped")

    def test_reused_process_identity_is_rejected(self):
        identity = {"pid": 12, "started": "old", "command": "original"}
        with patch.object(monitor, "process_identity", return_value={**identity, "started": "new"}):
            self.assertFalse(monitor.process_matches(identity))

    def test_process_title_change_does_not_report_live_agent_as_stopped(self):
        identity = {"pid": 12, "started": "Sat Nov 21 16:55:10 2026", "command": str(self.session)}
        launches = json.loads(self.manifest.read_text())
        launches[0]["pid"] = 12
        self.manifest.write_text(json.dumps(launches))
        with patch.object(monitor, "process_identity", return_value=identity):
            self.register()
        with patch.object(monitor, "process_identity", return_value={**identity, "command": "pi"}):
            self.assertEqual(self.poll()[0]["state"], "running")

    def test_registration_accepts_renamed_pi_only_with_matching_launch_time(self):
        identity = {"pid": 12, "started": time.strftime("%a %b %d %H:%M:%S %Y"), "command": "pi"}
        launches = json.loads(self.manifest.read_text())
        launches[0].update(pid=12, resumed_at=datetime.datetime.now(datetime.UTC).isoformat())
        self.manifest.write_text(json.dumps(launches))
        with patch.object(monitor, "process_identity", return_value=identity):
            self.register()
            self.assertEqual(self.poll()[0]["state"], "running")
        launches[0]["resumed_at"] = "2020-01-01T00:00:00Z"
        self.manifest.write_text(json.dumps(launches))
        with patch.object(monitor, "process_identity", return_value=identity):
            self.register()
            self.assertEqual(self.poll()[0]["state"], "stopped")

    def test_controller_waiting_keeps_batch_active(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(self.stop_process, process)
        self.register()
        (self.artifacts / "publication-continuation.json").write_text(json.dumps({
            "state": "waiting", "supervisor_pid": process.pid,
        }))
        self.assertEqual(self.poll()[0]["state"], "continuation_pending")
        process.terminate()
        process.wait(timeout=10)

    def test_reregister_preserves_notification_deduplication(self):
        self.register()
        self.poll()
        self.register()
        self.poll()
        self.assertEqual(len(self.events()), 1)

    def test_notifier_failure_is_retried_without_duplicate_inbox(self):
        self.register()
        with patch.object(monitor, "notify", side_effect=[RuntimeError("fixture failure"), None]) as notify:
            monitor.poll(self.state)
            monitor.poll(self.state)
            self.assertEqual(notify.call_count, 2)
        self.assertEqual(len(self.events()), 1)

    def test_cli_registration_report_poll_and_install(self):
        script = Path(monitor.__file__).resolve()
        def cli(*args):
            result = subprocess.run([sys.executable, str(script), "--state-root", str(self.state), *args],
                                    check=False, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        cli("register", "--manifest", str(self.manifest), "--parent-session", str(self.parent))
        cli("report", "--artifacts", str(self.artifacts), "--state", "blocked", "--summary", "Fixture blocker")
        self.assertEqual(cli("poll", "--no-notify")[0]["state"], "blocked")
        self.assertEqual(cli("status")[0]["state"], "blocked")
        home = self.root / "home"
        installed = cli("install", "--home", str(home), "--no-bootstrap")
        plist = plistlib.loads(Path(installed["plist"]).read_bytes())
        self.assertEqual(plist["StartInterval"], 60)
        self.assertTrue(plist["RunAtLoad"])
        self.assertIn("poll", plist["ProgramArguments"])
        self.assertTrue((home / ".agents/skills/subagent-monitor/SKILL.md").exists())
        self.assertTrue((home / ".pi/agent/extensions/subagent-monitor.ts").exists())
        self.assertTrue((home / ".local/bin/pi-subagent-monitor").is_file())
        again = cli("install", "--home", str(home), "--no-bootstrap")
        self.assertEqual(installed, again)

    def test_installer_preserves_unmanaged_extension(self):
        home = self.root / "home"
        target = home / ".pi/agent/extensions/subagent-monitor.ts"
        target.parent.mkdir(parents=True)
        target.write_text("// user-owned extension\n")
        with self.assertRaises(FileExistsError):
            monitor.install(home, self.state, bootstrap=False)
        self.assertEqual(target.read_text(), "// user-owned extension\n")


if __name__ == "__main__":
    unittest.main()
