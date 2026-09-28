"""Ciphertext, key rotation and legacy migration exercise real PostgreSQL storage."""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet, InvalidToken
from daimon.core.agent_env_crypto import PREFIX
from daimon.core.stores.agent_files import (
    get_agent_file,
    list_agent_files,
    put_agent_file,
    put_agent_file_if_unchanged,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs


async def test_ciphertext_and_rotation(db_session: AsyncSession) -> None:
    old, new = Fernet.generate_key(), Fernet.generate_key()
    db_session.info["crypto_keys"] = (old.decode(),)
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    args = dict(tenant_id=tenant.id, agent_id=agent, key="TOKEN")
    await put_agent_file(db_session, **args, content="secret-λ", set_by_account_id=None)
    ciphertext = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert "secret" not in ciphertext
    assert Fernet(old).decrypt(ciphertext.removeprefix(PREFIX).encode()).decode() == "secret-λ"
    db_session.info["crypto_keys"] = (new.decode(), old.decode())
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == "secret-λ"
    await put_agent_file(db_session, **args, content=row.content, set_by_account_id=None)
    rotated = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert Fernet(new).decrypt(rotated.removeprefix(PREFIX).encode()).decode() == "secret-λ"
    with pytest.raises(InvalidToken):
        Fernet(old).decrypt(rotated.removeprefix(PREFIX).encode())
    db_session.info["crypto_keys"] = (new.decode(),)
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == "secret-λ"


async def test_migration_mixed_rows_idempotence_and_downgrade(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key()
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    encrypted = PREFIX + Fernet(key).encrypt(b"already-secret").decode()
    await db_session.execute(
        text(
            "INSERT INTO agent_files (tenant_id, agent_id, key, content) "
            "VALUES (:tenant, :agent, 'TOKEN', 'legacy-value'), "
            "(:tenant, :agent, 'OTHER', :encrypted)"
        ),
        {"tenant": tenant.id, "agent": agent, "encrypted": encrypted},
    )
    path = Path(__file__).parents[2] / "alembic/versions/0028_sys051_agent_env_encryption.py"
    spec = importlib.util.spec_from_file_location("migration_env", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def upgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    def downgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    async def contents():
        return dict((await db_session.execute(text("SELECT key, content FROM agent_files"))).all())

    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "[]")
    await conn.run_sync(upgrade)
    assert await contents() == {"TOKEN": "legacy-value", "OTHER": encrypted}
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key.decode()]))
    await conn.run_sync(upgrade)
    first = await contents()
    assert first["OTHER"] == encrypted
    assert Fernet(key).decrypt(first["TOKEN"].removeprefix(PREFIX).encode()) == b"legacy-value"
    await conn.run_sync(upgrade)
    assert await contents() == first
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "[]")
    with pytest.raises(ValueError, match="DAIMON_CRYPTO__KEYS.*agent environment"):
        await conn.run_sync(downgrade)
    assert await contents() == first
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key.decode()]))
    await conn.run_sync(downgrade)
    assert await contents() == {"TOKEN": "legacy-value", "OTHER": "already-secret"}
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "[]")
    await conn.run_sync(downgrade)
    await conn.run_sync(upgrade)
    assert await contents() == {"TOKEN": "legacy-value", "OTHER": "already-secret"}


async def test_keyless_writes_legacy_reads_and_lazy_encryption(db_session: AsyncSession) -> None:
    db_session.info["crypto_keys"] = ()
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    args = dict(tenant_id=tenant.id, agent_id=agent, key="TOKEN")
    value = "legacy-secret-λ"
    row = await put_agent_file(db_session, **args, content=value, set_by_account_id=None)
    assert row.content == value
    updated = await put_agent_file_if_unchanged(
        db_session,
        **args,
        content=value,
        set_by_account_id=None,
        expected_updated_at=row.updated_at,
    )
    assert updated is not None and updated.content == value
    assert (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one() == value
    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),)
    with capture_logs() as logs:
        row = await get_agent_file(db_session, **args)
        rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent)
    assert row is not None and row.content == value
    assert rows[0].content == value
    assert len(logs) == 2
    assert all(log["event"] == "agent_env.legacy_plaintext" for log in logs)
    assert value not in str(logs)
    await put_agent_file(db_session, **args, content=row.content, set_by_account_id=None)
    encrypted = (await db_session.execute(text("SELECT content FROM agent_files"))).scalar_one()
    assert encrypted.startswith(PREFIX)
    assert Fernet(key).decrypt(encrypted.removeprefix(PREFIX).encode()).decode() == value
    # Writers recognize authenticated envelopes, including conditional writes.
    await put_agent_file(db_session, **args, content=encrypted, set_by_account_id=None)
    assert (
        await db_session.execute(text("SELECT content FROM agent_files"))
    ).scalar_one() == encrypted
    assert (
        await put_agent_file_if_unchanged(
            db_session,
            **args,
            content=encrypted,
            set_by_account_id=None,
            expected_updated_at=row.updated_at,
        )
        is not None
    )
    assert (
        await db_session.execute(text("SELECT content FROM agent_files"))
    ).scalar_one() == encrypted


@pytest.mark.parametrize("mode", ["wrong-key", "missing-key", "corrupt"])
async def test_encrypted_rows_never_fall_back_to_plaintext(
    db_session: AsyncSession, mode: str
) -> None:
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    args = dict(tenant_id=tenant.id, agent_id=agent, key="TOKEN")
    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),)
    await put_agent_file(db_session, **args, content="secret", set_by_account_id=None)
    if mode == "wrong-key":
        db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    elif mode == "missing-key":
        db_session.info["crypto_keys"] = ()
    else:
        await db_session.execute(text("UPDATE agent_files SET content = 'enc:v1:broken'"))
        db_session.expire_all()
    with pytest.raises(ValueError if mode == "missing-key" else InvalidToken):
        await get_agent_file(db_session, **args)
