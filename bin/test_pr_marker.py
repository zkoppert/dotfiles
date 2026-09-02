#!/usr/bin/env python3
"""Pin pr-marker's branch encoding to gh-guard's sed pipeline.

gh-guard computes the marker filename in bash with:
    printf '%s' "$branch" | sed -e 's/%/%25/g' -e 's|/|%2F|g'
pr-marker must produce the identical result or a marker written by pr-marker
would land at a path the gate does not check. These tests fail loudly if the
two implementations ever drift.

Run: python3 bin/test_pr_marker.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

# pr-marker has no .py extension, so point the loader at it explicitly.
_MODULE_PATH = Path(__file__).with_name("pr-marker")
_LOADER = SourceFileLoader("pr_marker", str(_MODULE_PATH))
_SPEC = importlib.util.spec_from_loader("pr_marker", _LOADER)
assert _SPEC
pr_marker = importlib.util.module_from_spec(_SPEC)
# Register before exec so @dataclass can resolve the module on Python 3.9.
sys.modules["pr_marker"] = pr_marker
_LOADER.exec_module(pr_marker)


def sed_encode(branch: str) -> str:
    """Encode a branch name exactly as gh-guard does, via the real sed pipeline."""
    result = subprocess.run(
        ["sed", "-e", "s/%/%25/g", "-e", "s|/|%2F|g"],
        input=branch,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def bash_count_models(path: str) -> int:
    """Count declared models via gh-guard's exact shell pipeline, for parity.

    Mirrors count_review_models in gh-guard *including* `set -euo pipefail` and
    the substitution-with-default guard, so the Python reader and the bash gate
    cannot silently disagree. Faithfully reproducing pipefail is what catches the
    header-less "0\\n0" fail-open class of bug: a regression to `grep -v '^$'`
    here would make this return "0\\n0" and raise in int(), failing the test.
    """
    script = (
        "set -euo pipefail\n"
        'count="$(\n'
        r"""  sed -n 's/^<!-- reviewed-by-models: \(.*\) -->$/\1/p' "$1" 2>/dev/null """
        "\\\n"
        "    | head -n1 | tr ',' '\\n' \\\n"
        r"""    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' """
        "\\\n"
        "    | tr '[:upper:]' '[:lower:]' | sed '/^$/d' | sort -u | wc -l | tr -d ' '\n"
        ')" || count=""\n'
        'printf "%s" "${count:-0}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip() or "0")


