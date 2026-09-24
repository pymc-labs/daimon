"""Tests for the GitHub App webhook handler (build_github_webhook) — Plan 56-04.

Patterns:
- In-process ASGI app via httpx.ASGITransport (mirrors test_webhooks_stripe.py).
- Payloads signed with HMAC-SHA256 (matching verify_signature in core).
- Transport-level MA fake for install/resync paths — NO model_construct, no AsyncMock.
- Descriptive assertion messages on every assert.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubSettings,
    McpSettings,
    Settings,
)
from daimon.core.stores import github_app_installations as install_store
from daimon.core.stores import github_push_resync
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

_WEBHOOK_SECRET = "test-webhook-secret-abc123"
# A valid Fernet key (base64-urlsafe 32 bytes). The GitHub App webhook requires
# crypto keys to be configured (create_mcp_app raises BootstrapError otherwise),
# since push-driven skill sync must decrypt the MA/MCP credential.
_FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def _sign_payload(body: bytes, secret: str) -> str:
    """Produce the X-Hub-Signature-256 header value for a payload."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Starlette:
    """Build the MCP app with GitHub App settings configured."""
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
            github=GithubSettings(
                app_id="123456",
                app_private_key=SecretStr("stub-pem-not-used-in-these-tests"),
                webhook_secret=SecretStr(_WEBHOOK_SECRET),
            ),
            crypto=CryptoSettings(keys=(SecretStr(_FERNET_KEY),)),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )


def _build_app_no_github(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Starlette:
    """Build the MCP app WITHOUT GitHub App settings — webhook route must not be mounted."""
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )


async def _post_github(
    app: Starlette,
    *,
    payload_dict: dict[str, Any],
    secret: str = _WEBHOOK_SECRET,
    event: str = "push",
    delivery_id: str = "delivery-001",
    bad_signature: bool = False,
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    """POST a signed (or forged) GitHub webhook to the app."""
    body = json.dumps(payload_dict).encode()
    sig = _sign_payload(body, secret) if not bad_signature else "sha256=badhex000"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": event,
                "x-hub-signature-256": sig,
                "x-github-delivery": delivery_id,
                "content-type": "application/json",
            },
        )


# ---------------------------------------------------------------------------
# Payload factories
# ---------------------------------------------------------------------------


def _push_payload(
    full_name: str = "owner/my-repo",
    ref: str = "refs/heads/main",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "repository": {"full_name": full_name},
        "pusher": {"name": "alice"},
    }


def _installation_created_payload(
    installation_id: int = 1234,
    account_login: str = "owner",
    repos: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": "created",
        "installation": {"id": installation_id, "account": {"login": account_login}},
        "repositories": repos or [{"full_name": "owner/my-repo"}],
    }


def _installation_repositories_added_payload(
    installation_id: int = 1234,
    repositories_added: list[dict[str, Any]] | None = None,
    repositories_removed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": "added",
        "installation": {"id": installation_id},
        "repositories_added": repositories_added or [{"full_name": "owner/new-repo"}],
        "repositories_removed": repositories_removed or [],
    }


