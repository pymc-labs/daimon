"""Encryption metadata, arbitrary literal values, rotation and real-DB migrations."""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet, InvalidToken
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


async def _migrate(session: AsyncSession, *, downgrade: bool = False) -> None:
    path = Path(__file__).parents[2] / "alembic/versions/0028_sys051_agent_env_encryption.py"
    spec = importlib.util.spec_from_file_location("migration_env", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await session.connection()

    def run(sync_conn):
        # A real Alembic command rolls back on failure. Preserve that atomicity
        # inside the fixture's surrounding transaction, including failed DDL.
        with sync_conn.begin_nested(), Operations.context(MigrationContext.configure(sync_conn)):
            if downgrade:
                migration.downgrade()
            else:
                migration.upgrade()

    await conn.run_sync(run)
    session.expire_all()


async def _stored(session: AsyncSession) -> dict[str, tuple[str, str]]:
    return {
        key: (content, encoding)
        for key, content, encoding in (
            await session.execute(text("SELECT key, content, encoding FROM agent_files"))
        ).all()
    }


async def test_ciphertext_and_rotation(db_session: AsyncSession) -> None:
    old, new = Fernet.generate_key(), Fernet.generate_key()
    db_session.info["crypto_keys"] = (old.decode(),)
    tenant = await make_tenant(db_session)
    args = dict(tenant_id=tenant.id, agent_id=uuid.uuid4(), key="TOKEN")
    await put_agent_file(db_session, **args, content="secret-λ", set_by_account_id=None)
    ciphertext, encoding = (await _stored(db_session))["TOKEN"]
    assert encoding == "fernet_v1"
    assert Fernet(old).decrypt(ciphertext.encode()).decode() == "secret-λ"
    db_session.info["crypto_keys"] = (new.decode(), old.decode())
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == "secret-λ"
    await put_agent_file(db_session, **args, content=row.content, set_by_account_id=None)
    rotated, encoding = (await _stored(db_session))["TOKEN"]
    assert encoding == "fernet_v1"
    assert Fernet(new).decrypt(rotated.encode()).decode() == "secret-λ"
    with pytest.raises(InvalidToken):
        Fernet(old).decrypt(rotated.encode())
    db_session.info["crypto_keys"] = (new.decode(),)
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == "secret-λ"


@pytest.mark.parametrize("value", ["legacy-secret-λ", "enc:v1:", "enc:v1:application-owned-value"])
async def test_keyless_writes_legacy_reads_and_lazy_encryption(
    db_session: AsyncSession, value: str
) -> None:
    db_session.info["crypto_keys"] = ()
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    args = dict(tenant_id=tenant.id, agent_id=agent, key="TOKEN")
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
    assert (await _stored(db_session))["TOKEN"] == (value, "plain")
    # Simulate a legacy insert: no encoding supplied, even for prefix-shaped text.
    await db_session.execute(text("DELETE FROM agent_files"))
    await db_session.execute(
        text(
            "INSERT INTO agent_files (tenant_id, agent_id, key, content) VALUES (:t, :a, 'TOKEN', :v)"
        ),
        {"t": tenant.id, "a": agent, "v": value},
    )
    db_session.expire_all()
    row = await get_agent_file(db_session, **args)
    assert row is not None and row.content == value
    assert [
        r.content
        for r in await list_agent_files(db_session, tenant_id=args["tenant_id"], agent_id=agent)
    ] == [value]
    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),)
    with capture_logs() as logs:
        row = await get_agent_file(db_session, **args)
        rows = await list_agent_files(db_session, tenant_id=args["tenant_id"], agent_id=agent)
    assert row is not None and row.content == value
    assert rows[0].content == value
    assert len(logs) == 2
    assert all(log["event"] == "agent_env.legacy_plaintext" for log in logs)
    assert value not in str(logs)
    # CAS changes the value and encoding together, even on a cached ORM instance.
    encrypted_row = await put_agent_file_if_unchanged(
        db_session,
        **args,
        content=value,
        set_by_account_id=None,
        expected_updated_at=row.updated_at,
    )
    assert encrypted_row is not None and encrypted_row.content == value
    encrypted, encoding = (await _stored(db_session))["TOKEN"]
    assert encoding == "fernet_v1"
    assert Fernet(key).decrypt(encrypted.encode()).decode() == value
    # A token-shaped user value is still literal input, never a storage envelope.
    for literal in (encrypted, "enc:v1:" + encrypted):
        result = await put_agent_file(db_session, **args, content=literal, set_by_account_id=None)
        assert result.content == literal
        ciphertext, encoding = (await _stored(db_session))["TOKEN"]
        assert encoding == "fernet_v1"
        assert Fernet(key).decrypt(ciphertext.encode()).decode() == literal
    # Removing configured keys makes the next explicit write plain, atomically.
    db_session.info["crypto_keys"] = ()
    result = await put_agent_file(db_session, **args, content=value, set_by_account_id=None)
    assert result.content == value
    assert (await _stored(db_session))["TOKEN"] == (value, "plain")


