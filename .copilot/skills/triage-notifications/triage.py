#!/usr/bin/env python3
"""Notification triage: classify GitHub notifications and route them to todo.yml.

For each unread notification from `gh api /notifications`, this tool:

1. Classifies it as one of:
   - DROP            (safe to mark-done without confirmation)
   - QUADRANT_Q1     (high-confidence: actionable now, goes straight to Q1)
   - INBOX           (actionable but needs human triage)
2. For DROP items: marks the thread done on GitHub (deletes from inbox).
3. For Q1/INBOX items: adds an entry to ~/repos/zkoppert-todo/todo.yml
   (deduped by notification thread_id).
4. Scans active todos for items in `done` status with a recorded
   notification thread_id and marks those notifications done on GitHub
   (the "mark-done-on-completed" loop).
5. Triggers a macOS notification if any new actionable items were added.

Designed to be safe to re-run (consistent: a second run produces no
duplicate todos and no spurious mark-dones).

Usage:
    triage.py [--dry-run] [--todo-file PATH] [--no-notify] [--verbose]

Exit codes:
    0  Triage completed (with or without items found)
    1  Error reading config, todo file, or hitting the API
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from ruamel.yaml import YAML
from ruamel.yaml import YAMLError as _RuamelYAMLError

# Round-trip YAML loader/dumper preserves comments, key order, and quoting
# in zkoppert-todo's todo.yml. Plain `yaml.safe_dump` drops every comment,
# which would silently destroy the manually maintained section headers.
_RT_YAML = YAML(typ="rt")
_RT_YAML.preserve_quotes = True
_RT_YAML.width = 4096
_RT_YAML.indent(mapping=2, sequence=4, offset=2)

# Allowlist of GitHub logins whose review_requested notifications auto-route
# to Q1. Currently Zack's direct reports plus his manager - kept narrow on
# purpose so cross-team requests still hit inbox for review.
NUX_TEAM_LOGINS_Q1: set[str] = {
    "iansan5653",  # Ian
    "andimiya",  # Andi
    "sutterj",  # Jacob
    "francisfuzz",  # Francis
    "Hkly",  # Hannah
    "depoll",  # David Poll (manager)
}

# Reasons that route straight to Q1 regardless of author.
Q1_REASONS: set[str] = {
    "mention",
    "assign",
    "security_alert",
}

# Subject states that are candidates for the drop bucket.
CLOSED_STATES: set[str] = {"closed", "merged"}

# Title patterns that auto-drop regardless of subject state. Useful for
# repetitive system-generated noise (intermittent test failures, flaky
# test reports) that lands as `team_mention` on open issues and would
# otherwise fall through to the inbox-by-default bucket.
#
# Patterns are deliberately anchored to the START of the title and
# require the phrase to be followed by a colon. This matches the
# system-generated shape (e.g. `Intermittent test failure: <test>` and
# `[Bug] Intermittent test failure: <test>`) without catching legitimate
# titles that mention the phrase as a substring (e.g. `Fix flaky test in
# dashboard` or `flaky test suite is failing CI completely`, which are
# real PRs / bugs we do NOT want to drop).
#
# Reasons in TITLE_DROP_PROTECTED_REASONS override this drop so an
# explicit ping still reaches the inbox. Add patterns as new noise
# shapes show up; keep them anchored on `^\s*(\[[^\]]+\]\s*)?` +
# specific phrase + `\s*:` so legitimate titles aren't swept up.
_TITLE_DROP_PREFIX = r"^\s*(\[[^\]]+\]\s*)?"
TITLE_DROP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        _TITLE_DROP_PREFIX + r"intermittent test failure\s*:", re.IGNORECASE
    ),
    re.compile(_TITLE_DROP_PREFIX + r"flaky test\s*:", re.IGNORECASE),
    re.compile(_TITLE_DROP_PREFIX + r"test flake\s*:", re.IGNORECASE),
]

# Reasons where a direct human action overrides title-pattern drops.
# If someone explicitly @-mentions or assigns Zack on a flaky-test
# issue, surface it instead of silently dropping.
TITLE_DROP_PROTECTED_REASONS: set[str] = {"mention", "assign"}

# Reasons that get a subject-state check at classify time. If the PR / issue
# is already closed/merged when the notification first arrives, drop it
# instead of routing to a quadrant or inbox - there is nothing left to do.
STATEFUL_REASONS: set[str] = {
    "review_requested",
    "mention",
    "assign",
    "manual",
    "team_mention",
    "author",
}

DEFAULT_TODO_FILE = Path.home() / "repos" / "zkoppert-todo" / "todo.yml"

# Buckets the classifier can return.
BUCKET_DROP = "DROP"
BUCKET_Q1 = "QUADRANT_Q1"
BUCKET_INBOX = "INBOX"
# Read notification that didn't match any drop rule. Already-viewed open
# subjects fall here so the cron leaves them where the user put them
# (don't re-add to inbox, don't mark notification done).
BUCKET_KEEP = "KEEP"

logger = logging.getLogger("triage")


@dataclass
class Classification:
    """The outcome of classifying a single notification."""

    bucket: str
    reason: str  # Human-readable justification for the bucket choice.
    # When BUCKET_DROP fires on a closed/merged PR I authored, also append
    # an entry to todo.yml's `done` section so the work shows up in
    # biannual reflections.
    archive_to_done: bool = False


@dataclass
class TriageStats:
    """Tally of what happened during one run, for the digest."""

    fetched: int = 0
    dropped: int = 0
    added_q1: int = 0
    added_inbox: int = 0
    already_tracked: int = 0
    marked_done: int = 0
    pruned_stale: int = 0
    pruned_by_reason: dict[str, int] = field(default_factory=dict)
    archived_to_done: int = 0
    skipped_read: int = 0
    errors: list[str] = field(default_factory=list)


# Pruner return sentinels for check_subject_stale().
STALE_DROP = "drop"  # Confirmed stale; safe to drop.
STALE_KEEP = "keep"  # Confirmed still active; keep.
STALE_UNKNOWN = "unknown"  # Could not determine (transient error); keep.


def run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run `gh <args>` and return stdout, or raise on non-zero exit."""
    cmd = ["gh", *args]
    logger.debug("running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout


def fetch_notifications() -> list[dict[str, Any]]:
    """Return all notifications for the authenticated user.

    Uses `?all=true` so we see notifications the user has already viewed
    on github.com (marked read) but not deleted. This is what lets the
    cron clean up merged/closed subjects after the fact - the previous
    unread-only fetch never re-saw a notification once the user clicked
    it, so closed PRs piled up in the inbox indefinitely.
    """
    # `--slurp` returns a JSON array-of-arrays (one inner array per page),
    # which is safe to parse regardless of titles that contain `][`.
    raw = run_gh(
        ["api", "/notifications?all=true", "--paginate", "--slurp"]
    ).strip()
    if not raw:
        return []
    try:
        pages = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("fetch_notifications: could not parse slurp output: %s", exc)
        return []
    merged: list[dict[str, Any]] = []
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, list):
                merged.extend(page)
            elif isinstance(page, dict):
                merged.append(page)
    return merged


