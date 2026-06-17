#!/usr/bin/env python3
"""Tests for lint.py - run with: python3 tests.py"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lint import Violation, find_violations, main  # noqa: E402


def rules_in(violations: list[Violation]) -> set[str]:
    return {v.rule for v in violations}


class TestCleanText(unittest.TestCase):
    def test_clean_text_produces_no_violations(self):
        text = (
            "This is a clean sentence with no style issues.\n"
            "It runs at 3 requests per second.\n"
            "We saw [the dashboard](https://example.com) light up.\n"
            "Thanks for the review!\n"
            "I made an error in the writeup.\n"
        )
        self.assertEqual(find_violations(text), [])


class TestEmDash(unittest.TestCase):
    def test_single_em_dash_flagged(self):
        violations = find_violations("Use ecosystem tools \u2014 they reduce mistakes.")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "no-em-dash")

    def test_multiple_em_dashes_on_same_line_each_flagged(self):
        violations = find_violations("Hello \u2014 world \u2014 again.")
        self.assertEqual(
            len([v for v in violations if v.rule == "no-em-dash"]), 2
        )

    def test_intraword_hyphen_is_not_flagged(self):
        violations = find_violations("The runner-up used a well-known trick.")
        self.assertNotIn("no-em-dash", rules_in(violations))
        self.assertNotIn("no-spaced-dash", rules_in(violations))


class TestSpacedDash(unittest.TestCase):
    def test_spaced_hyphen_punctuation_flagged(self):
        violations = find_violations("Our diff is dashboard only, so these are master drift - they came in.")
        self.assertIn("no-spaced-dash", rules_in(violations))

    def test_spaced_hyphen_not_flagged_as_em_dash(self):
        violations = find_violations("Use a hyphen - like this - between words.")
        self.assertNotIn("no-em-dash", rules_in(violations))
        self.assertEqual(
            len([v for v in violations if v.rule == "no-spaced-dash"]), 2
        )

    def test_spaced_en_dash_punctuation_flagged(self):
        violations = find_violations("We shipped it \u2013 then reverted.")
        self.assertIn("no-spaced-dash", rules_in(violations))

    def test_intraword_hyphen_not_flagged(self):
        violations = find_violations("The runner-up is a well-known face.")
        self.assertNotIn("no-spaced-dash", rules_in(violations))

    def test_unspaced_en_dash_range_not_flagged(self):
        violations = find_violations("Page 5\u20137 covers it.")
        self.assertNotIn("no-spaced-dash", rules_in(violations))

    def test_line_start_bullet_not_flagged(self):
        violations = find_violations("- a clean bullet item")
        self.assertNotIn("no-spaced-dash", rules_in(violations))

    def test_indented_bullet_not_flagged(self):
        violations = find_violations("  - an indented clean bullet item")
        self.assertNotIn("no-spaced-dash", rules_in(violations))

    def test_spaced_dash_inside_inline_code_ignored(self):
        violations = find_violations("Run `foo - bar` to see output.")
        self.assertNotIn("no-spaced-dash", rules_in(violations))

    def test_en_dash_is_not_flagged(self):
        violations = find_violations("Page 5\u20137 covers it.")
        self.assertNotIn("no-em-dash", rules_in(violations))


class TestPerAsAccordingTo(unittest.TestCase):
    def test_per_the_is_flagged(self):
        violations = find_violations("Per the docs, we should update this.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_my_is_flagged(self):
        violations = find_violations("Per my last email, the date is moving.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_their_is_flagged(self):
        violations = find_violations("Per their request, we will reschedule.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_named_possessive_is_flagged(self):
        violations = find_violations("Per Andi's recommendation, we shipped.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_named_possessive_with_smart_quote_is_flagged(self):
        # GitHub PR descriptions and Slack often substitute straight ' with smart '.
        violations = find_violations("Per Andi\u2019s recommendation, we shipped.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_name_with_internal_apostrophe_is_flagged(self):
        # Names like O'Brien must not let the possessive sneak past.
        violations = find_violations("Per O'Brien's recommendation, we shipped.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_lowercase_proper_noun_possessive_is_flagged(self):
        # "per github's policy" / "per npm's convention" — lowercase brand/tool names.
        violations = find_violations("Per github's policy, we close stale PRs.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_lowercase_npm_possessive_is_flagged(self):
        violations = find_violations("per npm's convention, we use kebab-case.")
        self.assertIn("no-per-as-according-to", rules_in(violations))

    def test_per_second_is_not_flagged(self):
        violations = find_violations("3 errors per second, sustained.")
        self.assertNotIn("no-per-as-according-to", rules_in(violations))

    def test_per_comment_is_not_flagged(self):
        violations = find_violations("One point per comment is the rule.")
        self.assertNotIn("no-per-as-according-to", rules_in(violations))

    def test_per_unit_minute_is_not_flagged(self):
        violations = find_violations("Two requests per minute, max.")
        self.assertNotIn("no-per-as-according-to", rules_in(violations))

    def test_per_capita_is_not_flagged(self):
        violations = find_violations("Cost per capita went down.")
        self.assertNotIn("no-per-as-according-to", rules_in(violations))


class TestPrayerHands(unittest.TestCase):
    def test_prayer_hands_flagged(self):
        violations = find_violations("Thanks \U0001F64F for the review!")
        self.assertIn("no-prayer-hands", rules_in(violations))

    def test_other_emoji_not_flagged(self):
        violations = find_violations("Thanks \U0001F44D for the review!")
        self.assertNotIn("no-prayer-hands", rules_in(violations))


class TestClickHere(unittest.TestCase):
    def test_click_here_flagged(self):
        violations = find_violations("See [click here](https://example.com).")
        self.assertIn("no-click-here", rules_in(violations))

    def test_bare_here_flagged(self):
        violations = find_violations("See [here](https://example.com).")
        self.assertIn("no-click-here", rules_in(violations))

    def test_reference_style_here_flagged(self):
        # `[here][1]` is a Markdown reference-style link with non-descriptive text.
        violations = find_violations("See [here][1]. [1]: https://example.com")
        self.assertIn("no-click-here", rules_in(violations))

    def test_click_here_case_insensitive(self):
        violations = find_violations(
            "[Click Here](https://example.com) and [HERE](https://example.com)."
        )
        self.assertEqual(
            len([v for v in violations if v.rule == "no-click-here"]), 2
        )

    def test_descriptive_link_text_not_flagged(self):
        violations = find_violations("See [the dashboard](https://example.com).")
        self.assertNotIn("no-click-here", rules_in(violations))

    def test_word_here_outside_link_not_flagged(self):
        violations = find_violations("The dashboard is here, and it works.")
        self.assertNotIn("no-click-here", rules_in(violations))


class TestIspIncident(unittest.TestCase):
    def test_isp_incident_flagged(self):
        violations = find_violations("We saw an ISP incident yesterday.")
        self.assertIn("no-isp-incident", rules_in(violations))

    def test_isp_incident_case_insensitive(self):
        violations = find_violations("isp Incident at 3pm.")
        self.assertIn("no-isp-incident", rules_in(violations))

    def test_isp_incidents_plural_flagged(self):
        violations = find_violations("We had three ISP incidents this week.")
        self.assertIn("no-isp-incident", rules_in(violations))

    def test_plain_incident_not_flagged(self):
        violations = find_violations("We saw an incident yesterday.")
        self.assertNotIn("no-isp-incident", rules_in(violations))


class TestAgenticPassive(unittest.TestCase):
    def test_claude_made_flagged(self):
        violations = find_violations("Claude made an error in my writeup.")
        self.assertIn("no-agentic-passive", rules_in(violations))

    def test_chatgpt_wrote_flagged(self):
        # `\bGPT\b` would not fire inside ChatGPT (no word boundary between t and G).
        violations = find_violations("ChatGPT wrote the script for me.")
        self.assertIn("no-agentic-passive", rules_in(violations))

    def test_gpt_wrote_flagged(self):
        violations = find_violations("GPT wrote the script for me.")
        self.assertIn("no-agentic-passive", rules_in(violations))

    def test_copilot_generated_flagged(self):
        violations = find_violations("Copilot generated the entire test file.")
        self.assertIn("no-agentic-passive", rules_in(violations))

    def test_the_model_wrote_flagged(self):
        violations = find_violations("The model wrote this section.")
        self.assertIn("no-agentic-passive", rules_in(violations))

    def test_the_agent_produced_flagged(self):
        violations = find_violations("The agent produced these results.")
        self.assertIn("no-agentic-passive", rules_in(violations))

    def test_first_person_not_flagged(self):
        violations = find_violations("I made an error in the writeup.")
        self.assertNotIn("no-agentic-passive", rules_in(violations))


class TestThisPrSubject(unittest.TestCase):
    def test_this_pr_at_line_start_flagged(self):
        violations = find_violations("This PR adds retry logic to the handler.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_this_change_at_line_start_flagged(self):
        violations = find_violations("This change introduces a new flag.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_this_commit_at_line_start_flagged(self):
        violations = find_violations("This commit fixes the off-by-one.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_this_pull_request_flagged(self):
        violations = find_violations("This pull request closes #42.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_this_mr_flagged(self):
        violations = find_violations("This MR introduces the helper.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_this_pr_after_period_flagged(self):
        # Sentence-initial mid-paragraph is also a violation.
        violations = find_violations("Closes #1. This PR also updates the docs.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_this_pr_case_insensitive(self):
        violations = find_violations("this pr adds X.")
        self.assertIn("no-this-pr-subject", rules_in(violations))

    def test_mid_sentence_this_pr_not_flagged(self):
        # "merging this PR" is the object of a verb, not the subject.
        violations = find_violations("After merging this PR, run the deploy script.")
        self.assertNotIn("no-this-pr-subject", rules_in(violations))

    def test_first_person_pr_phrasing_not_flagged(self):
        violations = find_violations("I opened this PR to fix the bug.")
        self.assertNotIn("no-this-pr-subject", rules_in(violations))

    def test_inside_inline_code_not_flagged(self):
        # PR templates and docs sometimes show the bad pattern as an example.
        violations = find_violations("Avoid `This PR adds X` - use first person.")
        self.assertNotIn("no-this-pr-subject", rules_in(violations))


class TestSubjectlessActionBullet(unittest.TestCase):
    def test_added_bullet_flagged(self):
        violations = find_violations("- Added one new post under _posts/.")
        self.assertIn("no-subjectless-action-bullet", rules_in(violations))

    def test_inspected_bullet_flagged(self):
        violations = find_violations("- Inspected the rendered HTML.")
        self.assertIn("no-subjectless-action-bullet", rules_in(violations))

    def test_ran_bullet_flagged(self):
        violations = find_violations("- Ran `validate-style` on the draft.")
        self.assertIn("no-subjectless-action-bullet", rules_in(violations))

    def test_asterisk_bullet_flagged(self):
        violations = find_violations("* Removed the deprecated flag.")
        self.assertIn("no-subjectless-action-bullet", rules_in(violations))

    def test_numbered_bullet_flagged(self):
        violations = find_violations("1. Updated the frontmatter image block.")
        self.assertIn("no-subjectless-action-bullet", rules_in(violations))

    def test_indented_bullet_flagged(self):
        violations = find_violations("  - Renamed the helper to make the intent clearer.")
        self.assertIn("no-subjectless-action-bullet", rules_in(violations))

    def test_first_person_bullet_not_flagged(self):
        violations = find_violations("- I added one new post under _posts/.")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_we_bullet_not_flagged(self):
        violations = find_violations("- We added retry logic to the handler.")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_imperative_bullet_not_flagged(self):
        # Setup steps and TODO items legitimately use bare imperatives ("Add X").
        violations = find_violations("- Add the API key to your `.env` file.")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_run_imperative_bullet_not_flagged(self):
        violations = find_violations("- Run `make test` before pushing.")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_prose_paragraph_starting_with_verb_not_flagged(self):
        # By design we only flag bullets - prose has too many false positives
        # ("Updated tests confirm the fix").
        violations = find_violations("Updated tests confirm the fix landed.")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_non_action_word_bullet_not_flagged(self):
        violations = find_violations("- Notes on the rollout below.")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_lowercase_verb_bullet_not_flagged(self):
        # Lowercase ("added X") is usually a continuation of a previous bullet
        # or wrapped line - not a fresh action report. Skip to avoid noise.
        violations = find_violations("- added one new post")
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))

    def test_inside_fenced_block_not_flagged(self):
        text = (
            "Example of a bad bullet:\n"
            "```\n"
            "- Added retry logic\n"
            "```\n"
        )
        violations = find_violations(text)
        self.assertNotIn("no-subjectless-action-bullet", rules_in(violations))


class TestLineAndColumn(unittest.TestCase):
    def test_line_number_reported_correctly(self):
        text = "Line one is clean.\nLine two has a \u2014 dash here.\nLine three is fine."
        violations = [v for v in find_violations(text) if v.rule == "no-em-dash"]
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].line, 2)
        self.assertGreater(violations[0].column, 0)

    def test_column_is_one_based(self):
        violations = [v for v in find_violations("\u2014 leading") if v.rule == "no-em-dash"]
        self.assertEqual(violations[0].column, 1)


class TestCLI(unittest.TestCase):
    def test_clean_file_exit_zero(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("This is fine.\n")
            path = f.name
        try:
            with redirect_stdout(io.StringIO()):
                code = main([path])
            self.assertEqual(code, 0)
        finally:
            os.unlink(path)

    def test_dirty_file_exit_one(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("This has an \u2014 em-dash.\n")
            path = f.name
        try:
            with redirect_stdout(io.StringIO()):
                code = main([path])
            self.assertEqual(code, 1)
        finally:
            os.unlink(path)

    def test_missing_file_exit_two(self):
        with redirect_stdout(io.StringIO()):
            code = main(["/nonexistent/path/that/does/not/exist.md"])
        self.assertEqual(code, 2)

    def test_non_utf8_file_exit_two_clean(self):
        # Binary/non-UTF-8 input must fail gracefully, not crash with traceback.
        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as f:
            f.write(b"\xff\xfe\x00\x01invalid utf-8 \xc3\x28 bytes\n")
            path = f.name
        try:
            stderr_buf = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = stderr_buf
            try:
                with redirect_stdout(io.StringIO()):
                    code = main([path])
            finally:
                sys.stderr = old_stderr
            self.assertEqual(code, 2)
            self.assertIn("decoding", stderr_buf.getvalue().lower())
        finally:
            os.unlink(path)

    def test_dash_path_reads_stdin(self):
        # `lint.py -` is a common Unix convention; document and support it.
        text_with_em_dash = "This has an \u2014 em-dash."
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(text_with_em_dash)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["-"])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 1)
        self.assertIn("<stdin>", buf.getvalue())

    def test_dash_path_with_json(self):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("Per the docs, this is bad.\n")
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--json", "-"])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 1)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["sources"][0]["source"], "<stdin>")

    def test_json_output_well_formed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("Per the docs, this is bad.\n")
            path = f.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--json", path])
            self.assertEqual(code, 1)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["total_violations"], 1)
            self.assertEqual(data["sources"][0]["source"], path)
            self.assertEqual(
                data["sources"][0]["violations"][0]["rule"], "no-per-as-according-to"
            )
        finally:
            os.unlink(path)


class TestCodeRegionMasking(unittest.TestCase):
    def test_em_dash_inside_inline_code_is_ignored(self):
        text = "The rule forbids `foo \u2014 bar` as an example.\n"
        self.assertEqual(find_violations(text), [])

    def test_em_dash_in_prose_still_flagged_when_line_has_code(self):
        text = "We saw `clean code` and then prose \u2014 with an em-dash.\n"
        violations = find_violations(text)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "no-em-dash")

    def test_per_inside_inline_code_is_ignored(self):
        text = "Avoid examples like `per the docs` in the rule listing.\n"
        self.assertEqual(find_violations(text), [])

    def test_agentic_passive_inside_inline_code_is_ignored(self):
        text = 'Counter-example: `Claude wrote this` - do not phrase it that way.\n'
        self.assertEqual(find_violations(text), [])

    def test_em_dash_inside_fenced_block_is_ignored(self):
        text = (
            "Before the fence.\n"
            "```\n"
            "literal \u2014 em-dash here\n"
            "literal per the docs\n"
            "```\n"
            "After the fence.\n"
        )
        self.assertEqual(find_violations(text), [])

    def test_em_dash_after_fenced_block_is_flagged(self):
        text = (
            "```\n"
            "code with \u2014 inside\n"
            "```\n"
            "Now in prose \u2014 here.\n"
        )
        violations = find_violations(text)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "no-em-dash")
        self.assertEqual(violations[0].line, 4)

    def test_tilde_fenced_block_is_respected(self):
        text = (
            "~~~yaml\n"
            "key: value \u2014 with em-dash\n"
            "~~~\n"
        )
        self.assertEqual(find_violations(text), [])

    def test_unclosed_fence_masks_remainder(self):
        text = (
            "Prose first.\n"
            "```\n"
            "code with \u2014 em-dash\n"
            "more code per the docs\n"
        )
        self.assertEqual(find_violations(text), [])

    def test_columns_remain_accurate_after_masking(self):
        prefix = "Outside `code span here` then \u2014 dash"
        violations = find_violations(prefix + "\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].column, prefix.index("\u2014") + 1)


class TestPrivateRepoRef(unittest.TestCase):
    """Tests for the no-private-repo-ref rule (--check-visibility)."""

    def _find_with_mock_visibility(self, text, visibility_map):
        """Run _find_private_repo_refs with a mocked visibility checker."""
        with unittest.mock.patch(
            "lint._check_repo_visibility",
            side_effect=lambda repo: visibility_map.get(repo),
        ):
            from lint import _find_private_repo_refs, mask_code_regions
            return _find_private_repo_refs(mask_code_regions(text))

    def test_github_url_private_repo_flagged(self):
        text = "See https://github.com/acme/secret-repo/pull/123 for details."
        violations = self._find_with_mock_visibility(text, {"acme/secret-repo": "private"})
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "no-private-repo-ref")
        self.assertIn("acme/secret-repo", violations[0].text)

    def test_github_url_public_repo_not_flagged(self):
        text = "See https://github.com/actions/checkout for the action."
        violations = self._find_with_mock_visibility(text, {"actions/checkout": "public"})
        self.assertEqual(len(violations), 0)

    def test_shorthand_private_repo_flagged(self):
        text = "Fixed in acme/internal-service#42."
        violations = self._find_with_mock_visibility(text, {"acme/internal-service": "private"})
        self.assertEqual(len(violations), 1)
        self.assertIn("acme/internal-service#42", violations[0].text)

    def test_shorthand_public_repo_not_flagged(self):
        text = "Related to actions/checkout#99."
        violations = self._find_with_mock_visibility(text, {"actions/checkout": "public"})
        self.assertEqual(len(violations), 0)

    def test_internal_visibility_flagged(self):
        text = "Documented in https://github.com/corp/wiki-repo/issues/5."
        violations = self._find_with_mock_visibility(text, {"corp/wiki-repo": "internal"})
        self.assertEqual(len(violations), 1)
        self.assertIn("internal", violations[0].message)

    def test_bare_word_slash_word_not_matched(self):
        """Bare prose like 'input/output' should not trigger API calls."""
        text = "The input/output ratio was fine. Also client/server architecture."
        with unittest.mock.patch("lint._check_repo_visibility") as mock_check:
            from lint import _find_private_repo_refs, mask_code_regions
            _find_private_repo_refs(mask_code_regions(text))
            mock_check.assert_not_called()

    def test_exclusion_skips_file_paths(self):
        """Paths like src/main, bin/utils should be excluded."""
        text = "Check https://github.com/src/main for the source."
        with unittest.mock.patch("lint._check_repo_visibility") as mock_check:
            from lint import _find_private_repo_refs, mask_code_regions
            _find_private_repo_refs(mask_code_regions(text))
            mock_check.assert_not_called()

    def test_deduplication(self):
        """Same repo referenced twice should only check visibility once."""
        text = "See acme/repo#1 and acme/repo#2."
        with unittest.mock.patch(
            "lint._check_repo_visibility", return_value="private"
        ) as mock_check:
            from lint import _find_private_repo_refs, mask_code_regions
            violations = _find_private_repo_refs(mask_code_regions(text))
            mock_check.assert_called_once_with("acme/repo")
            self.assertEqual(len(violations), 1)

    def test_code_block_refs_not_checked(self):
        """Repo refs inside code blocks should be masked and not checked."""
        text = "```\ngithub/secret#123\n```"
        with unittest.mock.patch("lint._check_repo_visibility") as mock_check:
            from lint import _find_private_repo_refs, mask_code_regions
            _find_private_repo_refs(mask_code_regions(text))
            mock_check.assert_not_called()

    def test_api_failure_does_not_flag(self):
        """If the API returns None (error/timeout), do not flag."""
        text = "See https://github.com/unknown/repo/pull/1."
        violations = self._find_with_mock_visibility(text, {"unknown/repo": None})
        self.assertEqual(len(violations), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
