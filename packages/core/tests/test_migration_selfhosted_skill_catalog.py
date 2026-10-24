"""The catalog extension retains existing rows and admits local selfhosted bundles."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import inspect, text


@pytest.mark.fresh_schema
async def test_selfhosted_skill_catalog_upgrade_preserves_existing_versions(db_session):
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    path = Path(__file__).parents[1] / "alembic/versions/0113_selfhosted_skill_catalog.py"
    spec = importlib.util.spec_from_file_location("selfhosted_skill_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = await db_session.connection()

    def upgrade(sync):
        sync.execute(
            text("ALTER TABLE neutral_skill_versions DROP CONSTRAINT ck_neutral_skill_provider")
        )
        sync.execute(
            text(
                "ALTER TABLE neutral_skill_versions ADD CONSTRAINT ck_neutral_skill_provider CHECK (provider IN ('openai', 'gemini'))"
            )
        )
        insert = text(
            "INSERT INTO neutral_skill_versions VALUES "
            "(:tenant, :account, :provider, 'workspace', 'skill', 'v1', 'principal', "
            "'agent', 'skill', '{}'::jsonb, decode('00', 'hex'))"
        )
        values = {"tenant": tenant.id, "account": account.id}
        sync.execute(insert, {**values, "provider": "openai"})
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
            with pytest.raises(NotImplementedError, match="cannot be discarded"):
                module.downgrade()
        sync.execute(insert, {**values, "provider": "selfhosted"})
        assert sync.scalars(
            text("SELECT provider FROM neutral_skill_versions ORDER BY provider")
        ).all() == ["openai", "selfhosted"]
        checks = inspect(sync).get_check_constraints("neutral_skill_versions")
        assert "selfhosted" in next(
            check["sqltext"] for check in checks if check["name"] == "ck_neutral_skill_provider"
        )

    await connection.run_sync(upgrade)


@pytest.mark.fresh_schema
async def test_attachment_upgrade_preserves_existing_agents(db_session):
    tenant = await make_tenant(db_session)
    path = Path(__file__).parents[1] / "alembic/versions/0114_selfhosted_skill_pins.py"
    spec = importlib.util.spec_from_file_location("selfhosted_attachment_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = await db_session.connection()

    def upgrade(sync):
        sync.execute(text("ALTER TABLE selfhosted_agents DROP COLUMN skill_attachments"))
        sync.execute(
            text(
                "INSERT INTO selfhosted_agents (id, tenant_id, name, spec, revision) "
                "VALUES ('sh_agent_existing', :tenant, 'existing', '{}'::jsonb, 1)"
            ),
            {"tenant": tenant.id},
        )
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
            with pytest.raises(NotImplementedError, match="cannot be discarded"):
                module.downgrade()
        assert sync.execute(text("SELECT id, skill_attachments FROM selfhosted_agents")).all() == [
            ("sh_agent_existing", [])
        ]

    await connection.run_sync(upgrade)
