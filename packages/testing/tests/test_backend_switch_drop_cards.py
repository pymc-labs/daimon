"""H6 end to end: a chat switch to another backend, with declared drops, on every chat surface.

Each test drives the real path: the model's `request_agent_backend_switch` preview, the
platform's own confirmation hook built from the turn's admission (its `prepare` recomputes
the preview and posts the card), the requester's click (its `check` recomputes the preview
again), then the approved tool call applying through the shared service. Nothing on a
card is hand-written: the "won't be available" list comes from the real preview.

The switched agent carries no tools of its own. A tool-bearing source cannot be projected
across profiles yet (`classify_source_obligations` reports `source_definition` for any
native tool controls), so cross-backend previews of such agents refuse outright. The chat
session's own frozen agent keeps the gated `daimon-mcp` toolset the card gate requires.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.core.confirmation import ApprovedConfirmation, prompt_for_tool_call
from daimon.core.native_admins import GroupMembersCache
from daimon.core.stores.domain import Role
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from daimon.testing.adapters.slack.notices import shown_text
from daimon.testing.db import db_committing_engine  # noqa: F401

PLATFORMS = ("discord", "slack", "teams")
TARGETS = ("openai", "gemini")
#: The refusal tests below run on one target. The click gate (`verified` in
#: `core/confirmation.py`, the comparison of tenant, agent, target, allowed, revision,
#: digest and accepted drops) compares by equality and has no provider branch; `DROPS`
#: is one constant for both targets. Provider-specific previews are exercised once, by
#: the apply-once test, which keeps every target.
TARGET = "openai"
#: Each platform's requester id, in that platform's own id shape.
REQUESTER = {
    "discord": "123",
    "slack": "U123",
    "teams": "00000000-0000-4000-8000-000000000123",
}
STRANGER = {
    "discord": "999",
    "slack": "U999",
    "teams": "00000000-0000-4000-8000-000000000999",
}
#: The losses of a tool-less Managed Agent on any other profile.
DROPS = ("memory", "sandbox")


@dataclasses.dataclass
class Chat:
    platform: str
    runtime: Any  # McpRuntime
    auth: Any  # AuthIdentity
    origin: Any  # TurnOriginRow
    service: Any  # AgentBackendService, no capture: reads revisions and audit only
    payload: dict[str, Any]  # the switched agent, as MA returns it
    turn_deps: Any
    admission: Any  # the chat turn's real admission shape

    async def tool(self, target: str, **kwargs: Any) -> Any:
        from daimon.adapters.mcp.tools.agents import _request_agent_backend_switch_impl

        return await _request_agent_backend_switch_impl(
            self.runtime,
            self.auth,
            agent_name="Researcher",
            target_backend=target,  # pyright: ignore[reportArgumentType]
            origin_context_id=str(self.origin.id),
            **kwargs,
        )

    async def current(self) -> tuple[Any, Any]:
        from daimon.core.channel_backend import load_backend_agent

        actual = await load_backend_agent(
            self.runtime.client,
            self.runtime.session_factory,
            tenant_id=self.auth.tenant_id,
            agent_name="Researcher",
        )
        return actual, await self.service.current(actual)

    async def audit(self) -> list[Any]:
        actual, _ = await self.current()
        return list(await self.service.audit(self.auth.tenant_id, agent_id=actual.agent_id))


@pytest.fixture(params=PLATFORMS)
async def chat(request: pytest.FixtureRequest, db_preflight, db_committing_engine):  # noqa: F811
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.core.authz import Place, Subject, Surface, build_agent_ref, build_subject
    from daimon.core.channel_backend import BackendActor, make_backend_service
    from daimon.core.config import McpSettings, Settings, TurnSettings
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.turn_origins import create_origin
    from daimon.core.turn.admission import Admission, AdmissionGrant
    from daimon.core.turn.deps import build_turn_deps
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import (
        make_account,
        make_platform_principal,
        make_tenant,
        make_thread_session,
    )
    from daimon.testing.ma_models import ma_agent, ma_environment
    from mux.testing.drivers.anthropic.ma import MARouter, list_response
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
    from sqlalchemy.ext.asyncio import async_sessionmaker

    platform: str = request.param
    requester = REQUESTER[platform]
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform=platform)  # pyright: ignore[reportArgumentType]
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=account, platform=platform, external_id=requester
        )
        await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            platform=platform,
            thread_id="thread",
            channel_id="channel",
            ma_session_id="sess_owned",
            ma_agent_id="agent_owned",
        )
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform=platform,
            parent_channel_id="channel",
            thread_id="thread",
            responder_ma_agent_id="agent_owned",
            responder_name="Researcher",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN,
            sender_foreign=False,
            shared_channel=False,
            now=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    gated = ma_agent(
        id="agent_owned",
        tenant_id=str(tenant.id),
        name="Researcher",
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
                "configs": [
                    {
                        "name": "request_agent_backend_switch",
                        "enabled": True,
                        "permission_policy": {"type": "always_ask"},
                    }
                ],
            }
        ],
        mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": "https://mcp.invalid"}],
    )
    frozen = ma_session(
        id="sess_owned",
        agent=gated,
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_account": str(account.id),
            "daimon_channel": "channel",
            "daimon_thread": "thread",
        },
    ).model_dump(mode="json")
    payload = ma_agent(id="agent_owned", tenant_id=str(tenant.id), name="Researcher").model_dump(
        mode="json"
    )
    payload["metadata"]["daimon_name"] = "Researcher"
    environment = ma_environment(tenant_id=tenant.id)
    environment.metadata["daimon_name"] = "default"
    router = MARouter()
    # Every read serves the payload as it is at that moment, so a test can drift it.
    router.add("GET", r"/v1/agents", lambda *_: list_response([payload]))
    router.add("GET", r"/v1/agents/agent_owned", lambda *_: httpx.Response(200, json=payload))
    router.add(
        "GET",
        rf"/v1/environments/{environment.id}",
        lambda *_: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    router.add(
        "GET", r"/v1/environments", lambda *_: list_response([environment.model_dump(mode="json")])
    )
    router.add("GET", r"/v1/sessions/sess_owned", lambda *_: httpx.Response(200, json=frozen))
    trace = ScriptedTransport(router=router)
    async with trace.client() as client:
        settings = Settings(
            database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
            anthropic={"api_key": "test-fixture-key"},
            turn=TurnSettings(path="mux", channel_backends=True),
            mcp=McpSettings(public_url="https://mcp.invalid"),
        )
        default = DeploymentDefault(agent_name="Daimon", environment_name="default")
        runtime = McpRuntime(sessions, client, settings, default)
        auth = AuthIdentity(
            tenant_id=tenant.id,
            account_id=account.id,
            role=Role.ADMIN,
            is_admin=True,
            platform=platform,  # pyright: ignore[reportArgumentType]
            platform_user_id=requester,
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_owned"),
        )
        service = make_backend_service(sessions, settings.turn)
        policy = await service.tenant_policy(tenant.id)
        await service.set_tenant_allowlist(
            tenant.id,
            ("anthropic", "openai", "gemini"),
            policy.revision,
            BackendActor(tenant.id, account.id, Subject(is_admin=True)),
        )
        place = Place(channel_id="thread", parent_channel_id="channel")
        native = BetaManagedAgentsAgent.model_validate(payload)
        admission = Admission(
            account.id,
            native,
            environment,  # pyright: ignore[reportArgumentType]
            None,  # pyright: ignore[reportArgumentType]
            grant=AdmissionGrant(
                tenant_id=tenant.id,
                subject=build_subject(is_admin=True, platform_user_id=requester),
                surface=Surface.CHANNEL,
                turn_place=place,
                run_place=place,
                agent=build_agent_ref(native.name, native.metadata or {}, "Researcher"),
                channel_id="channel",
                thread_id="thread",
                is_dm=False,
            ),
            origin_channel_id="channel",
            origin_thread_id="thread",
        )
        turn_deps = build_turn_deps(
            settings,
            client,
            sessions,
            deployment_default=default,
            resolver_cache=new_resolver_cache(),
            billing_config=None,
        )
        yield Chat(platform, runtime, auth, origin, service, payload, turn_deps, admission)
        assert not trace.violations
        assert all(r.method == "GET" for r in trace.requests), "nothing is written to MA"


@pytest.mark.parametrize("binding", ["main", "routed"])
async def test_chat_fixture_refuses_unowned_binds_without_skipping_or_connecting(
    db_preflight,
    db_committing_engine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
):
    # The fixture imports the turn package lazily, and its modules subscript
    # `async_sessionmaker` at import time. Import it before the patch below, or this
    # test fails whenever it is the first in its process to touch the turn package.
    import daimon.core.turn.admission  # noqa: F401
    from daimon.core._models import Account
    from sqlalchemy import event
    from sqlalchemy.ext import asyncio as sa_asyncio

    foreign = sa_asyncio.create_async_engine("postgresql+asyncpg://localhost/operator_test")
    factory = sa_asyncio.async_sessionmaker
    attempts = []

    def refuse_connection(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("fixture ownership validation must precede connection")

    # `do_connect` alone misses a pooled engine's warm connection, so also trap checkout.
    for engine in (db_committing_engine, foreign):
        event.listen(engine.sync_engine, "do_connect", refuse_connection)
        event.listen(engine.sync_engine.pool, "checkout", refuse_connection)

    def injected_factory(engine, **kwargs):
        if binding == "main":
            return factory(foreign, **kwargs)
        return factory(engine, binds={Account: foreign}, **kwargs)

    monkeypatch.setattr(sa_asyncio, "async_sessionmaker", injected_factory)
    # Invoke the actual fixture body so converting refusal to pytest.skip fails this control.
    fixture = chat.__wrapped__(SimpleNamespace(param="discord"), db_preflight, db_committing_engine)
    try:
        with pytest.raises(ValueError, match="owned local"):
            await anext(fixture)
        assert not attempts
    except pytest.skip.Exception:
        pytest.fail("an unowned fixture bind must raise ValueError, never skip")
    finally:
        await fixture.aclose()
        for engine in (db_committing_engine, foreign):
            event.remove(engine.sync_engine, "do_connect", refuse_connection)
            event.remove(engine.sync_engine.pool, "checkout", refuse_connection)
        await foreign.dispose()


class Card:
    """One platform's real confirmation hook, its posted card, and its Confirm button."""

    def __init__(self, chat: Chat, monkeypatch: pytest.MonkeyPatch, admission: Any) -> None:
        self.chat = chat
        self.admins = {REQUESTER[chat.platform]}

        async def owners(_team_id: str) -> frozenset[str]:
            return frozenset(self.admins)  # Teams: the admin team's owners, read live

        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                turn=chat.runtime.settings.turn,
                teams=SimpleNamespace(admin_team_id="admin-team", tenant_id=""),
            ),
            team_owners=owners,
            group_members=GroupMembersCache(ttl_s=0),
            turn_deps=chat.turn_deps,
            sessionmaker=chat.runtime.session_factory,
            anthropic=chat.runtime.client,
            deployment_default=chat.runtime.deployment_default,
        )
        ids = {"tenant_id": chat.auth.tenant_id, "account_id": chat.auth.account_id}
        builder = getattr(self, f"_{chat.platform}")
        self.hook: Callable[[Any], Awaitable[Any]] = builder(monkeypatch, runtime, admission, ids)
        self.task: asyncio.Task[Any] | None = None

    # --- per-platform hook, post, click and tamper --------------------------

    def _discord(self, monkeypatch, runtime, admission, ids):
        import discord
        from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook

        async def requester_role(guild: object, clicker: str) -> tuple[Role, None]:
            return (Role.ADMIN if clicker in self.admins else Role.USER), None

        monkeypatch.setattr("daimon.adapters.discord.bot.requester_role", requester_role)
        self.channel = MagicMock(spec=discord.Thread)
        self.channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
        return discord_confirmation_hook(self.channel, runtime=runtime, admission=admission, **ids)

    def _slack(self, monkeypatch, runtime, admission, ids):
        from daimon.adapters.slack.tool_confirmation import SlackConfirmationCards

        async def resolve_is_admin(client: object, *, user_id: str, host_team_id: str) -> bool:
            return host_team_id == "T1" and user_id in self.admins

        monkeypatch.setattr("daimon.adapters.slack.admin.resolve_is_admin", resolve_is_admin)
        self.client = MagicMock()
        self.client.chat_postMessage = AsyncMock(return_value={"ts": "1700.1"})
        self.client.chat_update = AsyncMock()
        self.client.chat_postEphemeral = AsyncMock()
        self.cards = SlackConfirmationCards()
        return self.cards.hook(
            self.client,
            channel="C1",
            thread_ts="1699.9",
            team_id="T1",
            runtime=runtime,
            admission=admission,
            **ids,
        )

    def _teams(self, monkeypatch, runtime, admission, ids):
        from daimon.adapters.teams.tool_confirmation import TeamsConfirmationCards
        from microsoft_teams.api import SentActivity

        self.sent: list[Any] = []

        class Sender:
            async def send(self_, conversation_id: str, activity: Any, *, service_url: Any) -> Any:
                self.sent.append(activity.model_copy(deep=True))
                return SentActivity(id=activity.id or "m-1", activity_params=activity)

        self.cards = TeamsConfirmationCards(Sender())  # pyright: ignore[reportArgumentType]
        return self.cards.hook(
            conversation_id="a:conversation",
            service_url="https://smba.test",
            runtime=runtime,
            admission=admission,
            **ids,
        )

    def _posted(self) -> bool:
        if self.chat.platform == "discord":
            return bool(self.channel.send.await_count)
        if self.chat.platform == "slack":
            return bool(self.client.chat_postMessage.await_count)
        return bool(self.sent)

    async def post(self, prompt: Any) -> str:
        """Run the hook as the turn would, and return the posted card's text."""
        self.task = asyncio.create_task(self.hook(prompt))
        async with asyncio.timeout(10):
            while not self._posted():
                if self.task.done():
                    self.task.result()  # raise the refusal
                    raise AssertionError("the hook answered without posting a card")
                await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        if self.chat.platform == "discord":
            view = self.channel.send.await_args.kwargs["view"]
            return "\n".join(
                item.content for item in view.walk_children() if hasattr(item, "content")
            )
        if self.chat.platform == "slack":
            return json.dumps(self.client.chat_postMessage.await_args.kwargs["attachments"])
        return self.sent[0].model_dump_json(by_alias=True)

    async def click(self, user: str, *, deny: bool = False) -> str | None:
        """Press Confirm (or Deny) as `user`; the refusal they are shown, or None."""
        label = "Deny" if deny else "Confirm"
        if self.chat.platform == "discord":
            import discord

            view = self.channel.send.await_args.kwargs["view"]
            confirm = next(
                i
                for i in view.walk_children()
                if isinstance(i, discord.ui.Button) and i.label == label
            )
            interaction = MagicMock()
            interaction.user.id = int(user)
            interaction.response.send_message = AsyncMock()
            interaction.response.edit_message = AsyncMock()
            await confirm.callback(interaction)
            sent = interaction.response.send_message
            if not sent.await_count:
                return None
            args, kwargs = sent.await_args
            return args[0] if args else kwargs["embed"].description
        if self.chat.platform == "slack":
            before = self.client.chat_postEphemeral.await_count
            await self.cards.handle_click(
                {"actions": [{"action_id": self._slack_action(deny)}], "user": {"id": user}}
            )
            ephemeral = self.client.chat_postEphemeral
            if ephemeral.await_count == before:
                return None
            return shown_text(ephemeral.await_args.kwargs)
        from daimon.adapters.teams.tool_confirmation import VERB
        from microsoft_teams.api import AdaptiveCardInvokeActivity

        action = {
            "type": "Action.Execute",
            "verb": VERB,
            "data": {"action": VERB, "op": "deny" if deny else "approve", "token": self._token()},
        }
        activity = AdaptiveCardInvokeActivity.model_validate(
            {
                "type": "invoke",
                "name": "adaptiveCard/action",
                "id": f"invoke-{uuid.uuid4()}",
                "channelId": "msteams",
                "serviceUrl": "https://smba.test",
                "from": {"id": f"29:{user}", "aadObjectId": user, "name": "Requester"},
                "recipient": {"id": "28:bot", "name": "daimon"},
                "conversation": {
                    "id": "a:conversation",
                    "conversationType": "personal",
                    "tenantId": "entra-tenant",
                },
                "replyToId": "m-1",
                "value": {"action": action, "trigger": "manual"},
            }
        )
        response = await self.cards.on_action(SimpleNamespace(activity=activity))  # pyright: ignore[reportArgumentType]
        return None if response.value in ("Approved", "Denied") else response.value

    def _slack_action(self, deny: bool = False) -> str:
        blocks = self.client.chat_postMessage.await_args.kwargs["attachments"][0]["blocks"]
        actions = next(block for block in blocks if block["type"] == "actions")
        return actions["elements"][1 if deny else 0]["action_id"]

    def _token(self) -> str:
        if self.chat.platform == "slack":
            from daimon.core.posted_controls.confirmation import parse_confirmation_custom_id

            parsed = parse_confirmation_custom_id(self._slack_action())
            assert parsed is not None
            return parsed[0]
        found = re.search(r'"token":"([^"]+)"', self.sent[0].model_dump_json(by_alias=True))
        assert found is not None
        return found.group(1)

    def tamper(self, **reference: Any) -> None:
        """Change the posted card's switch reference after `prepare`, before any click."""

        def changed(prompt: Any) -> Any:
            update = prompt.backend_switch.model_copy(update=reference)
            return prompt.model_copy(update={"backend_switch": update})

        if self.chat.platform == "discord":
            view = self.channel.send.await_args.kwargs["view"]
            view._prompt = changed(view._prompt)  # pyright: ignore[reportPrivateUsage]
            return
        cards = self.cards._controls.cards  # pyright: ignore[reportPrivateUsage]
        token = self._token()
        cards[token] = dataclasses.replace(cards[token], prompt=changed(cards[token].prompt))

    async def answer(self) -> Any:
        assert self.task is not None
        return await asyncio.wait_for(self.task, timeout=5)

    async def still_pending(self) -> bool:
        assert self.task is not None
        await asyncio.sleep(0.05)
        return not self.task.done()

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
async def card(chat: Chat, monkeypatch: pytest.MonkeyPatch):
    posted = Card(chat, monkeypatch, chat.admission)
    try:
        yield posted
    finally:
        await posted.close()