def fetch_thread_state(notif: dict[str, Any]) -> str | None:
    """Return the subject state for a notification, or None on failure.

    Used to decide whether comment-style notifications are on
    closed/merged subjects (drop bucket). Only called for the small set of
    notifications where the state actually changes the classification.
    """
    subject = notif.get("subject") or {}
    url = subject.get("url")
    if not url:
        return None
    # Strip the api prefix; `gh api` accepts paths.
    path = url.replace("https://api.github.com", "")
    try:
        out = run_gh(["api", path], timeout=20)
        return (json.loads(out).get("state") or "").lower()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("fetch_thread_state failed for %s: %s", path, exc)
        return None


def fetch_subject_author(notif: dict[str, Any]) -> str | None:
    """Return the GitHub login that opened the subject (PR or issue).

    Used for `review_requested` notifications: GitHub doesn't include the
    requester in the notification payload, so we use the PR author as a
    pragmatic proxy. This handles the dominant NUX-team case where a
    teammate opens a PR and adds Zack as reviewer in the same step.
    Returns None on any API or parse failure.
    """
    subject = notif.get("subject") or {}
    url = subject.get("url")
    if not url:
        return None
    path = url.replace("https://api.github.com", "")
    try:
        out = run_gh(["api", path], timeout=20)
        return ((json.loads(out).get("user") or {}).get("login")) or None
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("fetch_subject_author failed for %s: %s", path, exc)
        return None


