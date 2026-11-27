"""Policy, safety, and real CLI-parser contracts for repository-based automation."""

import base64
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dependabot_alerts as alerts
import dependabot_automerge as bot

REPO = "example/tools"
CONFIG = {
    "enabled": True,
    "organizations": ["example"],
    "repositories": ["other/one"],
    "exclude_repositories": ["example/frozen"],
}


def pull():
    return {
        "number": 7,
        "title": "Bump package from 1.0.0 to 2.0.0",
        "body": "",
        "user": {"login": bot.BOT, "type": "Bot"},
        "state": "open",
        "draft": False,
        "head": {"sha": "abc123", "repo": {"full_name": REPO}},
        "base": {"sha": "base123", "repo": {"full_name": REPO}},
        "mergeable": True,
        "mergeable_state": "clean",
        "html_url": "https://github.com/example/tools/pull/7",
        "auto_merge": None,
    }


class FakeGitHub:
    def __init__(self):
        self.pr = pull()
        self.reviews = []
        self.comments = []
        self.inline_comments = []
        self.checks = [
            {"head_sha": "abc123", "status": "completed", "conclusion": "success"}
        ]
        self.statuses = []
        self.writes = []
        self.merge_response = {"merged": True}
        self.comparison_base = None

    def request(self, method, path, payload=None, expression="."):
        if method != "GET":
            self.writes.append((method, path, payload))
            return self.merge_response if path.endswith("/merge") else {}
        if "/compare/" in path:
            return {"merge_base": self.comparison_base or self.pr["base"]["sha"]}
        return copy.deepcopy(self.pr)

    def pages(self, path, expression="."):
        if "/check-runs?" in path:
            return copy.deepcopy(self.checks)
        if path.endswith("/statuses"):
            return copy.deepcopy(self.statuses)
        if path.endswith("/reviews"):
            return copy.deepcopy(self.reviews)
        if path.endswith("/comments"):
            return copy.deepcopy(
                self.inline_comments if "/pulls/" in path else self.comments
            )
        raise AssertionError(path)


