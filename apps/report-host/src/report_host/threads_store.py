"""Durable store for threads, their per-turn boundary, and grounded messages.

Shares one SQLite file and one connection with ``reports_store``: this
module owns the DDL for ``threads`` and ``messages`` via ``apply_schema``,
called once by ``reports_store.connect()`` after that module's own schema.
This module holds no connection logic of its own and never imports
``reports_store`` — every function here takes an already-open connection
(the one ``reports_store.connect()`` returned) as its first parameter, which
is what lets ``reports_store`` import this module without a cycle.

A thread carries the seam session it belongs to (``handle``) and, per turn,
the boundary the seam handed back: ``turn_event_id`` and ``turn_started_at``.
Those two fields are what makes the poller read *this* turn's events rather
than a previous turn's — the prototype's own failure mode — and both are
cleared by ``end_turn``. ``begin_turn`` and ``end_turn`` are each a single
conditional statement, not a read followed by a write: a second question
racing an in-flight turn in the same thread must be refused by the store,
not by a route-level check-then-write.

Money (``reserved_usd``) and every timestamp column follow the same
representation ``reports_store`` documents: INTEGER micro-dollars and
ISO-8601 TEXT respectively, never ``REAL``.

Every stored message records the bundle digest and PDF revision it was
grounded in (``bundle_sha256``, ``pdf_revision``) — the audit trail SPEC §3
requires: every answer must be traceable to the data and the report revision
it was drawn from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from report_host.reports_store import dt_to_text, from_micros, text_to_dt, to_micros

ThreadStatus = Literal["idle", "running"]
Role = Literal["user", "assistant"]

# Alias for the connection's row type (row_factory is set once, in
# reports_store.connect()), so the mapping helpers below share one spelling.
Row = sqlite3.Row

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL REFERENCES reports(slug) ON DELETE CASCADE,
    recipient_token TEXT NOT NULL,
    handle TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    created_at TEXT NOT NULL,
    idle_since TEXT NOT NULL,
    turn_started_at TEXT,
    turn_event_id TEXT,
    turn_deadline_at TEXT,
    reserved_usd INTEGER,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bundle_sha256 TEXT,
    pdf_revision TEXT
);
"""


@dataclass(frozen=True)
class ThreadRow:
    id: int
    slug: str
    recipient_token: str
    handle: str | None
    title: str
    status: ThreadStatus
    created_at: datetime
    idle_since: datetime
    turn_started_at: datetime | None
    turn_event_id: str | None
    turn_deadline_at: datetime | None
    reserved_usd: Decimal | None
    archived_at: datetime | None


@dataclass(frozen=True)
class MessageRow:
    id: int
    thread_id: int
    role: Role
    text: str
    created_at: datetime
    bundle_sha256: str | None
    pdf_revision: str | None


def apply_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``threads`` and ``messages`` tables.

    Called once by ``reports_store.connect()``, after that module's own
    schema (``threads.slug`` references ``reports.slug``). Not meant to be
    called directly outside that flow, but safe to call more than once.
    """
    conn.executescript(_SCHEMA)


def _row_to_thread(row: Row) -> ThreadRow:
    turn_started_at = row["turn_started_at"]
    turn_deadline_at = row["turn_deadline_at"]
    reserved_usd = row["reserved_usd"]
    archived_at = row["archived_at"]
    return ThreadRow(
        id=row["id"],
        slug=row["slug"],
        recipient_token=row["recipient_token"],
        handle=row["handle"],
        title=row["title"],
        status=row["status"],
        created_at=text_to_dt(row["created_at"]),
        idle_since=text_to_dt(row["idle_since"]),
        turn_started_at=text_to_dt(turn_started_at) if turn_started_at else None,
        turn_event_id=row["turn_event_id"],
        turn_deadline_at=text_to_dt(turn_deadline_at) if turn_deadline_at else None,
        reserved_usd=from_micros(reserved_usd) if reserved_usd is not None else None,
        archived_at=text_to_dt(archived_at) if archived_at else None,
    )


def _row_to_message(row: Row) -> MessageRow:
    return MessageRow(
        id=row["id"],
        thread_id=row["thread_id"],
        role=row["role"],
        text=row["text"],
        created_at=text_to_dt(row["created_at"]),
        bundle_sha256=row["bundle_sha256"],
        pdf_revision=row["pdf_revision"],
    )


def create_thread(
    conn: sqlite3.Connection, *, slug: str, recipient_token: str, title: str, now: datetime
) -> ThreadRow:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO threads (slug, recipient_token, title, status, created_at, idle_since)
            VALUES (:slug, :recipient_token, :title, 'idle', :created_at, :idle_since)
            """,
            {
                "slug": slug,
                "recipient_token": recipient_token,
                "title": title,
                "created_at": dt_to_text(now),
                "idle_since": dt_to_text(now),
            },
        )
        thread_id = cur.lastrowid
    assert thread_id is not None  # AUTOINCREMENT always assigns one on INSERT
    return ThreadRow(
        id=thread_id,
        slug=slug,
        recipient_token=recipient_token,
        handle=None,
        title=title,
        status="idle",
        created_at=now,
        idle_since=now,
        turn_started_at=None,
        turn_event_id=None,
        turn_deadline_at=None,
        reserved_usd=None,
        archived_at=None,
    )


