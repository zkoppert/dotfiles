#!/usr/bin/env python3
"""Scaffold and verify the demo artifacts + demo marker for the record-demo skill.

Thin wrapper around `pr-marker` (the single source of truth for marker paths) so
this skill never re-derives the per-branch path encoding. Three subcommands:

  init          create the per-branch demo artifacts directory and print a
                fill-in demo-marker template on stdout (redirect it to a file,
                edit, then `pr-marker write demo <file>`).
  na REASON     write an "N/A - no visual surface" demo marker directly, with a
                required alternative-visual-aid line (--alt).
  check         verify the demo marker exists and, for a visual demo, that the
                artifacts directory holds at least one non-empty image (and warn
                when the preferred before/after video is missing); for an N/A
                marker, that it records an alternative visual aid.

Run from inside the git checkout whose branch you are demoing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEO_EXTS = {".webm", ".mp4", ".mov", ".gif"}

# Matches pr-marker's demo-kind floor. Duplicated here only so the N/A path can
# report a demo-appropriate message instead of pr-marker's image-oriented one.
DEMO_MIN_BYTES = 120


def find_pr_marker() -> str:
    """Locate the pr-marker executable: PATH, then repo-relative, then ~/repos."""
    on_path = shutil.which("pr-marker")
    if on_path:
        return on_path
    # Resolve symlinks so a symlinked skill still finds the sibling bin/ dir.
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[3]
        / "bin"
        / "pr-marker",  # dotfiles/bin from .copilot/skills/record-demo/
        Path.home() / "repos" / "dotfiles" / "bin" / "pr-marker",
    ):
        if candidate.is_file():
            return str(candidate)
    raise SystemExit(
        "scaffold: cannot find pr-marker (not on PATH and not in dotfiles/bin). "
        "Run install.sh or add ~/.local/bin to PATH."
    )


def pr_marker(*args: str, stdin: str | None = None, check: bool = True) -> str:
    """Invoke pr-marker and return stripped stdout (raising on failure if check)."""
    result = subprocess.run(
        [sys.executable, find_pr_marker(), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def demo_paths() -> tuple[Path, Path]:
    """Return (marker_path, artifacts_dir) for the demo kind on the current branch."""
    marker = Path(pr_marker("path", "demo"))
    artifacts = Path(pr_marker("artifacts-dir", "demo"))
    return marker, artifacts


TEMPLATE = """# Demo: {branch}

## Summary

<one or two sentences on what the change does and what the demo shows>

## Before / after

| View | Before | After |
| --- | --- | --- |
| <view name> | ![before]({rel}/before-<view>.png) | ![after]({rel}/after-<view>.png) |

## Walkthrough

<preferred: a before/after screen recording at {rel}/demo.webm; if you did not record one, state why here>

## Impact

<one concrete number: latency, users affected, requests covered, time saved>
"""


def current_branch() -> str:
    """Return the current branch name for display in the template title."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "this branch"


def cmd_init(_args: argparse.Namespace) -> int:
    """Create the demo artifacts dir and print a fill-in marker template."""
    marker, artifacts = demo_paths()
    artifacts.mkdir(parents=True, exist_ok=True)
    rel = artifacts.name
    template = TEMPLATE.format(branch=current_branch(), rel=rel)

    sys.stderr.write(f"scaffold: artifacts dir ready at {artifacts}\n")
    sys.stderr.write(f"scaffold: demo marker path is {marker}\n")
    sys.stderr.write(
        "scaffold: record a before/after demo video (demo.webm, preferred) and save it "
        "with the before-*/after-* stills into the\n         artifacts dir, then edit and "
        "write the marker:\n"
        "           python3 scaffold.py init > /tmp/demo-marker.md\n"
        "           # edit /tmp/demo-marker.md\n"
        "           pr-marker write demo /tmp/demo-marker.md\n"
    )
    sys.stdout.write(template)
    return 0


