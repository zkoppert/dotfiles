"""macOS alerts report actionable changes without hourly duplicates or CI-wait noise."""

import __future__

import ast
import concurrent.futures
import copy
import json
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import dependabot_alerts as alerts

REAL_RUN = subprocess.run
REAL_LINT = alerts.lint_report
GIST_URL = "https://gist.github.com/operator/" + "a" * 32


def scope_policy(excluded=()):
    return {
        "organizations": ["example"],
        "repositories": [],
        "exclude_repositories": list(excluded),
    }


def report():
    return {
        "scope_confirmed": True,
        "scope": scope_policy(),
        "decisions": [
            {
                "repo": "example/repo",
                "number": 1,
                "sha": "abc",
                "action": "hold",
                "reason": "security review incomplete",
                "url": "https://github.com/example/repo/pull/1",
            }
        ],
        "errors": [],
    }


class AlertTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "report.json"
        gist = mock.patch.object(alerts, "publish_gist", return_value=GIST_URL)
        self.gist = gist.start()
        self.addCleanup(gist.stop)
        binary = mock.patch.object(
            alerts.shutil, "which", return_value="/mock/terminal-notifier"
        )
        binary.start()
        self.addCleanup(binary.stop)
        patch = mock.patch.object(
            alerts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        )
        self.run = patch.start()
        self.addCleanup(patch.stop)

    def snapshot(self):
        snapshots = (self.path.parent / "report-alerts").glob("*.json")
        return max(snapshots, key=lambda path: path.stat().st_mtime_ns).resolve()

    def test_stdlib_bootstrap_helper_reports_failure_and_confirms_delivery(self):
        self.assertEqual(
            0, alerts.main(["--bootstrap-error", "42", "--report", str(self.path)])
        )
        saved = json.loads(self.path.read_text())
        self.assertIn("exited 42 before worker startup", saved["errors"][0])
        self.assertFalse(saved["notification_pending"])
        self.assertEqual(1, self.run.call_count)
        self.assertIn(
            "bootstrap failure", json.loads(self.snapshot().read_text())["errors"][0]
        )

    def test_new_hold_is_clickable_and_unchanged_hold_stays_silent(self):
        payload = report()
        alerts.publish(payload, self.path)
        first = self.run.call_args.args[0]
        self.assertIn(GIST_URL, first)
        self.assertNotIn("-execute", first)
        alerts.publish(payload, self.path)
        self.assertEqual(1, self.run.call_count)
        self.assertEqual(payload, json.loads(self.path.read_text()))
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_gist_failure_keeps_delivery_pending_without_opening_a_local_file(self):
        self.gist.side_effect = alerts.AutomationError("network unavailable")
        alerts.publish(report(), self.path)
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved["notification_pending"])
        self.assertEqual(
            {}, json.loads(self.path.with_suffix(".alerts.json").read_text())
        )
        self.assertNotIn("-open", self.run.call_args.args[0])
        self.gist.side_effect = None
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)
        self.assertIn(GIST_URL, self.run.call_args.args[0])
        self.assertFalse(json.loads(self.path.read_text())["notification_pending"])

    def test_publication_fallback_identifies_startup_failure_and_suppresses_duplicate_help(
        self,
    ):
        self.gist.side_effect = alerts.AutomationError("network unavailable")
        for started in (False, True):
            with self.subTest(started=started):
                path = self.path.with_name(f"startup-{started}.json")
                receipt = self.path.parent / f"fallback-{started}"
                args = ["--bootstrap-error", "42", "--report", str(path)]
                if started:
                    args.append("--worker-started")
                before = self.run.call_count
                with mock.patch.dict(
                    alerts.os.environ, {"DEPENDABOT_FALLBACK_ALERT_SENT": str(receipt)}
                ):
                    self.assertEqual(0, alerts.main(args))
                    self.assertEqual(0, alerts.main(args))
                self.assertEqual(before + 1, self.run.call_count)
                self.assertTrue(receipt.is_file())
                saved = json.loads(path.read_text())
                self.assertTrue(saved["notification_pending"])
                self.assertIn(
                    "help report publication failed", " ".join(saved["errors"])
                )
                command = self.run.call_args.args[0]
                self.assertNotIn("-open", command)
                self.assertIn(
                    "stopped" if started else "couldn't start",
                    command[command.index("-message") + 1],
                )

    def test_excluded_previous_holds_are_not_published_during_an_unrelated_scan_error(
        self,
    ):
        alerts.publish(report(), self.path)
        partial = {
            "decisions": [],
            "errors": ["example/other: unavailable"],
            "excluded_repositories": ["example/repo"],
            "completed_repositories": [],
            "failed_prs": [],
        }
        partial["scope_confirmed"] = True
        partial["scope"] = scope_policy(["example/repo"])
        alerts.publish(partial, self.path)
        self.assertEqual([], partial["previous_holds"])
        self.assertEqual(1, len(alerts.actionable(partial)))
        bootstrap = {
            "decisions": [],
            "errors": ["bootstrap failure: worker unavailable"],
        }
        alerts.publish(bootstrap, self.path)
        self.assertEqual(["example/repo"], bootstrap["excluded_repositories"])
        self.assertEqual([], bootstrap["previous_holds"])

    def test_bootstrap_with_unknown_scope_keeps_prior_pr_details_local(self):
        alerts.publish(report(), self.path)
        startup = {
            "decisions": [],
            "errors": ["bootstrap failure: worker unavailable"],
            "scope_confirmed": False,
            "completed_repositories": ["example/repo"],
        }
        alerts.publish(startup, self.path)
        self.assertEqual(1, len(startup["previous_holds"]))
        body = alerts.report_markdown(self.gist.call_args.args[0])
        self.assertNotIn("example/repo", body)
        self.assertIn("withheld", body)
        self.assertEqual(1, len(alerts.actionable(startup)))
        self.assertIn(
            "example/repo#1@abc",
            json.loads(self.path.with_suffix(".alerts.json").read_text()),
        )
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)

    def test_exclusion_retires_history_during_errors_so_reinclusion_alerts(self):
        alerts.publish(report(), self.path)
        excluded = {
            "decisions": [],
            "errors": ["other: API unavailable"],
            "scope_confirmed": True,
            "scope": scope_policy(["example/repo"]),
            "excluded_repositories": ["example/repo"],
            "completed_repositories": [],
            "failed_prs": [],
        }
        alerts.publish(excluded, self.path)
        self.assertNotIn(
            "example/repo#1@abc",
            json.loads(self.path.with_suffix(".alerts.json").read_text()),
        )
        included = report()
        included.update(errors=excluded["errors"], excluded_repositories=[])
        alerts.publish(included, self.path)
        self.assertEqual(3, self.run.call_count)
        self.assertIn(
            "1 new or changed",
            self.run.call_args.args[0][
                self.run.call_args.args[0].index("-message") + 1
            ],
        )

    def test_removed_inclusions_retire_holds_and_history_during_unrelated_errors(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                path = self.path.with_name(f"removed-{explicit}.json")
                old = report()
                if explicit:
                    old["scope"] = {
                        "organizations": [],
                        "repositories": ["example/repo"],
                        "exclude_repositories": [],
                    }
                alerts.publish(old, path)
                removed = {
                    "scope_confirmed": True,
                    "scope": {
                        "organizations": [],
                        "repositories": ["other/one"],
                        "exclude_repositories": [],
                    },
                    "decisions": [],
                    "errors": ["other/one: unavailable"],
                    "completed_repositories": [],
                    "failed_prs": [],
                }
                alerts.publish(removed, path)
                self.assertEqual([], removed["previous_holds"])
                self.assertNotIn(
                    "example/repo", alerts.report_markdown(self.gist.call_args.args[0])
                )
                self.assertNotIn(
                    "example/repo#1@abc",
                    json.loads(path.with_suffix(".alerts.json").read_text()),
                )
                retained = {
                    **removed,
                    "scope": {
                        "organizations": [],
                        "repositories": ["example/repo"],
                        "exclude_repositories": [],
                    },
                    "decisions": [report()["decisions"][0]],
                }
                alerts.publish(retained, path)
                self.assertEqual(
                    1,
                    len(
                        [
                            key
                            for key in alerts.actionable(retained)
                            if not key.startswith("error:")
                        ]
                    ),
                )

    def test_new_revision_or_reason_sends_one_updated_alert(self):
        payload = report()
        alerts.publish(payload, self.path)
        payload["decisions"][0]["sha"] = "new"
        alerts.publish(payload, self.path)
        payload["decisions"][0]["reason"] = "new advisory"
        alerts.publish(payload, self.path)
        alerts.publish(payload, self.path)
        self.assertEqual(3, self.run.call_count)

    def test_pending_ci_rebase_waits_and_successes_do_not_alert(self):
        for action in ("wait", "rebase", "merged"):
            payload = report()
            payload["decisions"][0]["action"] = action
            alerts.publish(payload, self.path)
        self.run.assert_not_called()

    def test_multiple_holds_and_worker_errors_produce_one_private_summary(self):
        payload = report()
        another = copy.deepcopy(payload["decisions"][0])
        another["number"] = 2
        payload["decisions"].append(another)
        payload["errors"] = ["worker failure: authentication required"]
        alerts.publish(payload, self.path)
        alerts.publish(payload, self.path)
        self.assertEqual(1, self.run.call_count)
        command = self.run.call_args.args[0]
        self.assertEqual(
            (self.path.parent / "report-alerts").resolve(), self.snapshot().parent
        )
        self.assertEqual(
            payload["errors"], json.loads(self.snapshot().read_text())["errors"]
        )
        self.assertIn(
            "3 new or changed items need attention.",
            command[command.index("-message") + 1],
        )
        self.assertNotIn("example/repo", command[command.index("-message") + 1])

    def test_existing_error_keeps_a_new_pr_alert_linked_to_all_active_details(self):
        error = "example/other: access denied"
        alerts.publish({"decisions": [], "errors": [error]}, self.path)
        changed = report()
        changed["errors"] = [error]
        alerts.publish(changed, self.path)
        self.assertEqual(2, self.run.call_count)
        saved = json.loads(self.snapshot().read_text())
        self.assertEqual([error], saved["errors"])
        self.assertEqual("example/repo", saved["decisions"][0]["repo"])

    def test_failed_delivery_does_not_suppress_retry_and_records_the_failure(self):
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        payload = report()
        alerts.publish(payload, self.path)
        self.assertIn(
            "macOS help alert failed", json.loads(self.path.read_text())["errors"][0]
        )
        self.assertEqual(
            {}, json.loads(self.path.with_suffix(".alerts.json").read_text())
        )
        self.run.side_effect = None
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)
        self.assertTrue(self.path.with_suffix(".alerts.json").exists())

    def test_undelivered_worker_error_survives_a_later_successful_scan(self):
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        failed = {
            "decisions": [],
            "errors": ["worker failure: authentication required"],
        }
        alerts.publish(failed, self.path)
        self.assertTrue(json.loads(self.path.read_text())["notification_pending"])
        self.run.side_effect = None
        recovered = {"decisions": [], "errors": []}
        alerts.publish(recovered, self.path)
        self.assertEqual(
            ["worker failure: authentication required"], recovered["previous_errors"]
        )
        self.assertFalse(recovered["notification_pending"])
        self.assertEqual([], recovered["errors"])
        alerts.publish({"decisions": [], "errors": []}, self.path)
        self.assertEqual(2, self.run.call_count)

    def test_undelivered_hold_is_retried_through_waits_on_the_same_revision(self):
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        alerts.publish(report(), self.path)
        self.run.side_effect = None
        waiting = report()
        waiting["decisions"][0].update(action="wait", reason="CI pending")
        alerts.publish(waiting, self.path)
        self.assertEqual(2, self.run.call_count)
        self.assertEqual(
            "security review incomplete", waiting["previous_holds"][0]["reason"]
        )
        self.assertIn(GIST_URL, self.run.call_args.args[0])
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)

    def test_routine_reclassification_retires_matching_legacy_holds(self):
        for action, reason in (
            ("wait", "rebase already requested for this revision"),
            ("wait", "CI checks pending"),
            ("rebase", "branch needs a Dependabot rebase"),
        ):
            for delivered in (False, True):
                with self.subTest(action=action, reason=reason, delivered=delivered):
                    path = self.path.with_name(f"{action}-{delivered}.json")
                    legacy = report()
                    legacy["decisions"][0]["reason"] = (
                        "rebase already requested for this revision"
                    )
                    self.run.side_effect = (
                        None
                        if delivered
                        else subprocess.TimeoutExpired("terminal-notifier", 15)
                    )
                    alerts.publish(legacy, path)
                    self.run.side_effect = None
                    before = self.run.call_count
                    waiting = report()
                    waiting["decisions"][0].update(action=action, reason=reason)
                    alerts.publish(waiting, path)
                    alerts.publish(waiting, path)
                    self.assertEqual(before, self.run.call_count)
                    self.assertEqual([], waiting["previous_holds"])
                    self.assertEqual({}, alerts.actionable(waiting))
                    self.assertEqual([], json.loads(path.read_text())["previous_holds"])

    def test_already_carried_routine_hold_does_not_inflate_a_new_alert(self):
        legacy = report()["decisions"][0]
        legacy["reason"] = "rebase already requested for this revision"
        waiting = copy.deepcopy(legacy)
        waiting["action"] = "wait"
        alerts.save(
            self.path,
            {"decisions": [waiting], "previous_holds": [legacy], "errors": []},
        )
        payload = report()
        payload["decisions"][0].update(
            number=2, url="https://github.com/example/repo/pull/2"
        )
        payload["decisions"].append(waiting)
        alerts.publish(payload, self.path)
        self.assertEqual([], payload["previous_holds"])
        self.assertEqual(1, len(alerts.actionable(payload)))
        command = self.run.call_args.args[0]
        self.assertEqual(
            "1 new or changed item needs attention. Select to open the details.",
            command[command.index("-message") + 1],
        )
        self.assertEqual(
            GIST_URL,
            command[command.index("-open") + 1],
        )
        alerts.publish(payload, self.path)
        self.assertEqual(1, self.run.call_count)

    def test_reclassified_hold_can_alert_again_even_after_unrelated_delivery_failure(
        self,
    ):
        for fail_delivery in (False, True):
            with self.subTest(fail_delivery=fail_delivery):
                path = self.path.with_name(f"recurrence-{fail_delivery}.json")
                legacy = report()
                legacy["decisions"][0]["reason"] = (
                    "rebase already requested for this revision"
                )
                alerts.publish(copy.deepcopy(legacy), path)
                before = self.run.call_count
                waiting = copy.deepcopy(legacy)
                waiting["decisions"][0]["action"] = "wait"
                waiting.update(completed_repositories=["example/repo"], failed_prs=[])
                if fail_delivery:
                    waiting["errors"] = ["example/other: unavailable"]
                    self.run.side_effect = subprocess.TimeoutExpired(
                        "terminal-notifier", 15
                    )
                alerts.publish(waiting, path)
                history = json.loads(path.with_suffix(".alerts.json").read_text())
                self.assertNotIn("example/repo#1@abc", history)
                self.run.side_effect = None
                alerts.publish(copy.deepcopy(legacy), path)
                self.assertEqual(before + 1 + fail_delivery, self.run.call_count)
                alerts.publish(copy.deepcopy(legacy), path)
                self.assertEqual(before + 1 + fail_delivery, self.run.call_count)

    def test_observed_routine_wait_retires_legacy_history_during_partial_scan(self):
        legacy = report()
        legacy["decisions"][0]["reason"] = "rebase already requested for this revision"
        alerts.publish(copy.deepcopy(legacy), self.path)
        partial = copy.deepcopy(legacy)
        partial["decisions"][0]["action"] = "wait"
        partial.update(
            errors=["worker failure: interrupted before repository completion"],
            completed_repositories=[],
            failed_prs=[],
        )
        alerts.publish(partial, self.path)
        self.assertEqual([], partial["previous_holds"])
        history = json.loads(self.path.with_suffix(".alerts.json").read_text())
        self.assertNotIn("example/repo#1@abc", history)
        self.assertEqual(1, len(alerts.actionable(partial)))
        alerts.publish(copy.deepcopy(legacy), self.path)
        self.assertEqual(3, self.run.call_count)

    def test_undelivered_hold_clears_after_a_new_revision_or_resolution(self):
        for replacement in (
            {
                "decisions": [],
                "errors": [],
                "scope_confirmed": True,
                "scope": scope_policy(),
            },
            report(),
        ):
            self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
            alerts.publish(report(), self.path)
            self.run.side_effect = None
            if replacement["decisions"]:
                replacement["decisions"][0].update(
                    action="wait", sha="new", reason="CI pending"
                )
            before = self.run.call_count
            alerts.publish(replacement, self.path)
            self.assertEqual(before, self.run.call_count)
            self.assertEqual([], replacement["previous_holds"])

    def test_failure_report_receipt_requires_a_persisted_error_report(self):
        receipt = self.path.parent / "reported"
        with mock.patch.dict(
            alerts.os.environ, {"DEPENDABOT_FAILURE_REPORTED": str(receipt)}
        ):
            alerts.publish({"decisions": [], "errors": []}, self.path)
            self.assertFalse(receipt.exists())
            alerts.publish(
                {"decisions": [], "errors": ["worker failure: unexpected exit"]},
                self.path,
            )
            self.assertTrue(receipt.is_file())
            self.assertIn(
                "unexpected exit", json.loads(self.path.read_text())["errors"][0]
            )

    def test_started_worker_fallback_does_not_call_it_a_bootstrap_failure(self):
        self.assertEqual(
            0,
            alerts.main(
                [
                    "--bootstrap-error",
                    "42",
                    "--worker-started",
                    "--report",
                    str(self.path),
                ]
            ),
        )
        saved = json.loads(self.path.read_text())
        self.assertIn("without a failure report", saved["errors"][0])
        self.assertNotIn("before worker startup", saved["errors"][0])

    def test_waits_do_not_erase_the_fingerprint_of_an_unresolved_hold(self):
        alerts.publish(report(), self.path)
        for action in ("wait", "rebase"):
            waiting = report()
            waiting["decisions"][0].update(action=action, reason="routine wait")
            alerts.publish(waiting, self.path)
            alerts.publish(report(), self.path)
        self.assertEqual(1, self.run.call_count)

    def test_concurrent_failure_publication_cannot_overwrite_an_undelivered_error(self):
        first = {"decisions": [], "errors": ["worker failure: transient auth"]}
        second = {"decisions": [], "errors": []}
        entered, release, attempted, second_inside = (
            threading.Event() for _ in range(4)
        )
        original_publish, original_notify = alerts._publish, alerts.notify

        def tracked(payload, path):
            if payload is second:
                second_inside.set()
            original_publish(payload, path)

        def gated(payload, path):
            if payload is first:
                payload["notification_attempted"] = True
                entered.set()
                release.wait(5)
                raise OSError("delivery unavailable")
            original_notify(payload, path)

        def run_second():
            attempted.set()
            alerts.publish(second, self.path)

        with (
            mock.patch.object(alerts, "_publish", side_effect=tracked),
            mock.patch.object(alerts, "notify", side_effect=gated),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor,
        ):
            one = executor.submit(alerts.publish, first, self.path)
            self.assertTrue(entered.wait(5))
            two = executor.submit(run_second)
            try:
                self.assertTrue(attempted.wait(5))
                self.assertFalse(second_inside.wait(0.2))
            finally:
                release.set()
            one.result(timeout=5)
            two.result(timeout=5)
        self.assertEqual(["worker failure: transient auth"], second["previous_errors"])
        self.assertFalse(second["notification_pending"])
        self.assertEqual(1, self.run.call_count)

    def test_recovered_error_can_recur_immediately_after_its_queued_alert_is_delivered(
        self,
    ):
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        alerts.publish({"decisions": [], "errors": ["worker failure: A"]}, self.path)
        self.run.side_effect = None
        alerts.publish({"decisions": [], "errors": []}, self.path)
        alerts.publish({"decisions": [], "errors": ["worker failure: A"]}, self.path)
        self.assertEqual(3, self.run.call_count)

    def test_error_recurrence_alerts_even_when_an_unrelated_error_persists(self):
        for errors in (
            ["example/a: unavailable"],
            ["example/b: unavailable"],
            ["example/a: unavailable", "example/b: unavailable"],
        ):
            alerts.publish({"decisions": [], "errors": errors}, self.path)
        self.assertEqual(3, self.run.call_count)
        self.assertEqual(2, len(json.loads(self.snapshot().read_text())["errors"]))

    def test_history_storage_failure_is_reported_even_when_only_waits_exist(self):
        history = self.path.with_suffix(".alerts.json")
        history.mkdir()
        waiting = report()
        waiting["decisions"][0].update(action="wait", reason="CI pending")
        alerts.publish(copy.deepcopy(waiting), self.path)
        self.assertIn(
            "alert history persistence failed",
            json.loads(self.path.read_text())["errors"][0],
        )
        self.assertEqual(1, self.run.call_count)
        command = self.run.call_args.args[0]
        self.assertIn(
            "couldn't save alert history", command[command.index("-message") + 1]
        )
        alerts.publish(copy.deepcopy(waiting), self.path)
        self.assertEqual(2, self.run.call_count)
        self.assertIn(
            "alert history persistence failed",
            json.loads(self.snapshot().read_text())["previous_errors"][0],
        )
        history.rmdir()
        alerts.publish(copy.deepcopy(waiting), self.path)
        delivered = self.run.call_count
        alerts.publish(copy.deepcopy(waiting), self.path)
        self.assertEqual(delivered, self.run.call_count)
        self.assertEqual({}, json.loads(history.read_text()))

    def test_completed_repository_resolution_retires_holds_despite_unrelated_errors(
        self,
    ):
        alerts.publish(report(), self.path)
        partial = {
            "decisions": [],
            "errors": ["example/other: unavailable"],
            "completed_repositories": ["example/repo"],
            "failed_prs": [],
        }
        partial["scope_confirmed"] = True
        partial["scope"] = scope_policy()
        alerts.publish(partial, self.path)
        reopened = report()
        reopened.update(
            errors=partial["errors"],
            completed_repositories=["example/repo"],
            failed_prs=[],
        )
        alerts.publish(reopened, self.path)
        self.assertEqual(3, self.run.call_count)

    def test_failed_pr_observation_retains_its_hold_despite_completed_repository_listing(
        self,
    ):
        alerts.publish(report(), self.path)
        partial = {
            "decisions": [],
            "errors": ["example/repo#1: unavailable"],
            "scope_confirmed": True,
            "scope": scope_policy(),
            "completed_repositories": ["example/repo"],
            "failed_prs": ["example/repo#1"],
        }
        alerts.publish(partial, self.path)
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)

    def test_completed_resolution_clears_undelivered_holds_during_unrelated_errors(
        self,
    ):
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        alerts.publish(report(), self.path)
        self.run.side_effect = None
        partial = {
            "decisions": [],
            "errors": ["example/other: unavailable"],
            "completed_repositories": ["example/repo"],
            "failed_prs": [],
        }
        partial["scope_confirmed"] = True
        partial["scope"] = scope_policy()
        alerts.publish(partial, self.path)
        self.assertEqual([], partial["previous_holds"])
        self.assertEqual(2, self.run.call_count)

    def test_unwritable_report_still_attempts_one_generic_private_help_alert(self):
        self.path.mkdir()
        payload = {"decisions": [], "errors": ["worker failure: private details"]}
        receipt = self.path.parent / "emergency"
        with mock.patch.dict(
            alerts.os.environ, {"DEPENDABOT_FALLBACK_ALERT_SENT": str(receipt)}
        ):
            for _ in range(2):
                with self.assertRaises(OSError):
                    alerts.publish(payload, self.path)
        self.assertEqual(1, self.run.call_count)
        command = self.run.call_args.args[0]
        self.assertIn(
            "couldn't save its report", command[command.index("-message") + 1]
        )
        self.assertNotIn("private details", " ".join(command))
        self.assertTrue(receipt.is_file())
        self.assertTrue(payload["fallback_alert_sent"])
        self.assertFalse(self.path.with_suffix(".alerts.json").exists())
        self.assertTrue(self.path.is_dir())

    def test_bootstrap_helper_accepts_generic_delivery_when_report_storage_is_broken(
        self,
    ):
        self.path.mkdir()
        self.assertEqual(
            0, alerts.main(["--bootstrap-error", "42", "--report", str(self.path)])
        )
        self.assertEqual(1, self.run.call_count)
        self.assertFalse(self.path.with_suffix(".alerts.json").exists())

    def test_revision_change_retires_an_old_hold_even_if_the_branch_returns(self):
        for index, errors in enumerate(([], ["example/other: unavailable"])):
            path = self.path.with_name(f"revision-{index}.json")
            before = self.run.call_count
            alerts.publish(report(), path)
            waiting = report()
            waiting["decisions"][0].update(
                action="wait", sha="new", reason="CI pending"
            )
            waiting["errors"] = errors
            alerts.publish(waiting, path)
            reverted = report()
            reverted["errors"] = errors
            alerts.publish(reverted, path)
            self.assertEqual(2 + bool(errors), self.run.call_count - before)

    def test_fallback_helper_supports_python38_syntax_and_postpones_annotations(self):
        source = Path(alerts.__file__).read_text()
        ast.parse(source, feature_version=(3, 8))
        compiled = compile(source, str(alerts.__file__), "exec")
        self.assertTrue(compiled.co_flags & __future__.annotations.compiler_flag)

    def test_actual_helper_imports_and_publishes_with_the_system_interpreter(self):
        python = Path("/usr/bin/python3")
        if not python.exists():
            python = Path(sys.executable)
        code = (
            "import importlib.util,sys,pathlib\n"
            "sys.path.insert(0,str(pathlib.Path(sys.argv[1]).parent))\n"
            "s=importlib.util.spec_from_file_location('fallback_contract',sys.argv[1])\n"
            "m=importlib.util.module_from_spec(s)\ns.loader.exec_module(m)\n"
            "m.subprocess.run=lambda *a,**k:None\n"
            "m.shutil.which=lambda name:'/mock/notifier'\n"
            f"m.publish_gist=lambda *args:{GIST_URL!r}\n"
            "sys.exit(m.main(['--bootstrap-error','42','--report',sys.argv[2]]))\n"
        )
        result = REAL_RUN(
            [str(python), "-c", code, alerts.__file__, str(self.path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(json.loads(self.path.read_text())["notification_pending"])

    def test_resolved_hold_stays_retired_when_an_unrelated_delivery_fails(self):
        alerts.publish(report(), self.path)
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        alerts.publish(
            {
                "decisions": [],
                "errors": ["example/other: unavailable"],
                "scope_confirmed": True,
                "scope": scope_policy(),
                "completed_repositories": ["example/repo"],
                "failed_prs": [],
            },
            self.path,
        )
        self.run.side_effect = None
        alerts.publish(report(), self.path)
        self.assertEqual(3, self.run.call_count)
        saved = json.loads(self.snapshot().read_text())
        self.assertEqual("example/repo", saved["decisions"][0]["repo"])

    def test_history_retirement_and_delivery_failure_cannot_suppress_recurrence(self):
        alerts.publish(report(), self.path)
        changed = report()
        changed["decisions"][0].update(
            repo="example/other",
            number=2,
            url="https://github.com/example/other/pull/2",
        )
        changed["completed_repositories"] = ["example/repo", "example/other"]
        history = self.path.with_suffix(".alerts.json")
        original_save = alerts.save

        def fail_history(path, payload):
            if path == history:
                raise OSError("history unavailable")
            original_save(path, payload)

        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        with mock.patch.object(alerts, "save", side_effect=fail_history):
            alerts.publish(changed, self.path)
        self.assertTrue(json.loads(self.path.read_text())["alert_history_invalid"])
        self.run.side_effect = None
        alerts.publish(report(), self.path)
        self.assertEqual(3, self.run.call_count)
        self.assertFalse(json.loads(self.path.read_text())["alert_history_invalid"])

    def test_changed_security_findings_or_full_summary_update_the_alert(self):
        payload = report()
        payload["decisions"][0]["security_review"] = {
            "summary": "same summary",
            "findings": ["old concern"],
        }
        alerts.publish(payload, self.path)
        payload["decisions"][0]["security_review"]["findings"] = ["new concern"]
        alerts.publish(payload, self.path)
        payload["decisions"][0]["security_review"]["summary"] = "different full summary"
        alerts.publish(payload, self.path)
        alerts.publish(payload, self.path)
        self.assertEqual(3, self.run.call_count)

    def test_receipt_write_failure_does_not_duplicate_a_saved_failure_alert(self):
        started = self.path.parent / "started"
        started.touch()
        missing = self.path.parent / "missing" / "reported"
        with mock.patch.dict(
            alerts.os.environ,
            {
                "DEPENDABOT_WORKER_STARTED": str(started),
                "DEPENDABOT_FAILURE_REPORTED": str(missing),
            },
        ):
            alerts.publish(
                {"decisions": [], "errors": ["worker failure: already reported"]},
                self.path,
            )
            self.assertEqual(1, self.run.call_count)
            self.assertFalse(missing.exists())
            self.assertEqual(
                0,
                alerts.main(
                    [
                        "--bootstrap-error",
                        "1",
                        "--worker-started",
                        "--report",
                        str(self.path),
                    ]
                ),
            )
        self.assertEqual(1, self.run.call_count)
        self.assertEqual(str(started), json.loads(self.path.read_text())["run_receipt"])

    def test_old_failure_report_cannot_suppress_a_different_worker_invocation(self):
        with mock.patch.dict(
            alerts.os.environ, {"DEPENDABOT_WORKER_STARTED": "old-run"}
        ):
            alerts.publish(
                {"decisions": [], "errors": ["worker failure: old"]}, self.path
            )
        with mock.patch.dict(
            alerts.os.environ, {"DEPENDABOT_WORKER_STARTED": "new-run"}
        ):
            self.assertEqual(
                0,
                alerts.main(
                    [
                        "--bootstrap-error",
                        "1",
                        "--worker-started",
                        "--report",
                        str(self.path),
                    ]
                ),
            )
        self.assertEqual(2, self.run.call_count)
        self.assertIn(
            "without a failure report", json.loads(self.path.read_text())["errors"][0]
        )

    def test_snapshot_storage_failure_uses_the_saved_report_without_acknowledging_delivery(
        self,
    ):
        directory = self.path.parent / "report-alerts"
        directory.write_text("blocks snapshot directory creation")
        payload = {"decisions": [], "errors": ["worker failure: unavailable"]}
        alerts.publish(payload, self.path)
        self.assertEqual(1, self.run.call_count)
        command = self.run.call_args.args[0]
        self.assertNotIn("-open", command)
        self.assertIn(
            "couldn't publish its help report", command[command.index("-message") + 1]
        )
        self.assertTrue(json.loads(self.path.read_text())["notification_pending"])
        self.assertEqual(
            {}, json.loads(self.path.with_suffix(".alerts.json").read_text())
        )

    def test_replacement_error_snapshot_includes_unobserved_unresolved_hold_details(
        self,
    ):
        alerts.publish(report(), self.path)
        failed_scan = {
            "decisions": [],
            "errors": ["example/repo: unavailable"],
            "completed_repositories": [],
            "failed_prs": [],
        }
        alerts.publish(failed_scan, self.path)
        snapshot = json.loads(self.snapshot().read_text())
        self.assertEqual("example/repo", snapshot["previous_holds"][0]["repo"])
        self.assertEqual("abc", snapshot["previous_holds"][0]["sha"])
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)

    def test_helper_retries_a_matching_report_saved_before_notification_delivery(self):
        receipt = self.path.parent / "started"
        alerts.save(
            self.path,
            {
                "decisions": [],
                "errors": ["worker failure: original"],
                "run_receipt": str(receipt),
                "notification_pending": True,
            },
        )
        with mock.patch.dict(
            alerts.os.environ, {"DEPENDABOT_WORKER_STARTED": str(receipt)}
        ):
            self.assertEqual(
                0,
                alerts.main(
                    [
                        "--bootstrap-error",
                        "1",
                        "--worker-started",
                        "--report",
                        str(self.path),
                    ]
                ),
            )
        saved = json.loads(self.path.read_text())
        self.assertEqual(1, self.run.call_count)
        self.assertFalse(saved["notification_pending"])
        self.assertIn("worker failure: original", saved["errors"])
        self.assertTrue(
            any(
                "unfinished notification delivery" in error for error in saved["errors"]
            )
        )

    def test_real_nonzero_notifier_exit_is_recorded_and_retried(self):
        executable = self.path.parent / "notifier"
        executable.write_text("#!/bin/sh\nexit 7\n")
        executable.chmod(0o700)
        actual_exists = alerts.Path.exists

        def exists(path):
            return (
                False
                if str(path) == "/opt/homebrew/bin/terminal-notifier"
                else actual_exists(path)
            )

        with (
            mock.patch.object(alerts.Path, "exists", new=exists),
            mock.patch.object(alerts.shutil, "which", return_value=str(executable)),
            mock.patch.object(alerts.subprocess, "run", side_effect=REAL_RUN),
        ):
            alerts.publish(report(), self.path)
            saved = json.loads(self.path.read_text())
            self.assertTrue(saved["notification_pending"])
            self.assertTrue(any("exit status 7" in error for error in saved["errors"]))
            self.assertEqual(
                {}, json.loads(self.path.with_suffix(".alerts.json").read_text())
            )
            executable.write_text("#!/bin/sh\nexit 0\n")
            alerts.publish(report(), self.path)
        self.assertFalse(json.loads(self.path.read_text())["notification_pending"])
        self.assertEqual(
            1, len(json.loads(self.path.with_suffix(".alerts.json").read_text()))
        )

    def test_interruption_after_a_clean_report_cannot_hide_a_recurring_error(self):
        alerts.publish({"decisions": [], "errors": ["worker failure: A"]}, self.path)
        with (
            mock.patch.object(alerts, "notify", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            alerts.publish({"decisions": [], "errors": []}, self.path)
        self.assertTrue(json.loads(self.path.read_text())["alert_history_invalid"])
        alerts.publish({"decisions": [], "errors": ["worker failure: A"]}, self.path)
        self.assertEqual(2, self.run.call_count)
        self.assertFalse(json.loads(self.path.read_text())["alert_history_invalid"])

    def test_republishing_a_reused_report_does_not_repeat_recovered_errors(self):
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        alerts.publish({"decisions": [], "errors": ["worker failure: A"]}, self.path)
        self.run.side_effect = None
        recovered = {"decisions": [], "errors": []}
        alerts.publish(recovered, self.path)
        self.assertIn("worker failure: A", recovered["previous_errors"])
        alerts.publish(recovered, self.path)
        self.assertEqual(2, self.run.call_count)
        self.assertNotIn("previous_errors", json.loads(self.path.read_text()))

    def test_partial_scan_failure_does_not_make_an_unchanged_hold_look_new(self):
        alerts.publish(report(), self.path)
        alerts.publish(
            {"decisions": [], "errors": ["example/repo: API unavailable"]}, self.path
        )
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)

    def test_distinct_undelivered_errors_with_the_same_prefix_are_not_overwritten(self):
        alerts.publish({"decisions": [], "errors": ["worker failure: B"]}, self.path)
        self.run.side_effect = subprocess.TimeoutExpired("terminal-notifier", 15)
        alerts.publish({"decisions": [], "errors": ["worker failure: A"]}, self.path)
        self.run.side_effect = None
        recovered = {"decisions": [], "errors": ["worker failure: B"]}
        alerts.publish(recovered, self.path)
        self.assertEqual(3, self.run.call_count)
        self.assertEqual(["worker failure: A"], recovered["previous_errors"])
        self.assertFalse(recovered["notification_pending"])
        snapshot = json.loads(self.snapshot().read_text())
        self.assertEqual(["worker failure: A"], snapshot["previous_errors"])
        self.assertEqual(["worker failure: B"], snapshot["errors"])

    def test_alert_snapshot_keeps_its_error_after_the_latest_report_is_replaced(self):
        alerts.publish(
            {"decisions": [], "errors": ["worker failure: authentication required"]},
            self.path,
        )
        target = self.snapshot()
        alerts.publish({"decisions": [], "errors": []}, self.path)
        self.assertEqual([], json.loads(self.path.read_text())["errors"])
        self.assertEqual(
            ["worker failure: authentication required"],
            json.loads(target.read_text())["errors"],
        )
        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
        self.assertEqual(1, self.run.call_count)

    def test_missing_notifier_is_recorded_and_not_marked_delivered(self):
        with (
            mock.patch.object(alerts.Path, "exists", return_value=False),
            mock.patch.object(alerts.shutil, "which", return_value=None),
        ):
            alerts.publish(report(), self.path)
        self.assertIn(
            "terminal-notifier is unavailable",
            json.loads(self.path.read_text())["errors"][0],
        )
        self.assertEqual(
            {}, json.loads(self.path.with_suffix(".alerts.json").read_text())
        )
        self.run.assert_not_called()

    def test_resolved_then_recurring_hold_can_alert_again(self):
        alerts.publish(report(), self.path)
        alerts.publish(
            {
                "decisions": [],
                "errors": [],
                "scope_confirmed": True,
                "scope": scope_policy(),
            },
            self.path,
        )
        alerts.publish(report(), self.path)
        self.assertEqual(2, self.run.call_count)

    def test_corrupt_state_does_not_hide_actionable_items(self):
        self.path.with_suffix(".alerts.json").write_text("not JSON")
        alerts.publish(report(), self.path)
        self.assertEqual(1, self.run.call_count)
        self.assertEqual(
            1, len(json.loads(self.path.with_suffix(".alerts.json").read_text()))
        )

    def test_untrusted_destination_is_not_used_by_the_secret_gist_alert(self):
        payload = report()
        payload["decisions"][0]["url"] = "file:///private/secret"
        alerts.publish(payload, self.path)
        self.assertEqual(
            "hold", json.loads(self.snapshot().read_text())["decisions"][0]["action"]
        )
        self.assertNotIn("file:///private/secret", self.run.call_args.args[0])


class FakeGists:
    def __init__(self):
        self.owner = "operator"
        self.gists = {}
        self.writes = []
        self.ambiguous_create = False
        self.truncate = False
        self.ignore_patch = False

    def pages(self, path):
        assert path == "/gists"
        return copy.deepcopy(list(self.gists.values()))

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/user":
            return {"login": self.owner}
        if method == "POST" and path == "/gists":
            self.writes.append((method, copy.deepcopy(payload)))
            identity = "a" * 32
            value = {
                "id": identity,
                "public": payload["public"],
                "description": payload["description"],
                "owner": {"login": self.owner},
                "html_url": GIST_URL,
                "truncated": False,
                "files": copy.deepcopy(payload["files"]),
            }
            self.gists[identity] = value
            if self.ambiguous_create:
                self.ambiguous_create = False
                raise alerts.AutomationError("response lost after creation")
            return copy.deepcopy(value)
        identity = path.removeprefix("/gists/")
        if method == "GET":
            value = copy.deepcopy(self.gists[identity])
            if self.truncate:
                value["files"]["dependabot-help.md"]["truncated"] = True
            return value
        if method == "PATCH":
            self.writes.append((method, copy.deepcopy(payload)))
            if not self.ignore_patch:
                self.gists[identity]["files"].update(copy.deepcopy(payload["files"]))
            return copy.deepcopy(self.gists[identity])
        raise AssertionError((method, path))


class GistTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "report.json"
        self.client = FakeGists()
        patch = mock.patch.object(alerts, "GitHub", return_value=self.client)
        patch.start()
        self.addCleanup(patch.stop)
        lint = mock.patch.object(alerts, "lint_report")
        self.lint = lint.start()
        self.addCleanup(lint.stop)

    def test_secret_creation_update_and_full_readback_keep_one_owned_gist(self):
        payload = report()
        payload["finished_at"] = "2026-11-18T07:00:00Z"
        self.assertEqual(GIST_URL, alerts.publish_gist(payload, self.path))
        changed = copy.deepcopy(payload)
        changed["decisions"][0]["reason"] = "CI checks absent"
        self.assertEqual(GIST_URL, alerts.publish_gist(changed, self.path))
        creates = [p for method, p in self.client.writes if method == "POST"]
        self.assertEqual(1, len(creates))
        self.assertIs(False, creates[0]["public"])
        body = self.client.gists["a" * 32]["files"]["dependabot-help.md"]["content"]
        self.assertIn("No successful CI checks were reported", body)
        self.assertIn("https://github.com/example/repo/pull/1", body)
        self.assertEqual(alerts.report_markdown(changed), body)
        self.assertEqual(
            0o600, stat.S_IMODE(self.path.with_suffix(".gist.json").stat().st_mode)
        )
        self.assertEqual(2, self.lint.call_count)

    def test_ambiguous_creation_is_recovered_without_a_duplicate(self):
        self.client.ambiguous_create = True
        with self.assertRaises(alerts.AutomationError):
            alerts.publish_gist(report(), self.path)
        self.assertEqual(GIST_URL, alerts.publish_gist(report(), self.path))
        self.assertEqual(1, sum(method == "POST" for method, _ in self.client.writes))

    def test_public_foreign_or_unmanaged_gists_cannot_be_updated(self):
        alerts.publish_gist(report(), self.path)
        original = copy.deepcopy(self.client.gists["a" * 32])
        for field, value in (
            ("public", True),
            ("owner", {"login": "another"}),
            ("description", "unmanaged"),
        ):
            with self.subTest(field=field):
                self.client.gists["a" * 32] = {**original, field: value}
                before = len(self.client.writes)
                with self.assertRaises(ValueError):
                    alerts.publish_gist(report(), self.path)
                self.assertEqual(before, len(self.client.writes))

    def test_only_owned_identity_determines_the_browser_url(self):
        alerts.publish_gist(report(), self.path)
        for value in ("https://gist.github.com/" + "a" * 32, "file:///private/log"):
            self.client.gists["a" * 32]["html_url"] = value
            self.assertEqual(GIST_URL, alerts.publish_gist(report(), self.path))

    def test_style_failure_diagnostics_are_kept_in_the_private_log(self):
        error = subprocess.CalledProcessError(
            1, "style", output="style-output-marker", stderr="style-error-marker"
        )
        with (
            mock.patch.object(alerts.subprocess, "run", side_effect=error),
            self.assertLogs(alerts.LOG, level="ERROR") as captured,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            REAL_LINT("# Report")
        self.assertIn("style-output-marker", " ".join(captured.output))
        self.assertIn("style-error-marker", " ".join(captured.output))

    def test_known_policy_holds_have_specific_explanations(self):
        for reason, expected in (
            (
                "existing deferred auto-merge needs human attention",
                "existing auto-merge request",
            ),
            (
                "waiting for branch policy after approval",
                "branch protections are not yet satisfied",
            ),
            ("dependency excluded by existing safety policy", "separate safety policy"),
        ):
            payload = report()
            payload["decisions"][0]["reason"] = reason
            self.assertIn(expected, alerts.report_markdown(payload))

    def test_cached_identity_cannot_resolve_to_another_owned_managed_gist(self):
        alerts.publish_gist(report(), self.path)
        self.client.gists["a" * 32].update(
            id="b" * 32,
            html_url="https://gist.github.com/operator/" + "b" * 32,
        )
        before = len(self.client.writes)
        with self.assertRaises(ValueError):
            alerts.publish_gist(report(), self.path)
        self.assertEqual(before, len(self.client.writes))

    def test_readback_truncation_or_corrupt_state_prevents_delivery(self):
        alerts.publish_gist(report(), self.path)
        self.client.truncate = True
        with self.assertRaisesRegex(ValueError, "differs"):
            alerts.publish_gist(report(), self.path)
        self.path.with_suffix(".gist.json").write_text("not JSON")
        before = len(self.client.writes)
        with self.assertRaises(ValueError):
            alerts.publish_gist(report(), self.path)
        self.assertEqual(before, len(self.client.writes))

    def test_success_response_without_the_requested_content_is_not_accepted(self):
        payload = report()
        alerts.publish_gist(payload, self.path)
        self.client.ignore_patch = True
        payload["decisions"][0]["reason"] = "CI checks absent"
        with self.assertRaisesRegex(ValueError, "differs"):
            alerts.publish_gist(payload, self.path)

    def test_report_byte_limit_accepts_boundary_and_rejects_the_next_character(self):
        with mock.patch.object(alerts, "report_markdown", return_value="é" * 64000):
            self.assertEqual(GIST_URL, alerts.publish_gist(report(), self.path))
        before = len(self.client.writes)
        with (
            mock.patch.object(alerts, "report_markdown", return_value="é" * 64001),
            self.assertRaisesRegex(ValueError, "too large"),
        ):
            alerts.publish_gist(report(), self.path)
        self.assertEqual(before, len(self.client.writes))

    def test_style_failure_prevents_all_remote_writes(self):
        self.lint.side_effect = subprocess.CalledProcessError(1, "style")
        with self.assertRaises(subprocess.CalledProcessError):
            alerts.publish_gist(report(), self.path)
        self.assertEqual([], self.client.writes)

    def test_renderer_omits_scope_raw_logs_findings_and_patches(self):
        payload = report()
        payload.update(
            repositories=["hidden/scope-secret"],
            excluded_repositories=["hidden/exclusion-secret"],
            errors=["ENETUNREACH token-secret"],
        )
        payload["decisions"][0].update(
            reason="security review unavailable: credential-secret",
            security_review={
                "findings": ["finding-secret"],
                "patches": ["patch-secret"],
            },
        )
        body = alerts.report_markdown(payload)
        for secret in (
            "scope-secret",
            "exclusion-secret",
            "token-secret",
            "credential-secret",
            "finding-secret",
            "patch-secret",
        ):
            self.assertNotIn(secret, body)
        self.assertIn("security review could not finish", body)
        self.assertIn("network or package-feed problem", body)
        payload["decisions"][0]["url"] = "https://evil.example/"
        self.assertNotIn("evil.example", alerts.report_markdown(payload))
        payload["decisions"][0]["repo"] = "example/repo](https://evil.example)"
        with self.assertRaises(ValueError):
            alerts.report_markdown(payload)


if __name__ == "__main__":
    unittest.main()
