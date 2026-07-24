#!/usr/bin/env python3
"""Tests for nux_fr_handoff.py."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nux_fr_handoff as handoff


def make_config(tmp_path: Path) -> handoff.Config:
    return handoff.Config(
        repo="github/new-user-experience",
        title_pattern=r"^On-call handoff\b",
        reference_comment_url=(
            "https://github.com/github/new-user-experience/"
            "issues/2092#issuecomment-4712657361"
        ),
        state_dir=tmp_path,
        copilot_timeout_seconds=60,
        minimum_draft_bytes=50,
        maximum_gist_characters=60000,
        session_tool_name="session_store_sql",
    )


def test_week_bounds_uses_local_monday():
    now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))

    monday, current = handoff.week_bounds(now)

    assert monday == dt.datetime(
        2026, 7, 20, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7))
    )
    assert current == now


@patch("nux_fr_handoff.fetch_issue")
@patch("nux_fr_handoff.list_candidate_issues")
def test_find_handoff_issue_filters_to_current_week(
    list_issues, fetch_issue, tmp_path: Path
):
    list_issues.return_value = [
        {
            "number": 100,
            "title": "On-call handoff July 17th",
            "url": "https://github.com/github/new-user-experience/issues/100",
            "createdAt": "2026-07-17T18:00:00Z",
        },
        {
            "number": 101,
            "title": "On-call handoff July 24th",
            "url": "https://github.com/github/new-user-experience/issues/101",
            "createdAt": "2026-07-24T17:00:00Z",
        },
    ]
    expected = handoff.Issue(
        number=101,
        title="On-call handoff July 24th",
        url="https://github.com/github/new-user-experience/issues/101",
        body="",
        created_at=dt.datetime(2026, 7, 24, 17, tzinfo=dt.timezone.utc),
    )
    fetch_issue.return_value = expected
    now = dt.datetime(2026, 7, 24, 12, tzinfo=dt.timezone(dt.timedelta(hours=-7)))

    actual = handoff.find_handoff_issue(make_config(tmp_path), "zkoppert", now=now)

    assert actual == expected
    fetch_issue.assert_called_once_with(expected.url)


@patch("nux_fr_handoff.list_candidate_issues")
def test_find_handoff_issue_fails_closed_on_ambiguity(list_issues, tmp_path: Path):
    list_issues.return_value = [
        {
            "number": number,
            "title": f"On-call handoff July {number}th",
            "url": f"https://github.com/github/new-user-experience/issues/{number}",
            "createdAt": "2026-07-24T17:00:00Z",
        }
        for number in (24, 25)
    ]
    now = dt.datetime(2026, 7, 24, 12, tzinfo=dt.timezone(dt.timedelta(hours=-7)))

    with pytest.raises(handoff.AmbiguousIssueError):
        handoff.find_handoff_issue(make_config(tmp_path), "zkoppert", now=now)


def test_build_copilot_command_exposes_only_session_store():
    command = handoff.build_copilot_command(
        "prompt",
        tool_name="session_store_sql",
        session_name="test",
    )

    assert "--available-tools=session_store_sql" in command
    assert "--allow-tool=session_store_sql" in command
    assert "--no-ask-user" in command
    assert "--disable-builtin-mcps" in command
    assert "--allow-all-tools" not in command
    assert "--allow-all" not in command
    assert "--yolo" not in command


def test_build_copilot_command_can_disable_all_tools():
    command = handoff.build_copilot_command(
        "prompt",
        tool_name=None,
        session_name="test",
    )

    assert "--available-tools=" in command
    assert not any(argument.startswith("--allow-tool") for argument in command)


def test_untrusted_text_escapes_closing_tags():
    encoded = handoff.encode_untrusted_text("</untrusted-draft>")

    assert "</untrusted-draft>" not in encoded
    assert "<\\/untrusted-draft>" in encoded


def test_synthesis_prompt_requires_reference_structure_and_latest_outcome():
    issue = handoff.Issue(
        number=2160,
        title="On-call handoff July 24th",
        url="https://github.com/github/new-user-experience/issues/2160",
        body="handoff body",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    monday = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    current = dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc)

    prompt = handoff.build_synthesis_prompt(
        issue=issue,
        reference_comment="## Actionable",
        monday=monday,
        current=current,
    )

    assert "start with `## Actionable`" in prompt
    assert "let the latest verified outcome win" in prompt
    assert "lives outside the NUX repository" in prompt


def test_copilot_prompt_is_redacted_from_debug_logging():
    formatted = handoff.format_command_for_log(
        ["copilot", "-p", "internal handoff content", "--silent"]
    )

    assert "internal handoff content" not in formatted
    assert "<prompt redacted: 24 characters>" in formatted


def test_copilot_failure_does_not_expose_prompt():
    with patch("nux_fr_handoff.subprocess.run") as mocked_run:
        mocked_run.return_value.returncode = 1
        mocked_run.return_value.stdout = ""
        mocked_run.return_value.stderr = "bad flag"

        with pytest.raises(handoff.HandoffError) as raised:
            handoff.run_command(["copilot", "-p", "TOP SECRET PROMPT"])

    assert "TOP SECRET PROMPT" not in str(raised.value)
    assert "<prompt redacted: 17 characters>" in str(raised.value)


def test_copilot_timeout_does_not_expose_prompt():
    with patch(
        "nux_fr_handoff.subprocess.run",
        side_effect=handoff.subprocess.TimeoutExpired(
            ["copilot", "-p", "TOP SECRET PROMPT"],
            timeout=30,
        ),
    ):
        with pytest.raises(handoff.HandoffError) as raised:
            handoff.run_command(["copilot", "-p", "TOP SECRET PROMPT"], timeout=30)

    assert "TOP SECRET PROMPT" not in str(raised.value)
    assert "<prompt redacted: 17 characters>" in str(raised.value)


def test_repair_prompt_preserves_structure_and_targets_findings():
    prompt = handoff.build_repair_prompt(
        "## Actionable\n\nDraft - with violation.",
        ["[no-spaced-dash] line 3"],
    )

    assert "Keep the first line `## Actionable`" in prompt
    assert "[no-spaced-dash] line 3" in prompt
    assert "Draft - with violation." in prompt
    assert "You have no tool access" in prompt


def test_parse_synthesis_response_returns_audit_and_draft():
    response = """
