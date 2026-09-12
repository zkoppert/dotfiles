#!/usr/bin/env python3
"""Shared helpers for the GitHub notification workers.

This module intentionally uses only the standard library so the shell
wrappers can rely on it after the pinned runtime passes import preflight.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


HOME = Path.home()
DEFAULT_SUPPORT_DIR = HOME / "Library" / "Application Support" / "notification-workers"
DEFAULT_LEDGER_FILE = DEFAULT_SUPPORT_DIR / "ledger.sqlite"


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
    return add_business_days(parsed, 1).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_type TEXT NOT NULL,
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
                  worker TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(notifications)")
            }
            for obsolete_column in ("lifecycle_state", "payload_json"):
                if obsolete_column in columns:
                    conn.execute(
                        f"ALTER TABLE notifications DROP COLUMN {obsolete_column}"
                    )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_source
                ON notifications (source_type, source_id)
                WHERE source_id IS NOT NULL AND source_id != ''
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_notifications_canonical")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notifications_canonical
                ON notifications (source_type, canonical_artifact)
                WHERE canonical_artifact IS NOT NULL AND canonical_artifact != ''
                """
            )
            conn.commit()

    def _find_row_id(
        self,
        conn: sqlite3.Connection,
        *,
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> int | None:
        if source_id:
            row = conn.execute(
                "SELECT id FROM notifications WHERE source_type = ? AND source_id = ?",
                (source_type, source_id),
            ).fetchone()
            if row:
                return int(row["id"])
        if not source_id and canonical_artifact:
            row = conn.execute(
                """
                SELECT id FROM notifications
                 WHERE source_type = ?
                   AND canonical_artifact = ?
                   AND (source_id IS NULL OR source_id = '')
                """,
                (source_type, canonical_artifact),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    def _insert_row(
        self,
        conn: sqlite3.Connection,
        *,
        source_type: str,
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
              source_type, source_id, canonical_artifact, title, reason, repo,
              classification, clear_state, first_seen_at,
              last_seen_at, classified_at, worker
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'not_applicable', ?, ?, ?, ?)
            """,
            (
                source_type,
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
        return int(cur.lastrowid)

    def _claim_canonical_row(
        self,
        conn: sqlite3.Connection,
        *,
        source_type: str,
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
                         WHERE source_type = ?
                           AND canonical_artifact = ?
                           AND (source_id IS NULL OR source_id = '')
                           AND terminal_disposition IS NULL
                           AND clear_state = 'not_applicable'
                        HAVING COUNT(*) = 1
                   )
               AND NOT EXISTS (
                       SELECT 1
                         FROM notifications
                        WHERE source_type = ?
                          AND source_id = ?
                   )
            RETURNING id
            """,
            (
                source_id,
                source_type,
                canonical_artifact,
                source_type,
                source_id,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def capture(
        self,
        *,
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
        classification: str,
        worker: str,
        title: str = "",
        reason: str = "",
        repo: str = "",
    ) -> int:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_type=source_type,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None and source_id:
                row_id = self._claim_canonical_row(
                    conn,
                    source_type=source_type,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_type=source_type,
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
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
        tracker_item_id: str,
        tracker_section: str,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_type=source_type,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_type=source_type,
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
        source_type: str,
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
                 WHERE source_type = ?
                   AND source_id = ?
                """,
                (reason, tracker_section, now, now, source_type, source_id),
            )
            conn.commit()

    def record_terminal(
        self,
        *,
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
        terminal_disposition: str,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_type=source_type,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_type=source_type,
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
                        terminal_recorded_at = COALESCE(terminal_recorded_at, ?),
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (terminal_disposition, now, now, row_id),
            )
            conn.commit()

    def queue_clear(
        self,
        *,
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_type=source_type,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_type=source_type,
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
                           WHEN clear_state = 'succeeded' THEN clear_state
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
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_type=source_type,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_type=source_type,
                    source_id=source_id,
                    canonical_artifact=canonical_artifact,
                    classification="policy_drop",
                    worker="clear-success",
                    now=now,
                )
            conn.execute(
                """
                UPDATE notifications
                   SET clear_state = 'succeeded',
                       last_clear_error = NULL,
                       clear_attempted_at = ?,
                       cleared_at = ?,
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (now, now, now, row_id),
            )
            conn.commit()

    def record_clear_failure(
        self,
        *,
        source_type: str,
        source_id: str | None,
        canonical_artifact: str | None,
        error: str,
    ) -> None:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        with self._connect() as conn:
            row_id = self._find_row_id(
                conn,
                source_type=source_type,
                source_id=source_id,
                canonical_artifact=canonical_artifact,
            )
            if row_id is None:
                row_id = self._insert_row(
                    conn,
                    source_type=source_type,
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

    def pending_github_clears(self) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT source_id, canonical_artifact, classification, reason, repo,
                   terminal_disposition, clear_state
              FROM notifications
             WHERE source_type = 'github'
               AND source_id IS NOT NULL
               AND source_id != ''
               AND clear_state IN ('pending', 'failed')
             ORDER BY first_seen_at ASC
            """
        )

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
) -> None:
    if ledger is None or dry_run:
        return
    ledger.capture(
        source_type="github",
        source_id=thread_id,
        canonical_artifact=canonical_artifact,
        classification=classification,
        worker=worker,
        title=title,
        reason=reason,
        repo=repo,
    )
    if tracker_item_id and tracker_section:
        ledger.link_tracker(
            source_type="github",
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
            tracker_item_id=tracker_item_id,
            tracker_section=tracker_section,
        )
    if terminal_disposition:
        ledger.record_terminal(
            source_type="github",
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
            terminal_disposition=terminal_disposition,
        )
    if queue_clear:
        ledger.queue_clear(
            source_type="github",
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
    if ledger is None or dry_run:
        return
    if error is None:
        ledger.record_clear_success(
            source_type="github",
            source_id=thread_id,
            canonical_artifact=canonical_artifact,
        )
        return
    ledger.record_clear_failure(
        source_type="github",
        source_id=thread_id,
        canonical_artifact=canonical_artifact,
        error=str(error),
    )
