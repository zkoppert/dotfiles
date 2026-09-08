#!/usr/bin/env python3
"""Unit tests for sweep.py."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sweep import (
    Memory,
    classify,
    collect_guidance_files,
    extract_quoted_phrases,
    extract_tokens,
    format_finding,
    main,
    parse_memories,
    resolve_path,
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

    def test_normalizes_hyphenated_and_spaced_terms(self) -> None:
        self.assertEqual(
            extract_tokens("Never use em-dashes."),
            extract_tokens("Never use em dashes."),
        )


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
        self.assertGreaterEqual(finding.score, 0.9)

    def test_present_when_hyphenation_differs_from_instructions(self) -> None:
        instructions = "Never use em dashes in text written on Zack's behalf.".lower()
        memory = Memory(
            subject="writing style",
            fact="Never use em-dashes in any text written on Zack's behalf.",
            citations="",
        )

        finding = classify(memory, self._tokens(instructions), instructions)

        self.assertEqual(finding.verdict, "PRESENT")

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

    def test_unrelated_vocabulary_match_remains_ambiguous(self) -> None:
        instructions = (
            "Before starting a workflow, review the session and order the tasks. "
            "Coffee preparation uses separate guidance."
        ).lower()
        memory = Memory(
            subject="coffee",
            fact="Always order the espresso before starting the review workflow session.",
            citations="",
        )

        finding = classify(memory, self._tokens(instructions), instructions)

        self.assertEqual(finding.verdict, "AMBIGUOUS")
        self.assertLess(finding.score, 0.9)


class TestFormatFinding(unittest.TestCase):
    def test_includes_candidate_citations(self) -> None:
        memory = Memory(
            subject="session rule",
            fact="Always preserve source provenance.",
            citations="Session abc at 2026-09-07T10:00:00-07:00",
        )
        finding = classify(memory, set(), "")

        rendered = format_finding(finding)

        self.assertIn("citations: Session abc at 2026-09-07T10:00:00-07:00", rendered)

    def test_preserves_long_candidate_citations(self) -> None:
        citations = " | ".join(
            f"Session {index:03d} at 2026-09-07T10:00:00-07:00"
            for index in range(8)
        )
        memory = Memory(
            subject="session rule",
            fact="Always preserve every source identifier.",
            citations=citations,
        )

        rendered = format_finding(classify(memory, set(), ""))

        self.assertIn(citations, rendered)


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

    def test_combines_multiple_guidance_files(self) -> None:
        candidates_text = (
            "**skill rule**\n"
            "- Fact: Always validate release milestones before publication.\n"
            "- Citations: Session abc\n"
            "\n"
            "**repo rule**\n"
            "- Fact: Always run the repository smoke test before merging.\n"
            "- Citations: Session def\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates_path = root / "candidates.md"
            skill_path = root / "SKILL.md"
            repo_path = root / "AGENTS.md"
            candidates_path.write_text(candidates_text)
            skill_path.write_text("Validate release milestones before publication.")
            repo_path.write_text("Run the repository smoke test before merging.")

            findings, counts = run_sweep(candidates_path, [skill_path, repo_path])

        self.assertEqual(len(findings), 2)
        self.assertEqual(counts["PRESENT"], 2)

    def test_reports_the_highest_scoring_guidance_file(self) -> None:
        candidates_text = (
            "**source attribution**\n"
            "- Fact: Require frobnicator velocipede deployment reviews.\n"
            "- Citations: Session abc\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates_path = root / "candidates.md"
            partial_path = root / "AGENTS.md"
            complete_path = root / "SKILL.md"
            candidates_path.write_text(candidates_text)
            partial_path.write_text("Require frobnicator reviews.")
            complete_path.write_text("Require frobnicator velocipede deployment reviews.")

            findings, _ = run_sweep(
                candidates_path,
                [partial_path, complete_path],
            )
            rendered = format_finding(findings[0])

        self.assertEqual(findings[0].guidance_path, complete_path)
        self.assertIn(f"closest guidance: {complete_path}", rendered)

    def test_score_tie_prefers_guidance_with_no_missing_tokens(self) -> None:
        candidates_text = (
            "**source attribution**\n"
            '- Fact: Require "release milestones" approval before publication.\n'
            "- Citations: Session abc\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates_path = root / "candidates.md"
            partial_path = root / "AGENTS.md"
            complete_path = root / "SKILL.md"
            candidates_path.write_text(candidates_text)
            partial_path.write_text('Require "release milestones" before publication.')
            complete_path.write_text(
                'Require "release milestones" approval before publication.'
            )

            findings, _ = run_sweep(
                candidates_path,
                [partial_path, complete_path],
            )

        self.assertEqual(findings[0].score, 1.0)
        self.assertEqual(findings[0].guidance_path, complete_path)
        self.assertEqual(findings[0].missing_tokens, [])

    def test_does_not_combine_partial_matches_across_guidance_files(self) -> None:
        candidates_text = (
            "**scattered rule**\n"
            "- Fact: Require frobnicator velocipede brontosaur deployment.\n"
            "- Citations: Session abc\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates_path = root / "candidates.md"
            first_path = root / "AGENTS.md"
            second_path = root / "SKILL.md"
            candidates_path.write_text(candidates_text)
            first_path.write_text("Require frobnicator velocipede.")
            second_path.write_text("Brontosaur deployment guidance.")

            findings, counts = run_sweep(
                candidates_path,
                [first_path, second_path],
            )

        self.assertEqual(findings[0].verdict, "AMBIGUOUS")
        self.assertEqual(counts["PRESENT"], 0)

    def test_does_not_score_guidance_path_tokens(self) -> None:
        candidates_text = (
            "**path-only rule**\n"
            "- Fact: Always batch dependabot notifications weekly.\n"
            "- Citations: Session abc\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates_path = root / "candidates.md"
            guidance_path = root / "dependabot-notifications" / "SKILL.md"
            candidates_path.write_text(candidates_text)
            guidance_path.parent.mkdir()
            guidance_path.write_text("Nothing relevant.")

            findings, _ = run_sweep(candidates_path, guidance_path)

        self.assertEqual(findings[0].verdict, "PROMOTE")
        self.assertEqual(findings[0].matched_tokens, [])


class TestCollectGuidanceFiles(unittest.TestCase):
    def test_symlink_loop_resolution_raises_cli_friendly_error(self) -> None:
        loop_error = RuntimeError("Symlink loop from loop")
        with patch("sweep.Path.resolve", side_effect=loop_error), self.assertRaisesRegex(
            IOError,
            "could not resolve guidance path",
        ):
            resolve_path(Path("loop"))

    def test_discovers_supported_guidance_and_ignores_other_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            guidance_names = {
                "AGENTS.md",
                "CLAUDE.md",
                "GEMINI.md",
                "SKILL.md",
                "copilot-instructions.md",
                "ruby.instructions.md",
            }
            guidance_paths = []
            for name in guidance_names:
                guidance_path = root / "guidance" / name
                guidance_path.parent.mkdir(exist_ok=True)
                guidance_path.write_text(f"{name} guidance")
                guidance_paths.append(guidance_path)
            readme = root / "README.md"
            readme.write_text("not guidance")

            result = collect_guidance_files([root])

        self.assertEqual({path.name for path in result}, guidance_names)

    def test_explicit_file_is_accepted_regardless_of_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            history = Path(tmpdir) / "session-history.md"
            history.write_text("session evidence")

            result = collect_guidance_files([history])

        self.assertEqual(result, [history])

    def test_follows_skill_symlinks_and_deduplicates_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            skills = root / ".copilot" / "skills"
            source.mkdir()
            skills.mkdir(parents=True)
            skill = source / "SKILL.md"
            skill.write_text("skill rule")
            (skills / "linked-skill").symlink_to(source, target_is_directory=True)

            result = collect_guidance_files([skills, skill])
            resolved_result = result[0].resolve()
            resolved_skill = skill.resolve()

        self.assertEqual(len(result), 1)
        self.assertEqual(resolved_result, resolved_skill)

    def test_relative_user_skill_path_follows_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            copilot = root / ".copilot"
            skills = copilot / "skills"
            source.mkdir()
            skills.mkdir(parents=True)
            skill = source / "SKILL.md"
            skill.write_text("skill rule")
            (skills / "linked-skill").symlink_to(source, target_is_directory=True)
            original_cwd = Path.cwd()
            os.chdir(copilot)
            try:
                result = collect_guidance_files([Path("skills")])
                resolved_result = result[0].resolve()
                resolved_skill = skill.resolve()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(len(result), 1)
        self.assertEqual(resolved_result, resolved_skill)

    def test_normalized_user_skill_path_follows_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            skills = root / ".copilot" / "skills"
            source.mkdir()
            skills.mkdir(parents=True)
            skill = source / "SKILL.md"
            skill.write_text("skill rule")
            (skills / "linked-skill").symlink_to(source, target_is_directory=True)

            result = collect_guidance_files([skills / ".." / "skills"])
            resolved_result = result[0].resolve()
            resolved_skill = skill.resolve()

        self.assertEqual(len(result), 1)
        self.assertEqual(resolved_result, resolved_skill)

    def test_symlinked_user_skill_root_follows_immediate_skill_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stored_skills = root / "stored-skills"
            supplied_skills = root / "home" / ".copilot" / "skills"
            skill_source = root / "skill-source"
            stored_skills.mkdir()
            supplied_skills.parent.mkdir(parents=True)
            skill_source.mkdir()
            skill_rule = skill_source / "SKILL.md"
            skill_rule.write_text("skill guidance")
            (stored_skills / "example").symlink_to(skill_source, target_is_directory=True)
            supplied_skills.symlink_to(stored_skills, target_is_directory=True)

            result = collect_guidance_files([supplied_skills])
            resolved_result = result[0].resolve()
            resolved_skill = skill_rule.resolve()

        self.assertEqual(len(result), 1)
        self.assertEqual(resolved_result, resolved_skill)

    def test_skips_dependency_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ignored = root / "node_modules" / "package" / "SKILL.md"
            included = root / "AGENTS.md"
            ignored.parent.mkdir(parents=True)
            ignored.write_text("dependency guidance")
            included.write_text("repository guidance")

            result = collect_guidance_files([root])

        self.assertEqual(result, [included])

    def test_stops_on_cyclic_skill_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills = root / ".copilot" / "skills"
            skill = skills / "skill"
            skill.mkdir(parents=True)
            skill_file = skill / "SKILL.md"
            skill_file.write_text("skill rule")
            (skill / "loop").symlink_to(skills, target_is_directory=True)

            result = collect_guidance_files([skills])
            resolved_result = result[0].resolve()
            resolved_skill = skill_file.resolve()

        self.assertEqual(len(result), 1)
        self.assertEqual(resolved_result, resolved_skill)

    def test_repository_scan_does_not_follow_external_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repository = root / "repository"
            external = root / "external"
            repository.mkdir()
            external.mkdir()
            repo_rule = repository / "AGENTS.md"
            external_rule = external / "SKILL.md"
            repo_rule.write_text("repository guidance")
            external_rule.write_text("unrelated guidance")
            (repository / "linked-external").symlink_to(external, target_is_directory=True)

            result = collect_guidance_files([repository])

        self.assertEqual(result, [repo_rule])

    def test_repository_scan_ignores_external_file_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repository = root / "repository"
            external = root / "external"
            repository.mkdir()
            external.mkdir()
            repo_rule = repository / "AGENTS.md"
            external_rule = external / "SKILL.md"
            repo_rule.write_text("repository guidance")
            external_rule.write_text("unrelated guidance")
            (repository / "SKILL.md").symlink_to(external_rule)

            result = collect_guidance_files([repository])

        self.assertEqual(result, [repo_rule])

    def test_skill_scan_does_not_follow_nested_external_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            external = root / "external"
            skills = root / ".copilot" / "skills"
            source.mkdir()
            external.mkdir()
            skills.mkdir(parents=True)
            skill_rule = source / "SKILL.md"
            external_rule = external / "AGENTS.md"
            skill_rule.write_text("skill guidance")
            external_rule.write_text("unrelated guidance")
            (source / "linked-external").symlink_to(external, target_is_directory=True)
            (skills / "linked-skill").symlink_to(source, target_is_directory=True)

            result = collect_guidance_files([skills])
            resolved_result = result[0].resolve()
            resolved_skill = skill_rule.resolve()

        self.assertEqual(len(result), 1)
        self.assertEqual(resolved_result, resolved_skill)

    def test_skill_scan_skips_ignored_broken_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill = root / ".copilot" / "skills" / "example"
            skill.mkdir(parents=True)
            skill_rule = skill / "SKILL.md"
            skill_rule.write_text("skill guidance")
            (skill / "node_modules").symlink_to(root / "missing", target_is_directory=True)

            result = collect_guidance_files([root / ".copilot" / "skills"])
            resolved_result = [path.resolve() for path in result]
            resolved_skill = skill_rule.resolve()

        self.assertEqual(resolved_result, [resolved_skill])

    def test_skill_root_skips_ignored_guidance_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills = root / ".copilot" / "skills"
            skill = skills / "example"
            dependency = skills / "node_modules"
            skill.mkdir(parents=True)
            dependency.mkdir()
            skill_rule = skill / "SKILL.md"
            dependency_rule = dependency / "AGENTS.md"
            skill_rule.write_text("skill guidance")
            dependency_rule.write_text("dependency guidance")

            result = collect_guidance_files([skills])

        self.assertEqual(result, [skill_rule])

    def test_user_skill_root_accepts_guidance_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills = root / ".copilot" / "skills"
            source = root / "source"
            skills.mkdir(parents=True)
            source.mkdir()
            source_rule = source / "AGENTS.md"
            source_rule.write_text("shared guidance")
            linked_rule = skills / "AGENTS.md"
            linked_rule.symlink_to(source_rule)

            result = collect_guidance_files([skills])

        self.assertEqual(result, [linked_rule])

    def test_broken_user_skill_symlink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills = Path(tmpdir) / ".copilot" / "skills"
            skills.mkdir(parents=True)
            (skills / "broken-skill").symlink_to(Path(tmpdir) / "missing")

            with self.assertRaisesRegex(IOError, "skill symlink does not resolve"):
                collect_guidance_files([skills])

    def test_empty_guidance_directory_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(IOError, "no guidance files found"):
                collect_guidance_files([Path(tmpdir)])

    def test_directory_scan_error_raises(self) -> None:
        denied = PermissionError(13, "permission denied", "/private/guidance")
        with patch("sweep.os.scandir", side_effect=denied), self.assertRaisesRegex(
            IOError,
            "could not scan guidance directory",
        ):
            collect_guidance_files([Path(__file__).parent])

    def test_missing_guidance_path_raises(self) -> None:
        with self.assertRaisesRegex(IOError, "guidance path not found"):
            collect_guidance_files([Path("/definitely/missing/guidance")])


class TestReadText(unittest.TestCase):
    def test_invalid_utf8_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = root / "candidates.md"
            guidance = root / "AGENTS.md"
            candidates.write_text(
                "**rule**\n"
                "- Fact: Always preserve readable guidance.\n"
                "- Citations: Session abc\n"
            )
            guidance.write_bytes(b"\xff")

            with self.assertRaisesRegex(IOError, "could not decode guidance file"):
                run_sweep(candidates, guidance)


class TestMain(unittest.TestCase):
    def test_legacy_cli_and_only_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = root / "candidates.md"
            guidance = root / "AGENTS.md"
            candidates.write_text(
                "**covered**\n"
                "- Fact: Always preserve release approval records.\n"
                "- Citations: Session abc\n"
            )
            guidance.write_text("Preserve release approval records.")
            stdout = io.StringIO()

            with (
                patch.object(
                    sys,
                    "argv",
                    ["sweep.py", str(candidates), str(guidance), "--only", "PROMOTE"],
                ),
                contextlib.redirect_stdout(stdout),
            ):
                result = main()

        self.assertEqual(result, 0)
        self.assertIn("(no findings with verdict PROMOTE)", stdout.getvalue())

    def test_missing_candidates_returns_error(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["sweep.py", "/missing/candidates.md", "/missing/guidance.md"],
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = main()

        self.assertEqual(result, 1)
        self.assertIn("candidates file not found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