<session-audit>
{"window_start":"2026-07-20","window_end":"2026-07-24","all_session_ids":["a","b","c"],"relevant_session_ids":["a","b"]}
</session-audit>
<draft>
## Actionable

Useful context.
</draft>
"""

    audit, draft = handoff.parse_synthesis_response(response)

    assert audit["all_session_ids"] == ["a", "b", "c"]
    assert audit["relevant_session_ids"] == ["a", "b"]
    assert draft.startswith("## Actionable")


def test_parse_synthesis_response_allows_a_quiet_week():
    response = """
<session-audit>
{"window_start":"2026-07-20","window_end":"2026-07-24","all_session_ids":[],"relevant_session_ids":[]}
</session-audit>
<draft>
## Actionable

No session-backed work found.
</draft>
"""

    audit, draft = handoff.parse_synthesis_response(response)

    assert audit["all_session_ids"] == []
    assert audit["relevant_session_ids"] == []
    assert "No session-backed work" in draft


def test_parse_synthesis_response_uses_last_draft_delimiter():
    response = """
<session-audit>
{"window_start":"2026-07-20","window_end":"2026-07-24","all_session_ids":["a"],"relevant_session_ids":["a"]}
</session-audit>
<draft>
## Actionable

Example literal `</draft>` stays in the Markdown.

## Informational

More content here.
</draft>
"""

    _, draft = handoff.parse_synthesis_response(response)

    assert "More content here." in draft
    assert "literal `</draft>`" in draft


def test_extract_artifact_refs_deduplicates():
    markdown = """