def tool_call(target: str, preview: Any, accepted: Any = ...) -> Any:
    """The confirmed tool call exactly as the model queues it (MA asks before it runs)."""
    data: dict[str, Any] = {
        "agent_name": "Researcher",
        "target_backend": target,
        "expected_revision": preview.expected_revision,
        "preview_digest": preview.preview_digest,
    }
    if accepted is not ...:
        data["accepted_drops"] = accepted
    return data


def prompt_for(chat: Chat, data: dict[str, Any]) -> Any:
    call = ToolCall(
        tool_use_id="tu_switch",
        server_name=DAIMON_SERVER_NAME,
        tool_name="request_agent_backend_switch",
        input=data,
    )
    return prompt_for_tool_call(
        call, requester_platform_user_id=REQUESTER[chat.platform], now=datetime.now(UTC)
    )


async def apply(chat: Chat, data: dict[str, Any]) -> Any:
    """The approved call runs with the arguments the card confirmed."""
    accepted = data.get("accepted_drops")
    return await chat.tool(
        data["target_backend"],
        expected_revision=data["expected_revision"],
        preview_digest=data["preview_digest"],
        **({"accepted_drops": tuple(accepted)} if accepted is not None else {}),
    )


@pytest.mark.parametrize("target", TARGETS)
async def test_confirming_the_previewed_losses_applies_once_with_revision_and_audit(
    chat: Chat, card: Card, target: str
) -> None:
    preview = await chat.tool(target)
    assert preview.status == "preview" and preview.declared_drops == DROPS
    data = tool_call(target, preview, list(preview.declared_drops))

    text = await card.post(prompt_for(chat, data))
    from daimon.core.confirmation import drop_words
    from daimon.core.turn.notices import provider_name

    losses = f"On {provider_name(target)} it won't have {drop_words(DROPS)}"
    assert losses in text, text
    assert "Nothing is deleted; switch back any time." in text

    assert await card.click(REQUESTER[chat.platform]) is None
    assert isinstance(await card.answer(), ApprovedConfirmation)

    changed = await apply(chat, data)
    assert changed.status == "changed" and changed.declared_drops == DROPS
    _, current = await chat.current()
    assert current.local == 1 and current.backend == target
    assert current.declared_drops == DROPS
    events = await chat.audit()
    assert [event.operation for event in events] == ["set"]
    assert events[0].account_id == chat.auth.account_id
    assert "declared_drops=" in events[0].reason
    # The confirmed call cannot apply twice.
    with pytest.raises(Exception, match="Someone just switched Researcher; it's now on"):
        await apply(chat, data)
    assert len(await chat.audit()) == 1


