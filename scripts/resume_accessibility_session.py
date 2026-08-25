#!/usr/bin/env python3
"""Open iTerm with a validated Copilot resume command ready to run."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
ITERM_SCRIPT = """
on run argv
  tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    tell current session of newWindow
      write text (item 1 of argv) newline NO
    end tell
  end tell
end run
"""


class ResumeError(RuntimeError):
    """Raised when saved resume state is invalid."""


def load_resume_state(state_dir: Path, issue_number: int) -> dict[str, Any]:
    """Load and validate the saved session information."""
    path = state_dir / f"issue-{issue_number}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise ResumeError(f"Unable to read {path}: {exc}") from exc
    session_id = state.get("session_id")
    workdir = state.get("workdir")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ResumeError(f"{path} does not contain a valid Copilot session ID")
    if not isinstance(workdir, str):
        raise ResumeError(f"{path} does not contain a working directory")
    resolved_workdir = Path(workdir).expanduser().resolve()
    if not resolved_workdir.is_dir():
        raise ResumeError(f"Saved working directory does not exist: {resolved_workdir}")
    copilot_home = (state_dir / "copilot-homes" / session_id).resolve()
    if not (copilot_home / "settings.json").is_file():
        raise ResumeError(f"Saved Copilot sandbox settings do not exist: {copilot_home}")
    config_file = Path(
        os.environ.get("ACCESSIBILITY_PICKER_CONFIG", "")
    ).expanduser().resolve()
    if not config_file.is_file():
        raise ResumeError(f"Accessibility picker config does not exist: {config_file}")
    runner_root = copilot_home / "runners"
    runner_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    runner_root.chmod(0o700)
    runner = runner_root / f"resume-{uuid.uuid4()}"
    runner.mkdir(mode=0o700)
    return {
        "session_id": session_id,
        "workdir": resolved_workdir,
        "copilot_home": copilot_home,
        "config_file": config_file,
        "runner": runner,
    }


def resume_command(state: dict[str, Any]) -> str:
    """Build the command that iTerm will display."""
    config_file = shlex.quote(str(state["config_file"]))
    copilot_command = shlex.join(
        [
            "copilot",
            "--experimental",
            "-C",
            str(state["runner"]),
            "--session-id",
            str(state["session_id"]),
            "--allow-all-paths",
            "--secret-env-vars",
            "COPILOT_GITHUB_TOKEN,GH_TOKEN,GITHUB_TOKEN",
        ]
    )
    script = (
        "( if ! /usr/bin/env python3 -c "
        "'import os, sys; sys.exit(0 if os.stat(sys.argv[1]).st_mode & 0o077 == 0 else 1)' "
        f"{config_file}; then printf "
        "'resume-accessibility-session: config file must have mode 0600: %s\\n' "
        f"{config_file} >&2; exit 1; fi; "
        "set -a; config_failed=0; trap 'config_failed=1' ERR; set +e; "
        f"{{ . {config_file}; config_status=$?; set +x; set +v; }} >/dev/null 2>&1; "
        "trap - ERR; set +a; "
        'if [ "$config_failed" -ne 0 ] || [ "$config_status" -ne 0 ]; then '
        "printf 'resume-accessibility-session: failed to load config file: %s\\n' "
        f"{config_file} >&2; exit 1; fi; "
        'unset token; token="$ACCESSIBILITY_GITHUB_TOKEN"; '
        "unset ACCESSIBILITY_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN; "
        f"HOME={shlex.quote(str(state['copilot_home'] / 'user-home'))} "
        f"COPILOT_HOME={shlex.quote(str(state['copilot_home']))} "
        'COPILOT_GITHUB_TOKEN="$token" '
        f"{copilot_command} )"
    )
    return shlex.join(["/bin/bash", "-c", script])


def open_iterm(command: str) -> None:
    """Open a new iTerm window and insert the command without executing it."""
    subprocess.run(
        ["osascript", "-e", ITERM_SCRIPT, command],
        check=True,
        capture_output=True,
        text=True,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""
    parser = argparse.ArgumentParser(
        description="Prepare an accessibility remediation session in iTerm."
    )
    parser.add_argument("issue_number", type=int)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/state/accessibility-issue-picker",
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print the validated resume command without opening iTerm.",
    )
    return parser


def main() -> int:
    """CLI entry point."""
    args = build_parser().parse_args()
    try:
        command = resume_command(load_resume_state(args.state_dir, args.issue_number))
        if args.print_command:
            print(command)
        else:
            open_iterm(command)
    except (ResumeError, subprocess.CalledProcessError) as exc:
        print(f"resume-accessibility-session: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
