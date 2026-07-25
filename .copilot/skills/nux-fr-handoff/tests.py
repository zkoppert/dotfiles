#!/usr/bin/env python3
"""Tests for nux_fr_handoff.py."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nux_fr_handoff as handoff


def make_config(tmp_path: Path) -> handoff.Config:
    return handoff.Config(
        repo="acme/on-call",
        title_pattern=r"^On-call handoff\b",
        reference_comment_url=(
            "https://github.com/acme/on-call/" "issues/120#issuecomment-456"
        ),
        state_dir=tmp_path,
        copilot_timeout_seconds=60,
        minimum_draft_bytes=50,
        maximum_gist_characters=60000,
    )


def test_committed_placeholder_config_requires_user_override(tmp_path: Path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                "repo: example-org/on-call",
                'title_pattern: "^On-call handoff\\\\b"',
                (
                    "reference_comment_url: "
                    "https://github.com/example-org/on-call/issues/120#issuecomment-456"
                ),
                f'state_dir: "{tmp_path}"',
                "copilot_timeout_seconds: 60",
                "minimum_draft_bytes: 50",
                "maximum_gist_characters: 60000",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(handoff.HandoffError, match="configure"):
        handoff.load_config(config_path)


def test_installer_rejects_existing_real_skill_directory(tmp_path: Path):
    home = tmp_path / "home"
    target = home / ".copilot/skills/nux-fr-handoff"
    target.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("python3", "copilot", "gh", "terminal-notifier"):
        script = fake_bin / command
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USER": "tester",
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
    )
    installer = Path(__file__).resolve().parent / "install.sh"

    result = subprocess.run(
        ["bash", str(installer), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "exists and is not a symlink" in result.stderr
    assert target.is_dir()
    assert not target.is_symlink()


def test_installer_rejects_unmanaged_command(tmp_path: Path):
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("python3", "copilot", "gh", "terminal-notifier"):
        script = fake_bin / command
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    managed_skill = home / ".copilot/skills/nux-fr-handoff"
    managed_skill.parent.mkdir(parents=True)
    managed_skill.symlink_to(Path(__file__).resolve().parent)
    command_path = home / ".local/bin/nux-fr-handoff"
    command_path.parent.mkdir(parents=True)
    command_path.write_text("#!/bin/sh\necho user-managed\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USER": "tester",
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
    )
    installer = Path(__file__).resolve().parent / "install.sh"

    result = subprocess.run(
        ["bash", str(installer)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "not managed by nux-fr-handoff" in result.stderr
    assert "user-managed" in command_path.read_text(encoding="utf-8")


def test_installer_rerun_through_skill_symlink_keeps_physical_target(tmp_path: Path):
    home = tmp_path / "home"
    source = Path(__file__).resolve().parent
    skill_target = home / ".copilot/skills/nux-fr-handoff"
    skill_target.parent.mkdir(parents=True)
    skill_target.symlink_to(source)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in (
        "python3",
        "copilot",
        "gh",
        "terminal-notifier",
        "plutil",
        "launchctl",
    ):
        script = fake_bin / command
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USER": "tester",
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
    )

    result = subprocess.run(
        ["bash", str(skill_target / "install.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert skill_target.is_symlink()
    assert skill_target.resolve() == source


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
            "url": "https://github.com/acme/on-call/issues/100",
            "createdAt": "2026-07-17T18:00:00Z",
        },
        {
            "number": 101,
            "title": "On-call handoff July 24th",
            "url": "https://github.com/acme/on-call/issues/101",
            "createdAt": "2026-07-24T17:00:00Z",
        },
    ]
    expected = handoff.Issue(
        number=101,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/101",
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
            "url": f"https://github.com/acme/on-call/issues/{number}",
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
    assert "--no-custom-instructions" in command
    assert "--disable-builtin-mcps" in command
    assert "--allow-all-tools" not in command
    assert "--allow-all" not in command
    assert "--yolo" not in command


def test_session_tool_name_is_not_user_configurable():
    assert handoff.SESSION_TOOL_NAME == "session_store_sql"


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
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
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
    assert "exclude any session named `NUX FR handoff ...`" in prompt
    assert "Update the draft handoff comment using the live GitHub" in prompt
    assert "Repair the draft so it passes the exact automated findings" in prompt


def test_copilot_prompt_is_redacted_from_debug_logging():
    formatted = handoff.format_command_for_log(
        ["copilot", "-p", "internal handoff content", "--silent"]
    )

    assert "internal handoff content" not in formatted
    assert "<prompt redacted: 24 characters>" in formatted


def test_copilot_failure_does_not_expose_prompt():
    with patch("nux_fr_handoff.subprocess.run") as mocked_run:
        mocked_run.return_value.returncode = 1
        mocked_run.return_value.stdout = "partial private draft"
        mocked_run.return_value.stderr = "echoed private prompt"

        with pytest.raises(handoff.HandoffError) as raised:
            handoff.run_command(["copilot", "-p", "TOP SECRET PROMPT"])

    assert "TOP SECRET PROMPT" not in str(raised.value)
    assert "partial private draft" not in str(raised.value)
    assert "echoed private prompt" not in str(raised.value)
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


def test_parse_synthesis_response_allows_text_between_blocks():
    response = """