@pytest.mark.parametrize("target", TARGETS)
async def test_an_agent_that_isnt_this_channels_target_previews_and_confirms_on_the_card(
    chat: Chat, card: Card, target: str
) -> None:
    """A copy's case: the channel answers with another agent, so both the tool's
    preview and the card's own capture read the named agent's own source, and
    agree on the digest."""
    from daimon.core.scope import ChannelScopeRef
    from daimon.core.stores.scoped_config_write import set_fields

    async with chat.runtime.session_factory.begin() as db:
        await set_fields(
            db,
            scope=ChannelScopeRef(tenant_id=chat.auth.tenant_id, channel_id="channel"),
            tenant_id=chat.auth.tenant_id,
            agent_name="someone-else",
            set_by_admin=True,
        )
    preview = await chat.tool(target)
    assert preview.status == "preview" and preview.declared_drops == DROPS
    data = tool_call(target, preview, list(preview.declared_drops))
    await card.post(prompt_for(chat, data))
    # The card recomputes the preview; a different digest would refuse the click.
    assert await card.click(REQUESTER[chat.platform]) is None
    changed = await apply(chat, data)
    assert changed.status == "changed"
    _, current = await chat.current()
    assert current.backend == target


@pytest.mark.parametrize(("target", "said"), [("gemini", "argon"), ("openai", "luna")])
async def test_a_model_said_by_name_prepares_the_card_the_tool_previewed(
    chat: Chat, card: Card, target: str, said: str
) -> None:
    """J2 smoke-2: "on Argon" reaches the tool as model="argon". The tool previews
    the resolved id, and the card must re-preview that same id: the raw word is
    no selectable model ("model not selectable", so the card could not be shown)."""
    from daimon.core.agent_model_names import resolve_switch_model

    resolved = resolve_switch_model(target, said)  # pyright: ignore[reportArgumentType]
    assert resolved is not None and resolved != said
    preview = await chat.tool(target, model=said)
    assert preview.status == "preview"
    data = {**tool_call(target, preview, list(preview.declared_drops)), "model": said}
    prompt = prompt_for(chat, data)
    assert prompt.backend_switch.model == resolved
    await card.post(prompt)
    # The click re-previews too; a different digest would refuse it.
    assert await card.click(REQUESTER[chat.platform]) is None
    changed = await chat.tool(
        target,
        model=said,
        expected_revision=data["expected_revision"],
        preview_digest=data["preview_digest"],
        accepted_drops=tuple(data["accepted_drops"]),
    )
    assert changed.status == "changed"
    _, current = await chat.current()
    assert current.backend == target