def bash_convergence_rounds(path: str) -> int:
    """Read convergence metadata with gh-guard's exact shell rules."""
    script = (
        "set -euo pipefail\n"
        "count=\"$(grep -c '^<!-- review-convergence:' \"$1\" 2>/dev/null || true)\"\n"
        "rounds=\"$(sed -n "
        "'s/^<!-- review-convergence: clean; rounds: \\([1-9][0-9]*\\) -->$/\\1/p' "
        "\"$1\" 2>/dev/null)\"\n"
        'if [ "${count:-0}" = "1" ] && [[ "$rounds" =~ ^[1-9][0-9]?$ ]]; then\n'
        '  printf "%s" "$rounds"\n'
        "else\n"
        '  printf "0"\n'
        "fi\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip() or "0")


def _gh_guard_create(repo: Path, fake_bin: Path) -> subprocess.CompletedProcess:
    """Run the real gh-guard `pr create` in repo with fake gh on PATH, confirmed."""
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["ZACK_CONFIRMED_PR_CREATE"] = "1"
    guard = Path(__file__).resolve().with_name("gh-guard")
    return subprocess.run(
        [str(guard), "pr", "create", "--title", "x"],
        cwd=str(repo),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _gh_guard_ready(
    args: list[str], *, confirmed_draft: bool = False
) -> subprocess.CompletedProcess:
    """Run gh-guard against a fake gh binary in a noninteractive shell."""
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin = Path(tmp)
        fake_gh = fake_bin / "gh"
        fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
        fake_gh.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin"
        if confirmed_draft:
            env["ZACK_CONFIRMED_PR_DRAFT"] = "1"
        guard = Path(__file__).resolve().with_name("gh-guard")
        return subprocess.run(
            [str(guard), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )


SAMPLE_BRANCHES = [
    "main",
    "feat/foo",
    "feat/foo/bar",
    "feat_foo",
    "a%b",
    "already%2Fencoded",
    "zkoppert/triage-dependabot-force-close-prerelease",
    "release/2026.01",
    "%",
    "/",
    "a/b%c/d",
]


def test_encoding_matches_sed() -> None:
    """pr-marker's encode_branch agrees with gh-guard's real sed pipeline."""
    for branch in SAMPLE_BRANCHES:
        expected = sed_encode(branch)
        actual = pr_marker.encode_branch(branch)
        assert (
            actual == expected
        ), f"encoding drift for {branch!r}: pr-marker={actual!r} sed={expected!r}"


def test_known_encodings() -> None:
    """Spot-check a few branch names against hand-computed encodings."""
    cases = {
        "main": "main",
        "feat/foo": "feat%2Ffoo",
        "a%b": "a%25b",
        "a/b%c": "a%2Fb%25c",
    }
    for branch, expected in cases.items():
        assert pr_marker.encode_branch(branch) == expected


def test_kinds_and_thresholds() -> None:
    """The kind filenames, byte floors, and model requirement match gh-guard."""
    assert pr_marker.KINDS["code-review"].filename == "code-review.md"
    assert pr_marker.KINDS["code-review"].min_bytes == 200
    for name in ("plan", "demo", "pr-review", "tests"):
        assert pr_marker.KINDS[name].filename == f"{name}.md"
        assert pr_marker.KINDS[name].min_bytes == 120
    # The three review kinds require a multi-model review; demo and tests are exempt.
    for name in ("code-review", "plan", "pr-review"):
        assert pr_marker.KINDS[name].requires_models, name
    assert not pr_marker.KINDS["demo"].requires_models
    assert not pr_marker.KINDS["tests"].requires_models
    assert pr_marker.KINDS["code-review"].requires_convergence
    for name in ("plan", "demo", "pr-review", "tests"):
        assert not pr_marker.KINDS[name].requires_convergence, name
    # code-review and tests are HEAD-pinned; the others are not.
    for name in ("code-review", "tests"):
        assert pr_marker.KINDS[name].pinned, name
    for name in ("plan", "demo", "pr-review"):
        assert not pr_marker.KINDS[name].pinned, name
    # Only tests is machine-produced (requires the tests-result header); the
    # rest are hand-written and must NOT require it.
    assert pr_marker.KINDS["tests"].requires_result
    for name in ("code-review", "plan", "demo", "pr-review"):
        assert not pr_marker.KINDS[name].requires_result, name


def test_branch_dir_and_paths() -> None:
    """Each branch gets its own encoded directory holding one file per kind."""
    demo = pr_marker.KINDS["demo"]
    code = pr_marker.KINDS["code-review"]
    bdir = pr_marker.branch_dir(branch="feat/foo")
    assert bdir.name == "feat%2Ffoo"
    demo_path = pr_marker.marker_path(demo, branch="feat/foo")
    code_path = pr_marker.marker_path(code, branch="feat/foo")
    assert demo_path.name == "demo.md"
    assert code_path.name == "code-review.md"
    assert demo_path.parent == bdir
    # The pre-refactor collision: branch `x`'s plan vs branch `x.plan`'s code
    # review must now resolve to different files.
    plan_of_x = pr_marker.marker_path(pr_marker.KINDS["plan"], branch="feat/foo")
    cr_of_x_plan = pr_marker.marker_path(code, branch="feat/foo.plan")
    assert plan_of_x != cr_of_x_plan


def test_artifacts_dir_derivation() -> None:
    """The artifacts dir is a sibling of the demo marker inside the branch dir."""
    demo = pr_marker.KINDS["demo"]
    path = pr_marker.marker_path(demo, branch="feat/foo")
    art = pr_marker.artifacts_dir(demo, branch="feat/foo")
    assert path.name == "demo.md"
    assert art.name == "demo.artifacts"
    assert art.parent == path.parent


def test_gh_guard_matches_kinds() -> None:
    """gh-guard's floors, filenames, pin flags, model + result rules must equal KINDS.

    gh-guard duplicates the byte floors, marker filenames, pin flags, the
    requires-models flag, and the requires-result flag for resilience (so the gate
    works without pr-marker). This asserts the two never drift, which is the real
    risk the "single source of truth" claim rests on.
    """
    gh = Path(__file__).resolve().with_name("gh-guard").read_text(encoding="utf-8")
    for kind in pr_marker.KINDS.values():
        # Filename: gh-guard references it as "$marker_base/<filename>".
        assert f'"$marker_base/{kind.filename}"' in gh, kind.filename

        # Byte floor: the constant CODE_REVIEW_MIN_BYTES etc. equals min_bytes.
        const = kind.name.upper().replace("-", "_") + "_MIN_BYTES"
        match = re.search(rf"^{const}=(\d+)$", gh, re.MULTILINE)
        assert match, f"gh-guard missing constant {const}"
        assert int(match.group(1)) == kind.min_bytes, const

        # The eval_marker line for this kind must reference that constant and end
        # with the pin, model, result, and convergence flags (each 1 or 0).
        line = next(
            ln
            for ln in gh.splitlines()
            if "eval_marker" in ln and f'"$marker_base/{kind.filename}"' in ln
        )
        assert f'"${const}"' in line, f"{kind.name} eval_marker uses wrong floor"
        fields = line.split()
        assert fields[-4] == str(int(kind.pinned)), f"{kind.name} pin flag drift"
        assert fields[-3] == str(
            int(kind.requires_models)
        ), f"{kind.name} requires-models flag drift"
        assert fields[-2] == str(
            int(kind.requires_result)
        ), f"{kind.name} requires-result flag drift"
        assert fields[-1] == str(
            int(kind.requires_convergence)
        ), f"{kind.name} requires-convergence flag drift"

    # The minimum-model threshold must agree between the two implementations.
    match = re.search(r"^MIN_REVIEW_MODELS=(\d+)$", gh, re.MULTILINE)
    assert match, "gh-guard missing constant MIN_REVIEW_MODELS"
    assert int(match.group(1)) == pr_marker.MIN_MODELS, "MIN_MODELS drift"
    assert pr_marker.MIN_REVIEW_ROUNDS == 1
    assert pr_marker.MAX_REVIEW_ROUNDS == 99
    assert (
        r"rounds: \([1-9][0-9]*\)" in gh and r'=~ ^[1-9][0-9]?$' in gh
    ), "gh-guard convergence range drift"

    # The tests-result header gh-guard greps for must match pr-marker's literal,
    # and use whole-line (-x) matching so it agrees with has_result_header.
    assert (
        f"grep -qxF -- '{pr_marker.TESTS_RESULT_HEADER}'" in gh
    ), "gh-guard tests-result header literal drift from pr_marker.TESTS_RESULT_HEADER"


def test_gh_guard_blocks_noninteractive_draft_conversion() -> None:
    """Automation cannot reverse a manual ready-for-review transition."""
    for flag in ("--undo", "--undo=true"):
        result = _gh_guard_ready(["pr", "ready", "32128", flag])

        assert result.returncode == 1
        assert "ZACK_CONFIRMED_PR_DRAFT=1" in result.stderr


def test_gh_guard_allows_explicit_draft_confirmation() -> None:
    """An explicit current-task confirmation can pass the draft guard."""
    result = _gh_guard_ready(
        ["pr", "ready", "32128", "--undo"], confirmed_draft=True
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "pr ready 32128 --undo"


def test_pin_roundtrip() -> None:
    """A pinned marker records HEAD and goes stale once HEAD advances."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            # Configure a repo-local identity so the commits work in a fresh
            # container without an ambient git user.name / user.email.
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/pin")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            code = pr_marker.KINDS["code-review"]
            content = "code review synthesis. " * 12  # > 200 bytes
            payload = Path(tmp) / "body.md"
            payload.write_text(content, encoding="utf-8")
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(payload),
                        "--models",
                        "opus-4.8,sonnet-4.6,gpt-5.5",
                        "--convergence-rounds",
                        "2",
                    ]
                )
                == 0
            )

            head1 = pr_marker.current_head()
            marker = pr_marker.marker_path(code, branch="feat/pin")
            assert pr_marker.read_reviewed_commit(marker) == head1
            assert pr_marker.read_convergence_rounds(marker) == 2
            ok, detail, _size, _path = pr_marker.marker_status(code, "feat/pin")
            assert ok and detail == "ok"

            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")
            ok, detail, _size, _path = pr_marker.marker_status(code, "feat/pin")
            assert not ok and detail == "stale", detail
    finally:
        os.chdir(restore)


def test_run_tests() -> None:
    """run-tests writes a pinned tests marker only when every command passes."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/tests")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            tests = pr_marker.KINDS["tests"]
            marker = pr_marker.marker_path(tests, branch="feat/tests")

            # A failing command writes nothing and exits non-zero.
            assert pr_marker.main(["run-tests", "--cmd", "false"]) == 1
            assert not marker.exists()

            # A failure among passing commands still writes nothing.
            assert pr_marker.main(["run-tests", "--cmd", "true", "--cmd", "false"]) == 1
            assert not marker.exists()

            # All commands passing writes a HEAD-pinned marker recording them.
            assert pr_marker.main(["run-tests", "--cmd", "true", "--cmd", "true"]) == 0
            head1 = pr_marker.current_head()
            assert marker.exists()
            assert pr_marker.read_reviewed_commit(marker) == head1
            body = marker.read_text(encoding="utf-8")
            assert "RESULT: passed" in body
            assert "exit 0" in body
            assert "Test-quality evidence preflight:" in body
            assert "- evidence: not-applicable" in body
            # The machine-produced result header must be present (the gate keys on it).
            assert pr_marker.TESTS_RESULT_HEADER in body
            assert pr_marker.has_result_header(marker)
            ok, detail, _size, _path = pr_marker.marker_status(tests, "feat/tests")
            assert ok and detail == "ok", detail

            # A later failing run must invalidate the prior passing marker, so a
            # green marker can never outlive a subsequent red run.
            assert pr_marker.main(["run-tests", "--cmd", "false"]) == 1
            assert not marker.exists()

            # An empty (or whitespace-only) command is rejected before running,
            # so `run-tests --cmd ''` cannot fabricate a passing marker.
            assert pr_marker.main(["run-tests", "--cmd", "   "]) == 1
            assert not marker.exists()

            # Re-establish a green marker, then confirm a later commit makes the
            # pinned tests marker stale.
            assert pr_marker.main(["run-tests", "--cmd", "true"]) == 0
            assert marker.exists()
            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")
            ok, detail, _size, _path = pr_marker.marker_status(tests, "feat/tests")
            assert not ok and detail == "stale", detail
    finally:
        os.chdir(restore)


def test_test_quality_preflight() -> None:
    """run-tests blocks source-only diffs, then records tests or a waiver."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "main")
            Path("README.md").write_text("# base\n", encoding="utf-8")
            _run("git", "add", "README.md")
            _run("git", "commit", "-q", "-m", "base")
            _run("git", "checkout", "-q", "-b", "feat/quality")

            Path("tool.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            _run("git", "add", "tool.py")
            _run("git", "commit", "-q", "-m", "source")
            tests = pr_marker.KINDS["tests"]
            marker = pr_marker.marker_path(tests, branch="feat/quality")

            assert (
                pr_marker.main(
                    ["run-tests", "--base-ref", "main", "--cmd", "true"]
                )
                == 1
            )
            assert not marker.exists()

            Path("test_tool.py").write_text(
                "from tool import value\n\n"
                "def test_value():\n"
                "    assert value() == 1\n",
                encoding="utf-8",
            )
            _run("git", "add", "test_tool.py")
            _run("git", "commit", "-q", "-m", "test")
            assert (
                pr_marker.main(
                    ["run-tests", "--base-ref", "main", "--cmd", "true"]
                )
                == 0
            )
            body = marker.read_text(encoding="utf-8")
            assert "- evidence: present" in body
            assert "- executable source files changed: 1" in body
            assert "- test files changed: 1" in body

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "main")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")
            _run("git", "checkout", "-q", "-b", "feat/waiver")
            Path("tool.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            _run("git", "add", "tool.py")
            _run("git", "commit", "-q", "-m", "source")
            reason = "Generated output is verified by the schema compatibility command."

            assert (
                pr_marker.main(
                    [
                        "run-tests",
                        "--base-ref",
                        "main",
                        "--no-test-change-reason",
                        reason,
                        "--cmd",
                        "true",
                    ]
                )
                == 0
            )
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/waiver"
            )
            body = marker.read_text(encoding="utf-8")
            assert "- evidence: waived" in body
            assert f"- no-test-change reason: {reason}" in body

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feature-only")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")

            assert pr_marker.main(["run-tests", "--cmd", "true"]) == 1
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feature-only"
            )
            assert not marker.exists()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/invalid-base")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")

            assert (
                pr_marker.main(
                    ["run-tests", "--base-ref", "missing", "--cmd", "true"]
                )
                == 1
            )
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/invalid-base"
            )
            assert not marker.exists()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/legacy-checker")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")
            original_checker_path = pr_marker.test_quality_checker_path
            pr_marker.test_quality_checker_path = lambda: Path(tmp) / "missing-check.py"
            try:
                assert pr_marker.main(["run-tests", "--cmd", "true"]) == 0
            finally:
                pr_marker.test_quality_checker_path = original_checker_path
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/legacy-checker"
            )
            body = marker.read_text(encoding="utf-8")
            assert "- status: unavailable" in body
            assert "- evidence: unavailable" in body
    finally:
        os.chdir(restore)


def test_run_tests_requires_clean_repo() -> None:
    """run-tests refuses dirty inputs and commands that dirty the repository."""
    restore = Path.cwd()
    try:
        for dirty_kind in ("unstaged", "staged", "untracked"):
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                _run("git", "init", "-q")
                _run("git", "config", "user.email", "test@example.com")
                _run("git", "config", "user.name", "pr-marker test")
                _run("git", "checkout", "-q", "-b", f"feat/{dirty_kind}")
                tracked = Path("tracked.txt")
                tracked.write_text("base\n", encoding="utf-8")
                _run("git", "add", "tracked.txt")
                _run("git", "commit", "-q", "-m", "base")

                if dirty_kind == "unstaged":
                    tracked.write_text("changed\n", encoding="utf-8")
                elif dirty_kind == "staged":
                    tracked.write_text("changed\n", encoding="utf-8")
                    _run("git", "add", "tracked.txt")
                else:
                    Path("untracked.txt").write_text("new\n", encoding="utf-8")

                assert pr_marker.main(["run-tests", "--cmd", "true"]) == 1
                marker = pr_marker.marker_path(
                    pr_marker.KINDS["tests"], branch=f"feat/{dirty_kind}"
                )
                assert not marker.exists()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/test-dirties")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")

            assert (
                pr_marker.main(
                    ["run-tests", "--cmd", f"{sys.executable} -c \"open('generated.txt', 'w').write('x')\""]
                )
                == 1
            )
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/test-dirties"
            )
            assert not marker.exists()
    finally:
        os.chdir(restore)


def test_run_tests_rejects_git_state_changes() -> None:
    """run-tests refuses commands that move HEAD or switch branches."""
    restore = Path.cwd()
    try:
        cases = [
            ("feat/head-move", "git commit -q --allow-empty -m c2"),
            ("feat/branch-move", "git checkout -q -b other"),
            ("feat/detach", "git checkout -q --detach"),
        ]
        for branch, command in cases:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                _run("git", "init", "-q")
                _run("git", "config", "user.email", "test@example.com")
                _run("git", "config", "user.name", "pr-marker test")
                _run("git", "checkout", "-q", "-b", branch)
                _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
                marker = pr_marker.marker_path(pr_marker.KINDS["tests"], branch=branch)

                assert pr_marker.main(["run-tests", "--cmd", command]) == 1
                assert not marker.exists()
    finally:
        os.chdir(restore)


def test_tests_marker_is_machine_only() -> None:
    """`write tests` is rejected, and a hand-forged marker lacks the result header."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/tests-write")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            tests = pr_marker.KINDS["tests"]
            marker = pr_marker.marker_path(tests, branch="feat/tests-write")

            # `pr-marker write tests` must refuse: the tests marker is machine-produced.
            # The reject happens before the content is read, so a real file over the
            # byte floor still fails.
            src = Path(tmp) / "payload.md"
            src.write_text("x" * 300, encoding="utf-8")
            assert pr_marker.main(["write", "tests", str(src)]) == 1
            assert not marker.exists()

            # A hand-forged file over the byte floor with a reviewed-commit but no
            # result header must still fail marker_status (the gate keys on the
            # machine-produced header, not just size + pin).
            head = pr_marker.current_head()
            marker.parent.mkdir(parents=True, exist_ok=True)
            forged = (
                f"{pr_marker.REVIEWED_COMMIT_PREFIX}{head}"
                f"{pr_marker.REVIEWED_COMMIT_SUFFIX}\n" + ("y" * 300) + "\n"
            )
            marker.write_text(forged, encoding="utf-8")
            ok, detail, _size, _path = pr_marker.marker_status(
                tests, "feat/tests-write"
            )
            assert not ok and detail == "no result", detail
    finally:
        os.chdir(restore)


def test_parse_models() -> None:
    """parse_models trims, drops empties, and dedupes case-insensitively."""
    assert pr_marker.parse_models(None) == []
    assert pr_marker.parse_models("") == []
    assert pr_marker.parse_models("opus") == ["opus"]
    assert pr_marker.parse_models(" opus , sonnet ,gpt ") == ["opus", "sonnet", "gpt"]
    # Case-insensitive dedupe preserves first-seen casing and order.
    assert pr_marker.parse_models("Opus, opus, SONNET, sonnet") == ["Opus", "SONNET"]
    assert pr_marker.parse_models("a,,b,  ,c") == ["a", "b", "c"]


def test_models_provenance() -> None:
    """Review markers require >=3 distinct models; demo is exempt.

    Also pins that the pinned code-review marker keeps the reviewed-commit header
    on line 1 (so gh-guard's line-1 sed still works) while carrying the models
    header too, and that gh-guard's shell counter agrees with the Python reader.
    """
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/models")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")

            prr = pr_marker.KINDS["pr-review"]
            body = Path(tmp) / "body.md"
            body.write_text("PR description review synthesis. " * 8, encoding="utf-8")

            # No --models: refused.
            assert pr_marker.main(["write", "pr-review", str(body)]) == 1
            # Fewer than 3 distinct (dupes collapse): refused.
            assert (
                pr_marker.main(
                    ["write", "pr-review", str(body), "--models", "opus, opus"]
                )
                == 1
            )
            # Three distinct: accepted.
            assert (
                pr_marker.main(
                    [
                        "write",
                        "pr-review",
                        str(body),
                        "--models",
                        "opus-4.8, sonnet-4.6, gpt-5.5",
                    ]
                )
                == 0
            )
            marker = pr_marker.marker_path(prr, branch="feat/models")
            assert pr_marker.read_reviewed_models(marker) == [
                "opus-4.8",
                "sonnet-4.6",
                "gpt-5.5",
            ]
            ok, detail, _s, _p = pr_marker.marker_status(prr, "feat/models")
            assert ok and detail == "ok", detail
            assert bash_count_models(str(marker)) == 3

            # Strip the models header -> status reports "needs models".
            kept = [
                ln
                for ln in marker.read_text(encoding="utf-8").splitlines()
                if not ln.startswith(pr_marker.REVIEWED_MODELS_PREFIX)
            ]
            marker.write_text("\n".join(kept) + "\n", encoding="utf-8")
            ok, detail, _s, _p = pr_marker.marker_status(prr, "feat/models")
            assert not ok and detail == "needs models", detail
            # The bash counter must also see 0 here (the fail-open regression case).
            assert bash_count_models(str(marker)) == 0

            # Pinned code-review: reviewed-commit on line 1, models header too.
            cr = pr_marker.KINDS["code-review"]
            crbody = Path(tmp) / "cr.md"
            crbody.write_text("code review synthesis. " * 12, encoding="utf-8")
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "opus-4.8,sonnet-4.6,gpt-5.5",
                    ]
                )
                == 1
            )
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "opus-4.8,sonnet-4.6,gpt-5.5",
                        "--convergence-rounds",
                        "3",
                    ]
                )
                == 0
            )
            crpath = pr_marker.marker_path(cr, branch="feat/models")
            first = crpath.read_text(encoding="utf-8").splitlines()[0]
            assert first.startswith(pr_marker.REVIEWED_COMMIT_PREFIX), first
            assert pr_marker.read_reviewed_models(crpath) == [
                "opus-4.8",
                "sonnet-4.6",
                "gpt-5.5",
            ]
            assert pr_marker.read_convergence_rounds(crpath) == 3
            ok, detail, _s, _p = pr_marker.marker_status(cr, "feat/models")
            assert ok and detail == "ok", detail

            # Demo is exempt: no --models needed.
            demo = pr_marker.KINDS["demo"]
            demobody = Path(tmp) / "demo.md"
            demobody.write_text("N/A - no visual surface. " * 6, encoding="utf-8")
            assert pr_marker.main(["write", "demo", str(demobody)]) == 0
            ok, detail, _s, _p = pr_marker.marker_status(demo, "feat/models")
            assert ok and detail == "ok", detail
    finally:
        os.chdir(restore)


