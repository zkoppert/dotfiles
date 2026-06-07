#!/usr/bin/env python3
"""Tests for triage.py - pure-function classification and todo manipulation.

We avoid hitting the real GitHub API by injecting fake state/comment
fetchers into the classifier and by mocking subprocess.run for the
integration-style tests.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import triage

# ----------------------------------------------------------------------
# classify()
# ----------------------------------------------------------------------


def _notif(reason: str, **overrides) -> dict:
    base = {
        "id": "1001",
        "reason": reason,
        "subject": {
            "title": "Sample PR",
            "url": "https://api.github.com/repos/zkoppert/example/pulls/42",
            "latest_comment_url": (
                "https://api.github.com/repos/zkoppert/example/issues/comments/9"
            ),
            "type": "PullRequest",
        },
        "repository": {"full_name": "zkoppert/example"},
    }
    base.update(overrides)
    return base


def test_classify_mention_goes_to_q1():
    notif = _notif("mention")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_assign_goes_to_q1():
    c = triage.classify(
        _notif("assign"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_security_alert_goes_to_q1():
    c = triage.classify(
        _notif("security_alert"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_assign_on_my_own_pr_goes_to_inbox():
    """Self-assign or CODEOWNERS auto-assign on a PR I authored is a
    'waiting on reviewers' status update, not a Q1 action item."""
    c = triage.classify(
        _notif("assign"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
    )
    assert c.bucket == triage.BUCKET_INBOX
    assert "authored" in c.reason


def test_classify_mention_on_my_own_pr_goes_to_inbox():
    """An @-mention in the body of a PR I wrote is not someone pulling
    me in - it's me referencing myself."""
    c = triage.classify(
        _notif("mention"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "ZKoppert",
    )
    assert c.bucket == triage.BUCKET_INBOX
    assert "authored" in c.reason


def test_classify_security_alert_on_my_own_pr_still_goes_to_q1():
    """The signal in a security_alert is the vulnerability, not the
    assignment, so authorship shouldn't downgrade it."""
    c = triage.classify(
        _notif("security_alert"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_self_assign_on_non_pr_still_goes_to_q1():
    """The self-author skip is scoped to PullRequest subjects. A self-
    assigned Issue, Discussion, or other subject type should still
    route to Q1 the way it did before."""
    notif = _notif("assign")
    notif["subject"]["type"] = "Issue"
    notif["subject"]["url"] = (
        "https://api.github.com/repos/zkoppert/example/issues/42"
    )
    fetcher_called = []

    def fetcher(n):
        fetcher_called.append(n)
        return "zkoppert"

    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=fetcher,
    )
    assert c.bucket == triage.BUCKET_Q1
    assert fetcher_called == [], "fetcher should be skipped for non-PR subjects"


def test_classify_manual_goes_to_inbox():
    c = triage.classify(
        _notif("manual"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_review_requested_from_teammate_goes_to_q1():
    c = triage.classify(
        _notif("review_requested"),
        my_login="zkoppert",
        q1_logins={"iansan5653"},
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "iansan5653",
    )
    assert c.bucket == triage.BUCKET_Q1
    assert "iansan5653" in c.reason


def test_review_requested_from_outsider_goes_to_inbox():
    c = triage.classify(
        _notif("review_requested"),
        my_login="zkoppert",
        q1_logins={"iansan5653"},
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "somerandomperson",
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_review_requested_unknown_author_falls_through_to_inbox():
    c = triage.classify(
        _notif("review_requested"),
        my_login="zkoppert",
        q1_logins={"iansan5653"},
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: None,
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_review_requested_ignores_latest_comment_author():
    # Even if the latest commenter is a teammate, the classifier should
    # look at the PR author (subject_author_fetcher), not the comment
    # author. This guards against a regression that previously routed
    # bot-noise comments to Q1.
    c = triage.classify(
        _notif("review_requested"),
        my_login="zkoppert",
        q1_logins={"iansan5653"},
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: ("iansan5653", "drive-by comment"),
        subject_author_fetcher=lambda _: "outsider",
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_comment_on_closed_thread_drops():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: ("someone", "hi"),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "closed" in c.reason


def test_comment_on_merged_thread_drops():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "merged",
        comment_fetcher=lambda _: ("someone", "hi"),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_comment_with_mention_goes_to_q1():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: ("teammate", "hey @zkoppert can you look?"),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_super_linter_without_mention_drops():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (
            "super-linter[bot]",
            "Super-linter summary: 3 issues found",
        ),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "super-linter" in c.reason


def test_super_linter_with_mention_still_goes_to_q1():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (
            "super-linter[bot]",
            "Super-linter found issues, @zkoppert please review",
        ),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_ci_activity_drops():
    c = triage.classify(
        _notif("ci_activity"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_subscribed_open_goes_to_inbox():
    c = triage.classify(
        _notif("subscribed"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_subscribed_closed_drops():
    c = triage.classify(
        _notif("subscribed"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_unknown_reason_falls_back_to_inbox():
    c = triage.classify(
        _notif("invitation"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_INBOX


# ----------------------------------------------------------------------
# helpers: mentions_me, is_super_linter, web_url, make_todo_id
# ----------------------------------------------------------------------


def test_mentions_me_case_insensitive():
    assert triage.mentions_me("hey @ZKoppert", "zkoppert") is True
    assert triage.mentions_me("nothing here", "zkoppert") is False
    assert triage.mentions_me("", "zkoppert") is False
    assert triage.mentions_me(None, "zkoppert") is False


def test_is_super_linter_by_author():
    assert triage.is_super_linter("super-linter[bot]", "anything") is True
    assert triage.is_super_linter("super-linter", "anything") is True
    assert triage.is_super_linter("dependabot", "anything") is False


def test_is_super_linter_by_body():
    assert triage.is_super_linter("dependabot", "Super-Linter found ...") is True
    assert triage.is_super_linter(None, None) is False


def test_web_url_converts_pulls_to_pull():
    notif = _notif("comment")
    assert triage.web_url(notif) == "https://github.com/zkoppert/example/pull/42"


def test_web_url_passes_through_issues():
    notif = _notif(
        "comment",
        subject={
            "title": "issue",
            "url": "https://api.github.com/repos/o/r/issues/7",
            "type": "Issue",
            "latest_comment_url": None,
        },
    )
    assert triage.web_url(notif) == "https://github.com/o/r/issues/7"


def test_make_todo_id_is_stable():
    notif = _notif("mention")
    assert triage.make_todo_id(notif) == "notif-example-pullrequest-42-sample-pr"


def test_make_todo_id_handles_no_number():
    notif = _notif(
        "subscribed",
        id="abc123",
        subject={
            "title": "X",
            "url": "",
            "type": "Discussion",
            "latest_comment_url": None,
        },
    )
    todo_id = triage.make_todo_id(notif)
    assert todo_id.startswith("notif-example-discussion-")


# ----------------------------------------------------------------------
# build_todo_entry
# ----------------------------------------------------------------------


def test_build_todo_entry_q1_has_quadrant_fields():
    notif = _notif("mention")
    c = triage.Classification(triage.BUCKET_Q1, "@mention")
    entry = triage.build_todo_entry(notif, c)
    assert entry["quadrant"] == "q1_do_first"
    assert entry["urgency"] == "high"
    assert entry["importance"] == "high"
    assert entry["status"] == "pending"
    assert entry["source"] == "github-notification"
    assert entry["notification"]["thread_id"] == "1001"
    assert entry["notification"]["url"].endswith("/pull/42")


def test_build_todo_entry_inbox_has_no_quadrant_fields():
    notif = _notif("subscribed")
    c = triage.Classification(triage.BUCKET_INBOX, "subscribed")
    entry = triage.build_todo_entry(notif, c)
    assert "quadrant" not in entry
    assert "urgency" not in entry
    assert entry["notification"]["thread_id"] == "1001"


# ----------------------------------------------------------------------
# load_todo / write_todo_atomic / existing_thread_ids / items_to_mark_done
# ----------------------------------------------------------------------


def _sample_todo() -> dict:
    return {
        "inbox": [
            {"id": "a", "title": "loose", "notification": {"thread_id": "111"}},
        ],
        "prioritized": {
            "q1_do_first": [
                {
                    "id": "b",
                    "title": "q1",
                    "status": "done",
                    "notification": {"thread_id": "222"},
                },
                {
                    "id": "b2",
                    "title": "q1-done-already-marked",
                    "status": "done",
                    "notification": {"thread_id": "999", "marked_done": True},
                },
            ],
            "q2_schedule": [],
        },
        # Entries here mirror real zkoppert-todo/todo.yml: items moved to the
        # top-level `done:` section don't carry a `status` field. The mark-
        # read loop must treat `done:` membership alone as proof of done.
        "done": [
            {
                "id": "c",
                "title": "done-archived",
                "completed": "2026-01-01",
                "category": "personal",
                "notification": {"thread_id": "333"},
            },
        ],
    }


def test_existing_thread_ids_covers_all_sections():
    ids = triage.existing_thread_ids(_sample_todo())
    assert ids == {"111", "222", "333", "999"}


def test_items_to_mark_done_picks_done_with_thread_id():
    # `done-archived` has no `status` field but lives in the `done:`
    # section, so it must still be picked up. `q1` has status=done and a
    # thread_id, so it's picked up. The marked_done item is skipped.
    items = triage.items_to_mark_done(_sample_todo())
    titles = sorted(i["title"] for i in items)
    assert titles == ["done-archived", "q1"]


def test_items_to_mark_done_skips_in_progress_without_done_status():
    data = {
        "in_progress": [
            {
                "id": "x",
                "title": "wip",
                "status": "in_progress",
                "notification": {"thread_id": "777"},
            },
        ],
        "done": [],
    }
    assert triage.items_to_mark_done(data) == []


def test_existing_thread_ids_skips_non_dict_notification():
    # A user-edited todo.yml could set ``notification:`` to a string or
    # list. The function must skip those entries, not crash with
    # AttributeError on ``str.get(...)``.
    data = {
        "inbox": [
            {"id": "ok", "notification": {"thread_id": "111"}},
            {"id": "bad-str", "notification": "not a dict"},
            {"id": "bad-list", "notification": ["unexpected"]},
        ],
        "done": [],
    }
    assert triage.existing_thread_ids(data) == {"111"}


def test_items_to_mark_done_skips_non_dict_notification():
    # Same defensive guard as existing_thread_ids: a non-dict
    # ``notification:`` value must be ignored, not crash the run.
    data = {
        "done": [
            {"id": "ok", "notification": {"thread_id": "111"}},
            {"id": "bad", "notification": "not a dict"},
        ],
    }
    items = triage.items_to_mark_done(data)
    titles = [i["id"] for i in items]
    assert titles == ["ok"]


def test_load_todo_creates_missing_sections(tmp_path):
    todo_path = tmp_path / "todo.yml"
    todo_path.write_text("# empty\n")
    data = triage.load_todo(todo_path)
    assert data["inbox"] == []
    assert data["prioritized"]["q1_do_first"] == []
    assert data["done"] == []


def test_write_todo_atomic_round_trip(tmp_path):
    todo_path = tmp_path / "todo.yml"
    todo_path.write_text("inbox: []\n")
    data = triage.load_todo(todo_path)
    data["inbox"].append({"id": "x", "title": "t"})
    triage.write_todo_atomic(todo_path, data)
    reloaded = yaml.safe_load(todo_path.read_text())
    assert reloaded["inbox"][0]["id"] == "x"


def test_write_todo_atomic_preserves_comments(tmp_path):
    # Real zkoppert-todo/todo.yml has 17+ section header comments. They
    # used to be silently destroyed because yaml.safe_dump strips them.
    todo_path = tmp_path / "todo.yml"
    todo_path.write_text(
        "# Top header\n"
        "inbox:\n"
        "  - id: a\n"
        "    title: t\n"
        "# Prioritized comment\n"
        "prioritized:\n"
        "  q1_do_first: []\n"
        "done: []\n"
    )
    data = triage.load_todo(todo_path)
    data["inbox"].append({"id": "b", "title": "added"})
    triage.write_todo_atomic(todo_path, data)
    text = todo_path.read_text()
    assert "# Top header" in text
    assert "# Prioritized comment" in text
    assert "id: b" in text


# ----------------------------------------------------------------------
# fetch_notifications() pagination via --slurp
# ----------------------------------------------------------------------


def test_fetch_notifications_flattens_slurp_pages():
    # `gh api --paginate --slurp` returns [[page1...], [page2...]] which
    # must be flattened. Previously a `re.split` on `][` corrupted JSON
    # whenever a notification title contained `][`.
    pages = [
        [{"id": "1", "subject": {"title": "weird [thing] [feature]"}}],
        [{"id": "2", "subject": {"title": "ok"}}],
    ]
    with patch("triage.run_gh", return_value=json.dumps(pages)):
        result = triage.fetch_notifications()
    assert [n["id"] for n in result] == ["1", "2"]
    assert result[0]["subject"]["title"] == "weird [thing] [feature]"


def test_fetch_notifications_empty_returns_empty_list():
    with patch("triage.run_gh", return_value=""):
        assert triage.fetch_notifications() == []


# ----------------------------------------------------------------------
# run() integration with mocked gh
# ----------------------------------------------------------------------


@pytest.fixture
def todo_file(tmp_path: Path) -> Path:
    path = tmp_path / "todo.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "inbox": [],
                "prioritized": {"q1_do_first": []},
                "done": [],
            }
        )
    )
    return path


def _gh_returns(responses: dict[str, str]):
    """Build a subprocess.run side_effect that maps `gh api <path>` → stdout."""

    def fake_run(cmd, *args, **kwargs):
        assert cmd[0] == "gh"
        # Find the path-like argument (last positional after `api`).
        path = None
        if "api" in cmd:
            idx = cmd.index("api")
            after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
            if after:
                path = after[0]
        text = responses.get(path or "", "")
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    return fake_run


def test_run_dedupes_already_tracked(todo_file):
    existing = {
        "inbox": [
            {"id": "old", "title": "x", "notification": {"thread_id": "1001"}},
        ],
        "prioritized": {"q1_do_first": []},
        "done": [],
    }
    todo_file.write_text(yaml.safe_dump(existing))

    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications?all=true": json.dumps([_notif("mention")]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.already_tracked == 1
    assert stats.added_q1 == 0


def test_run_adds_q1_for_mention(todo_file):
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications?all=true": json.dumps([_notif("mention")]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.added_q1 == 1
    data = yaml.safe_load(todo_file.read_text())
    q1 = data["prioritized"]["q1_do_first"]
    assert len(q1) == 1
    assert q1[0]["quadrant"] == "q1_do_first"


def test_run_drops_ci_activity_and_marks_done(todo_file):
    notif = _notif("ci_activity", id="555")
    delete_calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "DELETE" in cmd:
            delete_calls.append(tuple(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications?all=true": json.dumps([notif]),
        }
        # find path
        idx = cmd.index("api")
        after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
        path = after[0] if after else ""
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=responses.get(path, ""),
            stderr="",
        )

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.dropped == 1
    assert any("/notifications/threads/555" in " ".join(c) for c in delete_calls)


def test_run_dry_run_does_not_write(todo_file):
    before = todo_file.read_text()
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications?all=true": json.dumps([_notif("mention")]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--dry-run", "--no-notify"]
        )
        stats = triage.run(args)
    assert stats.added_q1 == 1
    assert todo_file.read_text() == before


def test_run_marks_done_on_completed(todo_file):
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [],
                "prioritized": {
                    "q1_do_first": [
                        {
                            "id": "doneone",
                            "title": "x",
                            "status": "done",
                            "notification": {"thread_id": "777"},
                        },
                    ],
                },
                "done": [],
            }
        )
    )

    delete_paths: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "DELETE" in cmd:
            # last positional is the path
            delete_paths.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications?all=true": json.dumps([]),
        }
        idx = cmd.index("api")
        after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
        path = after[0] if after else ""
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=responses.get(path, ""),
            stderr="",
        )

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)

    assert stats.marked_done == 1
    assert any("/notifications/threads/777" in p for p in delete_paths)
    data = yaml.safe_load(todo_file.read_text())
    entry = data["prioritized"]["q1_do_first"][0]
    assert entry["notification"]["marked_done"] is True
    assert entry["notification"]["marked_done_at"] == (
        datetime.date.today().isoformat()
    )


def test_run_handles_empty_notifications(todo_file):
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications?all=true": "[]",
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.fetched == 0
    assert stats.errors == []


def test_main_returns_zero_on_success(todo_file):
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications?all=true": "[]",
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        rc = triage.main(["--todo-file", str(todo_file), "--no-notify"])
    assert rc == 0


# ----------------------------------------------------------------------
# Inbox pruner: parse_github_url
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://github.com/octocat/Hello-World/pull/123",
            {"owner": "octocat", "repo": "Hello-World", "kind": "pr", "number": 123},
        ),
        (
            "https://github.com/octocat/Hello-World/pull/123/files",
            {"owner": "octocat", "repo": "Hello-World", "kind": "pr", "number": 123},
        ),
        (
            "https://github.com/octocat/Hello-World/pull/123#issuecomment-9",
            {"owner": "octocat", "repo": "Hello-World", "kind": "pr", "number": 123},
        ),
        (
            "https://github.com/octocat/Hello-World/issues/456",
            {"owner": "octocat", "repo": "Hello-World", "kind": "issue", "number": 456},
        ),
        (
            "https://github.com/octocat/Hello-World/discussions/789",
            {
                "owner": "octocat",
                "repo": "Hello-World",
                "kind": "discussion",
                "number": 789,
            },
        ),
        (
            "https://github.com/octocat/Hello-World/discussions/789#discussioncomment-1",
            {
                "owner": "octocat",
                "repo": "Hello-World",
                "kind": "discussion",
                "number": 789,
            },
        ),
        (
            "https://www.github.com/octocat/Hello-World/pull/1",
            {"owner": "octocat", "repo": "Hello-World", "kind": "pr", "number": 1},
        ),
    ],
)
def test_parse_github_url_supported(url, expected):
    assert triage.parse_github_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World/commit/abcdef",
        "https://github.com/octocat/Hello-World/releases/tag/v1",
        "https://github.com/octocat/Hello-World/actions/runs/123",
        "https://example.com/octocat/Hello-World/pull/123",
        "not a url",
    ],
)
def test_parse_github_url_unsupported(url):
    assert triage.parse_github_url(url) is None


