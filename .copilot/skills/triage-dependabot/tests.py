"""Tests for triage_dependabot.

The strategy mirrors triage-notifications/tests.py: mock every subprocess
boundary, exercise the decision tree branches one at a time, then drive
``run()`` end-to-end with fully mocked gh/copilot/state calls.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import triage_dependabot as td


def test_load_private_triage_repos_reads_config(tmp_path: Path) -> None:
    config_path = tmp_path / "triage-repos.yml"
    config_path.write_text(
        "\n".join(
            [
                "dependabot_skipped_repos:",
                "  - acme/private-thing",
            ]
        ),
        encoding="utf-8",
    )

    data = td.load_private_triage_repos(config_path)

    assert td._private_repo_set(data, "dependabot_skipped_repos") == {
        "acme/private-thing"
    }


def test_load_private_triage_repos_missing_file_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "missing.yml"

    data = td.load_private_triage_repos(config_path)

    assert data == {}
    assert "using public defaults" in caplog.text


@pytest.fixture(autouse=True)
def _stub_archive_lookup(request: Any) -> Any:
    """Default ``is_archived_repo`` to False for every test.

    Bug 2 added a per-loop archive check that talks to ``gh api``. Without
    this fixture every end-to-end test would hit the network (slowing the
    suite and flaking offline) and the archived-repo regression test
    would have to fight a cached True value from a prior run. Tests that
    exercise ``is_archived_repo`` directly opt out by marking themselves
    with ``@pytest.mark.no_archive_stub`` so the real function runs.
    """
    td._ARCHIVED_REPO_CACHE.clear()
    if request.node.get_closest_marker("no_archive_stub"):
        yield None
        td._ARCHIVED_REPO_CACHE.clear()
        return
    patcher = mock.patch.object(td, "is_archived_repo", return_value=False)
    patcher.start()
    try:
        yield patcher
    finally:
        patcher.stop()
        td._ARCHIVED_REPO_CACHE.clear()


# ---------------------------------------------------------------------------
# parse_pr_subject / is_dependabot_pr
# ---------------------------------------------------------------------------


def test_parse_pr_subject_returns_repo_and_number() -> None:
    notif = {
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/zkoppert/dotfiles/pulls/42",
        }
    }
    assert td.parse_pr_subject(notif) == ("zkoppert/dotfiles", 42)


def test_parse_pr_subject_ignores_non_pull_requests() -> None:
    notif = {"subject": {"type": "Issue", "url": "x"}}
    assert td.parse_pr_subject(notif) is None


def test_parse_pr_subject_returns_none_for_malformed_url() -> None:
    assert td.parse_pr_subject({"subject": {"type": "PullRequest", "url": ""}}) is None
    assert td.parse_pr_subject({"subject": {"type": "PullRequest", "url": "x"}}) is None
    assert (
        td.parse_pr_subject(
            {"subject": {"type": "PullRequest", "url": "https://api.github.com/foo"}}
        )
        is None
    )


def test_parse_pr_subject_returns_none_when_subject_missing() -> None:
    assert td.parse_pr_subject({}) is None


def test_is_dependabot_pr_true_for_known_logins() -> None:
    assert td.is_dependabot_pr({"author": {"login": "dependabot[bot]"}})
    assert td.is_dependabot_pr({"author": {"login": "dependabot-preview[bot]"}})


def test_is_dependabot_pr_true_for_app_prefixed_login() -> None:
    # `gh pr view --json author` returns App author logins prefixed with `app/`.
    assert td.is_dependabot_pr({"author": {"login": "app/dependabot"}})
    assert td.is_dependabot_pr({"author": {"login": "app/dependabot-preview"}})


def test_is_dependabot_pr_false_for_humans_and_missing() -> None:
    assert not td.is_dependabot_pr({"author": {"login": "zkoppert"}})
    assert not td.is_dependabot_pr({})


def test_is_bot_helper() -> None:
    assert td._is_bot("renovate[bot]")
    assert td._is_bot("dependabot[bot]")
    assert td._is_bot("app/dependabot")
    assert not td._is_bot("zkoppert")


def test_skipped_dependency_match_in_title() -> None:
    pr = {"title": "Bump super-linter/super-linter from 7.0.0 to 8.0.0", "body": ""}
    assert td.skipped_dependency_match(pr) == "super-linter/super-linter"


def test_skipped_dependency_match_in_grouped_body() -> None:
    # Grouped Dependabot PRs hide the dependency name behind a generic title and
    # list each bump in the body. The helper must check the body too.
    pr = {
        "title": "chore(deps): bump the github-actions group",
        "body": (
            "Bumps the github-actions group with 1 update:\n"
            "- [super-linter/super-linter](https://github.com/super-linter/super-linter) "
            "from 7.0.0 to 8.0.0\n"
        ),
    }
    assert td.skipped_dependency_match(pr) == "super-linter/super-linter"


def test_skipped_dependency_match_case_insensitive() -> None:
    pr = {"title": "Bump Super-Linter/Super-Linter from 7 to 8", "body": ""}
    match = td.skipped_dependency_match(pr)
    assert match is not None
    assert match.lower() == "super-linter/super-linter"


def test_skipped_dependency_match_negative() -> None:
    pr = {"title": "Bump actions/checkout from 4 to 5", "body": "no skips here"}
    assert td.skipped_dependency_match(pr) is None
    # Bare "super-linter" must NOT match (we require the action coordinate).
    pr_bare = {"title": "Refactor super-linter test fixture", "body": ""}
    assert td.skipped_dependency_match(pr_bare) is None


# ---------------------------------------------------------------------------
# Semver bump detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Bump foo from 1.2.3 to 1.2.4", td.BUMP_PATCH),
        ("Bump foo from 1.2.3 to 1.3.0", td.BUMP_MINOR),
        ("Bump foo from 1.2.3 to 2.0.0", td.BUMP_MAJOR),
        ("Bump foo from v1.2.3 to v1.2.4", td.BUMP_PATCH),
        ("chore(deps): update foo", td.BUMP_UNKNOWN),
        ("", td.BUMP_UNKNOWN),
        ("Bump foo from 1 to 2", td.BUMP_MAJOR),
    ],
)
def test_parse_bump_from_title(title: str, expected: str) -> None:
    assert td.parse_bump_from_title(title) == expected


def test_parse_bump_from_body_returns_highest_for_grouped() -> None:
    body = (
        "Bumps the prod group with 2 updates:\n"
        "- Bumps foo from 1.2.3 to 1.2.4\n"
        "- Bumps bar from 1.0.0 to 2.0.0\n"
    )
    assert td.parse_bump_from_body(body) == td.BUMP_MAJOR


def test_parse_bump_from_body_empty() -> None:
    assert td.parse_bump_from_body("") == td.BUMP_UNKNOWN
    assert td.parse_bump_from_body("no version info here") == td.BUMP_UNKNOWN


def test_detect_bump_grouped_uses_body() -> None:
    pr = {
        "title": "Bump the npm group with 3 updates",
        "body": "- bumps a from 1.0.0 to 1.0.1\n- bumps b from 2.0.0 to 3.0.0",
    }
    assert td.detect_bump(pr) == td.BUMP_MAJOR


def test_detect_bump_title_when_not_grouped() -> None:
    assert (
        td.detect_bump({"title": "Bump foo from 1.0.0 to 1.0.1", "body": ""})
        == td.BUMP_PATCH
    )


def test_detect_bump_falls_back_to_body() -> None:
    pr = {"title": "chore(deps): update", "body": "Bumps foo from 1.0.0 to 2.0.0"}
    assert td.detect_bump(pr) == td.BUMP_MAJOR


def test_detect_bump_does_not_misclassify_groupdate() -> None:
    # Substring match on "group" used to misclassify the "groupdate" package
    # as a grouped PR. The grouped pattern requires "bump the X group".
    pr = {
        "title": "Bump groupdate from 6.4.0 to 6.4.1",
        "body": "",
    }
    assert td.detect_bump(pr) == td.BUMP_PATCH


def test_detect_bump_grouped_pattern_recognised() -> None:
    # The strict "bump the X group" pattern should still match real grouped PRs
    # even when the title also names the group.
    pr = {
        "title": "Bump the rspec-suite group with 4 updates",
        "body": "- bumps rspec from 3.0 to 3.1\n- bumps rspec-mocks from 3.0 to 3.1",
    }
    assert td.detect_bump(pr) == td.BUMP_MINOR


# ---------------------------------------------------------------------------
# Coverage detection
# ---------------------------------------------------------------------------


def test_detect_repo_coverage_extracts_highest_value() -> None:
    files = {
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 75",
        "Makefile": "test:\n\tpytest --cov-fail-under=92",
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        assert td.detect_repo_coverage("zkoppert/dotfiles") == 92


def test_detect_repo_coverage_returns_none_when_no_signal() -> None:
    with mock.patch.object(
        td, "run_gh", side_effect=td.subprocess.CalledProcessError(1, "gh")
    ):
        assert td.detect_repo_coverage("z/r") is None


def test_detect_repo_coverage_extracts_simplecov_line_and_branch() -> None:
    # github/markup has ``SimpleCov.minimum_coverage line: 100, branch: 100``
    # in test/test_helper.rb. Lowest of the two gates is the one that fails
    # the build first, so we report the lowest (which is 100 here).
    files = {
        "test/test_helper.rb": (
            "require 'simplecov'\n"
            "SimpleCov.minimum_coverage line: 100, branch: 100\n"
        ),
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        assert td.detect_repo_coverage("github/markup") == 100


def test_detect_repo_coverage_extracts_simplecov_single_number() -> None:
    files = {
        "spec/spec_helper.rb": (
            "SimpleCov.start\nSimpleCov.minimum_coverage 90\n"
        ),
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        assert td.detect_repo_coverage("z/r") == 90


def test_detect_repo_coverage_extracts_simplecov_float() -> None:
    files = {
        ".simplecov": "SimpleCov.minimum_coverage 80.5\n",
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        # Float value is floored to int (matches the integer threshold
        # semantics used elsewhere in the pipeline).
        assert td.detect_repo_coverage("z/r") == 80


def test_detect_repo_coverage_simplecov_lowest_when_line_below_branch() -> None:
    files = {
        "test/test_helper.rb": "SimpleCov.minimum_coverage line: 80, branch: 95\n",
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        assert td.detect_repo_coverage("z/r") == 80


def test_detect_repo_coverage_simplecov_block_form() -> None:
    """github/markup uses bare ``minimum_coverage`` inside ``SimpleCov.start``.

    The prefix is optional, but only counted because SimpleCov appears in
    the file (guards against false positives on unrelated Ruby DSL).
    """
    files = {
        "test/test_helper.rb": (
            'require "simplecov"\n'
            "SimpleCov.start do\n"
            "  enable_coverage :branch\n"
            '  add_filter "/test/"\n'
            '  command_name "MarkupTests"\n'
            "  minimum_coverage line: 100, branch: 100\n"
            "end\n"
        ),
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        assert td.detect_repo_coverage("github/markup") == 100


def test_detect_repo_coverage_simplecov_ignored_without_simplecov_marker() -> None:
    """Unrelated files with a ``minimum_coverage`` DSL must not match."""
    files = {
        "Rakefile": "task :coverage do\n  minimum_coverage 50\nend\n",
    }

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        for name, body in files.items():
            if name in args[1]:
                return body
        raise FileNotFoundError(args[1])

    with mock.patch.object(td, "run_gh", side_effect=fake_run_gh):
        assert td.detect_repo_coverage("z/r") is None


# ---------------------------------------------------------------------------
# Human-activity / CI / rebase
# ---------------------------------------------------------------------------


def test_humans_engaged_skips_self_and_bots() -> None:
    pr = {
        "comments": [
            {"author": {"login": "zkoppert"}, "body": "ok"},
            {"author": {"login": "dependabot[bot]"}, "body": "ok"},
        ],
        "reviews": [],
    }
    assert not td.humans_engaged(pr, my_login="zkoppert")


def test_humans_engaged_true_when_human_comment_present() -> None:
    pr = {
        "comments": [{"author": {"login": "iansan5653"}, "body": "lgtm"}],
        "reviews": [],
    }
    assert td.humans_engaged(pr, my_login="zkoppert")


def test_humans_engaged_true_when_human_review_present() -> None:
    pr = {
        "comments": [],
        "reviews": [{"author": {"login": "andimiya"}, "state": "APPROVED"}],
    }
    assert td.humans_engaged(pr, my_login="zkoppert")


def test_humans_engaged_handles_missing_login() -> None:
    pr = {"comments": [{"author": None, "body": "anon"}], "reviews": []}
    assert not td.humans_engaged(pr, my_login="zkoppert")


def test_summarize_checks_states() -> None:
    assert td.summarize_checks({}) == "none"
    assert (
        td.summarize_checks({"statusCheckRollup": [{"conclusion": "SUCCESS"}]})
        == "passing"
    )
    assert (
        td.summarize_checks(
            {
                "statusCheckRollup": [
                    {"status": "IN_PROGRESS"},
                    {"conclusion": "SUCCESS"},
                ]
            }
        )
        == "pending"
    )
    assert (
        td.summarize_checks(
            {
                "statusCheckRollup": [
                    {"conclusion": "FAILURE"},
                    {"conclusion": "SUCCESS"},
                ]
            }
        )
        == "failing"
    )


def test_summarize_checks_action_required_is_failing() -> None:
    # ACTION_REQUIRED, STARTUP_FAILURE and STALE all indicate the check needs
    # attention and should not be treated as passing for auto-merge purposes.
    for conclusion in ("ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"):
        rollup = {"statusCheckRollup": [{"conclusion": conclusion}]}
        assert td.summarize_checks(rollup) == "failing", conclusion


def test_summarize_checks_neutral_and_skipped_are_passing() -> None:
    rollup = {
        "statusCheckRollup": [
            {"conclusion": "SUCCESS"},
            {"conclusion": "NEUTRAL"},
            {"conclusion": "SKIPPED"},
        ]
    }
    assert td.summarize_checks(rollup) == "passing"


def test_summarize_checks_unknown_conclusion_is_failing() -> None:
    # An unrecognised future check conclusion is treated as failing so we err
    # toward flag-for-review rather than auto-merge.
    rollup = {"statusCheckRollup": [{"conclusion": "MYSTERY_STATE"}]}
    assert td.summarize_checks(rollup) == "failing"


def test_needs_rebase_comment_true_when_no_prior_comment() -> None:
    pr = {"comments": [], "commits": []}
    assert td.needs_rebase_comment(pr, my_login="zkoppert")


def test_needs_rebase_comment_suppressed_when_rebase_newer_than_push() -> None:
    pr = {
        "comments": [
            {
                "author": {"login": "zkoppert"},
                "body": "@dependabot rebase",
                "createdAt": "2026-06-05T10:00:00Z",
            }
        ],
        "commits": [{"committedDate": "2026-06-05T09:00:00Z"}],
    }
    assert not td.needs_rebase_comment(pr, my_login="zkoppert")


def test_needs_rebase_comment_true_when_push_newer_than_rebase() -> None:
    pr = {
        "comments": [
            {
                "author": {"login": "zkoppert"},
                "body": "@dependabot rebase",
                "createdAt": "2026-06-05T08:00:00Z",
            }
        ],
        "commits": [{"committedDate": "2026-06-05T10:00:00Z"}],
    }
    assert td.needs_rebase_comment(pr, my_login="zkoppert")


def test_needs_rebase_comment_true_when_commits_missing_date() -> None:
    pr = {
        "comments": [
            {
                "author": {"login": "zkoppert"},
                "body": "@dependabot rebase",
                "createdAt": "2026-06-05T08:00:00Z",
            }
        ],
        "commits": [{}],
    }
    assert td.needs_rebase_comment(pr, my_login="zkoppert")


# ---------------------------------------------------------------------------
# Security classification
# ---------------------------------------------------------------------------


def test_classify_security_via_copilot_security() -> None:
    with mock.patch.object(td, "_run_copilot", return_value="security"):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is True


def test_classify_security_via_copilot_normal() -> None:
    with mock.patch.object(td, "_run_copilot", return_value="normal"):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is False


def test_classify_security_via_copilot_unparseable() -> None:
    with mock.patch.object(td, "_run_copilot", return_value="maybe?"):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is None


def test_classify_security_via_copilot_ignores_substring_in_explanation() -> None:
    # An LLM that returns "not a security release; normal" used to be classified
    # as security because the substring "security" appeared. The exact-token match
    # uses only the final word, so this correctly resolves to normal (False).
    with mock.patch.object(
        td,
        "_run_copilot",
        return_value="not a security release; normal",
    ):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is False


def test_classify_security_via_copilot_ignores_explanation_without_keyword() -> None:
    # Explanatory text whose last token is not "security" or "normal" should
    # produce None rather than a false-positive classification.
    with mock.patch.object(
        td,
        "_run_copilot",
        return_value="this is not a security release; please review further",
    ):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is None


def test_classify_security_via_copilot_empty_output() -> None:
    with mock.patch.object(td, "_run_copilot", return_value=""):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is None


def test_classify_security_via_copilot_failure() -> None:
    with mock.patch.object(td, "_run_copilot", return_value=None):
        assert td.classify_security_via_copilot({"title": "x", "body": ""}) is None


def test_is_security_change_falls_back_to_regex() -> None:
    pr = {"title": "Bump foo", "body": "Fixes CVE-2026-0001"}
    with mock.patch.object(td, "classify_security_via_copilot", return_value=None):
        assert td.is_security_change(pr, use_copilot=True)


def test_is_security_change_no_signal() -> None:
    pr = {"title": "Bump foo from 1 to 2", "body": "ordinary changelog"}
    assert not td.is_security_change(pr, use_copilot=False)


def test_run_copilot_returns_none_on_missing_binary() -> None:
    with mock.patch.object(
        td.subprocess, "run", side_effect=FileNotFoundError("copilot")
    ):
        assert td._run_copilot("x", timeout=1) is None


def test_run_copilot_returns_stdout_on_success() -> None:
    result = mock.MagicMock(stdout="hello\n")
    with mock.patch.object(td.subprocess, "run", return_value=result):
        assert td._run_copilot("x", timeout=1) == "hello\n"


def test_run_copilot_defaults_to_no_tools() -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: Any) -> Any:
        captured["cmd"] = cmd
        return mock.MagicMock(stdout="ok\n")

    with mock.patch.object(td.subprocess, "run", side_effect=fake_run):
        td._run_copilot("x", timeout=1)

    assert "--allow-all-tools" not in captured["cmd"]


def test_run_copilot_passes_allow_tools_when_requested() -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: Any) -> Any:
        captured["cmd"] = cmd
        return mock.MagicMock(stdout="ok\n")

    with mock.patch.object(td.subprocess, "run", side_effect=fake_run):
        td._run_copilot("x", timeout=1, allow_tools=True)

    assert "--allow-all-tools" in captured["cmd"]


# ---------------------------------------------------------------------------
# State file / cooldown
# ---------------------------------------------------------------------------


def test_load_state_missing_returns_empty(tmp_path: Path) -> None:
    assert td.load_state(tmp_path / "missing.json") == {}


def test_load_state_corrupt_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{not json")
    assert td.load_state(state_file) == {}


def test_save_state_and_load_state_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    td.save_state(state_file, {"https://github.com/o/r/pull/1": 1.0})
    assert td.load_state(state_file) == {"https://github.com/o/r/pull/1": 1.0}


def test_in_cooldown() -> None:
    state = {"u": 100.0}
    assert td.in_cooldown(state, "u", now=200.0)
    assert not td.in_cooldown(state, "u", now=100.0 + td.ACTION_COOLDOWN_SECONDS + 1)
    assert not td.in_cooldown(state, "other", now=100.0)


# ---------------------------------------------------------------------------
# Decision tree (one test per branch)
# ---------------------------------------------------------------------------


def _base_pr(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": 1,
        "title": "Bump foo from 1.0.0 to 1.0.1",
        "body": "",
        "author": {"login": "dependabot[bot]"},
        "state": "open",
        "isDraft": False,
        "mergeStateStatus": "clean",
        "url": "https://github.com/o/r/pull/1",
        "labels": [],
        "reviews": [],
        "comments": [],
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    pr.update(overrides)
    return pr


def _decide(
    pr: dict[str, Any], *, coverage: int | None = 95, **kwargs: Any
) -> td.Decision:
    return td.decide(
        pr,
        my_login="zkoppert",
        repo="o/r",
        coverage_lookup=lambda _r: coverage,
        use_copilot=False,
        **kwargs,
    )


def test_decide_skip_when_closed() -> None:
    decision = _decide(_base_pr(state="closed"))
    assert decision.outcome == td.OUTCOME_SKIP
    assert decision.terminal is True


def test_decide_skip_when_ci_pending_is_not_terminal() -> None:
    # CI may go green on the next run, so the notification must come back.
    decision = _decide(_base_pr(statusCheckRollup=[{"status": "IN_PROGRESS"}]))
    assert decision.outcome == td.OUTCOME_SKIP
    assert decision.terminal is False


def test_decide_flag_unknown_bump_even_with_high_coverage() -> None:
    # Unparseable titles should always route to flag-for-review - high
    # coverage is not a license to merge something the parser couldn't read.
    pr = _base_pr(title="chore(deps): refresh transitive dependencies")
    decision = _decide(pr, coverage=99)
    assert decision.outcome == td.OUTCOME_FLAG
    assert decision.bump == td.BUMP_UNKNOWN


def test_decide_flag_when_draft() -> None:
    assert _decide(_base_pr(isDraft=True)).outcome == td.OUTCOME_FLAG


def test_decide_flag_when_human_engaged() -> None:
    pr = _base_pr(comments=[{"author": {"login": "iansan5653"}, "body": "hi"}])
    assert _decide(pr).outcome == td.OUTCOME_FLAG


def test_decide_rebase_when_behind() -> None:
    decision = _decide(_base_pr(mergeStateStatus="behind"))
    assert decision.outcome == td.OUTCOME_REBASE


def test_decide_rebase_suppressed_when_already_requested() -> None:
    pr = _base_pr(
        mergeStateStatus="behind",
        comments=[
            {
                "author": {"login": "zkoppert"},
                "body": "@dependabot rebase",
                "createdAt": "2026-06-02T00:00:00Z",
            }
        ],
    )
    assert _decide(pr).outcome == td.OUTCOME_SKIP


def test_decide_flag_major_bump_low_coverage() -> None:
    pr = _base_pr(title="Bump foo from 1.0.0 to 2.0.0")
    assert _decide(pr, coverage=50).outcome == td.OUTCOME_FLAG


def test_decide_flag_major_bump_unknown_coverage() -> None:
    pr = _base_pr(title="Bump foo from 1.0.0 to 2.0.0")
    assert _decide(pr, coverage=None).outcome == td.OUTCOME_FLAG


def test_decide_flag_when_coverage_lookup_raises() -> None:
    # If coverage_lookup blows up unexpectedly, decide() must catch it and
    # treat coverage as unknown rather than letting the whole run crash.
    def boom(_repo: str) -> int | None:
        raise RuntimeError("coverage lookup exploded")

    pr = _base_pr(title="Bump foo from 1.0.0 to 2.0.0")
    decision = td.decide(
        pr,
        my_login="zkoppert",
        repo="o/r",
        coverage_lookup=boom,
        use_copilot=False,
    )
    assert decision.outcome == td.OUTCOME_FLAG


def test_decide_merge_major_bump_high_coverage() -> None:
    pr = _base_pr(title="Bump foo from 1.0.0 to 2.0.0")
    assert _decide(pr, coverage=95).outcome == td.OUTCOME_MERGE


def test_decide_skip_ci_pending() -> None:
    pr = _base_pr(statusCheckRollup=[{"status": "IN_PROGRESS"}])
    assert _decide(pr).outcome == td.OUTCOME_SKIP


def test_decide_flag_ci_failing() -> None:
    pr = _base_pr(statusCheckRollup=[{"conclusion": "FAILURE"}])
    assert _decide(pr).outcome == td.OUTCOME_FLAG


def test_decide_merge_happy_path() -> None:
    decision = _decide(_base_pr())
    assert decision.outcome == td.OUTCOME_MERGE
    assert not decision.is_security


def test_decide_label_and_merge_when_security() -> None:
    pr = _base_pr(body="Fixes CVE-2026-1234")
    decision = _decide(pr)
    assert decision.outcome == td.OUTCOME_LABEL_AND_MERGE
    assert decision.is_security


# ---------------------------------------------------------------------------
# todo.yml integration
# ---------------------------------------------------------------------------


def test_make_todo_id_normalizes() -> None:
    assert td.make_todo_id("zkoppert/My_Repo.99", 7) == "dependabot-my-repo-99-pr-7"


def test_build_flag_entry_schema() -> None:
    pr = _base_pr(number=42, title="Bump foo", url="https://github.com/o/r/pull/42")
    entry = td.build_flag_entry(
        pr,
        "o/r",
        {"id": "thread-123", "reason": "subscribed"},
        td.Decision(td.OUTCOME_FLAG, "ci failing", bump=td.BUMP_MAJOR),
    )
    assert entry["id"] == "dependabot-r-pr-42"
    assert entry["quadrant"] == "q1_do_first"
    assert entry["category"] == "process"
    assert entry["source"] == "dependabot-triage"
    assert entry["notification"]["thread_id"] == "thread-123"
    assert entry["notification"]["pr_number"] == 42
    assert entry["notification"]["bump"] == td.BUMP_MAJOR


def test_existing_thread_ids_walks_all_buckets() -> None:
    data = {
        "inbox": [{"notification": {"thread_id": "a"}}],
        "done": [{"notification": {"thread_id": "b"}}],
        "in_progress": [{"notification": {"thread_id": "c"}}],
        "blocked": [{"notification": {"thread_id": "d"}}],
        "in_review": [{"notification": {"thread_id": "e"}}],
        "prioritized": {
            "q1_do_first": [{"notification": {"thread_id": "f"}}],
            "q2_schedule": [{"notification": {"thread_id": "g"}}],
        },
    }
    assert td.existing_thread_ids(data) == {"a", "b", "c", "d", "e", "f", "g"}


def test_existing_thread_ids_handles_missing_and_garbage() -> None:
    data = {
        "inbox": "not a list",
        "prioritized": {"q1_do_first": [None, {"notification": "garbage"}, {}]},
    }
    assert td.existing_thread_ids(data) == set()


def test_remove_stale_entries_matches_by_thread_id() -> None:
    data = {
        "inbox": [
            {"id": "stale", "notification": {"thread_id": "T1"}},
            {"id": "keep", "notification": {"thread_id": "T2"}},
        ],
        "done": [],
        "prioritized": {"q1_do_first": []},
    }
    removed = td.remove_stale_entries(data, thread_id="T1", pr_url=None)
    assert removed == 1
    assert [item["id"] for item in data["inbox"]] == ["keep"]


def test_remove_stale_entries_matches_by_pr_url_fallback() -> None:
    data = {
        "inbox": [
            {"id": "stale", "notification": {"thread_id": "OLD", "url": "https://example/pr/1"}},
            {"id": "keep", "notification": {"thread_id": "OTHER", "url": "https://example/pr/2"}},
        ],
        "prioritized": {"q1_do_first": []},
    }
    removed = td.remove_stale_entries(data, thread_id="T-new", pr_url="https://example/pr/1")
    assert removed == 1
    assert [item["id"] for item in data["inbox"]] == ["keep"]


def test_remove_stale_entries_walks_all_buckets() -> None:
    data = {
        "inbox": [{"notification": {"thread_id": "T"}}],
        "done": [{"notification": {"thread_id": "T"}}],
        "in_progress": [{"notification": {"thread_id": "T"}}],
        "blocked": [{"notification": {"thread_id": "T"}}],
        "in_review": [{"notification": {"thread_id": "T"}}],
        "prioritized": {
            "q1_do_first": [{"notification": {"thread_id": "T"}}],
            "q2_schedule": [{"notification": {"thread_id": "T"}}],
            "q3_delegate": [{"notification": {"thread_id": "T"}}],
            "q4_eliminate": [{"notification": {"thread_id": "T"}}],
        },
    }
    removed = td.remove_stale_entries(data, thread_id="T", pr_url=None)
    assert removed == 9
    for bucket in ("inbox", "done", "in_progress", "blocked", "in_review"):
        assert data[bucket] == []
    for quadrant in data["prioritized"].values():
        assert quadrant == []


def test_remove_stale_entries_no_match_returns_zero() -> None:
    data = {
        "inbox": [{"id": "x", "notification": {"thread_id": "T1"}}],
        "prioritized": {"q1_do_first": []},
    }
    removed = td.remove_stale_entries(data, thread_id="NOPE", pr_url="https://nope")
    assert removed == 0
    assert len(data["inbox"]) == 1


def test_remove_stale_entries_empty_inputs_short_circuit() -> None:
    data = {"inbox": [{"notification": {"thread_id": "T"}}]}
    assert td.remove_stale_entries(data, thread_id=None, pr_url=None) == 0
    assert td.remove_stale_entries(data, thread_id="", pr_url="") == 0


def test_remove_stale_entries_handles_garbage_data() -> None:
    data = {
        "inbox": "not a list",
        "done": [None, "string", {"notification": "garbage"}, {}],
        "prioritized": {
            "q1_do_first": [{"notification": {"thread_id": "T"}}],
            "q2_schedule": None,
        },
    }
    removed = td.remove_stale_entries(data, thread_id="T", pr_url=None)
    assert removed == 1
    assert data["prioritized"]["q1_do_first"] == []


def test_remove_stale_entries_mutates_buckets_in_place() -> None:
    """Bucket list/dict references must be preserved so ruamel CommentedSeq
    objects keep their round-trip comment metadata."""
    inbox = [{"notification": {"thread_id": "T"}}, {"id": "keep"}]
    q1 = [{"notification": {"thread_id": "T"}}]
    prioritized = {"q1_do_first": q1}
    data = {"inbox": inbox, "prioritized": prioritized}

    removed = td.remove_stale_entries(data, thread_id="T", pr_url=None)

    assert removed == 2
    assert data["inbox"] is inbox
    assert data["prioritized"] is prioritized
    assert data["prioritized"]["q1_do_first"] is q1
    assert inbox == [{"id": "keep"}]
    assert q1 == []


def test_load_todo_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        td.load_todo(tmp_path / "absent.yml")


def test_load_and_write_todo_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "todo.yml"
    path.write_text(
        "inbox: []\n" "prioritized:\n" "  q1_do_first: []\n" "done: []\n",
        encoding="utf-8",
    )
    data = td.load_todo(path)
    data["inbox"].append({"id": "x"})
    td.write_todo_atomic(path, data)
    reloaded = td.load_todo(path)
    assert reloaded["inbox"][0]["id"] == "x"


def test_write_todo_atomic_cleans_up_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "todo.yml"
    path.write_text("inbox: []\n", encoding="utf-8")

    with mock.patch.object(td.os, "replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            td.write_todo_atomic(path, {"inbox": []})

    leftovers = list(tmp_path.glob(".todo-*"))
    assert leftovers == []


def test_apply_todo_mutations_with_lock_preserves_concurrent_manual_edit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "todo.yml"
    path.write_text(
        "inbox: []\nprioritized:\n  q1_do_first: []\ndone: []\n",
        encoding="utf-8",
    )
    _stale_snapshot = td.load_todo(path)
    path.write_text(
        "inbox:\n"
        "  - id: manual-added\n"
        "    title: Manual edit\n"
        "prioritized:\n"
        "  q1_do_first: []\n"
        "done: []\n",
        encoding="utf-8",
    )
    entry = td.build_flag_entry(
        _base_pr(number=7, url="https://github.com/o/r/pull/7"),
        "o/r",
        {"id": "thread-new", "reason": "subscribed"},
        td.Decision(td.OUTCOME_FLAG, "needs review", bump=td.BUMP_MAJOR),
    )
    mutations = td.TodoMutations(
        flags=[td.FlagTodoDelta(entry=entry, pr_url="https://github.com/o/r/pull/7")]
    )

    applied = td.apply_todo_mutations_with_lock(path, mutations)

    assert applied["changed"] is True
    reloaded = td.load_todo(path)
    assert [item["id"] for item in reloaded["inbox"]] == ["manual-added"]
    assert [item["id"] for item in reloaded["prioritized"]["q1_do_first"]] == [
        "dependabot-r-pr-7"
    ]


def test_apply_todo_mutations_with_lock_acquires_file_lock(tmp_path: Path) -> None:
    path = tmp_path / "todo.yml"
    path.write_text(
        "inbox: []\nprioritized:\n  q1_do_first: []\ndone: []\n",
        encoding="utf-8",
    )
    entry = td.build_flag_entry(
        _base_pr(number=8, url="https://github.com/o/r/pull/8"),
        "o/r",
        {"id": "thread-lock", "reason": "subscribed"},
        td.Decision(td.OUTCOME_FLAG, "needs review"),
    )
    mutations = td.TodoMutations(
        flags=[td.FlagTodoDelta(entry=entry, pr_url="https://github.com/o/r/pull/8")]
    )

    with mock.patch.object(td.fcntl, "flock") as flock_mock:
        td.apply_todo_mutations_with_lock(path, mutations)

    assert flock_mock.call_args_list[0].args[1] == td.fcntl.LOCK_EX
    assert flock_mock.call_args_list[-1].args[1] == td.fcntl.LOCK_UN


def test_commit_todo_changes_skips_commit_when_nothing_staged(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / ".git").mkdir()
    todo_path = repo / "todo.yml"
    todo_path.write_text("inbox: []\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "commit" in cmd:
            raise AssertionError("commit should not run with an empty staged diff")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch.object(td.subprocess, "run", side_effect=fake_run):
        committed = td.commit_todo_changes(
            todo_path,
            "Record Dependabot triage todo updates",
        )

    assert committed is False
    assert any("diff" in call for call in calls)
    assert not any("commit" in call for call in calls)


def _staged_commit_fake_run(
    calls: list[list[str]], *, fail_step: str | None = None
):
    """Fake subprocess.run for commit_todo_changes with a staged diff.

    ``diff --cached --quiet`` returns 1 (changes staged) so the commit
    proceeds. ``fail_step`` (e.g. "commit", "pull", "push") raises a
    CalledProcessError when that token appears in the git command.
    """

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if fail_step is not None and fail_step in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_commit_todo_changes_scopes_commit_to_todo_file(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / ".git").mkdir()
    todo_path = repo / "todo.yml"
    todo_path.write_text("inbox: []\n", encoding="utf-8")
    calls: list[list[str]] = []

    with mock.patch.object(
        td.subprocess, "run", side_effect=_staged_commit_fake_run(calls)
    ):
        result = td.commit_todo_changes(todo_path, "msg")

    assert result is True
    commit_cmd = next(c for c in calls if "commit" in c)
    # The commit must be scoped to todo.yml so unrelated staged files in
    # the repo are never swept into the cron's commit.
    assert commit_cmd[-2:] == ["--", "todo.yml"]


def test_commit_todo_changes_aborts_rebase_on_pull_failure(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    (repo / ".git").mkdir()
    todo_path = repo / "todo.yml"
    todo_path.write_text("inbox: []\n", encoding="utf-8")
    calls: list[list[str]] = []

    with mock.patch.object(
        td.subprocess,
        "run",
        side_effect=_staged_commit_fake_run(calls, fail_step="pull"),
    ):
        result = td.commit_todo_changes(todo_path, "msg")

    # A failed rebase must be aborted so the repo is not left wedged.
    assert result is True
    assert any("rebase" in c and "--abort" in c for c in calls)


def test_commit_todo_changes_push_failure_returns_true(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / ".git").mkdir()
    todo_path = repo / "todo.yml"
    todo_path.write_text("inbox: []\n", encoding="utf-8")
    calls: list[list[str]] = []

    with mock.patch.object(
        td.subprocess,
        "run",
        side_effect=_staged_commit_fake_run(calls, fail_step="push"),
    ):
        result = td.commit_todo_changes(todo_path, "msg")

    # Commit succeeded locally; a push failure degrades gracefully.
    assert result is True


def test_commit_todo_changes_commit_failure_returns_false(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    (repo / ".git").mkdir()
    todo_path = repo / "todo.yml"
    todo_path.write_text("inbox: []\n", encoding="utf-8")
    calls: list[list[str]] = []

    with mock.patch.object(
        td.subprocess,
        "run",
        side_effect=_staged_commit_fake_run(calls, fail_step="commit"),
    ):
        result = td.commit_todo_changes(todo_path, "msg")

    assert result is False


# ---------------------------------------------------------------------------
# Action executors (dry-run + live)
# ---------------------------------------------------------------------------


def test_do_merge_dry_run_no_subprocess() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_merge("o/r", 1, dry_run=True)
    mocked.assert_not_called()


def test_do_merge_invokes_gh() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_merge("o/r", 1, dry_run=False)
    mocked.assert_called_once()
    args = mocked.call_args[0][0]
    assert "--auto" in args
    assert "--squash" in args
    assert "--delete-branch" in args


def test_do_merge_falls_back_when_auto_merge_disabled() -> None:
    import subprocess

    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["gh", "pr", "merge"],
        output="",
        stderr="GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)",
    )
    with mock.patch.object(td, "run_gh") as mocked:
        mocked.side_effect = [err, None, None]
        td.do_merge("o/r", 7, dry_run=False)
    assert mocked.call_count == 3
    auto_call = mocked.call_args_list[0][0][0]
    approve_call = mocked.call_args_list[1][0][0]
    plain_merge_call = mocked.call_args_list[2][0][0]
    assert "--auto" in auto_call
    assert "review" in approve_call and "--approve" in approve_call
    assert "merge" in plain_merge_call and "--auto" not in plain_merge_call
    assert "--squash" in plain_merge_call and "--delete-branch" in plain_merge_call


def test_do_merge_propagates_other_errors() -> None:
    import subprocess

    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["gh", "pr", "merge"],
        output="",
        stderr="GraphQL: Pull request is not mergeable (mergeable)",
    )
    with mock.patch.object(td, "run_gh") as mocked:
        mocked.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            td.do_merge("o/r", 9, dry_run=False)
    mocked.assert_called_once()


def test_do_approve_dry_run() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_approve("o/r", 1, dry_run=True)
    mocked.assert_not_called()


def test_do_approve_invokes_gh() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_approve("o/r", 1, dry_run=False)
    mocked.assert_called_once()
    args = mocked.call_args[0][0]
    assert args[:2] == ["pr", "review"]
    assert "--approve" in args
    assert "--repo" in args


def test_do_rebase_comment_dry_run() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_rebase_comment("o/r", 1, dry_run=True)
    mocked.assert_not_called()


def test_do_rebase_comment_invokes_gh() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_rebase_comment("o/r", 1, dry_run=False)
    mocked.assert_called_once()
    args = mocked.call_args[0][0]
    assert "@dependabot rebase" in args


def test_do_add_label_dry_run() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_add_label("o/r", 1, "release", dry_run=True)
    mocked.assert_not_called()


def test_do_add_label_invokes_gh() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_add_label("o/r", 1, "release", dry_run=False)
    mocked.assert_called_once()


def test_mark_thread_done_dry_run() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.mark_thread_done("t1", dry_run=True)
    mocked.assert_not_called()


def test_mark_thread_done_uses_delete() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.mark_thread_done("t1", dry_run=False)
    args = mocked.call_args[0][0]
    assert "DELETE" in args
    assert "/notifications/threads/t1" in args


# ---------------------------------------------------------------------------
# gh wrappers
# ---------------------------------------------------------------------------


def test_fetch_notifications_parses_paginated_pages() -> None:
    pages = [[{"id": "1"}], [{"id": "2"}]]
    with mock.patch.object(td, "run_gh", return_value=json.dumps(pages)) as mock_run:
        assert td.fetch_notifications() == [{"id": "1"}, {"id": "2"}]
    args = mock_run.call_args[0][0]
    assert "/notifications?all=true" in args, (
        f"expected ?all=true so read-but-not-done threads stay visible; got {args!r}"
    )


def test_fetch_notifications_empty_when_no_output() -> None:
    with mock.patch.object(td, "run_gh", return_value=""):
        assert td.fetch_notifications() == []


def test_fetch_notifications_handles_bad_json() -> None:
    with mock.patch.object(td, "run_gh", return_value="not json"):
        assert td.fetch_notifications() == []


def test_fetch_pr_returns_parsed_json() -> None:
    with mock.patch.object(td, "run_gh", return_value='{"number": 1}'):
        assert td.fetch_pr("o/r", 1) == {"number": 1}


def test_fetch_pr_returns_none_on_error() -> None:
    with mock.patch.object(
        td, "run_gh", side_effect=td.subprocess.CalledProcessError(1, "gh")
    ):
        assert td.fetch_pr("o/r", 1) is None


def test_fetch_pr_returns_none_on_bad_json() -> None:
    with mock.patch.object(td, "run_gh", return_value="not json"):
        assert td.fetch_pr("o/r", 1) is None


def test_get_my_login_returns_login_field() -> None:
    with mock.patch.object(td, "run_gh", return_value='{"login": "zkoppert"}'):
        assert td.get_my_login() == "zkoppert"


def test_get_my_login_raises_on_missing_login() -> None:
    with mock.patch.object(td, "run_gh", return_value="{}"):
        with pytest.raises(LookupError):
            td.get_my_login()


def test_get_my_login_raises_on_non_dict_payload() -> None:
    with mock.patch.object(td, "run_gh", return_value="[]"):
        with pytest.raises(LookupError):
            td.get_my_login()


def test_fetch_repo_labels_returns_names() -> None:
    # --slurp wraps each page in an outer array.
    out = json.dumps([[{"name": "bug"}, {"name": "release"}, {"name": ""}]])
    with mock.patch.object(td, "run_gh", return_value=out):
        assert td.fetch_repo_labels("o/r") == {"bug", "release"}


def test_fetch_repo_labels_flattens_multiple_pages() -> None:
    # Repos with more than one page of labels (>30) need both pages flattened.
    out = json.dumps(
        [
            [{"name": "bug"}, {"name": "release"}],
            [{"name": "security"}, {"name": "dependencies"}],
        ]
    )
    with mock.patch.object(td, "run_gh", return_value=out):
        assert td.fetch_repo_labels("o/r") == {
            "bug",
            "release",
            "security",
            "dependencies",
        }


def test_fetch_repo_labels_returns_empty_on_error() -> None:
    with mock.patch.object(
        td, "run_gh", side_effect=td.subprocess.CalledProcessError(1, "gh")
    ):
        assert td.fetch_repo_labels("o/r") == set()


def test_fetch_repo_labels_returns_empty_on_bad_json() -> None:
    with mock.patch.object(td, "run_gh", return_value="not json"):
        assert td.fetch_repo_labels("o/r") == set()


# ---------------------------------------------------------------------------
# parse_args + run() end-to-end
# ---------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = td.parse_args([])
    assert args.todo_file == td.DEFAULT_TODO_FILE
    assert args.state_file == td.DEFAULT_STATE_FILE
    assert not args.dry_run
    assert not args.no_copilot_subagent
    assert args.allowed_repo == []
    assert not args.no_notify
    assert not args.verbose


def test_parse_args_passes_through() -> None:
    args = td.parse_args(
        [
            "--dry-run",
            "--allowed-repo",
            "o/r",
            "--allowed-repo",
            "o/r2",
            "--no-copilot-subagent",
            "--no-notify",
            "--verbose",
        ]
    )
    assert args.dry_run
    assert args.no_copilot_subagent
    assert args.allowed_repo == ["o/r", "o/r2"]
    assert args.no_notify
    assert args.verbose


def _make_args(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    todo_file = tmp_path / "todo.yml"
    todo_file.write_text(
        "inbox: []\nprioritized:\n  q1_do_first: []\ndone: []\n",
        encoding="utf-8",
    )
    defaults = dict(
        todo_file=todo_file,
        state_file=tmp_path / "state.json",
        dry_run=False,
        no_copilot_subagent=True,
        allowed_repo=[],
        no_notify=True,
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_run_end_to_end_merges_and_flags(tmp_path: Path) -> None:
    """Drive run() through one MERGE and one FLAG outcome."""
    notif_merge = {
        "id": "thread-merge",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r1/pulls/1",
        },
    }
    notif_flag = {
        "id": "thread-flag",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r2/pulls/2",
        },
    }

    pr_merge = _base_pr(number=1, url="https://github.com/o/r1/pull/1")
    pr_flag = _base_pr(
        number=2,
        url="https://github.com/o/r2/pull/2",
        title="Bump foo from 1.0.0 to 2.0.0",
    )

    def fake_fetch_pr(repo: str, number: int) -> dict[str, Any]:
        return pr_merge if number == 1 else pr_flag

    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif_merge, notif_flag]
    ), mock.patch.object(
        td, "fetch_pr", side_effect=fake_fetch_pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=50
    ), mock.patch.object(
        td, "do_merge"
    ) as do_merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_done_mock:
        stats = td.run(args)

    assert stats.fetched == 2
    assert stats.dependabot == 2
    assert stats.merged == 1
    assert stats.flagged == 1
    do_merge_mock.assert_called_once_with(
        "o/r1", 1, dry_run=False, my_login="zkoppert", head_sha=None
    )
    mark_done_mock.assert_called_once_with("thread-merge", dry_run=False)

    reloaded = td.load_todo(args.todo_file)
    flags = reloaded["prioritized"]["q1_do_first"]
    assert len(flags) == 1
    assert flags[0]["notification"]["thread_id"] == "thread-flag"

    state = td.load_state(args.state_file)
    assert "https://github.com/o/r1/pull/1" in state
    assert "https://github.com/o/r2/pull/2" in state


def test_run_cleans_stale_inbox_entries_on_merge(tmp_path: Path) -> None:
    """A pre-existing notif-* entry should be removed after the PR auto-merges."""
    notif = {
        "id": "thread-merge",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r1/pulls/1",
        },
    }
    pr = _base_pr(number=1, url="https://github.com/o/r1/pull/1")

    args = _make_args(tmp_path)
    args.todo_file.write_text(
        "inbox:\n"
        "  - id: notif-old-entry\n"
        "    title: review dependabot PR\n"
        "    notification:\n"
        "      thread_id: thread-merge\n"
        "      url: https://github.com/o/r1/pull/1\n"
        "      reason: subscribed\n"
        "prioritized:\n"
        "  q1_do_first: []\n"
        "done: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=50
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.merged == 1
    assert stats.stale_removed == 1
    reloaded = td.load_todo(args.todo_file)
    assert reloaded["inbox"] == []


def test_run_dry_run_previews_stale_cleanup_without_mutating(tmp_path: Path) -> None:
    """Dry-run should count stale entries it would remove but not write the file."""
    notif = {
        "id": "thread-merge",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r1/pulls/1",
        },
    }
    pr = _base_pr(number=1, url="https://github.com/o/r1/pull/1")

    args = _make_args(tmp_path, dry_run=True)
    args.todo_file.write_text(
        "inbox:\n"
        "  - id: notif-old-entry\n"
        "    notification:\n"
        "      thread_id: thread-merge\n"
        "prioritized:\n"
        "  q1_do_first: []\n"
        "done: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=50
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.stale_removed == 1
    reloaded = td.load_todo(args.todo_file)
    # Dry-run must NOT mutate the file on disk.
    assert len(reloaded["inbox"]) == 1
    assert reloaded["inbox"][0]["id"] == "notif-old-entry"


def test_run_cleans_stale_inbox_entries_on_label_and_merge(tmp_path: Path) -> None:
    """A pre-existing notif-* entry should be removed after a label+merge auto-action."""
    notif = {
        "id": "thread-lam",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/3",
        },
    }
    pr = _base_pr(
        number=3,
        title="Bump foo from 1.0.0 to 1.0.2",
        body="Fixes CVE-2026-0001",
        url="https://github.com/o/r/pull/3",
    )

    args = _make_args(tmp_path)
    args.todo_file.write_text(
        "inbox: []\n"
        "prioritized:\n"
        "  q1_do_first:\n"
        "    - id: notif-q1-stale\n"
        "      title: review dependabot security PR\n"
        "      notification:\n"
        "        thread_id: thread-lam\n"
        "        url: https://github.com/o/r/pull/3\n"
        "done: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=95
    ), mock.patch.object(
        td, "fetch_repo_labels", return_value={"release"}
    ), mock.patch.object(
        td, "do_add_label"
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.labeled_and_merged == 1
    assert stats.stale_removed == 1
    reloaded = td.load_todo(args.todo_file)
    assert reloaded["prioritized"]["q1_do_first"] == []


def test_run_cleans_stale_inbox_entries_on_terminal_skip(tmp_path: Path) -> None:
    """Closed-PR notifications should also sweep their pre-existing notif-* entries."""
    notif = {
        "id": "thread-closed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/9",
        },
    }
    pr = _base_pr(number=9, url="https://github.com/o/r/pull/9", state="closed")

    args = _make_args(tmp_path)
    args.todo_file.write_text(
        "inbox: []\n"
        "prioritized:\n"
        "  q1_do_first: []\n"
        "done:\n"
        "  - id: notif-done-stale\n"
        "    notification:\n"
        "      thread_id: thread-closed\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.skipped == 1
    assert stats.stale_removed == 1
    reloaded = td.load_todo(args.todo_file)
    assert reloaded["done"] == []


def test_run_skips_already_tracked_thread(tmp_path: Path) -> None:
    notif = {
        "id": "thread-flag",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/9",
        },
    }
    pr = _base_pr(number=9, title="Bump foo from 1 to 2")

    args = _make_args(tmp_path)
    args.todo_file.write_text(
        "inbox: []\n"
        "prioritized:\n"
        "  q1_do_first:\n"
        "    - id: existing\n"
        "      notification:\n"
        "        thread_id: thread-flag\n"
        "done: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=None
    ):
        stats = td.run(args)

    assert stats.already_tracked == 1
    assert stats.flagged == 0


def test_run_dry_run_dedupes_flag_against_already_tracked(tmp_path: Path) -> None:
    # A dry run must mirror the real dedup: a flag for a PR already tracked
    # in todo.yml counts as already_tracked, not flagged, so the preview
    # digest stays an accurate harness.
    notif = {
        "id": "thread-flag",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/9",
        },
    }
    pr = _base_pr(number=9, title="Bump foo from 1 to 2")

    args = _make_args(tmp_path, dry_run=True)
    args.todo_file.write_text(
        "inbox: []\n"
        "prioritized:\n"
        "  q1_do_first:\n"
        "    - id: existing\n"
        "      notification:\n"
        "        thread_id: thread-flag\n"
        "done: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=None
    ):
        stats = td.run(args)

    assert stats.already_tracked == 1
    assert stats.flagged == 0


def test_run_respects_cooldown(tmp_path: Path) -> None:
    notif = {
        "id": "t",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/1",
        },
    }
    pr = _base_pr(url="https://github.com/o/r/pull/1")

    args = _make_args(tmp_path)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    args.state_file.write_text(json.dumps({"https://github.com/o/r/pull/1": now}))

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "do_merge"
    ) as do_merge_mock:
        stats = td.run(args)

    assert stats.cooldown == 1
    do_merge_mock.assert_not_called()


def test_run_filters_by_allowed_repo(tmp_path: Path) -> None:
    notif_in = {
        "id": "in",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/keep/me/pulls/1",
        },
    }
    notif_out = {
        "id": "out",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/skip/me/pulls/1",
        },
    }
    args = _make_args(tmp_path, allowed_repo=["keep/me"])

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif_in, notif_out]
    ), mock.patch.object(
        td, "fetch_pr", return_value=_base_pr()
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=99
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.dependabot == 1


def test_run_handles_label_and_merge_with_release_label(tmp_path: Path) -> None:
    notif = {
        "id": "sec",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/3",
        },
    }
    pr = _base_pr(
        number=3,
        title="Bump foo from 1.0.0 to 1.0.2",
        body="Fixes CVE-2026-0001",
        url="https://github.com/o/r/pull/3",
    )

    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=95
    ), mock.patch.object(
        td, "fetch_repo_labels", return_value={"release", "bug"}
    ), mock.patch.object(
        td, "do_add_label"
    ) as add_label_mock, mock.patch.object(
        td, "do_merge"
    ) as do_merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.labeled_and_merged == 1
    add_label_mock.assert_called_once_with("o/r", 3, "release", dry_run=False)
    do_merge_mock.assert_called_once()


def test_run_label_and_merge_skips_label_when_repo_lacks_it(tmp_path: Path) -> None:
    notif = {
        "id": "sec",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/3",
        },
    }
    pr = _base_pr(
        number=3, body="Fixes CVE-2026-0001", url="https://github.com/o/r/pull/3"
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=95
    ), mock.patch.object(
        td, "fetch_repo_labels", return_value={"bug"}
    ), mock.patch.object(
        td, "do_add_label"
    ) as add_label_mock, mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.labeled_and_merged == 1
    add_label_mock.assert_not_called()


def test_run_skips_non_pr_notifications(tmp_path: Path) -> None:
    notif = {"id": "n", "subject": {"type": "Issue", "url": "x"}}
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(td, "fetch_notifications", return_value=[notif]):
        stats = td.run(args)

    assert stats.dependabot == 0


def test_run_skips_non_dependabot_authors(tmp_path: Path) -> None:
    notif = {
        "id": "n",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/1",
        },
    }
    pr = _base_pr(author={"login": "iansan5653"})
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ):
        stats = td.run(args)

    assert stats.dependabot == 0


def test_run_skips_super_linter_pr_with_mention_keeps_notification(
    tmp_path: Path,
) -> None:
    """Super-linter Dependabot PRs are excluded from action. If the reason is
    ``mention`` (or any non-auto-clear reason) the notification stays in the
    inbox so the user can respond directly."""
    notif = {
        "id": "thread-super-linter",
        "reason": "mention",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    pr = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "fetch_repo_labels"
    ) as fetch_labels_mock, mock.patch.object(
        td, "do_merge"
    ) as merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)

    assert stats.skipped_dependency == 1
    assert stats.dependabot == 0
    assert stats.merged == 0
    assert stats.flagged == 0
    fetch_labels_mock.assert_not_called()
    merge_mock.assert_not_called()
    # @mention reason: leave the notification so the user can respond.
    mark_mock.assert_not_called()
    # Cooldown state must be written so the hourly cron doesn't re-fetch the
    # same long-lived super-linter PR every hour forever.
    saved = td.load_state(args.state_file)
    assert "https://github.com/o/r/pull/42" in saved


def test_run_skips_fetch_when_pr_url_already_in_cooldown(tmp_path: Path) -> None:
    """A PR already in the cooldown state file must NOT be fetched: the cooldown
    check happens before fetch_pr so we don't burn a `gh pr view` API call per
    cron run on long-lived PRs (e.g. super-linter Dependabot bumps that sit
    open for days awaiting human review)."""
    notif = {
        "id": "thread-cached",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    args = _make_args(tmp_path)
    # Pre-populate cooldown state for the same URL the run loop will derive
    # from the notification subject.
    args.state_file.write_text(
        '{"https://github.com/o/r/pull/42": 9999999999.0}',
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr"
    ) as fetch_pr_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)

    fetch_pr_mock.assert_not_called()
    mark_mock.assert_not_called()
    assert stats.cooldown == 1
    assert stats.dependabot == 0


def test_run_skips_closed_super_linter_pr_clears_notification(tmp_path: Path) -> None:
    """Closed/merged super-linter PRs are terminal: clear the notification so it
    doesn't reappear next hour, but still don't merge or label."""
    notif = {
        "id": "thread-super-linter-closed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/43",
        },
    }
    pr = _base_pr(
        number=43,
        url="https://github.com/o/r/pull/43",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
        state="closed",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "do_merge"
    ) as merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)

    assert stats.skipped_dependency == 1
    assert stats.dependabot == 0
    merge_mock.assert_not_called()
    mark_mock.assert_called_once_with("thread-super-linter-closed", dry_run=False)


def test_run_marks_thread_done_for_closed_pr(tmp_path: Path) -> None:
    """Closed PR notifications must be cleared so they don't reappear next run."""
    notif = {
        "id": "thread-closed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/9",
        },
    }
    pr = _base_pr(number=9, url="https://github.com/o/r/pull/9", state="closed")
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_done_mock:
        stats = td.run(args)

    assert stats.skipped == 1
    mark_done_mock.assert_called_once_with("thread-closed", dry_run=False)