class ConfigTests(unittest.TestCase):
    def load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text(content)
            return bot.load_config(path)

    def test_exact_repository_and_future_organization_repositories(self):
        config = self.load(
            "enabled: false\norganizations: [Example]\nrepositories: [Other/One]\n"
            "exclude_repositories: [example/frozen]\n"
        )
        for name in ("example/new-repo", "other/one", "EXAMPLE/UPPER"):
            self.assertTrue(bot.in_scope(name, config))
        for name in ("example-extra/tools", "other/two", "example/frozen"):
            self.assertFalse(bot.in_scope(name, config))

    def test_dot_prefixed_repository_can_be_included_and_excluded(self):
        included = self.load("enabled: true\nrepositories: [example/.github]\n")
        self.assertTrue(bot.in_scope("example/.github", included))
        excluded = self.load(
            "enabled: true\norganizations: [example]\n"
            "repositories: [example/.github]\n"
            "exclude_repositories: [Example/.GitHub]\n"
        )
        self.assertFalse(bot.in_scope("example/.github", excluded))
        self.assertTrue(bot.in_scope("example/future-repo", excluded))
        client = mock.Mock()
        client.pages.return_value = [
            {"full_name": "example/.github", "archived": False},
            {"full_name": "example/tools", "archived": False},
        ]
        self.assertEqual(["example/tools"], bot.discover(client, excluded))

    def test_dot_repository_support_does_not_allow_traversal_or_dot_owners(self):
        for name in (
            "example/.",
            "example/..",
            "example/../other",
            "example/.github/other",
            ".example/tools",
            "example/.*",
            "example/.github?ref=main",
        ):
            for key in ("repositories", "exclude_repositories"):
                with (
                    self.subTest(name=name, key=key),
                    self.assertRaises(bot.AutomationError),
                ):
                    self.load(
                        f"enabled: true\norganizations: [example]\n{key}: ['{name}']\n"
                    )
        with self.assertRaises(bot.AutomationError):
            self.load("enabled: true\norganizations: [.example]\n")

    def test_missing_empty_malformed_or_unknown_config_never_broadens_scope(self):
        for content in (
            "",
            "[]",
            "enabled: true",
            "enabled: 'false'\norganizations: [example]",
            "enabled: true\nrepositories: ['example/*']",
            "enabled: true\norganizations: example",
            "enabled: true\norganizations: [example]\nrepository: [other/one]",
        ):
            with self.subTest(content=content), self.assertRaises(bot.AutomationError):
                self.load(content)

    def test_discovery_includes_exact_repos_and_filters_archived_and_foreign(self):
        client = mock.Mock()
        client.pages.return_value = [
            {"full_name": name, "archived": archived}
            for name, archived in [
                (REPO, False),
                ("example/frozen", False),
                ("example/old", True),
                ("foreign/repo", False),
            ]
        ]
        self.assertEqual([REPO, "other/one"], bot.discover(client, CONFIG))

    def test_disabled_config_stops_before_github(self):
        with (
            mock.patch.object(
                bot, "load_config", return_value={**CONFIG, "enabled": False}
            ),
            mock.patch.object(bot, "GitHub") as client,
        ):
            self.assertEqual(0, bot.main([]))
        client.assert_not_called()


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeGitHub()
        self.reviewer = mock.patch.object(
            bot, "review", return_value={"decision": "allow", "summary": "Reviewed"}
        )
        self.review = self.reviewer.start()
        self.addCleanup(self.reviewer.stop)

    def handle(self, dry_run=False):
        return bot.handle(self.client, CONFIG, REPO, 7, "operator", dry_run)

    def test_all_stable_upgrade_kinds_are_eligible_without_a_coverage_setting(self):
        for target, kind in (
            ("1.0.1", "patch"),
            ("1.1.0", "minor"),
            ("2.0.0", "major"),
        ):
            self.client.pr["title"] = f"Bump package from 1.0.0 to {target}"
            result = self.handle(dry_run=True)
            self.assertEqual(("merge", kind), (result["action"], result["bump"]))
        self.assertEqual([], self.client.writes)

    def test_reports_never_publish_the_consumer_configuration_payload(self):
        self.review.return_value = {
            "decision": "allow",
            "summary": "Reviewed",
            "findings": [],
            "evidence": {
                "dependencies": [],
                "consumer_context": {
                    "files": [{"content": "private-configuration-canary"}]
                },
            },
        }
        result = self.handle(dry_run=True)
        self.assertEqual("merge", result["action"])
        self.assertNotIn("private-configuration-canary", json.dumps(result))
        self.assertNotIn("evidence", result["security_review"])
        self.assertEqual([], self.client.writes)

    def test_security_findings_or_incomplete_review_prevent_all_writes(self):
        self.review.return_value = {
            "decision": "hold",
            "summary": "New credential exfiltration",
        }
        self.assertEqual("hold", self.handle()["action"])
        for error in (
            ValueError("missing upstream source"),
            FileNotFoundError("copilot missing"),
            OSError("cache unavailable"),
        ):
            self.review.side_effect = error
            self.assertEqual("hold", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_reports_keep_review_identity_without_copying_source_patches(self):
        self.review.return_value = {
            "decision": "allow",
            "summary": "Reviewed",
            "evidence_sha256": "digest",
            "evidence": {
                "consumer_patches": ["sensitive_diff"],
                "dependencies": [
                    {"name": "package", "new_sha": "new", "patches": ["sensitive_diff"]}
                ],
            },
        }
        result = self.handle(dry_run=True)
        self.assertNotIn("sensitive_diff", json.dumps(result))
        self.assertEqual("digest", result["security_review"]["evidence_sha256"])
        self.assertEqual("new", result["security_review"]["dependencies"][0]["new_sha"])

    def test_merge_and_approval_use_the_evaluated_commit(self):
        result = self.handle()
        self.assertEqual("merged", result["action"])
        self.assertEqual(
            [
                (
                    "POST",
                    f"/repos/{REPO}/pulls/7/reviews",
                    {
                        "event": "APPROVE",
                        "commit_id": "abc123",
                        "body": "<!-- dependabot-automerge:review:abc123 -->",
                    },
                ),
                (
                    "PUT",
                    f"/repos/{REPO}/pulls/7/merge",
                    {"sha": "abc123", "merge_method": "squash"},
                ),
            ],
            self.client.writes,
        )

    def test_no_checks_skipped_checks_failing_pending_or_wrong_commit_never_approve(
        self,
    ):
        scenarios = [
            [],
            [{"head_sha": "abc123", "status": "completed", "conclusion": "skipped"}],
            [{"head_sha": "abc123", "status": "completed", "conclusion": "failure"}],
            [{"head_sha": "different", "status": "completed", "conclusion": "success"}],
        ]
        for checks in scenarios:
            self.client.checks = checks
            self.assertEqual("hold", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_pending_ci_waits_without_reviewing_or_approving(self):
        self.client.checks = [
            {"head_sha": "abc123", "status": "in_progress", "conclusion": None}
        ]
        self.assertEqual("wait", self.handle()["action"])
        self.review.assert_not_called()
        self.assertEqual([], self.client.writes)

    def test_known_failures_take_precedence_over_pending_checks(self):
        scenarios = [
            (
                [
                    {
                        "head_sha": "abc123",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {"head_sha": "abc123", "status": "in_progress", "conclusion": None},
                ],
                [],
            ),
            (
                [{"head_sha": "abc123", "status": "in_progress", "conclusion": None}],
                [{"context": "integration", "state": "failure"}],
            ),
        ]
        for checks, statuses in scenarios:
            self.client.checks, self.client.statuses = checks, statuses
            result = self.handle()
            self.assertEqual("hold", result["action"])
            self.assertEqual(
                1,
                len(
                    alerts.actionable(
                        {
                            "decisions": [result],
                            "errors": [],
                            "scope_confirmed": True,
                            "scope": CONFIG,
                        }
                    )
                ),
            )
        self.review.assert_not_called()
        self.assertEqual([], self.client.writes)

    def test_inline_human_replies_stop_automation_even_without_a_new_review(self):
        self.client.inline_comments = [
            {
                "user": {"login": "human", "type": "User"},
                "in_reply_to_id": 1,
                "body": "Please wait.",
            }
        ]
        result = self.handle()
        self.assertEqual("hold", result["action"])
        self.assertIn("inline review", result["reason"])
        self.review.assert_not_called()
        self.assertEqual([], self.client.writes)

    def test_existing_auto_merge_blocks_rebases_and_pending_ci_before_actions(self):
        self.client.pr["auto_merge"] = {"enabled_by": {"login": "human"}}
        for state in ("behind", "dirty", "clean"):
            self.client.pr["mergeable_state"] = state
            self.client.checks = [
                {"head_sha": "abc123", "status": "in_progress", "conclusion": None}
            ]
            result = self.handle()
            self.assertEqual("hold", result["action"])
            self.assertIn("deferred auto-merge", result["reason"])
        self.review.assert_not_called()
        self.assertEqual([], self.client.writes)

    def test_latest_legacy_status_failure_blocks_even_with_passing_checks(self):
        self.client.statuses = [
            {"context": "tests", "state": "failure"},
            {"context": "tests", "state": "success"},
        ]
        self.assertEqual("hold", self.handle()["action"])
        self.client.statuses.reverse()
        self.assertEqual("merge", self.handle(dry_run=True)["action"])
        self.assertEqual([], self.client.writes)

    def test_unsafe_pr_states_never_mutate(self):
        for change in (
            {"draft": True},
            {"state": "closed"},
            {"title": "Bump package from 1.0.0 to 2.0.0-beta.1"},
            {"title": "Update dependencies"},
            {"auto_merge": {"enabled_by": {}}},
            {"user": {"login": bot.BOT, "type": "User"}},
            {"head": {"sha": "abc123", "repo": {"full_name": "foreign/repo"}}},
        ):
            self.client.pr = {**pull(), **change}
            self.assertEqual("hold", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_human_comment_or_own_request_for_changes_never_approve(self):
        for item in (
            {"user": {"login": "human", "type": "User"}, "body": "Please wait"},
            {
                "user": {"login": "operator", "type": "User"},
                "state": "CHANGES_REQUESTED",
            },
        ):
            self.client.reviews = [item]
            self.assertEqual("hold", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_manual_activity_from_the_automation_account_also_stops_merges(self):
        actor = {"login": "operator", "type": "User"}
        self.client.comments = [{"user": actor, "body": "Do not merge this yet."}]
        self.assertEqual("hold", self.handle()["action"])
        self.client.comments = []
        self.client.reviews = [
            {
                "user": actor,
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": "Looks good",
            }
        ]
        self.assertEqual("hold", self.handle()["action"])
        self.review.assert_not_called()
        self.assertEqual([], self.client.writes)

    def test_reuses_current_approval_but_not_approval_on_an_old_commit(self):
        self.client.reviews = [
            {
                "user": {"login": "operator", "type": "User"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": "<!-- dependabot-automerge:review:abc123 -->",
            }
        ]
        self.handle()
        self.assertEqual(1, len(self.client.writes))
        self.client.writes.clear()
        self.client.reviews[0]["commit_id"] = "old"
        self.client.reviews[0]["body"] = "<!-- dependabot-automerge:review:old -->"
        self.handle()
        self.assertEqual(2, len(self.client.writes))

    def test_base_changes_stop_actions_before_and_after_approval(self):
        for after_approval in (False, True):
            self.client = FakeGitHub()
            original = self.client.request
            reads = 0

            def request(
                method,
                path,
                *args,
                original=original,
                after_approval=after_approval,
                **kwargs,
            ):
                nonlocal reads
                value = original(method, path, *args, **kwargs)
                if method == "GET":
                    reads += 1
                    if (after_approval and self.client.writes) or (
                        not after_approval and reads == 2
                    ):
                        value["base"]["sha"] = "changed-base"
                return value

            self.client.request = request
            if after_approval:
                with self.assertRaises(bot.AutomationError):
                    self.handle()
            else:
                self.assertEqual("wait", self.handle()["action"])
            self.assertEqual(
                ["POST"] if after_approval else [],
                [method for method, _, _ in self.client.writes],
            )

    def test_head_change_between_evaluation_and_refresh_stops_all_writes(self):
        original = self.client.request
        calls = 0

        def request(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                result["head"]["sha"] = "changed"
            return result

        self.client.request = request
        result = self.handle()
        self.assertEqual("wait", result["action"])
        self.assertEqual({}, alerts.actionable({"decisions": [result], "errors": []}))
        self.assertEqual([], self.client.writes)

    def test_pending_ci_during_review_refresh_waits_without_an_alert(self):
        original = self.client.request
        calls = 0

        def request(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.client.checks = [
                    {"head_sha": "abc123", "status": "in_progress", "conclusion": None}
                ]
            return original(*args, **kwargs)

        self.client.request = request
        result = self.handle()
        self.assertEqual("wait", result["action"])
        self.assertEqual({}, alerts.actionable({"decisions": [result], "errors": []}))
        self.assertEqual([], self.client.writes)

    def test_failed_checks_on_an_outdated_base_request_one_rebase_without_approval(
        self,
    ):
        self.client.comparison_base = "older-base"
        self.client.checks[0]["conclusion"] = "failure"
        result = self.handle()
        self.assertEqual("rebase", result["action"])
        self.assertEqual(1, len(self.client.writes))
        self.assertEqual(
            ("POST", f"/repos/{REPO}/issues/7/comments"), self.client.writes[0][:2]
        )
        self.assertEqual(
            "@dependabot rebase\n\n<!-- dependabot-automerge:abc123 -->",
            self.client.writes[0][2]["body"],
        )
        self.review.assert_not_called()
        self.client.comments = [
            {
                "user": {"login": "operator", "type": "User"},
                "body": self.client.writes[0][2]["body"],
            }
        ]
        self.client.writes.clear()
        self.assertEqual("wait", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_failed_checks_after_reaching_the_latest_base_do_not_loop_rebases(self):
        self.client.pr["head"]["sha"] = "def456"
        self.client.checks[0].update(head_sha="def456", conclusion="failure")
        result = self.handle()
        self.assertEqual(
            ("hold", "CI checks failing"), (result["action"], result["reason"])
        )
        self.assertEqual([], self.client.writes)
        self.review.assert_not_called()

    def test_stale_check_evidence_never_triggers_recovery_or_approval(self):
        self.client.comparison_base = "older-base"
        self.client.checks[0]["head_sha"] = "other-head"
        result = self.handle()
        self.assertEqual(
            ("hold", "CI checks inconsistent"), (result["action"], result["reason"])
        )
        self.assertEqual([], self.client.writes)

    def test_failed_ci_recovery_preview_makes_no_writes_and_preserves_human_stops(self):
        self.client.comparison_base = "older-base"
        self.client.checks[0]["conclusion"] = "failure"
        self.assertEqual("rebase", self.handle(dry_run=True)["action"])
        self.assertEqual([], self.client.writes)
        self.client.comments = [
            {"user": {"login": "reviewer", "type": "User"}, "body": "Please wait"}
        ]
        self.assertEqual("hold", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_incomplete_recovery_comparison_stops_without_a_rebase(self):
        self.client.checks[0]["conclusion"] = "failure"
        original = self.client.request
        with (
            mock.patch.object(
                self.client,
                "request",
                side_effect=lambda method, path, *args, **kwargs: (
                    {}
                    if "/compare/" in path
                    else original(method, path, *args, **kwargs)
                ),
            ),
            self.assertRaisesRegex(bot.AutomationError, "lacks a merge base"),
        ):
            self.handle()
        self.assertEqual([], self.client.writes)

    def test_rebase_is_requested_only_once_for_each_commit(self):
        self.client.pr["mergeable_state"] = "behind"
        self.assertEqual("rebase", self.handle()["action"])
        self.assertIn("@dependabot rebase", self.client.writes[0][2]["body"])
        self.client.comments = [
            {
                "user": {"login": "operator", "type": "User"},
                "body": self.client.writes[0][2]["body"],
            }
        ]
        self.client.writes.clear()
        self.assertEqual("wait", self.handle()["action"])
        self.assertEqual([], self.client.writes)

    def test_unknown_post_approval_mergeability_waits_without_an_alert(self):
        original = self.client.request

        def request(method, path, *args, **kwargs):
            value = original(method, path, *args, **kwargs)
            if method == "GET" and self.client.writes:
                value["mergeable_state"] = "unknown"
            return value

        self.client.request = request
        result = self.handle()
        self.assertEqual("wait", result["action"])
        self.assertEqual({}, alerts.actionable({"decisions": [result], "errors": []}))
        self.assertEqual(["POST"], [method for method, _, _ in self.client.writes])

    def test_ci_that_becomes_pending_after_approval_does_not_alert(self):
        original = self.client.request

        def request(method, path, *args, **kwargs):
            value = original(method, path, *args, **kwargs)
            if method == "GET" and self.client.writes:
                value["mergeable_state"] = "blocked"
                self.client.checks = [
                    {"head_sha": "abc123", "status": "in_progress", "conclusion": None}
                ]
            return value

        self.client.request = request
        result = self.handle()
        self.assertEqual("wait", result["action"])
        self.assertEqual({}, alerts.actionable({"decisions": [result], "errors": []}))
        self.assertEqual(["POST"], [method for method, _, _ in self.client.writes])

    def test_post_approval_clean_state_still_requires_passing_checks(self):
        for conclusion, expected in ((None, "wait"), ("failure", "hold")):
            self.client = FakeGitHub()
            original = self.client.request

            def request(
                method, path, *args, original=original, conclusion=conclusion, **kwargs
            ):
                value = original(method, path, *args, **kwargs)
                if method == "GET" and self.client.writes:
                    self.client.checks = [
                        {
                            "head_sha": "abc123",
                            "status": (
                                "in_progress" if conclusion is None else "completed"
                            ),
                            "conclusion": conclusion,
                        }
                    ]
                return value

            self.client.request = request
            self.assertEqual(expected, self.handle()["action"])
            self.assertEqual(["POST"], [method for method, _, _ in self.client.writes])

    def test_human_activity_during_approval_prevents_the_merge(self):
        for kind in ("comment", "review"):
            self.client = FakeGitHub()
            original = self.client.request

            def request(method, path, *args, original=original, kind=kind, **kwargs):
                value = original(method, path, *args, **kwargs)
                if method == "POST" and path.endswith("/reviews"):
                    actor = {"login": "human", "type": "User"}
                    if kind == "comment":
                        self.client.comments.append(
                            {"user": actor, "body": "Please wait."}
                        )
                    else:
                        self.client.reviews.append(
                            {
                                "user": actor,
                                "state": "CHANGES_REQUESTED",
                                "body": "Needs changes",
                            }
                        )
                return value

            self.client.request = request
            self.assertEqual("hold", self.handle()["action"])
            self.assertEqual(["POST"], [method for method, _, _ in self.client.writes])

    def test_branch_policy_is_not_bypassed_after_approval(self):
        self.client.pr["mergeable_state"] = "blocked"
        result = self.handle()
        self.assertEqual("hold", result["action"])
        self.assertEqual(["POST"], [method for method, _, _ in self.client.writes])

    def test_head_change_after_approval_never_reaches_merge(self):
        original = self.client.request

        def request(method, path, *args, **kwargs):
            value = original(method, path, *args, **kwargs)
            if method == "GET" and self.client.writes:
                value["head"]["sha"] = "new"
            return value

        self.client.request = request
        with self.assertRaises(bot.AutomationError):
            self.handle()
        self.assertEqual(["POST"], [method for method, _, _ in self.client.writes])

    def test_merge_rejection_is_not_reported_as_completed(self):
        self.client.merge_response = {"merged": False, "message": "Branch protection"}
        with self.assertRaisesRegex(bot.AutomationError, "merge not completed"):
            self.handle()

    def test_out_of_scope_repository_stops_before_any_api_call(self):
        with self.assertRaises(bot.AutomationError):
            bot.handle(self.client, CONFIG, "other/unlisted", 7, "operator", False)
        self.assertEqual([], self.client.writes)


class ClientTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(
            alerts,
            "publish_gist",
            return_value="https://gist.github.com/operator/" + "a" * 32,
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_scan_failure_is_published_before_the_scan_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            published = []

            def publish_while_locked(report, destination):
                with (
                    destination.with_suffix(".lock").open("a") as probe,
                    self.assertRaises(BlockingIOError),
                ):
                    bot.fcntl.flock(probe, bot.fcntl.LOCK_EX | bot.fcntl.LOCK_NB)
                published.append(copy.deepcopy(report))

            with (
                mock.patch.object(bot, "load_config", return_value=CONFIG),
                mock.patch.object(bot, "GitHub") as client,
                mock.patch.object(bot, "publish", side_effect=publish_while_locked),
            ):
                client.return_value.request.side_effect = RuntimeError(
                    "unexpected failure"
                )
                self.assertEqual(1, bot.main(["--report", str(path)]))
            self.assertEqual(1, len(published))
            self.assertIn("unexpected failure", published[0]["errors"][0])

    def test_api_host_is_pinned_even_when_the_parent_selects_another_host(self):
        output = "api_response:\n  body: e30=\n  truncated: false\n"
        with (
            mock.patch.dict(bot.os.environ, {"GH_HOST": "unintended.example"}),
            mock.patch(
                "dependabot_api.run_command",
                return_value=subprocess.CompletedProcess([], 0, output),
            ) as run,
        ):
            self.assertEqual({}, bot.GitHub().request("GET", "/user"))
        command = run.call_args.args[0]
        self.assertEqual("github.com", command[command.index("--hostname") + 1])

    def test_startup_receipt_is_created_before_an_intentionally_disabled_run(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "started"
            with (
                mock.patch.dict(
                    bot.os.environ, {"DEPENDABOT_WORKER_STARTED": str(receipt)}
                ),
                mock.patch.object(
                    bot, "load_config", return_value={**CONFIG, "enabled": False}
                ),
            ):
                self.assertEqual(0, bot.main([]))
            self.assertTrue(receipt.is_file())

    def test_unexpected_worker_exception_still_publishes_a_help_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with (
                mock.patch.object(bot, "load_config", return_value=CONFIG),
                mock.patch.object(bot, "GitHub") as client,
                mock.patch.object(
                    bot.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch(
                    "dependabot_alerts.shutil.which",
                    return_value="/mock/terminal-notifier",
                ),
            ):
                client.return_value.request.side_effect = RuntimeError(
                    "unexpected API contract"
                )
                self.assertEqual(1, bot.main(["--report", str(path)]))
            self.assertIn(
                "unexpected API contract", json.loads(path.read_text())["errors"][0]
            )
            self.assertFalse(json.loads(path.read_text())["notification_pending"])

    def test_later_unexpected_failure_keeps_earlier_decisions(self):
        earlier = {
            "repo": REPO,
            "number": 1,
            "sha": "abc123",
            "action": "hold",
            "reason": "manual review",
            "url": "https://github.com/example/tools/pull/1",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with (
                mock.patch.object(bot, "load_config", return_value=CONFIG),
                mock.patch.object(bot, "GitHub") as client,
                mock.patch.object(bot, "discover", return_value=[REPO]),
                mock.patch.object(
                    bot, "handle", side_effect=[earlier, RuntimeError("later failure")]
                ),
                mock.patch.object(
                    bot.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch(
                    "dependabot_alerts.shutil.which",
                    return_value="/mock/terminal-notifier",
                ),
            ):
                client.return_value.request.side_effect = [
                    {"login": "operator"},
                    {"permissions": {"push": True}},
                ]
                client.return_value.pages.return_value = [
                    {"number": n, "user": {"login": bot.BOT}} for n in (1, 2)
                ]
                self.assertEqual(1, bot.main(["--report", str(path)]))
            saved = json.loads(path.read_text())
            self.assertEqual([earlier], saved["decisions"])
            self.assertIn("later failure", saved["errors"][0])

    def test_authentication_failure_persists_report_and_requests_a_help_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with (
                mock.patch.object(bot, "load_config", return_value=CONFIG),
                mock.patch.object(bot, "GitHub") as client,
                mock.patch.object(
                    bot.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as notifier,
                mock.patch(
                    "dependabot_alerts.shutil.which",
                    return_value="/mock/terminal-notifier",
                ),
            ):
                client.return_value.request.side_effect = bot.AutomationError(
                    "authentication required"
                )
                self.assertEqual(1, bot.main(["--report", str(path)]))
            self.assertIn(
                "authentication required", json.loads(path.read_text())["errors"][0]
            )
            command = notifier.call_args.args[0]
            self.assertEqual(
                "https://gist.github.com/operator/" + "a" * 32,
                command[command.index("-open") + 1],
            )
            snapshots = list((path.parent / f"{path.stem}-alerts").glob("*.json"))
            self.assertEqual(1, len(snapshots))
            self.assertIn(
                "authentication required",
                json.loads(snapshots[0].read_text())["errors"][0],
            )

    def test_dry_run_failure_does_not_save_a_report_or_send_an_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with (
                mock.patch.object(bot, "load_config", return_value=CONFIG),
                mock.patch.object(bot, "GitHub") as client,
                mock.patch.object(bot, "publish") as publish,
            ):
                client.return_value.request.side_effect = bot.AutomationError(
                    "authentication required"
                )
                self.assertEqual(1, bot.main(["--dry-run", "--report", str(path)]))
            self.assertFalse(path.exists())
            publish.assert_not_called()

    def test_overlapping_run_stops_before_api_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with (
                path.with_suffix(".lock").open("a") as lock,
                mock.patch.object(bot, "load_config", return_value=CONFIG),
                mock.patch.object(bot, "GitHub") as client,
            ):
                bot.fcntl.flock(lock, bot.fcntl.LOCK_EX | bot.fcntl.LOCK_NB)
                self.assertEqual(0, bot.main(["--report", str(path)]))
            client.assert_not_called()
            self.assertFalse(path.exists())

    def test_filesystem_failure_preserves_earlier_results_and_later_repositories(self):
        client = mock.Mock()
        client.request.side_effect = [{"login": "operator"}] + [
            {"permissions": {"push": True}}
        ] * 2
        client.pages.side_effect = [
            [{"number": n, "user": {"login": bot.BOT}} for n in (1, 2)],
            [{"number": 3, "user": {"login": bot.BOT}}],
        ]
        before = {"number": 1, "action": "hold", "reason": "manual review"}
        after = {"number": 3, "action": "hold", "reason": "manual review"}
        with (
            mock.patch.object(bot, "discover", return_value=[REPO, "other/one"]),
            mock.patch.object(
                bot, "handle", side_effect=[before, OSError("cache unavailable"), after]
            ),
        ):
            result = bot.scan(client, CONFIG, False)
        self.assertEqual(
            CONFIG["exclude_repositories"], result["excluded_repositories"]
        )
        self.assertEqual(
            {
                key: CONFIG[key]
                for key in ("organizations", "repositories", "exclude_repositories")
            },
            result["scope"],
        )
        self.assertEqual([before, after], result["decisions"])
        self.assertIn(f"{REPO}#2: cache unavailable", result["errors"])
        self.assertEqual([REPO, "other/one"], result["completed_repositories"])
        self.assertEqual([f"{REPO}#2"], result["failed_prs"])

    def test_repository_error_does_not_stop_later_repositories(self):
        client = mock.Mock()
        client.request.side_effect = [
            {"login": "operator"},
            bot.AutomationError("HTTP 404"),
            {"permissions": {"push": True}},
        ]
        client.pages.return_value = []
        with mock.patch.object(bot, "discover", return_value=["example/missing", REPO]):
            report = bot.scan(client, CONFIG, True)
        self.assertEqual([REPO], report["repositories"])
        self.assertEqual(["example/missing: HTTP 404"], report["errors"])

    def test_real_gh_axi_base64_envelope_contract(self):
        encoded = base64.b64encode(json.dumps({"login": "operator"}).encode()).decode()
        for body in (encoded, json.dumps(encoded)):
            output = f"api_response:\n  body: {body}\n  truncated: false\n"
            with mock.patch(
                "dependabot_api.run_command",
                return_value=subprocess.CompletedProcess([], 0, output),
            ):
                self.assertEqual(
                    {"login": "operator"}, bot.GitHub().request("GET", "/user")
                )

    def test_cli_failure_and_bad_envelope_do_not_become_empty_success(self):
        for output in (
            "",
            "api_response:\n  body: garbage\n  truncated: false",
            "api_response:\n  body: e30=\n  truncated: true",
        ):
            with (
                mock.patch(
                    "dependabot_api.run_command",
                    return_value=subprocess.CompletedProcess([], 0, output),
                ),
                self.assertRaises((bot.AutomationError, ValueError)),
            ):
                bot.GitHub().request("GET", "/user")
        with (
            mock.patch(
                "dependabot_api.run_command",
                return_value=subprocess.CompletedProcess([], 1, "", "HTTP 404"),
            ),
            self.assertRaisesRegex(bot.AutomationError, "HTTP 404"),
        ):
            bot.GitHub().request("GET", "/user")

    def test_pagination_fetches_the_final_page(self):
        client = bot.GitHub()
        with mock.patch.object(
            client,
            "request",
            side_effect=[[{"id": n} for n in range(100)], [{"id": 100}]],
        ) as request:
            records = client.pages("/items?state=open")
        self.assertEqual(list(range(101)), [item["id"] for item in records])
        self.assertIn("page=2", request.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
