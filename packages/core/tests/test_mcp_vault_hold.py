"""A held turn's Daimon credential: minted bound to its channel, vaulted on its own.

Transport-level fakes (httpx.MockTransport), as in `test_mcp_vault`.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from anthropic import AsyncAnthropic
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.mcp_hold import (
    HOLD_CLAIM,
    hold_from_metadata,
    hold_key,
    hold_metadata_value,
    hold_vault_suffix,
    parse_hold_claim,
)
from daimon.core.mcp_vault import (
    HOLD_METADATA_KEY,
    chat_mcp_credential,
    ensure_agent_mcp_vault,
    refresh_mounted_mcp_vaults,
)
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SECRET = b"a" * 32
URL = "https://mcp.example.com/mcp"
NOW = dt.datetime(2026, 4, 24, tzinfo=dt.UTC)
HOLD = TurnHold(platform="slack", channel_id="C-SYN-1", client=True)
OTHER_HOLD = TurnHold(platform="slack", channel_id="C-SYN-2", client=True)


async def _account(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as db, db.begin():
        tenant = await make_tenant(db, platform="slack")
        account = await make_account(db, tenant=tenant)
    return account.id


def _client(handler: Any) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _vault(vault_id: str, name: str) -> dict[str, Any]:
    return {
        "id": vault_id,
        "type": "vault",
        "display_name": name,
        "metadata": None,
        "archived_at": None,
        "created_at": "2026-04-01T00:00:00Z",
    }


def _credential(vault_id: str, metadata: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "vcrd_1",
        "type": "vault_credential",
        "vault_id": vault_id,
        "metadata": metadata,
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
        "auth": {"type": "static_bearer", "mcp_server_url": URL},
    }


def test_hold_claim_round_trips_and_rejects_other_shapes() -> None:
    assert parse_hold_claim({"platform": "slack", "channel_id": "C1", "client": False}) == (
        TurnHold(platform="slack", channel_id="C1", client=False)
    )
    for bad in (
        None,
        "slack",
        {"platform": "slack", "channel_id": "C1"},
        {"platform": "slack", "channel_id": "C1", "client": "yes"},
        {"platform": "slack", "channel_id": "", "client": True},
        {"platform": "slack", "channel_id": "C1", "client": True, "extra": 1},
    ):
        with pytest.raises(ValueError):
            parse_hold_claim(bad)
    assert hold_from_metadata(hold_metadata_value(HOLD)) == HOLD
    assert hold_from_metadata(None) is None
    with pytest.raises(ValueError):
        hold_from_metadata("not json at all")


def test_hold_names_tell_holds_apart() -> None:
    member = TurnHold(platform="slack", channel_id="C-SYN-1", client=False)
    suffixes = {hold_vault_suffix(h) for h in (HOLD, OTHER_HOLD, member)}
    assert len(suffixes) == 3
    assert hold_key(None) is None
    assert hold_key(HOLD) != hold_key(member)


async def test_held_chat_token_records_channel_on_row_and_signs_claim(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    account = await make_account(db_session, tenant=tenant)
    token, jti, _ = await mint_chat_mcp_token(
        db_session,
        account_id=account.id,
        agent_id=uuid.uuid4(),
        secret=SECRET,
        now=NOW,
        hold=HOLD,
    )
    row = await get_mcp_token(db_session, jti=jti)
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})

    assert row is not None
    assert (row.platform, row.channel_id) == ("slack", "C-SYN-1")
    assert claims[HOLD_CLAIM] == {"platform": "slack", "channel_id": "C-SYN-1", "client": True}


async def test_unheld_chat_token_is_unchanged(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    account = await make_account(db_session, tenant=tenant)
    token, jti, _ = await mint_chat_mcp_token(
        db_session, account_id=account.id, agent_id=uuid.uuid4(), secret=SECRET, now=NOW
    )
    row = await get_mcp_token(db_session, jti=jti)
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})

    assert row is not None
    assert (row.platform, row.channel_id) == (None, None)
    assert HOLD_CLAIM not in claims


async def test_held_and_unheld_turns_get_different_vaults(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _account(db_session_factory)
    agent_id = uuid.uuid4()
    created: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            name = json.loads(req.content)["display_name"]
            created.append(name)
            return httpx.Response(200, json=_vault(f"vlt_{len(created)}", name))
        if req.method == "POST" and req.url.path.endswith("/credentials"):
            return httpx.Response(200, json=_credential("vlt", {}))
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _client(handler)
    for hold in (None, HOLD, OTHER_HOLD):
        await ensure_agent_mcp_vault(
            client,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=SECRET,
            public_url=URL,
            now=NOW,
            session_factory=db_session_factory,
            hold=hold,
        )

    ordinary = f"daimon-mcp:{account_id}:{agent_id}"
    assert created[0] == ordinary
    assert created[1] == ordinary + hold_vault_suffix(HOLD)
    assert created[2] == ordinary + hold_vault_suffix(OTHER_HOLD)
    assert len(set(created)) == 3


async def _warm_held_metadata(
    factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    hold: TurnHold | None,
) -> dict[str, str]:
    async with factory() as db, db.begin():
        payload = await chat_mcp_credential(
            db,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=SECRET,
            public_url=URL,
            now=NOW,
            hold=hold,
        )
    return payload["metadata"]


def _warm_handler(name: str, metadata: dict[str, str], posts: list[dict[str, Any]]) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [_vault("vlt_w", name)], "has_more": False})
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_w/credentials":
            return httpx.Response(
                200, json={"data": [_credential("vlt_w", metadata)], "has_more": False}
            )
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_w/credentials/vcrd_1":
            posts.append(json.loads(req.content))
            return httpx.Response(200, json=_credential("vlt_w", {}))
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    return handler


async def test_warm_held_vault_with_same_hold_is_not_reminted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    metadata = await _warm_held_metadata(db_session_factory, account_id, agent_id, HOLD)
    posts: list[dict[str, Any]] = []
    name = f"daimon-mcp:{account_id}:{agent_id}{hold_vault_suffix(HOLD)}"

    await ensure_agent_mcp_vault(
        _client(_warm_handler(name, metadata, posts)),
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=SECRET,
        public_url=URL,
        now=NOW,
        session_factory=db_session_factory,
        hold=HOLD,
    )

    assert posts == []


async def test_warm_vault_whose_hold_differs_is_reminted_with_the_new_hold(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    # A vault that carries OTHER_HOLD sits under HOLD's name (a name collision).
    metadata = await _warm_held_metadata(db_session_factory, account_id, agent_id, OTHER_HOLD)
    posts: list[dict[str, Any]] = []
    name = f"daimon-mcp:{account_id}:{agent_id}{hold_vault_suffix(HOLD)}"

    await ensure_agent_mcp_vault(
        _client(_warm_handler(name, metadata, posts)),
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=SECRET,
        public_url=URL,
        now=NOW,
        session_factory=db_session_factory,
        hold=HOLD,
    )

    assert len(posts) == 1
    assert posts[0]["metadata"][HOLD_METADATA_KEY] == hold_metadata_value(HOLD)
    claims = pyjwt.decode(
        posts[0]["auth"]["token"], SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert claims[HOLD_CLAIM]["channel_id"] == "C-SYN-1"


async def test_unheld_vault_holding_a_hold_is_reminted_unheld(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    metadata = await _warm_held_metadata(db_session_factory, account_id, agent_id, HOLD)
    posts: list[dict[str, Any]] = []

    await ensure_agent_mcp_vault(
        _client(_warm_handler(f"daimon-mcp:{account_id}:{agent_id}", metadata, posts)),
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=SECRET,
        public_url=URL,
        now=NOW,
        session_factory=db_session_factory,
    )

    assert len(posts) == 1
    assert HOLD_METADATA_KEY not in posts[0]["metadata"]


async def test_refreshing_a_mounted_held_vault_keeps_its_hold(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    metadata = await _warm_held_metadata(db_session_factory, account_id, agent_id, HOLD)
    # Stale: expires inside the refresh margin, so the refresh must re-mint.
    later = NOW + dt.timedelta(minutes=55)
    posts: list[dict[str, Any]] = []

    await refresh_mounted_mcp_vaults(
        _client(_warm_handler("unused", metadata, posts)),
        vault_ids=["vlt_w"],
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=SECRET,
        public_url=URL,
        now=later,
        session_factory=db_session_factory,
        scope=Scope.legacy_host_authorized(call_site="test_mcp_vault_hold"),
    )

    assert len(posts) == 1
    assert posts[0]["metadata"][HOLD_METADATA_KEY] == hold_metadata_value(HOLD)
    claims = pyjwt.decode(
        posts[0]["auth"]["token"], SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert claims[HOLD_CLAIM] == {"platform": "slack", "channel_id": "C-SYN-1", "client": True}