def test_run_does_not_mark_thread_done_for_transient_skip(tmp_path: Path) -> None:
    """CI-pending skips must NOT mark the thread done - we want to retry next hour."""
    notif = {
        "id": "thread-pending",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/10",
        },
    }
    pr = _base_pr(
        number=10,
        url="https://github.com/o/r/pull/10",
        statusCheckRollup=[{"status": "IN_PROGRESS"}],
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_done_mock:
        stats = td.run(args)

    assert stats.skipped == 1
    mark_done_mock.assert_not_called()


def test_run_records_error_when_user_fetch_fails(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    with mock.patch.object(
        td,
        "get_my_login",
        side_effect=td.subprocess.CalledProcessError(1, "gh"),
    ):
        stats = td.run(args)
    assert stats.errors


def test_run_records_error_when_user_fetch_times_out(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    with mock.patch.object(
        td,
        "get_my_login",
        side_effect=td.subprocess.TimeoutExpired("gh", 60),
    ):
        stats = td.run(args)
    assert stats.errors


def test_run_records_error_when_user_response_is_malformed(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    with mock.patch.object(
        td,
        "get_my_login",
        side_effect=LookupError("unexpected /user response"),
    ):
        stats = td.run(args)
    assert stats.errors


def test_run_records_error_when_notifications_fetch_fails(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td,
        "fetch_notifications",
        side_effect=td.subprocess.CalledProcessError(1, "gh"),
    ):
        stats = td.run(args)
    assert stats.errors


def test_run_records_error_when_notifications_fetch_times_out(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td,
        "fetch_notifications",
        side_effect=td.subprocess.TimeoutExpired("gh", 60),
    ):
        stats = td.run(args)
    assert stats.errors


def test_run_dry_run_does_not_mutate(tmp_path: Path) -> None:
    notif = {
        "id": "t",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/1",
        },
    }
    pr = _base_pr()
    args = _make_args(tmp_path, dry_run=True)
    original = args.todo_file.read_text(encoding="utf-8")

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=99
    ):
        stats = td.run(args)

    assert stats.merged == 1
    assert args.todo_file.read_text(encoding="utf-8") == original
    assert not args.state_file.exists()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_no_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_file = tmp_path / "todo.yml"
    todo_file.write_text("inbox: []\nprioritized:\n  q1_do_first: []\ndone: []\n")
    state_file = tmp_path / "state.json"

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(td, "fetch_notifications", return_value=[]):
        rc = td.main(
            [
                "--todo-file",
                str(todo_file),
                "--state-file",
                str(state_file),
                "--no-notify",
            ]
        )

    captured = capsys.readouterr()
    assert rc == 0
    assert "fetched=0" in captured.out


def test_main_returns_one_when_errors(tmp_path: Path) -> None:
    with mock.patch.object(
        td,
        "get_my_login",
        side_effect=td.subprocess.CalledProcessError(1, "gh"),
    ):
        rc = td.main(
            [
                "--todo-file",
                str(tmp_path / "todo.yml"),
                "--state-file",
                str(tmp_path / "state.json"),
                "--no-notify",
            ]
        )
    assert rc == 1


def test_macos_notify_swallows_errors() -> None:
    with mock.patch.object(
        td.subprocess, "run", side_effect=FileNotFoundError("osascript")
    ):
        td.macos_notify("title", "msg")  # should not raise


def _run_skipped_super_linter_with_reason(
    tmp_path: Path, reason: str
) -> tuple[td.TriageStats, mock.MagicMock]:
    """Helper: run() against a single open super-linter PR with the given
    notification reason. Returns the stats plus the mark_thread_done mock so
    callers can assert on call count."""
    notif = {
        "id": f"thread-super-linter-{reason}",
        "reason": reason,
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    pr = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)
    return stats, mark_mock


def test_run_skips_super_linter_pr_with_review_requested_clears_notification(
    tmp_path: Path,
) -> None:
    """``review_requested`` on an excluded dependency is a passive ping - clear
    the notification so the inbox stops accumulating super-linter PRs."""
    stats, mark_mock = _run_skipped_super_linter_with_reason(
        tmp_path, "review_requested"
    )
    assert stats.skipped_dependency == 1
    assert stats.dependabot == 0
    mark_mock.assert_called_once_with(
        "thread-super-linter-review_requested", dry_run=False
    )


def test_run_skips_super_linter_pr_with_subscribed_clears_notification(
    tmp_path: Path,
) -> None:
    """``subscribed`` is also a passive reason: clear excluded-dep notifications
    rather than letting them sit in the inbox."""
    stats, mark_mock = _run_skipped_super_linter_with_reason(tmp_path, "subscribed")
    assert stats.skipped_dependency == 1
    mark_mock.assert_called_once_with(
        "thread-super-linter-subscribed", dry_run=False
    )


def test_run_skips_super_linter_pr_with_ci_activity_clears_notification(
    tmp_path: Path,
) -> None:
    """CI activity on an excluded dep is noise - clear the notification."""
    stats, mark_mock = _run_skipped_super_linter_with_reason(tmp_path, "ci_activity")
    assert stats.skipped_dependency == 1
    mark_mock.assert_called_once_with(
        "thread-super-linter-ci_activity", dry_run=False
    )


def test_run_skips_super_linter_pr_with_team_mention_keeps_notification(
    tmp_path: Path,
) -> None:
    """``team_mention`` is an actionable reason: do NOT auto-clear; the user
    should respond directly."""
    stats, mark_mock = _run_skipped_super_linter_with_reason(tmp_path, "team_mention")
    assert stats.skipped_dependency == 1
    mark_mock.assert_not_called()


def test_run_excluded_dep_dry_run_propagates_dry_run_flag(tmp_path: Path) -> None:
    """Dry-run mode must still invoke mark_thread_done at the call boundary,
    but with dry_run=True so the function itself short-circuits before
    touching the GitHub API. The script's contract is "respect dry_run at
    the call site"; the no-op behavior lives inside mark_thread_done."""
    notif = {
        "id": "thread-dry-run",
        "reason": "review_requested",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    pr = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
    )
    args = _make_args(tmp_path)
    args.dry_run = True

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)

    assert stats.skipped_dependency == 1
    # mark_thread_done is still invoked, but with dry_run=True so it should be
    # a no-op inside the function itself. The script's contract is "respect
    # dry_run at the call boundary".
    mark_mock.assert_called_once_with("thread-dry-run", dry_run=True)


def test_excluded_dep_auto_clear_reasons_constant() -> None:
    """Locks in the canonical set of passive reasons. Adding a new reason here
    is a deliberate behavior change and should require updating tests."""
    assert td.EXCLUDED_DEP_AUTO_CLEAR_REASONS == frozenset(
        {"review_requested", "subscribed", "ci_activity"}
    )


def test_run_excluded_dep_clear_calls_cleanup_stale(tmp_path: Path) -> None:
    """When clearing a passive excluded-dep notification, the script must also
    scrub any matching todo entries. Otherwise a stale entry written before
    the dependency was excluded would persist forever in todo.yml."""
    notif = {
        "id": "thread-with-stale",
        "reason": "review_requested",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    pr = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
    )
    args = _make_args(tmp_path)
    args.todo_file.write_text(
        "inbox:\n"
        "  - id: stale-dependabot\n"
        "    notification:\n"
        "      thread_id: thread-with-stale\n"
        "      url: https://github.com/o/r/pull/42\n"
        "prioritized:\n"
        "  q1_do_first: []\n"
        "done: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "mark_thread_done"
    ):
        stats = td.run(args)

    assert stats.skipped_dependency == 1
    assert stats.stale_removed == 1
    reloaded = td.load_todo(args.todo_file)
    assert reloaded["inbox"] == []


def test_run_excluded_dep_mark_done_failure_does_not_abort_run(
    tmp_path: Path,
) -> None:
    """A GitHub API failure during mark_thread_done must not abort the whole
    cron. The error is appended to stats.errors, cooldown state is NOT
    written so the next run can retry, and run() continues to process other
    notifications."""
    notif1 = {
        "id": "thread-fail",
        "reason": "review_requested",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    notif2 = {
        "id": "thread-ok",
        "reason": "review_requested",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/43",
        },
    }
    pr1 = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
    )
    pr2 = _base_pr(
        number=43,
        url="https://github.com/o/r/pull/43",
        title="Bump super-linter/super-linter from 8.0.0 to 8.0.1",
    )
    args = _make_args(tmp_path)

    def side_effect(thread_id: str, dry_run: bool = False) -> None:
        if thread_id == "thread-fail":
            raise td.subprocess.CalledProcessError(1, ["gh"])

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif1, notif2]
    ), mock.patch.object(
        td, "fetch_pr", side_effect=[pr1, pr2]
    ), mock.patch.object(
        td, "mark_thread_done", side_effect=side_effect
    ):
        stats = td.run(args)

    assert stats.skipped_dependency == 2
    assert any("mark-done failed" in e for e in stats.errors)
    saved = td.load_state(args.state_file)
    assert "https://github.com/o/r/pull/42" not in saved
    assert "https://github.com/o/r/pull/43" in saved