# ----------------------------------------------------------------------
# Inbox pruner: check_subject_stale (per-kind)
# ----------------------------------------------------------------------


def _called_process_error(
    returncode: int, stderr: str
) -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(returncode, ["gh", "api"], stderr=stderr)
    return exc


def _mock_run_gh(return_value=None, side_effect=None):
    """Patch triage.run_gh with a MagicMock."""
    return patch(
        "triage.run_gh",
        MagicMock(
            return_value=return_value,
            side_effect=side_effect,
        ),
    )


def test_check_subject_stale_pr_open_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "pr", "number": 1}
    with _mock_run_gh(return_value=json.dumps({"state": "open"})):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_KEEP


def test_check_subject_stale_pr_merged_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "pr", "number": 1}
    body = json.dumps({"state": "closed", "merged_at": "2024-01-01T00:00:00Z"})
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "merged"


def test_check_subject_stale_pr_closed_unmerged_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "pr", "number": 1}
    body = json.dumps({"state": "closed", "merged_at": None})
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "closed pr"


def test_check_subject_stale_issue_open_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "issue", "number": 1}
    with _mock_run_gh(return_value=json.dumps({"state": "open"})):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_KEEP


def test_check_subject_stale_issue_closed_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "issue", "number": 1}
    with _mock_run_gh(return_value=json.dumps({"state": "closed"})):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "closed issue"


