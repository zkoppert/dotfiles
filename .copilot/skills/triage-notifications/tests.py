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
        notif, my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: None, comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_assign_goes_to_q1():
    c = triage.classify(
        _notif("assign"), my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: None, comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_security_alert_goes_to_q1():
    c = triage.classify(
        _notif("security_alert"), my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: None, comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_classify_manual_goes_to_inbox():
    c = triage.classify(
        _notif("manual"), my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: None, comment_fetcher=lambda _: (None, None),
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
        my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: ("someone", "hi"),
    )
    assert c.bucket == triage.BUCKET_DROP
    assert "closed" in c.reason


def test_comment_on_merged_thread_drops():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: "merged",
        comment_fetcher=lambda _: ("someone", "hi"),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_comment_with_mention_goes_to_q1():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: ("teammate", "hey @zkoppert can you look?"),
    )
    assert c.bucket == triage.BUCKET_Q1


def test_super_linter_without_mention_drops():
    c = triage.classify(
        _notif("comment"),
        my_login="zkoppert", q1_logins=set(),
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
        my_login="zkoppert", q1_logins=set(),
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
        my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: None,
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_subscribed_open_goes_to_inbox():
    c = triage.classify(
        _notif("subscribed"),
        my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: "open",
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_INBOX


def test_subscribed_closed_drops():
    c = triage.classify(
        _notif("subscribed"),
        my_login="zkoppert", q1_logins=set(),
        state_fetcher=lambda _: "closed",
        comment_fetcher=lambda _: (None, None),
    )
    assert c.bucket == triage.BUCKET_DROP


def test_unknown_reason_falls_back_to_inbox():
    c = triage.classify(
        _notif("invitation"),
        my_login="zkoppert", q1_logins=set(),
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
        subject={"title": "X", "url": "", "type": "Discussion", "latest_comment_url": None},
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
                {"id": "b", "title": "q1", "status": "done",
                 "notification": {"thread_id": "222"}},
                {"id": "b2", "title": "q1-done-already-marked", "status": "done",
                 "notification": {"thread_id": "999", "marked_read": True}},
            ],
            "q2_schedule": [],
        },
        # Entries here mirror real zkoppert-todo/todo.yml: items moved to the
        # top-level `done:` section don't carry a `status` field. The mark-
        # read loop must treat `done:` membership alone as proof of done.
        "done": [
            {"id": "c", "title": "done-archived",
             "completed": "2026-01-01", "category": "personal",
             "notification": {"thread_id": "333"}},
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
            {"id": "x", "title": "wip", "status": "in_progress",
             "notification": {"thread_id": "777"}},
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
            {"id": "old", "title": "x",
             "notification": {"thread_id": "1001"}},
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
            cmd, 0, stdout=responses.get(path, ""), stderr="",
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
    todo_file.write_text(yaml.safe_dump({
        "inbox": [],
        "prioritized": {
            "q1_do_first": [
                {"id": "doneone", "title": "x", "status": "done",
                 "notification": {"thread_id": "777"}},
            ],
        },
        "done": [],
    }))

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
            cmd, 0, stdout=responses.get(path, ""), stderr="",
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
