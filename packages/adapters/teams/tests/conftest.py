"""Shared fixtures for Teams adapter tests."""

from __future__ import annotations

import dataclasses
import functools
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import TeamsSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.posted_controls.teams_card import ADAPTIVE_CARD_TYPE
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import (
    build_fake_anthropic,
    ma_session,
    ma_session_agent,
    make_agent_env_echo_handler,
)
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from jsonschema import Draft6Validator
from jsonschema.exceptions import best_match
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
USER_NAME = "Ada Lovelace"
# Vendored for an offline test from
# https://raw.githubusercontent.com/microsoft/AdaptiveCards/main/schemas/1.5.0/adaptive-card.json
CARD_SCHEMA = Path(__file__).parent / "data/adaptive-card-v1.5.schema.json"
# Teams' own element, outside the Adaptive Cards schema:
# https://learn.microsoft.com/microsoftteams/platform/task-modules-and-cards/cards/cards-format#codeblock-in-adaptive-cards
CODE_BLOCK = {
    "type": "object",
    "properties": {
        "type": {"enum": ["CodeBlock"]},
        "codeSnippet": {"type": "string"},
        "language": {"type": "string"},
        "startLineNumber": {"type": "number"},
    },
    "required": ["type", "codeSnippet"],
    "additionalProperties": False,
}
MAX_ACTIVITY_BYTES = 26_000  # a margin under Teams' 28 KB


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
    if is_group is None and conversation_type != "personal":
        is_group = True  # Teams marks every channel and group chat.
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


def make_invoke(
    name: str, value: dict[str, object], *, user: str = AAD_OBJECT_ID, chat: str = CONVERSATION_ID
) -> dict[str, object]:
    """An inbound invoke from a personal chat, sent from the card message `m-7`."""
    return {
        "type": "invoke",
        "name": name,
        "id": f"invoke-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": f"29:{user}", "aadObjectId": user, "name": USER_NAME},
        "recipient": {"id": BOT_ACCOUNT_ID, "name": "daimon"},
        "conversation": {"id": chat, "conversationType": "personal", "tenantId": ENTRA_TENANT_ID},
        "replyToId": "m-7",
        "value": value,
    }


def make_card_action(
    verb: str, op: str, *, user: str = AAD_OBJECT_ID, **data: object
) -> dict[str, object]:
    """A click on an `Action.Execute` button routed to `verb`."""
    action = {"type": "Action.Execute", "verb": verb, "data": {"action": verb, "op": op} | data}
    return make_invoke("adaptiveCard/action", {"action": action, "trigger": "manual"}, user=user)


def make_inbound(
    text: str = "hi",
    *,
    user: str = AAD_OBJECT_ID,
    conversation: str = CONVERSATION_ID,
    kind: Literal["dm", "channel"] = "dm",
    setup_thread_id: str | None = None,
) -> TeamsInbound:
    return TeamsInbound(
        kind=kind,
        entra_tenant_id=ENTRA_TENANT_ID,
        user_id=user,
        conversation_id=conversation,
        channel_id=conversation,
        activity_id=str(uuid.uuid4()),
        text=text,
        service_url=SERVICE_URL,
        setup_thread_id=setup_thread_id,
    )


async def bot_token() -> str:
    return "bot-token"


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


@functools.cache
def card_validator() -> Draft6Validator:
    schema = json.loads(CARD_SCHEMA.read_text())
    schema["definitions"]["ImplementationsOf.Element"]["anyOf"].append(CODE_BLOCK)
    return Draft6Validator(schema)


def assert_card_renders(content: dict[str, Any]) -> None:
    error = best_match(card_validator().iter_errors(content))
    assert error is None, f"{list(error.absolute_path)}: {error.message[:300]}"
    version = tuple(int(part) for part in content["version"].split("."))
    assert version <= (1, 5), f"Teams renders up to 1.5, not {content['version']}"


def assert_teams_accepts(request: SentRequest) -> None:
    """One recorded activity as Teams takes it: renderable cards, under its size cap, and
    an edit naming the activity it replaces."""
    attachments = cast(list[dict[str, Any]], request.body.get("attachments") or [])
    for attachment in attachments:
        content = attachment.get("content")
        if (
            isinstance(content, dict)
            and cast(dict[str, Any], content).get("type") == "AdaptiveCard"
        ):
            assert attachment.get("contentType") == ADAPTIVE_CARD_TYPE, "Teams shows a card by type"
        if attachment.get("contentType") == ADAPTIVE_CARD_TYPE:
            assert_card_renders(cast(dict[str, Any], content))
    size = len(httpx.Request(request.method, request.url, json=request.body).content)
    assert size < MAX_ACTIVITY_BYTES, f"{size:,} bytes; keep it under {MAX_ACTIVITY_BYTES:,}"
    if request.method == "PUT":
        edited = re.search(r"/activities/([^/]+)$", httpx.URL(request.url).path)
        assert edited and request.body.get("id") == edited.group(1), "an edit names its activity"


