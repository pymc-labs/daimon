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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_installation_reconcile import (
    drain_github_installation_reconciliations,
)
from daimon.core.stores import github_app_installations as install_store
from daimon.core.stores import github_installation_reconciliation, github_push_resync
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


def _installation_api_transport(
    repositories: list[str], *, installed: bool = True
) -> httpx.MockTransport:
    """Fake the App installation lookup, list-only token exchange, and list endpoint."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {
            "/app/installations/9901",
            "/app/installations/9902",
            "/app/installations/9905",
            "/app/installations/9906",
        }:
            if not installed:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"account": {"login": "test-owner"}})
        if request.url.path in {
            "/app/installations/9901/access_tokens",
            "/app/installations/9902/access_tokens",
            "/app/installations/9905/access_tokens",
            "/app/installations/9906/access_tokens",
        }:
            assert request.read() == b'{"permissions":{"metadata":"read"}}', (
                "reconciliation token must request metadata only and must not scope normal clone auth"
            )
            return httpx.Response(201, json={"token": "metadata-only-token"})
        if request.url.path == "/installation/repositories":
            assert request.headers["Authorization"] == "Bearer metadata-only-token", (
                "repo listing must use the metadata-only installation token"
            )
            page = int(request.url.params["page"])
            assert page == 1, "small test repository sets should use one API page"
            return httpx.Response(
                200,
                json={
                    "total_count": len(repositories),
                    "repositories": [{"full_name": name} for name in repositories],
                },
            )
        raise AssertionError(f"unexpected GitHub API request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _installation_reconcile_settings() -> GithubSettings:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return GithubSettings(
        app_id="123456",
        app_private_key=SecretStr(private_key_pem.decode()),
    )


async def _drain_installation_reconciliation(
    sessionmaker: async_sessionmaker[AsyncSession],
    repositories: list[str],
    *,
    installed: bool = True,
) -> None:
    async with httpx.AsyncClient(
        transport=_installation_api_transport(repositories, installed=installed)
    ) as client:
        await drain_github_installation_reconciliations(
            sessionmaker=sessionmaker,
            http_client=client,
            github_settings=_installation_reconcile_settings(),
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


async def test_installation_reconciliation_enqueue_failure_is_not_acknowledged(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_app(sessionmaker)

    async def fail_enqueue(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(github_installation_reconciliation, "enqueue", fail_enqueue)
    response = await _post_github(
        app,
        payload_dict=_installation_created_payload(installation_id=8899),
        event="installation",
        delivery_id="installation-persistence-failure",
        raise_app_exceptions=False,
    )

    assert response.status_code == 500, (
        "an installation notification must not ack before persistence"
    )


async def test_github_webhook_installation_event_upserts(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A created event durably queues reconciliation before returning 200."""
    app = _build_app(sessionmaker)
    payload = _installation_created_payload(
        installation_id=9999,
        account_login="test-owner",
        repos=[{"full_name": "test-owner/test-repo"}],
    )
    r = await _post_github(app, payload_dict=payload, event="installation")
    assert r.status_code == 200, "correctly-signed installation event must return 200"

    # Verify that no event payload snapshot is trusted before the API refresh.
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9999)
        job = await github_installation_reconciliation.get(check_session, installation_id=9999)
    assert row is None, "webhook payload must not be treated as the authoritative repo list"
    assert job is not None and job.state == "pending", (
        "installation event must persist reconciliation work before ack"
    )


