"""A data source or shared connection credential form: single use, the value handed only to
the data port or sealed onto the connection."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from cryptography.fernet import Fernet
from daimon.core import ports
from daimon.core.config import Settings
from daimon.core.credential_requests import mint_request_token
from daimon.core.credential_submit import apply_data_source_submit
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ports import data_credentials
from daimon.core.ports.data_credentials import SourceCredentialRefused
from daimon.core.stores import credential_requests as store
from daimon.core.stores import mcp_connections as connections
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_the_form_hands_its_value_to_the_source_once_and_records_how_it_ended(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    source = uuid.uuid4()
    written: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]] = []
    fail = False

    async def write(_s: Settings, t: uuid.UUID, s: uuid.UUID, by: uuid.UUID, value: str) -> None:
        if fail:
            raise SourceCredentialRefused("That source is retired. Nothing was saved.")
        written.append((t, s, by, value))

    ports.load()  # the installed data package registers first; this test's port replaces it
    monkeypatch.setattr(data_credentials, "_REGISTERED", [write])

    async def form() -> CredentialRequestRow:
        return await store.create_credential_request(
            db_session, token=mint_request_token(), kind="data_source", tenant_id=tenant.id,
            agent_id=uuid.uuid4(), account_id=account.id, target="Orders",
            mcp_server_url=str(source), requester_platform_user_id="U1", channel_id="C1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30), idempotency_key=uuid.uuid4(),
            target_ma_agent_id="agent_1", target_name="analyst", requested_work=None,
        )  # fmt: skip

    row = await form()
    settings, value, now = cast(Settings, None), "postgresql://r@db/app", datetime.now(UTC)

    async def submit(r: CredentialRequestRow) -> CredentialRequestRow | None:
        return await apply_data_source_submit(
            db_session_factory, settings, row=r, value=value, now=now
        )

    assert await submit(row)
    assert written == [(tenant.id, source, account.id, value)]
    assert await submit(row) is None, "a form is spent once"
    fail, refused = True, await form()
    with pytest.raises(SourceCredentialRefused, match="retired"):
        await submit(refused)
    outcomes = [
        found.outcome
        for r in (row, refused)
        if (found := await store.peek_credential_request(db_session, token=r.token))
    ]
    assert outcomes == ["applied", "write_failed"]


async def test_a_connection_form_seals_its_value_onto_the_connection_and_reads_its_tools(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    key = Fernet.generate_key().decode()
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://localhost/x"},
            "crypto": {"keys": [SecretStr(key)]},
        }
    )
    made = await connections.create_connection(
        db_session, tenant_id=tenant.id, name="crm", url="https://crm.example/mcp",
        encrypted_auth=None, scope=None,
    )  # fmt: skip
    read: list[uuid.UUID] = []

    async def refreshed(*_: object, connection_id: uuid.UUID, **__: object) -> None:
        read.append(connection_id)

    monkeypatch.setattr("daimon.core.shared_connection_admin.refresh", refreshed)
    row = await store.create_credential_request(
        db_session, token=mint_request_token(), kind="data_connection", tenant_id=tenant.id,
        agent_id=uuid.uuid4(), account_id=account.id, target="crm",
        mcp_server_url=str(made.id), requester_platform_user_id="U1", channel_id="C1",
        expires_at=datetime.now(UTC) + timedelta(minutes=30), idempotency_key=uuid.uuid4(),
        target_ma_agent_id="agent_1", target_name="analyst", requested_work=None,
    )  # fmt: skip
    now = datetime.now(UTC)
    assert await apply_data_source_submit(
        db_session_factory, settings, row=row, value="s3cret", now=now
    )
    stored = await connections.get_connection(
        db_session, tenant_id=tenant.id, connection_id=made.id
    )
    assert stored is not None
    fernet = build_multifernet((key,))
    assert stored.encrypted_auth is not None
    assert decrypt_token(fernet, stored.encrypted_auth) == "s3cret", "the token is stored encrypted"
    assert read == [made.id], "saving the token reads the server's tools with it"
    assert await apply_data_source_submit(
        db_session_factory, settings, row=row, value="again", now=now
    ) is None, "a form is spent once"  # fmt: skip