def load_thread(
    conn: sqlite3.Connection, *, thread_id: int, slug: str, recipient_token: str
) -> ThreadRow | None:
    """Look up a thread, scoped by id, slug and recipient together.

    A thread id from one recipient is not readable by another, and a thread
    id from one report is not readable under a different slug — this is the
    store-level half of the isolation the routes rely on (T-21-12-A).
    """
    row = conn.execute(
        "SELECT * FROM threads WHERE id = ? AND slug = ? AND recipient_token = ?",
        (thread_id, slug, recipient_token),
    ).fetchone()
    return _row_to_thread(row) if row is not None else None


def list_threads(conn: sqlite3.Connection, *, slug: str, recipient_token: str) -> list[ThreadRow]:
    rows = conn.execute(
        "SELECT * FROM threads WHERE slug = ? AND recipient_token = ? ORDER BY created_at",
        (slug, recipient_token),
    ).fetchall()
    return [_row_to_thread(row) for row in rows]


def count_open_threads(conn: sqlite3.Connection, *, slug: str, recipient_token: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM threads
        WHERE slug = ? AND recipient_token = ? AND archived_at IS NULL
        """,
        (slug, recipient_token),
    ).fetchone()
    return int(row["n"])


def count_running_turns(conn: sqlite3.Connection, *, slug: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM threads WHERE slug = ? AND status = 'running'",
        (slug,),
    ).fetchone()
    return int(row["n"])


def begin_turn(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    reserved_usd: Decimal,
    deadline_at: datetime,
    now: datetime,
) -> bool:
    """Start a turn, unless one is already running in this thread.

    One conditional ``UPDATE``, no preceding read of ``status``: a second
    question submitted while a turn is running must be refused, and the
    refusal is this function's return value, not a route-level
    read-then-write race.
    """
    with conn:
        cur = conn.execute(
            """
            UPDATE threads
            SET status = 'running', turn_started_at = :now, turn_deadline_at = :deadline_at,
                reserved_usd = :reserved_usd
            WHERE id = :thread_id AND status != 'running'
            """,
            {
                "thread_id": thread_id,
                "now": dt_to_text(now),
                "deadline_at": dt_to_text(deadline_at),
                "reserved_usd": to_micros(reserved_usd),
            },
        )
    return cur.rowcount == 1


def bind_turn_boundary(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    handle: str,
    turn_event_id: str,
    turn_started_at: datetime,
) -> None:
    """Record the seam session id and the exact boundary the seam handed back."""
    with conn:
        conn.execute(
            """
            UPDATE threads
            SET handle = :handle, turn_event_id = :turn_event_id, turn_started_at = :turn_started_at
            WHERE id = :thread_id
            """,
            {
                "thread_id": thread_id,
                "handle": handle,
                "turn_event_id": turn_event_id,
                "turn_started_at": dt_to_text(turn_started_at),
            },
        )


def end_turn(conn: sqlite3.Connection, *, thread_id: int, now: datetime) -> None:
    """Clear the turn boundary, the reservation and the deadline; return to idle."""
    with conn:
        conn.execute(
            """
            UPDATE threads
            SET status = 'idle', turn_started_at = NULL, turn_event_id = NULL,
                turn_deadline_at = NULL, reserved_usd = NULL, idle_since = :now
            WHERE id = :thread_id
            """,
            {"thread_id": thread_id, "now": dt_to_text(now)},
        )


def list_running_threads(conn: sqlite3.Connection) -> list[ThreadRow]:
    """What the restart-resume sweep reads: every thread mid-turn."""
    rows = conn.execute(
        "SELECT * FROM threads WHERE status = 'running' ORDER BY turn_started_at"
    ).fetchall()
    return [_row_to_thread(row) for row in rows]


def archive_thread(conn: sqlite3.Connection, *, thread_id: int, now: datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE threads SET archived_at = ? WHERE id = ?", (dt_to_text(now), thread_id)
        )


def list_threads_idle_since(conn: sqlite3.Connection, *, cutoff: datetime) -> list[ThreadRow]:
    """What the idle-archive sweep reads: idle, unarchived threads older than ``cutoff``."""
    rows = conn.execute(
        """
        SELECT * FROM threads
        WHERE status = 'idle' AND archived_at IS NULL AND idle_since < ?
        ORDER BY idle_since
        """,
        (dt_to_text(cutoff),),
    ).fetchall()
    return [_row_to_thread(row) for row in rows]


def add_message(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    role: Role,
    text: str,
    now: datetime,
    bundle_sha256: str | None,
    pdf_revision: str | None,
) -> MessageRow:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO messages (thread_id, role, text, created_at, bundle_sha256, pdf_revision)
            VALUES (:thread_id, :role, :text, :created_at, :bundle_sha256, :pdf_revision)
            """,
            {
                "thread_id": thread_id,
                "role": role,
                "text": text,
                "created_at": dt_to_text(now),
                "bundle_sha256": bundle_sha256,
                "pdf_revision": pdf_revision,
            },
        )
        message_id = cur.lastrowid
    assert message_id is not None  # AUTOINCREMENT always assigns one on INSERT
    return MessageRow(
        id=message_id,
        thread_id=thread_id,
        role=role,
        text=text,
        created_at=now,
        bundle_sha256=bundle_sha256,
        pdf_revision=pdf_revision,
    )


def list_messages(
    conn: sqlite3.Connection, *, thread_id: int, after_id: int = 0
) -> list[MessageRow]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? AND id > ? ORDER BY id",
        (thread_id, after_id),
    ).fetchall()
    return [_row_to_message(row) for row in rows]
