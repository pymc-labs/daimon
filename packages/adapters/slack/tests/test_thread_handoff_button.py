"""The Hand over button on a Slack thread whose channel now answers with another agent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack import thread_handoff
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.thread_handoff import HAND_OVER_ACTION_ID, handle_hand_over_click
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from yarl import URL

_TEAM = "T_HANDOVER"
_CHANNEL = "C_PARENT"
_THREAD_TS = "1700000000.000100"
_DEFAULT = DeploymentDefault(agent_name="daimon")


def _payload() -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": "U_CLICKER", "team_id": _TEAM},
        "channel": {"id": _CHANNEL},
        "container": {"channel_id": _CHANNEL, "message_ts": "1700000000.000200"},
        "message": {"ts": "1700000000.000200", "thread_ts": _THREAD_TS, "text": "notice"},
        "actions": [{"action_id": HAND_OVER_ACTION_ID, "value": "ag_research"}],
    }


async def _click(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: TenantAccessPolicy | None = None,
) -> Any:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=_CHANNEL),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant.id))
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        deployment_default=_DEFAULT,
    )

    async def _client(_runtime: Any, *, team_id: str) -> Any:
        assert team_id == _TEAM
        return fake_slack_web_client.client

    monkeypatch.setattr(thread_handoff, "resolve_web_client", _client)
    tasks: list[Any] = []
    app = SimpleNamespace(_spawn=tasks.append, _handle_app_mention=AsyncMock())
    await handle_hand_over_click(cast(SlackRuntime, runtime), _payload(), app=cast(SlackApp, app))
    await asyncio.gather(*tasks)
    return SimpleNamespace(id=tenant.id, ingress=app._handle_app_mention)


def _sent(fake_slack_web_client: Any, method: str) -> list[dict[str, Any]]:
    return [
        req.kwargs["json"]
        for (_m, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL(f"https://slack.com/api/{method}")
        for req in reqs
    ]


async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _click(db_session, db_session_factory, fake_slack_web_client, monkeypatch)

    (posted,) = _sent(fake_slack_web_client, "chat.postMessage")
    assert posted["thread_ts"] == _THREAD_TS
    assert "Your latest message is being sent to research-bot" in posted["text"]
    (updated,) = _sent(fake_slack_web_client, "chat.update")
    assert updated["blocks"] == [], "the notice loses its button"
    async with db_session_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id, channel_id=_CHANNEL, thread_id=_THREAD_TS, platform="slack"
            ),
            default=_DEFAULT,
        )
    assert routed.responder_ma_agent_id == "ag_research", "later messages go to research-bot"
    assert routed.thread_binding_kind == "handoff"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _click(
        db_session,
        db_session_factory,
        fake_slack_web_client,
        monkeypatch,
        policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
    )

    assert _sent(fake_slack_web_client, "chat.postMessage") == []
    (ephemeral,) = _sent(fake_slack_web_client, "chat.postEphemeral")
    assert ephemeral["user"] == "U_CLICKER"
    assert "Nothing was changed" in ephemeral["text"]
    async with db_session_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id, channel_id=_CHANNEL, thread_id=_THREAD_TS, platform="slack"
            ),
            default=_DEFAULT,
        )
    assert routed.thread_binding_id is None

    tenant.ingress.assert_not_awaited()


@pytest.mark.parametrize("busy,later", [(False, False), (True, False), (False, True), (True, True)])
async def test_click_and_following_mention_use_normal_ingress_in_order(
    busy: bool,
    later: bool,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re
    from dataclasses import replace
    from decimal import Decimal
    from unittest.mock import patch

    from aioresponses import CallbackResult
    from cryptography.fernet import Fernet
    from daimon.adapters.slack import app as app_module
    from daimon.core.github_credentials import build_multifernet, encrypt_token
    from daimon.core.stores import tenant_ledger
    from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
    from daimon.core.stores.task_continuations import list_pending_continuations
    from daimon.core.turn.run import RunOutcome
    from daimon.core.turn.state import TurnState
    from daimon.testing.ma import resolved_agent_env_router
    from daimon.testing.ma_models import ma_environment
    from daimon.testing.turn_fakes import deps_for
    from mux.testing.drivers.anthropic.ma_models import ma_session

    from .harness import make_orchestrate_app

    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=_CHANNEL),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100"),
        reason="trial",
        idempotency_key="busy-handover-credit",
    )
    await db_session.commit()
    agent = ma_agent(id="ag_research", name="research-bot", tenant_id=tenant.id)
    environment = ma_environment(id="env_handover", name="default", tenant_id=tenant.id)
    client = build_fake_anthropic(resolved_agent_env_router(agent, environment).dispatch)
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    key = Fernet.generate_key().decode()
    await upsert_slack_bot_token(
        db_session,
        team_id=_TEAM,
        encrypted_token=encrypt_token(build_multifernet((key,)), "xoxb-test"),
    )
    await db_session.commit()
    app, _ = make_orchestrate_app(db_session_factory, deployment_default=default, crypto_key=key)
    deps = replace(deps_for(client, db_session_factory), turn_path=None, deployment_default=default)
    app.runtime = replace(app.runtime, anthropic=client, turn_deps=deps)
    app.runtime.settings.agent_identity.enabled = False

    async def web_client(*args: Any, **kwargs: Any) -> Any:
        return fake_slack_web_client.client

    monkeypatch.setattr(thread_handoff, "resolve_web_client", web_client)

    fake_slack_web_client.mock.clear()

    def post(url: URL, **kwargs: Any) -> CallbackResult:
        if url.path.endswith("auth.test"):
            return CallbackResult(payload={"ok": True, "user_id": "U_BOT"})
        return CallbackResult(payload={"ok": True, "channel": _CHANNEL, "ts": "1700000000.000300"})

    fake_slack_web_client.mock.post(
        re.compile(r"https://slack.com/api/.*"), callback=post, repeat=True
    )
    fake_slack_web_client.mock.get(
        re.compile(r"https://slack.com/api/users.info.*"),
        payload={
            "ok": True,
            "user": {
                "id": "U_CLICKER",
                "name": "Clicker",
                "team_id": _TEAM,
                "deleted": False,
                "is_bot": False,
                "is_admin": False,
            },
        },
        repeat=True,
    )
    fake_slack_web_client.mock.get(
        re.compile(r"https://slack.com/api/conversations.replies.*"),
        payload={
            "ok": True,
            "messages": [
                {"user": "U_CLICKER", "text": "Please finish the analysis.", "ts": _THREAD_TS},
                {"user": "U_OTHER", "text": "Unrelated words", "ts": "1700000000.000150"},
            ],
            "has_more": False,
        },
        repeat=True,
    )
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []
    real_turn = app._run_thread_turn

    async def turn(event: dict[str, Any], **kwargs: Any) -> None:
        if event.get("_handover_agent_id"):
            await real_turn(event, **kwargs)
        else:
            order.append(event["text"])
            if "running turn" in event["text"]:
                started.set()
                await release.wait()

    def human(text: str, ts: str) -> dict[str, Any]:
        return {
            "type": "app_mention",
            "user": "U_CLICKER",
            "user_team": _TEAM,
            "channel": _CHANNEL,
            "thread_ts": _THREAD_TS,
            "ts": ts,
            "event_ts": ts,
            "text": text,
        }

    async def create(*args: Any, **kwargs: Any) -> Any:
        await kwargs["before_create"]()
        return ma_session(id="session_handover", agent=agent, environment_id=environment.id)

    async def context(*args: Any, **kwargs: Any) -> str:
        return kwargs["user_query"]

    async def answer(deps: Any, prepared: Any, **kwargs: Any) -> Any:
        assert prepared.admission.agent.id == "ag_research"
        assert "Please finish the analysis." in kwargs["user_message"]
        order.append("button")
        if not busy and later:
            started.set()
            await release.wait()
        await kwargs["lifecycle"].on_terminal_success(TurnState())
        return RunOutcome(
            state=TurnState(),
            ma_session_id=prepared.ma_session_id,
            mapping_id=prepared.mapping_id,
            recovered=False,
        )

    thread_key = (_TEAM, _CHANNEL, _THREAD_TS)
    with (
        patch.object(app, "_run_thread_turn", side_effect=turn),
        patch("daimon.core.turn.prepare.create_session", side_effect=create),
        patch.object(app_module, "build_context_xml", side_effect=context),
        patch.object(app_module, "admit", wraps=app_module.admit) as admitted,
        patch.object(app_module, "bind_session", wraps=app_module.bind_session) as bound,
        patch.object(app_module, "run_prepared_turn", side_effect=answer) as ran,
    ):
        owner = None
        if busy:
            owner = asyncio.create_task(
                app._handle_app_mention(
                    human("<@U_BOT> running turn", "1700000000.000001"), team_id=_TEAM
                )
            )
            await asyncio.wait_for(started.wait(), 20)
        await handle_hand_over_click(app.runtime, _payload(), app=app)
        await handle_hand_over_click(app.runtime, _payload(), app=app)
        if busy:
            async with asyncio.timeout(20):
                while not app._pending.get(thread_key):
                    await asyncio.sleep(0.01)
            assert any(
                "hourglass_flowing_sand" in str(url) or "hourglass_flowing_sand" in str(req.kwargs)
                for (_, url), reqs in fake_slack_web_client.mock.requests.items()
                for req in reqs
            )
            ran.assert_not_awaited()
        elif later:
            await asyncio.wait_for(started.wait(), 20)
        if later:
            await app._handle_app_mention(
                human("<@U_BOT> Later request.", "1900000000.000001"), team_id=_TEAM
            )
        release.set()
        if owner is not None:
            await owner
        async with asyncio.timeout(30):
            while app._bg_tasks:
                await asyncio.gather(*list(app._bg_tasks))
                await asyncio.sleep(0)
    assert order == (["<@U_BOT> running turn"] if busy else []) + ["button"] + (
        ["<@U_BOT> Later request."] if later else []
    )
    admitted.assert_awaited_once()
    assert admitted.await_args.kwargs["external_user_id"] == "U_CLICKER"
    bound.assert_awaited_once()
    ran.assert_awaited_once()
    assert thread_key not in app._processing
    assert not app._pending.get(thread_key)
    async with db_session_factory() as session:
        assert (
            await list_pending_continuations(
                session, tenant_id=tenant.id, platform="slack", thread_id=_THREAD_TS
            )
            == []
        )