def test_run_excluded_dep_mention_writes_cooldown_without_mark_done(
    tmp_path: Path,
) -> None:
    """A non-clearing reason (mention) still writes cooldown so the script
    doesn't fetch_pr the same notification every hour."""
    notif = {
        "id": "thread-mention",
        "reason": "mention",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    pr = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock, mock.patch.object(
        td, "_cleanup_stale_entries"
    ) as cleanup_mock:
        stats = td.run(args)

    assert stats.skipped_dependency == 1
    mark_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    saved = td.load_state(args.state_file)
    assert "https://github.com/o/r/pull/42" in saved


def test_run_closed_excluded_dep_mark_done_failure_does_not_abort_run(
    tmp_path: Path,
) -> None:
    """Mirrors the open-PR resilience test for the closed/merged excluded-dep
    branch: a GitHub API failure during mark_thread_done must not abort the
    cron, the error is recorded, and run() continues processing other
    notifications."""
    notif1 = {
        "id": "thread-closed-fail",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/42",
        },
    }
    notif2 = {
        "id": "thread-closed-ok",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/43",
        },
    }
    pr1 = _base_pr(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
        state="closed",
    )
    pr2 = _base_pr(
        number=43,
        url="https://github.com/o/r/pull/43",
        title="Bump super-linter/super-linter from 8.0.0 to 8.0.1",
        state="closed",
    )
    args = _make_args(tmp_path)

    def side_effect(thread_id: str, dry_run: bool = False) -> None:
        if thread_id == "thread-closed-fail":
            raise td.subprocess.CalledProcessError(1, ["gh"])

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif1, notif2]
    ), mock.patch.object(
        td, "fetch_pr", side_effect=[pr1, pr2]
    ), mock.patch.object(
        td, "mark_thread_done", side_effect=side_effect
    ):
        stats = td.run(args)

    assert stats.skipped_dependency == 2
    assert any(
        "mark-done failed for closed excluded-dep" in e for e in stats.errors
    )


