"""Persistent state for the receiver (SQLite, standard library).

Two tables in one database:
  * seen_events - eventIds already accepted, so retries and duplicate
    deliveries are ignored (including across restarts). Bounded by age and rows.
  * queue       - items waiting to be shown on the bar, oldest first (FIFO).
    Bounded by a row cap (oldest dropped first) and an age limit (stale items
    are never shown as if they were new).

accept_discovery() records the eventId and enqueues the item in ONE transaction,
so a crash can never leave an event marked "seen" without its item queued.

Designed by N8JRD - Jim Davis, Vibe Coded with Claude
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class QueueItem:
    id: int
    kind: str          # "rfy" or "test"
    event_id: str
    title: str
    price: str
    enqueued_at: int
    attempts: int
    price_kind: str = "normal"


class Store:
    def __init__(self, path: str, ttl_seconds: int = 24 * 3600, max_rows: int = 10_000,
                 prune_interval: float = 60.0, queue_max: int = 25,
                 queue_expiry_seconds: int = 1800):
        self._ttl = ttl_seconds
        self._max_rows = max_rows
        self._prune_interval = prune_interval
        self._queue_max = queue_max
        self._queue_expiry = queue_expiry_seconds
        self._lock = threading.Lock()
        self._last_prune = 0.0  # time.monotonic() value, so clock changes can't stall pruning
        # autocommit mode: each statement is atomic; multi-statement work uses BEGIN/COMMIT
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None,
                                   timeout=5)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS seen_events ("
            " event_id TEXT PRIMARY KEY,"
            " first_seen INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_events(first_seen)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS queue ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " kind TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " title TEXT NOT NULL,"
            " price TEXT NOT NULL,"
            " enqueued_at INTEGER NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " price_kind TEXT NOT NULL DEFAULT 'normal')"
        )
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(queue)")]
        if "price_kind" not in cols:   # database created by the previous version
            self._db.execute(
                "ALTER TABLE queue ADD COLUMN price_kind TEXT NOT NULL DEFAULT 'normal'")
        with self._lock:
            self._prune(int(time.time()))

    # ------------------------------------------------------------------ dedupe
    def record_new(self, event_id: str, now: float | None = None) -> bool:
        """Atomically record an eventId. True = first time seen, False = duplicate."""
        ts = int(time.time() if now is None else now)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen_events(event_id, first_seen) VALUES (?, ?)",
                (event_id, ts),
            )
            is_new = cur.rowcount == 1
            self._maybe_prune(ts)
            return is_new

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]

    # ------------------------------------------------------------------- queue
    def accept_discovery(self, event_id: str, title: str, price: str,
                         price_kind: str = "normal",
                         now: float | None = None) -> tuple[bool, int]:
        """Record the eventId and enqueue the item atomically.

        Returns (is_new, dropped). is_new=False means a duplicate (nothing queued).
        dropped = how many OLDEST queue items were evicted to respect the cap.
        """
        ts = int(time.time() if now is None else now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO seen_events(event_id, first_seen) VALUES (?, ?)",
                    (event_id, ts),
                )
                if cur.rowcount != 1:
                    self._db.execute("COMMIT")
                    return False, 0
                dropped = self._enqueue_locked("rfy", event_id, title, price, ts, price_kind)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._maybe_prune(ts)
            return True, dropped

    def enqueue_test(self, event_id: str, title: str, price: str,
                     price_kind: str = "normal", now: float | None = None) -> int:
        """Queue a webhook.test item (never deduplicated). Returns items evicted."""
        ts = int(time.time() if now is None else now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                dropped = self._enqueue_locked("test", event_id, title, price, ts, price_kind)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return dropped

    def _enqueue_locked(self, kind: str, event_id: str, title: str, price: str,
                        ts: int, price_kind: str = "normal") -> int:
        self._db.execute(
            "INSERT INTO queue(kind, event_id, title, price, enqueued_at, price_kind) "
            "VALUES (?,?,?,?,?,?)",
            (kind, event_id, title, price, ts, price_kind),
        )
        total = self._db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        excess = total - self._queue_max
        if excess <= 0:
            return 0
        self._db.execute(
            "DELETE FROM queue WHERE id IN (SELECT id FROM queue ORDER BY id ASC LIMIT ?)",
            (excess,),
        )
        return excess

    def next_item(self, now: float | None = None) -> tuple[QueueItem | None, int]:
        """Expire stale items, then return the oldest remaining one (not removed).

        Returns (item_or_None, expired_count).
        """
        ts = int(time.time() if now is None else now)
        with self._lock:
            cur = self._db.execute("DELETE FROM queue WHERE enqueued_at < ?",
                                   (ts - self._queue_expiry,))
            expired = cur.rowcount
            row = self._db.execute(
                "SELECT id, kind, event_id, title, price, enqueued_at, attempts, price_kind "
                "FROM queue ORDER BY id ASC LIMIT 1").fetchone()
            return (QueueItem(*row) if row else None), expired

    def mark_done(self, item_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM queue WHERE id = ?", (item_id,))

    def bump_attempts(self, item_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE queue SET attempts = attempts + 1 WHERE id = ?",
                             (item_id,))

    def queue_len(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    # ------------------------------------------------------------- housekeeping
    def _maybe_prune(self, ts: int) -> None:
        if time.monotonic() - self._last_prune >= self._prune_interval:
            self._prune(ts)

    def _prune(self, now: int) -> None:
        self._db.execute("DELETE FROM seen_events WHERE first_seen < ?", (now - self._ttl,))
        total = self._db.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
        if total > self._max_rows:
            self._db.execute(
                "DELETE FROM seen_events WHERE event_id IN ("
                " SELECT event_id FROM seen_events ORDER BY first_seen ASC LIMIT ?)",
                (total - self._max_rows,),
            )
        self._last_prune = time.monotonic()

    def close(self) -> None:
        with self._lock:
            self._db.close()
