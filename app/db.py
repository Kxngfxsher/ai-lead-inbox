"""Хранилище на SQLite: карточки обращений и журнал событий (аудит действий ИИ и оператора)."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schemas import Analysis, IncomingMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- new | answered | archived
    category TEXT NOT NULL,
    urgency TEXT NOT NULL,
    lead_score INTEGER NOT NULL,
    priority TEXT NOT NULL,
    queue TEXT NOT NULL,
    assignee TEXT,
    sla_due_at TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    analysis_json TEXT NOT NULL,
    reply_text TEXT,
    answered_at TEXT,
    corrected_from TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_message ON events(message_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _columns(analysis: Analysis, received_at: datetime) -> dict:
    """Поля, вынесенные из анализа в отдельные колонки для фильтров и сортировки."""
    sla = analysis.routing.sla_minutes
    return {
        "category": analysis.extraction.category.value,
        "urgency": analysis.extraction.urgency,
        "lead_score": analysis.score.value,
        "priority": analysis.routing.priority,
        "queue": analysis.routing.queue,
        "assignee": analysis.routing.assignee,
        "sla_due_at": iso(received_at + timedelta(minutes=sla)) if sla else None,
        "needs_review": int(analysis.needs_review),
        "analysis_json": analysis.model_dump_json(),
    }


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- обращения ---------------------------------------------------------------------------

    def insert(self, msg: IncomingMessage, analysis: Analysis, received_at: datetime, status: str) -> int:
        row = {
            "channel": msg.channel,
            "sender": msg.sender,
            "subject": msg.subject,
            "body": msg.body,
            "received_at": iso(received_at),
            "status": status,
            **_columns(analysis, received_at),
        }
        names = ", ".join(row)
        marks = ", ".join(f":{name}" for name in row)
        with self._conn() as conn:
            return conn.execute(f"INSERT INTO messages ({names}) VALUES ({marks})", row).lastrowid

    def update_analysis(self, message_id: int, analysis: Analysis, received_at: datetime, **extra) -> None:
        row = {**_columns(analysis, received_at), **extra}
        assignments = ", ".join(f"{name} = :{name}" for name in row)
        with self._conn() as conn:
            conn.execute(f"UPDATE messages SET {assignments} WHERE id = :id", {**row, "id": message_id})

    def update(self, message_id: int, **fields) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE messages SET {assignments} WHERE id = :id", {**fields, "id": message_id})

    def get(self, message_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._to_dict(row) if row else None

    def all(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM messages ORDER BY received_at DESC, id DESC").fetchall()
        return [self._to_dict(row) for row in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def clear(self) -> None:
        with self._conn() as conn:
            # Счётчики номеров тоже сбрасываются: после сброса демо обращения снова нумеруются с 1.
            conn.executescript("DELETE FROM events; DELETE FROM messages; DELETE FROM meta; DELETE FROM sqlite_sequence;")

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["analysis"] = json.loads(data.pop("analysis_json"))
        data["needs_review"] = bool(data["needs_review"])
        return data

    # --- журнал событий ----------------------------------------------------------------------

    def add_event(self, message_id: int, kind: str, detail: str, ts: datetime | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (message_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                (message_id, iso(ts or utcnow()), kind, detail),
            )

    def events(self, message_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, kind, detail FROM events WHERE message_id = ? ORDER BY ts, id", (message_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # --- служебные значения ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