def fetch_latest_comment(
    notif: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return (author_login, body) for the latest comment on a notification.

    Returns (None, None) if unavailable. Used to detect super-linter posts
    and bot-noise comments that don't @-mention the user.
    """
    subject = notif.get("subject") or {}
    latest = subject.get("latest_comment_url")
    if not latest:
        return None, None
    path = latest.replace("https://api.github.com", "")
    try:
        out = run_gh(["api", path], timeout=20)
        data = json.loads(out)
        author = (data.get("user") or {}).get("login")
        body = data.get("body") or ""
        return author, body
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("fetch_latest_comment failed for %s: %s", path, exc)
        return None, None


def is_super_linter(author: str | None, body: str | None) -> bool:
    """Return True if the latest comment looks like a super-linter post."""
    if author and author.lower() in {"super-linter", "super-linter[bot]"}:
        return True
    if body and "super-linter" in body.lower():
        return True
    return False


def fetch_pr_human_commenters(
    notif: dict[str, Any], *, my_login: str
) -> set[str] | None:
    """Return the set of human GitHub logins (other than `my_login`) who
    have commented or reviewed the PR.

    Walks three endpoints because GitHub splits PR conversation across
    them: ``/issues/{n}/comments`` (top-level), ``/pulls/{n}/comments``
    (inline review comments), and ``/pulls/{n}/reviews`` (review summary
    submissions). For each commenter we keep only ``user.type == "User"``
    entries - bots like Copilot Code Review (``Bot``) are excluded by
    design so a self-authored PR with only bot reviewers can still drop.

    Returns ``None`` on any API or parse failure so callers can be
    conservative (keep the notification instead of dropping it under
    uncertainty).
    """
    subject = notif.get("subject") or {}
    url = subject.get("url") or ""
    if "/pulls/" not in url:
        return None
    # url is `https://api.github.com/repos/{owner}/{repo}/pulls/{n}` -
    # extract the repo + number for the three derived endpoints.
    path = url.replace("https://api.github.com", "")
    parts = path.strip("/").split("/")
    if len(parts) < 5 or parts[0] != "repos" or parts[3] != "pulls":
        return None
    owner, repo, number = parts[1], parts[2], parts[4]
    base = f"/repos/{owner}/{repo}"
    endpoints = [
        f"{base}/issues/{number}/comments",
        f"{base}/pulls/{number}/comments",
        f"{base}/pulls/{number}/reviews",
    ]
    commenters: set[str] = set()
    my_login_lc = my_login.lower()
    for endpoint in endpoints:
        try:
            out = run_gh(
                ["api", "--paginate", "--slurp", endpoint], timeout=30
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "fetch_pr_human_commenters %s failed: %s", endpoint, exc
            )
            return None
        # `--slurp` wraps each page response in an outer JSON array so we
        # can json.loads the whole thing safely - body content containing
        # `][` (markdown reference links, array indexing) cannot fragment
        # the parse. Matches the pattern used by fetch_notifications.
        try:
            pages = json.loads(out) if out.strip() else []
        except json.JSONDecodeError as exc:
            logger.warning(
                "fetch_pr_human_commenters parse failed for %s: %s",
                endpoint,
                exc,
            )
            return None
        for page in pages or []:
            if not isinstance(page, list):
                continue
            for entry in page:
                if not isinstance(entry, dict):
                    continue
                user = entry.get("user") or {}
                if (user.get("type") or "").lower() != "user":
                    continue
                login = user.get("login") or ""
                if not login or login.lower() == my_login_lc:
                    continue
                commenters.add(login)
    return commenters


def mentions_me(body: str | None, my_login: str) -> bool:
    """Return True if `body` contains an @-mention of `my_login` or `me`."""
    if not body:
        return False
    needle = f"@{my_login.lower()}"
    return needle in body.lower()


def classify(
    notif: dict[str, Any],
    *,
    my_login: str,
    q1_logins: set[str],
    state_fetcher=fetch_thread_state,
    comment_fetcher=fetch_latest_comment,
    subject_author_fetcher=fetch_subject_author,
    human_commenter_fetcher=fetch_pr_human_commenters,
) -> Classification:
    """Decide which bucket a notification belongs in.

    `state_fetcher`, `comment_fetcher`, `subject_author_fetcher`, and
    `human_commenter_fetcher` are injectable so tests can avoid network
    calls. They default to the live API helpers.

    Read-notification override: if `_classify_internal` would have routed
    a read notification (``unread is False``) to INBOX or Q1, swap to
    BUCKET_KEEP instead. DROP results pass through unchanged so the
    cleanup paths (``ci_activity``, ``comment``/``subscribed`` on closed
    subjects, super-linter comments) still mark and delete the
    notification. This guarantees noise gets cleared regardless of
    read-status, while user-already-viewed actionable items aren't
    re-added to the inbox on every cron tick.
    """
    result = _classify_internal(
        notif,
        my_login=my_login,
        q1_logins=q1_logins,
        state_fetcher=state_fetcher,
        comment_fetcher=comment_fetcher,
        subject_author_fetcher=subject_author_fetcher,
        human_commenter_fetcher=human_commenter_fetcher,
    )
    if result.bucket in {BUCKET_INBOX, BUCKET_Q1} and notif.get("unread") is False:
        return Classification(
            BUCKET_KEEP,
            f"already-read; would have been {result.bucket} ({result.reason})",
        )
    return result


def _classify_internal(
    notif: dict[str, Any],
    *,
    my_login: str,
    q1_logins: set[str],
    state_fetcher=fetch_thread_state,
    comment_fetcher=fetch_latest_comment,
    subject_author_fetcher=fetch_subject_author,
    human_commenter_fetcher=fetch_pr_human_commenters,
) -> Classification:
    """Raw classification before the read-notification KEEP override.

    Split out from `classify` so the KEEP override can be applied
    uniformly to INBOX/Q1 results without short-circuiting the DROP
    branches that follow in this function.
    """
    reason = (notif.get("reason") or "").lower()
    subject = notif.get("subject") or {}
    subject_type = (subject.get("type") or "").lower()
    title = subject.get("title") or ""

    # Title-pattern drop: repetitive system-generated noise (intermittent
    # test failures, flaky test reports) lands as `team_mention` on open
    # issues and would otherwise fall through to the inbox. Mention/assign
    # reasons skip this so a direct human ping always reaches the inbox.
    if reason not in TITLE_DROP_PROTECTED_REASONS:
        for pattern in TITLE_DROP_PATTERNS:
            if pattern.search(title):
                return Classification(
                    BUCKET_DROP,
                    f"title matches drop pattern /{pattern.pattern}/",
                )

    # Cheap early drop: if the subject is already closed/merged when the
    # notification first lands, there is nothing left to do. Only check
    # reasons that route to inbox/Q1, and only when the subject is the
    # kind of resource whose state actually means "done". Conservative on
    # error: state_fetcher returns None for network/parse failures, which
    # falls through to the normal classification path.
    if reason in STATEFUL_REASONS and subject_type in {"pullrequest", "issue"}:
        state = state_fetcher(notif)
        if state in CLOSED_STATES:
            # Archive the work to todo.yml's `done` section when I'm the
            # PR author so biannual reflection has the history. Issues
            # are skipped: I rarely "complete" an issue by closing it,
            # and PR-as-shipped-work is the cleaner signal.
            archive = False
            if subject_type == "pullrequest":
                author = subject_author_fetcher(notif)
                if author and author.lower() == my_login.lower():
                    archive = True
            return Classification(
                BUCKET_DROP,
                f"{reason} on {state} {subject_type}",
                archive_to_done=archive,
            )

    # Self-authored `Enable Dependabot` housekeeping PRs are pure noise
    # once any bots (Copilot reviewer, super-linter) have weighed in -
    # there's nothing to triage unless an actual human reviewer joins.
    # Drop only when no human besides me has commented. On fetch failure
    # (None), fall through and let the normal classifier handle it.
    # Placed AFTER the closed-state drop so already-closed Enable
    # Dependabot PRs drop on the cheap state check instead of paying
    # for 3 commenter API calls first.
    if (
        reason == "author"
        and subject_type == "pullrequest"
        and title.strip().lower() == "enable dependabot"
    ):
        commenters = human_commenter_fetcher(notif, my_login=my_login)
        if commenters is not None and not commenters:
            return Classification(
                BUCKET_DROP,
                "self-authored Enable Dependabot PR with no human commenters",
            )

    # Already-viewed notifications that would otherwise route to
    # inbox/Q1 are handled by the `classify` wrapper which post-processes
    # the result of this function. The DROP rules above already fire for
    # read notifications too, so noise like `ci_activity` and
    # comment-on-closed still get cleaned up regardless of read status.

    if reason == "review_requested":
        # Auto-Q1 only when the PR author is on the narrow allowlist.
        # GitHub doesn't put the requester in the notification payload, and
        # the latest comment author is unrelated to who clicked "request
        # review". Use the PR author as a pragmatic proxy: the dominant
        # NUX-team case is teammates opening their own PR and adding Zack
        # as reviewer in one step. Fall through to inbox if we can't
        # confirm.
        author = subject_author_fetcher(notif)
        if author and author in q1_logins:
            return Classification(
                BUCKET_Q1,
                f"review_requested on PR by teammate @{author}",
            )
        return Classification(
            BUCKET_INBOX,
            "review_requested - PR author not on Q1 allowlist (or unknown)",
        )

    if reason in Q1_REASONS:
        if reason in {"assign", "mention"}:
            subject_type = (notif.get("subject") or {}).get("type")
            if subject_type == "PullRequest":
                author = subject_author_fetcher(notif)
                if author and author.lower() == my_login.lower():
                    return Classification(
                        BUCKET_INBOX,
                        f"{reason} on PR I authored - status update only",
                    )
        return Classification(BUCKET_Q1, f"{reason} → Q1")

    if reason == "manual":
        # Subscribed deliberately. Surface as actionable but let user
        # triage rather than force Q1.
        return Classification(BUCKET_INBOX, "manual subscription")

    if reason == "comment":
        state = state_fetcher(notif)
        if state in CLOSED_STATES:
            return Classification(BUCKET_DROP, f"comment on {state} {subject_type}")
        author, body = comment_fetcher(notif)
        if mentions_me(body, my_login):
            return Classification(BUCKET_Q1, f"@mention in comment by @{author}")
        if is_super_linter(author, body):
            return Classification(BUCKET_DROP, "super-linter comment without @mention")
        return Classification(BUCKET_INBOX, "comment thread (not closed)")

    if reason == "ci_activity":
        # CI on someone else's PR is generally noise; on Zack's own PRs
        # it's still in his GitHub UI and rarely needs todo tracking.
        return Classification(BUCKET_DROP, "ci_activity")

    if reason == "subscribed":
        state = state_fetcher(notif)
        if state in CLOSED_STATES:
            return Classification(
                BUCKET_DROP, f"subscribed notice on {state} {subject_type}"
            )
        return Classification(BUCKET_INBOX, "subscribed thread (not closed)")

    # Unknown reason: be safe and surface for human triage.
    return Classification(BUCKET_INBOX, f"unknown reason '{reason}' - inbox by default")


def make_todo_id(notif: dict[str, Any]) -> str:
    """Build a stable kebab-case id from a notification."""
    subject = notif.get("subject") or {}
    subject_type = (subject.get("type") or "thread").lower()
    title = subject.get("title") or "notification"
    repo = (notif.get("repository") or {}).get("full_name") or "unknown"
    repo_slug = repo.split("/")[-1].lower()
    # Pull a number out of the subject url if present (PR/issue number).
    url = subject.get("url") or ""
    match = re.search(r"/(\d+)$", url)
    number = match.group(1) if match else notif.get("id", "x")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    if not slug:
        slug = "notif"
    return f"notif-{repo_slug}-{subject_type}-{number}-{slug}"


def web_url(notif: dict[str, Any]) -> str:
    """Convert an api.github.com subject url to the human-facing url."""
    subject = notif.get("subject") or {}
    url = subject.get("url") or ""
    # /repos/owner/repo/pulls/123 → /owner/repo/pull/123
    # /repos/owner/repo/issues/45 → /owner/repo/issues/45
    web = url.replace("https://api.github.com/repos/", "https://github.com/")
    web = web.replace("/pulls/", "/pull/")
    return web or ((notif.get("repository") or {}).get("html_url", ""))


def build_todo_entry(
    notif: dict[str, Any],
    classification: Classification,
) -> dict[str, Any]:
    """Construct a todo.yml-shaped entry from a notification."""
    subject = notif.get("subject") or {}
    repo = (notif.get("repository") or {}).get("full_name") or "unknown"
    today = datetime.date.today().isoformat()
    title = subject.get("title") or "Untitled notification"
    reason = notif.get("reason") or "unknown"

    entry: dict[str, Any] = {
        "id": make_todo_id(notif),
        "title": f"{title} ({repo})",
        "description": f"GitHub notification - reason: {reason}. {classification.reason}.",
        "category": "process",
        "source": "github-notification",
        "added": today,
        "notes": "",
        "notification": {
            "thread_id": str(notif.get("id")),
            "url": web_url(notif),
            "reason": reason,
            "repo": repo,
        },
    }

    if classification.bucket == BUCKET_Q1:
        entry.update(
            {
                "urgency": "high",
                "importance": "high",
                "quadrant": "q1_do_first",
                "status": "pending",
            }
        )
    return entry


def build_done_archive_entry(notif: dict[str, Any]) -> dict[str, Any]:
    """Construct a done-archive entry for a closed/merged PR I authored.

    Used when classify() returns BUCKET_DROP with archive_to_done=True
    so the work is captured in todo.yml's `done` section for biannual
    reflection. Kept minimal on purpose - the cron knows the PR title,
    link, repo, and the date of the closing event but nothing about
    impact or context, so leave room for the user to enrich later.

    Includes a ``notification`` block so ``existing_thread_ids`` can
    dedupe future runs. Without that block, a failed ``mark_thread_done``
    DELETE would cause the same notification to be re-archived on every
    subsequent cron tick until the DELETE eventually succeeded.
    """
    subject = notif.get("subject") or {}
    repo = (notif.get("repository") or {}).get("full_name") or "unknown"
    today = datetime.date.today().isoformat()
    title = subject.get("title") or "Untitled PR"
    return {
        "id": make_todo_id(notif),
        "title": f"{title} ({repo})",
        "description": (
            "Auto-archived from GitHub notifications: PR I authored was "
            "closed or merged."
        ),
        "category": "technical",
        "source": "github-notification-auto-archive",
        "added": today,
        "due": None,
        "urgency": "medium",
        "importance": "medium",
        "quadrant": "q1_do_first",
        "status": "done",
        "completed": today,
        "link": web_url(notif),
        "notes": "",
        "notification": {
            "thread_id": str(notif.get("id") or ""),
            "url": web_url(notif),
            "reason": (notif.get("reason") or "").lower(),
            "repo": repo,
        },
    }


def build_done_archive_entry_from_tracked(entry: dict[str, Any]) -> dict[str, Any]:
    """Build a done-archive entry from an existing tracked todo entry.

    Used by ``prune_stale_notifications`` when it drops an inbox/quadrant
    entry whose original notification ``reason`` was ``"author"`` (i.e.
    the PR was mine). Without this archive step, self-authored PRs that
    were first tracked while open and then merged later would silently
    vanish from todo.yml instead of landing in ``done`` for biannual
    reflection.

    The tracked entry's title was formatted as ``"{title} ({repo})"``
    by ``build_todo_entry`` at fetch time, so we reuse it verbatim
    rather than re-decorating.
    """
    notif = entry.get("notification") or {}
    today = datetime.date.today().isoformat()
    return {
        "id": entry.get("id") or "archived-notification",
        "title": entry.get("title") or "Auto-archived PR",
        "description": (
            "Auto-archived from GitHub notifications: tracked PR I "
            "authored closed or merged after sitting in inbox/quadrant."
        ),
        "category": "technical",
        "source": "github-notification-auto-archive",
        "added": today,
        "due": None,
        "urgency": "medium",
        "importance": "medium",
        "quadrant": "q1_do_first",
        "status": "done",
        "completed": today,
        "link": notif.get("url") or "",
        "notes": "",
        "notification": {
            "thread_id": str(notif.get("thread_id") or ""),
            "url": notif.get("url") or "",
            "reason": notif.get("reason") or "author",
            "repo": notif.get("repo") or "unknown",
        },
    }


def load_todo(path: Path) -> dict[str, Any]:
    """Load todo.yml, returning at least the top-level keys we expect.

    Uses round-trip YAML so that comments, key order, and quoting from the
    user-maintained todo.yml survive the read-modify-write cycle in
    `write_todo_atomic`.
    """
    if not path.exists():
        raise FileNotFoundError(f"todo file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = _RT_YAML.load(fh) or {}
    data.setdefault("inbox", [])
    data.setdefault("prioritized", {})
    data["prioritized"].setdefault("q1_do_first", [])
    data.setdefault("done", [])
    return data


def write_todo_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write todo.yml atomically (temp file + rename) to avoid corruption.

    Uses round-trip YAML so existing comments and structure in todo.yml
    are preserved when this function rewrites the file.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".todo-", suffix=".yml", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            _RT_YAML.dump(data, fh)
        shutil.move(tmp_path, path)
    except Exception:
        if Path(tmp_path).exists():
            os.unlink(tmp_path)
        raise


def existing_thread_ids(data: dict[str, Any]) -> set[str]:
    """Return all notification.thread_id values already in todo.yml.

    Looks across inbox, all prioritized quadrants, in_progress, blocked,
    in_review, and done so we never double-add.
    """
    ids: set[str] = set()

    def collect(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            notif = item.get("notification")
            if not isinstance(notif, dict):
                continue
            thread_id = notif.get("thread_id")
            if thread_id:
                ids.add(str(thread_id))

    collect(data.get("inbox"))
    collect(data.get("done"))
    for key in ("in_progress", "blocked", "in_review"):
        collect(data.get(key))
    prioritized = data.get("prioritized") or {}
    for items in prioritized.values():
        collect(items)
    return ids


def items_to_mark_done(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return items with a thread_id whose notification can be marked done.

    Items in the top-level `done:` section are completed by definition:
    zkoppert-todo doesn't carry a `status` field there (entries have
    `id`/`title`/`completed`/`category`). So `done:` membership alone is
    proof of completion. All other sections still require `status: done`
    because they hold work-in-flight items where status drives this loop.
    """
    ready: list[dict[str, Any]] = []

    def scan(items: Any, *, require_status_done: bool) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if require_status_done and item.get("status") != "done":
                continue
            notif = item.get("notification")
            if not isinstance(notif, dict):
                continue
            if not notif.get("thread_id"):
                continue
            if notif.get("marked_done"):
                continue
            ready.append(item)

    scan(data.get("done"), require_status_done=False)
    prioritized = data.get("prioritized") or {}
    for items in prioritized.values():
        scan(items, require_status_done=True)
    for key in ("in_progress", "blocked", "in_review"):
        scan(data.get(key), require_status_done=True)
    return ready


def mark_thread_done(thread_id: str) -> None:
    """DELETE the GitHub notification thread to mark it done.

    DELETE moves the thread out of the inbox into the Done tab, matching
    the behavior of the Done button in the GitHub UI. This is distinct
    from PATCH, which only clears the unread/bold indicator while the
    thread stays in the inbox.
    """
    run_gh(["api", "-X", "DELETE", f"/notifications/threads/{thread_id}"], timeout=20)


def macos_notify(title: str, message: str) -> None:
    """Best-effort macOS notification via osascript. Silent on failure."""
    try:
        script = (
            f'display notification "{message}" '
            f'with title "{title}" sound name "default"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.debug("macos_notify failed: %s", exc)


def get_my_login() -> str:
    """Return the authenticated gh user login."""
    out = run_gh(["api", "/user"])
    return json.loads(out)["login"]


_GH_PATH_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/"
    r"(?P<kind>pull|issues|discussions)/(?P<number>\d+)"
)


def parse_github_url(url: str) -> dict[str, Any] | None:
    """Parse a github.com web URL into its owner/repo/kind/number parts.

    Returns ``None`` for URLs that don't point at a PR, issue, or discussion
    (release pages, commit URLs, repo root, malformed input, etc.). The
    pruner uses ``None`` as the signal to leave an entry alone.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.netloc not in ("github.com", "www.github.com"):
        return None
    match = _GH_PATH_RE.match(parsed.path)
    if not match:
        return None
    kind_raw = match.group("kind")
    # Normalize: "pull" -> "pr", "issues" -> "issue", "discussions" -> "discussion".
    kind = {"pull": "pr", "issues": "issue", "discussions": "discussion"}[kind_raw]
    return {
        "owner": match.group("owner"),
        "repo": match.group("repo"),
        "kind": kind,
        "number": int(match.group("number")),
    }


def _is_not_found_error(exc: subprocess.CalledProcessError) -> bool:
    """Detect a 404 from `gh api` error output.

    `gh` writes errors like ``gh: HTTP 404: Not Found (...)`` to stderr on
    REST 404s. We only treat that exact case as "the subject is gone".

    We deliberately do NOT match a bare ``"not found"`` substring because
    GitHub returns HTTP 403 with body ``{"message": "Not Found"}`` for
    private repos where access has been revoked (enumeration protection),
    and `gh` renders that as ``gh: HTTP 403: Not Found``. Treating that as
    "deleted" would silently drop inbox items for repos the user no longer
    has access to.

    We also deliberately do NOT match ``"could not resolve"`` from GraphQL
    because that same message is returned both when the repo / discussion
    has been deleted AND when the token has lost access to a private repo
    (enumeration protection at the GraphQL layer). We can't distinguish the
    two cases, so the discussion checker treats every GraphQL error as
    UNKNOWN and lets the pruner keep the entry. Discussions that are gone
    while we still have repo access surface through the ``discussion: null``
    path in ``_check_discussion_stale`` instead.
    """
    stderr = (exc.stderr or "").lower() if getattr(exc, "stderr", None) else ""
    return "http 404" in stderr


def _check_pr_or_issue_stale(parsed: dict[str, Any]) -> tuple[str, str]:
    owner, repo, kind, number = (
        parsed["owner"],
        parsed["repo"],
        parsed["kind"],
        parsed["number"],
    )
    api_path = (
        f"/repos/{owner}/{repo}/pulls/{number}"
        if kind == "pr"
        else f"/repos/{owner}/{repo}/issues/{number}"
    )
    try:
        raw = run_gh(["api", api_path], timeout=20)
    except subprocess.CalledProcessError as exc:
        if _is_not_found_error(exc):
            return (STALE_DROP, "deleted")
        return (STALE_UNKNOWN, f"api error: {exc.returncode}")
    except subprocess.TimeoutExpired:
        return (STALE_UNKNOWN, "api timeout")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (STALE_UNKNOWN, "invalid json")
    state = (data.get("state") or "").lower()
    if state != "closed":
        return (STALE_KEEP, f"{kind} state={state}")
    if kind == "pr":
        return (STALE_DROP, "merged" if data.get("merged_at") else "closed pr")
    return (STALE_DROP, "closed issue")


_DISCUSSION_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      closed
      locked
      answerChosenAt
      category { isAnswerable }
    }
  }
}
""".strip()


def _check_discussion_stale(parsed: dict[str, Any]) -> tuple[str, str]:
    owner, repo, number = parsed["owner"], parsed["repo"], parsed["number"]
    try:
        raw = run_gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={_DISCUSSION_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo}",
                "-F",
                f"number={number}",
            ],
            timeout=20,
        )
    except subprocess.CalledProcessError as exc:
        if _is_not_found_error(exc):
            return (STALE_DROP, "deleted")
        return (STALE_UNKNOWN, f"graphql error: {exc.returncode}")
    except subprocess.TimeoutExpired:
        return (STALE_UNKNOWN, "graphql timeout")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return (STALE_UNKNOWN, "invalid json")
    repo_node = (payload.get("data") or {}).get("repository")
    if repo_node is None:
        # ``repository: null`` here means GraphQL couldn't surface the repo
        # (deleted OR token has no access). We can't tell which, so be
        # conservative and keep the entry rather than silently dropping
        # something the user lost access to.
        return (STALE_UNKNOWN, "repository null")
    disc = repo_node.get("discussion")
    if disc is None:
        # We have repo access (repo_node is a real object), so a null
        # discussion means the discussion itself is gone - safe to drop.
        return (STALE_DROP, "deleted")
    if disc.get("locked"):
        return (STALE_DROP, "locked discussion")
    is_qa = (disc.get("category") or {}).get("isAnswerable")
    if is_qa and disc.get("answerChosenAt"):
        return (STALE_DROP, "answered Q&A")
    return (STALE_KEEP, "discussion still open")


