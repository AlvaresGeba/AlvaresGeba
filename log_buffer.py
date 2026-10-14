"""
log_buffer.py

The server log the app shows on its Logs screen.

Two sources go into one ring buffer:
    gateway  - this process's own logging records
    audit    - command_audit.log, written by the executor and mounted
               read-only here; tailed so every allowed/refused command
               shows up live

/logs/recent reads the buffer; /logs/stream (Server-Sent Events) sends a
backlog and then every new entry. A subscriber that cannot keep up loses
entries instead of slowing everyone down, and the next entry it does get
carries "dropped": true.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_AUDIT_LINE = re.compile(r"^(\S+ \S+) \| (\w+) \| (.*)$")


def level_no(name: str) -> int:
    return LEVELS.get((name or "").upper(), 10)


class LogBuffer:
    def __init__(self, size: int = 2000) -> None:
        self._entries: deque[dict] = deque(maxlen=size)
        self._seq = itertools.count(1)
        self._subscribers: set[_Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Records can arrive from any thread; they are handed to
        subscribers on this loop."""
        self._loop = loop

    def add(self, level: str, logger: str, msg: str, source: str, ts: float | None = None) -> dict:
        entry = {
            "seq": next(self._seq),
            "ts": ts if ts is not None else time.time(),
            "level": level,
            "logger": logger,
            "msg": msg,
            "source": source,
        }
        self._entries.append(entry)
        loop = self._loop
        if loop is not None and self._subscribers:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._publish(entry)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(self._publish, entry)
        return entry

    def _publish(self, entry: dict) -> None:
        for sub in list(self._subscribers):
            sub.offer(entry)

    def recent(self, limit: int = 300, level: str = "DEBUG", query: str = "") -> list[dict]:
        matches = [e for e in self._entries if _matches(e, level, query)]
        return matches[-max(1, min(limit, 2000)):]

    def subscribe(self, level: str, query: str) -> "_Subscriber":
        sub = _Subscriber(level, query)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: "_Subscriber") -> None:
        self._subscribers.discard(sub)


def _matches(entry: dict, level: str, query: str) -> bool:
    if level_no(entry["level"]) < level_no(level):
        return False
    if query:
        q = query.lower()
        return q in entry["msg"].lower() or q in entry["logger"].lower()
    return True


class _Subscriber:
    def __init__(self, level: str, query: str) -> None:
        self.level, self.query = level, query
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        self._dropped = False

    def offer(self, entry: dict) -> None:
        if not _matches(entry, self.level, self.query):
            return
        if self._dropped:
            entry = {**entry, "dropped": True}
        try:
            self.queue.put_nowait(entry)
            self._dropped = False
        except asyncio.QueueFull:
            self._dropped = True


class BufferHandler(logging.Handler):
    """Feeds this process's logging records into the buffer."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__(level=logging.DEBUG)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            self.buffer.add(record.levelname, record.name, msg, "gateway", record.created)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


async def tail_audit_log(buffer: LogBuffer, path: Path, interval: float = 1.0) -> None:
    """Follow the executor's audit log (survives truncation and the file
    not existing yet). Starts at the end: older lines are not replayed."""
    position: int | None = None
    partial = ""
    while True:
        try:
            size = path.stat().st_size
            if position is None or size < position:
                position = size if position is None else 0
                partial = ""
            if size > position:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(position)
                    chunk = f.read()
                    position = f.tell()
                lines = (partial + chunk).split("\n")
                partial = lines.pop()
                for line in lines:
                    if line.strip():
                        _add_audit_line(buffer, line)
        except FileNotFoundError:
            position = None
        except Exception:  # noqa: BLE001 - keep tailing whatever happens
            logging.getLogger("gateway").exception("Audit log tail failed")
        await asyncio.sleep(interval)


def _add_audit_line(buffer: LogBuffer, line: str) -> None:
    m = _AUDIT_LINE.match(line)
    if not m:
        buffer.add("INFO", "command_audit", line, "audit")
        return
    stamp, level, msg = m.groups()
    try:
        ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except ValueError:
        ts = None
    buffer.add(level.upper(), "command_audit", msg, "audit", ts)