# ---------------------------------------------------------------------------
# SKIPPED_REPO_PATTERNS - filter Dependabot PRs in super-linter repo itself
# ---------------------------------------------------------------------------


def test_skipped_repo_match_super_linter() -> None:
    assert td.skipped_repo_match("super-linter/super-linter") == "super-linter/super-linter"


def test_skipped_repo_match_case_insensitive() -> None:
    assert td.skipped_repo_match("Super-Linter/Super-Linter") is not None


def test_skipped_repo_match_private_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(td, "SKIPPED_REPOS", {"acme/private-thing"})

    assert td.skipped_repo_match("acme/private-thing") == "acme/private-thing"
    assert td.skipped_repo_match("Acme/Private-Thing") == "Acme/Private-Thing"


def test_skipped_repo_match_negative() -> None:
    assert td.skipped_repo_match("github-community-projects/stale-repos") is None
    assert td.skipped_repo_match("") is None
    # Partial / superstring matches must not fire - pattern is anchored.
    assert td.skipped_repo_match("foo/super-linter") is None
    assert td.skipped_repo_match("super-linter/super-linter-fork") is None


def _run_skipped_super_linter_repo_with_reason(
    tmp_path: Path, reason: str
) -> tuple[td.TriageStats, mock.MagicMock]:
    """Run() against a Dependabot PR *inside* super-linter/super-linter with a
    title that does NOT mention super-linter (so only the repo filter can
    catch it). Returns stats + mark_thread_done mock."""
    notif = {
        "id": f"thread-super-linter-repo-{reason}",
        "reason": reason,
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/super-linter/super-linter/pulls/9999",
        },
    }
    pr = _base_pr(
        number=9999,
        url="https://github.com/super-linter/super-linter/pull/9999",
        title="chore(deps): bump actions/checkout from 4 to 5",
    )
    args = _make_args(tmp_path)
    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)
    return stats, mark_mock


