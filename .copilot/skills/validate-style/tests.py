#!/usr/bin/env python3
"""Tests for lint.py - run with: python3 tests.py"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
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

    def test_hyphen_is_not_flagged(self):
        violations = find_violations("Use a hyphen - like this - between words.")
        self.assertNotIn("no-em-dash", rules_in(violations))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
