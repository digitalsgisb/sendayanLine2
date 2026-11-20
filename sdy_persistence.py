"""Durable runtime snapshots and an HTTP event outbox for Sendayan lines."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib import error, request


OUTBOX_STATUSES = ("PENDING", "SENDING", "SENT", "DEAD_LETTER")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductionSQLiteStore:
    """Thread-safe current-state snapshot plus durable event outbox."""

    def __init__(self, database_path: str) -> None:
        self.database_path = os.path.abspath(database_path)
        directory = os.path.dirname(self.database_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        self.recover_interrupted_deliveries()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    line_code TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    state_status TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    line_code TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','SENDING','SENT','DEAD_LETTER')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_delivery
                    ON outbox_events(status, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_outbox_shift
                    ON outbox_events(shift_id, event_type);
                CREATE TABLE IF NOT EXISTS google_queue (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    item_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        self._connection.execute("ROLLBACK")

    def _save_state(self, line_code: str, state: Dict[str, Any], status: str) -> None:
        self._connection.execute(
            """
            INSERT INTO runtime_state (
                line_code, state_json, state_status, state_version, updated_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(line_code) DO UPDATE SET
                state_json = excluded.state_json,
                state_status = excluded.state_status,
                state_version = runtime_state.state_version + 1,
                updated_at = excluded.updated_at
            """,
            (
                line_code,
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                status,
                utc_now_iso(),
            ),
        )

    def save_runtime_state(self, line_code: str, state: Dict[str, Any], status: str) -> None:
        with self._lock:
            self._begin()
            try:
                self._save_state(line_code, state, status)
                self._connection.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def load_runtime_state(self, line_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_state WHERE line_code = ?", (line_code,)
            ).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_json"])
        state["_sqlite_state_status"] = row["state_status"]
        state["_sqlite_state_version"] = row["state_version"]
        state["_sqlite_updated_at"] = row["updated_at"]
        return state

    def _insert_event(
        self,
        event_type: str,
        line_code: str,
        shift_id: str,
        payload: Dict[str, Any],
        event_id: Optional[str],
        occurred_at: Optional[str],
    ) -> str:
        created_at = utc_now_iso()
        stable_id = event_id or str(uuid.uuid4())
        self._connection.execute(
            """
            INSERT INTO outbox_events (
                event_id, event_type, line_code, shift_id, occurred_at,
                payload_json, status, attempt_count, next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
            """,
            (
                stable_id,
                event_type,
                line_code,
                shift_id,
                occurred_at or created_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created_at,
                created_at,
            ),
        )
        return stable_id

    def save_state_and_enqueue(
        self,
        line_code: str,
        state: Dict[str, Any],
        state_status: str,
        event_type: str,
        shift_id: str,
        payload: Dict[str, Any],
        event_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> str:
        with self._lock:
            self._begin()
            try:
                self._save_state(line_code, state, state_status)
                stable_id = self._insert_event(
                    event_type, line_code, shift_id, payload, event_id, occurred_at
                )
                self._connection.execute("COMMIT")
                return stable_id
            except Exception:
                self._rollback()
                raise

    def recover_interrupted_deliveries(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE outbox_events
                SET status = 'PENDING', next_attempt_at = ?,
                    last_error = COALESCE(last_error, 'Delivery interrupted by restart')
                WHERE status = 'SENDING'
                """,
                (utc_now_iso(),),
            )
            return cursor.rowcount

    def claim_next_event(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    """
                    SELECT * FROM outbox_events
                    WHERE status = 'PENDING'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY created_at, event_id LIMIT 1
                    """,
                    (utc_now_iso(),),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                self._connection.execute(
                    """
                    UPDATE outbox_events
                    SET status = 'SENDING', attempt_count = attempt_count + 1,
                        last_error = NULL
                    WHERE event_id = ?
                    """,
                    (row["event_id"],),
                )
                claimed = self._connection.execute(
                    "SELECT * FROM outbox_events WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchone()
                self._connection.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
        return {
            "event_id": claimed["event_id"],
            "event_type": claimed["event_type"],
            "line_code": claimed["line_code"],
            "shift_id": claimed["shift_id"],
            "occurred_at": claimed["occurred_at"],
            "payload": json.loads(claimed["payload_json"]),
            "attempt_count": claimed["attempt_count"],
        }

    def mark_sent(self, event_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE outbox_events SET status='SENT', sent_at=?, next_attempt_at=NULL WHERE event_id=?",
                (utc_now_iso(), event_id),
            )

    def mark_retry(self, event_id: str, message: str, delay_seconds: int) -> None:
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._lock:
            self._connection.execute(
                "UPDATE outbox_events SET status='PENDING', next_attempt_at=?, last_error=? WHERE event_id=?",
                (next_attempt.isoformat(), message[:1000], event_id),
            )

    def mark_dead_letter(self, event_id: str, message: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE outbox_events SET status='DEAD_LETTER', next_attempt_at=NULL, last_error=? WHERE event_id=?",
                (message[:1000], event_id),
            )

    def get_outbox_counts(self) -> Dict[str, int]:
        counts = {status: 0 for status in OUTBOX_STATUSES}
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS total FROM outbox_events GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = row["total"]
        return counts

    # --- Google Sheets queue: survives restarts, strictly first-in first-out ---
    def enqueue_google(self, item: Dict[str, Any]) -> bool:
        """Store one Sheets request. A repeated request_id is ignored (same logical row)."""
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO google_queue (request_id, item_json, created_at) VALUES (?, ?, ?)",
                (item["request_id"], json.dumps(item, ensure_ascii=False, default=str), utc_now_iso()),
            )
            return cursor.rowcount == 1

    def peek_google(self) -> Optional[Dict[str, Any]]:
        """Oldest queued request; later ones wait so a shift PDF never overtakes its rows."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM google_queue ORDER BY queue_id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        item = json.loads(row["item_json"])
        item["queue_id"] = row["queue_id"]
        item["attempt_count"] = row["attempt_count"]
        item["next_retry_at"] = row["next_attempt_at"]
        return item

    def complete_google(self, queue_id: int) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM google_queue WHERE queue_id = ?", (queue_id,))

    def retry_google(self, queue_id: int, attempt_count: int, next_attempt_at: float, message: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE google_queue SET attempt_count=?, next_attempt_at=?, last_error=? WHERE queue_id=?",
                (attempt_count, next_attempt_at, message[:1000], queue_id),
            )

    def google_queue_count(self) -> int:
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) FROM google_queue").fetchone()[0]

    # --- Small key/value settings pushed from Smart Checksheet ---
    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT setting_value FROM settings WHERE setting_key = ?", (key,)
            ).fetchone()
        return None if row is None else row["setting_value"]

    def set_setting(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            if value is None:
                self._connection.execute("DELETE FROM settings WHERE setting_key = ?", (key,))
            else:
                self._connection.execute(
                    """INSERT INTO settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value, updated_at = excluded.updated_at""",
                    (key, value, utc_now_iso()),
                )

    # --- Outbox retention ---
    def prune_finished_events(self, api_enabled: bool, older_than_days: int = 7) -> int:
        """Delete outbox events older than N days that every consumer has finished with.

        Consumers: the optional analytics API sender (status SENT/DEAD_LETTER, or
        disabled) and the Smart Checksheet relay, which keeps its own
        checksheet_deliveries ledger in this database. Until that relay is
        installed nothing is deleted, so it can still deliver the history.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            relay_installed = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checksheet_deliveries'"
            ).fetchone() is not None
            if not relay_installed:
                return 0
            api_done = "1" if not api_enabled else "e.status IN ('SENT','DEAD_LETTER')"
            self._begin()
            try:
                event_ids = [row[0] for row in self._connection.execute(
                    f"""SELECT e.event_id FROM outbox_events e
                    JOIN checksheet_deliveries d ON d.event_id = e.event_id
                    WHERE d.delivered_at IS NOT NULL AND e.created_at < ? AND {api_done}""",
                    (cutoff,),
                ).fetchall()]
                for event_id in event_ids:
                    self._connection.execute("DELETE FROM outbox_events WHERE event_id = ?", (event_id,))
                    self._connection.execute("DELETE FROM checksheet_deliveries WHERE event_id = ?", (event_id,))
                self._connection.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
        return len(event_ids)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class ProductionOutboxSender(threading.Thread):
    def __init__(
        self,
        store: ProductionSQLiteStore,
        api_url: str,
        api_key: str,
        enabled: bool,
        thread_name: str,
    ) -> None:
        super().__init__(name=thread_name, daemon=True)
        self.store = store
        self.api_url = api_url.strip()
        self.api_key = api_key
        self.enabled = enabled and bool(self.api_url)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self.enabled:
            return
        while not self._stop_event.is_set():
            event_record = self.store.claim_next_event()
            if event_record is None:
                self._stop_event.wait(2.0)
                continue
            event_id = event_record["event_id"]
            try:
                headers = {"Content-Type": "application/json"}
                if self.api_key:
                    headers["X-API-Key"] = self.api_key
                api_request = request.Request(
                    self.api_url,
                    data=json.dumps(event_record, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with request.urlopen(api_request, timeout=5.0) as response:
                    if 200 <= response.getcode() < 300:
                        self.store.mark_sent(event_id)
                        print(f"[OUTBOX] Sent {event_record['event_type']} ({event_id}).")
                    else:
                        self._retry(event_record, f"HTTP {response.getcode()}")
            except error.HTTPError as exc:
                message = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
                if exc.code in (408, 425, 429) or exc.code >= 500:
                    self._retry(event_record, message)
                else:
                    self.store.mark_dead_letter(event_id, message)
            except Exception as exc:
                self._retry(event_record, str(exc))

    def _retry(self, event_record: Dict[str, Any], message: str) -> None:
        attempts = int(event_record.get("attempt_count", 1))
        delay_seconds = min(300, 2 ** min(attempts, 8))
        self.store.mark_retry(event_record["event_id"], message, delay_seconds)
        print(f"[OUTBOX] Delivery failed; retrying in {delay_seconds}s: {message}")
