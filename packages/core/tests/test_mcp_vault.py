"""Tests for daimon.core.mcp_vault.ensure_agent_mcp_vault.

Uses httpx.MockTransport wired into AsyncAnthropic rather than AsyncMock on
client.beta.vaults — per guideline:testing, transport-level fakes validate
SDK signature drift; method-level mocks silently accept wrong kwargs.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from anthropic import AsyncAnthropic
from daimon.core.mcp_vault import add_external_mcp_credential, ensure_agent_mcp_vault

pytestmark = pytest.mark.asyncio


def _make_client(handler: httpx.MockTransport) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=handler),
    )


def _vault_obj(vault_id: str, display_name: str, created_at: str) -> dict[str, Any]:
    return {
        "id": vault_id,
        "type": "vault",
        "display_name": display_name,
        "metadata": None,
        "archived_at": None,
        "created_at": created_at,
    }


async def test_ensure_agent_mcp_vault_returns_existing_oldest_when_present() -> None:
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _vault_obj("vlt_old", display, "2026-04-01T00:00:00Z"),
                        _vault_obj("vlt_new", display, "2026-04-23T00:00:00Z"),
                        _vault_obj("vlt_other", "unrelated", "2026-04-01T00:00:00Z"),
                    ],
                    "has_more": False,
                    "first_id": "vlt_old",
                    "last_id": "vlt_other",
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_old/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_existing",
                            "type": "credential",
                            "vault_id": "vlt_old",
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": "https://mcp.example.com/mcp",
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    vault_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert vault_id == "vlt_old", "must pick oldest matching display_name"
    # Warm path verifies the credential URL matches; no rebind needed when it does.
    assert calls == ["GET /v1/vaults", "GET /v1/vaults/vlt_old/credentials"], (
        "must list creds to verify URL match, but not create/delete when matching"
    )


async def test_ensure_agent_mcp_vault_cold_path_creates_vault_and_credential() -> None:
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            body = json.loads(req.content)
            created_bodies.append(body)
            assert body == {"display_name": display}
            return httpx.Response(
                200,
                json=_vault_obj("vlt_new", display, "2026-04-24T00:00:00Z"),
            )
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_new/credentials":
            body = json.loads(req.content)
            created_bodies.append(body)
            assert body["auth"]["type"] == "static_bearer"
            assert body["auth"]["mcp_server_url"] == "https://mcp.example.com/mcp"
            assert isinstance(body["auth"]["token"], str)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_new",
                    "type": "credential",
                    "vault_id": "vlt_new",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://mcp.example.com/mcp",
                    },
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    vault_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert vault_id == "vlt_new"
    assert len(created_bodies) == 2, "must POST vault, then credential"


def _cold_path_handler(
    display_name: str,
    captured: list[dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Cold-path handler: no vaults exist; record credential POST bodies."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json=_vault_obj("vlt_new", display_name, "2026-04-24T00:00:00Z"),
            )
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_new/credentials":
            body = json.loads(req.content)
            captured.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_new",
                    "type": "vault_credential",
                    "vault_id": "vlt_new",
                    "metadata": {},
                    "created_at": "2026-04-24T00:00:00Z",
                    "updated_at": "2026-04-24T00:00:00Z",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://mcp.example.com/mcp",
                    },
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    return handler


async def test_ensure_agent_mcp_vault_cold_path_mints_claimless_jwt() -> None:
    """Cold-path credential carries no platform/guild_id wire claims (account-scoped only)."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    captured: list[dict[str, Any]] = []
    secret = b"a" * 32

    client = _make_client(httpx.MockTransport(_cold_path_handler(display, captured)))
    await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=secret,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert len(captured) == 1, "exactly one credential POST on cold path"
    token = captured[0]["auth"]["token"]
    # Inspect-only: signature verification is the MCP verifier's job; here we assert claim shape.
    claims = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "platform" not in claims, "minted token must carry no platform wire claim"
    assert "guild_id" not in claims, "minted token must carry no guild_id wire claim"


def _credential_obj(
    *,
    credential_id: str,
    vault_id: str,
    mcp_server_url: str,
) -> dict[str, Any]:
    """Validated construction via the SDK model — serialized for the wire."""
    from anthropic.types.beta.vaults import (
        BetaManagedAgentsCredential,
        BetaManagedAgentsStaticBearerAuthResponse,
    )

    return BetaManagedAgentsCredential(
        id=credential_id,
        type="vault_credential",
        vault_id=vault_id,
        metadata={},
        created_at=dt.datetime(2026, 4, 1, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 4, 1, tzinfo=dt.UTC),
        auth=BetaManagedAgentsStaticBearerAuthResponse(
            type="static_bearer",
            mcp_server_url=mcp_server_url,
        ),
    ).model_dump(mode="json")