def check_subject_stale(parsed: dict[str, Any]) -> tuple[str, str]:
    """Decide if a parsed GitHub subject is stale enough to drop from inbox.

    Returns a ``(action, reason)`` tuple where action is one of
    ``STALE_DROP``, ``STALE_KEEP``, or ``STALE_UNKNOWN``. The reason is a
    short human-readable string used in summary logging.

    Error policy: a confirmed 404 from the API means "drop" because the
    subject is gone. Every other error (network, 5xx, rate limit, parse
    failure) returns UNKNOWN, which the pruner treats as "keep" so we never
    drop an item based on a transient failure.
    """
    kind = parsed.get("kind")
    if kind in ("pr", "issue"):
        return _check_pr_or_issue_stale(parsed)
    if kind == "discussion":
        return _check_discussion_stale(parsed)
    return (STALE_UNKNOWN, f"unsupported kind: {kind}")


def _check_and_drop_stale(
    entries: list[Any],
    stats: TriageStats,
    *,
    section: str,
    dry_run: bool,
) -> tuple[list[Any], int, list[dict[str, Any]]]:
    """Filter one list of todo entries, dropping the stale github-notification ones.

    Returns ``(kept_entries, checked_count, archive_entries)``. Mutates
    ``stats`` in place: increments ``pruned_stale``, updates
    ``pruned_by_reason``, appends to ``errors`` on mark-done failures.
    ``section`` is included in log lines so inbox vs. quadrant drops are
    distinguishable in the cron log.

    Stale-detection and mark-done policy match the original inbox pruner:
    only confirmed-closed subjects drop, transient errors keep the entry,
    and a mark-done failure is logged but does not block the drop.

    Archive policy: when a dropped entry's stored
    ``notification.reason == "author"`` and the parsed URL kind is
    ``"pr"``, build a ``done``-shaped archive entry and return it.
    Callers append these to ``data["done"]`` so self-authored PRs that
    were first tracked open and later merged still land in the biannual
    reflection archive instead of vanishing silently.
    """
    kept: list[Any] = []
    archive: list[dict[str, Any]] = []
    checked = 0
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        if entry.get("source") != "github-notification":
            kept.append(entry)
            continue
        notif = entry.get("notification")
        if not isinstance(notif, dict):
            kept.append(entry)
            continue
        url = notif.get("url", "")
        parsed = parse_github_url(url)
        if parsed is None:
            kept.append(entry)
            continue
        checked += 1
        action, reason = check_subject_stale(parsed)
        if action == STALE_DROP:
            stats.pruned_stale += 1
            stats.pruned_by_reason[reason] = stats.pruned_by_reason.get(reason, 0) + 1
            if (
                parsed.get("kind") == "pr"
                and (notif.get("reason") or "").lower() == "author"
            ):
                archive.append(build_done_archive_entry_from_tracked(entry))
                stats.archived_to_done += 1
            thread_id = notif.get("thread_id")
            if thread_id and not dry_run:
                try:
                    mark_thread_done(str(thread_id))
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    stats.errors.append(
                        f"prune mark-done failed for thread {thread_id}: {exc}"
                    )
            logger.info(
                "pruned %s item %s (%s)",
                section,
                entry.get("id"),
                reason,
            )
            continue
        kept.append(entry)
    return kept, checked, archive