@pytest.mark.parametrize("target", TARGETS)
async def test_a_change_after_the_preview_posts_one_card_with_the_fresh_preview(
    chat: Chat, card: Card, target: str
) -> None:
    """J5 (smoke 857a82bb): "switch it to GPT and turn off web search" can preview
    the switch first, then change the agent, then confirm with the stale preview.
    The card re-previews the same request against the agent as it is now and shows
    that; the approved call (the model's original arguments) then applies it.
    Adapted from dsh's stale-preview test."""
    preview = await chat.tool(target)
    data = tool_call(target, preview, list(preview.declared_drops))
    # Web search is turned off after the preview: the agent's setup moved.
    _web_search_off(chat)
    from daimon.core.confirmation import drop_words

    fresh = await chat.tool(target)
    assert fresh.declared_drops == (*DROPS, "tools") != preview.declared_drops
    text = await card.post(prompt_for(chat, data))
    assert f"won't have {drop_words(fresh.declared_drops)}" in text, text
    assert await card.click(REQUESTER[chat.platform]) is None
    assert isinstance(await card.answer(), ApprovedConfirmation)
    changed = await apply(chat, data)
    assert changed.status == "changed" and changed.declared_drops == fresh.declared_drops
    _, current = await chat.current()
    assert current.backend == target and current.declared_drops == fresh.declared_drops
    assert len(await chat.audit()) == 1
    # The re-preview is used once: the same stale call cannot apply again.
    with pytest.raises(Exception, match="Someone just switched Researcher"):
        await apply(chat, data)


