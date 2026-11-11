#!/usr/bin/env python3
"""Resolve private repository assignments without repo-local policy overrides."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

PROFILES = {
    "relaxed": {"markers": [], "tests": "optional", "review": "optional"},
    "standard": {
        "markers": ["code-review", "tests"],
        "tests": "local",
        "review": "self-review",
    },
    "strict": {
        "markers": ["plan", "code-review", "demo", "pr-review", "tests"],
        "tests": "full Codespace checks",
        "review": "three-model panels",
    },
}
RANK = {name: index for index, name in enumerate(PROFILES)}
IDENTITY = re.compile(r"[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9-]*/[a-z0-9_.-]+\Z")


@dataclass(frozen=True)
class Policy:
    profile: str = "strict"
    reason: str = "unconfigured or unresolved repository"
    repository: str = ""

    @property
    def markers(self) -> list[str]:
        return PROFILES[self.profile]["markers"]


def repository_id(raw: str) -> str:
    """Keep hosts distinct and normalize Git and gh repository selectors."""
    value = raw.lower()
    if value != value.strip() or any(char.isspace() for char in value):
        return ""
    if "://" in value:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http", "ssh", "git"}:
                return ""
            host = parsed.hostname or ""
            port = parsed.port
            if parsed.query or parsed.fragment:
                return ""
            if host == "ssh.github.com" and parsed.scheme == "ssh" and port == 443:
                host = "github.com"
            elif port and port not in {22, 80, 443}:
                return ""
            value = host + parsed.path
        except ValueError:
            return ""
    elif re.fullmatch(r"[^/@:]+@[^/:]+:.+", value):
        host, path = value.split("@", 1)[1].split(":", 1)
        value = host + "/" + path
    elif value.count("/") == 1:
        value = "github.com/" + value
    if value.endswith(".git"):
        value = value[:-4]
    if not IDENTITY.fullmatch(value) or value.split("/")[-1] in {".", ".."}:
        return ""
    return value


def config_path() -> Path:
    return Path.home() / ".config/repo-guards/config.yaml"


def load_assignments() -> tuple[dict[str, str], bool]:
    """Accept only the documented scalar-mapping YAML subset, without dependencies."""
    path = config_path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, True
    except OSError:
        return {}, False
    try:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            return {}, False
        text = path.read_text(encoding="utf-8")
        entries: dict[str, str] = {}
        headers: list[str] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].rstrip(" ")
            if not line:
                continue
            if not headers and line == "version: 1":
                headers.append("version")
            elif headers == ["version"] and line == "repositories:":
                headers.append("repositories")
            elif headers == ["version", "repositories"]:
                match = re.fullmatch(
                    r"  ([A-Za-z0-9_./-]+): (relaxed|standard|strict)", line
                )
                if not match:
                    return {}, False
                key, profile = match.groups()
                identity = repository_id(key)
                if not identity or identity in entries or key.lower().endswith(".git"):
                    return {}, False
                entries[identity] = profile
            else:
                return {}, False
        return (
            (entries, True) if headers == ["version", "repositories"] else ({}, False)
        )
    except (OSError, UnicodeError):
        return {}, False


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def checkout_identities() -> list[str]:
    identities = []
    for remote in git("remote").splitlines():
        urls = git("remote", "get-url", "--all", remote).splitlines()
        for url in urls:
            identity = repository_id(url)
            if identity:
                identities.append(identity)
            elif remote == "origin" or "://" in url or "@" in url:
                identities.append("")
    return identities


def resolve_policy(target: Optional[str] = None) -> Policy:
    assignments, valid = load_assignments()
    if not valid:
        return Policy(reason="invalid private configuration")
    identities = checkout_identities()
    if target is not None:
        identities.append(repository_id(target))
    if not identities:
        return Policy()
    profiles = [assignments.get(identity, "strict") for identity in identities]
    profile = max(profiles, key=RANK.__getitem__)
    return Policy(
        profile,
        "private assignment" if profile != "strict" else "strict default or assignment",
    )


# Values must be consumed before interpreting a later token as a repository flag.
GH_VALUE_OPTIONS = {
    "-R",
    "--repo",
    "-B",
    "--base",
    "-H",
    "--head",
    "-a",
    "--assignee",
    "--attach",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-l",
    "--label",
    "-m",
    "--milestone",
    "-p",
    "--project",
    "--recover",
    "-r",
    "--reviewer",
    "-T",
    "--template",
    "-t",
    "--title",
    "--hostname",
    "--token",
}
GH_BOOLEAN_OPTIONS = {
    "--draft",
    "-d",
    "--dry-run",
    "--fill",
    "--fill-first",
    "--fill-verbose",
    "--web",
    "-w",
    "--help",
    "-h",
}


def gh_context(argv: list[str]) -> tuple[Policy, bool]:
    targets = []
    draft = False
    uncertain = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in GH_VALUE_OPTIONS:
            index += 1
            if index == len(argv):
                uncertain = True
                break
            if token in {"-R", "--repo"}:
                targets.append(argv[index])
            elif token == "--hostname":
                uncertain = True
            elif token in {"-H", "--head"} and argv[index] != git(
                "symbolic-ref", "--short", "HEAD"
            ):
                uncertain = True
        elif token.startswith("--") and "=" in token:
            option, value = token.split("=", 1)
            if option == "--repo":
                targets.append(value)
            elif option == "--draft":
                draft = value == "true"
            elif option == "--head" and value != git("symbolic-ref", "--short", "HEAD"):
                uncertain = True
            elif (
                option not in GH_VALUE_OPTIONS | GH_BOOLEAN_OPTIONS
                or option == "--hostname"
            ):
                uncertain = True
        elif token.startswith("-R") and len(token) > 2:
            targets.append(token[2:].removeprefix("="))
        elif token.startswith("-") and token not in GH_BOOLEAN_OPTIONS:
            # Compact short value flags are valid gh syntax, not repository selectors.
            if len(token) < 3 or token[:2] not in GH_VALUE_OPTIONS:
                uncertain = True
            elif token[:2] == "-H" and token[2:] != git(
                "symbolic-ref", "--short", "HEAD"
            ):
                uncertain = True
        elif token in {"--draft", "-d"}:
            draft = True
        elif token not in GH_BOOLEAN_OPTIONS and token not in {"pr", "create"}:
            uncertain = True
        index += 1
    target = targets[0] if len(targets) == 1 else os.environ.get("GH_REPO")
    if len(targets) > 1:
        uncertain = True
    host = os.environ.get("GH_HOST", "github.com")
    if target and target.count("/") == 1 and ":" not in target:
        target = host + "/" + target
    if not target:
        identities = checkout_identities()
        if not identities or any(
            not identity.startswith(host.lower() + "/") for identity in identities
        ):
            uncertain = True
    policy = resolve_policy(target)
    repository = repository_id(target) if target else ""
    if uncertain or (target is not None and not repository):
        policy = Policy(reason="uncertain PR target")
    return Policy(policy.profile, policy.reason, repository), draft


def check_push(remote_url: str, updates: str) -> int:
    """Protect the actual destination default branch; preserve local gate transport."""
    identity = repository_id(remote_url)
    if not identity:
        if (
            "://" not in remote_url
            and "@" not in remote_url
            or remote_url.startswith("file://")
        ):
            return 0
        print(
            "repo-guards: blocked push with unresolved network destination",
            file=sys.stderr,
        )
        return 1
    policy = resolve_policy(remote_url)
    branches = []
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4:
            print("repo-guards: invalid push ref updates", file=sys.stderr)
            return 1
        if fields[2].startswith("refs/heads/"):
            branches.append(fields)
    if not branches:
        return 0
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", "--", remote_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    refs = (
        re.findall(r"^ref: (refs/heads/\S+)\tHEAD$", result.stdout, re.MULTILINE)
        if result and result.returncode == 0
        else []
    )
    if len(refs) != 1:
        print(
            "repo-guards: blocked branch push; destination default branch is unavailable",
            file=sys.stderr,
        )
        return 1
    for _, local_sha, remote_ref, remote_sha in branches:
        if remote_ref != refs[0]:
            continue
        if policy.profile != "relaxed":
            print(
                f"repo-guards: {policy.profile} blocks direct default-branch pushes; use a draft PR",
                file=sys.stderr,
            )
            return 1
        if set(local_sha) == {"0"} or set(remote_sha) == {"0"}:
            print(
                "repo-guards: blocked default-branch deletion or unverified creation",
                file=sys.stderr,
            )
            return 1
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
            capture_output=True,
        ).returncode:
            print(
                "repo-guards: blocked default-branch history rewrite", file=sys.stderr
            )
            return 1
    print(f"repo-guards: push policy {policy.profile}", file=sys.stderr)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser(
        "show", help="show effective policy without listing private assignments"
    )
    show.add_argument("--repo")
    show.add_argument("--json", action="store_true")
    context = sub.add_parser("gh-context", help="resolve actual gh pr create arguments")
    context.add_argument("args", nargs=argparse.REMAINDER)
    push = sub.add_parser("check-push", help="check pre-push ref updates on stdin")
    push.add_argument("remote_url")
    args = parser.parse_args(argv)
    if args.command == "check-push":
        return check_push(args.remote_url, sys.stdin.read())
    if args.command == "gh-context":
        gh_args = args.args[1:] if args.args[:1] == ["--"] else args.args
        policy, draft = gh_context(gh_args)
        print(f"{policy.profile}\t{policy.repository}\t{int(draft)}")
        return 0
    policy = resolve_policy(args.repo)
    payload = {
        "profile": policy.profile,
        "reason": policy.reason,
        "config": str(config_path()),
        "required_markers": policy.markers,
        "direct_default_branch_push": policy.profile == "relaxed",
        "tests": PROFILES[policy.profile]["tests"],
        "review": PROFILES[policy.profile]["review"],
    }
    if args.json:
        print(json.dumps(payload))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