<session-audit>
{"window_start":"2026-07-20","window_end":"2026-07-24","all_session_ids":["a"],"relevant_session_ids":["a"]}
</session-audit>
Here is the draft:
<draft>
## Actionable

Nothing open.

## Informational

One completed item.
</draft>
"""

    _, draft = handoff.parse_synthesis_response(response)

    assert draft.startswith("## Actionable")


def test_extract_artifact_refs_deduplicates():
    markdown = """
[one](https://github.com/acme/widgets/pull/42)
[again](https://github.com/acme/widgets/pull/42)
[issue](https://github.com/acme/on-call/issues/9)
"""

    refs = handoff.extract_artifact_refs(markdown)

    assert {(ref.kind, ref.number) for ref in refs} == {("pull", 42), ("issues", 9)}


@patch("nux_fr_handoff.run_command")
def test_fetch_pull_state_includes_live_review_context(mocked_run):
    mocked_run.return_value = SimpleNamespace(
        stdout=json.dumps(
            {
                "url": "https://github.com/acme/widgets/pull/42",
                "title": "Improve widgets",
                "state": "OPEN",
                "isDraft": False,
                "mergedAt": None,
                "closedAt": None,
                "assignees": [{"login": "owner"}],
                "reviewDecision": "REVIEW_REQUIRED",
                "reviewRequests": [{"login": "reviewer"}],
            }
        )
    )
    ref = handoff.ArtifactRef(
        owner="acme",
        repo="widgets",
        kind="pull",
        number=42,
        url="https://github.com/acme/widgets/pull/42",
    )

    states = handoff.fetch_artifact_states([ref])

    assert states[0]["review_decision"] == "REVIEW_REQUIRED"
    assert states[0]["review_requests"] == ["reviewer"]
    command = mocked_run.call_args.args[0]
    assert command[:4] == ["gh", "pr", "view", "42"]


@patch("nux_fr_handoff.run_command", side_effect=handoff.HandoffError("not found"))
def test_artifact_refresh_fails_closed(mocked_run):
    ref = handoff.ArtifactRef(
        owner="acme",
        repo="widgets",
        kind="pull",
        number=404,
        url="https://github.com/acme/widgets/pull/404",
    )

    with pytest.raises(handoff.HandoffError, match="refusing stale handoff"):
        handoff.fetch_artifact_states([ref])

    mocked_run.assert_called_once()


def test_secret_scan_catches_credentials_not_domain_words():
    assert handoff.scan_secrets("verification token handling remains open") == []
    assert handoff.scan_secrets("github_pat_abcdefghijklmnopqrstuvwxyz123456")
    assert handoff.scan_secrets("-----BEGIN DSA PRIVATE KEY-----")
    assert handoff.scan_secrets("-----BEGIN ENCRYPTED PRIVATE KEY-----")


def test_artifact_count_limit_fails_before_refresh():
    refs = [
        handoff.ArtifactRef(
            owner="acme",
            repo="widgets",
            kind="issues",
            number=number,
            url=f"https://github.com/acme/widgets/issues/{number}",
        )
        for number in range(handoff.MAX_ARTIFACTS + 1)
    ]

    with pytest.raises(handoff.HandoffError, match="maximum"):
        handoff.validate_artifact_count(refs)


def test_final_draft_cannot_add_unrefreshed_artifacts():
    refreshed = {
        ("acme", "widgets", "issues", 1),
    }
    markdown = (
        "[known](https://github.com/acme/widgets/issues/1) "
        "[new](https://github.com/acme/widgets/issues/2)"
    )

    with pytest.raises(handoff.HandoffError, match="not refreshed"):
        handoff.validate_final_artifacts(markdown, refreshed)


@patch("nux_fr_handoff.run_copilot")
@patch("nux_fr_handoff.fetch_artifact_states")
def test_supplied_draft_uses_live_artifact_refresh(
    mocked_fetch,
    mocked_copilot,
    tmp_path: Path,
):
    draft = (
        "## Actionable\n\n"
        "[PR](https://github.com/acme/widgets/pull/42)\n\n"
        "## Informational\n\nDone.\n"
    )
    mocked_fetch.return_value = [{"url": "https://github.com/acme/widgets/pull/42"}]
    mocked_copilot.return_value = draft

    refreshed, keys = handoff.refresh_draft_artifacts(
        draft,
        config=make_config(tmp_path),
        current=dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc),
    )

    assert refreshed == draft.strip()
    assert keys == {("acme", "widgets", "pull", 42)}
    mocked_fetch.assert_called_once()
    mocked_copilot.assert_called_once()


def test_quiet_week_draft_passes_structural_safety(tmp_path: Path):
    content = (
        "## Actionable\n\nNo session-backed work needs handoff.\n\n"
        "## Informational\n\nI found no FR-related sessions this week.\n"
    )

    handoff.validate_content_safety(content, make_config(tmp_path))


def test_gist_id_from_url():
    assert (
        handoff.gist_id_from_url(
            "https://gist.github.com/octocat/d9dc38d0d178a7efa090870b20602187"
        )
        == "d9dc38d0d178a7efa090870b20602187"
    )


@patch("nux_fr_handoff.run_command")
def test_update_secret_gist_reuses_existing_id(mocked_run, tmp_path: Path):
    draft_path = tmp_path / "new-name.md"
    draft_path.write_text("updated content", encoding="utf-8")

    url = handoff.update_secret_gist(
        "https://gist.github.com/octocat/abc123",
        draft_path,
        previous_filename="old-name.md",
    )

    assert url == "https://gist.github.com/octocat/abc123"
    command = mocked_run.call_args.args[0]
    payload = json.loads(mocked_run.call_args.kwargs["input_text"])
    assert command == [
        "gh",
        "api",
        "--method",
        "PATCH",
        "gists/abc123",
        "--input",
        "-",
    ]
    assert payload["files"]["old-name.md"]["content"] == "updated content"


@patch("nux_fr_handoff.create_secret_gist")
@patch("nux_fr_handoff.update_secret_gist")
def test_force_persists_by_updating_existing_gist(
    mocked_update, mocked_create, tmp_path
):
    draft_path = tmp_path / "draft.md"
    mocked_update.return_value = "https://gist.github.com/octocat/abc123"

    result = handoff.persist_secret_gist(
        draft_path,
        issue_number=123,
        force=True,
        previous_gist_url="https://gist.github.com/octocat/abc123",
        previous_filename="old.md",
    )

    assert result == "https://gist.github.com/octocat/abc123"
    mocked_update.assert_called_once_with(
        "https://gist.github.com/octocat/abc123",
        draft_path,
        previous_filename="old.md",
    )
    mocked_create.assert_not_called()


def test_write_state_is_atomic_json(tmp_path: Path):
    state_path = tmp_path / "state.json"

    handoff.write_state(state_path, {"run": {"status": "verified"}})

    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["run"]["status"]
        == "verified"
    )
    assert not state_path.with_suffix(".tmp").exists()


def test_notification_group_uses_current_user(monkeypatch):
    monkeypatch.setenv("USER", "first.responder")

    assert handoff.notification_group("123") == "com.first.responder.nux-fr-handoff.123"


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
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
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
            "2026-07-20-issue-123": {
                "status": "notified",
                "gist_url": "https://gist.github.com/octocat/abc",
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


@patch("nux_fr_handoff.load_config", side_effect=AssertionError("config loaded"))
@patch("nux_fr_handoff.notify")
def test_notification_test_skips_workflow_config(mocked_notify, _mocked_load):
    result = handoff.main(["--notify-test", "https://gist.github.com/octocat/abc123"])

    assert result == 0
    mocked_notify.assert_called_once()


@patch("nux_fr_handoff.fetch_issue")
@patch("nux_fr_handoff.get_login")
def test_explicit_issue_url_skips_user_lookup(mocked_get_login, mocked_fetch_issue):
    expected = handoff.Issue(
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
        body="",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    mocked_fetch_issue.return_value = expected
    args = SimpleNamespace(issue_url=expected.url)

    login, issue = handoff.resolve_handoff_issue(args, make_config(Path("/tmp")))

    assert login == ""
    assert issue == expected
    mocked_get_login.assert_not_called()


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
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
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
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
        body="",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    state_path = tmp_path / "state.json"
    run_key = "2026-07-20-issue-123"
    state = {
        run_key: {
            "status": "verified",
            "gist_url": "https://gist.github.com/octocat/abc",
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


@patch("nux_fr_handoff.notify")
@patch("nux_fr_handoff.verify_gist")
@patch("nux_fr_handoff.update_secret_gist")
def test_resume_draft_validated_updates_existing_gist(
    mocked_update,
    mocked_verify,
    mocked_notify,
    tmp_path: Path,
):
    issue = handoff.Issue(
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
        body="",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    draft_path = tmp_path / "new-name.md"
    draft_path.write_text("updated content", encoding="utf-8")
    run_key = "2026-07-20-issue-123"
    state_path = tmp_path / "state.json"
    state = {
        run_key: {
            "status": "draft_validated",
            "gist_url": "https://gist.github.com/octocat/abc",
            "gist_filename": "old-name.md",
            "draft_path": str(draft_path),
            "draft_sha256": handoff.sha256_text("updated content"),
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
    mocked_update.assert_called_once_with(
        "https://gist.github.com/octocat/abc",
        draft_path,
        previous_filename="old-name.md",
    )
    mocked_verify.assert_called_once_with(
        "https://gist.github.com/octocat/abc",
        draft_path,
        "updated content",
        gist_filename="old-name.md",
    )
    mocked_notify.assert_called_once()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))[run_key]
    assert persisted["status"] == "notified"
    assert persisted["gist_filename"] == "old-name.md"


@patch("nux_fr_handoff.collect_draft_check_findings", return_value=[])
@patch("nux_fr_handoff.validate_content_safety")
@patch("nux_fr_handoff.week_bounds")
@patch("nux_fr_handoff.resolve_handoff_issue")
def test_force_no_gist_preserves_previous_gist_state_without_uploading(
    mocked_resolve,
    mocked_week_bounds,
    _mocked_safety,
    _mocked_findings,
    tmp_path: Path,
):
    monday = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    current = dt.datetime(2026, 7, 24, 19, tzinfo=dt.timezone.utc)
    issue = handoff.Issue(
        number=123,
        title="On-call handoff July 24th",
        url="https://github.com/acme/on-call/issues/123",
        body="",
        created_at=current,
    )
    mocked_resolve.return_value = ("tester", issue)
    mocked_week_bounds.return_value = (monday, current)
    run_key = "2026-07-20-issue-123"
    handoff.write_state(
        tmp_path / "state.json",
        {
            run_key: {
                "status": "notified",
                "gist_url": "https://gist.github.com/octocat/abc",
                "gist_filename": "old.md",
                "draft_path": str(tmp_path / "old.md"),
            }
        },
    )
    provided_draft = tmp_path / "provided.md"
    provided_draft.write_text(
        "## Actionable\n\nNothing.\n\n## Informational\n\nDone.\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        notify_test=None,
        issue_url=None,
        dry_run=False,
        force=True,
        draft_file=str(provided_draft),
        no_gist=True,
        no_notify=True,
    )

    assert handoff.run_workflow(args, make_config(tmp_path)) == 0
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))[
        run_key
    ]
    assert persisted["status"] == "notified"
    assert persisted["gist_url"] == "https://gist.github.com/octocat/abc"