def _web_search_off(chat: Chat) -> None:
    chat.payload["tools"] = [
        *(chat.payload.get("tools") or []),
        {"type": "agent_toolset_20260401", "configs": [{"name": "web_search", "enabled": False}]},
    ]


async def test_a_re_previewed_card_applies_only_while_the_agent_is_as_it_showed(
    chat: Chat, card: Card
) -> None:
    """The agent moves again after Confirm, before the approved call runs: the card's
    refresh no longer matches, so the call refuses and nothing moves."""
    preview = await chat.tool(TARGET)
    data = tool_call(TARGET, preview, list(preview.declared_drops))
    _web_search_off(chat)
    await card.post(prompt_for(chat, data))
    assert await card.click(REQUESTER[chat.platform]) is None
    chat.payload["system"] = "Changed again after the card was confirmed."
    with pytest.raises(Exception, match="(?i)preview again"):
        await apply(chat, data)
    _, current = await chat.current()
    assert current is None and await chat.audit() == []


async def test_a_denied_re_previewed_card_never_applies(chat: Chat, card: Card) -> None:
    preview = await chat.tool(TARGET)
    data = tool_call(TARGET, preview, list(preview.declared_drops))
    _web_search_off(chat)
    await card.post(prompt_for(chat, data))
    assert await card.click(REQUESTER[chat.platform], deny=True) is None
    with pytest.raises(Exception, match="(?i)preview again"):
        await apply(chat, data)
    _, current = await chat.current()
    assert current is None and await chat.audit() == []