async def test_ensure_agent_mcp_vault_warm_path_url_drift_creates_at_new_url_without_sweeping() -> (
    None
):
    """URL drift: a fresh credential is POSTed at the current ``public_url`` and the prior
    daimon-mcp credential is left as an inert orphan. We do NOT delete on URL drift because
    the vault is shared with user-added external MCP credentials whose URLs we cannot
    authenticate as "ours" — sweeping would silently nuke user data on the first deploy-URL
    change."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    public_url = "https://mcp.example.com/mcp"
    stale_url = "https://old-tunnel.example.com/mcp"

    call_log: list[tuple[str, str]] = []
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        call_log.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [_vault_obj("vlt_warm", display, "2026-04-01T00:00:00Z")],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_stale",
                            "type": "credential",
                            "vault_id": "vlt_warm",
                            "auth": {"type": "static_bearer", "mcp_server_url": stale_url},
                        }
                    ],
                    "has_more": False,
                },
            )
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            body = json.loads(req.content)
            created_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_fresh",
                    "type": "credential",
                    "vault_id": "vlt_warm",
                    "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    vault_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url=public_url,
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert vault_id == "vlt_warm"
    deletes = [c for c in call_log if c[0] == "DELETE"]
    assert deletes == [], (
        f"URL drift must NOT delete the prior credential (user-MCP data-loss guard); got {deletes}"
    )
    assert ("POST", "/v1/vaults/vlt_warm/credentials") in call_log, (
        "must POST a fresh credential with the current URL"
    )
    assert len(created_bodies) == 1, "exactly one credential POST"
    assert created_bodies[0]["auth"]["mcp_server_url"] == public_url, (
        "fresh credential targets the current public_url"
    )
    assert created_bodies[0]["auth"]["type"] == "static_bearer", (
        "fresh credential is a static_bearer"
    )


async def test_ensure_agent_mcp_vault_warm_path_with_matching_url_skips_rebind() -> None:
    """When the existing credential already matches public_url, no rebind happens —
    list creds to verify match, then return without mutation."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    public_url = "https://mcp.example.com/mcp"

    call_log: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        call_log.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [_vault_obj("vlt_warm", display, "2026-04-01T00:00:00Z")],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_ok",
                            "type": "credential",
                            "vault_id": "vlt_warm",
                            "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                        }
                    ],
                    "has_more": False,
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    vault_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url=public_url,
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert vault_id == "vlt_warm"
    mutating = [c for c in call_log if c[0] in ("POST", "DELETE")]
    assert mutating == [], f"matching URL must not mutate credentials; got {mutating}"


async def test_ensure_agent_mcp_vault_rebind_preserves_external_mcp_credentials() -> None:
    """URL-drift: no credential is deleted when public_url changes across deploys.

    The per-agent vault holds the daimon-mcp credential AND any external
    MCP credentials added separately (GitHub Copilot via
    add_github_copilot_credential, plus user-added MCPs via
    add_external_mcp_credential). When daimon's public_url changes across
    deploys, NO credentials are deleted — we just create a fresh credential
    at the new URL. The prior daimon-mcp credential is left as an inert orphan;
    user-added external creds are left strictly alone.

    Latent footgun this guards against: first URL drift after a user adds
    any external MCP would silently nuke their creds. The fix is to not
    sweep at all on URL drift (we cannot authenticate "ours" vs theirs).
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    public_url = "https://mcp.example.com/mcp"
    stale_daimon_url = "https://old-tunnel.example.com/mcp"
    github_copilot_url = "https://api.githubcopilot.com/mcp"

    deleted_cred_ids: list[str] = []
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [_vault_obj("vlt_warm", display, "2026-04-01T00:00:00Z")],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_stale_daimon",
                            "type": "credential",
                            "vault_id": "vlt_warm",
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": stale_daimon_url,
                            },
                        },
                        {
                            "id": "vcrd_github_copilot",
                            "type": "credential",
                            "vault_id": "vlt_warm",
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": github_copilot_url,
                            },
                        },
                    ],
                    "has_more": False,
                },
            )
        if req.method == "DELETE" and req.url.path.startswith("/v1/vaults/vlt_warm/credentials/"):
            cred_id = req.url.path.rsplit("/", 1)[-1]
            deleted_cred_ids.append(cred_id)
            return httpx.Response(200, json={"id": cred_id, "deleted": True})
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            body = json.loads(req.content)
            created_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_fresh",
                    "type": "credential",
                    "vault_id": "vlt_warm",
                    "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url=public_url,
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert deleted_cred_ids == [], (
        f"URL drift must NOT delete any credential (user-MCP data-loss guard); "
        f"the stale daimon-mcp cred is left as an inert orphan and external "
        f"MCP creds are untouched. got deletions: {deleted_cred_ids}"
    )
    assert len(created_bodies) == 1, "exactly one credential POST at the new URL"
    assert created_bodies[0]["auth"]["mcp_server_url"] == public_url, (
        "fresh credential targets the current public_url"
    )


async def test_ensure_agent_mcp_vault_oauth_callback_path_mints_claimless_jwt() -> None:
    """oauth_github.py call path — must mint a claim-less JWT (account-scoped only)."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    captured: list[dict[str, Any]] = []
    secret = b"a" * 32

    client = _make_client(httpx.MockTransport(_cold_path_handler(display, captured)))
    await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=secret,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert len(captured) == 1
    token = captured[0]["auth"]["token"]
    # Inspect-only: signature verification is the MCP verifier's job; here we assert claim shape.
    claims = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "platform" not in claims, "oauth callback path stays claim-less"
    assert "guild_id" not in claims, "oauth callback path stays claim-less"


