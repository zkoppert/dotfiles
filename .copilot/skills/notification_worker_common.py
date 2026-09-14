#!/usr/bin/env python3
"""Shared helpers for the GitHub notification workers.

This module intentionally uses only the standard library so the shell
wrappers can rely on it after the pinned runtime passes import preflight.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

HOME = Path.home()
DEFAULT_SUPPORT_DIR = HOME / "Library" / "Application Support" / "notification-workers"
DEFAULT_LEDGER_FILE = DEFAULT_SUPPORT_DIR / "ledger.sqlite"
ALLOW_DRY_RUN_LEDGER_WRITES = False


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def utcnow_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str | None) -> _dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def normalize_github_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = parsed.netloc.lower()
    if host not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        return None
    owner = parts[0]
    repo = parts[1]
    kind = parts[2]
    remainder = parts[3:]
    if kind == "pulls":
        kind = "pull"
    elif kind == "commits":
        kind = "commit"
    if kind in {"pull", "issues", "discussions", "commit"} and remainder:
        return f"https://github.com/{owner}/{repo}/{kind}/{remainder[0]}"
    return f"https://github.com/{owner}/{repo}"


def add_business_days(moment: _dt.datetime, days: int) -> _dt.datetime:
    current = moment
    remaining = max(days, 0)
    while remaining:
        current += _dt.timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def review_request_escalates_at(captured_at: str | None) -> str | None:
    parsed = parse_iso_datetime(captured_at)
    if parsed is None:
        return None
    return (
        add_business_days(parsed, 1)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _comment_stream_key_for_path(path: str, subject_type: str) -> str | None:
    if path.endswith("/reviews"):
        return "pull_reviews"
    if "/issues/" in path and path.endswith("/comments"):
        return "issue_comments"
    if "/pulls/" in path and path.endswith("/comments"):
        return "pull_comments"
    if subject_type == "issue" and path.endswith("/comments"):
        return "issue_comments"
    if subject_type == "pullrequest" and path.endswith("/comments"):
        return "pull_comments"
    return None


def _comment_collection_paths(
    subject: dict[str, Any], latest_path: str
) -> list[tuple[str, str]]:
    subject_type = (subject.get("type") or "").lower()
    subject_path = str(subject.get("url") or "").replace("https://api.github.com", "")
    paths: list[tuple[str, str]] = []
    if subject_type == "pullrequest" and subject_path:
        issue_path = subject_path.replace("/pulls/", "/issues/")
        paths.extend(
            [
                ("issue_comments", f"{issue_path}/comments"),
                ("pull_comments", f"{subject_path}/comments"),
                ("pull_reviews", f"{subject_path}/reviews"),
            ]
        )
    elif subject_type == "issue" and subject_path:
        paths.append(("issue_comments", f"{subject_path}/comments"))
    elif "/pulls/comments/" in latest_path and "/pulls/" in subject_path:
        paths.append(("pull_comments", f"{subject_path}/comments"))
    elif "/issues/comments/" in latest_path and subject_path:
        paths.append(("issue_comments", f"{subject_path.replace('/pulls/', '/issues/')}/comments"))
    elif "/comments/" in latest_path and subject_path:
        paths.append((_comment_stream_key_for_path(latest_path, subject_type) or "pull_comments", f"{subject_path}/comments"))
    return list(dict.fromkeys(paths))


@dataclass(frozen=True)
class CommentNotificationSnapshot:
    author: str | None
    body: str | None
    direct: bool | None
    history_complete: bool
    comment_cursors: dict[str, str] | None = None


def _comment_cursor_from_parts(
    stream_key: str,
    stamp: _dt.datetime | None,
    comment_id: int,
) -> str:
    payload = {
        "stream": stream_key,
        "ts": stamp.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if stamp
        else "",
        "id": comment_id,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _parse_comment_cursor(
    since: str | None,
) -> tuple[str, _dt.datetime | None, int] | None:
    if not since or not isinstance(since, str):
        return None
    text = since.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        stream = str(payload.get("stream") or "").strip()
        stamp = parse_iso_datetime(str(payload.get("ts") or ""))
        comment_id_value = payload.get("id")
        if not stream or comment_id_value is None:
            return None
        try:
            comment_id = int(comment_id_value)
        except (TypeError, ValueError):
            return None
        return stream, stamp, comment_id
    return None


def _parse_comment_watermarks(
    since: str | None,
) -> dict[str, tuple[_dt.datetime | None, int]]:
    if not since or not isinstance(since, str):
        return {}
    text = since.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict) and "stream" in payload:
        cursor = _parse_comment_cursor(text)
        if cursor is None:
            return {}
        stream, stamp, comment_id = cursor
        return {stream: (stamp, comment_id)}
    if not isinstance(payload, dict):
        return {}
    watermarks: dict[str, tuple[_dt.datetime | None, int]] = {}
    for stream, cursor_payload in payload.items():
        if not isinstance(stream, str) or not isinstance(cursor_payload, dict):
            continue
        stamp = parse_iso_datetime(str(cursor_payload.get("ts") or ""))
        comment_id_value = cursor_payload.get("id")
        if comment_id_value is None:
            continue
        try:
            comment_id = int(comment_id_value)
        except (TypeError, ValueError):
            continue
        watermarks[stream] = (stamp, comment_id)
    return watermarks


def comment_notification_snapshot(
    notif: dict[str, Any],
    *,
    my_login: str,
    run_gh,
    since: str | None = None,
) -> CommentNotificationSnapshot | None:
    subject = notif.get("subject") or {}
    latest = subject.get("latest_comment_url")
    if not latest:
        return None
    latest_path = str(latest).replace("https://api.github.com", "")
    latest_stream_key = _comment_stream_key_for_path(
        latest_path, (subject.get("type") or "").lower()
    )
    watermarks = _parse_comment_watermarks(since)
    pattern = (
        rf"(?<![A-Za-z0-9])@{re.escape(my_login)}(?![A-Za-z0-9-])"
        if my_login
        else None
    )
    collected: list[tuple[_dt.datetime | None, str, int, int, int, dict[str, Any]]] = []
    history_incomplete = False
    paths = _comment_collection_paths(subject, latest_path)
    if not paths and latest_stream_key:
        paths = [(latest_stream_key, latest_path)]
    for stream_key, collection_path in paths:
        stream_cursor = watermarks.get(stream_key)
        try:
            out = run_gh(
                ["api", collection_path, "--method", "GET", "--paginate", "--slurp"],
                timeout=20,
            )
            pages = json.loads(out)
            pages_list = pages if isinstance(pages, list) else [pages]
            for page_index, page in enumerate(pages_list):
                comments = page if isinstance(page, list) else [page]
                for comment_index, comment in enumerate(comments):
                    if not isinstance(comment, dict):
                        continue
                    stamp = parse_iso_datetime(
                        str(
                            comment.get("updated_at")
                            or comment.get("submitted_at")
                            or comment.get("created_at")
                            or ""
                        )
                    )
                    comment_id = comment.get("id")
                    if isinstance(comment_id, int):
                        numeric_comment_id = comment_id
                    elif isinstance(comment_id, str) and comment_id.isdigit():
                        numeric_comment_id = int(comment_id)
                    else:
                        numeric_comment_id = -1
                    collected.append(
                        (
                            stamp,
                            stream_key,
                            numeric_comment_id,
                            page_index,
                            comment_index,
                            comment,
                        )
                    )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ):
            history_incomplete = True
    if not collected:
        if history_incomplete:
            return CommentNotificationSnapshot(None, None, None, False, None)
        return None
    relevant: list[tuple[_dt.datetime | None, str, int, int, int, dict[str, Any]]] = []
    for entry in collected:
        entry_ts, entry_stream, entry_comment_id, _, _, _ = entry
        stream_cursor = watermarks.get(entry_stream)
        if stream_cursor is None:
            relevant.append(entry)
            continue
        cursor_ts, cursor_comment_id = stream_cursor
        if cursor_ts is None:
            if entry_comment_id > cursor_comment_id:
                relevant.append(entry)
            continue
        if entry_ts is None:
            continue
        if entry_ts > cursor_ts:
            relevant.append(entry)
            continue
        if entry_ts == cursor_ts and entry_comment_id > cursor_comment_id:
            relevant.append(entry)
    if not relevant:
        if history_incomplete:
            return CommentNotificationSnapshot(None, None, None, False, None)
        return CommentNotificationSnapshot(None, None, False, True, None)
    relevant.sort(
        key=lambda entry: (
            entry[0] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc),
            entry[2],
            entry[3],
            entry[4],
        )
    )
    latest_entry = relevant[-1]
    direct_entries = [
        entry
        for entry in relevant
        if pattern
        and str(entry[5].get("body") or "")
        and re.search(pattern, str(entry[5].get("body") or ""), re.IGNORECASE)
    ]
    latest_by_stream: dict[str, tuple[_dt.datetime | None, int]] = {}
    for entry in relevant:
        latest_by_stream[entry[1]] = (entry[0], entry[2])
    latest_comment = latest_entry[5]
    author = (latest_comment.get("user") or {}).get("login")
    body = str(latest_comment.get("body") or "") or None
    direct = bool(direct_entries)
    if history_incomplete:
        return CommentNotificationSnapshot(
            author,
            body,
            direct if direct else None,
            False,
            None,
        )
    return CommentNotificationSnapshot(
        author,
        body,
        direct,
        True,
        {
            stream: _comment_cursor_from_parts(stream, stamp, comment_id)
            for stream, (stamp, comment_id) in latest_by_stream.items()
        },
    )


class NotificationLedger:
    """Durable local ledger for notification lifecycle state."""

    def __init__(self, path: Path = DEFAULT_LEDGER_FILE):
        self.path = path
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path == DEFAULT_LEDGER_FILE:
            self.path.parent.chmod(0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self._secure_files()
        self._ensure_schema()
        self._secure_files()

    def _secure_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-journal"),
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_id TEXT,
                  canonical_artifact TEXT,
                  title TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  repo TEXT NOT NULL DEFAULT '',
                  classification TEXT NOT NULL DEFAULT '',
                  tracker_item_id TEXT,
                  tracker_section TEXT,
                  terminal_disposition TEXT,
                  clear_state TEXT NOT NULL DEFAULT 'not_applicable',
                  last_clear_error TEXT,
                  first_seen_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  classified_at TEXT NOT NULL,
                  tracker_linked_at TEXT,
                  terminal_recorded_at TEXT,
                  clear_attempted_at TEXT,
                  cleared_at TEXT,
                  comment_watermark TEXT,
                  worker TEXT NOT NULL DEFAULT ''
                )
                """)
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(notifications)")
            }
            conn.execute("DROP INDEX IF EXISTS idx_notifications_source")
            conn.execute("DROP INDEX IF EXISTS idx_notifications_canonical")
            for obsolete_column in ("source_type", "lifecycle_state", "payload_json"):
                if obsolete_column in columns:
                    conn.execute(
                        f"ALTER TABLE notifications DROP COLUMN {obsolete_column}"
                    )
            if "comment_watermark" not in columns:
                conn.execute("ALTER TABLE notifications ADD COLUMN comment_watermark TEXT")
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_source
                ON notifications (source_id)
                WHERE source_id IS NOT NULL AND source_id != ''
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_notifications_canonical
                ON notifications (canonical_artifact)
                WHERE canonical_artifact IS NOT NULL AND canonical_artifact != ''
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_health (
                  worker TEXT PRIMARY KEY,
                  updated_at TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL
                )
                """)
            conn.commit()

    def _find_row_id(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> int | None:
        if source_id:
            row = conn.execute(
                "SELECT id FROM notifications WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row:
                return int(row["id"])
        if not source_id and canonical_artifact:
            row = conn.execute(
                """
                SELECT id FROM notifications
                 WHERE canonical_artifact = ?
                   AND (source_id IS NULL OR source_id = '')
                """,
                (canonical_artifact,),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    def _insert_row(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
        classification: str,
        worker: str,
        title: str = "",
        reason: str = "",
        repo: str = "",
        now: str | None = None,
    ) -> int:
        timestamp = now or utcnow_iso()
        cur = conn.execute(
            """
            INSERT INTO notifications (
              source_id, canonical_artifact, title, reason, repo,
              classification, clear_state, first_seen_at,
              last_seen_at, classified_at, worker
            ) VALUES (?, ?, ?, ?, ?, ?, 'not_applicable', ?, ?, ?, ?)
            """,
            (
                source_id,
                canonical_artifact,
                title,
                reason,
                repo,
                classification,
                timestamp,
                timestamp,
                timestamp,
                worker,
            ),
        )
        if cur.lastrowid is None:
            raise RuntimeError("notification ledger insert did not return a row id")
        return cur.lastrowid

    def _claim_canonical_row(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        canonical_artifact: str | None,
    ) -> int | None:
        if not canonical_artifact:
            return None
        row = conn.execute(
            """
            UPDATE notifications
               SET source_id = ?
             WHERE id = (
                       SELECT MIN(id)
                         FROM notifications
                         WHERE canonical_artifact = ?
                           AND (source_id IS NULL OR source_id = '')
                           AND terminal_disposition IS NULL
                           AND clear_state = 'not_applicable'
                        HAVING COUNT(*) = 1
                   )
               AND NOT EXISTS (
                       SELECT 1
                         FROM notifications
                        WHERE source_id = ?
                   )
            RETURNING id
            """,
            (
                source_id,
                canonical_artifact,
                source_id,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def capture(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
        classification: str,
        worker: str,
        title: str = "",
        reason: str = "",
        repo: str = "",
        event_at: str | None = None,
    ) -> int:
        now = utcnow_iso()
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None and source_id:
                row_id = self._claim_canonical_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification=classification,
                    worker=worker,
                    title=title,
                    reason=reason,
                    repo=repo,
                    now=now,
                )
                conn.commit()
                return row_id
            conn.execute(
                """
                UPDATE notifications
                   SET source_id = COALESCE(NULLIF(source_id, ''), ?),
                       canonical_artifact = COALESCE(?, canonical_artifact),
                       title = CASE WHEN ? != '' THEN ? ELSE title END,
                       reason = CASE WHEN ? != '' THEN ? ELSE reason END,
                       repo = CASE WHEN ? != '' THEN ? ELSE repo END,
                        classification = ?,
                        last_seen_at = ?,
                        classified_at = ?,
                        worker = ?
                 WHERE id = ?
                """,
                (
                    source_id,
                    canonical_artifact,
                    title,
                    title,
                    reason,
                    reason,
                    repo,
                    repo,
                    classification,
                    now,
                    now,
                    worker,
                    row_id,
                ),
            )
            conn.commit()
            return row_id

    def link_tracker(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
        tracker_item_id: str,
        tracker_section: str,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="actionable",
                    worker="tracker-reconcile",
                    now=now,
                )
            conn.execute(
                """
                UPDATE notifications
                   SET tracker_item_id = ?,
                        tracker_section = ?,
                       tracker_linked_at = COALESCE(tracker_linked_at, ?),
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (tracker_item_id, tracker_section, now, now, row_id),
            )
            conn.commit()

    def reopen_actionable(
        self,
        *,
        source_id: str,
        reason: str,
        tracker_section: str,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE notifications
                   SET classification = 'actionable',
                       reason = ?,
                       tracker_section = ?,
                       terminal_disposition = NULL,
                       terminal_recorded_at = NULL,
                       clear_state = 'not_applicable',
                       clear_attempted_at = NULL,
                       cleared_at = NULL,
                       last_clear_error = NULL,
                       last_seen_at = ?,
                       classified_at = ?
                 WHERE source_id = ?
                """,
                (reason, tracker_section, now, now, source_id),
            )
            conn.commit()

    def suspend_pending_clear(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> None:
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        now = utcnow_iso()
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                return
            conn.execute(
                """
                UPDATE notifications
                   SET clear_state = 'not_applicable',
                       clear_attempted_at = NULL,
                       cleared_at = NULL,
                       last_clear_error = NULL,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (now, row_id),
            )
            conn.commit()

    def comment_watermark(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> str | None:
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                return None
            row = conn.execute(
                "SELECT comment_watermark FROM notifications WHERE id = ?",
                (row_id,),
            ).fetchone()
            if not row:
                return None
            watermark = row["comment_watermark"]
            return str(watermark) if watermark else None

    def notification_record(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> dict[str, Any] | None:
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                return None
            row = conn.execute(
                """
                SELECT first_seen_at, last_seen_at, terminal_recorded_at,
                       terminal_disposition, clear_state
                  FROM notifications
                 WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
            return dict(row) if row else None

    def record_comment_watermark(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
        comment_watermark: str,
    ) -> None:
        if not comment_watermark:
            return
        cursor = _parse_comment_cursor(comment_watermark)
        if cursor is None:
            return
        stream_key, stamp, comment_id = cursor
        now = utcnow_iso()
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="actionable",
                    worker="comment-watermark",
                    now=now,
                )
            row = conn.execute(
                "SELECT comment_watermark FROM notifications WHERE id = ?",
                (row_id,),
            ).fetchone()
            state = _parse_comment_watermarks(str(row["comment_watermark"]) if row and row["comment_watermark"] else None)
            state[stream_key] = (stamp, comment_id)
            payload = {
                stream: {
                    "ts": cursor_stamp.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if cursor_stamp else "",
                    "id": cursor_comment_id,
                }
                for stream, (cursor_stamp, cursor_comment_id) in state.items()
            }
            conn.execute(
                """
                UPDATE notifications
                   SET comment_watermark = ?,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (json.dumps(payload, separators=(",", ":"), sort_keys=True), now, row_id),
            )
            conn.commit()

    def record_terminal(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
        terminal_disposition: str,
        event_at: str | None = None,
    ) -> None:
        now = utcnow_iso()
        terminal_at = event_at or now
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="actionable",
                    worker="tracker-reconcile",
                    now=now,
                )
            conn.execute(
                """
                UPDATE notifications
                   SET terminal_disposition = ?,
                       terminal_recorded_at = CASE
                           WHEN terminal_recorded_at IS NULL
                           THEN ?
                           ELSE terminal_recorded_at
                       END,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (terminal_disposition, terminal_at, now, row_id),
            )
            conn.commit()

    def queue_clear(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="policy_drop",
                    worker="clear-queue",
                    now=now,
                )
            conn.execute(
                """
                UPDATE notifications
                   SET clear_state = CASE
                           WHEN clear_state IN ('succeeded', 'cleared') THEN clear_state
                           WHEN clear_state = 'failed' THEN clear_state
                           ELSE 'pending'
                        END,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (now, row_id),
            )
            conn.commit()

    def record_clear_success(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="policy_drop",
                    worker="clear-success",
                    now=now,
                )
            terminal_disposition = None
            row = conn.execute(
                """
                SELECT terminal_disposition
                  FROM notifications
                 WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
            if row is not None:
                terminal_disposition = row[0]
            clear_state = (
                'cleared' if terminal_disposition == 'tracked_elsewhere' else 'succeeded'
            )
            conn.execute(
                """
                UPDATE notifications
                   SET clear_state = ?,
                       last_clear_error = NULL,
                       clear_attempted_at = ?,
                       cleared_at = ?,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (clear_state, now, now, now, row_id),
            )
            conn.commit()

    def record_clear_failure(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
        error: str,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="policy_drop",
                    worker="clear-failure",
                    now=now,
                )
            conn.execute(
                """
                UPDATE notifications
                   SET clear_state = 'failed',
                       last_clear_error = ?,
                       clear_attempted_at = ?,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (error, now, now, row_id),
            )
            conn.commit()

    def _rows(self, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def has_active_actionable_notification(
        self,
        *,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> bool:
        canonical_artifact = (
            normalize_github_url(canonical_artifact) or canonical_artifact
        )
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                return False
            row = conn.execute(
                """
                SELECT classification, terminal_disposition, clear_state
                  FROM notifications
                 WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
            if not row:
                return False
            return (
                row["classification"] == "actionable"
                and row["terminal_disposition"] is None
                and row["clear_state"] == "not_applicable"
            )

    def pending_github_clears(self) -> list[dict[str, Any]]:
        return self._rows("""
            SELECT source_id, canonical_artifact, classification, reason, repo,
                   terminal_disposition, clear_state
              FROM notifications
             WHERE source_id IS NOT NULL
               AND source_id != ''
               AND clear_state IN ('pending', 'failed')
             ORDER BY first_seen_at ASC
            """)

    def health_metrics(self) -> dict[str, Any]:
        actionable_without_tracker_links = self._rows(
            """
            SELECT source_id, canonical_artifact, title, reason, repo,
                   classification, tracker_item_id, tracker_section,
                   terminal_disposition, clear_state
              FROM notifications
             WHERE classification = 'actionable'
               AND (tracker_item_id IS NULL OR tracker_item_id = '')
               AND terminal_disposition IS NULL
               AND clear_state = 'not_applicable'
             ORDER BY first_seen_at ASC
            """
        )
        clear_failures = self._rows(
            """
            SELECT source_id, canonical_artifact, title, reason, repo,
                   classification, tracker_item_id, tracker_section,
                   terminal_disposition, clear_state, last_clear_error
              FROM notifications
             WHERE clear_state = 'failed'
             ORDER BY first_seen_at ASC
            """
        )
        stale_dropped_items = self._rows(
            """
            SELECT source_id, canonical_artifact, title, reason, repo,
                   classification, tracker_item_id, tracker_section,
                   terminal_disposition, clear_state
              FROM notifications
             WHERE terminal_disposition = 'irrelevant'
               AND clear_state NOT IN ('succeeded', 'cleared')
             ORDER BY first_seen_at ASC
            """
        )
        notification_counts = self._rows(
            """
            SELECT
              COUNT(*) AS total,
              COALESCE(SUM(CASE WHEN clear_state = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
              COALESCE(SUM(CASE WHEN clear_state = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
              COALESCE(SUM(CASE WHEN clear_state IN ('succeeded', 'cleared') THEN 1 ELSE 0 END), 0) AS cleared,
              COALESCE(SUM(CASE WHEN classification = 'actionable' THEN 1 ELSE 0 END), 0) AS actionable,
              COALESCE(SUM(CASE WHEN terminal_disposition IS NOT NULL THEN 1 ELSE 0 END), 0) AS terminal
              FROM notifications
            """
        )
        counts = notification_counts[0] if notification_counts else {}
        return {
            "actionable_without_tracker_links": actionable_without_tracker_links,
            "clear_failures": clear_failures,
            "stale_dropped_items": stale_dropped_items,
            "notification_counts": counts,
        }

    def record_health_snapshot(self, *, worker: str, snapshot: dict[str, Any]) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_health (
                  worker TEXT PRIMARY KEY,
                  updated_at TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO notification_health (worker, updated_at, snapshot_json)
                VALUES (?, ?, ?)
                ON CONFLICT(worker) DO UPDATE SET
                  updated_at = excluded.updated_at,
                  snapshot_json = excluded.snapshot_json
                """,
                (worker, now, json.dumps(snapshot, sort_keys=True)),
            )
            conn.commit()

    def health_snapshot(self, *, worker: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT updated_at, snapshot_json
                      FROM notification_health
                     WHERE worker = ?
                    """,
                    (worker,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        payload = json.loads(row["snapshot_json"])
        payload["updated_at"] = row["updated_at"]
        payload["worker"] = worker
        return payload


@contextlib.contextmanager
def permit_dry_run_ledger_writes():
    global ALLOW_DRY_RUN_LEDGER_WRITES
    previous = ALLOW_DRY_RUN_LEDGER_WRITES
    ALLOW_DRY_RUN_LEDGER_WRITES = True
    try:
        yield
    finally:
        ALLOW_DRY_RUN_LEDGER_WRITES = previous


def ledger_capture(
    ledger: NotificationLedger | None,
    *,
    dry_run: bool,
    thread_id: str | None,
    canonical_artifact: str | None,
    classification: str,
    worker: str,
    title: str = "",
    reason: str = "",
    repo: str = "",
    tracker_item_id: str | None = None,
    tracker_section: str | None = None,
    terminal_disposition: str | None = None,
    queue_clear: bool = False,
    event_at: str | None = None,
) -> None:
    if ledger is None or (dry_run and not ALLOW_DRY_RUN_LEDGER_WRITES):
        return
    ledger.capture(
        source_id=thread_id,
        canonical_artifact=canonical_artifact,
        classification=classification,
        worker=worker,
        title=title,
        reason=reason,
        repo=repo,
        event_at=event_at,
    )
    if tracker_item_id and tracker_section:
        ledger.link_tracker(
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
            tracker_item_id=tracker_item_id,
            tracker_section=tracker_section,
        )
        if (
            worker == "tracker-link"
            and classification == "actionable"
            and terminal_disposition is None
            and thread_id
        ):
            ledger.reopen_actionable(
                source_id=thread_id,
                reason=reason or "actionable",
                tracker_section=tracker_section,
            )
    if terminal_disposition:
        ledger.record_terminal(
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
            terminal_disposition=terminal_disposition,
            event_at=event_at,
        )
    if queue_clear:
        ledger.queue_clear(
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
        )


def ledger_record_clear_result(
    ledger: NotificationLedger | None,
    *,
    dry_run: bool,
    thread_id: str,
    canonical_artifact: str | None,
    error: BaseException | None = None,
) -> None:
    if ledger is None or (dry_run and not ALLOW_DRY_RUN_LEDGER_WRITES):
        return
    if error is None:
        ledger.record_clear_success(
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
        )
        return
    ledger.record_clear_failure(
        source_id=thread_id,
        canonical_artifact=canonical_artifact,
        error=str(error),
    )
