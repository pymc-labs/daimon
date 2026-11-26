"""Offline repository-memory import (no connection to the old service).

Runbook:
1. Keep native memory disabled. Freeze old memory writes and export all rows to
   a JSON array containing id, repo_owner, repo_name, name, title, body, kind,
   scope, created_by_discord_id, created_at and updated_at. Never put that export
   or the maps in Git. Repository slugs, content and timestamps are preserved.
2. Supply repos JSON
   {"owner/name": {"tenant_id": tenant_uuid, "repo_id": github_repository_id}} and
   either accounts JSON {discord_id: account_uuid} or `--tenant TENANT_UUID`, or both.
   With `--tenant`, a creator missing from the accounts map gets (or keeps) the Discord
   identity in that tenant, as their first message would; their repositories must map
   to it. Without it every creator must map, including shared entries.
3. Run `python scripts/import_repo_memory.py --snapshot EXPORT --repos REPOS
   --tenant TENANT_UUID --manifest MANIFEST` against the destination using
   DAIMON_DATABASE__URL. The image has no `scripts/`, but this imports only
   `daimon.core`, so copy it and the inputs into a worker container and run it there:
   `docker cp scripts/import_repo_memory.py WORKER:/tmp/`, then
   `docker exec WORKER python /tmp/import_repo_memory.py ...`.
   This defaults to dry run and rolls back all writes.
   Resolve every mapping or destination conflict, then inspect the manifest's
   per-repo row counts and content digests (which include personal directives).
4. Repeat with --apply. Require its manifest to match the successful dry run.
   Exact replays are idempotent; destination edits cause a refusal, never overwrite.
5. Configure repo_memory.legacy_mcp_server_names, drain/retire old Daimon sessions,
   enable repo_memory.enabled on all participating processes, start fresh sessions
   and verify the four native tools show the imported shared/personal lists while
   the legacy memory readers are disabled. Leave old writes frozen.

This script does not switch production traffic to the imported memory.
Before Daimon writes, rollback means disabling native memory and restoring the
old service. After a Daimon write, rollback needs reverse reconciliation;
never just re-enable the stale readers.
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
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.repo_memory import MemoryEntry, import_entry
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


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


async def add_people(
    session: AsyncSession,
    export: list[ExportEntry],
    accounts: dict[str, uuid.UUID],
    repos: dict[str, RepoTarget],
    tenant: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """`accounts` plus an identity in `tenant` for every creator it leaves out."""
    canonical = {key.casefold(): value for key, value in repos.items()}
    found = dict(accounts)
    for row in export:
        creator = row.created_by_discord_id
        if creator in accounts:
            continue
        # Every row of an unmapped creator is checked, not only the first.
        target = canonical.get(f"{row.repo_owner}/{row.repo_name}".casefold())
        if target is not None and target.tenant_id != tenant:
            raise ValueError("A creator without an account writes to another tenant; map them.")
        if not (creator.isascii() and creator.isdigit()):
            raise ValueError("A creator without an account needs a Discord user id; map them.")
        if creator not in found:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant, platform="discord", external_id=creator
            )
            found[creator] = principal.account_id
    return found


def manifest_for(
    entries: list[MemoryEntry], owners: dict[uuid.UUID, str] | None = None
) -> dict[str, dict[str, str | int]]:
    """Counts and digests per repository. `owners` names each account by its Discord id,
    so a dry run and the apply that creates the same people match."""
    pools: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        key = f"{entry.tenant_id}/{entry.repo_id}"
        row = entry.model_dump(mode="json")
        owner = (
            owners.get(entry.created_by_account_id)
            if owners and entry.created_by_account_id
            else None
        )
        if owner is not None:
            row["created_by_account_id"] = owner
        pools[key].append(json.dumps(row, sort_keys=True, ensure_ascii=False))
    return {
        key: {
            "count": len(rows),
            "sha256": hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest(),
        }
        for key, rows in sorted(pools.items())
    }


async def run_import(
    export: list[ExportEntry],
    accounts: dict[str, uuid.UUID],
    repos: dict[str, RepoTarget],
    *,
    tenant: uuid.UUID | None,
    database_url: str,
    apply: bool,
) -> tuple[list[MemoryEntry], dict[str, uuid.UUID]]:
    """Import in one transaction, people added with `tenant` included; a dry run rolls
    it all back. Returns the entries and every creator's account."""
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            if tenant is not None:
                accounts = await add_people(session, export, accounts, repos, tenant)
            entries = prepare_entries(export, accounts, repos)
            for entry in entries:
                await import_entry(session, entry=entry)
            if not apply:
                await session.rollback()
    finally:
        await engine.dispose()
    return entries, accounts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "repos", "manifest"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--accounts", type=Path, help="JSON {discord_id: account_uuid}.")
    parser.add_argument("--tenant", type=uuid.UUID, help="Add unmapped creators to this tenant.")
    parser.add_argument("--apply", action="store_true", help="Commit; omitted means dry run.")
    args = parser.parse_args()
    try:
        export = TypeAdapter(list[ExportEntry]).validate_json(args.snapshot.read_text())
        accounts = (
            TypeAdapter(dict[str, uuid.UUID]).validate_json(args.accounts.read_text())
            if args.accounts
            else {}
        )
        repos = TypeAdapter(dict[str, RepoTarget]).validate_json(args.repos.read_text())
    except ValidationError:
        parser.error("Invalid export or mapping schema; check the documented input formats.")
    database_url = os.environ.get("DAIMON_DATABASE__URL")
    if not database_url:
        parser.error("Set DAIMON_DATABASE__URL for the destination database.")
    try:
        entries, owners = asyncio.run(
            run_import(
                export,
                accounts,
                repos,
                tenant=args.tenant,
                database_url=database_url,
                apply=args.apply,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    names = {account: discord_id for discord_id, account in owners.items()}
    args.manifest.write_text(json.dumps(manifest_for(entries, names), indent=2) + "\n")
    print(f"{'Applied' if args.apply else 'Dry run verified'} {len(entries)} entries.")


if __name__ == "__main__":
    main()