def _installation_repositories_removed_payload(
    installation_id: int = 1234,
    repositories_removed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": "removed",
        "installation": {"id": installation_id},
        "repositories_added": [],
        "repositories_removed": repositories_removed or [{"full_name": "owner/changed-repo"}],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_github_webhook_rejects_bad_signature(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A push payload with a forged X-Hub-Signature-256 returns 401; no resync is scheduled."""
    app = _build_app(sessionmaker)
    r = await _post_github(app, payload_dict=_push_payload(), bad_signature=True)
    assert r.status_code == 401, (
        "forged/missing signature must return 401 (SC-3: reject before parse)"
    )


async def test_github_webhook_valid_push_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A correctly-signed push is committed before the webhook returns 200."""
    app = _build_app(sessionmaker)
    r = await _post_github(
        app,
        payload_dict=_push_payload(full_name="https://github.com/owner/my-repo.git"),
        event="push",
        delivery_id="durable-delivery",
    )
    assert r.status_code == 200, "correctly-signed push webhook must return 200"
    async with db_session_factory() as session:
        row = await github_push_resync.get_for_repo_ref(
            session,
            repo_full_name="owner/my-repo",
            ref="refs/heads/main",
        )
    assert row is not None, "the 200 response must follow durable queue insertion"
    assert row.delivery_id == "durable-delivery", "the job should retain the GitHub delivery ID"
    assert row.generation == 1, "the first push should create the first generation"
    assert row.state == "pending", "the persisted push must remain schedulable after process death"

    duplicate = await _post_github(
        app,
        payload_dict=_push_payload(),
        event="push",
        delivery_id="durable-delivery",
    )
    assert duplicate.status_code == 200, "GitHub delivery retries should be acknowledged"
    async with db_session_factory() as session:
        after_duplicate = await github_push_resync.get_for_repo_ref(
            session,
            repo_full_name="owner/my-repo",
            ref="refs/heads/main",
        )
    assert after_duplicate is not None, "the original queue row should remain present"
    assert after_duplicate.generation == 1, "a duplicate delivery must not add a generation"


async def test_github_webhook_queue_failure_is_not_acknowledged(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistence failure returns non-2xx; GitHub does not auto-redeliver it."""
    app = _build_app(sessionmaker)

    async def fail_enqueue(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(github_push_resync, "enqueue", fail_enqueue)
    response = await _post_github(
        app,
        payload_dict=_push_payload(),
        delivery_id="persistence-failure",
        raise_app_exceptions=False,
    )

    assert response.status_code == 500, "unpersisted work must never receive HTTP 200"


async def test_github_webhook_installation_event_upserts(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A correctly-signed installation 'created' event upserts the install store, returns 200."""
    app = _build_app(sessionmaker)
    payload = _installation_created_payload(
        installation_id=9999,
        account_login="test-owner",
        repos=[{"full_name": "test-owner/test-repo"}],
    )
    r = await _post_github(app, payload_dict=payload, event="installation")
    assert r.status_code == 200, "correctly-signed installation event must return 200"

    # Verify the install was persisted
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9999)
    assert row is not None, "installation event must persist the install row"
    assert row.account_login == "test-owner", (
        "install row must record the account login from the payload"
    )
    assert "test-owner/test-repo" in row.repo_full_names, (
        "install row must record the repo from the payload"
    )


async def test_github_webhook_ignores_non_lifecycle_installation_actions(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A suspend event must not erase the repository snapshot from a created event."""
    async with db_session_factory.begin() as seed_session:
        await install_store.upsert(
            seed_session,
            installation_id=9998,
            account_login="test-owner",
            repo_full_names=["test-owner/kept-repo"],
        )

    app = _build_app(sessionmaker)
    r = await _post_github(
        app,
        payload_dict={"action": "suspend", "installation": {"id": 9998}},
        event="installation",
    )
    assert r.status_code == 200, "unrelated installation actions should be acknowledged"

    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9998)
    assert row is not None, "suspend event must preserve the installation row"
    assert list(row.repo_full_names) == ["test-owner/kept-repo"], (
        "suspend event must not replace the created-event repository snapshot"
    )


async def test_repository_delta_delivered_before_created_snapshot_is_lost(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A delayed created snapshot can overwrite a repository delta delivered first."""
    app = _build_app(sessionmaker)
    async with sessionmaker.begin() as session:
        await install_store.upsert(
            session,
            installation_id=9901,
            account_login="test-owner",
            repo_full_names=["test-owner/initial"],
        )
    delta = _installation_repositories_added_payload(
        installation_id=9901,
        repositories_added=[{"full_name": "test-owner/added-later"}],
    )
    created = _installation_created_payload(
        installation_id=9901,
        account_login="test-owner",
        repos=[{"full_name": "test-owner/initial"}],
    )

    delta_response = await _post_github(
        app, payload_dict=delta, event="installation_repositories", delivery_id="delta-first"
    )
    created_response = await _post_github(
        app, payload_dict=created, event="installation", delivery_id="created-late"
    )

    assert delta_response.status_code == created_response.status_code == 200
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9901)
    assert row is not None
    assert set(row.repo_full_names) == {"test-owner/initial"}, (
        "the stale created snapshot replaces the earlier delta; current cache misses "
        "test-owner/added-later even though that repository was added after creation"
    )


async def test_out_of_order_opposite_repository_deltas_leave_stale_membership(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Delivering a later removal before its earlier addition leaves the repo cached."""
    app = _build_app(sessionmaker)
    async with sessionmaker.begin() as session:
        await install_store.upsert(
            session,
            installation_id=9902,
            account_login="test-owner",
            repo_full_names=["test-owner/initial"],
        )

    removed = _installation_repositories_removed_payload(
        installation_id=9902,
        repositories_removed=[{"full_name": "test-owner/changed-repo"}],
    )
    added = _installation_repositories_added_payload(
        installation_id=9902,
        repositories_added=[{"full_name": "test-owner/changed-repo"}],
    )
    remove_response = await _post_github(
        app,
        payload_dict=removed,
        event="installation_repositories",
        delivery_id="remove-first",
    )
    add_response = await _post_github(
        app,
        payload_dict=added,
        event="installation_repositories",
        delivery_id="add-late",
    )

    assert remove_response.status_code == add_response.status_code == 200
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9902)
    assert row is not None
    assert "test-owner/changed-repo" in row.repo_full_names, (
        "the cache reflects delivery order (remove then add), although the actual "
        "event order was add then remove"
    )


async def test_malformed_created_snapshot_does_not_clear_cached_repositories(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed repositories field must not replace a valid cached repo list."""
    app = _build_app(sessionmaker)
    async with sessionmaker.begin() as session:
        await install_store.upsert(
            session,
            installation_id=9903,
            account_login="test-owner",
            repo_full_names=["test-owner/known-good"],
        )

    malformed = {
        "action": "created",
        "installation": {"id": 9903, "account": {"login": "test-owner"}},
        "repositories": "not-an-array",
    }
    response = await _post_github(app, payload_dict=malformed, event="installation")

    assert response.status_code == 200
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9903)
    assert row is not None
    assert set(row.repo_full_names) == {"test-owner/known-good"}, (
        "malformed snapshot data must not be interpreted as an authoritative empty set"
    )


async def test_github_webhook_malformed_payload_is_200_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A signed-but-missing-repository push payload returns 200 no-op (mirrors Stripe pattern)."""
    app = _build_app(sessionmaker)
    # Push payload with no 'repository' key
    malformed = {"ref": "refs/heads/main", "pusher": {"name": "alice"}}
    r = await _post_github(app, payload_dict=malformed, event="push")
    assert r.status_code == 200, (
        "malformed/incomplete push payload must return 200 no-op (never crash the handler)"
    )


async def test_github_webhook_route_not_mounted_without_app_settings(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The /webhooks/github route must not be mounted when App settings are absent."""
    app = _build_app_no_github(sessionmaker)
    body = json.dumps(_push_payload()).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "push",
                "x-hub-signature-256": _sign_payload(body, _WEBHOOK_SECRET),
                "content-type": "application/json",
            },
        )
    assert r.status_code == 404, (
        "/webhooks/github must return 404 when GitHub App settings are not configured"
    )


async def test_github_webhook_unhandled_event_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An unrecognized event type returns 200 (no-op)."""
    app = _build_app(sessionmaker)
    r = await _post_github(app, payload_dict={"some": "data"}, event="ping")
    assert r.status_code == 200, "unhandled event types must return 200 (not 4xx/5xx)"
