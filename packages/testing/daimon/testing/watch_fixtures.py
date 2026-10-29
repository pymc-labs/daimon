"""Install the frozen ingress ports into a process-owned throwaway schema."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def watch_migration() -> dict[str, object]:
    return runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "core/alembic/versions/0142_watches.py")
    )


async def install_watch_ports(session: AsyncSession) -> None:
    schema = await session.scalar(text("SELECT current_schema()"))
    if not isinstance(schema, str) or not schema.startswith("test_"):
        raise ValueError("Watch fixtures require an owned test schema")
    quoted = '"' + schema + '"'
    migration = watch_migration()
    for key in ("ACCEPT_SQL", "PRUNE_SQL"):
        await session.execute(text(cast(str, migration[key]).format(schema=quoted)))
    await session.execute(
        text(f"GRANT USAGE,CREATE ON SCHEMA {quoted} TO daimon_watch_ingress_owner")
    )
    await session.execute(text(f"REVOKE CREATE ON SCHEMA {quoted} FROM PUBLIC,daimon_sidecar"))
    for table in (
        "watches",
        "watch_capabilities",
        "watch_controls",
        "accounts",
        "tenants",
        "platform_principals",
        "thread_sessions",
    ):
        await session.execute(
            text(f"GRANT SELECT ON {quoted}.{table} TO daimon_watch_ingress_owner")
        )
    await session.execute(
        text(f"GRANT UPDATE(fire_count) ON {quoted}.watches TO daimon_watch_ingress_owner")
    )
    for table in ("watch_inbox", "watch_firings"):
        await session.execute(
            text(
                f"GRANT SELECT,INSERT,UPDATE,DELETE ON {quoted}.{table} "
                "TO daimon_watch_ingress_owner"
            )
        )
    await session.execute(
        text(
            f"GRANT SELECT,INSERT,UPDATE(status) ON {quoted}.task_continuations "
            "TO daimon_watch_ingress_owner"
        )
    )
    for signature in (
        "accept_watch_page_event(uuid,uuid,text,bigint,uuid,text,text,text)",
        "prune_watch_evidence()",
    ):
        await session.execute(text(f"REVOKE ALL ON FUNCTION {quoted}.{signature} FROM PUBLIC"))
        await session.execute(
            text(f"ALTER FUNCTION {quoted}.{signature} OWNER TO daimon_watch_ingress_owner")
        )
        await session.execute(
            text(f"GRANT EXECUTE ON FUNCTION {quoted}.{signature} TO daimon_sidecar")
        )
    await session.execute(text(f"REVOKE CREATE ON SCHEMA {quoted} FROM daimon_watch_ingress_owner"))
    await session.execute(text(f"GRANT USAGE ON SCHEMA {quoted} TO daimon_sidecar"))


async def install_poll_ports(session: AsyncSession) -> None:
    """Install the successor polling ports into the same owned throwaway schema."""
    await install_watch_ports(session)
    schema = await session.scalar(text("SELECT current_schema()"))
    assert isinstance(schema, str) and schema.startswith("test_")
    quoted = '"' + schema + '"'
    migration = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "core/alembic/versions/0143_watch_polling.py")
    )
    for key in ("PROJECT_SQL", "PAGE_SQL", "POLL_SQL", "CLAIM_SQL"):
        await session.execute(text(migration[key].format(schema=quoted)))
    # 0156 replaces the firing transaction with the larger kept bounds.
    bounds = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "core/alembic/versions/0156_watch_mcp_sources.py")
    )
    await session.execute(text(cast(str, bounds["POLL_SQL"]).format(schema=quoted)))
    await session.execute(
        text(f"REVOKE ALL ON FUNCTION {quoted}.project_watch_document(jsonb,text) FROM PUBLIC")
    )
    await session.execute(
        text(
            f"GRANT EXECUTE ON FUNCTION {quoted}.project_watch_document(jsonb,text) "
            "TO daimon_watch_ingress_owner"
        )
    )
    await session.execute(
        text(
            f"REVOKE ALL ON FUNCTION {quoted}.accept_watch_poll_result(uuid,text,jsonb,text) "
            "FROM PUBLIC,daimon_sidecar,daimon_watch_ingress_owner"
        )
    )
    await session.execute(
        text(
            f"GRANT UPDATE(status,revision,lease_owner,lease_expires_at) "
            f"ON {quoted}.watches TO daimon_watch_ingress_owner"
        )
    )
    await session.execute(text(f"GRANT CREATE ON SCHEMA {quoted} TO daimon_watch_ingress_owner"))
    await session.execute(text(f"REVOKE ALL ON FUNCTION {quoted}.claim_watch_polls() FROM PUBLIC"))
    await session.execute(
        text(f"ALTER FUNCTION {quoted}.claim_watch_polls() OWNER TO daimon_watch_ingress_owner")
    )
    await session.execute(
        text(f"GRANT EXECUTE ON FUNCTION {quoted}.claim_watch_polls() TO daimon_sidecar")
    )
    # 0157 replaces the ingress-owned claim so it also leases mcp_poll watches,
    # then restates the ownership and grants, as the migration does.
    claim = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "core/alembic/versions/0157_watch_mcp_claim.py")
    )
    await session.execute(text(cast(str, claim["CLAIM_SQL"]).format(schema=quoted)))
    await session.execute(text(f"REVOKE ALL ON FUNCTION {quoted}.claim_watch_polls() FROM PUBLIC"))
    await session.execute(
        text(f"ALTER FUNCTION {quoted}.claim_watch_polls() OWNER TO daimon_watch_ingress_owner")
    )
    await session.execute(
        text(f"GRANT EXECUTE ON FUNCTION {quoted}.claim_watch_polls() TO daimon_sidecar")
    )
    await session.execute(text(f"REVOKE CREATE ON SCHEMA {quoted} FROM daimon_watch_ingress_owner"))