def test_run_skips_super_linter_repo_pr_with_subscribed_clears_notification(
    tmp_path: Path,
) -> None:
    """Passive subscription noise inside super-linter repo: clear the
    notification so the inbox stops accumulating. Matches the @-mention-only
    behavior of the notification-triage SUBSCRIPTION_FILTERED_REPOS entry."""
    stats, mark_mock = _run_skipped_super_linter_repo_with_reason(
        tmp_path, "subscribed"
    )
    assert stats.skipped_dependency == 1
    assert stats.dependabot == 0
    mark_mock.assert_called_once_with(
        "thread-super-linter-repo-subscribed", dry_run=False
    )


def test_run_skips_super_linter_repo_pr_with_mention_keeps_notification(
    tmp_path: Path,
) -> None:
    """@-mentions in the super-linter repo are actionable: skip the auto
    action but leave the notification so the user can respond."""
    stats, mark_mock = _run_skipped_super_linter_repo_with_reason(
        tmp_path, "mention"
    )
    assert stats.skipped_dependency == 1
    mark_mock.assert_not_called()


def test_run_skips_private_config_repo_and_clears_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passive Dependabot bumps in private skipped repos clear the notification."""
    monkeypatch.setattr(td, "SKIPPED_REPOS", {"acme/private-thing"})
    notif = {
        "id": "thread-private-repo",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/acme/private-thing/pulls/42",
        },
    }
    pr = _base_pr(
        number=42,
        url="https://github.com/acme/private-thing/pull/42",
        title="Bump actions/checkout from 4.2.2 to 4.3.0",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "fetch_repo_labels"
    ) as fetch_labels_mock, mock.patch.object(
        td, "do_merge"
    ) as merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_mock:
        stats = td.run(args)

    assert stats.skipped_dependency == 1
    assert stats.dependabot == 0
    assert stats.merged == 0
    assert stats.flagged == 0
    fetch_labels_mock.assert_not_called()
    merge_mock.assert_not_called()
    mark_mock.assert_called_once_with("thread-private-repo", dry_run=False)
    saved = td.load_state(args.state_file)
    assert "https://github.com/acme/private-thing/pull/42" in saved


# ---------------------------------------------------------------------------
# decide() defense-in-depth: repo/dep skip takes priority over bump severity
# ---------------------------------------------------------------------------


def test_decide_skips_super_linter_repo_major_bump_with_passive_reason() -> None:
    """A major bump PR in super-linter/super-linter with a passive notification
    reason must be skipped inside decide() - not flagged for review. This is the
    defense-in-depth fix for the filter priority bug where bump severity ran
    before the repo-level skip."""
    pr = _base_pr(
        title="Bump actions/checkout from 4 to 5",
        statusCheckRollup=[{"conclusion": "SUCCESS"}],
    )
    decision = td.decide(
        pr,
        my_login="zkoppert",
        repo="super-linter/super-linter",
        coverage_lookup=lambda _r: None,
        use_copilot=False,
        notif_reason="review_requested",
    )
    assert decision.outcome == td.OUTCOME_SKIP
    assert "excluded repo/dependency" in decision.reason


def test_decide_does_not_skip_super_linter_repo_when_mentioned() -> None:
    """If the notification reason is a direct @mention, the repo-level skip
    should NOT suppress the decision - the user needs to act."""
    pr = _base_pr(
        title="Bump actions/checkout from 4 to 5",
        statusCheckRollup=[{"conclusion": "SUCCESS"}],
    )
    decision = td.decide(
        pr,
        my_login="zkoppert",
        repo="super-linter/super-linter",
        coverage_lookup=lambda _r: 95,
        use_copilot=False,
        notif_reason="mention",
    )
    # Should proceed through normal logic (not skipped)
    assert decision.outcome != td.OUTCOME_SKIP


def test_decide_skips_super_linter_dependency_major_bump() -> None:
    """A PR bumping super-linter/super-linter as a dependency (title match)
    with a passive reason must be skipped inside decide() even though the
    bump is major."""
    pr = _base_pr(
        title="Bump super-linter/super-linter from 7.0.0 to 8.0.0",
        statusCheckRollup=[{"conclusion": "SUCCESS"}],
    )
    decision = td.decide(
        pr,
        my_login="zkoppert",
        repo="o/r",
        coverage_lookup=lambda _r: None,
        use_copilot=False,
        notif_reason="review_requested",
    )
    assert decision.outcome == td.OUTCOME_SKIP
    assert "excluded repo/dependency" in decision.reason


# ---------------------------------------------------------------------------
# Archived repo handling (bug 2)
# ---------------------------------------------------------------------------


@pytest.mark.no_archive_stub
def test_is_archived_repo_returns_true_when_gh_returns_true() -> None:
    td._ARCHIVED_REPO_CACHE.clear()
    with mock.patch.object(td, "run_gh", return_value="true\n"):
        assert td.is_archived_repo("zkoppert/advanced-security-enforcer") is True


@pytest.mark.no_archive_stub
def test_is_archived_repo_returns_false_when_gh_returns_false() -> None:
    td._ARCHIVED_REPO_CACHE.clear()
    with mock.patch.object(td, "run_gh", return_value="false\n"):
        assert td.is_archived_repo("github/markup") is False


@pytest.mark.no_archive_stub
def test_is_archived_repo_caches_per_process() -> None:
    td._ARCHIVED_REPO_CACHE.clear()
    with mock.patch.object(td, "run_gh", return_value="true\n") as gh_mock:
        td.is_archived_repo("o/r")
        td.is_archived_repo("o/r")
        td.is_archived_repo("o/r")
    assert gh_mock.call_count == 1


@pytest.mark.no_archive_stub
def test_is_archived_repo_falls_back_to_false_on_api_error() -> None:
    td._ARCHIVED_REPO_CACHE.clear()
    with mock.patch.object(
        td, "run_gh", side_effect=td.subprocess.CalledProcessError(1, "gh")
    ):
        assert td.is_archived_repo("o/r") is False


def test_run_skips_archived_repo_and_clears_notification(tmp_path: Path) -> None:
    """Archived repos can never accept merges - skip + clear, never flag."""
    notif = {
        "id": "thread-archived",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": (
                "https://api.github.com/repos/zkoppert/"
                "advanced-security-enforcer/pulls/73"
            ),
        },
    }
    pr = _base_pr(
        number=73,
        url="https://github.com/zkoppert/advanced-security-enforcer/pull/73",
        title="Bump foo from 1.0.0 to 1.1.0",
    )

    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "is_archived_repo", return_value=True
    ) as archive_mock, mock.patch.object(
        td, "do_merge"
    ) as merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_done_mock:
        stats = td.run(args)

    archive_mock.assert_called_with("zkoppert/advanced-security-enforcer")
    merge_mock.assert_not_called()
    mark_done_mock.assert_called_once_with("thread-archived", dry_run=False)
    assert stats.skipped_archived == 1
    assert stats.flagged == 0
    assert stats.dependabot == 0  # archived skip happens before dependabot count

    reloaded = td.load_todo(args.todo_file)
    assert reloaded["prioritized"]["q1_do_first"] == []

    state = td.load_state(args.state_file)
    assert "https://github.com/zkoppert/advanced-security-enforcer/pull/73" in state


# ---------------------------------------------------------------------------
# Branch-protection / consistent approval (bug 3)
# ---------------------------------------------------------------------------


def test_is_branch_protection_error_matches_known_markers() -> None:
    assert td._is_branch_protection_error(
        "the base branch policy prohibits the merge"
    )
    # Case-insensitive.
    assert td._is_branch_protection_error(
        "The Base Branch Policy Prohibits The Merge"
    )
    assert td._is_branch_protection_error(
        "GraphQL: At least 1 approving review is required by reviewers"
    )
    assert td._is_branch_protection_error("Required status check missing")
    assert td._is_branch_protection_error("Changes requested by reviewer")
    assert td._is_branch_protection_error(
        "Review is required by reviewers with write access"
    )


def test_is_branch_protection_error_negative() -> None:
    assert not td._is_branch_protection_error("Auto merge is not allowed")
    assert not td._is_branch_protection_error("merge conflict")
    assert not td._is_branch_protection_error("")


def test_has_existing_approval_true_when_login_matches_head() -> None:
    # Production-shape payload: ``gh pr view --json reviews,latestReviews``
    # returns ``commit`` as a nested ``{"oid": "<sha>"}`` object rather than
    # a flat ``commit_id`` field. Regression for PR #37 review feedback.
    payload = {
        "headRefOid": "abc123",
        "reviews": [
            {
                "state": "APPROVED",
                "author": {"login": "zkoppert"},
                "commit": {"oid": "abc123"},
            }
        ],
        "latestReviews": [],
    }
    with mock.patch.object(td, "run_gh", return_value=json.dumps(payload)):
        assert td.has_existing_approval("o/r", 1, "zkoppert", "abc123") is True


def test_has_existing_approval_false_when_head_changed() -> None:
    payload = {
        "headRefOid": "newsha",
        "reviews": [
            {
                "state": "APPROVED",
                "author": {"login": "zkoppert"},
                "commit": {"oid": "oldsha"},
            }
        ],
        "latestReviews": [],
    }
    with mock.patch.object(td, "run_gh", return_value=json.dumps(payload)):
        assert td.has_existing_approval("o/r", 1, "zkoppert", "newsha") is False


def test_has_existing_approval_false_when_other_user_approved() -> None:
    payload = {
        "headRefOid": "abc123",
        "reviews": [
            {
                "state": "APPROVED",
                "author": {"login": "other"},
                "commit": {"oid": "abc123"},
            }
        ],
        "latestReviews": [],
    }
    with mock.patch.object(td, "run_gh", return_value=json.dumps(payload)):
        assert td.has_existing_approval("o/r", 1, "zkoppert", "abc123") is False


def test_has_existing_approval_accepts_flat_commit_id_fallback() -> None:
    # Some older or alternate gh versions may emit a flat ``commit_id`` field
    # instead of the nested ``commit.oid`` object. Keep the fallback covered
    # so we don't regress resilience while fixing the primary shape.
    payload = {
        "headRefOid": "abc123",
        "reviews": [
            {
                "state": "APPROVED",
                "author": {"login": "zkoppert"},
                "commit_id": "abc123",
            }
        ],
        "latestReviews": [],
    }
    with mock.patch.object(td, "run_gh", return_value=json.dumps(payload)):
        assert td.has_existing_approval("o/r", 1, "zkoppert", "abc123") is True


def test_has_existing_approval_uses_latest_reviews_with_nested_commit() -> None:
    # ``latestReviews`` uses the same nested ``commit.oid`` shape as ``reviews``.
    payload = {
        "headRefOid": "abc123",
        "reviews": [],
        "latestReviews": [
            {
                "state": "APPROVED",
                "author": {"login": "zkoppert"},
                "commit": {"oid": "abc123"},
            }
        ],
    }
    with mock.patch.object(td, "run_gh", return_value=json.dumps(payload)):
        assert td.has_existing_approval("o/r", 1, "zkoppert", "abc123") is True


def test_has_existing_approval_false_on_gh_error() -> None:
    with mock.patch.object(
        td, "run_gh", side_effect=td.subprocess.CalledProcessError(1, "gh")
    ):
        assert td.has_existing_approval("o/r", 1, "zkoppert", "abc123") is False


def test_do_merge_skips_approve_when_already_approved(tmp_path: Path) -> None:
    """If my login already approved this head SHA, do not re-approve."""
    auto_merge_err = td.subprocess.CalledProcessError(1, "gh")
    auto_merge_err.stderr = "Auto merge is not allowed for this repository"

    call_log: list[list[str]] = []

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        call_log.append(list(args))
        if "--auto" in args:
            raise auto_merge_err
        return ""

    with mock.patch.object(
        td, "has_existing_approval", return_value=True
    ) as approval_check, mock.patch.object(
        td, "do_approve"
    ) as approve_mock, mock.patch.object(
        td, "run_gh", side_effect=fake_run_gh
    ):
        td.do_merge(
            "o/r", 1, dry_run=False, my_login="zkoppert", head_sha="abc123"
        )

    approval_check.assert_called_once_with("o/r", 1, "zkoppert", "abc123")
    approve_mock.assert_not_called()
    # First call was --auto attempt; second was the sync merge.
    assert any("--auto" in a for a in call_log)
    assert any(
        "--auto" not in a and "merge" in a and "--squash" in a for a in call_log
    )


def test_do_merge_raises_branch_protection_blocked_on_sync_merge_failure(
    tmp_path: Path,
) -> None:
    auto_merge_err = td.subprocess.CalledProcessError(1, "gh")
    auto_merge_err.stderr = "Auto merge is not allowed for this repository"

    bp_err = td.subprocess.CalledProcessError(1, "gh")
    bp_err.stderr = "the base branch policy prohibits the merge"

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        if "--auto" in args:
            raise auto_merge_err
        raise bp_err

    with mock.patch.object(
        td, "has_existing_approval", return_value=True
    ), mock.patch.object(
        td, "do_approve"
    ), mock.patch.object(
        td, "run_gh", side_effect=fake_run_gh
    ):
        with pytest.raises(td.BranchProtectionBlocked) as exc_info:
            td.do_merge(
                "jmeridth/gh-health-files",
                59,
                dry_run=False,
                my_login="zkoppert",
                head_sha="abc",
            )

    assert "the base branch policy prohibits the merge" in exc_info.value.marker


def test_run_branch_protection_failure_flags_and_sets_long_cooldown(
    tmp_path: Path,
) -> None:
    """Regression test for the 52-retry approve loop on gh-health-files#59."""
    notif = {
        "id": "thread-bp",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/jmeridth/gh-health-files/pulls/59",
        },
    }
    pr = _base_pr(
        number=59,
        url="https://github.com/jmeridth/gh-health-files/pull/59",
    )
    pr["headRefOid"] = "abc123"

    args = _make_args(tmp_path)

    bp_err = td.BranchProtectionBlocked(
        repo="jmeridth/gh-health-files",
        number=59,
        marker="the base branch policy prohibits the merge",
    )

    approve_mock = mock.MagicMock()

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=95
    ), mock.patch.object(
        td, "do_merge", side_effect=bp_err
    ), mock.patch.object(
        td, "do_approve", approve_mock
    ), mock.patch.object(
        td, "mark_thread_done"
    ) as mark_done_mock:
        stats = td.run(args)

    # Flagged for review (not retried, no errors).
    assert stats.merged == 0
    assert stats.flagged == 1
    assert stats.errors == []
    # No additional approve happened in the run loop; do_merge raised
    # before its internal approve fallback could re-fire.
    approve_mock.assert_not_called()
    # Notification cleared so it does not re-enter next hour.
    mark_done_mock.assert_called_once_with("thread-bp", dry_run=False)

    # Q1 entry has the branch-protection reason in its description.
    reloaded = td.load_todo(args.todo_file)
    flags = reloaded["prioritized"]["q1_do_first"]
    assert len(flags) == 1
    assert "branch protection" in flags[0]["description"]

    # Long cooldown set: a follow-up run within the next 24h must skip.
    state = td.load_state(args.state_file)
    pr_url = "https://github.com/jmeridth/gh-health-files/pull/59"
    assert pr_url in state
    # Within the 24h window in_cooldown should still return True.
    now_plus_23h = datetime.datetime.now(datetime.timezone.utc).timestamp() + (
        23 * 3600
    )
    assert td.in_cooldown(state, pr_url, now=now_plus_23h)


