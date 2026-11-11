"""Fast-forward a clean dotfiles checkout and add missing agent skill links."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Iterator, Optional

from scripts.skill_metadata import skill_metadata_errors


class SyncError(RuntimeError):
    """An update or link check could not complete safely."""


def run_command(command: list[str], repository: Path, *, timeout: float = 120) -> str:
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    with subprocess.Popen(
        command, cwd=repository, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    ) as process:
        communication_complete = False
        try:
            output, _ = process.communicate(timeout=timeout)
            communication_complete = True
        except subprocess.TimeoutExpired as error:
            raise SyncError("command timed out; inspect the checkout before retrying") from error
        finally:
            if not communication_complete:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if process.returncode:
            raise SyncError(f"{Path(command[0]).name} {command[1]} failed (exit {process.returncode})")
    return output


def git(repository: Path, *arguments: str) -> str:
    return run_command(["git", *arguments], repository).strip()


def require_clean_branch(repository: Path, branch: str) -> str:
    root = Path(git(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository:
        raise SyncError("repository must be the checkout root")
    current = git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current != branch:
        raise SyncError(f"expected branch {branch}; checkout remains on {current}")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock"):
        path = Path(git(repository, "rev-parse", "--git-path", marker))
        if not path.is_absolute():
            path = repository / path
        if path.exists():
            raise SyncError(f"Git operation is in progress ({marker}); checkout was not updated")
    if git(repository, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        raise SyncError("checkout has local changes; nothing was stashed or discarded")
    return git(repository, "rev-parse", "HEAD")


def link_state(source: Path, target: Path) -> str:
    if target.is_symlink():
        try:
            return "ok" if target.resolve(strict=True) == source.resolve(strict=True) else "conflict"
        except (OSError, RuntimeError):
            return "conflict"
    return "conflict" if target.exists() else "missing"


def synchronize(
    repository: Path, home: Path, branch: str, *, check: bool = False,
    pi_agent_dir: Optional[Path] = None,
) -> dict:
    repository = repository.expanduser().resolve(strict=True)
    home = home.expanduser().resolve(strict=True)
    agent_dir = pi_agent_dir or Path(os.environ.get("PI_CODING_AGENT_DIR") or str(home / ".pi/agent"))
    agent_dir = agent_dir.expanduser()
    if not agent_dir.is_absolute():
        raise SyncError("Pi agent directory must be absolute")
    before = require_clean_branch(repository, branch)
    if not check:
        remote_ref = f"refs/remotes/origin/{branch}"
        git(repository, "fetch", "--no-recurse-submodules", "origin", f"refs/heads/{branch}:{remote_ref}")
        require_clean_branch(repository, branch)
        # FETCH_HEAD can be replaced by an unrelated fetch between these commands.
        git(repository, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", remote_ref)
        require_clean_branch(repository, branch)
    manifests = {}
    tracked_paths = git(repository, "ls-files", "-z", "--", ".copilot/skills", ".github/copilot-instructions.md").split("\0")
    for tracked in tracked_paths:
        relative = Path(tracked)
        if len(relative.parts) == 4 and relative.parts[-1] == "SKILL.md":
            manifests[repository / relative] = relative.parts[2]
    if not manifests:
        raise SyncError("no tracked skill manifests were found")
    errors = skill_metadata_errors(manifests)
    invalid = {path: messages for path, messages in errors.items() if messages}
    if invalid:
        names = sorted(manifests[Path(path)] for path in invalid)
        raise SyncError("invalid skill metadata; no new links created: " + ", ".join(names))
    instructions = repository / ".github/copilot-instructions.md"
    if ".github/copilot-instructions.md" not in tracked_paths or not instructions.is_file():
        raise SyncError("global instruction source is missing")
    links = [(path.parent, home / root / name)
             for path, name in sorted(manifests.items())
             for root in (".copilot/skills", ".agents/skills")]
    links.extend([
        (instructions, home / ".copilot/copilot-instructions.md"),
        (instructions, agent_dir / "AGENTS.md"),
    ])
    states = [(source, target, link_state(source, target)) for source, target in links]
    conflicts = [str(target) for _, target, state in states if state == "conflict"]
    if conflicts:
        raise SyncError("existing destinations were preserved; resolve link conflicts: " + ", ".join(conflicts))
    missing = [(source, target) for source, target, state in states if state == "missing"]
    created = []
    if not check:
        for source, target in missing:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.symlink_to(source, target_is_directory=source.is_dir())
            except FileExistsError:
                if link_state(source, target) != "ok":
                    raise SyncError(f"destination appeared during linking and was preserved: {target}")
            else:
                created.append(str(target))
    return {
        "status": "attention_required" if check and missing else "ok",
        "remote_checked": not check,
        "head_before": before,
        "head_after": git(repository, "rev-parse", "HEAD"),
        "skills": len(manifests),
        "missing": [str(target) for _, target in missing] if check else [],
        "created": created,
    }


@contextlib.contextmanager
def run_lock(home: Path) -> Iterator[bool]:
    directory = home / "Library/Caches"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "dotfiles-sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--pi-agent-dir", type=Path)
    parser.add_argument("--check", action="store_true", help="inspect local state without updating Git or creating links")
    args = parser.parse_args(argv)
    code = 0
    try:
        if args.check:
            result = synchronize(args.repository, args.home, args.branch, check=True, pi_agent_dir=args.pi_agent_dir)
        else:
            with run_lock(args.home.expanduser()) as acquired:
                result = synchronize(args.repository, args.home, args.branch, pi_agent_dir=args.pi_agent_dir) if acquired else {"status": "already_running"}
        if result["status"] == "attention_required":
            code = 2
    except (OSError, ValueError, SyncError) as error:
        result = {"status": "error", "error": str(error)}
        code = 1
    result["timestamp"] = dt.datetime.now(dt.timezone.utc).isoformat()
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
