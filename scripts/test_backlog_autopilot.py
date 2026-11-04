"""Behavioral contracts for the local backlog worker."""

import base64
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


class ScheduleTests(unittest.TestCase):
    def test_friday_boundary_and_host_timezone_independence(self):
        before = datetime.fromisoformat("2026-11-07T01:59:59+00:00")
        after = datetime.fromisoformat("2026-11-07T02:00:00+00:00")
        self.assertEqual(
            worker.latest_slot(before).isoformat(), "2026-10-30T18:00:00-07:00"
        )
        self.assertEqual(
            worker.latest_slot(after).isoformat(), "2026-11-06T18:00:00-08:00"
        )

    def test_spring_offset_changes_without_changing_wall_time(self):
        instant = datetime.fromisoformat("2026-03-14T01:00:00+00:00")
        self.assertEqual(
            worker.latest_slot(instant).isoformat(), "2026-03-13T18:00:00-07:00"
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
        self.assertEqual(observed, ["2026-11-06T18:00:00-08:00"])

    def test_manual_run_does_not_consume_friday_slot(self):
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
        self.assertEqual(run["outcome_counts"], {"blocked": 2})
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
            self.assertEqual(saved["last_slot"], "2026-10-30T18:00:00-07:00")
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
            worker, "command", side_effect=[json.dumps(rows), "", "returned"]
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
                "passed",
            ],
        ) as command:
            worker.verify_pull_requests(self.config, receipt)
        self.assertEqual(command.call_args.args[0], ["pr-marker", "check"])
        self.assertEqual(command.call_args.kwargs["cwd"], "/worktree")

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
