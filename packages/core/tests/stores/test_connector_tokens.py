"""Real-Postgres tests for the connector_tokens store: round trip, tenancy, claims."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores import connector_tokens as store
from daimon.core.stores.domain import ConnectorRefreshOwner, ConnectorTokenRow
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_URL = "https://mcp.example.com/mcp"
_AGENT = uuid.UUID("00000000-0000-0000-0000-0000000000a7")


async def _put(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    *,
    access: bytes = b"at",
    now: datetime = _NOW,
    owner: ConnectorRefreshOwner = "daimon",
) -> ConnectorTokenRow:
    return await store.upsert_connector_token(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=_AGENT,
        server_url=_URL,
        access_token_encrypted=access,
        refresh_token_encrypted=b"rt",
        token_endpoint="https://auth.example.com/token",
        client_id="client",
        client_secret_encrypted=None,
        token_endpoint_auth_method="none",
        scope="read",
        resource=_URL,
        expires_at=now + timedelta(hours=1),
        refresh_owner=owner,
        now=now,
    )


async def test_personal_and_agent_wide_rows_are_separate_and_per_tenant(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    mine = await _put(db_session, tenant.id, account.id, access=b"mine")
    shared = await _put(db_session, tenant.id, None, access=b"shared")
    again = await _put(db_session, tenant.id, account.id, access=b"new", now=_NOW + timedelta(1))
    assert again.id == mine.id, "a reconnect must replace the personal row, not add one"
    assert again.access_token_encrypted == b"new", "a reconnect must store the new ciphertext"
    wide = await store.get_connector_token(
        db_session, tenant_id=tenant.id, account_id=None, agent_id=_AGENT, server_url=_URL
    )
    assert wide is not None and wide.id == shared.id, "the agent-wide row reads by account None"
    assert wide.access_token_encrypted == b"shared", "agent-wide and personal rows never mix"
    foreign = await store.get_connector_token(
        db_session, tenant_id=other.id, account_id=account.id, agent_id=_AGENT, server_url=_URL
    )
    assert foreign is None, "another tenant must not read this tenant's grant"
    by_id = await store.get_connector_token_by_id(db_session, tenant_id=other.id, token_id=mine.id)
    assert by_id is None, "a row id from another tenant must not resolve"


async def test_token_urls_list_only_daimon_owned_personal_rows(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _put(db_session, tenant.id, account.id)
    urls = await store.list_connector_token_urls(
        db_session,
        tenant_id=tenant.id,
        agent_id=_AGENT,
        account_id=account.id,
    )
    assert urls == frozenset({_URL}), "the URL the person holds a token for is listed"
    await _put(db_session, tenant.id, account.id, owner="ma", now=_NOW + timedelta(1))
    urls = await store.list_connector_token_urls(
        db_session, tenant_id=tenant.id, agent_id=_AGENT, account_id=account.id
    )
    assert urls == frozenset(), "an MA-owned row is not a Daimon token"


async def test_one_claim_wins_and_the_write_releases_it(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await _put(db_session, tenant.id, None)
    later = _NOW + timedelta(minutes=1)
    stale = later - timedelta(minutes=2)
    claim = await store.claim_refresh(
        db_session,
        tenant_id=tenant.id,
        token_id=row.id,
        read_at=row.updated_at,
        stale_before=stale,
        now=later,
    )
    assert claim is not None, "the first refresher must win the claim"
    second = await store.claim_refresh(
        db_session,
        tenant_id=tenant.id,
        token_id=row.id,
        read_at=claim.updated_at,
        stale_before=stale,
        now=later + timedelta(seconds=1),
    )
    assert second is None, "a live claim must refuse a second refresher"
    written = await store.update_refreshed_token(
        db_session,
        tenant_id=tenant.id,
        token_id=row.id,
        read_at=claim.updated_at,
        access_token_encrypted=b"at2",
        refresh_token_encrypted=b"rt2",
        scope="read",
        expires_at=None,
        now=later + timedelta(seconds=2),
    )
    assert written is not None and written.refresh_started_at is None, "the write ends the claim"
    stale_write = await store.update_refreshed_token(
        db_session,
        tenant_id=tenant.id,
        token_id=row.id,
        read_at=claim.updated_at,
        access_token_encrypted=b"at3",
        refresh_token_encrypted=b"rt3",
        scope="read",
        expires_at=None,
        now=later + timedelta(seconds=3),
    )
    assert stale_write is None, "a write over a changed row must be dropped"


async def test_a_dead_claim_can_be_taken_over_and_ma_rows_never_claim(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = await _put(db_session, tenant.id, None)
    first = await store.claim_refresh(
        db_session,
        tenant_id=tenant.id,
        token_id=row.id,
        read_at=row.updated_at,
        stale_before=_NOW,
        now=_NOW + timedelta(seconds=1),
    )
    assert first is not None, "the first claim must win"
    taken = await store.claim_refresh(
        db_session,
        tenant_id=tenant.id,
        token_id=row.id,
        read_at=first.updated_at,
        stale_before=_NOW + timedelta(minutes=5),
        now=_NOW + timedelta(minutes=5),
    )
    assert taken is not None, "a claim older than the lease must be taken over"
    ma = await _put(db_session, tenant.id, None, owner="ma", now=_NOW + timedelta(minutes=6))
    refused = await store.claim_refresh(
        db_session,
        tenant_id=tenant.id,
        token_id=ma.id,
        read_at=ma.updated_at,
        stale_before=_NOW + timedelta(minutes=6),
        now=_NOW + timedelta(minutes=7),
    )
    assert refused is None, "an MA-owned grant is never refreshed by Daimon"


async def test_account_purge_counts_and_deletes_only_personal_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _put(db_session, tenant.id, account.id)
    await _put(db_session, tenant.id, None)
    count = await store.count_connector_tokens_for_account(db_session, account_id=account.id)
    assert count == 1, "only the personal row belongs to the account"
    deleted = await store.delete_connector_tokens_for_account(db_session, account_id=account.id)
    assert deleted == 1, "the purge must delete exactly the counted rows"
    wide = await store.get_connector_token(
        db_session, tenant_id=tenant.id, account_id=None, agent_id=_AGENT, server_url=_URL
    )
    assert wide is not None, "the agent-wide grant must survive a personal purge"
