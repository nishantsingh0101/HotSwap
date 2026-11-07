"""Durable delivery inbox. An interrupted command needs explicit reconciliation."""
import json
import sqlite3
import uuid


class DeliveryBlocked(RuntimeError):
    pass


class Journal:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,payload TEXT,state TEXT);")
        if not self.get("connection"):
            self.put("connection", str(uuid.uuid4()))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def enqueue(self, key, payload, cursor):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO inbox VALUES (?,?,'pending')", (key, json.dumps(payload)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('cursor',?)", (json.dumps(cursor),))

    def next(self):
        if self.get("blocked") or self.db.execute("SELECT 1 FROM inbox WHERE state='running'").fetchone():
            raise DeliveryBlocked("Interrupted or gapped Switch delivery. Use operator recovery after reconciling work.")
        row = self.db.execute("SELECT id,payload FROM inbox WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
        if row:
            self.db.execute("UPDATE inbox SET state='running' WHERE id=?", (row[0],))
            self.db.commit()
            return row[0], json.loads(row[1])

    def done(self, key):
        self.db.execute("UPDATE inbox SET state='done' WHERE id=?", (key,))
        self.db.commit()

    def abandon_for_recovery(self):
        # Only an operator may abandon old commands. Never replay an uncertain
        # command automatically; the gateway issues new, reconciled work instead.
        with self.db:
            self.db.execute("UPDATE inbox SET state='abandoned' WHERE state IN ('pending','running')")
            self.db.execute("DELETE FROM meta WHERE key IN ('blocked','generation','cursor')")
