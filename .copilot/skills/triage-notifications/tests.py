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
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_assign_goes_to_q1():
    c = triage.classify(
        _notif("assign"),
        my_login="zkoppert",
        q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
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
# load_todo / write_todo_atomic / existing_thread_ids / items_to_mark_read
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
                    "notification": {"thread_id": "999", "marked_read": True},
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


def test_items_to_mark_read_picks_done_with_thread_id():
    # `done-archived` has no `status` field but lives in the `done:`
    # section, so it must still be picked up. `q1` has status=done and a
    # thread_id, so it's picked up. The marked_read item is skipped.
    items = triage.items_to_mark_read(_sample_todo())
    titles = sorted(i["title"] for i in items)
    assert titles == ["done-archived", "q1"]


def test_items_to_mark_read_skips_in_progress_without_done_status():
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
    assert triage.items_to_mark_read(data) == []


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
        "/notifications": json.dumps([_notif("mention")]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.already_tracked == 1
    assert stats.added_q1 == 0


def test_run_adds_q1_for_mention(todo_file):
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications": json.dumps([_notif("mention")]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.added_q1 == 1
    data = yaml.safe_load(todo_file.read_text())
    q1 = data["prioritized"]["q1_do_first"]
    assert len(q1) == 1
    assert q1[0]["quadrant"] == "q1_do_first"


def test_run_drops_ci_activity_and_marks_read(todo_file):
    notif = _notif("ci_activity", id="555")
    patch_calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "PATCH" in cmd:
            patch_calls.append(tuple(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications": json.dumps([notif]),
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
    assert any("/notifications/threads/555" in " ".join(c) for c in patch_calls)


def test_run_dry_run_does_not_write(todo_file):
    before = todo_file.read_text()
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications": json.dumps([_notif("mention")]),
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(
            ["--todo-file", str(todo_file), "--dry-run", "--no-notify"]
        )
        stats = triage.run(args)
    assert stats.added_q1 == 1
    assert todo_file.read_text() == before


def test_run_marks_read_on_done(todo_file):
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

    patch_paths: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd and "PATCH" in cmd:
            # last positional is the path
            patch_paths.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        responses = {
            "/user": json.dumps({"login": "zkoppert"}),
            "/notifications": json.dumps([]),
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

    assert stats.marked_read_on_done == 1
    assert any("/notifications/threads/777" in p for p in patch_paths)
    data = yaml.safe_load(todo_file.read_text())
    entry = data["prioritized"]["q1_do_first"][0]
    assert entry["notification"]["marked_read"] is True
    assert entry["notification"]["marked_read_at"] == (
        datetime.date.today().isoformat()
    )


def test_run_handles_empty_notifications(todo_file):
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications": "[]",
    }
    with patch("triage.subprocess.run", side_effect=_gh_returns(responses)):
        args = triage.parse_args(["--todo-file", str(todo_file), "--no-notify"])
        stats = triage.run(args)
    assert stats.fetched == 0
    assert stats.errors == []


def test_main_returns_zero_on_success(todo_file):
    responses = {
        "/user": json.dumps({"login": "zkoppert"}),
        "/notifications": "[]",
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


def test_check_subject_stale_discussion_could_not_resolve_drops():
    parsed = {"owner": "o", "repo": "r", "kind": "discussion", "number": 1}
    err = _called_process_error(1, "Could not resolve to a Repository with the name")
    with _mock_run_gh(side_effect=err):
        action, _ = triage.check_subject_stale(parsed)
    assert action == triage.STALE_DROP


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
        "/notifications": json.dumps([]),
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


def test_run_prune_marks_thread_read_so_it_does_not_reappear(todo_file):
    """Regression: pruner must mark the underlying thread read, otherwise the
    next cron cycle re-fetches the unread notification and re-adds the inbox
    entry the pruner just dropped (verified bug from gpt-5.4 review).
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
    patch_calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "-X" in cmd and "PATCH" in cmd:
            patch_calls.append(tuple(cmd))
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
        "/notifications/threads/123" in " ".join(c) for c in patch_calls
    ), "pruner must PATCH the thread so the next run doesn't re-add it"

    # Simulate next cron cycle: GitHub now omits the thread (it's been marked
    # read), so nothing should be re-added.
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


def test_run_prune_dry_run_does_not_mark_thread_read(todo_file):
    """Dry-run must not PATCH /notifications/threads even during pruning."""
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
    patch_calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "-X" in cmd and "PATCH" in cmd:
            patch_calls.append(tuple(cmd))
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
    assert patch_calls == [], "dry-run must not PATCH threads"