# ---------------------------------------------------------------------------
# Stale-removal guard (bug 4)
# ---------------------------------------------------------------------------


def test_remove_stale_entries_never_removes_items_without_notification(
    tmp_path: Path, caplog: Any
) -> None:
    """Regression: hand-curated Q1 entries (no ``notification`` field) must
    survive a stale-removal pass even when another item in the same bucket
    is matched and removed.
    """
    todo_file = tmp_path / "todo.yml"
    todo_file.write_text(
        "inbox: []\n"
        "prioritized:\n"
        "  q1_do_first:\n"
        "    - id: hand-curated-no-notification\n"
        "      title: 'IssueQuery#maybe_expand_author_for_agents fix'\n"
        "      status: pending\n"
        "    - id: dependabot-foo-pr-9\n"
        "      title: Review dependabot PR\n"
        "      notification:\n"
        "        thread_id: thread-resolved\n"
        "        url: https://github.com/o/r/pull/9\n"
        "        reason: subscribed\n"
        "    - id: hand-curated-null-notif\n"
        "      title: notification field present but null\n"
        "      notification: null\n"
        "    - id: hand-curated-empty-notif\n"
        "      title: notification field present but empty\n"
        "      notification: {}\n"
        "done: []\n",
        encoding="utf-8",
    )
    data = td.load_todo(todo_file)

    with caplog.at_level("INFO", logger="triage-dependabot"):
        removed = td.remove_stale_entries(
            data,
            thread_id="thread-resolved",
            pr_url="https://github.com/o/r/pull/9",
        )

    assert removed == 1

    ids_left = [item["id"] for item in data["prioritized"]["q1_do_first"]]
    assert ids_left == [
        "hand-curated-no-notification",
        "hand-curated-null-notif",
        "hand-curated-empty-notif",
    ]

    # Diagnostic logging captured the removal with the matched key + item id.
    matching_records = [
        rec.getMessage() for rec in caplog.records if "stale-removal" in rec.getMessage()
    ]
    assert any("dependabot-foo-pr-9" in msg for msg in matching_records)
    assert any("thread_id=thread-resolved" in msg for msg in matching_records)


