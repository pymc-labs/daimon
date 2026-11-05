"""`POST /coding-keys/claim`: a code in, a bare key out, and nothing else tells a caller more."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.mcp.coding_key_claim import build_coding_key_claim_route, client_address
from daimon.core._models import CodingKeyClaim, McpToken
from daimon.core.agent_editing import AgentEditDeps
from daimon.core.coding_key_connect import hash_code
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import token_jti
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.stores import agent_owners
from daimon.core.stores import coding_key_claims as claims
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

SECRET = "s" * 40
_SECRET = SecretStr(SECRET)
_ROUTER = MARouter()


async def _batch(
    sessions: async_sessionmaker[AsyncSession], *, owned: bool = True, ttl: timedelta
) -> dict[str, str]:
    """One batch for an agent, with codes `code-<client>`; returns client -> code."""
    now = datetime.now(UTC)
    salt = uuid.uuid4().hex
    ma_agent_id = f"ag_{salt}"
    codes = {client: f"code-{client}-{salt}" for client in ("codex", "cursor")}
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
        if owned:
            await agent_owners.record_creator(
                session, tenant_id=tenant.id, agent_id=agent_id, account_id=account.id
            )
        await claims.insert_claims(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            agent_id=agent_id,
            ma_agent_id=ma_agent_id,
            agent_name="helper",
            batch=uuid.uuid4(),
            created_at=now,
            expires_at=now + ttl,
            code_hashes={client: hash_code(code) for client, code in codes.items()},
        )
    body = ma_agent(id=ma_agent_id, name="helper", tenant_id=tenant.id).model_dump(mode="json")
    _ROUTER.add("GET", rf"/v1/agents/{ma_agent_id}", lambda _r, _m: httpx.Response(200, json=body))
    return codes


def _client(
    sessions: async_sessionmaker[AsyncSession],
    *,
    secret: SecretStr | None = _SECRET,
    limit: int = 100,
    trusted_hops: int = 1,
) -> httpx.AsyncClient:
    route = build_coding_key_claim_route(
        deps=AgentEditDeps(
            client=build_fake_anthropic(_ROUTER.dispatch),
            session_factory=sessions,
            public_url="https://daimon.example.com/mcp",
        ),
        jwt_secret=secret,
        rate_limiter=RateLimiter(max_requests=limit, window=timedelta(minutes=10)),
        trusted_hops=trusted_hops,
    )
    app = Starlette(routes=[Route("/coding-keys/claim", route, methods=["POST"])])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_a_code_returns_the_bare_key_once(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    codes = await _batch(committing_sessionmaker, ttl=timedelta(minutes=10))
    async with _client(committing_sessionmaker) as client:
        first = await client.post("/coding-keys/claim", data={"code": codes["codex"]})
        again = await client.post("/coding-keys/claim", data={"code": codes["codex"]})
        sibling = await client.post("/coding-keys/claim", data={"code": codes["cursor"]})

    assert first.status_code == 200, "the code trades"
    assert first.headers["content-type"].startswith("text/plain"), "the key is plain text"
    assert first.headers["cache-control"] == "no-store", "the key is never cached"
    assert first.text.count(".") == 2, "the body is the bare token, nothing around it"
    async with committing_sessionmaker() as session:
        stored = await session.get(McpToken, str(token_jti(first.text)))
    assert stored is not None, "the key is on record"
    assert (stored.purpose, stored.client) == ("coding", "codex"), "a coding key for the tool"
    assert (again.status_code, again.content) == (404, b""), "a code works once"
    assert (sibling.status_code, sibling.content) == (404, b""), "its siblings died with it"


async def test_every_failure_is_the_same_empty_404(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    expired = await _batch(committing_sessionmaker, ttl=timedelta(minutes=-1))
    ownerless = await _batch(committing_sessionmaker, owned=False, ttl=timedelta(minutes=10))
    async with _client(committing_sessionmaker) as client:
        answers = [
            await client.post("/coding-keys/claim", data={"code": "unknown"}),
            await client.post("/coding-keys/claim", data={"code": expired["codex"]}),
            await client.post("/coding-keys/claim", data={"code": ownerless["codex"]}),
            await client.post("/coding-keys/claim", data={}),
            await client.post("/coding-keys/claim", data={"code": ""}),
            await client.post("/coding-keys/claim", content=b"x" * 5000),
        ]

    assert {(a.status_code, a.content) for a in answers} == {(404, b"")}, "all empty 404s"
    assert len({tuple(sorted(a.headers.items())) for a in answers}) == 1, "headers match too"


async def test_the_route_refuses_without_a_signing_secret(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    codes = await _batch(committing_sessionmaker, ttl=timedelta(minutes=10))
    async with _client(committing_sessionmaker, secret=None) as client:
        response = await client.post("/coding-keys/claim", data={"code": codes["codex"]})
    assert response.status_code == 404, "no secret, no key"


async def test_attempts_are_rate_limited_per_address(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    codes = await _batch(committing_sessionmaker, ttl=timedelta(minutes=10))
    async with _client(committing_sessionmaker, limit=2) as client:
        for _ in range(2):
            await client.post(
                "/coding-keys/claim",
                data={"code": "guess"},
                headers={"x-forwarded-for": "198.51.100.7"},
            )
        limited = await client.post(
            "/coding-keys/claim",
            data={"code": codes["codex"]},
            headers={"x-forwarded-for": "198.51.100.7"},
        )
        other = await client.post(
            "/coding-keys/claim",
            data={"code": codes["codex"]},
            headers={"x-forwarded-for": "10.0.0.1, 203.0.113.9"},
        )

    assert limited.status_code == 404, "a right code from a limited address is refused"
    assert other.status_code == 200, "another address has its own allowance"
    async with committing_sessionmaker() as session:
        used = (
            await session.scalars(select(CodingKeyClaim).where(CodingKeyClaim.jti.is_not(None)))
        ).all()
    assert len(used) == 1, "exactly the one claim from the unlimited address minted"


async def test_a_forged_leftmost_forwarded_address_buys_no_fresh_allowance(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    codes = await _batch(committing_sessionmaker, ttl=timedelta(minutes=10))
    async with _client(committing_sessionmaker, limit=2) as client:
        for n in range(2):
            await client.post(
                "/coding-keys/claim",
                data={"code": "guess"},
                headers={"x-forwarded-for": f"192.0.2.{n}, 198.51.100.7"},
            )
        forged = await client.post(
            "/coding-keys/claim",
            data={"code": codes["codex"]},
            headers={"x-forwarded-for": "192.0.2.99, 198.51.100.7"},
        )

    assert forged.status_code == 404, "the proxy-appended address is counted, not the forged one"


@pytest.mark.parametrize(
    ("header", "peer", "hops", "expected"),
    [
        ("1.1.1.1, 2.2.2.2, 3.3.3.3", "10.0.0.1", 1, "3.3.3.3"),
        ("1.1.1.1, 2.2.2.2, 3.3.3.3", "10.0.0.1", 2, "2.2.2.2"),
        ("9.9.9.9", "10.0.0.1", 1, "9.9.9.9"),
        ("9.9.9.9", "10.0.0.1", 2, "10.0.0.1"),
        ("9.9.9.9", "10.0.0.1", 0, "10.0.0.1"),
        ("", "10.0.0.1", 1, "10.0.0.1"),
        ("", None, 1, "unknown"),
        ("1.1.1.1,", "10.0.0.1", 1, "10.0.0.1"),
    ],
)
def test_the_client_address_is_the_hop_a_trusted_proxy_appended(
    header: str, peer: str | None, hops: int, expected: str
) -> None:
    got = client_address(header, peer, trusted_hops=hops)

    assert got == expected, f"{header!r} behind {hops} trusted hop(s) from {peer}"
