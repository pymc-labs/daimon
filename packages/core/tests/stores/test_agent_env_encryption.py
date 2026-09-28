"""Ciphertext, key rotation and legacy migration exercise real PostgreSQL storage."""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet, InvalidToken
from daimon.core.stores.agent_files import get_agent_file, put_agent_file
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_ciphertext_and_rotation(db_session: AsyncSession) -> None:
    old, new = Fernet.generate_key(), Fernet.generate_key()
    db_session.info["crypto_keys"] = (old.decode(),)
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    args = dict(tenant_id=tenant.id, agent_id=agent, key="TOKEN")
    await put_agent_file(db_session, **args, content="secret-λ", set_by_account_id=None)
    ciphertext = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert "secret" not in ciphertext
    assert Fernet(old).decrypt(ciphertext.encode()).decode() == "secret-λ"
    db_session.info["crypto_keys"] = (new.decode(), old.decode())
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == "secret-λ"
    await put_agent_file(db_session, **args, content=row.content, set_by_account_id=None)
    rotated = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert Fernet(new).decrypt(rotated.encode()).decode() == "secret-λ"
    with pytest.raises(InvalidToken):
        Fernet(old).decrypt(rotated.encode())
    db_session.info["crypto_keys"] = (new.decode(),)
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == "secret-λ"


async def test_migration_encrypts_legacy_rows(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key()
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key.decode()]))
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO agent_files (tenant_id, agent_id, key, content) "
            "VALUES (:tenant, :agent, 'TOKEN', 'legacy-value')"
        ),
        {"tenant": tenant.id, "agent": agent},
    )
    path = Path(__file__).parents[2] / "alembic/versions/0028_agent_env_encryption.py"
    spec = importlib.util.spec_from_file_location("migration_env", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "[]")
    with pytest.raises(ValueError, match="at least one Fernet key"):
        await conn.run_sync(upgrade)
    unchanged = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert unchanged == "legacy-value"
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key.decode()]))
    await conn.run_sync(upgrade)
    raw = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert raw != "legacy-value"
    assert Fernet(key).decrypt(raw.encode()) == b"legacy-value"
    db_session.info["crypto_keys"] = (key.decode(),)
    row = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent, key="TOKEN")
    assert row is not None and row.content == "legacy-value"
