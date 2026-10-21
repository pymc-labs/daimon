"""Compact durable claims, with shared content-addressed bundles for active turns."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from mux.drivers.selfhosted.wire import TurnRequest


def canonical(request: TurnRequest) -> str:
    return json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def minimal(request: TurnRequest) -> TurnRequest:
    # Terminal turns only need identity/model/limits for recovery and cold cleanup.
    # Their full request digest still rejects changed-body idempotent resends.
    return request.model_copy(
        update={"instructions": "", "input": [], "skills": [], "mcp_servers": [], "tool_policy": {}}
    )


def store_request(db: sqlite3.Connection, request: TurnRequest) -> tuple[str, str]:
    digest = hashlib.sha256(canonical(request).encode()).hexdigest()
    for position, skill in enumerate(request.skills):
        body = json.dumps(skill.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        bundle_id = hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO skill_bundles VALUES (?, ?)", (bundle_id, body))
        db.execute(
            "INSERT INTO turn_skills VALUES (?, ?, ?)", (request.turn_id, position, bundle_id)
        )
    return canonical(request.model_copy(update={"skills": []})), digest


def load_request(db: sqlite3.Connection, body: str) -> TurnRequest:
    request = json.loads(body)
    skills = db.execute(
        "SELECT skill_bundles.body FROM turn_skills JOIN skill_bundles USING(digest) "
        "WHERE turn_id=? ORDER BY position",
        (request["turn_id"],),
    ).fetchall()
    if skills:
        request["skills"] = [json.loads(row[0]) for row in skills]
    return TurnRequest.model_validate(request)


def compact_request(db: sqlite3.Connection, turn_id: str) -> None:
    row = db.execute("SELECT request FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    if row is None:
        return
    request = TurnRequest.model_validate_json(row[0])
    db.execute("UPDATE turns SET request=? WHERE turn_id=?", (canonical(minimal(request)), turn_id))
    db.execute("DELETE FROM turn_skills WHERE turn_id=?", (turn_id,))
    db.execute(
        "DELETE FROM skill_bundles WHERE NOT EXISTS "
        "(SELECT 1 FROM turn_skills WHERE turn_skills.digest=skill_bundles.digest)"
    )


def initialize_requests(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS skill_bundles (digest TEXT PRIMARY KEY, body TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS turn_skills (turn_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "digest TEXT NOT NULL, PRIMARY KEY(turn_id, position))"
    )
    db.execute("CREATE INDEX IF NOT EXISTS turn_skills_digest ON turn_skills(digest)")
    if "request_digest" not in {row[1] for row in db.execute("PRAGMA table_info(turns)")}:
        db.execute("ALTER TABLE turns ADD COLUMN request_digest TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS turns_missing_digest ON turns(turn_id) "
        "WHERE request_digest IS NULL"
    )
    # Migrate one row at a time; never hold every inline skill bundle in memory.
    while row := db.execute(
        "SELECT turn_id, request, ended FROM turns WHERE request_digest IS NULL LIMIT 1"
    ).fetchone():
        with db:
            request = TurnRequest.model_validate_json(row[1])
            body, digest = store_request(db, request)
            db.execute(
                "UPDATE turns SET request=?, request_digest=? WHERE turn_id=?",
                (body, digest, row[0]),
            )
            if row[2]:
                compact_request(db, row[0])
    db.commit()
    # Enable reclaiming old inline-bundle pages, including pre-existing DBs.
    if db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
