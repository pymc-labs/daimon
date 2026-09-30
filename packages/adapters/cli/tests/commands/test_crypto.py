"""`daimon crypto verify` / `encrypt-plaintext`: find and fix plaintext agent keys."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from cryptography.fernet import Fernet
from daimon.adapters.cli.commands import crypto
from daimon.adapters.cli.main import app
from daimon.core.stores.agent_files import put_agent_file
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner


async def test_verify_fails_on_plaintext_then_passes_after_encrypt(
    db_nullpool_engine, db_clean, monkeypatch
):
    key = Fernet.generate_key().decode()
    plain = async_sessionmaker(
        db_nullpool_engine,
        expire_on_commit=False,
        info={"crypto_keys": (), "crypto_allow_plaintext": True},
    )
    keyed = async_sessionmaker(
        db_nullpool_engine, expire_on_commit=False, info={"crypto_keys": (key,)}
    )
    tenant_id = uuid.uuid4()
    async with plain() as session, session.begin():
        await make_tenant(session, id=tenant_id)
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            key="CRM_TOKEN",
            content="legacy-secret",
            set_by_account_id=None,
        )

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=keyed)

    monkeypatch.setattr(crypto, "build_runtime", runtime)
    monkeypatch.setattr(
        crypto,
        "load_settings",
        lambda: SimpleNamespace(crypto=SimpleNamespace(keys=(SecretStr(key),))),
    )

    def invoke(*args: str):
        return CliRunner().invoke(app, ["crypto", *args])

    before = await asyncio.to_thread(invoke, "verify")
    assert before.exit_code == 1
    assert "1 agent key(s) stored in plaintext" in before.output
    assert "legacy-secret" not in before.output and "CRM_TOKEN" not in before.output

    encrypted = await asyncio.to_thread(invoke, "encrypt-plaintext")
    assert encrypted.exit_code == 0, encrypted.output
    assert "Encrypted 1" in encrypted.output
    async with keyed() as session:
        encoding = (await session.execute(text("SELECT encoding FROM agent_files"))).scalar_one()
    assert encoding == "fernet_v1"

    after = await asyncio.to_thread(invoke, "verify")
    assert after.exit_code == 0, after.output


def test_verify_fails_without_keys(monkeypatch):
    @asynccontextmanager
    async def runtime(_settings):
        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        async def _count(_session):
            return {}

        monkeypatch.setattr(crypto, "count_plaintext_agent_files", _count)
        yield SimpleNamespace(sessionmaker=_Session)

    monkeypatch.setattr(crypto, "build_runtime", runtime)
    monkeypatch.setattr(
        crypto, "load_settings", lambda: SimpleNamespace(crypto=SimpleNamespace(keys=()))
    )
    result = CliRunner().invoke(app, ["crypto", "verify"])
    assert result.exit_code == 1
    assert "DAIMON_CRYPTO__KEYS is not set" in result.output
