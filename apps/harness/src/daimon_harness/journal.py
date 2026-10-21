"""Durable turn claims and indexed session journals; called through a serial worker."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mux.drivers.selfhosted.wire import Event, EventType, TurnRequest
from pydantic import JsonValue

from .errors import HarnessError
from .isolation import TOOL_UID_MAX

MAX_CURSOR = 2**53
BLOCK = 1024


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
        self.reserve: Callable[[str, int], None] | None = None
        self.commit_inventory: Callable[[TurnRequest], None] | None = None
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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS session_binding "
            "(id INTEGER PRIMARY KEY CHECK(id=1), session_id TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS recovery_digests "
            "(turn_id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
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
        with self.db:
            row = self.db.execute(
                "SELECT ordinal FROM sandboxes WHERE session_id=?", (session_id,)
            ).fetchone()
            ordinal = (
                row[0]
                if row
                else self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM sandboxes").fetchone()[
                    0
                ]
            )
            if 10_000 + ordinal > TOOL_UID_MAX:
                raise ConflictError("session_uid_exhausted")
            if row is None:
                self.db.execute("INSERT INTO sandboxes VALUES (?, ?)", (ordinal, session_id))
        name = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        parent = self.root.parent / "sessions"
        parent.mkdir(exist_ok=True, mode=0o711)
        self.root.parent.chmod(0o711)
        return parent / name, 10_000 + ordinal

    def configure_single_session(self) -> None:
        known = {
            row[0]
            for row in self.db.execute(
                "SELECT session_id FROM sandboxes UNION SELECT session_id FROM turns "
                "UNION SELECT session_id FROM session_binding"
            )
        }
        if len(known) > 1:
            raise RuntimeError("single session mode cannot open multi-session state")
        if known:
            self.bind_session(known.pop())

    def bind_session(self, session_id: str) -> None:
        identifier(session_id)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO session_binding VALUES (1, ?)", (session_id,))
            bound = self.db.execute("SELECT session_id FROM session_binding WHERE id=1").fetchone()
            if bound[0] != session_id:
                raise ConflictError("session_bound")

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
            self.sequence_state(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending_inventory "
                "(turn_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)"
            )
            db.commit()
            self.connections[session_id] = db
        return self.connections[session_id]

    def is_replay(self, request: TurnRequest) -> bool:
        identifier(request.session_id)
        identifier(request.turn_id)
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        recovered = self.db.execute(
            "SELECT turns.turn_id, turns.session_id, turns.key, recovery_digests.digest "
            "FROM turns JOIN recovery_digests USING(turn_id) WHERE key=? OR turns.turn_id=?",
            (request.idempotency_key, request.turn_id),
        ).fetchall()
        if recovered:
            digest = hashlib.sha256(body.encode()).hexdigest()
            if any(
                row != (request.turn_id, request.session_id, request.idempotency_key, digest)
                for row in recovered
            ):
                raise ConflictError("turn id or idempotency key already has a different request")
            return True
        previous = self.db.execute(
            "SELECT request FROM turns WHERE key=? OR turn_id=?",
            (request.idempotency_key, request.turn_id),
        ).fetchall()
        if previous:
            if any(row[0] != body for row in previous):
                raise ConflictError("turn id or idempotency key already has a different request")
            return True
        return False

    def remember_recovery(self, request: TurnRequest, digest: str) -> None:
        """Recover a compact remote claim without retaining prompts or skill bundles."""
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute(
                "INSERT INTO turns(turn_id, session_id, key, request) VALUES (?, ?, ?, ?)",
                (request.turn_id, request.session_id, request.idempotency_key, body),
            )
            self.db.execute("INSERT INTO recovery_digests VALUES (?, ?)", (request.turn_id, digest))

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
        self.reserve_append(request.session_id)
        with db:
            if (
                kind in {"tool_use", "tool_result", "requires_action"}
                and db.execute(
                    "SELECT 1 FROM events WHERE turn_id=? AND type='turn_ended' LIMIT 1",
                    (request.turn_id,),
                ).fetchone()
            ):
                raise ConflictError("turn already ended; tool events refused")
            self._announce_gap(db, request.session_id, request.turn_id)
            seq = self._next_seq(db)
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
            if self.commit_inventory is not None and (
                kind in {"model_request_started", "usage"}
                or (kind == "error" and data.get("kind") == "usage_unavailable")
            ):
                # Remote outcome commits before this transaction can become visible.
                try:
                    self.commit_inventory(request)
                except Exception:
                    # Keep the immutable event locally, but withhold it from
                    # replay until its remote inventory commit is confirmed.
                    db.execute(
                        "INSERT OR REPLACE INTO pending_inventory VALUES (?, ?)",
                        (request.turn_id, seq),
                    )
                    db.commit()
                    raise
        if kind == "turn_ended":
            with self.db:
                self.db.execute("UPDATE turns SET ended=1 WHERE turn_id=?", (request.turn_id,))
        return event

    def events(
        self, session_id: str, after: int = 0, *, include_pending: bool = False
    ) -> list[Event]:
        identifier(session_id)
        if not (self.root / "sessions" / session_id / "journal.sqlite3").exists():
            return []
        db = self.connection(session_id)
        upper = (
            2**63 - 1
            if include_pending
            else db.execute(
                "SELECT COALESCE(MIN(seq), 9223372036854775807) FROM pending_inventory"
            ).fetchone()[0]
        )
        rows = db.execute(
            "SELECT body FROM events WHERE seq>? AND seq<? ORDER BY seq", (after, upper)
        ).fetchall()
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

    def latest_seq(self, session_id: str) -> int:
        return (
            self.connection(session_id)
            .execute("SELECT COALESCE(MAX(seq),0) FROM events")
            .fetchone()[0]
        )

    @staticmethod
    def sequence_state(db: sqlite3.Connection) -> None:
        """Migrate old checkpoints without discarding their reserved high-water."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS event_sequence "
            "(id INTEGER PRIMARY KEY CHECK(id=1), floor INTEGER, "
            "max_client_cursor INTEGER NOT NULL DEFAULT 0)"
        )
        if "max_client_cursor" not in {
            row[1] for row in db.execute("PRAGMA table_info(event_sequence)")
        }:
            db.execute(
                "ALTER TABLE event_sequence ADD COLUMN max_client_cursor INTEGER NOT NULL DEFAULT 0"
            )
        columns = {row[1] for row in db.execute("PRAGMA table_info(event_sequence)")}
        for name, definition in (
            ("reserved_high_water", "INTEGER NOT NULL DEFAULT 0"),
            ("gap_from", "INTEGER"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE event_sequence ADD COLUMN {name} {definition}")
        db.execute("INSERT OR IGNORE INTO event_sequence(id, floor) VALUES (1, 0)")

    @staticmethod
    def record_cursor_db(db: sqlite3.Connection, cursor: int, reserved: int = 0) -> None:
        if not 0 <= cursor <= MAX_CURSOR:
            raise ValueError("invalid client cursor")
        with db:
            Journal.sequence_state(db)
            local_next = db.execute("SELECT COALESCE(MAX(seq), 0)+1 FROM events").fetchone()[0]
            if reserved >= local_next:
                db.execute(
                    "UPDATE event_sequence SET gap_from=MIN(COALESCE(gap_from, ?), ?) WHERE id=1",
                    (local_next, local_next),
                )
            db.execute(
                "UPDATE event_sequence SET max_client_cursor=MAX(max_client_cursor, ?), "
                "floor=MAX(floor, ?) WHERE id=1",
                (cursor, reserved + 1),
            )

    def client_cursor(self, session_id: str) -> int:
        db = self.connection(session_id)
        row = db.execute("SELECT max_client_cursor FROM event_sequence WHERE id=1").fetchone()
        if row is None:
            # A failed restore/rolled-back schema transaction can leave an open
            # cached connection without the singleton. Repair it durably.
            with db:
                self.sequence_state(db)
            row = db.execute("SELECT max_client_cursor FROM event_sequence WHERE id=1").fetchone()
        return row[0]

    def reserved_high_water(self, session_id: str) -> int:
        return (
            self.connection(session_id)
            .execute("SELECT reserved_high_water FROM event_sequence WHERE id=1")
            .fetchone()[0]
        )

    def flush_inventory(self, session_id: str) -> None:
        if self.commit_inventory is None:
            return
        db = self.connection(session_id)
        for (turn_id,) in db.execute(
            "SELECT turn_id FROM pending_inventory ORDER BY seq"
        ).fetchall():
            request = self.find(turn_id)
            if request is None:
                raise HarnessError("durability_unavailable", "Pending inventory has no turn claim")
            self.commit_inventory(request)
            with db:
                db.execute("DELETE FROM pending_inventory WHERE turn_id=?", (turn_id,))

    def reserve_append(self, session_id: str) -> None:
        self.flush_inventory(session_id)
        if self.reserve is None:
            return
        db = self.connection(session_id)
        next_seq = self._next_seq(db)
        local_next = db.execute("SELECT COALESCE(MAX(seq), 0)+1 FROM events").fetchone()[0]
        # A gap itself consumes a sequence before the requested event.
        needed = next_seq + int(next_seq > local_next)
        if needed > self.reserved_high_water(session_id):
            high_water = next_seq + BLOCK - 1
            try:
                self.reserve(session_id, high_water)
            except Exception:
                raise HarnessError(
                    "durability_unavailable", "Sequence reservation is unavailable"
                ) from None
            with db:
                db.execute(
                    "UPDATE event_sequence SET reserved_high_water=? WHERE id=1", (high_water,)
                )

    def record_cursor(self, session_id: str, cursor: int) -> int:
        # FULL SQLite commit precedes restore, recovery or request side effects.
        db = self.connection(session_id)
        self.record_cursor_db(db, cursor)
        return self.client_cursor(session_id)

    @staticmethod
    def _next_seq(db: sqlite3.Connection) -> int:
        local_next = db.execute("SELECT COALESCE(MAX(seq), 0)+1 FROM events").fetchone()[0]
        # floor is the legacy column name for the local next-position
        # reservation retained in checkpoints and remote write-ahead records.
        floor, cursor = db.execute(
            "SELECT floor, max_client_cursor FROM event_sequence WHERE id=1"
        ).fetchone()
        return max(local_next, floor, cursor + 1)

    @staticmethod
    def _announce_gap(db: sqlite3.Connection, session_id: str, turn_id: str) -> None:
        local_next = db.execute("SELECT COALESCE(MAX(seq), 0)+1 FROM events").fetchone()[0]
        gap_from = db.execute("SELECT gap_from FROM event_sequence WHERE id=1").fetchone()[0]
        if gap_from is not None:
            local_next = min(local_next, gap_from)
        seq = Journal._next_seq(db)
        if seq > local_next:
            event = Event(
                seq=seq,
                session_id=session_id,
                turn_id=turn_id,
                at=datetime.now(UTC),
                type="history_gap",
                data={
                    "from_seq": local_next,
                    "to_seq": seq - 1,
                    "reason": "sandbox_restored",
                },
            )
            db.execute(
                "INSERT INTO events(seq, body, turn_id, type) VALUES (?, ?, ?, ?)",
                (seq, event.model_dump_json(), turn_id, event.type),
            )
            db.execute("UPDATE event_sequence SET gap_from=NULL WHERE id=1")

    def announce_gap(self, session_id: str) -> None:
        requests = self.turns(session_id)
        # A cursor is a valid fence even before this process knows any turn.
        turn_id = requests[-1].turn_id if requests else "session"
        db = self.connection(session_id)
        if (
            self._next_seq(db)
            > db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        ):
            self.reserve_append(session_id)
        with db:
            self._announce_gap(db, session_id, turn_id)

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