async def test_deleted_notification_clears_cache_then_current_api_state_can_restore_it(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        await install_store.upsert(
            session,
            installation_id=9905,
            account_login="test-owner",
            repo_full_names=["test-owner/old-repo"],
        )

    app = _build_app(sessionmaker)
    response = await _post_github(
        app,
        payload_dict={"action": "deleted", "installation": {"id": 9905}},
        event="installation",
        delivery_id="possibly-stale-delete",
    )
    assert response.status_code == 200, "deletion should be durably queued before acknowledgment"
    async with db_session_factory() as session:
        cleared = await install_store.get(session, installation_id=9905)
    assert cleared is None, "a deleted notification should clear repository access immediately"

    await _drain_installation_reconciliation(
        sessionmaker,
        ["test-owner/current-repo"],
    )
    async with db_session_factory() as session:
        refreshed = await install_store.get(session, installation_id=9905)
    assert refreshed is not None and refreshed.repo_full_names == ("test-owner/current-repo",), (
        "the App API should restore an installation still present despite a stale delete delivery"
    )


async def test_deleted_installation_confirmed_by_github_stays_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        await install_store.upsert(
            session,
            installation_id=9906,
            account_login="test-owner",
            repo_full_names=["test-owner/old-repo"],
        )
    app = _build_app(sessionmaker)
    response = await _post_github(
        app,
        payload_dict={"action": "deleted", "installation": {"id": 9906}},
        event="installation",
        delivery_id="current-delete",
    )
    assert response.status_code == 200, "the deletion should be acknowledged after cache removal"

    await _drain_installation_reconciliation(
        sessionmaker,
        [],
        installed=False,
    )
    async with db_session_factory() as session:
        row = await install_store.get(session, installation_id=9906)
        job = await github_installation_reconciliation.get(session, installation_id=9906)
    assert row is None, "a confirmed uninstall must leave no cached repository membership"
    assert job is not None and job.state == "done", "a confirmed uninstall should finish the job"


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


async def test_repository_delta_delivered_before_created_snapshot_reconciles_to_github(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A delayed created snapshot cannot overwrite GitHub's current repo set."""
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
    await _drain_installation_reconciliation(
        sessionmaker,
        ["test-owner/initial", "test-owner/added-later"],
    )
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9901)
    assert row is not None
    assert set(row.repo_full_names) == {"test-owner/initial", "test-owner/added-later"}, (
        "the completed API reconciliation must repair the stale created snapshot"
    )


async def test_out_of_order_opposite_repository_deltas_reconcile_to_github(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reversed add/remove deliveries converge to GitHub's current repo set."""
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
    await _drain_installation_reconciliation(sessionmaker, ["test-owner/initial"])
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9902)
    assert row is not None
    assert set(row.repo_full_names) == {"test-owner/initial"}, (
        "the completed API reconciliation must remove stale delivery-order membership"
    )


async def test_reconciliation_page_failure_keeps_previous_complete_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed later page must retry without writing the first page as complete."""
    async with db_session_factory.begin() as session:
        await install_store.upsert(
            session,
            installation_id=9904,
            account_login="test-owner",
            repo_full_names=["test-owner/known-good"],
        )
    app = _build_app(sessionmaker)
    response = await _post_github(
        app,
        payload_dict=_installation_repositories_added_payload(installation_id=9904),
        event="installation_repositories",
        delivery_id="page-failure-delivery",
    )
    assert response.status_code == 200, "the durable queue should be acknowledged before API work"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/9904":
            return httpx.Response(200, json={"account": {"login": "test-owner"}})
        if request.url.path == "/app/installations/9904/access_tokens":
            return httpx.Response(201, json={"token": "metadata-only-token"})
        if request.url.path == "/installation/repositories" and request.url.params["page"] == "1":
            return httpx.Response(
                200,
                headers={
                    "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
                },
                json={
                    "total_count": 101,
                    "repositories": [
                        {"full_name": f"test-owner/repo-{index}"} for index in range(100)
                    ],
                },
            )
        if request.url.path == "/installation/repositories" and request.url.params["page"] == "2":
            return httpx.Response(503, json={"message": "try again"})
        raise AssertionError(f"unexpected GitHub API request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await drain_github_installation_reconciliations(
            sessionmaker=sessionmaker,
            http_client=client,
            github_settings=_installation_reconcile_settings(),
        )

    async with db_session_factory() as session:
        row = await install_store.get(session, installation_id=9904)
        job = await github_installation_reconciliation.get(session, installation_id=9904)
    assert row is not None and row.repo_full_names == ("test-owner/known-good",), (
        "a partial API response must never replace the last complete snapshot"
    )
    assert job is not None and job.state == "pending" and job.attempts == 1, (
        "a failed page must leave the reconciliation durably retryable"
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
