"""A Slack turn from a fresh install: the OAuth install route, then the real Socket Mode app."""

from __future__ import annotations

import time
from typing import Any

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.mcp.oauth_slack import build_oauth_slack_routes
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.config import CryptoSettings, SlackSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.slack_oauth import mint_state
from daimon.testing.sim.script import Tool, Turn
from daimon.testing.sim.slack import SlackAPI, aiohttp_routed
from daimon.testing.sim.tasks import settled
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.requests import Request

from .world import World, metering, switch_to, world

TEAM, CHANNEL, PERSON = "T_SIM", "C_SIM", "U_PERSON"
SLACK = SlackSettings(
    signing_secret=SecretStr("sim-signing"),
    app_token=SecretStr("xapp-sim"),
    client_id="sim.client",
    client_secret=SecretStr("sim"),
)


class _Socket:
    """The Socket Mode connection: it only carries acks back to Slack."""

    def __init__(self) -> None:
        self.acks: list[SocketModeResponse] = []

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.acks.append(response)


async def _install(w: World) -> None:
    """The workspace admin approves the app; Slack redirects to the install callback."""
    fernet = build_multifernet(tuple(k.get_secret_value() for k in w.settings.crypto.keys))
    _, callback, _ = build_oauth_slack_routes(
        sessionmaker=w.sessions, settings=w.settings, fernet=fernet
    )
    state = mint_state(signing_secret="sim-signing", now=time.time())
    query = f"state={state}&code=sim-code".encode()
    scope: dict[str, Any] = {"type": "http", "method": "GET", "query_string": query, "headers": []}
    page = await callback(Request(scope))
    assert page.status_code == 200, f"the install succeeds: {page.body!r}"


def _mention(text: str, ts: str, thread_ts: str | None) -> SocketModeRequest:
    event = {"type": "app_mention", "channel": CHANNEL, "user": PERSON, "ts": ts, "event_ts": ts}
    event |= {"text": f"<@U_BOT> {text}", "team": TEAM}
    if thread_ts:
        event["thread_ts"] = thread_ts
    payload = {"team_id": TEAM, "event": event}
    return SocketModeRequest(type="events_api", envelope_id=f"env-{ts}", payload=payload)


async def _say(app: SlackApp, slack: SlackAPI, text: str, *, thread: str | None = None) -> str:
    """Mention the bot over Socket Mode; return the reply that replaced the progress card."""
    start, socket = len(slack.posts), _Socket()
    async with settled():
        await app.on_request(socket, _mention(text, slack.next_ts(), thread))  # pyright: ignore[reportArgumentType]
    assert socket.acks, "the event is acked first"
    shown = slack.texts(start)
    assert shown[0].startswith("Working on it"), f"the turn shows progress first: {shown}"
    card, (method, edit) = slack.posts[start][1], slack.posts[-1]
    assert method == "chat.update" and edit["ts"] == card["ts"], (
        f"the reply replaces the progress card: {slack.posts[start:]}"
    )
    return shown[-1]


async def test_a_fresh_install_answers_then_moves_to_openai(
    db_preflight: None, db_nullpool_engine: AsyncEngine
) -> None:
    """Install, a mention seeds the defaults and answers, then the thread goes on on OpenAI."""
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    crypto = CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),))
    async with (
        world(sessions, slack=SLACK, crypto=crypto) as w,
        aiohttp_routed(w.network),
        httpx.AsyncClient() as http,
    ):
        slack = SlackAPI(team=TEAM)
        w.network.fakes[slack.host] = slack
        await _install(w)
        assert "Daimon is ready" in slack.texts()[0], f"the installer is welcomed: {slack.texts()}"
        runtime = SlackRuntime(
            settings=w.settings,
            anthropic=w.anthropic,
            sessionmaker=w.sessions,
            billing_config=None,
            http_client=http,
            resolver_cache=w.deps.resolver_cache,
            turn_deps=w.deps,
            deployment_default=w.deps.deployment_default,
        )
        app = SlackApp(runtime=runtime)
        app.start_orphan_recovery()
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM)
        tool = Tool("discord_search_messages", {"query": "launch notes"}, server="daimon-mcp")
        w.script.add(Turn("Found the launch notes.", tool, input_tokens=1500, output_tokens=90))
        w.script.add(Turn("Same notes, from OpenAI.", tool, input_tokens=1700, output_tokens=40))

        first = await _say(app, slack, "find the launch notes")
        assert "Found the launch notes." in first and "used" in first, f"reply and cost: {first}"
        metered, billed = await metering(w, tenant_id, "anthropic")
        assert metered == billed, f"a turn is metered as it ends: {metered} != {billed}"

        await switch_to(w, tenant_id, "openai", platform="slack", user_id=PERSON)
        second = await _say(app, slack, "again please", thread=slack.posts[1][1]["thread_ts"])
        assert "Same notes, from OpenAI." in second and w.openai.reported, f"on OpenAI: {second}"
        metered, billed = await metering(w, tenant_id, "openai")
        assert metered == billed, f"one usage row per model call: {metered} != {billed}"
        w.network.assert_clean()
