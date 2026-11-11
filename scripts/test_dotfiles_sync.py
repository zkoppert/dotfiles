"""Exercise daily updates and additive skill installation with real Git repositories."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dotfiles_sync as sync

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): (os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
        for path in root.rglob("*")
    }


class DotfilesSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "PI_CODING_AGENT_DIR": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "checkout"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "init", "--initial-branch=main", str(self.seed))
        (self.seed / ".github").mkdir()
        (self.seed / ".github/copilot-instructions.md").write_text("# Fixture instructions\n")
        (self.seed / ".gitignore").write_text("collision.txt\n")
        self.add_skill("example")
        self.publish()
        self.git(self.root, "clone", str(self.remote), str(self.repo))
        for repo in (self.seed, self.repo):
            self.git(repo, "config", "core.hooksPath", os.devnull)

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", *args],
            cwd=cwd, text=True, capture_output=True, check=True,
        ).stdout.strip()

    def add_skill(self, name: str, *, declared_name: str = "") -> None:
        directory = self.seed / ".copilot/skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {declared_name or name}\ndescription: Use when checking fixture skills.\n---\n# Fixture\n"
        )

    def publish(self) -> None:
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Fixture update")
        self.git(self.seed, "push", str(self.remote), "main")

    def run_sync(self, *, check: bool = False) -> dict:
        return sync.synchronize(self.repo, self.home, "main", check=check)

    def test_fast_forward_links_new_skills_and_preserves_existing_links(self) -> None:
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.add_skill("second")
        self.publish()
        result = self.run_sync()
        self.assertNotEqual(before, self.git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.git(self.seed, "rev-parse", "HEAD"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["skills"], 2)
        self.assertEqual(len(result["created"]), 6)
        for root in (self.home / ".copilot/skills", self.home / ".agents/skills"):
            for name in ("example", "second"):
                self.assertEqual((root / name).resolve(), self.repo / ".copilot/skills" / name)
        again = self.run_sync()
        self.assertEqual(again["created"], [])
        self.assertEqual(result["head_after"], again["head_after"])

    def test_shared_fetch_head_cannot_select_an_unmerged_branch(self) -> None:
        self.add_skill("main-skill")
        self.publish()
        expected = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "checkout", "-b", "feature")
        self.add_skill("unmerged")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Unmerged feature")
        self.git(self.seed, "push", str(self.remote), "feature")
        original = sync.git

        def interleave_fetch(repository, *arguments):
            output = original(repository, *arguments)
            if arguments[0] == "fetch":
                self.git(repository, "fetch", "origin", "feature")
            return output

        with patch.object(sync, "git", side_effect=interleave_fetch):
            self.run_sync()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), expected)
        self.assertFalse((self.home / ".agents/skills/unmerged").exists())

    def test_branch_switch_during_fetch_is_detected_before_merge(self) -> None:
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.add_skill("new-main-skill")
        self.publish()
        original = sync.git

        def switch_during_fetch(repository, *arguments):
            output = original(repository, *arguments)
            if arguments[0] == "fetch":
                self.git(repository, "checkout", "-b", "work")
            return output

        with patch.object(sync, "git", side_effect=switch_during_fetch), self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "work")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertFalse((self.repo / ".copilot/skills/new-main-skill").exists())

    def test_dirty_wrong_branch_and_detached_checkouts_are_preserved(self) -> None:
        cases = ("tracked", "staged", "untracked", "branch", "detached")
        for case in cases:
            with self.subTest(case=case):
                self.git(self.repo, "reset", "--hard", "origin/main")
                self.git(self.repo, "checkout", "main")
                extra = self.repo / "new.txt"
                extra.unlink(missing_ok=True)
                if case in ("tracked", "staged"):
                    (self.repo / ".gitignore").write_text("keep this local edit\n")
                    if case == "staged":
                        self.git(self.repo, "add", ".gitignore")
                elif case == "untracked":
                    extra.write_text("keep this local file")
                elif case == "branch":
                    self.git(self.repo, "checkout", "-b", "work")
                else:
                    self.git(self.repo, "checkout", "--detach")
                head = self.git(self.repo, "rev-parse", "HEAD")
                files = snapshot(self.repo)
                with self.assertRaises(sync.SyncError):
                    self.run_sync()
                self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), head)
                self.assertEqual(snapshot(self.repo), files)
                self.assertFalse((self.home / ".copilot").exists())

    def test_divergence_never_rebases_or_stashes(self) -> None:
        (self.repo / "local.txt").write_text("local")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "Local work")
        self.add_skill("second")
        self.publish()
        self.git(self.repo, "config", "pull.rebase", "true")
        self.git(self.repo, "config", "rebase.autoStash", "true")
        self.git(self.repo, "config", "merge.autoStash", "true")
        head = self.git(self.repo, "rev-parse", "HEAD")
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.repo, "stash", "list"), "")
        self.assertEqual((self.repo / "local.txt").read_text(), "local")
        self.assertFalse((self.home / ".copilot").exists())

    def test_ignored_user_file_cannot_be_overwritten_by_the_update(self) -> None:
        collision = self.repo / "collision.txt"
        collision.write_text("private local content")
        (self.seed / "collision.txt").write_text("upstream content")
        self.git(self.seed, "add", "--force", "collision.txt")
        self.git(self.seed, "commit", "-m", "Add tracked collision")
        self.git(self.seed, "push", str(self.remote), "main")
        before = self.git(self.repo, "rev-parse", "HEAD")
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(collision.read_text(), "private local content")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)

    def test_only_tracked_manifests_are_installed(self) -> None:
        (self.repo / ".git/info/exclude").write_text(".copilot/skills/ignored/\n")
        ignored = self.repo / ".copilot/skills/ignored"
        ignored.mkdir()
        (ignored / "SKILL.md").write_text("not a repository-owned skill")
        result = self.run_sync()
        self.assertEqual(result["skills"], 1)
        self.assertFalse((self.home / ".copilot/skills/ignored").exists())

    def test_existing_files_directories_and_wrong_links_are_never_replaced(self) -> None:
        for kind in ("file", "directory", "wrong-link", "broken-link"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                home = Path(temporary)
                target = home / ".copilot/skills/example"
                target.parent.mkdir(parents=True)
                if kind == "file":
                    target.write_text("keep this file")
                elif kind == "directory":
                    target.mkdir()
                    (target / "SKILL.md").write_text("keep my custom skill")
                else:
                    destination = home / "custom"
                    if kind == "wrong-link":
                        destination.mkdir()
                    target.symlink_to(destination, target_is_directory=True)
                before = snapshot(home)
                with self.assertRaises(sync.SyncError):
                    sync.synchronize(self.repo, home, "main")
                self.assertEqual(snapshot(home), before)

    def test_existing_indirect_instruction_link_is_accepted(self) -> None:
        self.run_sync()
        target = self.home / ".pi/agent/AGENTS.md"
        target.unlink()
        target.symlink_to(self.home / ".copilot/copilot-instructions.md")
        raw_target = os.readlink(target)
        self.assertEqual(self.run_sync()["created"], [])
        self.assertEqual(os.readlink(target), raw_target)

    def test_invalid_metadata_blocks_all_new_links(self) -> None:
        self.add_skill("invalid", declared_name="wrong")
        self.publish()
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertFalse((self.home / ".copilot").exists())
        self.assertFalse((self.home / ".agents").exists())

    def test_missing_tracked_instruction_source_blocks_new_links(self) -> None:
        self.git(self.seed, "rm", ".github/copilot-instructions.md")
        self.git(self.seed, "commit", "-m", "Remove instruction source")
        self.git(self.seed, "push", str(self.remote), "main")
        with self.assertRaisesRegex(sync.SyncError, "instruction source"):
            self.run_sync()
        self.assertFalse((self.home / ".copilot").exists())

    def test_unreachable_remote_preserves_head_and_leaves_links_unchanged(self) -> None:
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.remote.rename(self.root / "offline.git")
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertFalse((self.home / ".copilot").exists())

    def test_custom_pi_agent_directory_is_honored(self) -> None:
        agent_dir = self.home / "custom agent"
        result = sync.synchronize(self.repo, self.home, "main", pi_agent_dir=agent_dir)
        self.assertEqual(result["status"], "ok")
        self.assertEqual((agent_dir / "AGENTS.md").resolve(), self.repo / ".github/copilot-instructions.md")
        self.assertFalse((self.home / ".pi").exists())

    def test_file_appearing_during_link_creation_is_preserved(self) -> None:
        target = self.home / ".copilot/skills/example"
        original = Path.symlink_to

        def create_race(path, source, **kwargs):
            if path == target:
                path.write_text("new user file")
            return original(path, source, **kwargs)

        with patch.object(Path, "symlink_to", create_race), self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(target.read_text(), "new user file")
        self.assertFalse(target.is_symlink())

    def test_check_does_not_fetch_pull_create_links_or_write_target_files(self) -> None:
        self.add_skill("second")
        self.publish()
        before_repo, before_home = snapshot(self.repo), snapshot(self.home)
        result = self.run_sync(check=True)
        self.assertEqual(result["status"], "attention_required")
        self.assertFalse(result["remote_checked"])
        self.assertEqual(len(result["missing"]), 4)
        self.assertEqual(snapshot(self.repo), before_repo)
        self.assertEqual(snapshot(self.home), before_home)

    def test_in_progress_operations_are_refused(self) -> None:
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "index.lock", "rebase-merge", "rebase-apply"):
            with self.subTest(name=name):
                path = self.repo / ".git" / name
                path.write_text("pending operation")
                with self.assertRaises(sync.SyncError):
                    self.run_sync()
                self.assertEqual(path.read_text(), "pending operation")
                path.unlink()

    def test_lock_excludes_a_second_run_without_removing_the_lock_file(self) -> None:
        lock_path = self.home / "Library/Caches/dotfiles-sync.lock"
        with sync.run_lock(self.home) as acquired:
            self.assertTrue(acquired)
            inode = lock_path.stat().st_ino
            with sync.run_lock(self.home) as again:
                self.assertFalse(again)
        self.assertEqual(lock_path.stat().st_ino, inode)
        with sync.run_lock(self.home) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(lock_path.stat().st_ino, inode)

    def test_real_cli_check_reports_json_and_preserves_missing_links(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "scripts.dotfiles_sync", "--repository", str(self.repo), "--home", str(self.home), "--branch", "main", "--check"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "attention_required")
        self.assertEqual(len(payload["missing"]), 4)
        self.assertFalse((self.home / ".copilot").exists())

    def test_successful_command_does_not_stop_its_background_helper(self) -> None:
        started, completed = self.root / "helper-started", self.root / "helper-completed"
        child = f"import time; from pathlib import Path; Path({str(started)!r}).touch(); time.sleep(1); Path({str(completed)!r}).touch()"
        parent = ("import subprocess,sys,time; from pathlib import Path; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                  f"\nwhile not Path({str(started)!r}).exists(): time.sleep(0.01)\nprint('done')")
        output = sync.run_command([sys.executable, "-c", parent], self.repo, timeout=5)
        self.assertEqual(output, "done\n")
        deadline = time.monotonic() + 5
        while not completed.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(completed.exists(), "successful commands must not kill persistent helpers")

    def test_subprocess_timeout_stops_the_process_group(self) -> None:
        started, completed = self.root / "started", self.root / "completed"
        code = f"import time; from pathlib import Path; Path({str(started)!r}).touch(); time.sleep(5); Path({str(completed)!r}).touch()"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{code!r}]); time.sleep(10)"
        with self.assertRaises(sync.SyncError):
            sync.run_command([sys.executable, "-c", parent], self.repo, timeout=3)
        self.assertTrue(started.exists())
        time.sleep(5.1)
        self.assertFalse(completed.exists())

    def test_actual_plist_reuses_the_existing_daily_label(self) -> None:
        plist = plistlib.loads((ROOT / "LaunchAgents/com.zkoppert.dotfiles-sync.plist").read_bytes())
        self.assertEqual(plist["Label"], "com.zkoppert.dotfiles-sync")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["WorkingDirectory"], "/Users/zkoppert/repos/dotfiles")
        arguments = plist["ProgramArguments"]
        self.assertIn("scripts.dotfiles_sync", arguments)
        for flag, expected in (
            ("--repository", plist["WorkingDirectory"]),
            ("--home", plist["EnvironmentVariables"]["HOME"]),
            ("--pi-agent-dir", "/Users/zkoppert/.pi/agent"),
        ):
            self.assertEqual(arguments[arguments.index(flag) + 1], expected)
        self.assertIn("-B", arguments)
        self.assertIn("/opt/homebrew/bin", plist["EnvironmentVariables"]["PATH"])
        self.assertNotIn("-c", plist["ProgramArguments"])


if __name__ == "__main__":
    unittest.main()
