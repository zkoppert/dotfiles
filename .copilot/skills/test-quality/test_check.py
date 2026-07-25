#!/usr/bin/env python3
"""Tests for check.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("check.py")
LOADER = SourceFileLoader("test_quality_check", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader("test_quality_check", LOADER)
assert SPEC
check = importlib.util.module_from_spec(SPEC)
sys.modules["test_quality_check"] = check
LOADER.exec_module(check)


class Repo:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def write(self, path: str, content: str, *, executable: bool = False) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if executable:
            target.chmod(0o755)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)


def make_repo(tmp: str) -> Repo:
    repo = Repo(Path(tmp))
    repo.git("init", "-q")
    repo.git("config", "user.email", "test@example.com")
    repo.git("config", "user.name", "test-quality")
    repo.git("checkout", "-q", "-b", "main")
    repo.write("README.md", "# fixture\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    return repo


class TestAnalysis(unittest.TestCase):
    def test_source_without_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.commit("source")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.evidence, "missing")
            self.assertIn("source-without-test", {item.rule for item in result.errors})

    def test_meaningful_waiver_allows_source_without_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.commit("source")
            reason = "Generated output is verified by the schema compatibility command."

            result = check.analyze(
                cwd=repo.root,
                base_ref="main",
                no_test_change_reason=reason,
            )

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.evidence, "waived")
            self.assertEqual(result.waiver, reason)

    def test_placeholder_waiver_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.commit("source")

            result = check.analyze(
                cwd=repo.root,
                base_ref="main",
                no_test_change_reason="not needed",
            )

            self.assertEqual(result.status, "failed")
            self.assertIn(
                "invalid-no-test-waiver", {item.rule for item in result.errors}
            )

    def test_source_and_test_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.write(
                "test_tool.py",
                "from tool import value\n\ndef test_value():\n    assert value() == 1\n",
            )
            repo.commit("source and test")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.evidence, "present")
            self.assertEqual(result.source_files, ["tool.py"])
            self.assertEqual(result.test_files, ["test_tool.py"])

    def test_module_level_tests_py_and_unittest_assertion_are_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.write(
                "tests.py",
                "import unittest\n\n"
                "class ToolTest(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 1)\n",
            )
            repo.commit("source and module test")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.test_files, ["tests.py"])
            self.assertNotIn(
                "test-change-without-assertion",
                {item.rule for item in result.warnings},
            )
            self.assertIsNone(check.UNITTEST_ASSERTION_RE.search("# assertion later"))
            self.assertIsNotNone(
                check.UNITTEST_ASSERTION_RE.search("self.assertEqual(value(), 1)")
            )

    def test_docs_only_is_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("README.md", "# updated\n")
            repo.commit("docs")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.evidence, "not-applicable")

    def test_extensionless_executable_is_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("bin/tool", "#!/usr/bin/env python3\nprint('ok')\n", executable=True)
            repo.commit("script")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.source_files, ["bin/tool"])

    def test_removing_script_markers_still_counts_as_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("bin/tool", "#!/usr/bin/env bash\necho ok\n", executable=True)
            repo.commit("add script")
            repo.git("checkout", "-q", "main")
            repo.git("merge", "-q", "--ff-only", "feature")
            repo.git("checkout", "-q", "-b", "remove-script-markers")
            repo.write("bin/tool", "echo no longer executable\n")
            (repo.root / "bin/tool").chmod(0o644)
            repo.commit("remove script markers")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.source_files, ["bin/tool"])
            self.assertIn("source-without-test", {item.rule for item in result.errors})

    def test_modern_javascript_module_extensions_are_source(self) -> None:
        for extension in (".cjs", ".cts", ".mjs", ".mts"):
            with self.subTest(extension=extension):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = make_repo(tmp)
                    path = f"tool{extension}"
                    repo.write(path, "export const value = 1\n")
                    repo.commit("module source")

                    result = check.analyze(cwd=repo.root, base_ref="main")

                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.source_files, [path])

    def test_inline_rust_tests_count_as_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write(
                "src/lib.rs",
                "pub fn value() -> i32 { 1 }\n\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::value;\n\n"
                "    #[test]\n"
                "    fn returns_one() { assert_eq!(value(), 1); }\n"
                "}\n",
            )
            repo.commit("rust source and inline test")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.evidence, "present")
            self.assertEqual(result.source_files, ["src/lib.rs"])
            self.assertEqual(result.test_files, ["src/lib.rs"])
            self.assertNotIn(
                "test-change-without-assertion",
                {item.rule for item in result.warnings},
            )

    def test_fixture_file_does_not_count_as_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.write("tests/fixture.json", '{"value": 1}\n')
            repo.commit("source and fixture")

            original_tree_entry = check.tree_entry
            original_has_shebang = check.has_shebang
            with patch.object(
                check, "tree_entry", wraps=original_tree_entry
            ) as tree_entry_mock, patch.object(
                check, "has_shebang", wraps=original_has_shebang
            ) as shebang_mock:
                result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.evidence, "missing")
            self.assertEqual(result.test_files, [])
            self.assertIn("source-without-test", {item.rule for item in result.errors})
            self.assertEqual(tree_entry_mock.call_count, 0)
            self.assertEqual(shebang_mock.call_count, 0)

    def test_renamed_script_checks_the_original_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            original = "#!/usr/bin/env bash\n" + "".join(
                f"echo line-{index}\n" for index in range(30)
            )
            repo.write("bin/tool", original, executable=True)
            repo.commit("add script")
            repo.git("checkout", "-q", "main")
            repo.git("merge", "-q", "--ff-only", "feature")
            repo.git("checkout", "-q", "-b", "rename-script")
            repo.git("mv", "bin/tool", "bin/tool-renamed")
            renamed = repo.root / "bin/tool-renamed"
            renamed.write_text(
                original.removeprefix("#!/usr/bin/env bash\n"),
                encoding="utf-8",
            )
            renamed.chmod(0o644)
            repo.commit("rename and remove script markers")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.source_files, ["bin/tool-renamed"])

    def test_extensionless_executable_in_test_directory_counts_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.write(
                "tests/check-tool",
                "#!/usr/bin/env bash\npython3 -c 'from tool import value; assert value() == 1'\n",
                executable=True,
            )
            repo.commit("source and executable test")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.evidence, "present")
            self.assertEqual(result.test_files, ["tests/check-tool"])

    def test_new_catch_all_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.write(
                "test_coverage_additions.py",
                "def test_value():\n    assert True\n",
            )
            repo.commit("coverage file")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertEqual(result.status, "failed")
            self.assertIn("new-catch-all-test", {item.rule for item in result.errors})

    def test_deleted_tests_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("test_tool.py", "def test_value():\n    assert True\n")
            repo.commit("add test")
            repo.git("checkout", "-q", "main")
            repo.git("merge", "-q", "--ff-only", "feature")
            repo.git("checkout", "-q", "-b", "delete-tests")
            (repo.root / "test_tool.py").unlink()
            repo.commit("delete test")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertIn(
                "deleted-tests-without-replacement",
                {item.rule for item in result.warnings},
            )

    def test_threshold_changes_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write(
                "Makefile",
                "test:\n\tpytest --cov-fail-under=90\nmax-module-lines=500\n",
            )
            repo.write(
                "ci.mk",
                "test:\n\tpytest --cov-fail-under=70\nmax-module-lines=1000\n",
            )
            repo.commit("threshold base")
            repo.git("checkout", "-q", "main")
            repo.git("merge", "-q", "--ff-only", "feature")
            repo.git("checkout", "-q", "-b", "threshold-change")
            repo.write(
                "Makefile",
                "test:\n\tpytest --cov-fail-under=80\nmax-module-lines=700\n",
            )
            repo.write(
                "ci.mk",
                "test:\n\tpytest --cov-fail-under=95\nmax-module-lines=800\n",
            )
            repo.commit("loosen thresholds")

            result = check.analyze(cwd=repo.root, base_ref="main")
            rules = {item.rule for item in result.warnings}

            coverage_finding = next(
                item
                for item in result.warnings
                if item.rule == "coverage-threshold-lowered"
            )
            self.assertEqual(coverage_finding.paths, ["Makefile"])
            self.assertIn("module-size-threshold-raised", rules)

    def test_coverage_prose_and_weak_assertions_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return object()\n")
            repo.write(
                "test_tool.py",
                'def test_value():\n    """Covers tool.py:1 coverage gap."""\n'
                "    assert value() is not None\n",
            )
            repo.commit("weak test")

            result = check.analyze(cwd=repo.root, base_ref="main")
            rules = {item.rule for item in result.warnings}

            self.assertIn("coverage-shaped-test", rules)
            self.assertIn("weak-assertion", rules)

    def test_assert_true_warns_as_weak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.write("test_tool.py", "def test_value():\n    assert True\n")
            repo.commit("shallow test")

            result = check.analyze(cwd=repo.root, base_ref="main")

            self.assertIn("weak-assertion", {item.rule for item in result.warnings})

    def test_no_inferred_base_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.git("init", "-q")
            repo.git("config", "user.email", "test@example.com")
            repo.git("config", "user.name", "test-quality")
            repo.git("checkout", "-q", "-b", "feature")
            repo.write("README.md", "# one\n")
            repo.commit("one")
            repo.write("README.md", "# two\n")
            repo.commit("two")

            with patch.dict(
                os.environ,
                {"TEST_QUALITY_BASE_REF": "", "GITHUB_BASE_REF": ""},
                clear=False,
            ):
                result = check.analyze(cwd=repo.root)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.evidence, "unavailable")

    def test_root_commit_uses_empty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.git("init", "-q")
            repo.git("config", "user.email", "test@example.com")
            repo.git("config", "user.name", "test-quality")
            repo.git("checkout", "-q", "-b", "feature")
            repo.write("README.md", "# one\n")
            repo.commit("root")

            with patch.dict(
                os.environ,
                {"TEST_QUALITY_BASE_REF": "", "GITHUB_BASE_REF": ""},
                clear=False,
            ):
                result = check.analyze(cwd=repo.root)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.base_ref, "(empty tree)")

    def test_root_commit_on_main_uses_empty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.git("init", "-q")
            repo.git("config", "user.email", "test@example.com")
            repo.git("config", "user.name", "test-quality")
            repo.git("checkout", "-q", "-b", "main")
            repo.write("tool.py", "def value():\n    return 1\n")
            repo.commit("root source")

            result = check.analyze(cwd=repo.root)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.base_ref, "(empty tree)")
            self.assertEqual(result.source_files, ["tool.py"])

    def test_explicit_missing_base_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)

            result = check.analyze(cwd=repo.root, base_ref="missing")

            self.assertEqual(result.status, "error")
            self.assertIn("invalid-base", {item.rule for item in result.errors})


class TestCLI(unittest.TestCase):
    def test_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            repo.write("README.md", "# updated\n")
            repo.commit("docs")
            script = str(MODULE_PATH)

            result = subprocess.run(
                [sys.executable, script, "--base", "main", "--json"],
                cwd=repo.root,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)

            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["evidence"], "not-applicable")
            self.assertEqual(payload["error_count"], 0)

    def test_skipped_analysis_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.git("init", "-q")
            repo.git("config", "user.email", "test@example.com")
            repo.git("config", "user.name", "test-quality")
            repo.git("checkout", "-q", "-b", "feature")
            repo.write("README.md", "# one\n")
            repo.commit("one")
            repo.write("README.md", "# two\n")
            repo.commit("two")
            env = dict(os.environ)
            env.pop("TEST_QUALITY_BASE_REF", None)
            env.pop("GITHUB_BASE_REF", None)

            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--json"],
                cwd=repo.root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(payload["status"], "skipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
