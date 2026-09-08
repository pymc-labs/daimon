"""Durable store for reports, their recipients and revisions.

One SQLite file at ``data_dir / "host.sqlite"``. ``connect()`` is the only
function that opens a connection and the only place any schema is applied
(idempotently, ``CREATE TABLE IF NOT EXISTS``) — it applies this module's own
tables, then calls ``threads_store.apply_schema(conn)`` so both halves of the
host's durable state live in one file. That import happens inside
``connect()``, not at module load time, so ``threads_store`` (which reuses
this module's connection but never imports it) and this module never form a
cycle. Every other function in this module and in ``threads_store`` takes an
already-open connection as its first parameter — there is no module-level
connection and no import-time side effect, unlike the prototype this ports
from (``spikes/report-host/host/app.py``), which opened a connection at
import and kept it as a global.

Money (``cap_usd``, ``spent_usd``) is stored as **INTEGER micro-dollars**
(1 USD = 1_000_000) and surfaced as ``Decimal`` at every function boundary —
never as SQLite's ``REAL``. Integers are exact; the prototype stored money as
``REAL`` and compared floats against a cap, which is a rounding bug waiting
to happen. Storing micro-dollars as an integer also makes ``reserve_budget``'s
single conditional ``UPDATE`` an exact comparison, not a floating-point one.
Every other point-in-time column (``created_at``, ``expires_at``,
``bundle_expires_at``, ...) is stored as TEXT, an ISO-8601 string produced by
``datetime.isoformat()`` — never ``REAL`` either — and read back with
``datetime.fromisoformat``. Callers should pass timezone-aware datetimes
(UTC) so that lexical TEXT ordering agrees with chronological ordering.

``archive_path`` is the publish route's own record of where it persisted the
report's most recently pushed archive under ``settings.data_dir / slug``. It
is never derived from caller input by this module (T-21-12-H): the publish
route composes it and hands it to ``save_bundle_reference`` as one unit with
the bundle handle/sha256/expiry it describes, because a handle recorded
without the archive that produced it is a re-push that cannot happen.

Per-report seam tokens (``seam_token``) sit in this database at the same
trust level as the admin bearer (T-21-12-F): the host's data volume is not
shared with any other service, and revocation-on-delete is the real control,
landing with the publishing routes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

SeamStatus = Literal["ok", "unauthorized"]

_MICROS_PER_USD = Decimal(1_000_000)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    cap_usd INTEGER NOT NULL,
    spent_usd INTEGER NOT NULL DEFAULT 0,
    current_pdf TEXT,
    seam_token TEXT NOT NULL,
    seam_status TEXT NOT NULL DEFAULT 'ok',
    bundle_handle TEXT,
    bundle_sha256 TEXT,
    bundle_expires_at TEXT,
    archive_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    token TEXT PRIMARY KEY,
    slug TEXT NOT NULL REFERENCES reports(slug) ON DELETE CASCADE,
    name TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS revisions (
    slug TEXT NOT NULL REFERENCES reports(slug) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    by_thread TEXT,
    note TEXT,
    PRIMARY KEY (slug, name)
);
"""


@dataclass(frozen=True)
class ReportRow:
    slug: str
    title: str
    tenant_id: str
    agent_name: str
    cap_usd: Decimal
    spent_usd: Decimal
    current_pdf: str | None
    seam_token: str
    seam_status: SeamStatus
    bundle_handle: str | None
    bundle_sha256: str | None
    bundle_expires_at: datetime | None
    archive_path: str | None
    created_at: datetime


@dataclass(frozen=True)
class RecipientRow:
    token: str
    slug: str
    name: str
    label: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class RevisionRow:
    slug: str
    name: str
    created_at: datetime
    by_thread: str | None
    note: str | None


