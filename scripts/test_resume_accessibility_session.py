"""Tests for resume_accessibility_session."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import resume_accessibility_session as resume


def write_state(state_dir: Path, state: dict[str, object]) -> None:
    """Write representative picker state."""
    state_dir.mkdir(exist_ok=True)
    (state_dir / "issue-42.json").write_text(json.dumps(state), encoding="utf-8")


def test_load_resume_state_and_build_command(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    state_dir = tmp_path / "state"
    session_id = "e6877215-fb9f-432e-acd8-7c06a902d3a5"
    copilot_home = state_dir / "copilot-homes" / session_id
    copilot_home.mkdir(parents=True)
    (copilot_home / "settings.json").write_text("{}\n", encoding="utf-8")
    config = tmp_path / "picker.env"
    config.write_text(
        "ACCESSIBILITY_GITHUB_TOKEN=repository-scoped-token\n",
        encoding="utf-8",
    )
    write_state(
        state_dir,
        {"session_id": session_id, "workdir": str(workdir)},
    )

    with mock.patch.dict(
        resume.os.environ,
        {"ACCESSIBILITY_PICKER_CONFIG": str(config)},
    ):
        state = resume.load_resume_state(state_dir, 42)

    command = resume.resume_command(state)
    assert f"COPILOT_HOME={copilot_home}" in command
    assert f"HOME={copilot_home / 'user-home'}" in command
    assert f". {config}" in command
    assert 'unset token; token="$ACCESSIBILITY_GITHUB_TOKEN"' in command
    assert 'COPILOT_GITHUB_TOKEN="$token"' in command
    assert "gh auth token" not in command
    assert "repository-scoped-token" not in command
    assert f"copilot --experimental -C {state['runner']}" in command
    assert f"--session-id {session_id}" in command
    assert "--allow-all-paths" in command
    assert state["runner"].parent == copilot_home / "runners"


def test_load_resume_state_rejects_invalid_session_id(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_state(
        state_dir,
        {"session_id": "$(touch /tmp/injected)", "workdir": str(tmp_path)},
    )

    with pytest.raises(resume.ResumeError, match="valid Copilot session ID"):
        resume.load_resume_state(state_dir, 42)


def test_open_iterm_inserts_without_executing() -> None:
    with mock.patch.object(resume.subprocess, "run") as run:
        resume.open_iterm("copilot --resume=abc")

    command = run.call_args.args[0]
    assert command[-1] == "copilot --resume=abc"
    assert "newline NO" in command[2]
