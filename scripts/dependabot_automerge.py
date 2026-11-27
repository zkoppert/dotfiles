#!/usr/bin/env python3
"""Scan configured repositories for passing Dependabot updates without inbox coupling."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from dependabot_alerts import publish
from dependabot_api import AutomationError, GitHub, latest_statuses
from dependabot_scope import in_scope
from dependency_review import review

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / ".copilot/skills/triage-dependabot")
)
from triage_dependabot import (
    detect_bump,
    is_prerelease_target,
    skipped_dependency_match,
)

LOG = logging.getLogger("dependabot-automerge")
DEFAULT_CONFIG = Path.home() / ".config/dependabot-automerge/config.yml"
DEFAULT_REPORT = Path.home() / "Library/Logs/dependabot-automerge.json"
BOT = "dependabot[bot]"
NAME = r"[a-z0-9][a-z0-9_.-]*"


API_ERRORS = (
    AutomationError,
    subprocess.SubprocessError,
    ValueError,
    KeyError,
    TypeError,
    OSError,
)


def load_config(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = {"enabled", "organizations", "repositories", "exclude_repositories"}
    if not isinstance(data, dict) or set(data) - keys:
        raise AutomationError("config must be a mapping with supported keys only")
    if type(data.get("enabled")) is not bool:
        raise AutomationError("enabled must be true or false")
    for key in keys - {"enabled"}:
        values = data.get(key, [])
        pattern = NAME if key == "organizations" else rf"{NAME}/\.?{NAME}"
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not re.fullmatch(pattern, value.lower())
            for value in values
        ):
            raise AutomationError(f"{key} must contain valid names")
        data[key] = sorted({value.lower() for value in values})
    if not data["organizations"] and not data["repositories"]:
        raise AutomationError("repository scope must not be empty")
    return data


def discover(client: GitHub, config: dict[str, Any]) -> list[str]:
    repos = set(config["repositories"])
    for org in config["organizations"]:
        for repo in client.pages(
            f"/orgs/{org}/repos?type=all", "map({full_name, archived, disabled})"
        ):
            name = repo["full_name"].lower()
            if (
                in_scope(name, config)
                and not repo.get("archived")
                and not repo.get("disabled")
            ):
                repos.add(name)
    return sorted(repo for repo in repos if in_scope(repo, config))


def check_state(client: GitHub, repo: str, sha: str) -> str:
    checks = client.pages(
        f"/repos/{repo}/commits/{sha}/check-runs?filter=latest", ".check_runs"
    )
    statuses = client.pages(f"/repos/{repo}/commits/{sha}/statuses")
    latest = {
        status["context"]: status["state"] for status in latest_statuses(statuses)
    }
    if any(check.get("head_sha") != sha for check in checks):
        return "inconsistent"
    passing = {"success", "neutral", "skipped"}
    if any(
        check.get("conclusion") not in passing | {None, ""} for check in checks
    ) or any(state not in passing | {"pending"} for state in latest.values()):
        return "failing"
    if (
        any(check.get("status") != "completed" for check in checks)
        or "pending" in latest.values()
    ):
        return "pending"
    conclusions = [check.get("conclusion") for check in checks] + list(latest.values())
    if any(value not in passing for value in conclusions):
        return "failing"
    return "passing" if "success" in conclusions else "absent"


def evaluate(
    client: GitHub, repo: str, pr: dict[str, Any], login: str
) -> tuple[str, str]:
    if pr.get("state") != "open" or pr.get("draft"):
        return "hold", "not an open, non-draft PR"
    if pr.get("user", {}).get("login") != BOT or pr["user"].get("type") != "Bot":
        return "hold", "author is not the Dependabot bot"
    if pr.get("auto_merge"):
        return "hold", "existing deferred auto-merge needs human attention"
    if pr["head"].get("repo", {}).get("full_name", "").lower() != repo:
        return "hold", "head branch is outside the target repository"
    if pr["base"].get("repo", {}).get("full_name", "").lower() != repo:
        return "hold", "base repository does not match policy"
    if skipped_dependency_match(pr):
        return "hold", "dependency excluded by existing safety policy"
    if is_prerelease_target(pr) or detect_bump(pr) not in {"patch", "minor", "major"}:
        return "hold", "prerelease or unknown upgrade"
    root = f"/repos/{repo}"
    number = pr["number"]
    reviews = client.pages(f"{root}/pulls/{number}/reviews")
    comments = client.pages(f"{root}/issues/{number}/comments")
    inline = client.pages(f"{root}/pulls/{number}/comments")
    if any((item.get("user") or {}).get("type") != "Bot" for item in inline):
        return "hold", "human inline review activity"
    for item in reviews + comments:
        author = item.get("user") or {}
        body = item.get("body") or ""
        own_approval = (
            item.get("state") == "APPROVED"
            and item.get("commit_id")
            and body == f"<!-- dependabot-automerge:review:{item['commit_id']} -->"
        )
        own_rebase = "state" not in item and re.fullmatch(
            r"@dependabot rebase\n\n<!-- dependabot-automerge:[0-9a-f]{1,40} -->", body
        )
        automation = author.get("login") == login and (own_approval or own_rebase)
        if item.get("state") == "CHANGES_REQUESTED" or (
            author.get("type") != "Bot" and not automation
        ):
            return "hold", "human activity or requested changes"
    needs_rebase = pr.get("mergeable_state") in {"behind", "dirty"}
    if not needs_rebase:
        checks = check_state(client, repo, pr["head"]["sha"])
        if checks == "failing":
            comparison = client.request(
                "GET",
                f"{root}/compare/{pr['base']['sha']}...{pr['head']['sha']}",
                expression="{merge_base: .merge_base_commit.sha}",
            )
            merge_base = comparison.get("merge_base")
            if not isinstance(merge_base, str) or not merge_base:
                raise AutomationError("CI recovery comparison lacks a merge base")
            needs_rebase = merge_base != pr["base"]["sha"]
        if checks != "passing" and not needs_rebase:
            return "wait" if checks == "pending" else "hold", f"CI checks {checks}"
    if needs_rebase:
        marker = f"<!-- dependabot-automerge:{pr['head']['sha']} -->"
        if any(
            marker in item.get("body", "")
            and item.get("user", {}).get("login") == login
            for item in comments
        ):
            return "wait", "rebase already requested for this revision"
        return "rebase", "branch needs a Dependabot rebase"
    if pr.get("mergeable") is not True:
        return "wait", "mergeability not confirmed"
    return "merge", "stable upgrade with passing checks"


def handle(
    client: GitHub,
    config: dict[str, Any],
    repo: str,
    number: int,
    login: str,
    dry_run: bool,
) -> dict[str, Any]:
    if not in_scope(repo, config):
        raise AutomationError(f"repository outside policy: {repo}")
    path = f"/repos/{repo}/pulls/{number}"
    pr = client.request("GET", path)
    action, reason = evaluate(client, repo, pr, login)
    sha = pr["head"]["sha"]
    result = {
        "repo": repo,
        "number": number,
        "sha": sha,
        "action": action,
        "reason": reason,
        "url": pr["html_url"],
        "bump": detect_bump(pr),
        "dry_run": dry_run,
    }
    if action == "merge":
        try:
            security = review(client, repo, pr)
            result["security_review"] = {
                key: value for key, value in security.items() if key != "evidence"
            }
            result["security_review"]["dependencies"] = [
                {key: value for key, value in dependency.items() if key != "patches"}
                for dependency in security.get("evidence", {}).get("dependencies", [])
            ]
            if security["decision"] != "allow":
                result.update(
                    action="hold",
                    reason="security review: "
                    + " ".join(security["summary"].split())[:600],
                )
        except API_ERRORS as exc:
            result.update(action="hold", reason=f"security review unavailable: {exc}")
    if dry_run or result["action"] not in {"merge", "rebase"}:
        return result
    current = client.request("GET", path)
    if (
        current["head"]["sha"] != sha
        or current["base"].get("sha") != pr["base"].get("sha")
        or current.get("state") != "open"
    ):
        result.update(action="wait", reason="PR changed during evaluation")
        return result
    action_now, reason_now = evaluate(client, repo, current, login)
    if action_now != action:
        result.update(
            action="hold" if action_now == "hold" else "wait", reason=reason_now
        )
        return result
    if action == "rebase":
        client.request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            {"body": f"@dependabot rebase\n\n<!-- dependabot-automerge:{sha} -->"},
        )
        return result
    reviews = client.pages(f"{path}/reviews")
    own = [
        review
        for review in reviews
        if review.get("user", {}).get("login") == login
        and review.get("state") != "COMMENTED"
    ]
    if not own or own[-1].get("state") != "APPROVED" or own[-1].get("commit_id") != sha:
        client.request(
            "POST",
            f"{path}/reviews",
            {
                "event": "APPROVE",
                "commit_id": sha,
                "body": f"<!-- dependabot-automerge:review:{sha} -->",
            },
        )
    ready = client.request("GET", path)
    if (
        ready["head"]["sha"] != sha
        or ready["base"].get("sha") != pr["base"].get("sha")
        or ready.get("state") != "open"
    ):
        raise AutomationError(f"PR changed after approval: {repo}#{number}")
    final_action, final_reason = evaluate(client, repo, ready, login)
    if final_action != "merge":
        result.update(
            action="wait" if final_action == "rebase" else final_action,
            reason=f"{final_reason} after approval",
        )
        return result
    if ready.get("mergeable_state") in {None, "unknown"}:
        result.update(action="wait", reason="mergeability not confirmed after approval")
        return result
    if ready.get("mergeable_state") != "clean":
        result.update(action="hold", reason="waiting for branch policy after approval")
        return result
    response = client.request(
        "PUT", f"{path}/merge", {"sha": sha, "merge_method": "squash"}
    )
    if response.get("merged") is not True:
        raise AutomationError(
            f"merge not completed: {repo}#{number}: {response.get('message')}"
        )
    result["action"] = "merged"
    return result


def scan(
    client: GitHub,
    config: dict[str, Any],
    dry_run: bool,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if report is None:
        report = {}
    report.update(
        {
            "started_at": dt.datetime.now(dt.UTC).isoformat(),
            "dry_run": dry_run,
            "repositories": [],
            "excluded_repositories": list(config["exclude_repositories"]),
            "scope_confirmed": True,
            "scope": {
                key: list(config[key])
                for key in ("organizations", "repositories", "exclude_repositories")
            },
            "completed_repositories": [],
            "failed_prs": [],
            "decisions": [],
            "errors": [],
        }
    )
    login = client.request("GET", "/user")["login"]
    for repo in discover(client, config):
        try:
            metadata = client.request(
                "GET", f"/repos/{repo}", expression="{archived, disabled, permissions}"
            )
            if metadata.get("archived") or metadata.get("disabled"):
                continue
            if not (metadata.get("permissions") or {}).get("push"):
                raise AutomationError(f"write permission unavailable: {repo}")
            report["repositories"].append(repo)
            for pr in client.pages(
                f"/repos/{repo}/pulls?state=open",
                "map({number, user: {login: .user.login}})",
            ):
                if pr.get("user", {}).get("login") != BOT:
                    continue
                try:
                    decision = handle(
                        client, config, repo, pr["number"], login, dry_run
                    )
                    report["decisions"].append(decision)
                    LOG.info(
                        "%s#%s: %s (%s)",
                        repo,
                        pr["number"],
                        decision["action"],
                        decision["reason"],
                    )
                except API_ERRORS as exc:
                    report["failed_prs"].append(f"{repo}#{pr['number']}")
                    report["errors"].append(f"{repo}#{pr['number']}: {exc}")
            report["completed_repositories"].append(repo)
        except API_ERRORS as exc:
            report["errors"].append(f"{repo}: {exc}")
    report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
    return report


def failed_run(
    args: argparse.Namespace, report: dict[str, Any] | None, exc: Exception
) -> int:
    report = report or {"decisions": [], "errors": [], "dry_run": args.dry_run}
    report["errors"].append(f"worker failure: {exc}")
    report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
    if not args.dry_run:
        try:
            publish(report, args.report)
        except (OSError, ValueError, subprocess.SubprocessError) as alert_error:
            LOG.error("Unable to publish worker failure: %s", alert_error)
    print(json.dumps(report, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read GitHub only; print the report without saving it.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if receipt := os.environ.get("DEPENDABOT_WORKER_STARTED"):
        Path(receipt).touch(mode=0o600)
    report = None
    try:
        config = load_config(args.config)
        if not args.dry_run and not config["enabled"]:
            LOG.info("Automation is disabled; use --dry-run to preview.")
            return 0
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.info(
                    "Another Dependabot scan is running; skipping this invocation."
                )
                return 0
            report = {}
            try:
                scan(GitHub(), config, args.dry_run, report=report)
                if not args.dry_run:
                    publish(report, args.report)
                print(json.dumps(report, indent=2))
                return 1 if report["errors"] else 0
            except Exception as exc:
                LOG.exception("Dependabot scan stopped")
                return failed_run(args, report, exc)
    except Exception as exc:
        LOG.exception("Dependabot initialization stopped")
        return failed_run(args, report, exc)


if __name__ == "__main__":
    sys.exit(main())