# ----- add_external_mcp_credential -----


def _stateful_vault_handler(
    *,
    initial_vaults: list[dict[str, Any]],
    per_vault_creds: dict[str, list[dict[str, Any]]],
    created_bodies: list[dict[str, Any]],
    deleted_ids: list[str],
    vault_create_bodies: list[dict[str, Any]] | None = None,
    next_vault_id: str = "vlt_new",
) -> Callable[[httpx.Request], httpx.Response]:
    """Stateful handler modeling per-vault credential lists."""
    vaults = list(initial_vaults)
    creds = dict(per_vault_creds)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": vaults, "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            body = json.loads(req.content)
            if vault_create_bodies is not None:
                vault_create_bodies.append(body)
            new_vault = _vault_obj(next_vault_id, body["display_name"], "2026-04-24T00:00:00Z")
            vaults.append(new_vault)
            creds.setdefault(next_vault_id, [])
            return httpx.Response(200, json=new_vault)
        # credentials endpoints
        for vid in list(creds.keys()) + ([next_vault_id] if next_vault_id not in creds else []):
            if req.method == "GET" and req.url.path == f"/v1/vaults/{vid}/credentials":
                return httpx.Response(200, json={"data": creds.get(vid, []), "has_more": False})
            if req.method == "POST" and req.url.path == f"/v1/vaults/{vid}/credentials":
                body = json.loads(req.content)
                created_bodies.append(body)
                cred: dict[str, Any] = {
                    "id": f"vcrd_{vid}_{len(creds.get(vid, []))}",
                    "type": "vault_credential",
                    "vault_id": vid,
                    "metadata": {},
                    "created_at": "2026-04-24T00:00:00Z",
                    "updated_at": "2026-04-24T00:00:00Z",
                    "auth": body["auth"],
                }
                creds.setdefault(vid, []).append(cred)
                return httpx.Response(200, json=cred)
            if req.method == "DELETE" and req.url.path.startswith(f"/v1/vaults/{vid}/credentials/"):
                cred_id = req.url.path.rsplit("/", 1)[-1]
                deleted_ids.append(cred_id)
                creds[vid] = [c for c in creds.get(vid, []) if c.get("id") != cred_id]
                return httpx.Response(200, json={"id": cred_id, "deleted": True})
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    return handler


