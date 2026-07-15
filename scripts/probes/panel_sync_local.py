"""Local repro of the Discord panel skill_sync flow — no Discord needed.

Mirrors `daimon.adapters.discord.agent_setup.write.kick_off_skill_sync` exactly:
  fernet (built from settings) -> sync_agent_skills(...)

Runs against the SAME staging MA + Postgres the panel hits, via .env.fly.
Prints the full SyncReport so we don't have to grep fly logs after every
panel click.

Setup:
  set -a; source .env.fly; set +a

Run:
  uv run python scripts/probes/panel_sync_local.py <account_id> <agent_name> <repo_url>

Example:
  uv run python scripts/probes/panel_sync_local.py \\
    fd2f683c-8f08-4a9f-a734-9f487ad99b83 \\
    daimon-copy \\
    https://github.com/Wangnov/gpt-image-2-skill
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import httpx
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession

from daimon.core.config import load_settings
from daimon.core.skill_sync import sync_agent_skills
from daimon.core.specs import SkillRepo


def _build_fernet(secret_keys: list) -> MultiFernet:
    from cryptography.fernet import Fernet
    return MultiFernet([
        Fernet(k.get_secret_value().encode() if hasattr(k, "get_secret_value") else k.encode())
        for k in secret_keys
    ])


async def _resolve_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> uuid.UUID:
    from sqlalchemy import text
    async with sessionmaker() as s:
        row = (
            await s.execute(
                text("SELECT tenant_id FROM accounts WHERE id = :a"),
                {"a": str(account_id)},
            )
        ).first()
        if row is None:
            raise RuntimeError(f"no account {account_id}")
        return uuid.UUID(str(row[0]))


async def main(account_id_arg: str, agent_name: str, repo_url: str) -> None:
    settings = load_settings()
    account_id = uuid.UUID(account_id_arg)

    engine = create_async_engine(str(settings.database.url))
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = await _resolve_tenant(sessionmaker, account_id)
    print(f"resolved tenant_id={tenant_id} for account={account_id}")

    fernet = _build_fernet(settings.crypto.keys)
    api_key = settings.anthropic.api_key
    if hasattr(api_key, "get_secret_value"):
        api_key = api_key.get_secret_value()
    anthropic_client = AsyncAnthropic(api_key=api_key)

    async with httpx.AsyncClient() as http_client:
        report = await sync_agent_skills(
            principal_id=account_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
            repos=[SkillRepo(url=repo_url, branch="main", path="", split=True)],
            sessionmaker=sessionmaker,
            fernet=fernet,
            http_client=http_client,
            anthropic_client=anthropic_client,
        )

    print()
    print("=== SyncReport ===")
    print(f"synced:          {report.synced}")
    print(f"updated:         {report.updated}")
    print(f"deleted:         {report.deleted}")
    print(f"skipped_repos:   {report.skipped_repos}")
    print(f"skipped_skills:  {report.skipped_skills}")
    print(f"failed_uploads:  {report.failed_uploads}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: panel_sync_local.py <account_id> <agent_name> <repo_url>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