# Quadrants that the pruner sweeps in addition to ``inbox``. Order is
# stable for predictable log output but does not affect correctness.
PRUNE_QUADRANTS: tuple[str, ...] = (
    "q1_do_first",
    "q2_schedule",
    "q3_delegate",
    "q4_eliminate",
)


def prune_stale_notifications(
    data: dict[str, Any], stats: TriageStats, dry_run: bool = False
) -> None:
    """Drop stale github-notification entries from inbox and all quadrants.

    Walks ``data['inbox']`` and each quadrant in ``data['prioritized']``,
    dropping entries where ``source == 'github-notification'`` and whose
    subject URL parses as a PR / issue / discussion that is now closed,
    merged, locked, or answered. Manually added items (any other
    ``source``) are left alone, as are entries with unparseable URLs.

    This is the only mechanism that cleans entries already promoted to a
    quadrant - ``classify()`` only sees fresh notifications because the
    ``run()`` loop skips ``already_tracked`` thread IDs. Without this
    sweep, a Q2 PR that gets merged sits in the quadrant forever unless
    the user manually marks it done.

    Drop policy matches the original inbox pruner: confirmed-closed →
    drop + mark thread done; transient errors → keep; mark-done failures
    → log to ``stats.errors`` but still drop the local entry.

    Archive: when a dropped entry's stored ``notification.reason`` was
    ``"author"`` (so the PR was mine) and the URL kind is a PR, an
    archive entry is appended to ``data["done"]`` before the inbox/
    quadrant entry is discarded. This catches the dominant
    self-authored-PR lifecycle (open → tracked in inbox → merged later)
    which classify() can no longer see because the run loop skips
    ``already_tracked`` thread ids.
    """
    total_checked = 0
    archive_entries: list[dict[str, Any]] = []
    inbox = data.get("inbox")
    if inbox:
        kept, checked, archive = _check_and_drop_stale(
            inbox, stats, section="inbox", dry_run=dry_run
        )
        data["inbox"] = kept
        archive_entries.extend(archive)
        total_checked += checked

    prioritized = data.get("prioritized") or {}
    for quadrant in PRUNE_QUADRANTS:
        entries = prioritized.get(quadrant)
        if not entries:
            continue
        kept, checked, archive = _check_and_drop_stale(
            entries, stats, section=quadrant, dry_run=dry_run
        )
        prioritized[quadrant] = kept
        archive_entries.extend(archive)
        total_checked += checked

    if archive_entries:
        data.setdefault("done", []).extend(archive_entries)

    logger.debug(
        "pruner checked %d github-notification entries across inbox + quadrants, dropped %d (archived %d)",
        total_checked,
        stats.pruned_stale,
        len(archive_entries),
    )


