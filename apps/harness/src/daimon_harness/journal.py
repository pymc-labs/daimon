"""Durable turn claims and session journals, owned by one harness process."""

from __future__ import annotations

import json
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


class Journal:
    def __init__(self, home: Path) -> None:
        self.root = home / ".daimon"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "turns.sqlite3")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS turns (turn_id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL, key TEXT UNIQUE NOT NULL, request TEXT NOT NULL)"
        )
        self.db.commit()

    def session(self, session_id: str) -> Path:
        path = self.root / "sessions" / identifier(session_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def connection(self, session_id: str) -> sqlite3.Connection:
        db = sqlite3.connect(self.session(session_id) / "journal.sqlite3")
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT)"
        )
        return db

    def claim(self, request: TurnRequest) -> bool:
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
            return False
        if any(not self.ended(turn.turn_id) for turn in self.turns(request.session_id)):
            raise ConflictError("session already has an active turn")
        with self.db:
            self.db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?)",
                (
                    request.turn_id,
                    request.session_id,
                    request.idempotency_key,
                    body,
                ),
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
        try:
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
                db.execute("INSERT INTO events VALUES (?, ?)", (seq, event.model_dump_json()))
            return event
        finally:
            db.close()

    def events(self, session_id: str, after: int = 0) -> list[Event]:
        db = self.connection(session_id)
        try:
            rows = db.execute(
                "SELECT body FROM events WHERE seq>? ORDER BY seq", (after,)
            ).fetchall()
            return [Event.model_validate_json(row[0]) for row in rows]
        finally:
            db.close()

    def ended(self, turn_id: str) -> bool:
        request = self.find(turn_id)
        return request is not None and any(
            event.turn_id == turn_id and event.type == "turn_ended"
            for event in self.events(request.session_id)
        )

    def history(self, session_id: str) -> list[JsonValue]:
        db = self.connection(session_id)
        try:
            row = db.execute("SELECT body FROM history WHERE id=1").fetchone()
            return json.loads(row[0]) if row else []
        finally:
            db.close()

    def save_history(self, session_id: str, history: list[JsonValue]) -> None:
        db = self.connection(session_id)
        try:
            with db:
                db.execute("INSERT OR REPLACE INTO history VALUES (1, ?)", (json.dumps(history),))
        finally:
            db.close()

    def pending_requests(self, request: TurnRequest) -> dict[str, str]:
        pending: dict[str, str] = {}
        for event in self.events(request.session_id):
            if event.turn_id != request.turn_id:
                continue
            request_id = event.data.get("request_id")
            if not isinstance(request_id, str):
                continue
            if event.type == "model_request_started":
                pending[request_id] = str(event.data["model"])
            elif event.type == "usage" or (
                event.type == "error" and event.data.get("kind") == "usage_unavailable"
            ):
                pending.pop(request_id, None)
        return pending

    def close(self) -> None:
        self.db.close()