async def test_add_external_mcp_credential_creates_when_no_prior_credential() -> None:
    """Vault exists for agent; no prior credential at target URL.
    Lists vault, lists creds, POSTs new credential. No DELETEs."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    target_url = "https://ga4.example.com/mcp"
    target_token = "secret_token_abcd"

    created_bodies: list[dict[str, Any]] = []
    deleted_ids: list[str] = []

    handler = _stateful_vault_handler(
        initial_vaults=[_vault_obj("vlt_acct", display, "2026-04-01T00:00:00Z")],
        per_vault_creds={
            "vlt_acct": [
                {
                    "id": "vcrd_daimon",
                    "type": "credential",
                    "vault_id": "vlt_acct",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://mcp.example.com/mcp",
                    },
                }
            ]
        },
        created_bodies=created_bodies,
        deleted_ids=deleted_ids,
    )

    client = _make_client(httpx.MockTransport(handler))
    await add_external_mcp_credential(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"x" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        mcp_server_url=target_url,
        token=target_token,
    )

    assert deleted_ids == [], (
        f"no DELETE expected when no prior credential at URL; got {deleted_ids}"
    )
    assert len(created_bodies) == 1, "exactly one credential POST"
    assert created_bodies[0]["auth"] == {
        "type": "static_bearer",
        "mcp_server_url": target_url,
        "token": target_token,
    }, "credential body must carry the supplied URL and token"


async def test_add_external_mcp_credential_replaces_existing_credential_at_same_url() -> None:
    """Replace path: an existing static_bearer credential at the same URL is DELETEd,
    then a new one POSTed. Credentials at OTHER URLs are not touched."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    target_url = "https://ga4.example.com/mcp"
    other_url = "https://other.example.com/mcp"

    deleted_ids: list[str] = []
    created_bodies: list[dict[str, Any]] = []

    handler = _stateful_vault_handler(
        initial_vaults=[_vault_obj("vlt_acct", display, "2026-04-01T00:00:00Z")],
        per_vault_creds={
            "vlt_acct": [
                {
                    "id": "vcrd_prior",
                    "type": "credential",
                    "vault_id": "vlt_acct",
                    "auth": {"type": "static_bearer", "mcp_server_url": target_url},
                },
                {
                    "id": "vcrd_other",
                    "type": "credential",
                    "vault_id": "vlt_acct",
                    "auth": {"type": "static_bearer", "mcp_server_url": other_url},
                },
            ]
        },
        created_bodies=created_bodies,
        deleted_ids=deleted_ids,
    )

    client = _make_client(httpx.MockTransport(handler))
    await add_external_mcp_credential(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"x" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        mcp_server_url=target_url,
        token="fresh_token",
    )

    assert deleted_ids == ["vcrd_prior"], (
        f"only the prior credential at the target URL must be deleted; got {deleted_ids}"
    )
    assert len(created_bodies) == 1
    assert created_bodies[0]["auth"]["mcp_server_url"] == target_url
    assert created_bodies[0]["auth"]["token"] == "fresh_token"


async def test_add_external_mcp_credential_bootstraps_vault_when_missing() -> None:
    """If no vault exists matching daimon-mcp:{account_id}:{agent_id}, the helper
    bootstraps the per-agent vault (creates it + daimon-mcp JWT) then writes the cred.
    No DaimonError is raised."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    target_url = "https://ext.example.com/mcp"
    target_token = "tok_bootstrap"

    vault_create_bodies: list[dict[str, Any]] = []
    created_bodies: list[dict[str, Any]] = []
    deleted_ids: list[str] = []

    handler = _stateful_vault_handler(
        initial_vaults=[],  # no vaults exist initially
        per_vault_creds={},
        created_bodies=created_bodies,
        deleted_ids=deleted_ids,
        vault_create_bodies=vault_create_bodies,
        next_vault_id="vlt_new",
    )

    client = _make_client(httpx.MockTransport(handler))
    await add_external_mcp_credential(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"s" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        mcp_server_url=target_url,
        token=target_token,
    )

    assert len(vault_create_bodies) == 1, (
        "bootstrap must POST to create the vault when it doesn't exist"
    )
    assert vault_create_bodies[0]["display_name"] == display, (
        "bootstrapped vault must use the per-agent display name"
    )
    # Two credential POSTs: one for daimon-mcp JWT (bootstrap), one for the external cred.
    assert len(created_bodies) == 2, "bootstrap creates daimon-mcp JWT cred, then the external cred"
    # The last credential is the external one.
    assert created_bodies[-1]["auth"]["mcp_server_url"] == target_url, (
        "last cred POST must be the external MCP credential"
    )
    assert created_bodies[-1]["auth"]["token"] == target_token, (
        "external credential must carry the supplied token"
    )


# ----- NEW: SC-1, SC-3c, SC-4 regression tests -----


async def test_ensure_agent_mcp_vault_cold_path_creates_per_agent_vault() -> None:
    """SC-1: cold path creates vault named daimon-mcp:{account_id}:{agent_id}."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    expected_display = f"daimon-mcp:{account_id}:{agent_id}"
    created_vault_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            body = json.loads(req.content)
            created_vault_bodies.append(body)
            return httpx.Response(
                200, json=_vault_obj("vlt_1", body["display_name"], "2026-04-24T00:00:00Z")
            )
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_1/credentials":
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_1",
                    "type": "vault_credential",
                    "vault_id": "vlt_1",
                    "metadata": {},
                    "created_at": "2026-04-24T00:00:00Z",
                    "updated_at": "2026-04-24T00:00:00Z",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://mcp.example.com/mcp",
                    },
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    result_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"k" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert result_id == "vlt_1", "must return the created vault id"
    assert len(created_vault_bodies) == 1, "must POST exactly one vault"
    assert created_vault_bodies[0]["display_name"] == expected_display, (
        f"vault display_name must be {expected_display!r}; "
        f"got {created_vault_bodies[0]['display_name']!r}"
    )


