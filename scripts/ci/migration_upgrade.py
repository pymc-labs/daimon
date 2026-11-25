#!/usr/bin/env python3
"""Upgrade a database from an earlier schema to this tree's head, the way a deploy does.

For each start revision (the latest release tag, and the commit this one builds on) it:
  1. checks the revision's migration chain still leads to the single head here;
  2. builds that revision's schema as a role that is not a superuser, with that
     revision's own migration files, and seeds one row per stable table;
  3. upgrades to head as the same role and checks the rows survived.

    migration_upgrade.py ADMIN_DSN [--db NAME] [--base REV ...]

ADMIN_DSN is a superuser connection, used only to create the role and databases.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
ROLE = "daimon_migrator"
SEED = (
    "INSERT INTO tenants (id, platform, external_id) VALUES ('{t}', 'discord', 'guild-1')",
    "INSERT INTO tenant_config (tenant_id) VALUES ('{t}')",
    "INSERT INTO channel_config (tenant_id, channel_id) VALUES ('{t}', 'chan-1')",
    "INSERT INTO tenant_ledger (tenant_id, delta_usd, reason, idempotency_key)"
    " VALUES ('{t}', 5, 'grant', 'seed-1')",
    "INSERT INTO thread_sessions (tenant_id, platform, thread_id, ma_session_id)"
    " VALUES ('{t}', 'discord', 'thread-1', 'sess-1')",
    "INSERT INTO routines (tenant_id, agent_id, agent_name, cron_expr, trigger_message)"
    " VALUES ('{t}', 'agent-1', 'daimon', '0 9 * * *', 'hello')",
)
TENANT = "00000000-0000-4000-8000-000000000001"
TABLES = (
    "tenants",
    "tenant_config",
    "channel_config",
    "tenant_ledger",
    "thread_sessions",
    "routines",
)


def git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def default_bases() -> list[str]:
    release = git("tag", "--merged", "HEAD", "--sort=-v:refname", "--list", "v*").splitlines()
    parent = git("rev-parse", "--verify", "-q", "HEAD^1")
    return [*release[:1], *([parent] if parent else [])]


def with_db(dsn: str, db: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or "localhost"
    netloc = f"{host}:{parts.port}" if parts.port else host
    if user:
        netloc = f"{user}:{password}@{netloc}"
    elif parts.username:
        netloc = f"{parts.username}:{parts.password}@{netloc}"
    return urlunsplit((parts.scheme, netloc, "/" + db, "", ""))


def alembic(chdir: Path, url: str) -> None:
    env = {
        **os.environ,
        "DAIMON_DATABASE__URL": url.replace("postgresql://", "postgresql+asyncpg://"),
    }
    env.pop("DAIMON_DATABASE_URL", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=chdir, env=env, check=True
    )


def chain(root: Path) -> tuple[list[str], set[str]]:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "packages/core/alembic"))
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    return heads, {rev.revision for rev in script.walk_revisions()}


async def check(admin: str, base: str, workdir: Path, db: str, password: str) -> None:
    old = workdir / "base"
    old.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", base, "packages/core/alembic", "alembic.ini"],
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive.stdout, check=True)

    old_heads, _ = chain(old)
    heads, ancestors = chain(ROOT)
    assert len(heads) == 1, f"{len(heads)} migration heads, want 1: {heads}"
    stray = [rev for rev in old_heads if rev not in ancestors]
    assert not stray, f"{base} is at {stray}, which the head {heads[0]} does not descend from"

    conn = await asyncpg.connect(with_db(admin, "postgres"))
    await conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    await conn.execute(f'CREATE DATABASE "{db}" OWNER {ROLE}')
    await conn.close()

    url = with_db(admin, db, ROLE, password)
    alembic(old, url)
    conn = await asyncpg.connect(url)
    for statement in SEED:
        await conn.execute(statement.format(t=TENANT))
    await conn.close()

    alembic(ROOT, url)
    conn = await asyncpg.connect(url)
    version = await conn.fetchval("SELECT version_num FROM alembic_version")
    assert version == heads[0], f"upgraded to {version}, head is {heads[0]}"
    for table in TABLES:
        count = await conn.fetchval(f"SELECT count(*) FROM {table}")
        assert count == 1, f"{table} has {count} rows after the upgrade from {base}, want 1"
    # The migrator can create roles, so the data store's revision set the store up.
    assert await conn.fetchval("SELECT to_regnamespace('data') IS NOT NULL"), "no data schema"
    await conn.close()
    print(f"ok: {base} ({', '.join(old_heads)}) -> {heads[0]}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("admin_dsn")
    parser.add_argument("--db", default="daimon_upgrade_check")
    parser.add_argument("--base", action="append", default=[])
    args = parser.parse_args()
    bases = args.base or default_bases()
    assert bases, "no release tag or parent commit to upgrade from"

    password = "migrator"
    admin = await asyncpg.connect(with_db(args.admin_dsn, "postgres"))
    await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    await admin.execute(f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER CREATEROLE PASSWORD '{password}'")
    await admin.close()
    try:
        for base in bases:
            with tempfile.TemporaryDirectory() as tmp:
                await check(args.admin_dsn, base, Path(tmp), args.db, password)
    finally:
        admin = await asyncpg.connect(with_db(args.admin_dsn, "postgres"))
        await admin.execute(f'DROP DATABASE IF EXISTS "{args.db}" WITH (FORCE)')
        await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
