"""Cross-conversation caller memory, keyed by the VERIFIED policyholder (party_id).

Written only after identity verification and read only after the next verification, so it can never
leak to an unverified caller. One record per policyholder holding their recent contacts (newest first).

Backend: Postgres when DATABASE_URL is set (the hosted demo), otherwise a local SQLite file
(MEMORY_DB_PATH, default data/caller_memory.db). Set MEMORY_DISABLED=1 to turn it off.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_CONTACTS = 5
_lock = threading.Lock()


class MemoryStore:
    def __init__(self, url: str | None = None, sqlite_path: str | None = None):
        self.url = url if url is not None else os.getenv("DATABASE_URL", "")
        self.enabled = os.getenv("MEMORY_DISABLED", "") not in ("1", "true", "yes")
        if self.url:
            import psycopg  # only needed for the hosted Postgres backend
            self._psycopg = psycopg
            with self._pg() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS caller_memory (party_id TEXT PRIMARY KEY, "
                             "data TEXT NOT NULL, updated_at TEXT NOT NULL)")
        else:
            path = Path(sqlite_path or os.getenv("MEMORY_DB_PATH", config.APP_ROOT / "data" / "caller_memory.db"))
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(path)
            with self._sqlite() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS caller_memory (party_id TEXT PRIMARY KEY, "
                             "data TEXT NOT NULL, updated_at TEXT NOT NULL)")

    # --- connections
    def _pg(self):
        return self._psycopg.connect(self.url, autocommit=True)

    def _sqlite(self):
        conn = sqlite3.connect(self.path)
        conn.isolation_level = None
        return conn

    def _query(self, sql: str, params: tuple):
        if self.url:
            with self._pg() as conn:
                cur = conn.execute(sql.replace("?", "%s"), params)
                return cur.fetchall() if cur.description else []
        with self._sqlite() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # --- API
    def load(self, party_id: str) -> list[dict]:
        """Previous contacts for a verified policyholder, newest first."""
        if not self.enabled:
            return []
        try:
            rows = self._query("SELECT data FROM caller_memory WHERE party_id = ?", (party_id,))
            return json.loads(rows[0][0]).get("contacts", []) if rows else []
        except Exception:
            return []          # memory is a convenience; never break the call over it

    def save_contact(self, party_id: str, contact: dict) -> None:
        """Insert or update this conversation's entry (matched by contact id)."""
        if not self.enabled:
            return
        with _lock:
            try:
                rows = self._query("SELECT data FROM caller_memory WHERE party_id = ?", (party_id,))
                contacts = json.loads(rows[0][0]).get("contacts", []) if rows else []
                contacts = [c for c in contacts if c.get("contact_id") != contact["contact_id"]]
                contacts = ([contact] + contacts)[:MAX_CONTACTS]
                data = json.dumps({"contacts": contacts})
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._query("INSERT INTO caller_memory (party_id, data, updated_at) VALUES (?, ?, ?) "
                            "ON CONFLICT (party_id) DO UPDATE SET data = excluded.data, "
                            "updated_at = excluded.updated_at", (party_id, data, now))
            except Exception:
                pass

    def forget(self, party_id: str) -> None:
        self._query("DELETE FROM caller_memory WHERE party_id = ?", (party_id,))


_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
