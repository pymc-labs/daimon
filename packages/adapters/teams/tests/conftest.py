"""Shared fixtures for Teams adapter tests."""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams.runtime import TeamsRuntime, build_turn_deps
from daimon.core.config import TeamsSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
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
from microsoft_teams.api import MessageActivityInput, SentActivity
from microsoft_teams.common import Client, ClientOptions
from microsoft_teams.common.http.client import MiddlewareContext
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Synthetic identifiers only.
ENTRA_TENANT_ID = str(uuid.UUID(int=1))
AAD_OBJECT_ID = str(uuid.UUID(int=2))
OTHER_AAD_OBJECT_ID = str(uuid.UUID(int=3))
BOT_CLIENT_ID = "bot-client-id"
BOT_ACCOUNT_ID = f"28:{BOT_CLIENT_ID}"
SERVICE_URL = "https://smba.trafficmanager.net/test"
CONVERSATION_ID = "a:conversation-1"
CHANNEL_ID = "19:channel-1@thread.tacv2"
THREAD_ID = f"{CHANNEL_ID};messageid=1700000000001"


def teams_settings(*, enabled: bool = True, admins: tuple[str, ...] = ()) -> TeamsSettings:
    return TeamsSettings(
        client_id=BOT_CLIENT_ID,
        client_secret=SecretStr("test-secret"),
        tenant_id=ENTRA_TENANT_ID,
        enabled=enabled,
        admin_user_ids=admins,
    )


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
    mention_bot: bool = False,
    service_url: str = SERVICE_URL,
) -> dict[str, object]:
    """An inbound Bot Framework `message` activity, camelCase as Teams posts it.

    `mention_bot` prefixes an `<at>` mention of the bot, as a channel post has.
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
    if conversation_type == "channel":
        channel_data["channel"] = {"id": conversation_id.split(";", 1)[0]}
        channel_data["team"] = {"id": "19:team@thread.tacv2"}
    entities: list[dict[str, object]] = []
    if mention_bot:
        mention = "<at>daimon</at>"
        text = f"{mention} {text}"
        entities.append(
            {
                "type": "mention",
                "text": mention,
                "mentioned": {"id": BOT_ACCOUNT_ID, "name": "daimon"},
            }
        )
    return {
        "type": "message",
        "id": activity_id,
        "channelId": channel_id,
        "serviceUrl": service_url,
        "from": from_,
        "conversation": conversation,
        "recipient": {"id": BOT_ACCOUNT_ID},
        "text": text,
        "entities": entities,
        "channelData": channel_data,
    }


def make_channel_activity(**kwargs: Any) -> dict[str, object]:
    """A channel-thread reply that @mentions the bot."""
    params: dict[str, Any] = {
        "conversation_id": THREAD_ID,
        "conversation_type": "channel",
        "mention_bot": True,
    }
    return make_message_activity(**(params | kwargs))


@dataclasses.dataclass
class SentRequest:
    """One recorded outbound SDK HTTP call."""

    method: str
    url: str
    body: dict[str, object]


@dataclasses.dataclass
class TeamsApiFake:
    """SDK middleware that fabricates every outbound Bot Framework response.

    Passed to `create_teams_http_service(client=...)`; nothing reaches the
    network. POST /activities answers with `m-<n>`; PUT echoes the id.
    """

    requests: list[SentRequest] = dataclasses.field(default_factory=list[SentRequest])
    posted: int = 0

    async def send(
        self,
        context: MiddlewareContext,
        next: Callable[[], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        del next
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
        update = re.search(
            r"/v3/conversations/[^/]+/activities/([^/]+)$", httpx.URL(context.url).path
        )
        if context.method == "PUT" and update:
            return httpx.Response(200, json={"id": update.group(1)}, request=request)
        if context.method == "POST" and re.search(r"/conversations/[^/]+/activities$", context.url):
            self.posted += 1
            return httpx.Response(200, json={"id": f"m-{self.posted}"}, request=request)
        return httpx.Response(200, json={}, request=request)

    @property
    def activity_requests(self) -> list[SentRequest]:
        return [r for r in self.requests if "/activities" in r.url]


def build_teams_client(fake: TeamsApiFake) -> Client:
    client = Client(ClientOptions())
    client.use(fake)
    return client


@dataclasses.dataclass
class FakeSender:
    """A `TeamsSender` recording every send. Indices in `fail_on` raise."""

    sent: list[tuple[str, MessageActivityInput, str | None]] = dataclasses.field(
        default_factory=list[tuple[str, MessageActivityInput, str | None]]
    )
    fail_on: set[int] = dataclasses.field(default_factory=set[int])

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        index = len(self.sent)
        self.sent.append((conversation_id, activity.model_copy(deep=True), service_url))
        if index in self.fail_on:
            raise httpx.ConnectError("unreachable")
        return SentActivity(id=activity.id or f"m-{index + 1}", activity_params=activity)

    @property
    def activities(self) -> list[MessageActivityInput]:
        return [activity for _, activity, _ in self.sent]


def build_teams_runtime(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic: AsyncAnthropic | None = None,
    teams: TeamsSettings | None = None,
) -> TeamsRuntime:
    """A runtime over the test DB and a fake MA transport, with real turn deps."""
    settings = MagicMock()
    settings.teams = teams or teams_settings()
    settings.crypto.keys = ()
    settings.mcp.public_url = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    client = anthropic or build_fake_anthropic(make_agent_env_echo_handler())
    deployment_default = DeploymentDefault(agent_name="daimon", environment_name="default")
    resolver_cache = new_resolver_cache()
    return TeamsRuntime(
        settings=settings,
        anthropic=client,
        sessionmaker=db_factory,
        billing_config=None,
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            client,
            db_factory,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
        deployment_default=deployment_default,
    )


@contextmanager
def patched_admission() -> Iterator[None]:
    """Admission resolves a fake agent and binding creates a fake MA session."""
    with (
        patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock) as agent,
        patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock) as env,
        patch("daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock) as balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as cap,
        patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock) as create,
    ):
        agent.return_value = "agent_test_id"
        env.return_value = "env_test_id"
        balance.return_value = False
        cap.return_value = False
        create.return_value = ma_session(
            id="sess-teams-1",
            agent=ma_session_agent(id="agent_test_id"),
            environment_id="env_test_id",
        )
        yield


@pytest.fixture(autouse=True)
def no_boot_provisioning() -> Iterator[AsyncMock]:
    """Boot provisioning reconciles against MA; service tests skip it."""
    with patch(
        "daimon.adapters.teams.app.provision_configured_tenant", new_callable=AsyncMock
    ) as provision:
        yield provision


@pytest.fixture
def teams_api_fake() -> TeamsApiFake:
    return TeamsApiFake()


@pytest.fixture
def entra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow unauthenticated ingress; the SDK reads this at `App` construction."""
    monkeypatch.setenv("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", "true")


@pytest.fixture
def stub_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip MSAL bot-token acquisition, which no local fake can intercept."""
    from microsoft_teams.apps.token_manager import TokenManager

    async def _fake_token(self: TokenManager) -> object:
        return "test-bot-token"

    monkeypatch.setattr(TokenManager, "get_bot_token", _fake_token)
