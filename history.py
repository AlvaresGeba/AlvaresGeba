"""
history.py

Journal of every command the gateway handled - typed or spoken, what was
heard, what Claude did (each tool call) and how it ended. Backs the app's
/history endpoints. SQLite, one file under data/ (a docker volume).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           TEXT PRIMARY KEY,
    started_at   REAL NOT NULL,
    channel      TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    transcript   TEXT NOT NULL,
    result_short TEXT NOT NULL,
    detail       TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    step_count   INTEGER NOT NULL,
    steps        TEXT NOT NULL,
    elevation    TEXT NOT NULL,
    took_ms      INTEGER NOT NULL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS requests_started ON requests(started_at);
"""

_LIST_COLUMNS = ("id, started_at, channel, device_id, transcript, result_short, "
                 "ok, step_count, elevation, took_ms, error")


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def record(self, *, id: str, started_at: float, channel: str, device_id: str,
               transcript: str, result_short: str, detail: str, ok: bool,
               steps: list[dict], elevation: str, took_ms: int, error: str | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id, started_at, channel, device_id, transcript, result_short, detail,
                 int(ok), len(steps), json.dumps(steps, ensure_ascii=False), elevation,
                 took_ms, error),
            )
            self._db.commit()

    def recent(self, limit: int = 50, query: str = "", failures: bool = False) -> list[dict]:
        sql = f"SELECT {_LIST_COLUMNS} FROM requests WHERE 1=1"
        args: list = []
        if query:
            sql += " AND (transcript LIKE ? OR result_short LIKE ? OR device_id LIKE ?)"
            like = f"%{query}%"
            args += [like, like, like]
        if failures:
            sql += " AND ok = 0"
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args)]

    def get(self, request_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["steps"] = json.loads(out["steps"])
        return out

    def stats(self, hours: int = 24) -> dict:
        since = time.time() - max(1, hours) * 3600
        with self._lock:
            total, ok, avg_ms = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(ok), 0), COALESCE(AVG(took_ms), 0) "
                "FROM requests WHERE started_at >= ?", (since,)).fetchone()
            by_channel = dict(self._db.execute(
                "SELECT channel, COUNT(*) FROM requests WHERE started_at >= ? GROUP BY channel",
                (since,)).fetchall())
            by_device = dict(self._db.execute(
                "SELECT device_id, COUNT(*) FROM requests WHERE started_at >= ? GROUP BY device_id",
                (since,)).fetchall())
        return {
            "hours": hours,
            "total": total,
            "ok": ok,
            "failed": total - ok,
            "avg_took_ms": int(avg_ms),
            "by_channel": by_channel,
            "by_device": by_device,
        }
