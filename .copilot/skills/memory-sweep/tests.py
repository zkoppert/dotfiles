#!/usr/bin/env python3
"""Unit tests for sweep.py."""

import tempfile
import unittest
from pathlib import Path

from sweep import (
    Memory,
    classify,
    extract_quoted_phrases,
    extract_tokens,
    parse_memories,
    run_sweep,
)


class TestParseMemories(unittest.TestCase):
    def test_parses_basic_block(self) -> None:
        text = (
            "**writing style**\n"
            "- Fact: Never use em-dashes in any text written on Zack's behalf.\n"
            "- Citations: User input: \"stop using em dashes\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].subject, "writing style")
        self.assertIn("em-dashes", result[0].fact)
        self.assertIn("stop using em dashes", result[0].citations)

    def test_parses_multiple_blocks_separated_by_blank_lines(self) -> None:
        text = (
            "**alpha**\n"
            "- Fact: First rule about alpha.\n"
            "- Citations: User input: \"alpha source\"\n"
            "\n"
            "**beta**\n"
            "- Fact: Second rule about beta.\n"
            "- Citations: User input: \"beta source\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].subject, "alpha")
        self.assertEqual(result[1].subject, "beta")

    def test_ignores_markdown_headings_and_blank_lines(self) -> None:
        text = (
            "## User memories for @zkoppert\n"
            "\n"
            "**writing style**\n"
            "- Fact: Some rule text.\n"
            "- Citations: User input: \"source quote\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].subject, "writing style")

    def test_handles_multi_line_fact_continuation(self) -> None:
        text = (
            "**topic**\n"
            "- Fact: This rule has more detail\n"
            "  on a second line for clarity.\n"
            "- Citations: User input: \"quote\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertIn("second line", result[0].fact)

    def test_returns_empty_list_for_empty_input(self) -> None:
        self.assertEqual(parse_memories(""), [])

    def test_skips_entry_with_subject_but_no_fact(self) -> None:
        text = "**orphan subject**\n"
        self.assertEqual(parse_memories(text), [])

    def test_skips_entry_with_empty_fact_line(self) -> None:
        text = "**topic**\n- Fact: \n- Citations: User input: \"x\"\n"
        self.assertEqual(parse_memories(text), [])

    def test_accepts_h2_heading_as_subject(self) -> None:
        text = (
            "### writing style\n"
            "- Fact: Never use em-dashes.\n"
            "- Citations: User input: \"x\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].subject, "writing style")
        self.assertIn("em-dashes", result[0].fact)

    def test_does_not_append_unrecognized_field_to_fact(self) -> None:
        text = (
            "**topic**\n"
            "- Fact: Never use em-dashes.\n"
            "- Note: this is extra prose that must not be appended.\n"
            "- Citations: User input: \"x\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fact, "Never use em-dashes.")
        self.assertNotIn("extra prose", result[0].fact)

    def test_non_indented_prose_does_not_pollute_fact(self) -> None:
        text = (
            "**topic**\n"
            "- Fact: Never use em-dashes.\n"
            "Stray paragraph that should not be part of the fact.\n"
            "- Citations: User input: \"x\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fact, "Never use em-dashes.")
        self.assertNotIn("Stray paragraph", result[0].fact)

    def test_multiple_facts_under_one_subject_become_separate_memories(self) -> None:
        text = (
            "**writing style**\n"
            "- Fact: First rule about writing.\n"
            "- Citations: User input: \"first source\"\n"
            "- Fact: Second rule about writing.\n"
            "- Citations: User input: \"second source\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].subject, "writing style")
        self.assertEqual(result[1].subject, "writing style")
        self.assertIn("First rule", result[0].fact)
        self.assertIn("Second rule", result[1].fact)
        self.assertIn("first source", result[0].citations)
        self.assertIn("second source", result[1].citations)

    def test_handles_multi_line_citations_continuation(self) -> None:
        text = (
            "**topic**\n"
            "- Fact: A short fact.\n"
            "- Citations: User input: \"source one\"\n"
            "  continued source line\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertIn("source one", result[0].citations)
        self.assertIn("continued source line", result[0].citations)

    def test_accepts_h4_heading_as_subject(self) -> None:
        text = (
            "#### deep topic\n"
            "- Fact: An h4 heading should also start a new subject block.\n"
            "- Citations: User input: \"x\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].subject, "deep topic")

    def test_ignores_h1_heading_as_subject_marker(self) -> None:
        text = (
            "# top level title\n"
            "- Fact: An h1 heading should not start a subject block.\n"
            "- Citations: User input: \"x\"\n"
        )
        result = parse_memories(text)
        self.assertEqual(result, [])


class TestExtractTokens(unittest.TestCase):
    def test_returns_distinctive_words(self) -> None:
        tokens = extract_tokens("Always use Python typing in new files.")
        # 'use' and 'the' are filtered by the 4-char minimum; 'new' is too
        # short to reach STOPWORDS. 'always' is a stopword.
        self.assertIn("python", tokens)
        self.assertIn("typing", tokens)
        self.assertIn("files", tokens)
        self.assertNotIn("the", tokens)
        self.assertNotIn("always", tokens)

    def test_filters_short_words(self) -> None:
        tokens = extract_tokens("Run gh pr ready guard now.")
        self.assertNotIn("gh", tokens)
        self.assertNotIn("pr", tokens)
        self.assertIn("guard", tokens)
        self.assertIn("ready", tokens)


class TestExtractQuotedPhrases(unittest.TestCase):
    def test_finds_quoted_substrings(self) -> None:
        text = 'Use "alpha beta gamma" instead of "delta epsilon zeta".'
        phrases = extract_quoted_phrases(text)
        self.assertIn("alpha beta gamma", phrases)
        self.assertIn("delta epsilon zeta", phrases)

    def test_ignores_empty_quotes(self) -> None:
        phrases = extract_quoted_phrases('Something "" empty here.')
        self.assertEqual(phrases, [])


class TestClassify(unittest.TestCase):
    def _tokens(self, text: str) -> set[str]:
        return extract_tokens(text)

    def test_present_when_tokens_appear_in_instructions(self) -> None:
        instructions = (
            "never use em-dashes in any text written on zack's behalf, "
            "including commit messages and pr descriptions."
        ).lower()
        memory = Memory(
            subject="writing style",
            fact="Never use em-dashes in any text written on Zack's behalf.",
            citations="",
        )
        finding = classify(memory, self._tokens(instructions), instructions)
        self.assertEqual(finding.verdict, "PRESENT")
        self.assertGreaterEqual(finding.score, 0.7)

    def test_promote_when_no_overlap_with_instructions(self) -> None:
        instructions = "this file talks about completely unrelated stuff.".lower()
        memory = Memory(
            subject="github mentions",
            fact="Never include @username mentions without checking first.",
            citations="",
        )
        finding = classify(memory, self._tokens(instructions), instructions)
        self.assertEqual(finding.verdict, "PROMOTE")
        self.assertLess(finding.score, 0.3)

    def test_phrase_match_drives_score_when_tokens_are_all_stopwords(self) -> None:
        # The fact's distinctive tokens (enforce, everywhere, consistently)
        # do not appear in instructions, so token_score is 0. Only the
        # quoted phrase "no per" matches verbatim. The phrase bonus alone
        # must carry the score to AMBIGUOUS - regression test for the
        # earlier bug where the empty/no-overlap token guard discarded
        # the phrase bonus entirely. The phrase is chosen so its words
        # ("no", "per") are both filtered out by tokenization (stopword
        # and below the 4-character cutoff) and therefore cannot leak
        # into the token score.
        instructions = 'follow the "no per" rule strictly here.'.lower()
        memory = Memory(
            subject="writing style",
            fact='Enforce "no per" everywhere consistently.',
            citations="",
        )
        finding = classify(memory, self._tokens(instructions), instructions)
        self.assertEqual(finding.matched_phrases, ["no per"])
        self.assertEqual(finding.matched_tokens, [])
        self.assertGreaterEqual(finding.score, 0.3)
        self.assertLess(finding.score, 0.7)
        self.assertEqual(finding.verdict, "AMBIGUOUS")

    def test_substring_match_does_not_inflate_score(self) -> None:
        # 'rate' is a substring of 'separate' and 'aggregate' but is NOT
        # an instructions token. The classifier must reject the match.
        instructions = "separate concerns aggregate metrics deploy services.".lower()
        memory = Memory(
            subject="rate limiting",
            fact="Limit rate when sending requests.",
            citations="",
        )
        finding = classify(memory, self._tokens(instructions), instructions)
        self.assertNotIn("rate", finding.matched_tokens)
        self.assertEqual(finding.verdict, "PROMOTE")
        self.assertLess(finding.score, 0.3)


class TestRunSweep(unittest.TestCase):
    def test_end_to_end_classifies_each_memory(self) -> None:
        memories_text = (
            "**covered topic**\n"
            "- Fact: Always use bicycle helmets when riding velocipedes.\n"
            "- Citations: User input: \"x\"\n"
            "\n"
            "**uncovered topic**\n"
            "- Fact: A completely orthogonal rule about brontosaurs.\n"
            "- Citations: User input: \"y\"\n"
        )
        instructions_text = (
            "# instructions\n\n"
            "Always wear bicycle helmets when riding velocipedes on roads.\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mem_path = Path(tmpdir) / "memories.md"
            ins_path = Path(tmpdir) / "instructions.md"
            mem_path.write_text(memories_text)
            ins_path.write_text(instructions_text)

            findings, counts = run_sweep(mem_path, ins_path)

        self.assertEqual(len(findings), 2)
        verdicts = {f.memory.subject: f.verdict for f in findings}
        self.assertEqual(verdicts["covered topic"], "PRESENT")
        self.assertEqual(verdicts["uncovered topic"], "PROMOTE")
        self.assertEqual(counts["PRESENT"], 1)
        self.assertEqual(counts["PROMOTE"], 1)


if __name__ == "__main__":
    unittest.main()