def test_convergence_parsing_parity() -> None:
    """Python and gh-guard accept only one canonical clean-round header."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "code-review.md"
        cases = [
            (["<!-- review-convergence: clean; rounds: 2 -->"], 2),
            (["<!-- review-convergence: clean; rounds: 8 -->"], 8),
            (["<!-- review-convergence: clean; rounds: 99 -->"], 99),
            (["<!-- review-convergence: clean; rounds:  2 -->"], 0),
            (["<!-- review-convergence: clean; rounds: two -->"], 0),
            (["<!-- review-convergence: clean; rounds: 100 -->"], 0),
            (["<!-- review-convergence: clean; rounds: 999999999999999999 -->"], 0),
            (
                [
                    "<!-- review-convergence: clean; rounds: two -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
                0,
            ),
            (
                [
                    "<!-- review-convergence: clean; rounds: 2 -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
                0,
            ),
        ]
        for headers, expected in cases:
            marker.write_text("\n".join(headers) + "\n", encoding="utf-8")
            assert (pr_marker.read_convergence_rounds(marker) or 0) == expected
            assert bash_convergence_rounds(str(marker)) == expected


def test_code_review_marker_rewrite() -> None:
    """Rewriting an existing marker replaces generated headers instead of duplicating."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/rewrite")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            body = Path(tmp) / "body.md"
            body.write_text("code review synthesis. " * 12, encoding="utf-8")
            args = [
                "write",
                "code-review",
                str(body),
                "--models",
                "a,b,c",
                "--convergence-rounds",
                "2",
            ]
            assert pr_marker.main(args) == 0
            marker = pr_marker.marker_path(
                pr_marker.KINDS["code-review"], branch="feat/rewrite"
            )
            rewrite_args = [
                "write",
                "code-review",
                str(marker),
                "--models",
                "d,e,f",
                "--convergence-rounds",
                "3",
            ]
            assert pr_marker.main(rewrite_args) == 0
            lines = marker.read_text(encoding="utf-8").splitlines()
            assert (
                sum(
                    line.startswith(pr_marker.REVIEWED_COMMIT_PREFIX)
                    for line in lines
                )
                == 1
            )
            assert (
                sum(
                    line.startswith(pr_marker.REVIEWED_MODELS_PREFIX)
                    for line in lines
                )
                == 1
            )
            assert sum(line.startswith("<!-- review-convergence:") for line in lines) == 1
            assert pr_marker.read_reviewed_models(marker) == ["d", "e", "f"]
            assert pr_marker.read_convergence_rounds(marker) == 3
            ok, detail, _size, _path = pr_marker.marker_status(
                pr_marker.KINDS["code-review"], "feat/rewrite"
            )
            assert ok and detail == "ok", detail
    finally:
        os.chdir(restore)