def test_remove_stale_entries_no_op_when_both_keys_missing() -> None:
    """An empty resolver argument list must never remove anything."""
    data = {
        "inbox": [
            {
                "id": "anything",
                "notification": {
                    "thread_id": "t1",
                    "url": "https://github.com/o/r/pull/1",
                },
            },
        ],
        "prioritized": {"q1_do_first": []},
        "done": [],
    }
    assert td.remove_stale_entries(data) == 0
    assert td.remove_stale_entries(data, thread_id=None, pr_url=None) == 0
    assert td.remove_stale_entries(data, thread_id="", pr_url="") == 0
    assert data["inbox"][0]["id"] == "anything"


def test_load_and_write_todo_roundtrips_ruby_method_and_backticks(
    tmp_path: Path,
) -> None:
    """ruamel round-trip must preserve ``IssueQuery#maybe_expand_*`` text.

    The hand-curated Q1 item lost in the 2026-06-11 triage session
    contained a single-quoted title with ``IssueQuery#maybe_expand_*``
    and a description with backticks. Verify load_todo + write_todo_atomic
    preserves the content exactly so any future loss is not a YAML
    round-trip bug.
    """
    todo_file = tmp_path / "todo.yml"
    todo_file.write_text(
        "inbox: []\n"
        "prioritized:\n"
        "  q1_do_first:\n"
        "    - id: core-ux-2746-author-me-copilot-coauthored-prs\n"
        "      title: 'core-ux#2746: bare `author:@me` drops Copilot-coauthored PRs'\n"
        "      description: 'IssueQuery#maybe_expand_author_for_agents was gated"
        " by `pull_request_scoped_search?`'\n"
        "      status: pending\n"
        "done: []\n",
        encoding="utf-8",
    )

    data = td.load_todo(todo_file)
    flags = data["prioritized"]["q1_do_first"]
    assert len(flags) == 1
    assert flags[0]["id"] == "core-ux-2746-author-me-copilot-coauthored-prs"
    assert "#maybe_expand_author_for_agents" in flags[0]["description"]
    assert "pull_request_scoped_search?" in flags[0]["description"]
    assert "`author:@me`" in flags[0]["title"]

    out_path = tmp_path / "todo-out.yml"
    td.write_todo_atomic(out_path, data)
    reloaded = td.load_todo(out_path)
    flags2 = reloaded["prioritized"]["q1_do_first"]
    assert len(flags2) == 1
    assert flags2[0]["id"] == flags[0]["id"]
    assert flags2[0]["title"] == flags[0]["title"]
    assert flags2[0]["description"] == flags[0]["description"]


# ---------------------------------------------------------------------------
# Bug 5 / FR: prerelease (alpha/beta/rc/dev/preview) bumps -> force-close via gh pr close
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        # Real github-community-projects/stale-repos#520 title.
        "chore(deps): bump python from 3.14.5-slim to 3.15.0b2-slim in the dependencies group",
        # PEP 440 short forms glued to the patch digit.
        "Bump foo from 1.0.0 to 2.0.0a1",
        "Bump foo from 1.0.0 to 2.0.0b3",
        "Bump foo from 1.0.0 to 2.0.0rc1",
        # Semver / npm word forms with `-` or `.` separators.
        "Bump foo from 1.0.0 to 2.0.0-alpha",
        "Bump foo from 1.0.0 to 2.0.0-beta.1",
        "Bump foo from 1.0.0 to 2.0.0-rc1",
        "Bump foo from 1.0.0 to 2.0.0-preview",
        "Bump foo from 1.0.0 to 2.0.0-pre",
        # PEP 440 dev release.
        "Bump foo from 1.0.0 to 2.0.0.dev1",
    ],
)
def test_is_prerelease_target_positive(title: str) -> None:
    assert td.is_prerelease_target({"title": title, "body": ""})


@pytest.mark.parametrize(
    "title",
    [
        # Stable docker tags with build variants must not match.
        "chore(deps): bump python from 3.14.5-slim to 3.15.0-slim",
        "Bump nginx from 1.20.0-alpine to 1.21.0-alpine",
        "Bump base from 1.0-bookworm to 1.1-bookworm",
        # PEP 440 post-release: a bug-fix tag, fine to auto-merge.
        "Bump foo from 1.0.0 to 1.0.0.post1",
        # Plain semver bumps.
        "Bump foo from 1.0.0 to 2.0.0",
        "Bump foo from 1.2.3 to 1.2.4",
        # Words that contain prerelease substrings must not match (e.g.
        # `alpine` ≠ `alpha`, `predictor` ≠ `pre`).
        "Bump foo from 1.0.0 to 1.0.0-alpine",
    ],
)
def test_is_prerelease_target_negative(title: str) -> None:
    assert not td.is_prerelease_target({"title": title, "body": ""})


def test_is_prerelease_target_detects_in_body_for_grouped_pr() -> None:
    """Grouped PR titles often hide the version - body must be scanned too."""
    pr = {
        "title": "Bump the dependencies group with 1 update",
        "body": "Updates python from 3.14.5-slim to 3.15.0b2-slim",
    }
    assert td.is_prerelease_target(pr)