def test_check_subject_stale_404_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "pr", "number": 1}
    err = _called_process_error(1, "gh: HTTP 404: Not Found")
    with _mock_run_gh(side_effect=err):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "deleted"


def test_check_subject_stale_403_private_repo_keeps():
    """Regression: GitHub returns HTTP 403 with body {'message': 'Not Found'}
    for private repos where access has been revoked. `gh` renders this as
    'gh: HTTP 403: Not Found ...'. We must NOT treat that as deleted, or
    we'd silently drop inbox items for repos the user lost access to.
    """
    parsed = {"owner": "o", "repo": "private", "kind": "pr", "number": 1}
    err = _called_process_error(
        1,
        "gh: HTTP 403: Not Found (https://api.github.com/repos/o/private/pulls/1)",
    )
    with _mock_run_gh(side_effect=err):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN


def test_check_subject_stale_500_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "pr", "number": 1}
    err = _called_process_error(1, "gh: HTTP 500: Server Error")
    with _mock_run_gh(side_effect=err):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN


def test_check_subject_stale_timeout_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "issue", "number": 1}
    timeout = subprocess.TimeoutExpired(cmd=["gh"], timeout=20)
    with _mock_run_gh(side_effect=timeout):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN


def test_check_subject_stale_bad_json_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "issue", "number": 1}
    with _mock_run_gh(return_value="not json"):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN


def test_check_subject_stale_discussion_open_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps(
        {
            "data": {
                "repository": {
                    "discussion": {
                        "closed": False,
                        "locked": False,
                        "answerChosenAt": None,
                        "category": {"isAnswerable": True},
                    }
                }
            }
        }
    )
    with _mock_run_gh(return_value=body):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_KEEP


def test_check_subject_stale_discussion_locked_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps(
        {
            "data": {
                "repository": {
                    "discussion": {
                        "closed": False,
                        "locked": True,
                        "answerChosenAt": None,
                        "category": {"isAnswerable": False},
                    }
                }
            }
        }
    )
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "locked discussion"


def test_check_subject_stale_discussion_closed_but_unlocked_keeps():
    """Closed-but-not-locked discussions can still receive activity; keep them."""
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps(
        {
            "data": {
                "repository": {
                    "discussion": {
                        "closed": True,
                        "locked": False,
                        "answerChosenAt": None,
                        "category": {"isAnswerable": False},
                    }
                }
            }
        }
    )
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_KEEP
    assert reason == "discussion still open"


def test_check_subject_stale_discussion_answered_qa_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps(
        {
            "data": {
                "repository": {
                    "discussion": {
                        "closed": False,
                        "locked": False,
                        "answerChosenAt": "2024-01-01T00:00:00Z",
                        "category": {"isAnswerable": True},
                    }
                }
            }
        }
    )
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "answered Q&A"


def test_check_subject_stale_discussion_answered_non_qa_keeps():
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps(
        {
            "data": {
                "repository": {
                    "discussion": {
                        "closed": False,
                        "locked": False,
                        "answerChosenAt": "2024-01-01T00:00:00Z",
                        "category": {"isAnswerable": False},
                    }
                }
            }
        }
    )
    with _mock_run_gh(return_value=body):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_KEEP


def test_check_subject_stale_discussion_null_data_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps({"data": {"repository": {"discussion": None}}})
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP
    assert reason == "deleted"


def test_check_subject_stale_discussion_could_not_resolve_keeps():
    # GraphQL "Could not resolve" is ambiguous between "deleted" and
    # "private repo / access revoked", so we treat every GraphQL error as
    # UNKNOWN and let the pruner keep the entry.
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    err = _called_process_error(1, "Could not resolve to a Repository with the name")
    with _mock_run_gh(side_effect=err):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN


def test_check_subject_stale_discussion_repository_null_keeps():
    # ``repository: null`` can mean deleted OR access revoked - keep, don't drop.
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    body = json.dumps({"data": {"repository": None}})
    with _mock_run_gh(return_value=body):
        action, reason = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN
    assert reason == "repository null"


def test_check_subject_stale_unsupported_kind():
    parsed = {"owner": "o", "repo": "r", "kind": "release", "number": 1}
    action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_UNKNOWN


# ----------------------------------------------------------------------
# prune_stale_inbox
# ----------------------------------------------------------------------


def test_prune_stale_inbox_empty_is_noop():
    stats = triage.TriageStats()
    data = {"inbox": []}
    triage.prune_stale_inbox(data, stats)
    assert stats.pruned_stale == 0
    assert data["inbox"] == []


def test_prune_stale_inbox_skips_missing_inbox():
    stats = triage.TriageStats()
    data = {}
    triage.prune_stale_inbox(data, stats)
    assert stats.pruned_stale == 0


def test_prune_stale_inbox_keeps_non_github_source():
    stats = triage.TriageStats()
    data = {
        "inbox": [
            {
                "id": "manual1",
                "source": "manual",
                "notification": {"url": "https://github.com/o/r/pull/1"},
            },
        ]
    }
    # run_gh should never be called for non-github items.
    with patch("triage.run_gh") as run_gh_mock:
        triage.prune_stale_inbox(data, stats)
    run_gh_mock.assert_not_called()
    assert len(data["inbox"]) == 1
    assert stats.pruned_stale == 0


def test_prune_stale_inbox_keeps_unparseable_url():
    stats = triage.TriageStats()
    data = {
        "inbox": [
            {
                "id": "weird",
                "source": "github-notification",
                "notification": {"url": "https://github.com/o/r/commit/abc"},
            },
        ]
    }
    with patch("triage.run_gh") as run_gh_mock:
        triage.prune_stale_inbox(data, stats)
    run_gh_mock.assert_not_called()
    assert len(data["inbox"]) == 1


def test_prune_stale_inbox_drops_stale_and_keeps_active():
    stats = triage.TriageStats()
    data = {
        "inbox": [
            {
                "id": "active-pr",
                "source": "github-notification",
                "notification": {"url": "https://github.com/o/r/pull/1"},
            },
            {
                "id": "stale-pr",
                "source": "github-notification",
                "notification": {"url": "https://github.com/o/r/pull/2"},
            },
            {
                "id": "manual",
                "source": "manual",
                "notification": {"url": "https://example.com"},
            },
        ]
    }

    def fake_run_gh(cmd, *args, **kwargs):
        # Map by PR number in the API path.
        if "/pulls/1" in " ".join(cmd):
            return json.dumps({"state": "open"})
        if "/pulls/2" in " ".join(cmd):
            return json.dumps({"state": "closed", "merged_at": "x"})
        raise AssertionError(f"unexpected call: {cmd}")

    with patch("triage.run_gh", side_effect=fake_run_gh):
        triage.prune_stale_inbox(data, stats)

    ids = [e["id"] for e in data["inbox"]]
    assert ids == ["active-pr", "manual"]
    assert stats.pruned_stale == 1
    assert stats.pruned_by_reason == {"merged": 1}


def test_prune_stale_inbox_unknown_keeps():
    stats = triage.TriageStats()
    data = {
        "inbox": [
            {
                "id": "transient",
                "source": "github-notification",
                "notification": {"url": "https://github.com/o/r/issues/1"},
            },
        ]
    }
    err = _called_process_error(1, "HTTP 503: Service Unavailable")
    with patch("triage.run_gh", side_effect=err):
        triage.prune_stale_inbox(data, stats)
    assert len(data["inbox"]) == 1
    assert stats.pruned_stale == 0


def test_prune_stale_inbox_handles_non_dict_entries():
    stats = triage.TriageStats()
    data = {"inbox": ["not a dict", None, {"id": "good", "source": "manual"}]}
    triage.prune_stale_inbox(data, stats)
    assert data["inbox"] == ["not a dict", None, {"id": "good", "source": "manual"}]