def test_models_argv_order() -> None:
    """--models parses in either position, incl. the `--models a,b,c -` form."""
    # Optional-before-positional (the natural / gh-guard-suggested order).
    assert pr_marker._reorder_write_argv(
        ["write", "pr-review", "--models", "a,b,c", "-"]
    ) == ["write", "pr-review", "-", "--models", "a,b,c"]
    # --models=VALUE form, also before the positional.
    assert pr_marker._reorder_write_argv(
        [
            "write",
            "code-review",
            "--models=a,b,c",
            "--convergence-rounds",
            "2",
            "body.md",
        ]
    ) == [
        "write",
        "code-review",
        "body.md",
        "--models=a,b,c",
        "--convergence-rounds",
        "2",
    ]
    # Already-trailing optional is left in a working order.
    assert pr_marker._reorder_write_argv(
        ["write", "plan", "-", "--models", "a,b,c"]
    ) == ["write", "plan", "-", "--models", "a,b,c"]
    # Non-write commands are untouched.
    assert pr_marker._reorder_write_argv(["status"]) == ["status"]


def test_gh_guard_gate() -> None:
    """The real gh-guard `pr create` gate blocks review markers with <3 models.

    Drives the actual gh-guard binary (not a Python reimplementation) against a
    fake `gh`, so it exercises count_review_models/eval_marker for real. Covers
    the header-less 0-model case that previously failed open.
    """
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            fake_bin = tmp_p / "bin"
            fake_bin.mkdir()
            gh = fake_bin / "gh"
            gh.write_text("#!/bin/bash\necho FAKE-GH-EXECUTED\n", encoding="utf-8")
            gh.chmod(0o755)

            repo = tmp_p / "repo"
            repo.mkdir()
            os.chdir(repo)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "t@e.com")
            _run("git", "config", "user.name", "t")
            _run("git", "checkout", "-q", "-b", "feat/gate")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")

            body = tmp_p / "body.md"
            body.write_text("review synthesis content padding. " * 6, encoding="utf-8")
            crbody = tmp_p / "cr.md"
            crbody.write_text("code review synthesis padding. " * 10, encoding="utf-8")
            demobody = tmp_p / "demo.md"
            demobody.write_text("N/A no visual surface here. " * 6, encoding="utf-8")

            assert (
                pr_marker.main(["write", "plan", str(body), "--models", "a,b,c"]) == 0
            )
            assert pr_marker.main(["write", "demo", str(demobody)]) == 0
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "a,b,c",
                        "--convergence-rounds",
                        "2",
                    ]
                )
                == 0
            )
            crpath = pr_marker.marker_path(
                pr_marker.KINDS["code-review"], branch="feat/gate"
            )
            without_convergence = [
                line
                for line in crpath.read_text(encoding="utf-8").splitlines()
                if not line.startswith(pr_marker.REVIEW_CONVERGENCE_PREFIX)
            ]
            invalid_headers = [
                [],
                ["<!-- review-convergence: clean; rounds:  2 -->"],
                ["<!-- review-convergence: clean; rounds: 999999999999999999 -->"],
                [
                    "<!-- review-convergence: clean; rounds: two -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
                [
                    "<!-- review-convergence: clean; rounds: 2 -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
            ]
            for headers in invalid_headers:
                crpath.write_text(
                    "\n".join([*without_convergence[:2], *headers, *without_convergence[2:]])
                    + "\n",
                    encoding="utf-8",
                )
                ok, detail, _size, _path = pr_marker.marker_status(
                    pr_marker.KINDS["code-review"], "feat/gate"
                )
                assert not ok and detail == "needs convergence", (headers, detail)
                res = _gh_guard_create(repo, fake_bin)
                out = res.stdout + res.stderr
                assert res.returncode == 1, (headers, out)
                assert "NO-CONVERGENCE" in out, (headers, out)
                assert "FAKE-GH-EXECUTED" not in out, (headers, out)
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "a,b,c",
                        "--convergence-rounds",
                        "2",
                    ]
                )
                == 0
            )
            prr = pr_marker.marker_path(
                pr_marker.KINDS["pr-review"], branch="feat/gate"
            )
            prr.parent.mkdir(parents=True, exist_ok=True)

            # (1) Header-less pr-review over the byte floor -> gate must BLOCK.
            prr.write_text("pr description review synthesis. " * 6, encoding="utf-8")
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "FEW-MODELS" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (2) Two distinct models -> still BLOCK.
            prr.write_text(
                "<!-- reviewed-by-models: a, b -->\n"
                + "pr description review synthesis. " * 6,
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "FEW-MODELS" in out, out

            # (3) Three distinct models -> still BLOCK until the tests marker exists.
            assert (
                pr_marker.main(["write", "pr-review", str(body), "--models", "a,b,c"])
                == 0
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (3b) A forged tests marker over the byte floor and correctly pinned to
            # HEAD, but WITHOUT the machine-produced result header, must still BLOCK
            # with NO-RESULT (the gate keys on the header, not size + pin).
            tests_path = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/gate"
            )
            head = pr_marker.current_head()
            tests_path.parent.mkdir(parents=True, exist_ok=True)
            tests_path.write_text(
                f"{pr_marker.REVIEWED_COMMIT_PREFIX}{head}"
                f"{pr_marker.REVIEWED_COMMIT_SUFFIX}\n" + ("z" * 300) + "\n",
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "NO-RESULT" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (3c) The result header embedded MID-LINE (not on its own line) must
            # also BLOCK: gh-guard's grep -qxF matches whole lines only, agreeing
            # with pr-marker's exact-line membership test (no substring bypass).
            tests_path.write_text(
                f"{pr_marker.REVIEWED_COMMIT_PREFIX}{head}"
                f"{pr_marker.REVIEWED_COMMIT_SUFFIX}\n"
                f"prefix {pr_marker.TESTS_RESULT_HEADER} suffix\n" + ("z" * 300) + "\n",
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "NO-RESULT" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (4) Machine-produced tests marker present -> PASS (execs the fake gh).
            assert pr_marker.main(["run-tests", "--cmd", "true"]) == 0
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 0, out
            assert "FAKE-GH-EXECUTED" in res.stdout, out
    finally:
        os.chdir(restore)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True)


def main() -> int:
    """Run every test and report a pass/fail summary."""
    # test_* helpers resolve the marker dir via git, so run from the repo root
    # regardless of the caller's cwd (allows `python3 /abs/path/bin/test...` from
    # anywhere).
    os.chdir(Path(__file__).resolve().parents[1])
    tests = [
        test_encoding_matches_sed,
        test_known_encodings,
        test_kinds_and_thresholds,
        test_branch_dir_and_paths,
        test_artifacts_dir_derivation,
        test_gh_guard_matches_kinds,
        test_pin_roundtrip,
        test_run_tests,
        test_test_quality_preflight,
        test_run_tests_requires_clean_repo,
        test_run_tests_rejects_git_state_changes,
        test_tests_marker_is_machine_only,
        test_parse_models,
        test_models_provenance,
        test_convergence_parsing_parity,
        test_code_review_marker_rewrite,
        test_models_argv_order,
        test_gh_guard_gate,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
    if failures:
        print(f"\n{failures} test(s) failed", file=sys.stderr)
        return 1
    print(f"\nall {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
