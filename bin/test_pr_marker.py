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
    """The kind filenames and byte floors match what gh-guard checks."""
    assert pr_marker.KINDS["code-review"].filename == "code-review.md"
    assert pr_marker.KINDS["code-review"].min_bytes == 200
    for name in ("plan", "demo", "pr-review"):
        assert pr_marker.KINDS[name].filename == f"{name}.md"
        assert pr_marker.KINDS[name].min_bytes == 120


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
    """gh-guard's floors, filenames, and pin flags must equal pr-marker.KINDS.

    gh-guard duplicates the byte floors, marker filenames, and pin flags for
    resilience (so the gate works without pr-marker). This asserts the two never
    drift, which is the real risk the "single source of truth" claim rests on.
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
        # with the correct pin flag (1 for pinned, 0 otherwise).
        line = next(
            ln
            for ln in gh.splitlines()
            if "eval_marker" in ln and f'"$marker_base/{kind.filename}"' in ln
        )
        assert f'"${const}"' in line, f"{kind.name} eval_marker uses wrong floor"
        assert line.split()[-1] == str(int(kind.pinned)), f"{kind.name} pin flag drift"


def test_pin_roundtrip() -> None:
    """A pinned marker records HEAD and goes stale once HEAD advances."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "checkout", "-q", "-b", "feat/pin")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            code = pr_marker.KINDS["code-review"]
            content = "code review synthesis. " * 12  # > 200 bytes
            payload = Path(tmp) / "body.md"
            payload.write_text(content, encoding="utf-8")
            assert pr_marker.main(["write", "code-review", str(payload)]) == 0

            head1 = pr_marker.current_head()
            marker = pr_marker.marker_path(code, branch="feat/pin")
            assert pr_marker.read_reviewed_commit(marker) == head1
            ok, detail, _size, _path = pr_marker.marker_status(code, "feat/pin")
            assert ok and detail == "ok"

            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")
            ok, detail, _size, _path = pr_marker.marker_status(code, "feat/pin")
            assert not ok and detail == "stale", detail
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