# ----------------------------------------------------------------------
# run() integration: pruner respects --no-prune and --dry-run
# ----------------------------------------------------------------------


def test_run_no_prune_skips_pruner(todo_file):
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [
                    {
                        "id": "n1",
                        "source": "github-notification",
                        "notification": {"url": "https://github.com/o/r/pull/1"},
                    },
                ],
                "prioritized": {"q1_do_first": []},
                "done": [],
            }
        )
    )
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications?all=true": json.dumps([]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--no-notify", "--no-prune"],
        )
        stats = triage.run(args)
    assert stats.pruned_stale == 0
    # Inbox unchanged.
    assert len(yaml.safe_load(todo_file.read_text())["inbox"]) == 1


def test_run_dry_run_does_not_write_pruned_inbox(todo_file):
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [
                    {
                        "id": "n1",
                        "source": "github-notification",
                        "notification": {"url": "https://github.com/o/r/pull/1"},
                    },
                ],
                "prioritized": {"q1_do_first": []},
                "done": [],
            }
        )
    )
    before = todo_file.read_text()

    def fake_run(cmd, *args, **kwargs):
        path_args = " ".join(cmd)
        if "/user" in path_args:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"login": "zkoppert"}),
                stderr="",
            )
        if "/notifications" in path_args and "/threads" not in path_args:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "/pulls/1" in path_args:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"state": "closed", "merged_at": "x"}),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--no-notify", "--dry-run"],
        )
        stats = triage.run(args)

    assert stats.pruned_stale == 1
    # File unchanged because --dry-run.
    assert todo_file.read_text() == before


def test_run_prunes_and_writes_when_live(todo_file):
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [
                    {
                        "id": "stale-1",
                        "source": "github-notification",
                        "notification": {"url": "https://github.com/o/r/pull/1"},
                    },
                    {
                        "id": "active-1",
                        "source": "github-notification",
                        "notification": {"url": "https://github.com/o/r/issues/2"},
                    },
                ],
                "prioritized": {"q1_do_first": []},
                "done": [],
            }
        )
    )

    def fake_run(cmd, *args, **kwargs):
        path_args = " ".join(cmd)
        if "/user" in path_args:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"login": "zkoppert"}),
                stderr="",
            )
        if "/notifications" in path_args and "/threads" not in path_args:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "/pulls/1" in path_args:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"state": "closed", "merged_at": "x"}),
                stderr="",
            )
        if "/issues/2" in path_args:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"state": "open"}),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--no-notify"],
        )
        stats = triage.run(args)

    assert stats.pruned_stale == 1
    data = yaml.safe_load(todo_file.read_text())
    ids = [e["id"] for e in data["inbox"]]
    assert ids == ["active-1"]


def test_run_prune_marks_thread_done_so_it_does_not_reappear(todo_file):
    """Regression: pruner must mark the underlying thread done (DELETE),
    otherwise the next cron cycle re-fetches the unread notification and
    re-adds the inbox entry the pruner just dropped (verified bug from
    gpt-5.4 review).
    """
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [
                    {
                        "id": "stale-1",
                        "source": "github-notification",
                        "notification": {
                            "thread_id": "123",
                            "url": "https://github.com/o/r/pull/1",
                        },
                    },
                ],
                "prioritized": {"q1_do_first": []},
                "done": [],
            }
        )
    )

    unread_notif = {
        "id": "123",
        "unread": True,
        "reason": "subscribed",
        "updated_at": "2025-01-01T00:00:00Z",
        "subject": {
            "title": "stale PR",
            "url": "https://api.github.com/repos/o/r/pulls/1",
            "type": "PullRequest",
        },
        "repository": {"full_name": "o/r"},
    }
    delete_calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "-X" in cmd and "DELETE" in cmd:
            delete_calls.append(tuple(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "/user" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"login": "zkoppert"}), stderr=""
            )
        if "/notifications" in joined and "/threads" not in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([unread_notif]), stderr=""
            )
        if "/pulls/1" in joined:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"state": "closed", "merged_at": "x"}),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--no-notify"],
        )
        stats_first = triage.run(args)

    assert stats_first.pruned_stale == 1
    assert any(
        "/notifications/threads/123" in " ".join(c) for c in delete_calls
    ), "pruner must DELETE the thread so the next run doesn't re-add it"

    # Simulate next cron cycle: GitHub now omits the thread (it's been marked
    # done via DELETE), so nothing should be re-added.
    def fake_run_after(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "/user" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"login": "zkoppert"}), stderr=""
            )
        if "/notifications" in joined and "/threads" not in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("triage.subprocess.run", side_effect=fake_run_after):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--no-notify"],
        )
        stats_second = triage.run(args)

    assert stats_second.added_inbox == 0
    assert stats_second.added_q1 == 0
    data = yaml.safe_load(todo_file.read_text())
    assert data["inbox"] == []


def test_run_prune_dry_run_does_not_mark_thread_done(todo_file):
    """Dry-run must not DELETE /notifications/threads even during pruning."""
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [
                    {
                        "id": "stale-1",
                        "source": "github-notification",
                        "notification": {
                            "thread_id": "123",
                            "url": "https://github.com/o/r/pull/1",
                        },
                    },
                ],
                "prioritized": {"q1_do_first": []},
                "done": [],
            }
        )
    )
    delete_calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "-X" in cmd and "DELETE" in cmd:
            delete_calls.append(tuple(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "/user" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"login": "zkoppert"}), stderr=""
            )
        if "/notifications" in joined and "/threads" not in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "/pulls/1" in joined:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"state": "closed", "merged_at": "x"}),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--dry-run", "--no-notify"],
        )
        stats = triage.run(args)

    assert stats.pruned_stale == 1
    assert delete_calls == [], "dry-run must not DELETE threads"


def test_prune_stale_inbox_skips_non_dict_notification():
    # A user-edited todo.yml could put a string (or anything else) under
    # ``notification``. The pruner must keep the entry instead of crashing
    # with AttributeError on ``.get()``.
    data = {
        "inbox": [
            {
                "id": "weird",
                "source": "github-notification",
                "notification": "not a dict",
            },
            {
                "id": "also-weird",
                "source": "github-notification",
                "notification": ["list", "instead", "of", "dict"],
            },
        ]
    }
    stats = triage.TriageStats()
    # If the pruner ever calls run_gh on these, that's also a bug - mock it
    # to raise so we'd notice.
    with _mock_run_gh(side_effect=AssertionError("should not be called")):
        triage.prune_stale_inbox(data, stats)
    assert len(data["inbox"]) == 2
    assert stats.pruned_stale == 0
    assert stats.errors == []


def test_prune_stale_inbox_handles_mark_done_timeout():
    # ``run_gh`` can raise subprocess.TimeoutExpired, not just
    # CalledProcessError. A timeout during mark-done must be logged and the
    # drop must still proceed - it must not crash the prune loop.
    data = {
        "inbox": [
            {
                "id": "stale-with-timeout",
                "source": "github-notification",
                "notification": {
                    "url": "https://github.com/o/r/issues/1",
                    "thread_id": "12345",
                },
            }
        ]
    }
    stats = triage.TriageStats()
    timeout_exc = subprocess.TimeoutExpired(cmd=["gh"], timeout=20)
    with patch(
        "triage.check_subject_stale",
        MagicMock(return_value=(triage.STALE_DROP, "closed issue")),
    ), patch("triage.mark_thread_done", MagicMock(side_effect=timeout_exc)):
        triage.prune_stale_inbox(data, stats)
    assert data["inbox"] == []  # drop still happened
    assert stats.pruned_stale == 1
    assert len(stats.errors) == 1
    assert "12345" in stats.errors[0]
    assert "mark-done failed" in stats.errors[0]


def test_run_bucket_drop_handles_mark_done_timeout(todo_file):
    # The BUCKET_DROP mark-done site in run() must catch TimeoutExpired,
    # not just CalledProcessError. Otherwise a slow GitHub DELETE crashes
    # the whole run before the YAML write happens.
    notif = _notif("ci_activity", id="555")

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "DELETE" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=20)
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications?all=true": json.dumps([notif]),
        }
        idx = cmd.index("api")
        after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
        path = after[0] if after else ""
        return subprocess.CompletedProcess(
            cmd, 0, stdout=responses.get(path, ""), stderr=""
        )

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.dropped == 1
    assert any("555" in e and "mark-done failed" in e for e in stats.errors)


def test_run_marks_done_on_completed_handles_timeout(todo_file):
    # The mark-done-on-completed site in run() must catch TimeoutExpired too.
    # A timeout must be logged in stats.errors and must not stop the run
    # before the YAML write.
    todo_file.write_text(
        yaml.safe_dump(
            {
                "inbox": [],
                "prioritized": {
                    "q1_do_first": [
                        {
                            "id": "doneone",
                            "title": "x",
                            "status": "done",
                            "notification": {"thread_id": "777"},
                        },
                    ],
                },
                "done": [],
            }
        )
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "DELETE" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=20)
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications?all=true": json.dumps([]),
        }
        idx = cmd.index("api")
        after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
        path = after[0] if after else ""
        return subprocess.CompletedProcess(
            cmd, 0, stdout=responses.get(path, ""), stderr=""
        )

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert any(
        "777" in e and "mark-done-on-completed failed" in e for e in stats.errors
    )
    # The YAML write still happened - the run did not crash mid-way.
    data = yaml.safe_load(todo_file.read_text())
    entry = data["prioritized"]["q1_do_first"][0]
    assert entry["notification"].get("marked_done") is not True


