"""Durable turn claims and indexed session journals; called through a serial worker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from mux.drivers.selfhosted.wire import Event, EventType, TurnRequest
from pydantic import JsonValue


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", value):
        raise ValueError("identifier must contain only letters, digits, underscores or hyphens")
    return value


class ConflictError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    return db


class Journal:
    def __init__(self, home: Path) -> None:
        self.root = home / ".daimon"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.db = connect(self.root / "turns.sqlite3")
        self.connections: dict[str, sqlite3.Connection] = {}
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS turns (turn_id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL, key TEXT UNIQUE NOT NULL, request TEXT NOT NULL, "
            "ended INTEGER NOT NULL DEFAULT 0)"
        )
        if "ended" not in {row[1] for row in self.db.execute("PRAGMA table_info(turns)")}:
            self.db.execute("ALTER TABLE turns ADD COLUMN ended INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE INDEX IF NOT EXISTS turns_active ON turns(session_id, ended)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sandboxes (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT UNIQUE NOT NULL)"
        )
        self.db.commit()
        # Repair only the terminal-event/global-index crash window, once at startup.
        for turn_id, session_id in self.db.execute(
            "SELECT turn_id, session_id FROM turns WHERE ended=0"
        ).fetchall():
            path = self.root / "sessions" / session_id / "journal.sqlite3"
            if (
                path.exists()
                and self.connection(session_id)
                .execute(
                    "SELECT 1 FROM events WHERE turn_id=? AND type='turn_ended' LIMIT 1", (turn_id,)
                )
                .fetchone()
            ):
                self.db.execute("UPDATE turns SET ended=1 WHERE turn_id=?", (turn_id,))
        self.db.commit()

    def session(self, session_id: str) -> Path:
        path = self.root / "sessions" / identifier(session_id)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def sandbox(self, session_id: str) -> tuple[Path, int]:
        identifier(session_id)
        if (
            os.geteuid() != 0
            and self.db.execute(
                "SELECT 1 FROM sandboxes WHERE session_id<>? LIMIT 1", (session_id,)
            ).fetchone()
        ):
            raise ValueError("fixed-UID mode requires one isolated HOME per session")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sandboxes(session_id) VALUES (?)", (session_id,))
        ordinal = self.db.execute(
            "SELECT ordinal FROM sandboxes WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        name = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        parent = self.root.parent / "sessions"
        parent.mkdir(exist_ok=True, mode=0o711)
        self.root.parent.chmod(0o711)
        return parent / name, 10_000 + ordinal

    def connection(self, session_id: str) -> sqlite3.Connection:
        if session_id not in self.connections:
            db = connect(self.session(session_id) / "journal.sqlite3")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, body TEXT NOT NULL, "
                "turn_id TEXT, type TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            if "turn_id" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN turn_id TEXT")
                db.execute("ALTER TABLE events ADD COLUMN type TEXT")
                db.execute(
                    "UPDATE events SET turn_id=json_extract(body, '$.turn_id'), "
                    "type=json_extract(body, '$.type')"
                )
            db.execute("CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id, type, seq)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT)"
            )
            db.commit()
            self.connections[session_id] = db
        return self.connections[session_id]

    def is_replay(self, request: TurnRequest) -> bool:
        identifier(request.session_id)
        identifier(request.turn_id)
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        previous = self.db.execute(
            "SELECT request FROM turns WHERE key=? OR turn_id=?",
            (request.idempotency_key, request.turn_id),
        ).fetchall()
        if previous:
            if any(row[0] != body for row in previous):
                raise ConflictError("turn id or idempotency key already has a different request")
            return True
        return False

    def claim(self, request: TurnRequest) -> bool:
        if self.is_replay(request):
            return False
        if not self.complete(request.session_id):
            raise ConflictError("session already has an active turn")
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute(
                "INSERT INTO turns(turn_id, session_id, key, request) VALUES (?, ?, ?, ?)",
                (request.turn_id, request.session_id, request.idempotency_key, body),
            )
        return True

    def turns(self, session_id: str | None = None) -> list[TurnRequest]:
        if session_id is None:
            rows = self.db.execute("SELECT request FROM turns").fetchall()
        else:
            rows = self.db.execute(
                "SELECT request FROM turns WHERE session_id=?", (session_id,)
            ).fetchall()
        return [TurnRequest.model_validate_json(row[0]) for row in rows]

    def find(self, turn_id: str) -> TurnRequest | None:
        row = self.db.execute("SELECT request FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return TurnRequest.model_validate_json(row[0]) if row else None

    def append(self, request: TurnRequest, kind: EventType, data: dict[str, JsonValue]) -> Event:
        db = self.connection(request.session_id)
        with db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0)+1 FROM events").fetchone()[0]
            event = Event(
                seq=seq,
                session_id=request.session_id,
                turn_id=request.turn_id,
                at=datetime.now(UTC),
                type=kind,
                data=data,
            )
            db.execute(
                "INSERT INTO events(seq, body, turn_id, type) VALUES (?, ?, ?, ?)",
                (seq, event.model_dump_json(), request.turn_id, kind),
            )
            if kind in {"tool_use", "tool_result"}:
                # Recovery summaries avoid dangling provider function-call items.
                # Committing with the event preserves known side effects after a crash.
                history = self.history(request.session_id)
                history.append(
                    {"role": "user", "content": "Interrupted-turn tool record: " + json.dumps(data)}
                )
                db.execute("INSERT OR REPLACE INTO history VALUES (1, ?)", (json.dumps(history),))
        if kind == "turn_ended":
            with self.db:
                self.db.execute("UPDATE turns SET ended=1 WHERE turn_id=?", (request.turn_id,))
        return event

    def events(self, session_id: str, after: int = 0) -> list[Event]:
        identifier(session_id)
        if not (self.root / "sessions" / session_id / "journal.sqlite3").exists():
            return []
        rows = (
            self.connection(session_id)
            .execute("SELECT body FROM events WHERE seq>? ORDER BY seq", (after,))
            .fetchall()
        )
        return [Event.model_validate_json(row[0]) for row in rows]

    def ended(self, turn_id: str) -> bool:
        row = self.db.execute("SELECT ended FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return bool(row and row[0])

    def complete(self, session_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM turns WHERE session_id=? AND ended=0 LIMIT 1", (session_id,)
            ).fetchone()
            is None
        )

    def history(self, session_id: str) -> list[JsonValue]:
        row = self.connection(session_id).execute("SELECT body FROM history WHERE id=1").fetchone()
        return json.loads(row[0]) if row else []

    def save_history(self, session_id: str, history: list[JsonValue]) -> None:
        with self.connection(session_id) as db:
            db.execute("INSERT OR REPLACE INTO history VALUES (1, ?)", (json.dumps(history),))

    def pending_requests(self, request: TurnRequest) -> dict[str, str]:
        pending: dict[str, str] = {}
        rows = self.connection(request.session_id).execute(
            "SELECT body FROM events WHERE turn_id=? AND type IN "
            "('model_request_started', 'usage', 'error') ORDER BY seq",
            (request.turn_id,),
        )
        for row in rows:
            event = Event.model_validate_json(row[0])
            request_id = event.data.get("request_id")
            if not isinstance(request_id, str):
                continue
            if event.type == "model_request_started":
                pending[request_id] = str(event.data["model"])
            elif event.type == "usage" or event.data.get("kind") == "usage_unavailable":
                pending.pop(request_id, None)
        return pending

    def close(self) -> None:
        for db in self.connections.values():
            db.close()
        self.db.close()