def to_micros(amount: Decimal) -> int:
    """Quantize a Decimal USD amount to an exact integer micro-dollar count."""
    return int((amount * _MICROS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def from_micros(micros: int) -> Decimal:
    return Decimal(micros) / _MICROS_PER_USD


def dt_to_text(value: datetime) -> str:
    return value.isoformat()


def text_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_report(row: sqlite3.Row) -> ReportRow:
    bundle_expires_at = row["bundle_expires_at"]
    return ReportRow(
        slug=row["slug"],
        title=row["title"],
        tenant_id=row["tenant_id"],
        agent_name=row["agent_name"],
        cap_usd=from_micros(row["cap_usd"]),
        spent_usd=from_micros(row["spent_usd"]),
        current_pdf=row["current_pdf"],
        seam_token=row["seam_token"],
        seam_status=row["seam_status"],
        bundle_handle=row["bundle_handle"],
        bundle_sha256=row["bundle_sha256"],
        bundle_expires_at=text_to_dt(bundle_expires_at) if bundle_expires_at else None,
        archive_path=row["archive_path"],
        created_at=text_to_dt(row["created_at"]),
    )


def _row_to_recipient(row: sqlite3.Row) -> RecipientRow:
    revoked_at = row["revoked_at"]
    return RecipientRow(
        token=row["token"],
        slug=row["slug"],
        name=row["name"],
        label=row["label"],
        created_at=text_to_dt(row["created_at"]),
        expires_at=text_to_dt(row["expires_at"]),
        revoked_at=text_to_dt(revoked_at) if revoked_at else None,
    )


def _row_to_revision(row: sqlite3.Row) -> RevisionRow:
    return RevisionRow(
        slug=row["slug"],
        name=row["name"],
        created_at=text_to_dt(row["created_at"]),
        by_thread=row["by_thread"],
        note=row["note"],
    )


def connect(data_dir: Path) -> sqlite3.Connection:
    """Open (creating if needed) the host's single SQLite database.

    Creates ``data_dir`` if missing, opens ``data_dir / "host.sqlite"``,
    enables foreign keys and WAL, and idempotently applies this module's
    schema followed by ``threads_store``'s. This is the only place a
    connection is created in the report-host codebase.
    """
    from report_host import threads_store

    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "host.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    threads_store.apply_schema(conn)
    return conn


def load_report(conn: sqlite3.Connection, *, slug: str) -> ReportRow | None:
    row = conn.execute("SELECT * FROM reports WHERE slug = ?", (slug,)).fetchone()
    return _row_to_report(row) if row is not None else None


def save_report(
    conn: sqlite3.Connection,
    *,
    slug: str,
    title: str,
    tenant_id: str,
    agent_name: str,
    cap_usd: Decimal,
    agent_token: str,
    now: datetime,
) -> ReportRow:
    """Create a report, or replace its metadata if ``slug`` already exists.

    A re-publish (an existing ``slug``) updates ``title``, ``tenant_id``,
    ``agent_name``, ``cap_usd`` and the seam token, and resets
    ``seam_status`` to healthy (a fresh token implies re-authorization) —
    but leaves ``spent_usd``, ``current_pdf`` and every bundle field
    (``bundle_handle``, ``bundle_sha256``, ``bundle_expires_at``,
    ``archive_path``) untouched. An admin metadata edit must not zero a
    report's spend or orphan the archive the one re-push depends on.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO reports (
                slug, title, tenant_id, agent_name, cap_usd, spent_usd,
                seam_token, seam_status, created_at
            )
            VALUES (
                :slug, :title, :tenant_id, :agent_name, :cap_usd, 0,
                :agent_token, 'ok', :created_at
            )
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                tenant_id = excluded.tenant_id,
                agent_name = excluded.agent_name,
                cap_usd = excluded.cap_usd,
                seam_token = excluded.seam_token,
                seam_status = 'ok'
            """,
            {
                "slug": slug,
                "title": title,
                "tenant_id": tenant_id,
                "agent_name": agent_name,
                "cap_usd": to_micros(cap_usd),
                "agent_token": agent_token,
                "created_at": dt_to_text(now),
            },
        )
    report = load_report(conn, slug=slug)
    if report is None:
        raise RuntimeError(f"report {slug!r} vanished immediately after save_report")
    return report


def delete_report(conn: sqlite3.Connection, *, slug: str) -> bool:
    """Delete a report (cascading to its recipients and revisions).

    Returns whether a row was actually removed, so a caller can distinguish
    a wrong slug from a real deletion.
    """
    with conn:
        cur = conn.execute("DELETE FROM reports WHERE slug = ?", (slug,))
    return cur.rowcount > 0


def save_bundle_reference(
    conn: sqlite3.Connection,
    *,
    slug: str,
    handle: str,
    sha256: str,
    expires_at: datetime,
    archive_path: str,
) -> None:
    """Record a freshly pushed bundle and the archive that produced it.

    All four fields move together in one statement: a handle saved without
    the archive path that produced it is a re-push that cannot happen, so
    these are not separate setters.
    """
    with conn:
        conn.execute(
            """
            UPDATE reports
            SET bundle_handle = :handle,
                bundle_sha256 = :sha256,
                bundle_expires_at = :expires_at,
                archive_path = :archive_path
            WHERE slug = :slug
            """,
            {
                "slug": slug,
                "handle": handle,
                "sha256": sha256,
                "expires_at": dt_to_text(expires_at),
                "archive_path": archive_path,
            },
        )


def set_seam_status(conn: sqlite3.Connection, *, slug: str, status: SeamStatus) -> None:
    with conn:
        conn.execute("UPDATE reports SET seam_status = ? WHERE slug = ?", (status, slug))


def reserve_budget(conn: sqlite3.Connection, *, slug: str, amount: Decimal) -> Decimal | None:
    """Atomically add ``amount`` to a report's spend if it still fits under the cap.

    One conditional ``UPDATE ... RETURNING``, with no preceding read of
    ``spent_usd``. Two concurrent readers racing this function cannot both
    pass: the ``WHERE`` clause is evaluated against the row as SQLite's
    writer serialization sees it at ``UPDATE`` time, never against a value
    either caller read earlier. Returns the new spend on success, ``None``
    when the reservation would exceed the cap (the spend is left unchanged).
    """
    with conn:
        cur = conn.execute(
            """
            UPDATE reports
            SET spent_usd = spent_usd + :amount
            WHERE slug = :slug AND spent_usd + :amount <= cap_usd
            RETURNING spent_usd
            """,
            {"slug": slug, "amount": to_micros(amount)},
        )
        row = cur.fetchone()
    return from_micros(row["spent_usd"]) if row is not None else None


def settle_budget(
    conn: sqlite3.Connection, *, slug: str, reserved: Decimal, actual: Decimal | None
) -> None:
    """Replace a prior reservation with the real cost once a turn ends.

    When ``actual`` is ``None`` (an unpriced model), the reservation is left
    in place rather than refunded — an unknown cost is not a zero cost.
    """
    if actual is None:
        return
    delta = to_micros(actual) - to_micros(reserved)
    with conn:
        conn.execute(
            "UPDATE reports SET spent_usd = spent_usd + :delta WHERE slug = :slug",
            {"slug": slug, "delta": delta},
        )


def add_recipient(
    conn: sqlite3.Connection,
    *,
    slug: str,
    name: str,
    label: str,
    token: str,
    now: datetime,
    ttl_days: int,
) -> RecipientRow:
    expires_at = now + timedelta(days=ttl_days)
    with conn:
        conn.execute(
            """
            INSERT INTO recipients (token, slug, name, label, created_at, expires_at)
            VALUES (:token, :slug, :name, :label, :created_at, :expires_at)
            """,
            {
                "token": token,
                "slug": slug,
                "name": name,
                "label": label,
                "created_at": dt_to_text(now),
                "expires_at": dt_to_text(expires_at),
            },
        )
    return RecipientRow(
        token=token,
        slug=slug,
        name=name,
        label=label,
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )


def load_recipient(
    conn: sqlite3.Connection, *, slug: str, token: str, now: datetime
) -> RecipientRow | None:
    """Look up a recipient. Returns ``None`` for an unknown, revoked, or expired token."""
    row = conn.execute(
        """
        SELECT * FROM recipients
        WHERE slug = ? AND token = ? AND revoked_at IS NULL AND expires_at > ?
        """,
        (slug, token, dt_to_text(now)),
    ).fetchone()
    return _row_to_recipient(row) if row is not None else None


def revoke_recipient(conn: sqlite3.Connection, *, slug: str, token: str, now: datetime) -> bool:
    with conn:
        cur = conn.execute(
            """
            UPDATE recipients SET revoked_at = ?
            WHERE slug = ? AND token = ? AND revoked_at IS NULL
            """,
            (dt_to_text(now), slug, token),
        )
    return cur.rowcount > 0


def list_recipients(conn: sqlite3.Connection, *, slug: str) -> list[RecipientRow]:
    rows = conn.execute(
        "SELECT * FROM recipients WHERE slug = ? ORDER BY created_at", (slug,)
    ).fetchall()
    return [_row_to_recipient(row) for row in rows]


def add_revision(
    conn: sqlite3.Connection,
    *,
    slug: str,
    name: str,
    by_thread: str | None,
    note: str | None,
    now: datetime,
) -> RevisionRow:
    with conn:
        conn.execute(
            """
            INSERT INTO revisions (slug, name, created_at, by_thread, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (slug, name, dt_to_text(now), by_thread, note),
        )
    return RevisionRow(slug=slug, name=name, created_at=now, by_thread=by_thread, note=note)


def list_revisions(conn: sqlite3.Connection, *, slug: str) -> list[RevisionRow]:
    rows = conn.execute(
        "SELECT * FROM revisions WHERE slug = ? ORDER BY created_at", (slug,)
    ).fetchall()
    return [_row_to_revision(row) for row in rows]


def set_current_pdf(conn: sqlite3.Connection, *, slug: str, name: str) -> None:
    with conn:
        conn.execute("UPDATE reports SET current_pdf = ? WHERE slug = ?", (name, slug))
