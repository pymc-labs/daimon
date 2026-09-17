"""Shared fixtures for Teams adapter tests."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams.app import AuthorizedTeamsActivity, DirectCoreTurnDispatcher
from daimon.adapters.teams.runtime import TeamsRuntime, build_turn_deps
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_sessions import mark_turn_active
from daimon.testing import (
    build_fake_anthropic,
    ma_session,
    ma_session_agent,
    make_agent_env_echo_handler,
)
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from microsoft_teams.api import (  # pyright: ignore[reportMissingTypeStubs]
    MessageActivityInput,
    SentActivity,
)
from microsoft_teams.common import Client, ClientOptions  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.common.http.client import (  # pyright: ignore[reportMissingTypeStubs]
    MiddlewareContext,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A fixed Entra tenant GUID the seeded daimon tenant folds to via
# derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID).
ENTRA_TENANT_ID = "11111111-2222-3333-4444-55555555555a"
BOT_CLIENT_ID = "bot-client-id"
SERVICE_URL = "https://smba.trafficmanager.net/test"
CONVERSATION_ID = "a:conversation-1"
AAD_OBJECT_ID = "66666666-7777-8888-9999-00000000000a"


def make_message_activity(
    *,
    text: str = "hello daimon",
    activity_id: str = "activity-1",
    conversation_id: str = CONVERSATION_ID,
    conversation_type: str = "personal",
    tenant_id: str = ENTRA_TENANT_ID,
    channel_tenant_id: str | None = ENTRA_TENANT_ID,
    aad_object_id: str | None = AAD_OBJECT_ID,
    channel_id: str = "msteams",
    is_group: bool | None = None,
    service_url: str = SERVICE_URL,
) -> dict[str, object]:
    """A realistic inbound Bot Framework ``message`` activity JSON payload.

    camelCase keys match what the Teams channel actually POSTs; the SDK's
    CustomBaseModel alias generator folds them onto the snake_case model.
    """
    conversation: dict[str, object] = {
        "id": conversation_id,
        "conversationType": conversation_type,
        "tenantId": tenant_id,
    }
    if is_group is not None:
        conversation["isGroup"] = is_group
    from_: dict[str, object] = {"id": f"29:{aad_object_id or 'unknown'}"}
    if aad_object_id is not None:
        from_["aadObjectId"] = aad_object_id
    channel_data: dict[str, object] = {}
    if channel_tenant_id is not None:
        channel_data["tenant"] = {"id": channel_tenant_id}
    return {
        "type": "message",
        "id": activity_id,
        "channelId": channel_id,
        "serviceUrl": service_url,
        "from": from_,
        "conversation": conversation,
        "recipient": {"id": f"28:{BOT_CLIENT_ID}"},
        "text": text,
        "channelData": channel_data,
    }


@dataclasses.dataclass
class SentRequest:
    """One recorded outbound SDK HTTP call."""

    method: str
    url: str
    body: dict[str, object]


@dataclasses.dataclass
class TeamsApiFake:
    """SDK ``Middleware`` that fabricates every outbound Bot Framework response.

    Registered on a ``microsoft_teams.common.Client`` and passed to
    ``create_teams_http_service(client=...)``: the SDK clones the client into
    its ApiClient and ActivitySender, so the same fake sees activity creates,
    updates, and any other call — no transport survives to the network.

    ``next_message_id`` is what POST /activities responses hand back; the
    dispatch test asserts progress and terminal updates then target that id.
    """

    next_message_id: str = "m-1"
    requests: list[SentRequest] = dataclasses.field(default_factory=list)

    async def send(
        self,
        context: MiddlewareContext,
        next: Callable[[], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        del next  # never dispatched — nothing leaves the test process
        body: dict[str, object] = {}
        if isinstance(context.json, dict):
            body = context.json
        elif context.content:
            try:
                body = json.loads(context.content)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                body = {}
        self.requests.append(SentRequest(method=context.method, url=context.url, body=body))
        request = httpx.Request(context.method, context.url)
        update_match = re.match(
            r"/v3/conversations/[^/]+/activities/([^/]+)$", httpx.URL(context.url).path
        )
        # PUT .../activities/{id} must echo the activity id back.
        if context.method == "PUT" and update_match:
            return httpx.Response(200, json={"id": update_match.group(1)}, request=request)
        # Bot Framework answers activity creates/replies with the new resource id.
        if context.method == "POST" and re.search(r"/conversations/[^/]+/activities$", context.url):
            return httpx.Response(200, json={"id": self.next_message_id}, request=request)
        return httpx.Response(200, json={}, request=request)

    @property
    def activity_requests(self) -> list[SentRequest]:
        return [r for r in self.requests if "/activities" in r.url]


def build_teams_client(fake: TeamsApiFake) -> Client:
    """An SDK ``Client`` carrying the fake middleware; passed to the service."""
    client = Client(ClientOptions())
    client.use(fake)
    return client


def build_teams_runtime(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic: AsyncAnthropic | None = None,
    deployment_default: DeploymentDefault | None = None,
) -> TeamsRuntime:
    """A `TeamsRuntime` over the test DB and a fake MA transport.

    Mirrors slack tests' ``make_orchestrate_app`` recipe: a MagicMock
    settings with empty crypto keys, a real ``build_turn_deps`` bundle so
    ``admit`` → ``bind_session`` → ``run_prepared_turn`` runs for real
    against ``make_agent_env_echo_handler``, and the seeded-defaults
    deployment default so tenant-scope config resolves the same tags.
    """
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.mcp.public_url = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")

    anthropic_client = (
        anthropic if anthropic is not None else build_fake_anthropic(make_agent_env_echo_handler())
    )
    resolved_default = (
        deployment_default
        if deployment_default is not None
        else DeploymentDefault(agent_name="daimon", environment_name="default")
    )
    resolver_cache = new_resolver_cache()
    turn_deps = build_turn_deps(
        settings,
        anthropic_client,
        db_factory,
        deployment_default=resolved_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )
    return TeamsRuntime(
        settings=settings,
        anthropic=anthropic_client,
        sessionmaker=db_factory,
        billing_config=None,
        resolver_cache=resolver_cache,
        turn_deps=turn_deps,
        resolver=AsyncMock(return_value=None),  # deny by default; tests build their own
        dispatcher=AsyncMock(),  # tests that exercise dispatch build their own
        deployment_default=resolved_default,
    )


class FakeStream:
    """A minimal ``StreamerProtocol`` for dispatching ``_run_turn`` directly.

    ``update`` feeds a ``SentActivity`` carrying ``message_id`` back through
    the registered ``on_chunk`` handler — the same identity feedback the
    SDK's real streamer gives ``TeamsTurnLifecycle`` — and ``close()``
    resolves with that same id as the message the terminal card landed on.
    """

    def __init__(self, message_id: str = "m-1") -> None:
        self.message_id = message_id
        self.updates: list[str] = []
        self.emitted: list[Any] = []
        self.canceled = False
        self._closed = False
        self._on_chunk: Callable[[SentActivity], Awaitable[None]] | None = None
        self._on_close: Callable[[SentActivity], Awaitable[None]] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def count(self) -> int:
        return len(self.updates) + len(self.emitted)

    @property
    def sequence(self) -> int:
        return self.count

    def on_chunk(self, handler: Callable[[SentActivity], Awaitable[None]]) -> None:
        self._on_chunk = handler

    def on_close(self, handler: Callable[[SentActivity], Awaitable[None]]) -> None:
        self._on_close = handler

    def _sent(self) -> SentActivity:
        return SentActivity(id=self.message_id, activity_params=MessageActivityInput(text=""))

    def update(self, text: str) -> None:
        self.updates.append(text)
        if self._on_chunk is not None:
            asyncio.get_running_loop().create_task(self._on_chunk(self._sent()))

    def emit(self, activity: Any) -> None:
        self.emitted.append(activity)

    def clear_text(self) -> None:
        return None

    async def close(self) -> SentActivity:
        self._closed = True
        sent = self._sent()
        if self._on_close is not None:
            await self._on_close(sent)
        return sent


@dataclasses.dataclass
class FakeTurnContext:
    """The three ``ActivityContext`` members ``_run_turn`` reads, faked.

    ``stream`` is the render target; ``send`` records admission-bailout
    replies; ``conversation_ref.service_url`` is what the orphan marker
    stores in ``active_turn_channel_id``.
    """

    stream: FakeStream
    service_url: str = SERVICE_URL
    sent: list[str] = dataclasses.field(default_factory=list)

    @property
    def conversation_ref(self) -> Any:
        return SimpleNamespace(service_url=self.service_url)

    async def send(self, text: str) -> None:
        self.sent.append(text)


def make_dispatch_target(
    db_factory: async_sessionmaker[AsyncSession],
) -> tuple[DirectCoreTurnDispatcher, FakeTurnContext, AuthorizedTeamsActivity]:
    """A real ``DirectCoreTurnDispatcher`` over the test DB, plus the faked
    inbound context and verified activity its ``dispatch`` accepts."""
    runtime = build_teams_runtime(db_factory)
    dispatcher = DirectCoreTurnDispatcher(turn_deps=runtime.turn_deps, sessionmaker=db_factory)
    ctx = FakeTurnContext(stream=FakeStream())
    activity = AuthorizedTeamsActivity(
        tenant_id=derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID),
        external_user_id=AAD_OBJECT_ID,
        conversation_id=CONVERSATION_ID,
        activity_id="activity-1",
        message="hello daimon",
    )
    return dispatcher, ctx, activity


@contextlib.contextmanager
def patched_turn_pipeline(marked_ids: list[uuid.UUID]) -> Iterator[None]:
    """The seams test_dispatch patches, plus a ``mark_turn_active`` spy.

    ``admit``'s resolvers and MA ``create_session`` are mocked so the turn
    binds a real ``thread_sessions`` row without touching the network; the
    spy records which mapping row carried the orphan marker so a test can
    read that row back after the turn's own path finishes with it.
    """
    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.core.turn.prepare.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.adapters.teams.app.mark_turn_active", new_callable=AsyncMock) as mock_mark,
    ):
        mock_resolve_agent.return_value = "agent_test_id"
        mock_resolve_env.return_value = "env_test_id"
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_create_session.return_value = ma_session(
            id="sess-teams-1",
            agent=ma_session_agent(id="agent_test_id"),
            environment_id="env_test_id",
        )

        async def _spy_mark(*args: Any, **kwargs: Any) -> None:
            marked_ids.append(kwargs["id"])
            await mark_turn_active(*args, **kwargs)

        mock_mark.side_effect = _spy_mark
        yield


@pytest.fixture
def teams_api_fake() -> TeamsApiFake:
    return TeamsApiFake()


@pytest.fixture
def entra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow unauthenticated ingress for the duration of one test.

    The SDK's own DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS env var is read
    at ``App`` construction — set it before ``create_teams_http_service``.
    """
    monkeypatch.setenv("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", "true")


@pytest.fixture
def stub_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit outbound bot-token acquisition (MSAL) in tests.

    ``App._get_bot_token`` delegates to ``TokenManager.get_bot_token``; with
    real client-secret credentials that goes to login.microsoftonline.com
    through MSAL, which no local fake can see. Patching the bound lookup is
    the narrowest seam — the alternative is a static ``token`` AppOption,
    which ``create_teams_http_service`` deliberately does not expose because
    production always carries client-secret credentials.
    """
    from microsoft_teams.apps.token_manager import (  # pyright: ignore[reportMissingTypeStubs]
        TokenManager,
    )

    async def _fake_token(self: TokenManager) -> object:
        return "test-bot-token"

    monkeypatch.setattr(TokenManager, "get_bot_token", _fake_token)