async def test_a_refresh_is_spent_only_once_by_its_own_confirmed_card(chat: Chat) -> None:
    """Card Y re-previewed to F2 and denied; card X re-previewed to F1 and confirmed. The
    agent is back at F2 when X's call runs: Y's refresh was never confirmed, X's shows
    F1, so nothing applies. A confirmed refresh is spent once, by its own account and
    agent only."""
    from daimon.core.confirmation import BackendSwitchReference
    from daimon.core.stores import backend_switch_cards as cards

    if chat.platform != "discord":
        pytest.skip("store-level: one platform is enough")
    now = datetime.now(UTC)
    tenant, account = chat.auth.tenant_id, chat.auth.account_id

    def fresh(digest: str) -> BackendSwitchReference:
        return BackendSwitchReference(
            agent="Researcher",
            target_backend=TARGET,
            expected_revision=0,
            preview_digest=digest,
            refreshed_from="s" * 64,
        )

    key = {"tenant_id": tenant, "account_id": account, "agent_name": "Researcher"}
    spend = {**key, "stale_digest": "s" * 64, "fresh_revision": 0, "now": now}
    async with chat.runtime.session_factory.begin() as db:
        for digest in ("2" * 64, "1" * 64):  # Y, then X
            await cards.record_refresh(
                db,
                tenant_id=tenant,
                account_id=account,
                platform="discord",
                channel_id="channel",
                thread_id="thread",
                requester_platform_user_id=REQUESTER["discord"],
                agent_name="Researcher",
                stale_digest="s" * 64,
                fresh=fresh(digest),
                expires_at=now + timedelta(minutes=10),
            )
        # Only X is confirmed.
        assert await cards.approve_refresh(db, **spend, fresh_digest="1" * 64)
    async with chat.runtime.session_factory.begin() as db:
        assert not await cards.consume_refresh(db, **spend, fresh_digest="2" * 64)
        other_account = {**spend, "account_id": uuid.uuid4()}
        assert not await cards.consume_refresh(db, **other_account, fresh_digest="1" * 64)
        other_agent = {**spend, "agent_name": "someone-else"}
        assert not await cards.consume_refresh(db, **other_agent, fresh_digest="1" * 64)
        assert await cards.consume_refresh(db, **spend, fresh_digest="1" * 64)
        assert not await cards.consume_refresh(db, **spend, fresh_digest="1" * 64)


