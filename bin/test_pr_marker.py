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
    for name in ("plan", "demo", "pr-review"):
        assert pr_marker.KINDS[name].filename == f"{name}.md"
        assert pr_marker.KINDS[name].min_bytes == 120
    # The three review kinds require a multi-model review; demo is exempt.
    for name in ("code-review", "plan", "pr-review"):
        assert pr_marker.KINDS[name].requires_models, name
    assert not pr_marker.KINDS["demo"].requires_models


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
    """gh-guard's floors, filenames, pin flags, and model rule must equal KINDS.

    gh-guard duplicates the byte floors, marker filenames, pin flags, and the
    requires-models flag for resilience (so the gate works without pr-marker).
    This asserts the two never drift, which is the real risk the "single source
    of truth" claim rests on.
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
        # with the pin flag then the requires-models flag (each 1 or 0).
        line = next(
            ln
            for ln in gh.splitlines()
            if "eval_marker" in ln and f'"$marker_base/{kind.filename}"' in ln
        )
        assert f'"${const}"' in line, f"{kind.name} eval_marker uses wrong floor"
        fields = line.split()
        assert fields[-2] == str(int(kind.pinned)), f"{kind.name} pin flag drift"
        assert fields[-1] == str(
            int(kind.requires_models)
        ), f"{kind.name} requires-models flag drift"

    # The minimum-model threshold must agree between the two implementations.
    match = re.search(r"^MIN_REVIEW_MODELS=(\d+)$", gh, re.MULTILINE)
    assert match, "gh-guard missing constant MIN_REVIEW_MODELS"
    assert int(match.group(1)) == pr_marker.MIN_MODELS, "MIN_MODELS drift"


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
                    ]
                )
                == 0
            )

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


def test_models_argv_order() -> None:
    """--models parses in either position, incl. the `--models a,b,c -` form."""
    # Optional-before-positional (the natural / gh-guard-suggested order).
    assert pr_marker._reorder_write_argv(
        ["write", "pr-review", "--models", "a,b,c", "-"]
    ) == ["write", "pr-review", "-", "--models", "a,b,c"]
    # --models=VALUE form, also before the positional.
    assert pr_marker._reorder_write_argv(
        ["write", "code-review", "--models=a,b", "body.md"]
    ) == ["write", "code-review", "body.md", "--models=a,b"]
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
                    ["write", "code-review", str(crbody), "--models", "a,b,c"]
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

            # (3) Three distinct models -> PASS (execs the fake gh).
            assert (
                pr_marker.main(["write", "pr-review", str(body), "--models", "a,b,c"])
                == 0
            )
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
        test_parse_models,
        test_models_provenance,
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