async def test_two_agents_one_account_get_distinct_vaults() -> None:
    """SC-1: two distinct agent_ids under one account_id produce two distinct vault create calls."""
    account_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    created_vault_display_names: list[str] = []
    existing_vaults: list[dict[str, Any]] = []
    per_vault_creds: dict[str, list[dict[str, Any]]] = {}
    vault_counter = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": list(existing_vaults), "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            body = json.loads(req.content)
            dn = body["display_name"]
            created_vault_display_names.append(dn)
            vault_counter[0] += 1
            vid = f"vlt_{vault_counter[0]}"
            vault = _vault_obj(vid, dn, "2026-04-24T00:00:00Z")
            existing_vaults.append(vault)
            per_vault_creds[vid] = []
            return httpx.Response(200, json=vault)
        for vid in list(per_vault_creds.keys()):
            if req.method == "GET" and req.url.path == f"/v1/vaults/{vid}/credentials":
                return httpx.Response(200, json={"data": per_vault_creds[vid], "has_more": False})
            if req.method == "POST" and req.url.path == f"/v1/vaults/{vid}/credentials":
                body = json.loads(req.content)
                cred: dict[str, Any] = {
                    "id": f"vcrd_{vid}_{len(per_vault_creds[vid])}",
                    "type": "vault_credential",
                    "vault_id": vid,
                    "metadata": {},
                    "created_at": "2026-04-24T00:00:00Z",
                    "updated_at": "2026-04-24T00:00:00Z",
                    "auth": body["auth"],
                }
                per_vault_creds[vid].append(cred)
                return httpx.Response(200, json=cred)
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))

    vault_a = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_a,
        jwt_secret=b"k" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )
    vault_b = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_b,
        jwt_secret=b"k" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert vault_a != vault_b, "two agents under one account must resolve to two distinct vault ids"
    assert len(created_vault_display_names) == 2, (
        "two POSTs to /v1/vaults must occur — one per agent"
    )
    expected_a = f"daimon-mcp:{account_id}:{agent_a}"
    expected_b = f"daimon-mcp:{account_id}:{agent_b}"
    assert expected_a in created_vault_display_names, (
        f"vault for agent A must use display_name {expected_a!r}"
    )
    assert expected_b in created_vault_display_names, (
        f"vault for agent B must use display_name {expected_b!r}"
    )


async def test_agent_x_vault_never_holds_agent_y_external_cred() -> None:
    """SC-3c regression: credential written for agent A is NOT visible
    in the vault resolved for agent B.

    The handler models per-vault credential lists as dict[vault_id, list[cred]].
    """
    account_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    display_a = f"daimon-mcp:{account_id}:{agent_a}"
    display_b = f"daimon-mcp:{account_id}:{agent_b}"
    ext_url = "https://ext.example.com/mcp"

    # Start with both vaults pre-created; agent B's vault is empty.
    vaults = [
        _vault_obj("vlt_a", display_a, "2026-04-01T00:00:00Z"),
        _vault_obj("vlt_b", display_b, "2026-04-01T00:00:00Z"),
    ]
    per_vault_creds: dict[str, list[dict[str, Any]]] = {
        "vlt_a": [],
        "vlt_b": [],
    }
    created_bodies: list[dict[str, Any]] = []
    deleted_ids: list[str] = []

    handler = _stateful_vault_handler(
        initial_vaults=vaults,
        per_vault_creds=per_vault_creds,
        created_bodies=created_bodies,
        deleted_ids=deleted_ids,
    )

    client = _make_client(httpx.MockTransport(handler))

    # Write external cred for agent A.
    await add_external_mcp_credential(
        client,
        account_id=account_id,
        agent_id=agent_a,
        jwt_secret=b"s" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        mcp_server_url=ext_url,
        token="tok_a",
    )

    # Assert agent B's vault has no credential at ext_url.
    agent_b_creds = per_vault_creds["vlt_b"]
    b_has_ext_url = any(c.get("auth", {}).get("mcp_server_url") == ext_url for c in agent_b_creds)
    assert not b_has_ext_url, (
        f"agent B's vault must not contain a credential at {ext_url!r} "
        f"written for agent A; got agent B creds: {agent_b_creds}"
    )