def cmd_na(args: argparse.Namespace) -> int:
    """Write an N/A demo marker (no visual surface) with a required alternative aid."""
    content = (
        "# Demo: N/A - no visual surface\n\n"
        f"{args.reason.strip()}\n\n"
        f"Alternative visual aid: {args.alt.strip()}\n"
    )
    if len(content.encode("utf-8")) < DEMO_MIN_BYTES:
        sys.stderr.write(
            f"scaffold na: the N/A justification is too short (needs >= {DEMO_MIN_BYTES} "
            "bytes). Expand the reason and --alt with specifics: what changed, why there "
            "is no visual surface, and exactly which alternative aid (table, mermaid, "
            "terminal output) is in the PR body.\n"
        )
        return 1
    pr_marker("write", "demo", "-", stdin=content)
    return 0


def _nonempty_images(artifacts: Path) -> list[Path]:
    """Return the non-empty image files in the artifacts dir, sorted by name."""
    if not artifacts.is_dir():
        return []
    return [
        p
        for p in sorted(artifacts.iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0
    ]


def _has_video(artifacts: Path) -> bool:
    """Return True if the artifacts dir holds a non-empty video file."""
    if not artifacts.is_dir():
        return False
    return any(
        p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.stat().st_size > 0
        for p in artifacts.iterdir()
    )


def _matching_image_pairs(images: list[Path]) -> list[str]:
    """Return view names that have non-empty before and after images."""
    before = {
        path.stem.removeprefix("before-")
        for path in images
        if path.stem.startswith("before-")
    }
    after = {
        path.stem.removeprefix("after-")
        for path in images
        if path.stem.startswith("after-")
    }
    return sorted(before & after)


def cmd_check(_args: argparse.Namespace) -> int:
    """Verify the demo marker exists and has images (visual) or an alt aid (N/A)."""
    marker, artifacts = demo_paths()
    if not marker.is_file():
        sys.stderr.write(
            f"scaffold check: demo marker missing at {marker}. "
            "Run `scaffold.py init` (visual) or `scaffold.py na` (no surface).\n"
        )
        return 1

    text = marker.read_text(encoding="utf-8")
    lowered = text.lower()
    # Anchor the N/A classification to the title line that `cmd_na` writes, not a
    # substring scan of the whole body (a visual demo whose prose contains "n/a",
    # or whose before/after table contains "|", must not be treated as N/A).
    is_na = text.lstrip().lower().startswith("# demo: n/a")

    if is_na:
        has_alt = (
            "alternative visual aid" in lowered or "|" in text or "mermaid" in lowered
        )
        if not has_alt:
            sys.stderr.write(
                "scaffold check: N/A demo marker does not record an alternative "
                "visual aid (table, mermaid, or terminal output). Add one.\n"
            )
            return 1
        print(
            f"scaffold check: ok (N/A demo marker with an alternative visual aid) -> {marker}"
        )
        return 0

    images = _nonempty_images(artifacts)
    pairs = _matching_image_pairs(images)
    if not pairs:
        sys.stderr.write(
            f"scaffold check: no matched non-empty before-*/after-* image pair in "
            f"{artifacts}. Capture both sides of at least one view, or record an N/A "
            "marker if there is no surface.\n"
        )
        return 1

    print(
        f"scaffold check: ok ({len(pairs)} matched before/after pair(s) in "
        f"{artifacts.name})"
    )
    for view in pairs:
        print(f"  - {view}")
    print(f"scaffold check: found {len(images)} non-empty image(s)")
    for img in images:
        print(f"  - {img.name} ({img.stat().st_size} bytes)")
    if _has_video(artifacts):
        print("scaffold check: ok (before/after video walkthrough present)")
    else:
        sys.stderr.write(
            "scaffold check: WARNING - no video walkthrough recorded. A before/after "
            "video is the preferred demo deliverable; record one unless there is a "
            "specific reason a recording is not feasible.\n"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the scaffold subcommands."""
    parser = argparse.ArgumentParser(
        prog="scaffold.py",
        description="Scaffold/verify the demo artifacts and demo marker for record-demo.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init", help="create artifacts dir and print a marker template"
    )
    p_init.set_defaults(func=cmd_init)

    p_na = sub.add_parser("na", help="write an N/A demo marker (no visual surface)")
    p_na.add_argument("reason", help="why there is no visual surface to demo")
    p_na.add_argument(
        "--alt",
        required=True,
        help="the alternative visual aid included in the PR body (table, mermaid, terminal output)",
    )
    p_na.set_defaults(func=cmd_na)

    p_check = sub.add_parser("check", help="verify the demo marker and artifacts")
    p_check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the selected subcommand."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
