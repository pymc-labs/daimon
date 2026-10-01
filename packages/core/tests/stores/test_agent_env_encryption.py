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
    # Keyless writes are the local-development opt-in; without it they refuse.
    db_session.info["crypto_allow_plaintext"] = True
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
    db_session.info["crypto_allow_plaintext"] = not keyed
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
    with capture_logs() as logs:
        await _migrate(db_session)
    assert [log["event"] for log in logs] == ["agent_env.migration_keyless_noop"]
    assert all(value not in str(logs) for value in originals.values())
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
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key.decode()]))
    await _migrate(db_session)
    await put_agent_file(db_session, **args, content="secret", set_by_account_id=None)
    if mode == "wrong-key":
        key = Fernet.generate_key()
    elif mode == "corrupt":
        # Simulate damaged *tagged* ciphertext, not a legacy plaintext write.
        await db_session.execute(text("SET LOCAL daimon.agent_env_writer = 'v1'"))
        await db_session.execute(text("UPDATE agent_files SET content = 'enc:v1:broken'"))
        await db_session.execute(text("SET LOCAL daimon.agent_env_writer = ''"))
        db_session.expire_all()
    keys = [] if mode == "missing-key" else [key.decode()]
    db_session.info["crypto_keys"] = tuple(keys)
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps(keys))
    with pytest.raises(ValueError, match="DAIMON_CRYPTO__KEYS") as raised:
        await get_agent_file(db_session, **args)
    with pytest.raises(ValueError, match="DAIMON_CRYPTO__KEYS"):
        await list_agent_files(db_session, tenant_id=args["tenant_id"], agent_id=args["agent_id"])
    before = await _stored(db_session)
    with pytest.raises(ValueError, match="DAIMON_CRYPTO__KEYS") as migration_error:
        await _migrate(db_session, downgrade=True)
    assert await _stored(db_session) == before

    for message in (str(raised.value), str(migration_error.value)):
        if mode != "missing-key":
            assert str(args["tenant_id"]) in message
            assert str(args["agent_id"]) in message
            assert args["key"] in message
            assert "cannot be decrypted" in message
        assert "secret" not in message
        assert before["TOKEN"][0] not in message
        assert key.decode() not in message


# Frozen write SQL from origin/main 4ce6d457's stores/agent_files.py. Neither
# statement knows about encoding or the new-writer marker. Keep these legacy
# statements unchanged so this cannot accidentally test the new store twice.
_LEGACY_UPSERT = text("""
    INSERT INTO agent_files
        (tenant_id, agent_id, key, content, created_by_account_id, last_set_by_account_id)
    VALUES (:tenant, :agent, :key, :content, :actor, :actor)
    ON CONFLICT ON CONSTRAINT pk_agent_files DO UPDATE
    SET content = :content, updated_at = now(), last_set_by_account_id = :actor
    RETURNING tenant_id, agent_id, key, content, created_by_account_id,
              last_set_by_account_id, created_at, updated_at
""")
_LEGACY_CAS = text("""
    UPDATE agent_files
    SET content = :content, updated_at = now(), last_set_by_account_id = :actor
    WHERE tenant_id = :tenant AND agent_id = :agent AND key = :key
      AND updated_at = :expected
    RETURNING tenant_id, agent_id, key, content, created_by_account_id,
              last_set_by_account_id, created_at, updated_at
""")