async def test_ensure_agent_mcp_vault_does_not_restamp_matching_url_credential() -> None:
    """Phase 88-03 (T-88-03-02): no delete+recreate on a matching-URL credential.

    When the vault already has a static_bearer credential at the current public_url,
    ensure_agent_mcp_vault must leave it in place — no DELETE, no extra POST.
    The warm re-stamp race (A3) is eliminated: the long-lived credential is
    identity-stable and nothing per-turn mutates it.
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    public_url = "https://mcp.example.com/mcp"
    call_log: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        call_log.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [_vault_obj("vlt_warm", display, "2026-04-01T00:00:00Z")],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_existing",
                            "type": "credential",
                            "vault_id": "vlt_warm",
                            "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                        }
                    ],
                    "has_more": False,
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    vault_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url=public_url,
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert vault_id == "vlt_warm", "must return the existing warm vault"
    mutating = [c for c in call_log if c[0] in ("DELETE", "POST")]
    assert mutating == [], (
        f"matching-URL warm vault must NOT trigger any DELETE or POST "
        f"(warm re-stamp race A3 eliminated — Phase 88-03); got: {mutating}"
    )


async def test_ensure_agent_mcp_vault_long_lived_credential_never_carries_is_admin() -> None:
    """Phase 88 ADMIN-01: the long-lived Discord vault credential never carries is_admin.

    This guards the invariant that ensure_agent_mcp_vault cannot bake privilege
    escalation into the daimon-mcp credential that an in-flight session or future
    turn reuses. is_admin must never appear on the cold-path minted token.
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    captured: list[dict[str, object]] = []
    secret = b"k" * 32

    client = _make_client(httpx.MockTransport(_cold_path_handler(display, captured)))
    await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=secret,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert len(captured) == 1, "exactly one credential POST on cold path"
    token = captured[0]["auth"]["token"]  # type: ignore[index]
    claims = pyjwt.decode(token, secret, algorithms=["HS256"])  # type: ignore[arg-type]
    assert "is_admin" not in claims, (
        "long-lived vault credential must NEVER carry is_admin (Phase 88 ADMIN-01); "
        "a non-admin acting in a thread the creator started must not inherit admin "
        "from a frozen credential"
    )
    assert "internal" not in claims, (
        "long-lived Discord vault credential must NEVER carry internal claim — "
        "only mint_internal_mcp_token emits internal (ADMIN-02)"
    )


async def test_minted_jwt_has_no_agent_claim() -> None:
    """SC-4: the daimon-mcp JWT minted into the vault carries no agent or agent_id claim.

    The vault is per-agent (storage location), but the JWT content stays
    account-scoped (account/platform/guild/is_admin). No agent claim is added.
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{agent_id}"
    captured: list[dict[str, Any]] = []
    secret = b"k" * 32

    client = _make_client(httpx.MockTransport(_cold_path_handler(display, captured)))
    await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=secret,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
    )

    assert len(captured) == 1, "exactly one credential POST on cold path"
    token = captured[0]["auth"]["token"]
    claims = pyjwt.decode(token, secret, algorithms=["HS256"])

    assert "agent" not in claims, "daimon-mcp JWT must NOT carry an 'agent' claim (SC-4)"
    assert "agent_id" not in claims, "daimon-mcp JWT must NOT carry an 'agent_id' claim (SC-4)"
    # Verify the expected account-scoped claims are present.
    # The account UUID is carried as the JWT `sub` claim.
    assert "sub" in claims, "JWT must carry sub (account) claim"
    # Phase 58.5 re-key: platform and guild_id are NOT carried as wire claims —
    # the JWT is account-scoped only (sub + iat).
    assert "platform" not in claims, "daimon-mcp JWT must NOT carry a platform wire claim (58.5)"
    assert "guild_id" not in claims, "daimon-mcp JWT must NOT carry a guild_id wire claim (58.5)"