def test_decide_routes_prerelease_to_close(tmp_path: Path) -> None:
    pr = _base_pr(
        number=520,
        title="chore(deps): bump python from 3.14.5-slim to 3.15.0b2-slim",
        url="https://github.com/github-community-projects/stale-repos/pull/520",
    )
    decision = _decide(pr)
    assert decision.outcome == td.OUTCOME_CLOSE_PRERELEASE
    assert "prerelease" in decision.reason


def test_decide_prerelease_check_is_after_humans_engaged(tmp_path: Path) -> None:
    """Human review activity wins over the prerelease close: maybe the human
    is actively evaluating whether to take the beta. Don't yank the PR
    out from under them."""
    pr = _base_pr(
        title="Bump foo from 1.0.0 to 2.0.0-beta.1",
        comments=[{"author": {"login": "iansan5653"}, "body": "looks interesting"}],
    )
    decision = _decide(pr)
    assert decision.outcome == td.OUTCOME_FLAG
    assert decision.reason == "human review activity present"


def test_decide_prerelease_check_runs_before_rebase(tmp_path: Path) -> None:
    """A prerelease PR that's behind shouldn't get a rebase comment - we're
    just going to close it. Saves a wasted rebase round-trip."""
    pr = _base_pr(
        title="Bump foo from 1.0.0 to 2.0.0-beta.1",
        mergeStateStatus="behind",
    )
    decision = _decide(pr)
    assert decision.outcome == td.OUTCOME_CLOSE_PRERELEASE


def test_do_dependabot_close_dry_run_no_subprocess() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_dependabot_close("o/r", 1, dry_run=True)
    mocked.assert_not_called()


def test_do_dependabot_close_force_closes_via_api() -> None:
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_dependabot_close("github-community-projects/stale-repos", 520, dry_run=False)
    assert mocked.call_count == 1
    close_args = mocked.call_args_list[0][0][0]
    assert close_args[:2] == ["pr", "close"]
    assert "520" in close_args
    assert "--repo" in close_args
    assert "github-community-projects/stale-repos" in close_args
    assert "--delete-branch" in close_args


def test_do_dependabot_close_does_not_post_dependabot_close_comment() -> None:
    """Regression guard for the comment-spam pathology. Posting
    ``@dependabot close`` is pure noise on the PR timeline now that
    ``gh pr close --delete-branch`` closes the PR directly, so the
    comment must never be sent."""
    with mock.patch.object(td, "run_gh") as mocked:
        td.do_dependabot_close("github-community-projects/stale-repos", 520, dry_run=False)
    for call in mocked.call_args_list:
        args = call[0][0]
        assert args[:2] != ["pr", "comment"], (
            "do_dependabot_close must not post any PR comment; got "
            f"{args!r}"
        )


def test_do_dependabot_close_swallows_already_closed_error() -> None:
    """If ``gh pr close`` fails with an 'already closed' stderr (a benign
    race where Dependabot or a parallel run closed the PR before we
    got there), the tool must not crash."""

    def _run_gh_side_effect(args: list[str], *, timeout: int = 60) -> str:
        if args[:2] == ["pr", "close"]:
            raise subprocess.CalledProcessError(
                1,
                ["gh", *args],
                stderr="pull request is closed",
            )
        return ""

    with mock.patch.object(td, "run_gh", side_effect=_run_gh_side_effect) as mocked:
        td.do_dependabot_close(
            "github-community-projects/contributors", 496, dry_run=False
        )
    assert mocked.call_count == 1


def test_do_dependabot_close_propagates_real_close_failure() -> None:
    """If ``gh pr close`` fails for a real reason (auth, rate limit,
    branch deletion, network), the error MUST propagate so the outer
    run loop records it and skips the cooldown / mark-done that would
    otherwise hide the still-open PR. Without this, the fix would
    silently re-introduce the spam pathology it is trying to escape."""

    def _run_gh_side_effect(args: list[str], *, timeout: int = 60) -> str:
        if args[:2] == ["pr", "close"]:
            raise subprocess.CalledProcessError(
                1,
                ["gh", *args],
                stderr="HTTP 403: API rate limit exceeded for installation",
            )
        return ""

    with mock.patch.object(td, "run_gh", side_effect=_run_gh_side_effect):
        with pytest.raises(subprocess.CalledProcessError):
            td.do_dependabot_close(
                "github-community-projects/contributors", 496, dry_run=False
            )


def test_do_dependabot_close_propagates_timeout() -> None:
    """A timeout on the close call leaves the PR in unknown state -
    propagate so the outer loop retries on the next cron tick instead
    of silently marking the notification done."""

    def _run_gh_side_effect(args: list[str], *, timeout: int = 60) -> str:
        if args[:2] == ["pr", "close"]:
            raise subprocess.TimeoutExpired(cmd=["gh", *args], timeout=timeout)
        return ""

    with mock.patch.object(td, "run_gh", side_effect=_run_gh_side_effect):
        with pytest.raises(subprocess.TimeoutExpired):
            td.do_dependabot_close(
                "github-community-projects/contributors", 496, dry_run=False
            )


def test_run_closes_prerelease_and_marks_notification_done(tmp_path: Path) -> None:
    """End-to-end: a Dependabot PR targeting a beta version is
    force-closed via ``gh pr close --delete-branch``, the notification
    is marked done, and the cooldown is applied so the next run skips
    it."""
    notif = {
        "id": "thread-prerelease",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/github-community-projects/stale-repos/pulls/520",
        },
    }
    pr = _base_pr(
        number=520,
        title="chore(deps): bump python from 3.14.5-slim to 3.15.0b2-slim in the dependencies group",
        url="https://github.com/github-community-projects/stale-repos/pull/520",
    )
    args = _make_args(tmp_path)
    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "do_dependabot_close"
    ) as close_mock, mock.patch.object(
        td, "do_merge"
    ) as merge_mock, mock.patch.object(
        td, "mark_thread_done"
    ) as mark_done_mock:
        stats = td.run(args)

    assert stats.closed_prerelease == 1
    assert stats.merged == 0
    assert stats.flagged == 0
    assert stats.errors == []
    close_mock.assert_called_once_with(
        "github-community-projects/stale-repos", 520, dry_run=False
    )
    merge_mock.assert_not_called()
    mark_done_mock.assert_called_once_with("thread-prerelease", dry_run=False)
    state = td.load_state(args.state_file)
    assert (
        "https://github.com/github-community-projects/stale-repos/pull/520" in state
    )


def test_run_merge_cooldown_set_when_mark_done_fails(tmp_path: Path) -> None:
    """If mark_thread_done throws after a successful merge, the cooldown
    state MUST still be written so the next cron tick does not try to
    re-merge the already-merged PR. The mark-done failure is recorded
    in stats.errors and processing continues.

    Regression guard for a latent bug at the run-loop call site where
    a notifications-API hiccup mid-tick would jump to the outer except
    and leave state[pr_url] unset, causing the next tick to re-run
    do_merge against a closed PR."""
    notif = {
        "id": "thread-merge-flaky-mark",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r1/pulls/1",
        },
    }
    pr = _base_pr(number=1, url="https://github.com/o/r1/pull/1")
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=95
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td,
        "mark_thread_done",
        side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 500"),
    ):
        stats = td.run(args)

    assert stats.merged == 1
    assert any("mark-done failed for merged" in e for e in stats.errors)
    state = td.load_state(args.state_file)
    assert "https://github.com/o/r1/pull/1" in state


def test_run_label_and_merge_cooldown_set_when_mark_done_fails(
    tmp_path: Path,
) -> None:
    """Same cooldown-preservation invariant for the label-and-merge path."""
    notif = {
        "id": "thread-lm-flaky-mark",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r1/pulls/7",
        },
    }
    pr = _base_pr(
        number=7,
        url="https://github.com/o/r1/pull/7",
        title="chore(deps): bump cryptography from 41.0.0 to 41.0.7 (CVE-2024-1234)",
        body="Fixes CVE-2024-1234, a security advisory.",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "detect_repo_coverage", return_value=95
    ), mock.patch.object(
        td, "fetch_repo_labels", return_value={"release"}
    ), mock.patch.object(
        td, "is_security_change", return_value=True
    ), mock.patch.object(
        td, "do_add_label"
    ), mock.patch.object(
        td, "do_merge"
    ), mock.patch.object(
        td,
        "mark_thread_done",
        side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=20),
    ):
        stats = td.run(args)

    assert stats.labeled_and_merged == 1
    assert any("mark-done failed for labeled-and-merged" in e for e in stats.errors)
    state = td.load_state(args.state_file)
    assert "https://github.com/o/r1/pull/7" in state


def test_run_close_prerelease_cooldown_set_when_mark_done_fails(
    tmp_path: Path,
) -> None:
    """Same cooldown-preservation invariant for the close-prerelease path.

    Without this guard a notifications-API hiccup right after the
    direct close would leave the cooldown unset, and the next cron
    tick (within 1 hour) could re-encounter the still-listed
    notification and re-run the close path against an already-closed
    PR (reproducing the contributors#496 churn pattern from a
    different angle)."""
    notif = {
        "id": "thread-prerelease-flaky-mark",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r1/pulls/520",
        },
    }
    pr = _base_pr(
        number=520,
        url="https://github.com/o/r1/pull/520",
        title="chore(deps): bump python from 3.14.5-slim to 3.15.0b2-slim",
    )
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "do_dependabot_close"
    ), mock.patch.object(
        td,
        "mark_thread_done",
        side_effect=subprocess.CalledProcessError(
            1, ["gh"], stderr="HTTP 403: API rate limit exceeded"
        ),
    ):
        stats = td.run(args)

    assert stats.closed_prerelease == 1
    assert any("mark-done failed for closed-prerelease" in e for e in stats.errors)
    state = td.load_state(args.state_file)
    assert "https://github.com/o/r1/pull/520" in state


def test_safe_mark_thread_done_no_thread_is_noop() -> None:
    """Empty thread_id is the documented no-op (matches the prior
    ``if thread_id:`` guard at the call sites)."""
    stats = td.TriageStats()
    with mock.patch.object(td, "mark_thread_done") as mocked:
        result = td._safe_mark_thread_done(
            "", dry_run=False, stats=stats, context="ctx"
        )
    assert result is True
    mocked.assert_not_called()
    assert stats.errors == []


def test_safe_mark_thread_done_records_failure() -> None:
    """A flaky mark-done returns False and appends to stats.errors so
    callers can branch on it if they need to."""
    stats = td.TriageStats()
    with mock.patch.object(
        td,
        "mark_thread_done",
        side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 500"),
    ):
        result = td._safe_mark_thread_done(
            "thread-x", dry_run=False, stats=stats, context="merged https://x/y"
        )
    assert result is False
    assert len(stats.errors) == 1
    assert "mark-done failed for merged https://x/y" in stats.errors[0]


def test_run_terminal_skip_cooldown_set_when_mark_done_fails(
    tmp_path: Path,
) -> None:
    """Same cooldown-preservation invariant for the terminal-skip path
    (closed/merged PRs). Without this guard, a flaky mark-done on a
    closed PR would leave the cooldown unset, and the next cron tick
    would re-fetch the same closed PR and retry mark-done forever."""
    notif = {
        "id": "thread-closed-flaky-mark",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/9",
        },
    }
    pr = _base_pr(number=9, url="https://github.com/o/r/pull/9", state="closed")
    args = _make_args(tmp_path)

    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td,
        "mark_thread_done",
        side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 500"),
    ):
        stats = td.run(args)

    assert stats.skipped == 1
    assert any("mark-done failed for terminal-skip" in e for e in stats.errors)
    state = td.load_state(args.state_file)
    assert "https://github.com/o/r/pull/9" in state


def test_run_closes_prerelease_dry_run_does_not_post(tmp_path: Path) -> None:
    """Dry-run prerelease close: count the action, don't actually post the
    comment, don't mark the thread done, don't write state."""
    notif = {
        "id": "thread-prerelease-dry",
        "reason": "subscribed",
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/o/r/pulls/9",
        },
    }
    pr = _base_pr(number=9, title="Bump foo from 1.0.0 to 2.0.0-beta.1")
    args = _make_args(tmp_path, dry_run=True)
    with mock.patch.object(
        td, "get_my_login", return_value="zkoppert"
    ), mock.patch.object(
        td, "fetch_notifications", return_value=[notif]
    ), mock.patch.object(
        td, "fetch_pr", return_value=pr
    ), mock.patch.object(
        td, "run_gh"
    ) as run_gh_mock:
        stats = td.run(args)

    assert stats.closed_prerelease == 1
    # No mutating gh call should fire in dry-run (the only run_gh calls that
    # could happen here come from is_archived_repo / coverage, both of which
    # are autouse-stubbed or unused in this path).
    for call in run_gh_mock.call_args_list:
        cmd = call[0][0]
        assert "comment" not in cmd, f"unexpected comment call: {cmd}"
        assert not (cmd[0] == "api" and "-X" in cmd and "DELETE" in cmd), (
            f"unexpected DELETE call: {cmd}"
        )
    assert not args.state_file.exists()