# ----------------------------------------------------------------------
# prune_stale_notifications: quadrant sweep
# ----------------------------------------------------------------------


def _stale_notification_entry(entry_id: str, pr_number: int) -> dict:
    """Helper: a tracked github-notification entry pointing at a PR URL."""
    return {
        "id": entry_id,
        "source": "github-notification",
        "notification": {
            "url": f"https://github.com/o/r/pull/{pr_number}",
            "thread_id": f"thr-{pr_number}",
        },
    }


def _all_prs_merged(cmd, *args, **kwargs):
    return json.dumps({"state": "closed", "merged_at": "2024-01-01T00:00:00Z"})


def test_prune_stale_notifications_drops_from_q1_do_first():
    stats = triage.TriageStats()
    data = {
        "inbox": [],
        "prioritized": {
            "q1_do_first": [
                _stale_notification_entry("q1-pr", 100),
            ]
        },
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["prioritized"]["q1_do_first"] == []
    assert stats.pruned_stale == 1


def test_prune_stale_notifications_drops_from_q2_schedule():
    stats = triage.TriageStats()
    data = {
        "inbox": [],
        "prioritized": {
            "q2_schedule": [
                _stale_notification_entry("q2-pr", 200),
            ]
        },
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["prioritized"]["q2_schedule"] == []
    assert stats.pruned_stale == 1


def test_prune_stale_notifications_drops_from_q3_and_q4():
    stats = triage.TriageStats()
    data = {
        "inbox": [],
        "prioritized": {
            "q3_delegate": [_stale_notification_entry("q3-pr", 300)],
            "q4_eliminate": [_stale_notification_entry("q4-pr", 400)],
        },
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["prioritized"]["q3_delegate"] == []
    assert data["prioritized"]["q4_eliminate"] == []
    assert stats.pruned_stale == 2


def test_prune_stale_notifications_sweeps_inbox_and_quadrants_together():
    stats = triage.TriageStats()
    data = {
        "inbox": [_stale_notification_entry("inbox-pr", 1)],
        "prioritized": {
            "q1_do_first": [_stale_notification_entry("q1-pr", 2)],
            "q2_schedule": [_stale_notification_entry("q2-pr", 3)],
        },
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["inbox"] == []
    assert data["prioritized"]["q1_do_first"] == []
    assert data["prioritized"]["q2_schedule"] == []
    assert stats.pruned_stale == 3


def test_prune_stale_notifications_keeps_active_quadrant_entries():
    stats = triage.TriageStats()
    data = {
        "inbox": [],
        "prioritized": {
            "q2_schedule": [
                _stale_notification_entry("active-pr", 500),
                _stale_notification_entry("stale-pr", 501),
            ]
        },
    }

    def mixed_states(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "/pulls/500" in joined:
            return json.dumps({"state": "open"})
        if "/pulls/501" in joined:
            return json.dumps({"state": "closed", "merged_at": "x"})
        raise AssertionError(f"unexpected gh call: {cmd}")

    with patch("triage.run_gh", side_effect=mixed_states), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    ids = [e["id"] for e in data["prioritized"]["q2_schedule"]]
    assert ids == ["active-pr"]
    assert stats.pruned_stale == 1


def test_prune_stale_notifications_keeps_non_github_quadrant_entries():
    """Manually added quadrant todos must never be touched, even if they
    happen to have a github.com URL stashed somewhere."""
    stats = triage.TriageStats()
    manual = {
        "id": "manual-q2",
        "source": "manual",
        "notification": {"url": "https://github.com/o/r/pull/999"},
    }
    no_source = {
        "id": "bare-q2",
        "title": "Plain todo with no source field",
    }
    data = {
        "inbox": [],
        "prioritized": {"q2_schedule": [manual, no_source]},
    }
    with patch("triage.run_gh") as run_gh_mock:
        triage.prune_stale_notifications(data, stats)
    run_gh_mock.assert_not_called()
    assert len(data["prioritized"]["q2_schedule"]) == 2
    assert stats.pruned_stale == 0


def test_prune_stale_notifications_handles_missing_prioritized_key():
    """An older or partially populated todo.yml may have no
    ``prioritized`` key at all - the pruner must not crash."""
    stats = triage.TriageStats()
    data = {"inbox": []}
    triage.prune_stale_notifications(data, stats)
    assert stats.pruned_stale == 0


def test_prune_stale_notifications_handles_missing_quadrant():
    """Only some quadrants may exist - skip absent ones cleanly."""
    stats = triage.TriageStats()
    data = {
        "inbox": [],
        "prioritized": {
            "q2_schedule": [_stale_notification_entry("only-q2", 700)],
            # no q1_do_first, q3_delegate, q4_eliminate
        },
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["prioritized"]["q2_schedule"] == []
    assert stats.pruned_stale == 1


def test_prune_stale_notifications_dry_run_skips_mark_done():
    stats = triage.TriageStats()
    data = {
        "inbox": [],
        "prioritized": {
            "q1_do_first": [_stale_notification_entry("dry-pr", 800)],
        },
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ) as mark_done_mock:
        triage.prune_stale_notifications(data, stats, dry_run=True)
    # The entry is still removed from the in-memory structure (caller
    # decides whether to persist), but no DELETE was sent to GitHub.
    assert data["prioritized"]["q1_do_first"] == []
    mark_done_mock.assert_not_called()


def test_prune_stale_inbox_alias_points_at_new_pruner():
    """Backwards-compat alias still works and sweeps quadrants too."""
    assert triage.prune_stale_inbox is triage.prune_stale_notifications


# ----------------------------------------------------------------------
# classify(): subject-state check at intake for actionable reasons
# ----------------------------------------------------------------------


def test_classify_drops_review_requested_on_closed_pr():
    notif = _notif("review_requested")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "closed" in c.reason and "review_requested" in c.reason


def test_classify_drops_review_requested_on_merged_pr():
    c = triage.classify(
        _notif("review_requested"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "merged",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_drops_mention_on_closed_pr():
    c = triage.classify(
        _notif("mention"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_drops_assign_on_closed_issue():
    issue_notif = _notif(
        "assign",
        subject={
            "title": "Closed issue",
            "url": "https://api.github.com/repos/o/r/issues/42",
            "latest_comment_url": None,
            "type": "Issue",
        },
    )
    c = triage.classify(
        issue_notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_drops_manual_on_closed_subject():
    c = triage.classify(
        _notif("manual"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_keeps_mention_on_open_subject():
    """Regression guard: open subjects still flow through to Q1."""
    c = triage.classify(
        _notif("mention"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_keeps_review_requested_on_open_pr():
    """Regression guard: open PR review requests still flow through."""
    c = triage.classify(
        _notif("review_requested"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket != triage.BUCKET_DROP


def test_classify_keeps_assign_when_state_unknown():
    """If state_fetcher returns None (network blip, parse failure), the
    classifier must fall through to normal classification - never drop on
    unknown state."""
    c = triage.classify(
        _notif("assign"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_skips_state_check_for_non_subject_types():
    """A security_alert subject (RepositoryVulnerabilityAlert) has no
    PR/issue state - classifier must not even call state_fetcher and must
    route normally to Q1."""
    sec_notif = _notif(
        "security_alert",
        subject={
            "title": "Vuln",
            "url": "https://api.github.com/repos/o/r/dependabot/alerts/1",
            "latest_comment_url": None,
            "type": "RepositoryVulnerabilityAlert",
        },
    )
    fetcher = MagicMock(return_value=None)
    c = triage.classify(
        sec_notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=fetcher,
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    fetcher.assert_not_called()
    assert c.bucket == triage.BUCKET_Q1


def _enable_dependabot_notif() -> dict:
    """Helper: self-authored Enable Dependabot PR notification."""
    return _notif(
        "author",
        subject={
            "title": "Enable Dependabot",
            "url": "https://api.github.com/repos/o/r/pulls/30",
            "latest_comment_url": None,
            "type": "PullRequest",
        },
    )


def test_classify_closed_enable_dependabot_drops_via_state_check_not_commenter_fetch():
    """A closed self-authored Enable Dependabot PR must drop via the cheap
    state check before paying for 3 commenter API calls. Regression guard
    on the ordering of classify() branches."""
    fetcher_calls = []

    def fetcher(n, *, my_login):
        fetcher_calls.append(n)
        return set()

    c = triage.classify(
        _enable_dependabot_notif(),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=fetcher,
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "closed pullrequest" in c.reason
    assert fetcher_calls == []  # cheap state check short-circuited


def test_classify_drops_self_authored_enable_dependabot_with_no_human_commenters():
    """My own Enable Dependabot PR with only bot reviewers (Copilot,
    super-linter) is noise - drop it so the inbox stays clean."""
    c = triage.classify(
        _enable_dependabot_notif(),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "Enable Dependabot" in c.reason


def test_classify_keeps_enable_dependabot_when_human_commented():
    """If an actual teammate has commented, keep the notification so
    the response reaches the inbox."""
    c = triage.classify(
        _enable_dependabot_notif(),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: {"andimiya"},
    )
    assert c.bucket != triage.BUCKET_DROP


def test_classify_keeps_enable_dependabot_on_fetch_failure():
    """On fetcher returning None (network blip, parse failure), fall
    through to normal classification - never drop on uncertainty."""
    c = triage.classify(
        _enable_dependabot_notif(),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: None,
    )
    assert c.bucket != triage.BUCKET_DROP


def test_classify_does_not_drop_unrelated_author_pr():
    """`reason=author` on a PR with a different title is not affected
    by the Enable Dependabot rule - falls through to normal classifier."""
    notif = _notif(
        "author",
        subject={
            "title": "Fix payment processing bug",
            "url": "https://api.github.com/repos/o/r/pulls/30",
            "latest_comment_url": None,
            "type": "PullRequest",
        },
    )
    fetcher_calls = []

    def fetcher(n, *, my_login):
        fetcher_calls.append(n)
        return set()

    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=fetcher,
    )
    assert c.bucket != triage.BUCKET_DROP
    assert fetcher_calls == []  # short-circuit before any API call


def test_classify_enable_dependabot_only_fires_on_author_reason():
    """Same title under a different reason (e.g. team_mention) doesn't
    trigger the self-authored rule."""
    notif = _notif(
        "team_mention",
        subject={
            "title": "Enable Dependabot",
            "url": "https://api.github.com/repos/o/r/pulls/30",
            "latest_comment_url": None,
            "type": "PullRequest",
        },
    )
    fetcher_calls = []

    def fetcher(n, *, my_login):
        fetcher_calls.append(n)
        return set()

    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=fetcher,
    )
    assert c.bucket != triage.BUCKET_DROP
    assert fetcher_calls == []


def test_classify_enable_dependabot_title_match_is_case_insensitive():
    """Whitespace and case variations on the exact title still match."""
    notif = _notif(
        "author",
        subject={
            "title": "  enable dependabot  ",
            "url": "https://api.github.com/repos/o/r/pulls/30",
            "latest_comment_url": None,
            "type": "PullRequest",
        },
    )
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP


# --- fetch_pr_human_commenters helper tests ---


def _commenter(login: str, user_type: str = "User") -> dict:
    return {"user": {"login": login, "type": user_type}}


def test_fetch_pr_human_commenters_excludes_bots_and_self(monkeypatch):
    """Bots (Copilot reviewer, super-linter) and my own comments are
    excluded - only other humans count."""
    page_responses = {
        "/repos/o/r/issues/30/comments": [
            _commenter("zkoppert"),
            _commenter("super-linter[bot]", "Bot"),
        ],
        "/repos/o/r/pulls/30/comments": [
            _commenter("Copilot", "Bot"),
        ],
        "/repos/o/r/pulls/30/reviews": [
            _commenter("copilot-pull-request-reviewer[bot]", "Bot"),
        ],
    }

    def fake_run_gh(args, *, timeout=60):
        # args = ["api", "--paginate", "--slurp", "/repos/..."]
        endpoint = args[-1]
        # --slurp wraps each page in an outer array; here we have 1 page
        return json.dumps([page_responses[endpoint]])

    monkeypatch.setattr(triage, "run_gh", fake_run_gh)
    notif = _enable_dependabot_notif()
    result = triage.fetch_pr_human_commenters(notif, my_login="zkoppert")
    assert result == set()


def test_fetch_pr_human_commenters_includes_other_humans(monkeypatch):
    """Real human reviewers (User type, not me) are counted."""
    page_responses = {
        "/repos/o/r/issues/30/comments": [_commenter("andimiya")],
        "/repos/o/r/pulls/30/comments": [_commenter("iansan5653")],
        "/repos/o/r/pulls/30/reviews": [],
    }

    def fake_run_gh(args, *, timeout=60):
        return json.dumps([page_responses[args[-1]]])

    monkeypatch.setattr(triage, "run_gh", fake_run_gh)
    result = triage.fetch_pr_human_commenters(
        _enable_dependabot_notif(), my_login="zkoppert"
    )
    assert result == {"andimiya", "iansan5653"}


def test_fetch_pr_human_commenters_returns_none_on_api_failure(monkeypatch):
    """Any subprocess failure surfaces as None so the caller stays
    conservative."""

    def fake_run_gh(args, *, timeout=60):
        raise triage.subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(triage, "run_gh", fake_run_gh)
    result = triage.fetch_pr_human_commenters(
        _enable_dependabot_notif(), my_login="zkoppert"
    )
    assert result is None


def test_fetch_pr_human_commenters_returns_none_on_non_pr_url():
    """Issue URLs (no `/pulls/` segment) return None - the helper is
    PR-only."""
    notif = _notif(
        "author",
        subject={
            "title": "Enable Dependabot",
            "url": "https://api.github.com/repos/o/r/issues/30",
            "latest_comment_url": None,
            "type": "Issue",
        },
    )
    assert triage.fetch_pr_human_commenters(notif, my_login="zkoppert") is None


def test_fetch_pr_human_commenters_handles_bracket_substring_in_body(monkeypatch):
    """Regression: a comment body containing the literal substring ``][``
    (markdown reference link, array indexing, table cell, etc.) MUST
    NOT corrupt the parse. The earlier implementation used a custom
    split-on-`][` parser that fragmented these payloads and silently
    returned None, which made the rule no-op exactly when comments had
    non-trivial content."""
    page_responses = {
        "/repos/o/r/issues/30/comments": [
            {
                "user": {"login": "alice", "type": "User"},
                "body": "see [foo][bar] and arr[0][1]",
            }
        ],
        "/repos/o/r/pulls/30/comments": [],
        "/repos/o/r/pulls/30/reviews": [],
    }

    def fake_run_gh(args, *, timeout=60):
        return json.dumps([page_responses[args[-1]]])

    monkeypatch.setattr(triage, "run_gh", fake_run_gh)
    result = triage.fetch_pr_human_commenters(
        _enable_dependabot_notif(), my_login="zkoppert"
    )
    assert result == {"alice"}


def test_fetch_pr_human_commenters_handles_multi_page_slurp(monkeypatch):
    """--slurp returns [[page1],[page2],...] - the parser must flatten
    across pages."""
    page_responses = {
        "/repos/o/r/issues/30/comments": [
            [_commenter("alice")],
            [_commenter("bob")],
        ],
        "/repos/o/r/pulls/30/comments": [[]],
        "/repos/o/r/pulls/30/reviews": [[]],
    }

    def fake_run_gh(args, *, timeout=60):
        return json.dumps(page_responses[args[-1]])

    monkeypatch.setattr(triage, "run_gh", fake_run_gh)
    result = triage.fetch_pr_human_commenters(
        _enable_dependabot_notif(), my_login="zkoppert"
    )
    assert result == {"alice", "bob"}


def _flaky_notif(reason: str, title: str) -> dict:
    """Helper: open Issue with a flaky/intermittent test title under the
    given reason. Mirrors the github-ui pattern seen in production."""
    return _notif(
        reason,
        subject={
            "title": title,
            "url": "https://api.github.com/repos/o/r/issues/42",
            "latest_comment_url": None,
            "type": "Issue",
        },
    )


def test_classify_drops_intermittent_test_failure_team_mention():
    """The github-ui pattern: `team_mention` on an open Issue titled
    'Intermittent test failure: ...' must drop instead of landing in the
    inbox-by-default bucket."""
    notif = _flaky_notif(
        "team_mention",
        "Intermittent test failure: DashboardSidebarCollapsed#calls onSelectLink",
    )
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "intermittent test failure" in c.reason.lower()


def test_classify_drops_flaky_test_subscribed():
    """`flaky test` variant on a `subscribed` notification also drops."""
    notif = _flaky_notif("subscribed", "Flaky test: NuxDashboard#renders")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_drops_test_flake_team_mention():
    """`test flake` variant also drops when shaped as the bot output."""
    notif = _flaky_notif("team_mention", "test flake: foo#bar started failing")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_drops_title_match_is_case_insensitive():
    """Title matching ignores case - all-caps still drops."""
    notif = _flaky_notif("team_mention", "INTERMITTENT TEST FAILURE: thing")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_keeps_intermittent_test_failure_when_at_mentioned():
    """Direct @mention overrides the title drop - if someone explicitly
    pings Zack on a flaky-test issue, surface it instead of dropping."""
    notif = _flaky_notif(
        "mention",
        "Intermittent test failure: needs your attention",
    )
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_keeps_intermittent_test_failure_when_assigned():
    """Direct assignment overrides the title drop."""
    notif = _flaky_notif(
        "assign",
        "Intermittent test failure: please look",
    )
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_title_drop_does_not_match_unrelated_titles():
    """Regression guard: a title without any drop pattern still flows
    through normal classification (here: team_mention falls through to
    inbox-by-default)."""
    notif = _flaky_notif(
        "team_mention",
        "Real production bug: dashboard crashes",
    )
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket != triage.BUCKET_DROP


def test_classify_title_drop_does_not_match_phrase_as_substring():
    """Regression guard from review feedback: legitimate titles that
    contain the trigger phrase as a substring must NOT drop. Each of
    these is a real PR or bug we'd actually want to triage."""
    not_noise = [
        ("review_requested", "Fix flaky test in dashboard"),
        ("team_mention", "flaky test suite is failing CI completely"),
        ("team_mention", "Intermittent test failure modes - design doc"),
        ("subscribed", "test flake reproduction script"),
        ("team_mention", "investigation: why is the flaky test detector broken"),
    ]
    for reason, title in not_noise:
        notif = _flaky_notif(reason, title)
        c = triage.classify(
            notif,
            my_login="zkoppert",
            q1_logins=set(),
            state_fetcher=lambda _: "open",
            comment_fetcher=lambda _: (None, None),
            subject_author_fetcher=lambda _: "someone-else",
        )
        assert c.bucket != triage.BUCKET_DROP, (
            f"{reason!r} title {title!r} should not drop, got {c}"
        )


def test_classify_title_drop_matches_bracketed_prefix():
    """The github-ui bot prepends `[Bug]` to some intermittent-failure
    titles - the pattern must still match those."""
    notif = _flaky_notif(
        "team_mention",
        "[Bug] Intermittent test failure: useSectionData#fails sometimes",
    )
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
    )
    assert c.bucket == triage.BUCKET_DROP


def test_title_drop_patterns_constant_seeded():
    """Sanity check: the constant is non-empty and the three seed
    patterns are present."""
    sources = {p.pattern for p in triage.TITLE_DROP_PATTERNS}
    assert any("intermittent test failure" in p for p in sources)
    assert any("flaky test" in p for p in sources)
    assert any("test flake" in p for p in sources)


# --- Tests for read-notification sweep + done-archive (PR sweep-read-closed) ---


def _read_notif(reason: str, *, subject_type: str = "PullRequest", title: str = "Some PR") -> dict:
    """A notification the user has already viewed on github.com."""
    return {
        "id": "9999",
        "reason": reason,
        "unread": False,
        "repository": {"full_name": "github/example"},
        "subject": {
            "title": title,
            "url": "https://api.github.com/repos/github/example/pulls/42",
            "latest_comment_url": None,
            "type": subject_type,
        },
    }


def test_classify_read_open_notification_returns_keep():
    """Already-viewed open notifications must KEEP - don't re-route to inbox
    once the user has clicked on them."""
    c = triage.classify(
        _read_notif("manual"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_KEEP
    assert "already-read" in c.reason


def test_classify_unread_notification_does_not_return_keep():
    """The read-skip guard must NOT short-circuit unread notifications -
    those go through the full reason-based classifier."""
    notif = _read_notif("manual")
    notif["unread"] = True
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket != triage.BUCKET_KEEP


def test_classify_read_closed_pr_still_drops_via_state_check():
    """Read notifications on closed/merged subjects must still drop -
    KEEP only applies when no drop rule fired."""
    c = triage.classify(
        _read_notif("author"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "closed pullrequest" in c.reason


def test_classify_drop_sets_archive_to_done_when_zack_is_pr_author():
    """A closed/merged PR I authored must flag archive_to_done so the
    work shows up in todo.yml's done section for biannual reflection."""
    c = triage.classify(
        _read_notif("author"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "merged",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert c.archive_to_done is True


def test_classify_drop_does_not_archive_when_zack_is_not_author():
    """A closed/merged PR somebody else authored still drops, but never
    archives - those aren't my completed work."""
    c = triage.classify(
        _read_notif("assign"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andimiya",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert c.archive_to_done is False


def test_classify_drop_does_not_archive_for_issues():
    """Issues never archive to done even when I'm the closer - the
    biannual reflection use case is about shipped PR work, not closed
    issues."""
    notif = _read_notif("author", subject_type="Issue", title="Some issue")
    notif["subject"]["url"] = "https://api.github.com/repos/github/example/issues/42"
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert c.archive_to_done is False


def test_classify_drop_does_not_call_author_fetcher_for_issues():
    """Avoid the extra API call for issues - we never archive issues so
    skip the author lookup entirely."""
    fetcher_calls = []

    def fetcher(_):
        fetcher_calls.append(_)
        return "zkoppert"

    notif = _read_notif("author", subject_type="Issue", title="Some issue")
    notif["subject"]["url"] = "https://api.github.com/repos/github/example/issues/42"
    triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=fetcher,
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert fetcher_calls == []


def test_build_done_archive_entry_has_expected_shape():
    """The archived done entry must include the fields the biannual
    reflection workflow expects: link, title, completed date, source."""
    entry = triage.build_done_archive_entry(_read_notif("author"))
    assert entry["status"] == "done"
    assert entry["source"] == "github-notification-auto-archive"
    assert entry["link"] == "https://github.com/github/example/pull/42"
    assert "github/example" in entry["title"]
    assert entry["completed"]  # ISO date string set


def test_run_archives_to_done_and_marks_notification_done(todo_file):
    """End-to-end: a read+merged PR I authored must (a) DELETE the
    notification, (b) append a done entry to todo.yml."""
    notif = _read_notif("author")
    notif["id"] = "888"

    delete_calls = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "DELETE" in cmd:
            delete_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications?all=true": json.dumps([notif]),
            "/repos/github/example/pulls/42": json.dumps(
                {
                    "state": "closed",
                    "merged_at": "2026-06-07T00:00:00Z",
                    "locked": False,
                    "user": {"login": "zkoppert"},
                }
            ),
        }
        idx = cmd.index("api")
        after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
        path = after[0] if after else ""
        return subprocess.CompletedProcess(
            cmd, 0, stdout=responses.get(path, ""), stderr=""
        )

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)

    assert stats.dropped == 1
    assert stats.archived_to_done == 1
    assert any("/notifications/threads/888" in " ".join(c) for c in delete_calls)
    # Done section should now have one new auto-archive entry.
    data = yaml.safe_load(todo_file.read_text())
    archived = [
        e
        for e in data.get("done", [])
        if e.get("source") == "github-notification-auto-archive"
    ]
    assert len(archived) == 1


def test_run_skipped_read_does_not_add_to_inbox_or_delete(todo_file):
    """A read+open notification must not be re-added to inbox AND must
    not be marked done on GitHub - it just falls out of the loop."""
    notif = _read_notif("manual")
    notif["id"] = "777"

    delete_calls = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "DELETE" in cmd:
            delete_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications?all=true": json.dumps([notif]),
            "/repos/github/example/pulls/42": json.dumps(
                {"state": "open", "locked": False}
            ),
        }
        idx = cmd.index("api")
        after = [a for a in cmd[idx + 1 :] if not a.startswith("-")]
        path = after[0] if after else ""
        return subprocess.CompletedProcess(
            cmd, 0, stdout=responses.get(path, ""), stderr=""
        )

    with patch("triage.subprocess.run", side_effect=fake_run):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)

    assert stats.skipped_read == 1
    assert stats.added_inbox == 0
    assert stats.added_q1 == 0
    assert delete_calls == []


def test_fetch_notifications_uses_all_true_query():
    """Sanity guard: the cron MUST request `?all=true` so it picks up
    notifications the user has read but not deleted. The whole sweep
    rule depends on this."""
    captured = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    with patch("triage.subprocess.run", side_effect=fake_run):
        triage.fetch_notifications()

    assert any("/notifications?all=true" in arg for c in captured for arg in c)


# --- Tests for review-feedback fixes: KEEP wrapper, archive dedup, prune-archive ---


def test_classify_read_ci_activity_still_drops():
    """ci_activity is always-drop noise - read status must not flip it to KEEP."""
    notif = _read_notif("ci_activity")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP, (
        "read ci_activity must drop so the notification gets cleared, "
        "not lingered as KEEP"
    )


def test_classify_read_comment_on_closed_pr_still_drops():
    """Read comments on already-closed PRs must still drop - otherwise
    they linger in the all=true fetch forever."""
    notif = _read_notif("comment")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: ("someone", "body"),
        subject_author_fetcher=lambda _: "andi",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "closed" in c.reason


def test_classify_read_subscribed_on_closed_pr_still_drops():
    """Read subscribed-thread notice on a closed PR must drop too."""
    notif = _read_notif("subscribed")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "merged",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_read_super_linter_comment_still_drops():
    """Super-linter comments are noise regardless of read status."""
    notif = _read_notif("comment")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: ("super-linter", "lint failed"),
        subject_author_fetcher=lambda _: "andi",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "super-linter" in c.reason


def test_classify_read_inbox_routing_swaps_to_keep():
    """Read notification that would otherwise route to INBOX must swap
    to KEEP. (manual subscription on open subject routes to INBOX.)"""
    notif = _read_notif("manual")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andi",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_KEEP
    assert "already-read" in c.reason
    assert "INBOX" in c.reason  # mentions what it would have been


def test_classify_read_q1_mention_swaps_to_keep():
    """Read notification that would route to Q1 also swaps to KEEP -
    user already saw the mention and chose not to act on it."""
    notif = _read_notif("mention")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andi",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_KEEP
    assert "Q1" in c.reason


def test_build_done_archive_entry_includes_notification_thread_id():
    """Archive entries MUST carry notification.thread_id so a failed
    mark_thread_done DELETE on one run doesn't cause duplicate archive
    writes on every subsequent run."""
    notif = {
        "id": "55555",
        "reason": "author",
        "repository": {"full_name": "github/example"},
        "subject": {
            "title": "Some PR",
            "url": "https://api.github.com/repos/github/example/pulls/42",
            "type": "PullRequest",
        },
    }
    entry = triage.build_done_archive_entry(notif)
    assert "notification" in entry
    assert entry["notification"]["thread_id"] == "55555"
    assert entry["notification"]["repo"] == "github/example"


def test_existing_thread_ids_finds_archived_entries():
    """existing_thread_ids must include archive entries so re-archive
    is suppressed when the prior DELETE failed and the notification
    reappears on the next fetch."""
    notif = {
        "id": "77777",
        "reason": "author",
        "repository": {"full_name": "github/example"},
        "subject": {
            "title": "X",
            "url": "https://api.github.com/repos/github/example/pulls/42",
            "type": "PullRequest",
        },
    }
    archived = triage.build_done_archive_entry(notif)
    data = {"inbox": [], "done": [archived], "prioritized": {}}
    ids = triage.existing_thread_ids(data)
    assert "77777" in ids


def test_build_done_archive_entry_from_tracked_carries_thread_id():
    """Pruner-built archive entries also carry the original
    notification.thread_id so dedup works on subsequent runs."""
    tracked = {
        "id": "pr-merged",
        "title": "Cleanup script (github/example)",
        "source": "github-notification",
        "notification": {
            "thread_id": "tid-123",
            "url": "https://api.github.com/repos/github/example/pulls/42",
            "reason": "author",
            "repo": "github/example",
        },
    }
    entry = triage.build_done_archive_entry_from_tracked(tracked)
    assert entry["status"] == "done"
    assert entry["source"] == "github-notification-auto-archive"
    assert entry["notification"]["thread_id"] == "tid-123"
    assert entry["notification"]["reason"] == "author"
    assert entry["title"] == "Cleanup script (github/example)"


def _stale_self_authored_entry(entry_id: str, pr_number: int) -> dict:
    """Tracked entry for a PR I authored, ready to be archived on prune."""
    return {
        "id": entry_id,
        "title": f"My PR #{pr_number} (github/example)",
        "source": "github-notification",
        "notification": {
            "url": f"https://github.com/github/example/pull/{pr_number}",
            "thread_id": f"thr-{pr_number}",
            "reason": "author",
            "repo": "github/example",
        },
    }


def test_prune_archives_self_authored_pr_to_done():
    """When the pruner drops a tracked PR with reason=author, it must
    add an entry to data['done'] so the work survives for biannual
    reflection."""
    stats = triage.TriageStats()
    entry = _stale_self_authored_entry("my-pr", 42)
    data = {"inbox": [entry], "prioritized": {}, "done": []}
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["inbox"] == []
    assert len(data["done"]) == 1
    assert data["done"][0]["source"] == "github-notification-auto-archive"
    assert data["done"][0]["notification"]["thread_id"] == "thr-42"
    assert stats.archived_to_done == 1


def test_prune_does_not_archive_non_author_pr():
    """A tracked PR with reason!=author (e.g., mention, review_requested)
    still drops on close but must not get archived as my work."""
    stats = triage.TriageStats()
    entry = _stale_self_authored_entry("mention-pr", 99)
    entry["notification"]["reason"] = "mention"
    data = {"inbox": [entry], "prioritized": {}, "done": []}
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["inbox"] == []
    assert data["done"] == []
    assert stats.archived_to_done == 0


def test_prune_does_not_archive_self_authored_issue():
    """Only PRs get archived - reason=author on an issue (rare) still
    drops but does not pollute the done section."""
    stats = triage.TriageStats()
    entry = _stale_self_authored_entry("my-issue", 7)
    entry["notification"]["url"] = "https://github.com/github/example/issues/7"
    data = {"inbox": [entry], "prioritized": {}, "done": []}
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["inbox"] == []
    assert data["done"] == []
    assert stats.archived_to_done == 0


def test_prune_archives_from_quadrant_too():
    """Quadrant-tracked self-authored PRs also get archived on close."""
    stats = triage.TriageStats()
    entry = _stale_self_authored_entry("q1-mine", 11)
    data = {
        "inbox": [],
        "prioritized": {"q1_do_first": [entry]},
        "done": [],
    }
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    assert data["prioritized"]["q1_do_first"] == []
    assert len(data["done"]) == 1
    assert data["done"][0]["notification"]["thread_id"] == "thr-11"


def test_prune_archive_visible_to_dedup_on_next_run():
    """End-to-end: after pruner archives a self-authored PR, the
    archive entry's thread_id is in existing_thread_ids - so even if
    mark_thread_done failed and GitHub re-delivers the notification,
    the run loop's already_tracked check will catch it."""
    stats = triage.TriageStats()
    entry = _stale_self_authored_entry("dedupe-test", 88)
    data = {"inbox": [entry], "prioritized": {}, "done": []}
    with patch("triage.run_gh", side_effect=_all_prs_merged), patch(
        "triage.mark_thread_done"
    ):
        triage.prune_stale_notifications(data, stats)
    ids = triage.existing_thread_ids(data)
    assert "thr-88" in ids


# --- Tests for SUBSCRIPTION_FILTERED_REPOS (NUX subscription filter) ---


def _nux_notif(reason: str, *, subject_type: str = "PullRequest") -> dict:
    """A notification on github/new-user-experience with the given reason."""
    return {
        "id": "nux-1",
        "reason": reason,
        "unread": True,
        "repository": {"full_name": "github/new-user-experience"},
        "subject": {
            "title": "Some NUX notification",
            "url": "https://api.github.com/repos/github/new-user-experience/pulls/1",
            "latest_comment_url": None,
            "type": subject_type,
        },
    }


def test_classify_nux_subscribed_drops():
    """Subscribed-thread noise on github/new-user-experience drops -
    I only want directed pings from that repo."""
    c = triage.classify(
        _nux_notif("subscribed"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "subscription allowlist" in c.reason


def test_classify_nux_comment_drops():
    """Plain comment notifications on NUX repo drop - if I was
    @-mentioned in the comment the reason would be `mention`."""
    c = triage.classify(
        _nux_notif("comment"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: ("andimiya", "lgtm"),
        subject_author_fetcher=lambda _: "andimiya",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "subscription allowlist" in c.reason


def test_classify_nux_ci_activity_drops():
    """ci_activity on NUX drops via the subscription filter (it would
    drop anyway via the always-drop ci_activity rule - both paths agree)."""
    c = triage.classify(
        _nux_notif("ci_activity"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_nux_author_drops():
    """My own PRs on NUX (reason=author) drop too - I don't want the
    cron tracking PRs I authored unless someone pings me on them."""
    c = triage.classify(
        _nux_notif("author"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_nux_mention_still_routes_normally():
    """An @-mention on NUX is a directed ping - keep the normal
    Q1 routing for it."""
    c = triage.classify(
        _nux_notif("mention"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andimiya",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_nux_assign_still_routes_normally():
    """Assign on NUX is a directed ping - Q1."""
    c = triage.classify(
        _nux_notif("assign"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andimiya",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_nux_review_requested_still_routes_normally():
    """review_requested on NUX is a directed ping - either Q1 (if PR
    author is on the NUX teammate list) or INBOX."""
    c = triage.classify(
        _nux_notif("review_requested"),
        my_login="zkoppert",
        q1_logins={"andimiya"},
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andimiya",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_nux_team_mention_still_routes_normally():
    """team_mention on NUX is a team-level directed ping - INBOX
    (team_mention isn't in Q1_REASONS, so it gets the default INBOX
    routing; the point is the subscription filter doesn't DROP it)."""
    c = triage.classify(
        _nux_notif("team_mention"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "andimiya",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_classify_non_filtered_repo_subscribed_still_routes_to_inbox():
    """Repos NOT in SUBSCRIPTION_FILTERED_REPOS keep the normal
    subscribed→INBOX routing."""
    notif = _nux_notif("subscribed")
    notif["repository"]["full_name"] = "github/some-other-repo"
    notif["subject"]["url"] = "https://api.github.com/repos/github/some-other-repo/pulls/1"
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_classify_nux_filter_runs_after_closed_state_drop():
    """A closed/merged PR on NUX still drops via the closed-state rule
    (cheaper than the subscription filter), and self-authored ones
    still archive to done."""
    notif = _nux_notif("author")
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "merged",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "zkoppert",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "merged pullrequest" in c.reason
    assert c.archive_to_done is True


def test_classify_nux_filter_runs_before_keep_wrapper():
    """A read subscribed notification on NUX should DROP (so the cron
    actually clears it), not KEEP. The subscription filter must run
    before the read-notification KEEP override."""
    notif = _nux_notif("subscribed")
    notif["unread"] = False
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_classify_nux_security_alert_still_routes_to_q1():
    """security_alert on NUX must still reach Q1 - vulnerabilities
    and secret scans are too important to silently drop."""
    c = triage.classify(
        _nux_notif("security_alert"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_subscription_filter_is_case_insensitive():
    """Repo full_name lookup is lowercased so future entries for
    mixed-case repos (e.g. github/CodeQL) don't silently miss."""
    notif = _nux_notif("subscribed")
    notif["repository"]["full_name"] = "GitHub/New-User-Experience"
    c = triage.classify(
        notif,
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
        subject_author_fetcher=lambda _: "someone-else",
        human_commenter_fetcher=lambda _, my_login: set(),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "subscription allowlist" in c.reason