@pytest.mark.parametrize("keyed", [False, True])
async def test_conditional_insert_preserves_prefix_plaintext(
    db_session: AsyncSession, keyed: bool
) -> None:
    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),) if keyed else ()
    tenant = await make_tenant(db_session)
    args = dict(tenant_id=tenant.id, agent_id=uuid.uuid4(), key="TOKEN")
    value = "enc:v1:literal"
    row = await put_agent_file_if_unchanged(
        db_session,
        **args,
        content=value,
        set_by_account_id=None,
        expected_updated_at=None,
    )
    assert row is not None and row.content == value
    stored, encoding = (await _stored(db_session))["TOKEN"]
    assert encoding == ("fernet_v1" if keyed else "plain")
    assert (Fernet(key).decrypt(stored.encode()).decode() if keyed else stored) == value
    assert (
        await put_agent_file_if_unchanged(
            db_session,
            **args,
            content="replacement",
            set_by_account_id=None,
            expected_updated_at=None,
        )
        is None
    )
    assert (await _stored(db_session))["TOKEN"] == (stored, encoding)


async def test_migration_prefix_plaintext_idempotence_and_downgrade(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Fernet.generate_key()
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    agent = uuid.uuid4()
    literal_token = Fernet(key).encrypt(b"application-owned-value").decode()
    originals = {
        "TOKEN": "legacy-value",
        "PREFIX": "enc:v1:application-owned-value",
        "EMPTY": "enc:v1:",
        "VALID_TOKEN": literal_token,
        "VALID_PREFIX": "enc:v1:" + literal_token,
    }
    # Exercise real schema addition against pre-feature rows.
    await db_session.execute(text("ALTER TABLE agent_files DROP COLUMN encoding"))
    for name, value in originals.items():
        await db_session.execute(
            text(
                "INSERT INTO agent_files (tenant_id, agent_id, key, content) VALUES (:t, :a, :k, :v)"
            ),
            {"t": tenant_id, "a": agent, "k": name, "v": value},
        )
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "[]")
    await _migrate(db_session)
    assert await _stored(db_session) == {k: (v, "plain") for k, v in originals.items()}
    await _migrate(db_session, downgrade=True)
    assert (
        dict((await db_session.execute(text("SELECT key, content FROM agent_files"))).all())
        == originals
    )
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key.decode()]))
    await _migrate(db_session)
    first = await _stored(db_session)
    assert all(encoding == "fernet_v1" for _, encoding in first.values())
    assert {k: Fernet(key).decrypt(v.encode()).decode() for k, (v, _) in first.items()} == originals
    # Mixed state: a new legacy insertion must be encrypted, existing ciphertext unchanged.
    await db_session.execute(
        text(
            "INSERT INTO agent_files (tenant_id, agent_id, key, content) VALUES (:t, :a, 'LATE', :v)"
        ),
        {"t": tenant_id, "a": agent, "v": "enc:v1:late-legacy"},
    )
    originals["LATE"] = "enc:v1:late-legacy"
    await _migrate(db_session)
    second = await _stored(db_session)
    assert {k: second[k] for k in first} == first
    await _migrate(db_session)
    assert await _stored(db_session) == second
    await _migrate(db_session, downgrade=True)
    assert (
        dict((await db_session.execute(text("SELECT key, content FROM agent_files"))).all())
        == originals
    )
    # Downgrade removed the encoding column (not just its values).
    await _migrate(db_session)
    assert all(encoding == "fernet_v1" for _, encoding in (await _stored(db_session)).values())


@pytest.mark.parametrize("mode", ["wrong-key", "missing-key", "corrupt"])
async def test_encrypted_rows_never_fall_back_to_plaintext(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    tenant = await make_tenant(db_session)
    args = dict(tenant_id=tenant.id, agent_id=uuid.uuid4(), key="TOKEN")
    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),)
    await put_agent_file(db_session, **args, content="secret", set_by_account_id=None)
    if mode == "wrong-key":
        key = Fernet.generate_key()
    elif mode == "corrupt":
        await db_session.execute(text("UPDATE agent_files SET content = 'enc:v1:broken'"))
        db_session.expire_all()
    keys = [] if mode == "missing-key" else [key.decode()]
    db_session.info["crypto_keys"] = tuple(keys)
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps(keys))
    error = ValueError if mode == "missing-key" else InvalidToken
    with pytest.raises(error):
        await get_agent_file(db_session, **args)
    with pytest.raises(error):
        await list_agent_files(db_session, tenant_id=args["tenant_id"], agent_id=args["agent_id"])
    before = await _stored(db_session)
    with pytest.raises(error):
        await _migrate(db_session, downgrade=True)
    assert await _stored(db_session) == before
