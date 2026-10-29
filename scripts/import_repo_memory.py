"""Offline repository-memory import (no connection to the old service).

Runbook:
1. Keep native memory disabled. Freeze old memory writes and export all rows to
   a JSON array containing id, repo_owner, repo_name, name, title, body, kind,
   scope, created_by_discord_id, created_at and updated_at. Never put that export
   or the maps in Git. Repository slugs, content and timestamps are preserved.
2. Supply accounts JSON {discord_id: account_uuid} and repos JSON
   {"owner/name": {"tenant_id": tenant_uuid, "repo_id": github_repository_id}}.
   Every creator must map, including shared entries; do not guess personal owners.
3. Run `uv run python scripts/import_repo_memory.py --snapshot EXPORT
   --accounts ACCOUNTS --repos REPOS --manifest MANIFEST` against the destination
   using DAIMON_DATABASE__URL. This defaults to dry run and rolls back all writes.
   Resolve every mapping or destination conflict, then inspect the manifest's
   per-repo row counts and content digests (which include personal directives).
4. Repeat with --apply. Require its manifest to match the successful dry run.
   Exact replays are idempotent; destination edits cause a refusal, never overwrite.
5. Verify the imported shared and personal lists in fresh Daimon sessions.

Before Daimon writes, the source export still contains the latest memory data.
After Daimon writes, returning to the source requires reconciling those new
changes; do not resume writes against stale data.
The manifest contains counts/digests only; memory text and identities stay local.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from daimon.core.repo_memory import slugify
from daimon.core.stores.repo_memory import MemoryEntry, import_entry
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class ExportEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    repo_owner: str
    repo_name: str
    name: str
    title: str
    body: str
    kind: Literal["reference", "directive"] = "reference"
    scope: Literal["repo", "user"] = "repo"
    created_by_discord_id: str
    created_at: datetime
    updated_at: datetime


class RepoTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: uuid.UUID
    repo_id: int


def prepare_entries(
    export: list[ExportEntry], accounts: dict[str, uuid.UUID], repos: dict[str, RepoTarget]
) -> list[MemoryEntry]:
    destinations: set[tuple[uuid.UUID, int, str]] = set()
    sources: set[str] = set()
    entries: list[MemoryEntry] = []
    canonical = {key.casefold(): value for key, value in repos.items()}
    if len(canonical) != len(repos):
        raise ValueError("Ambiguous repository casing in the map.")
    for row in export:
        target = canonical.get(f"{row.repo_owner}/{row.repo_name}".casefold())
        account = accounts.get(row.created_by_discord_id)
        if target is None or account is None:
            raise ValueError("Unmapped repository or creator; every exported row needs a mapping.")
        if (
            not row.name
            or slugify(row.name) != row.name
            or row.name == "_more"
            or (row.scope == "user" and row.kind != "directive")
        ):
            raise ValueError("Invalid source name or personal reference; repair the export first.")
        if row.created_at.tzinfo is None or row.updated_at.tzinfo is None:
            raise ValueError("Export timestamps must include a timezone.")
        slot = (target.tenant_id, target.repo_id, row.name)
        if slot in destinations or row.id in sources:
            raise ValueError("Duplicate source or destination slot; refusing an ambiguous import.")
        destinations.add(slot)
        sources.add(row.id)
        entries.append(
            MemoryEntry(
                tenant_id=target.tenant_id,
                repo_id=target.repo_id,
                name=row.name,
                title=row.title,
                body=row.body,
                kind=row.kind,
                scope=row.scope,
                created_by_account_id=account,
                source_id=row.id,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return entries


def manifest_for(entries: list[MemoryEntry]) -> dict[str, dict[str, str | int]]:
    pools: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        key = f"{entry.tenant_id}/{entry.repo_id}"
        pools[key].append(
            json.dumps(entry.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        )
    return {
        key: {
            "count": len(rows),
            "sha256": hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest(),
        }
        for key, rows in sorted(pools.items())
    }


async def run_import(entries: list[MemoryEntry], *, database_url: str, apply: bool) -> None:
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            for entry in entries:
                await import_entry(session, entry=entry)
            if not apply:
                await session.rollback()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "accounts", "repos", "manifest"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Commit; omitted means dry run.")
    args = parser.parse_args()
    try:
        entries = prepare_entries(
            TypeAdapter(list[ExportEntry]).validate_json(args.snapshot.read_text()),
            TypeAdapter(dict[str, uuid.UUID]).validate_json(args.accounts.read_text()),
            TypeAdapter(dict[str, RepoTarget]).validate_json(args.repos.read_text()),
        )
    except ValidationError:
        parser.error("Invalid export or mapping schema; check the documented input formats.")
    except ValueError as exc:
        parser.error(str(exc))
    database_url = os.environ.get("DAIMON_DATABASE__URL")
    if not database_url:
        parser.error("Set DAIMON_DATABASE__URL for the destination database.")
    asyncio.run(run_import(entries, database_url=database_url, apply=args.apply))
    args.manifest.write_text(json.dumps(manifest_for(entries), indent=2) + "\n")
    print(f"{'Applied' if args.apply else 'Dry run verified'} {len(entries)} entries.")


if __name__ == "__main__":
    main()