def build_teams_client(fake: TeamsApiFake) -> Client:
    client = Client(ClientOptions())
    client.use(fake)
    return client


@dataclasses.dataclass
class FakeSender:
    """A `TeamsSender` recording every send. Indices in `fail_on` raise; in
    `timeout_on` they time out after landing (odd: httpx, even: asyncio)."""

    sent: list[tuple[str, MessageActivityInput, str | None]] = dataclasses.field(
        default_factory=list[tuple[str, MessageActivityInput, str | None]]
    )
    fail_on: set[int] = dataclasses.field(default_factory=set[int])
    timeout_on: set[int] = dataclasses.field(default_factory=set[int])

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        index = len(self.sent)
        self.sent.append((conversation_id, activity.model_copy(deep=True), service_url))
        if index in self.fail_on:
            raise httpx.ConnectError("unreachable")
        if index in self.timeout_on:
            raise httpx.ReadTimeout("slow") if index % 2 else TimeoutError()
        return SentActivity(id=activity.id or f"m-{index + 1}", activity_params=activity)

    @property
    def activities(self) -> list[MessageActivityInput]:
        return [activity for _, activity, _ in self.sent]


def build_teams_runtime(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic: AsyncAnthropic | None = None,
    teams: TeamsSettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    deployment_default: DeploymentDefault | None = None,
) -> TeamsRuntime:
    """A runtime over the test DB and a fake MA transport, with real turn deps.

    Outbound HTTP never reaches the network: it answers 404 unless given.
    """
    settings = MagicMock()
    settings.teams = teams or teams_settings()
    settings.crypto.keys = ()
    settings.mcp.public_url = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    settings.tool_safety = OPEN_TOOL_SAFETY
    client = anthropic or build_fake_anthropic(make_agent_env_echo_handler())
    deployment_default = deployment_default or DeploymentDefault(
        agent_name="daimon", environment_name="default"
    )
    resolver_cache = new_resolver_cache()
    return TeamsRuntime(
        settings=settings,
        anthropic=client,
        sessionmaker=db_factory,
        billing_config=None,
        http_client=http_client
        or httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
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


@asynccontextmanager
async def running_service(
    runtime: TeamsRuntime, fake: TeamsApiFake
) -> AsyncIterator[TeamsHttpService]:
    """The real HTTP service over `runtime`, started, with `fake` as the Bot Framework."""
    settings = runtime.settings.teams
    assert settings is not None
    service = create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(fake)
    )
    async with asgi_lifespan(service.app):
        # One shared test connection: let the boot sweep finish first.
        await service.turns.start()
        yield service


async def post_activity(service: TeamsHttpService, payload: dict[str, object]) -> Any:
    """POST one activity to `/api/messages`; the invoke response body, if any."""
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/messages", json=payload)
    assert response.status_code == 200, response.text
    return response.json() if response.content else None


@contextmanager
def patched_turns(answer: str = "On it.") -> Iterator[list[dict[str, Any]]]:
    """Admission patched and every MA turn answering `answer`; yields each turn's kwargs."""
    turns: list[dict[str, Any]] = []

    async def fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        turns.append(kwargs)
        state = TurnState(content=[TextBlock(kind="text", text=answer)])
        await lifecycle.on_terminal_success(state)
        return state

    with patched_admission(), patch("daimon.core.turn.run.run_turn", side_effect=fake_run_turn):
        yield turns


@pytest.fixture
async def provisioned_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)


@pytest.fixture(autouse=True)
def no_boot_provisioning() -> Iterator[AsyncMock]:
    """Boot provisioning reconciles against MA; service tests skip it."""
    with patch(
        "daimon.adapters.teams.app.provision_configured_tenant", new_callable=AsyncMock
    ) as provision:
        yield provision


@pytest.fixture(autouse=True)
def no_wake_poller() -> Iterator[AsyncMock]:
    """The wake poller would share the test connection with the turn under test."""
    with patch("daimon.adapters.teams.app.run_wake_poller", new_callable=AsyncMock) as poller:
        yield poller


@pytest.fixture
def teams_api_fake() -> TeamsApiFake:
    return TeamsApiFake()


@pytest.fixture
def entra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow unauthenticated ingress; the SDK reads this at `App` construction."""
    monkeypatch.setenv("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", "true")


@pytest.fixture
def stub_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip MSAL token acquisition, which no local fake can intercept.

    Stubbed at `get_app_token`, which the SDK's bot token and the adapter's
    pasted-image fetch both go through.
    """
    from microsoft_teams.apps.token_manager import TokenManager

    async def _fake_token(self: TokenManager, *args: object, **kwargs: object) -> object:
        return "test-bot-token"

    monkeypatch.setattr(TokenManager, "get_app_token", _fake_token)