@pytest.mark.parametrize("legacy_statement", [_LEGACY_UPSERT, _LEGACY_CAS], ids=["upsert", "cas"])
@pytest.mark.parametrize("literal", ["rotated-by-old-writer", "enc:v1:old-literal"])
async def test_legacy_update_after_encryption_remains_readable_and_reversible(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    legacy_statement,
    literal: str,
) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", json.dumps([key]))
    db_session.info["crypto_keys"] = (key,)
    tenant = await make_tenant(db_session)
    tenant_id, agent_id = tenant.id, uuid.uuid4()
    await _migrate(db_session)  # Install the real trigger, not a store mock.
    args = dict(tenant_id=tenant_id, agent_id=agent_id, key="TOKEN")

    row = await put_agent_file_if_unchanged(
        db_session,
        **args,
        content="first",
        set_by_account_id=None,
        expected_updated_at=None,
    )
    assert row is not None and row.content == "first"
    assert (await _stored(db_session))["TOKEN"][1] == "fernet_v1"
    row = await put_agent_file(db_session, **args, content="second", set_by_account_id=None)
    assert row.content == "second"
    assert (await _stored(db_session))["TOKEN"][1] == "fernet_v1"
    row = await put_agent_file_if_unchanged(
        db_session,
        **args,
        content="third",
        set_by_account_id=None,
        expected_updated_at=row.updated_at,
    )
    assert row is not None and row.content == "third"
    assert (await _stored(db_session))["TOKEN"][1] == "fernet_v1"
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        key="KEEPER",
        content="still-encrypted",
        set_by_account_id=None,
    )
    # Same transaction as the new writes: the marker must not leak to later SQL.
    updated = await db_session.execute(
        legacy_statement,
        {
            "tenant": tenant_id,
            "agent": agent_id,
            "key": "TOKEN",
            "content": literal,
            "actor": None,
            "expected": row.updated_at,
        },
    )
    assert updated.one().content == literal
    assert (await _stored(db_session))["TOKEN"] == (literal, "plain")
    db_session.expire_all()
    rows = await list_agent_files(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert {r.key: r.content for r in rows} == {"TOKEN": literal, "KEEPER": "still-encrypted"}
    await _migrate(db_session, downgrade=True)
    assert dict((await db_session.execute(text("SELECT key, content FROM agent_files"))).all()) == {
        "TOKEN": literal,
        "KEEPER": "still-encrypted",
    }
    assert not (
        await db_session.execute(
            text("""
        SELECT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'agent_files'::regclass
                       AND tgname = 'daimon_agent_env_encoding_guard')
    """)
        )
    ).scalar_one()
    assert not (
        await db_session.execute(
            text("""
        SELECT EXISTS (SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                       WHERE n.nspname = current_schema()
                         AND p.proname = 'daimon_agent_env_encoding_guard')
    """)
        )
    ).scalar_one()


@pytest.mark.parametrize("conditional", [False, True], ids=["upsert", "compare-and-set"])
async def test_keyless_write_fails_closed_without_the_plaintext_opt_in(
    db_session: AsyncSession, conditional: bool
) -> None:
    """H3/H6: no crypto keys and no opt-in means no agent key is stored at all."""
    from daimon.core.stores.agent_files import (
        AgentEnvEncryptionRequiredError,
        agent_env_writes_allowed,
    )

    db_session.info["crypto_keys"] = ()
    db_session.info["crypto_allow_plaintext"] = False
    tenant = await make_tenant(db_session)
    args = dict(tenant_id=tenant.id, agent_id=uuid.uuid4(), key="TOKEN")

    assert agent_env_writes_allowed(db_session) is False
    with pytest.raises(AgentEnvEncryptionRequiredError, match="DAIMON_CRYPTO__KEYS"):
        if conditional:
            await put_agent_file_if_unchanged(
                db_session,
                **args,
                content="secret",
                set_by_account_id=None,
                expected_updated_at=None,
            )
        else:
            await put_agent_file(db_session, **args, content="secret", set_by_account_id=None)
    assert await _stored(db_session) == {}, "nothing may be written in plaintext"

    db_session.info["crypto_allow_plaintext"] = True
    assert agent_env_writes_allowed(db_session) is True
    await put_agent_file(db_session, **args, content="dev-only", set_by_account_id=None)
    assert (await _stored(db_session))["TOKEN"] == ("dev-only", "plain")


async def test_encrypt_plaintext_rewrites_legacy_rows_and_verify_counts_them(
    db_session: AsyncSession,
) -> None:
    """The operator step after enabling keys: find plaintext rows, encrypt them in place."""
    from daimon.core.stores.agent_files import (
        count_plaintext_agent_files,
        encrypt_plaintext_agent_files,
    )

    db_session.info["crypto_keys"] = ()
    db_session.info["crypto_allow_plaintext"] = True
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    for key in ("A", "B"):
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent,
            key=key,
            content=f"value-{key}",
            set_by_account_id=None,
        )
    before = {
        r.key: r.updated_at
        for r in await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent)
    }
    assert await count_plaintext_agent_files(db_session) == {tenant.id: 2}

    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),)
    assert await encrypt_plaintext_agent_files(db_session) == 2
    db_session.expire_all()

    assert await count_plaintext_agent_files(db_session) == {}
    stored = await _stored(db_session)
    for name in ("A", "B"):
        ciphertext, encoding = stored[name]
        assert encoding == "fernet_v1"
        assert Fernet(key).decrypt(ciphertext.encode()).decode() == f"value-{name}"
    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent)
    assert {r.key: r.content for r in rows} == {"A": "value-A", "B": "value-B"}
    assert {r.key: r.updated_at for r in rows} == before, (
        "encrypting in place must not move updated_at, which posted cards compare against"
    )
    assert await encrypt_plaintext_agent_files(db_session) == 0, "idempotent"
