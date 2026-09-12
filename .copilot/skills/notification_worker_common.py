#!/usr/bin/env python3
"""Shared helpers for the GitHub notification workers.

This module intentionally uses only the standard library so the shell
wrappers can rely on it after the pinned runtime passes import preflight.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


HOME = Path.home()
DEFAULT_RUNTIME_ROOT = _path_from_env(
    "DOTFILES_NOTIFICATION_RUNTIME_ROOT",
    HOME / ".local" / "share" / "dotfiles" / "notification-workers",
)
DEFAULT_RUNTIME_PYTHON = _path_from_env(
    "DOTFILES_NOTIFICATION_RUNTIME_PYTHON",
    DEFAULT_RUNTIME_ROOT / "venv" / "bin" / "python3",
)
DEFAULT_SUPPORT_DIR = _path_from_env(
    "DOTFILES_NOTIFICATION_SUPPORT_DIR",
    HOME / "Library" / "Application Support" / "notification-workers",
)
DEFAULT_LOG_DIR = _path_from_env(
    "DOTFILES_NOTIFICATION_LOG_DIR",
    HOME / "Library" / "Logs",
)
DEFAULT_LEDGER_FILE = _path_from_env(
    "DOTFILES_NOTIFICATION_LEDGER_FILE",
    DEFAULT_SUPPORT_DIR / "ledger.sqlite",
)
DEFAULT_NOTIFICATION_HEALTH_FILE = _path_from_env(
    "DOTFILES_NOTIFICATION_HEALTH_FILE",
    DEFAULT_LOG_DIR / "notification-triage-health.json",
)
DEFAULT_DEPENDABOT_HEALTH_FILE = _path_from_env(
    "DOTFILES_DEPENDABOT_HEALTH_FILE",
    DEFAULT_LOG_DIR / "triage-dependabot-health.json",
)


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


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    except OSError:
        return {}


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

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
                  lifecycle_state TEXT NOT NULL DEFAULT 'captured',
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
                  worker TEXT NOT NULL DEFAULT '',
                  payload_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_source
                ON notifications (source_type, source_id)
                WHERE source_id IS NOT NULL AND source_id != ''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_canonical
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
        if canonical_artifact:
            row = conn.execute(
                "SELECT id FROM notifications WHERE source_type = ? AND canonical_artifact = ?",
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
        payload_json: str = "{}",
        now: str | None = None,
    ) -> int:
        timestamp = now or utcnow_iso()
        cur = conn.execute(
            """
            INSERT INTO notifications (
              source_type, source_id, canonical_artifact, title, reason, repo,
              classification, lifecycle_state, clear_state, first_seen_at,
              last_seen_at, classified_at, worker, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'captured', 'not_applicable', ?, ?, ?, ?, ?)
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
                payload_json,
            ),
        )
        return int(cur.lastrowid)

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
        payload: dict[str, Any] | None = None,
    ) -> int:
        now = utcnow_iso()
        canonical_artifact = normalize_github_url(canonical_artifact) or canonical_artifact
        payload_json = json.dumps(payload or {}, sort_keys=True)
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
                    classification=classification,
                    worker=worker,
                    title=title,
                    reason=reason,
                    repo=repo,
                    payload_json=payload_json,
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
                       worker = ?,
                       payload_json = ?
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
                    payload_json,
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
                       lifecycle_state = 'tracker_linked',
                       tracker_linked_at = COALESCE(tracker_linked_at, ?),
                       last_seen_at = ?
                 WHERE id = ?
                """,
                (tracker_item_id, tracker_section, now, now, row_id),
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
                       lifecycle_state = CASE
                           WHEN clear_state = 'succeeded' THEN 'cleared'
                           ELSE 'terminal_recorded'
                       END,
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
                       lifecycle_state = CASE
                           WHEN clear_state = 'succeeded' THEN 'cleared'
                           ELSE 'clear_pending'
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
                       lifecycle_state = 'cleared',
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
                       lifecycle_state = 'clear_pending',
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

    def rows_missing_tracker_links(self, *, source_type: str = "github") -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT source_id, canonical_artifact, reason, repo, classification,
                   first_seen_at, last_seen_at
              FROM notifications
             WHERE source_type = ?
               AND classification = 'actionable'
               AND (tracker_item_id IS NULL OR tracker_item_id = '')
               AND terminal_disposition IS NULL
             ORDER BY first_seen_at ASC
            """,
            (source_type,),
        )

    def rows_with_clear_failures(self, *, source_type: str = "github") -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT source_id, canonical_artifact, reason, repo, terminal_disposition,
                   clear_state, last_clear_error, clear_attempted_at
              FROM notifications
             WHERE source_type = ?
               AND clear_state = 'failed'
             ORDER BY clear_attempted_at DESC
            """,
            (source_type,),
        )

    def rows_with_stale_irrelevant_items(
        self, *, source_type: str = "github"
    ) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT source_id, canonical_artifact, reason, repo, tracker_item_id,
                   clear_state, terminal_recorded_at, last_clear_error
              FROM notifications
             WHERE source_type = ?
               AND terminal_disposition = 'irrelevant'
               AND clear_state != 'succeeded'
             ORDER BY terminal_recorded_at ASC
            """,
            (source_type,),
        )

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

    def row_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM notifications").fetchone()
        return int(row["count"] if row else 0)


def update_health_file(
    path: Path,
    *,
    worker: str,
    had_errors: bool,
    summary: dict[str, Any],
    details: dict[str, Any],
) -> None:
    now = utcnow_iso()
    previous = load_json(path)
    payload: dict[str, Any] = {
        "worker": worker,
        "status": "error" if had_errors else "ok",
        "last_run_at": now,
        "last_success_at": previous.get("last_success_at"),
        "last_error_at": previous.get("last_error_at"),
        "last_error": previous.get("last_error"),
        "summary": summary,
        "details": details,
    }
    if had_errors:
        errors = summary.get("errors") or []
        payload["last_error_at"] = now
        payload["last_error"] = errors[0] if errors else f"{worker} run failed"
    else:
        payload["last_success_at"] = now
    write_json_atomic(path, payload)