async def test_a_stale_preview_whose_re_preview_is_refused_asks_to_preview_again(
    chat: Chat, card: Card, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback (dsh's typed refusal): when the agent changed and the switch is
    now refused (the provider was turned off), the model is told to preview again."""
    from daimon.core.confirmation import STALE_SWITCH_TEXT, StalePreview

    preview = await chat.tool(TARGET)
    data = tool_call(TARGET, preview, list(preview.declared_drops))
    chat.payload["system"] = "Changed after the switch preview was taken."
    monkeypatch.setattr("daimon.core.channel_backend.backend_is_enabled", lambda *_: False)
    with pytest.raises(StalePreview, match="Preview the switch again") as refused:
        await card.post(prompt_for(chat, data))
    assert str(refused.value) == STALE_SWITCH_TEXT
    assert not card._posted()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Exception, match="(?i)isn't turned on"):
        await apply(chat, data)
    _, current = await chat.current()
    assert current is None and await chat.audit() == []


@pytest.mark.parametrize("accepted", ["missing", "partial", "extra", "empty"])
async def test_accepted_drops_other_than_the_preview_never_post_or_apply(
    chat: Chat, card: Card, accepted: str
) -> None:
    target = TARGET
    preview = await chat.tool(target)
    assert preview.declared_drops == DROPS
    drops = {
        "missing": ...,
        "partial": list(DROPS[:1]),
        "extra": [*DROPS, "files"],
        "empty": [],
    }[accepted]
    data = tool_call(target, preview, drops)
    with pytest.raises(ValueError, match="Preview and confirm again"):
        await card.post(prompt_for(chat, data))
    assert not card._posted()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Exception, match="(?i)preview again"):
        await apply(chat, data)
    _, current = await chat.current()
    assert current is None and await chat.audit() == []


@pytest.mark.parametrize("change", ["drop_list", "digest", "source"])
async def test_a_card_changed_after_prepare_is_refused_on_click(
    chat: Chat, card: Card, change: str
) -> None:
    target = TARGET
    preview = await chat.tool(target)
    data = tool_call(target, preview, list(preview.declared_drops))
    assert "it won't have" in await card.post(prompt_for(chat, data))
    if change == "drop_list":
        card.tamper(accepted_drops=DROPS[:1])
    elif change == "digest":
        card.tamper(preview_digest="0" * 64)
    else:
        # The agent itself changed after the card was drawn: a fresh preview differs.
        chat.payload["system"] = "Changed after the card was drawn."
    refusal = await card.click(REQUESTER[chat.platform])
    assert refusal is not None and re.search(
        r"(?i)preview (the switch )?(and confirm )?again", refusal
    )
    assert await card.still_pending()
    if change == "source":
        with pytest.raises(Exception, match="(?i)preview again"):
            await apply(chat, data)
    _, current = await chat.current()
    assert current is None and await chat.audit() == []


@pytest.mark.parametrize("actor", ["another_user", "demoted_requester"])
async def test_only_the_requesting_current_admin_can_confirm(
    chat: Chat, card: Card, actor: str
) -> None:
    target = TARGET
    preview = await chat.tool(target)
    data = tool_call(target, preview, list(preview.declared_drops))
    await card.post(prompt_for(chat, data))
    if actor == "another_user":
        card.admins.add(STRANGER[chat.platform])
        refusal = await card.click(STRANGER[chat.platform])
    else:
        card.admins.clear()
        refusal = await card.click(REQUESTER[chat.platform])
    assert refusal is not None
    assert await card.still_pending()
    _, current = await chat.current()
    assert current is None and await chat.audit() == []


#: A private conversation id on each platform (`is_private_conversation`).
PRIVATE_CHANNEL = {"discord": "dm:123", "slack": "D123", "teams": "a:123"}


async def _assert_admission_refused(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, admission: str, reason: str
) -> None:
    from daimon.core.channel_backend import AgentBackendRefused

    own = chat.admission
    grant = own.grant
    private = PRIVATE_CHANNEL[chat.platform]
    replaced = {
        "none": None,
        "no_grant": dataclasses.replace(own, grant=None),
        "another_account": dataclasses.replace(own, account_id=uuid.uuid4()),
        "dm": dataclasses.replace(own, grant=dataclasses.replace(grant, is_dm=True)),
        "private_dm": dataclasses.replace(own, private_dm_id=private),
        "private_slack_context": dataclasses.replace(own, slack_turn_context_id=uuid.uuid4()),
        "private_conversation": dataclasses.replace(own, origin_channel_id=private),
        "private_thread": dataclasses.replace(own, origin_thread_id=private),
        "no_channel": dataclasses.replace(own, origin_channel_id=None),
    }[admission]
    preview = await chat.tool(TARGET)
    data = tool_call(TARGET, preview, list(preview.declared_drops))
    card = Card(chat, monkeypatch, replaced)
    try:
        with pytest.raises(AgentBackendRefused) as refused:
            await card.post(prompt_for(chat, data))
        assert refused.value.reason == reason
        assert not card._posted()  # pyright: ignore[reportPrivateUsage]
    finally:
        await card.close()


#: These seven refusals read fields of the admission itself (`capture_admitted_backend_source`
#: in `core/turn/admission.py`): a missing admission, grant or channel, another account, a DM
#: grant, a private-DM id or a Slack turn context. Each platform's hook hands the admission to
#: that one shared function unchanged, so one platform stands for all three.
@pytest.mark.parametrize("chat", ["discord"], indirect=True)
@pytest.mark.parametrize(
    ("admission", "reason"),
    [
        ("none", "source_unavailable"),
        ("no_grant", "source_unavailable"),
        ("another_account", "source_unavailable"),
        ("dm", "source_context_unavailable"),
        ("private_dm", "source_context_unavailable"),
        ("private_slack_context", "source_context_unavailable"),
        ("no_channel", "source_context_unavailable"),
    ],
)
async def test_only_the_turns_own_public_admission_can_prepare_a_card(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, admission: str, reason: str
) -> None:
    await _assert_admission_refused(chat, monkeypatch, admission, reason)


#: These two depend on the platform's id shape (`is_private_conversation`: a `dm:` scope, a
#: Slack `D` id, a Teams `a:` id), so they run on every platform.
@pytest.mark.parametrize("admission", ["private_conversation", "private_thread"])
async def test_a_private_conversation_or_thread_cannot_prepare_a_card(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, admission: str
) -> None:
    await _assert_admission_refused(chat, monkeypatch, admission, "source_context_unavailable")


@pytest.mark.parametrize("tenant", ["own", "another"])
async def test_capture_reads_only_the_admitted_tenants_source(chat: Chat, tenant: str) -> None:
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.turn.admission import capture_admitted_backend_source

    source, _ = await chat.current()
    capture = capture_admitted_backend_source(
        chat.turn_deps,
        source,
        admission=chat.admission,
        tenant_id=chat.auth.tenant_id if tenant == "own" else uuid.uuid4(),
        account_id=chat.auth.account_id,
        platform=chat.platform,
    )
    if tenant == "own":
        captured = await capture
        assert (captured.tenant_id, captured.agent_id) == (source.tenant_id, source.agent_id)
        assert captured.source is not None and captured.environment is not None
    else:
        with pytest.raises(AgentBackendRefused) as refused:
            await capture
        assert refused.value.reason == "source_unavailable"
