"""The restricted poll scheduler cannot read credentials or submit poll evidence."""

import uuid

import pytest
from daimon.core.stores import watches
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from . import test_polling, test_watch_privilege_audit

# Effective privilege audits deliberately inspect all accessible schemas.
db_engine = test_watch_privilege_audit.db_engine
db_session_factory = test_watch_privilege_audit.db_session_factory
poll_watch = test_polling.poll_watch
pytestmark = [pytest.mark.fresh_schema, pytest.mark.usefixtures("watch_privilege_audit_lock")]


async def test_scheduler_role_only_claims_leases_and_cannot_forge_poll_evidence(
    db_engine, db_session_factory, poll_watch
):
    watch, _ = poll_watch
    async with db_session_factory() as db:
        schema = await db.scalar(text("SELECT current_schema()"))
    name = "poll_" + uuid.uuid4().hex
    async with db_engine.begin() as db:
        await db.execute(
            text(f"CREATE ROLE {name} LOGIN PASSWORD 'isolated-test' IN ROLE daimon_sidecar")
        )
    engine = create_async_engine(
        db_engine.url.set(username=name, password="isolated-test"),
        connect_args={"server_settings": {"search_path": f'"{schema}",public'}},
    )
    factory = async_sessionmaker(engine)
    try:
        async with factory.begin() as db:
            await watches.audit_sidecar_identity(db)
            leases = await watches.claim_poll_watches(db)
            assert len(leases) == 1
            assert set(leases[0]) == {"watch_id", "lease"}
            assert leases[0]["watch_id"] == str(watch.id)
        for sql in (
            "SELECT * FROM watches",
            "SELECT * FROM agent_mcp_credentials",
            "SELECT * FROM tenant_access_policies",
            "SELECT * FROM watch_capabilities",
            "UPDATE watches SET instructions='forged'",
            "INSERT INTO task_continuations(reason) VALUES('watch_fired')",
            "SELECT accept_watch_poll_result(gen_random_uuid(),'fake','null'::jsonb,'forged')",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as refused:
                    await db.execute(text(sql))
                assert getattr(refused.value.orig, "sqlstate", None) == "42501"
    finally:
        await engine.dispose()
        async with db_engine.begin() as db:
            await db.execute(text(f"DROP ROLE {name}"))
