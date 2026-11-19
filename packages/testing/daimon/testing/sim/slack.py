"""Slack's Web API, simulated: one workspace, its install exchange and the calls a turn makes.

slack_sdk talks to Slack over aiohttp, not httpx: `aiohttp_routed` sends aiohttp
requests for a fake's host through a loopback server to that fake, and refuses
every other host, as the simulated network does for httpx.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import parse_qsl

import aiohttp
import httpx
from aiohttp import web
from daimon.testing.sim.network import Handler, ProviderNetwork, StrictFake
from multidict import CIMultiDict
from yarl import URL

HOST = "slack.com"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_SIM_HOST = "x-sim-host"
type Args = dict[str, Any]


def _args(request: httpx.Request) -> Args:
    """The call's arguments, wherever slack_sdk put them: query, form or JSON."""
    args: Args = dict(request.url.params)
    kind = request.headers.get("content-type", "")
    if "json" in kind:
        args |= json.loads(request.content or b"{}")
    elif "form" in kind:
        args |= dict(parse_qsl(request.content.decode()))
    return args


def _words(blocks: Any) -> list[str]:  # noqa: ANN401  # Block Kit JSON
    """Every text a person reads in Block Kit, in order: sections, context, buttons."""
    if isinstance(blocks, list):
        return [w for b in cast("list[Any]", blocks) for w in _words(b)]
    if not isinstance(blocks, dict):
        return []
    found: list[str] = []
    for key, value in cast("dict[str, Any]", blocks).items():
        if key == "text" and isinstance(value, str):
            found.append(value)
        elif key not in ("value", "action_id", "block_id", "url"):
            found.extend(_words(value))
    return found


class SlackAPI(StrictFake):
    """One workspace `team`: the bot is `bot`, every person a plain member."""

    def __init__(self, *, team: str, bot: str = "U_BOT") -> None:
        super().__init__(host=HOST)
        self.team, self.bot = team, bot
        #: Each post, edit and ephemeral as (method, arguments with the message's `ts`).
        self.posts: list[tuple[str, Args]] = []
        self._ts = 1_700_000_000
        methods: dict[str, Callable[[Args], Args]] = {
            "oauth.v2.access": self._install,
            "auth.test": lambda a: {"user_id": bot, "team_id": team, "team": "Sim"},
            "users.info": self._user,
            "conversations.info": lambda a: {
                "channel": {"id": a["channel"], "is_ext_shared": False}
            },
            "conversations.replies": lambda a: {"messages": [], "has_more": False},
            "conversations.history": lambda a: {"messages": [], "has_more": False},
            "chat.postMessage": self._post("chat.postMessage"),
            "chat.update": self._post("chat.update"),
            "chat.postEphemeral": self._post("chat.postEphemeral"),
            "reactions.add": lambda a: {},
            "reactions.remove": lambda a: {},
        }
        for name, answer in methods.items():
            for verb in ("GET", "POST"):
                self.route(verb, f"/api/{re.escape(name)}", self._answer(answer))

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        return httpx.Response(status, json={"ok": False, "error": message})

    @staticmethod
    def _answer(answer: Callable[[Args], Args]) -> Handler:
        return lambda request, _: httpx.Response(200, json={"ok": True, **answer(_args(request))})

    def _install(self, args: Args) -> Args:
        assert args.get("code"), "the install exchange carries the code Slack redirected with"
        return {
            "access_token": "xoxb-sim",
            "token_type": "bot",
            "scope": "app_mentions:read,chat:write",
            "bot_user_id": self.bot,
            "app_id": "A_SIM",
            "team": {"id": self.team, "name": "Sim"},
            "enterprise": None,
            "is_enterprise_install": False,
            "authed_user": {"id": "U_INSTALLER"},
        }

    def _user(self, args: Args) -> Args:
        user = {"id": args["user"], "team_id": self.team, "name": "person", "deleted": False}
        flags = ("is_bot", "is_admin", "is_owner", "is_primary_owner", "is_restricted")
        return {"user": user | dict.fromkeys(flags, False) | {"is_ultra_restricted": False}}

    def _post(self, method: str) -> Callable[[Args], Args]:
        def post(args: Args) -> Args:
            ts = args.get("ts") or self.next_ts()
            self.posts.append((method, args | {"ts": ts}))
            if method == "chat.postEphemeral":
                return {"message_ts": ts}
            return {"ts": ts, "channel": args["channel"], "message": {"ts": ts}}

        return post

    def next_ts(self) -> str:
        """A fresh message timestamp, unique in the workspace."""
        self._ts += 1
        return f"{self._ts}.000100"

    def texts(self, start: int = 0) -> list[str]:
        """What the person sees, post by post: the text, else the blocks' words."""
        shown: list[str] = []
        for _, args in self.posts[start:]:
            blocks = args.get("blocks")
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            shown.append("\n".join([args.get("text") or "", *_words(blocks)]).strip())
        return shown


@asynccontextmanager
async def aiohttp_routed(network: ProviderNetwork) -> AsyncIterator[None]:
    """aiohttp requests reach the network's fakes through a loopback relay; others are refused."""

    async def relay(request: web.Request) -> web.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() != _SIM_HOST}
        url = request.url.with_scheme("https").with_host(request.headers[_SIM_HOST]).with_port(None)
        forwarded = httpx.Request(
            request.method, str(url), headers=headers, content=await request.read()
        )
        answer = await network.fakes[url.host or ""].handle(forwarded)
        kind = answer.headers.get("content-type", "application/json")
        return web.Response(
            status=answer.status_code, body=answer.content, headers={"content-type": kind}
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", relay)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port: int = runner.addresses[0][1]
    real = aiohttp.ClientSession._request  # pyright: ignore[reportPrivateUsage]

    async def routed(
        session: aiohttp.ClientSession,
        method: str,
        str_or_url: str | URL,
        **kwargs: Any,  # noqa: ANN401
    ) -> aiohttp.ClientResponse:
        url = URL(str(str_or_url))
        if url.host in network.fakes:
            headers = CIMultiDict[str](kwargs.pop("headers", None) or {})
            headers[_SIM_HOST] = url.host
            local = url.with_scheme("http").with_host("127.0.0.1").with_port(port)
            return await real(session, method, local, headers=headers, **kwargs)
        if url.host not in _LOOPBACK:
            network.refused.append(f"{method} {url.host}{url.path} (aiohttp)")
            raise aiohttp.ClientConnectionError(f"simulated network: no route to {url.host}")
        return await real(session, method, str_or_url, **kwargs)

    aiohttp.ClientSession._request = routed  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        aiohttp.ClientSession._request = real  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        await runner.cleanup()