# Backward-compatible alias for the old single-section pruner. The
# function now sweeps quadrants too despite the legacy name, so external
# callers keep working without code changes.
prune_stale_inbox = prune_stale_notifications


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Triage GitHub notifications into ~/repos/zkoppert-todo/todo.yml.",
    )
    parser.add_argument(
        "--todo-file",
        type=Path,
        default=DEFAULT_TODO_FILE,
        help=f"Path to todo.yml (default: {DEFAULT_TODO_FILE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and report, but do not modify todo.yml or call DELETE.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip the macOS notification even if actionable items were added.",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip the inbox staleness pruner (still classifies new notifications).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> TriageStats:
    """Main entrypoint - returns stats so tests can assert behaviour."""
    stats = TriageStats()

    try:
        my_login = get_my_login()
        logger.debug("authenticated as @%s", my_login)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        stats.errors.append(f"failed to fetch /user: {exc}")
        return stats

    try:
        notifications = fetch_notifications()
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        stats.errors.append(f"failed to fetch notifications: {exc}")
        return stats

    stats.fetched = len(notifications)
    logger.info("fetched %d notification(s)", stats.fetched)

    try:
        data = load_todo(args.todo_file)
    except (FileNotFoundError, yaml.YAMLError, _RuamelYAMLError) as exc:
        stats.errors.append(f"failed to load todo file: {exc}")
        return stats

    seen_ids = existing_thread_ids(data)
    new_q1: list[dict[str, Any]] = []
    new_inbox: list[dict[str, Any]] = []
    new_done: list[dict[str, Any]] = []

    for notif in notifications:
        thread_id = str(notif.get("id") or "")
        if thread_id and thread_id in seen_ids:
            stats.already_tracked += 1
            continue
        classification = classify(
            notif,
            my_login=my_login,
            q1_logins=NUX_TEAM_LOGINS_Q1,
        )
        logger.debug(
            "thread %s → %s (%s)",
            thread_id,
            classification.bucket,
            classification.reason,
        )
        if classification.bucket == BUCKET_KEEP:
            stats.skipped_read += 1
            continue
        if classification.bucket == BUCKET_DROP:
            stats.dropped += 1
            if classification.archive_to_done:
                new_done.append(build_done_archive_entry(notif))
                stats.archived_to_done += 1
            if not args.dry_run:
                try:
                    mark_thread_done(thread_id)
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    stats.errors.append(
                        f"mark-done failed for thread {thread_id}: {exc}"
                    )
            continue
        entry = build_todo_entry(notif, classification)
        if classification.bucket == BUCKET_Q1:
            new_q1.append(entry)
            stats.added_q1 += 1
        else:
            new_inbox.append(entry)
            stats.added_inbox += 1

    # Mark-done-on-completed loop: scan tracked items now marked done.
    ready = items_to_mark_done(data)
    for item in ready:
        notif_meta = item["notification"]
        thread_id = str(notif_meta["thread_id"])
        if not args.dry_run:
            try:
                mark_thread_done(thread_id)
                notif_meta["marked_done"] = True
                notif_meta["marked_done_at"] = datetime.date.today().isoformat()
                stats.marked_done += 1
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ) as exc:
                stats.errors.append(
                    f"mark-done-on-completed failed for thread {thread_id}: {exc}"
                )
        else:
            stats.marked_done += 1

    # Stale-notification pruner: drop github-notification entries from
    # inbox and quadrants whose subject is now closed/merged/locked/
    # answered. Runs even on --dry-run (so we can see what would be
    # pruned) but the write itself is gated below.
    if not args.no_prune:
        prune_stale_notifications(data, stats, dry_run=args.dry_run)

    # Append new entries to todo.yml.
    if (
        new_q1
        or new_inbox
        or new_done
        or stats.marked_done
        or stats.pruned_stale
    ) and not args.dry_run:
        data["inbox"].extend(new_inbox)
        data["prioritized"]["q1_do_first"].extend(new_q1)
        data["done"].extend(new_done)
        try:
            write_todo_atomic(args.todo_file, data)
        except OSError as exc:
            stats.errors.append(f"failed to write todo file: {exc}")
            return stats

    new_actionable = stats.added_q1 + stats.added_inbox
    if new_actionable and not args.no_notify:
        title = "Notification triage"
        message = (
            f"{stats.added_q1} new Q1, {stats.added_inbox} to inbox "
            f"({stats.dropped} dropped)"
        )
        macos_notify(title, message)

    return stats


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    stats = run(args)
    print(
        f"fetched={stats.fetched} added_q1={stats.added_q1} "
        f"added_inbox={stats.added_inbox} dropped={stats.dropped} "
        f"archived_to_done={stats.archived_to_done} "
        f"skipped_read={stats.skipped_read} "
        f"already_tracked={stats.already_tracked} "
        f"marked_done={stats.marked_done} "
        f"pruned_stale={stats.pruned_stale}"
    )
    if stats.pruned_by_reason:
        breakdown = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(stats.pruned_by_reason.items())
        )
        print(f"pruned_breakdown: {breakdown}")
    for err in stats.errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main())
