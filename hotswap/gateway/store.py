"""SQLite state for the gateway. One connection, guarded by a lock."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    number INTEGER NOT NULL,
    holder TEXT,
    phase TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
    request_id TEXT PRIMARY KEY,
    lease INTEGER NOT NULL,
    agent TEXT NOT NULL,
    op TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worklog (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE NOT NULL,
    lease INTEGER NOT NULL,
    agent TEXT NOT NULL,
    step INTEGER,
    status TEXT NOT NULL,
    commit_sha TEXT,
    note TEXT,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    provider TEXT,
    vendor TEXT,
    pid INTEGER,
    state TEXT,
    step INTEGER,
    note TEXT,
    last_heartbeat REAL,
    last_progress REAL,
    errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    code_sha TEXT NOT NULL,
    test_sha TEXT,
    passed TEXT NOT NULL,
    failed TEXT NOT NULL,
    detail TEXT,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_sha TEXT NOT NULL,
    test_sha TEXT NOT NULL,
    req_version TEXT NOT NULL,
    authors TEXT NOT NULL,
    attested_by TEXT NOT NULL,
    evidence TEXT NOT NULL,
    accepted_by TEXT,
    accepted_at REAL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prs (
    number INTEGER PRIMARY KEY AUTOINCREMENT,
    head TEXT NOT NULL,
    base TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        with self.lock:
            if self.db.execute("SELECT 1 FROM lease WHERE id = 1").fetchone() is None:
                self.db.execute("INSERT INTO lease VALUES (1, 0, NULL, 'idle', ?)", (time.time(),))
            self.db.commit()

    # Generic helpers -----------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cursor = self.db.execute(sql, params)
            self.db.commit()
            return cursor

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    # Lease ---------------------------------------------------------------

    def lease(self) -> dict:
        return self.one("SELECT number, holder, phase, updated_at FROM lease WHERE id = 1")

    def set_lease(self, number: int | None = None, holder: str | None = ..., phase: str | None = None) -> dict:
        current = self.lease()
        number = current["number"] if number is None else number
        holder = current["holder"] if holder is ... else holder
        phase = current["phase"] if phase is None else phase
        self.execute(
            "UPDATE lease SET number = ?, holder = ?, phase = ?, updated_at = ? WHERE id = 1",
            (number, holder, phase, time.time()),
        )
        return self.lease()

    # Intents -------------------------------------------------------------

    def intent(self, request_id: str) -> dict | None:
        row = self.one("SELECT * FROM intents WHERE request_id = ?", (request_id,))
        if row:
            row["payload"] = json.loads(row["payload"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    def add_intent(self, request_id: str, lease: int, agent: str, op: str, payload: dict) -> None:
        now = time.time()
        self.execute(
            "INSERT INTO intents VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
            (request_id, lease, agent, op, json.dumps(payload), now, now),
        )

    def finish_intent(self, request_id: str, status: str, result: dict | None = None) -> None:
        self.execute(
            "UPDATE intents SET status = ?, result = ?, updated_at = ? WHERE request_id = ?",
            (status, json.dumps(result) if result is not None else None, time.time(), request_id),
        )

    def intents(self, status: str | None = None) -> list[dict]:
        rows = (
            self.all("SELECT * FROM intents WHERE status = ? ORDER BY created_at", (status,))
            if status
            else self.all("SELECT * FROM intents ORDER BY created_at")
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
        return rows

    # Events --------------------------------------------------------------

    def add_event(self, kind: str, data: dict) -> dict:
        at = time.time()
        cursor = self.execute("INSERT INTO events (at, kind, data) VALUES (?, ?, ?)", (at, kind, json.dumps(data)))
        return {"seq": cursor.lastrowid, "at": at, "kind": kind, "data": data}

    def events(self, after: int = 0, limit: int = 500) -> list[dict]:
        rows = self.all("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    # Reset ---------------------------------------------------------------

    def reset_run(self) -> None:
        """Clears everything about the current task run, keeping nothing from it."""
        with self.lock:
            for table in ("intents", "worklog", "test_runs", "ledger", "prs", "events"):
                self.db.execute(f"DELETE FROM {table}")
            # Event sequence and authority epochs must never move backwards:
            # browsers and old workers can outlive an explicit fixture reset.
            self.db.execute("DELETE FROM kv WHERE key LIKE 'run.%'")
            self.db.execute("UPDATE agents SET state = NULL, step = NULL, note = NULL, errors = '[]', last_progress = NULL")
            self.db.execute("UPDATE lease SET holder = NULL, phase = 'idle', updated_at = ? WHERE id = 1", (time.time(),))
            self.db.commit()