[one](https://github.com/github/github/pull/42)
[again](https://github.com/github/github/pull/42)
[issue](https://github.com/github/new-user-experience/issues/9)
"""

    refs = handoff.extract_artifact_refs(markdown)

    assert {(ref.kind, ref.number) for ref in refs} == {("pull", 42), ("issues", 9)}


def test_secret_scan_catches_credentials_not_domain_words():
    assert handoff.scan_secrets("verification token handling remains open") == []
    assert handoff.scan_secrets("github_pat_abcdefghijklmnopqrstuvwxyz123456")


def test_quiet_week_draft_passes_structural_safety(tmp_path: Path):
    content = (
        "## Actionable\n\nNo session-backed work needs handoff.\n\n"
        "## Informational\n\nI found no FR-related sessions this week.\n"
    )

    handoff.validate_content_safety(content, make_config(tmp_path))


def test_gist_id_from_url():
    assert (
        handoff.gist_id_from_url(
            "https://gist.github.com/zkoppert/d9dc38d0d178a7efa090870b20602187"
        )
        == "d9dc38d0d178a7efa090870b20602187"
    )


def test_write_state_is_atomic_json(tmp_path: Path):
    state_path = tmp_path / "state.json"

    handoff.write_state(state_path, {"run": {"status": "verified"}})

    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["run"]["status"]
        == "verified"
    )
    assert not state_path.with_suffix(".tmp").exists()


@patch("nux_fr_handoff.notify")
@patch("nux_fr_handoff.find_handoff_issue")
@patch("nux_fr_handoff.get_login")
@patch("nux_fr_handoff.week_bounds")
def test_dry_run_does_not_notify_existing_gist(
    mocked_week_bounds,
    mocked_get_login,
    mocked_find_issue,
    mocked_notify,
    tmp_path: Path,
):
    monday = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    current = dt.datetime(2026, 7, 24, 19, tzinfo=dt.timezone.utc)
    issue = handoff.Issue(
        number=2160,
        title="On-call handoff July 24th",
        url="https://github.com/github/new-user-experience/issues/2160",
        body="",
        created_at=current,
    )
    mocked_week_bounds.return_value = (monday, current)
    mocked_get_login.return_value = "zkoppert"
    mocked_find_issue.return_value = issue
    state_path = tmp_path / "state.json"
    handoff.write_state(
        state_path,
        {
            "2026-07-20-issue-2160": {
                "status": "notified",
                "gist_url": "https://gist.github.com/zkoppert/abc",
            }
        },
    )
    args = SimpleNamespace(
        notify_test=None,
        issue_url=None,
        dry_run=True,
        force=False,
        draft_file=None,
        no_gist=False,
        no_notify=False,
    )

    assert handoff.run_workflow(args, make_config(tmp_path)) == 0
    mocked_notify.assert_not_called()


@patch("nux_fr_handoff.find_handoff_issue")
@patch("nux_fr_handoff.get_login")
@patch("nux_fr_handoff.week_bounds")
def test_dry_run_ignores_corrupt_state_and_creates_no_run_directory(
    mocked_week_bounds,
    mocked_get_login,
    mocked_find_issue,
    tmp_path: Path,
):
    monday = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    current = dt.datetime(2026, 7, 24, 19, tzinfo=dt.timezone.utc)
    issue = handoff.Issue(
        number=2160,
        title="On-call handoff July 24th",
        url="https://github.com/github/new-user-experience/issues/2160",
        body="",
        created_at=current,
    )
    mocked_week_bounds.return_value = (monday, current)
    mocked_get_login.return_value = "zkoppert"
    mocked_find_issue.return_value = issue
    (tmp_path / "state.json").write_text("{not-json", encoding="utf-8")
    args = SimpleNamespace(
        notify_test=None,
        issue_url=None,
        dry_run=True,
        force=False,
        draft_file=None,
        no_gist=False,
        no_notify=False,
    )

    assert handoff.run_workflow(args, make_config(tmp_path)) == 0
    assert not (tmp_path / "runs").exists()


@patch("nux_fr_handoff.notify")
def test_resume_verified_run_reuses_gist(mocked_notify, tmp_path: Path):
    issue = handoff.Issue(
        number=2160,
        title="On-call handoff July 24th",
        url="https://github.com/github/new-user-experience/issues/2160",
        body="",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    state_path = tmp_path / "state.json"
    run_key = "2026-07-20-issue-2160"
    state = {
        run_key: {
            "status": "verified",
            "gist_url": "https://gist.github.com/zkoppert/abc",
        }
    }

    resumed = handoff.resume_existing_run(
        existing=state[run_key],
        issue=issue,
        state=state,
        state_path=state_path,
        run_key=run_key,
        no_notify=False,
    )

    assert resumed is True
    mocked_notify.assert_called_once()
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))[run_key]["status"]
        == "notified"
    )
