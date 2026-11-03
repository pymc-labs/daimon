"""The registered hub wrappers, over the wire, under the unattended tool gate.

A hub read reports a turn paused on confirmations and answers nothing; the
owner's hub `continue_turn` resumes an agent-chat turn under the current
policy; an attended card is never resumed from the hub.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _agent_chat_safety as safety
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.defaults.loader import DeploymentDefault
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import session_settle_state
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing import AGENT_ID, build_turn_router, ma_agent
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.testing.drivers.anthropic.ma import (
    build_fake_anthropic,
    json_body,
    ma_session,
    send_events_response,
)
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_TOKEN = "tok"
_SESSION = "ses_hub_safety"


class _Paused:
    """A session whose every turn pauses on `linear/create_issue`.

    It starts paused on turn `evt_user` (unless `fresh`); a message starts
    another such turn, and a confirmation ends the turn with a reply. Its
    frozen config allows; `updates` records every tool rewrite.
    """

    def __init__(self, account: str, *, fresh: bool = False) -> None:
        self.account = account
        self.policy = "always_allow"
        self.events: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        if not fresh:
            self._turn("evt_user")

    def _turn(self, boundary: str) -> None:
        n = len(self.events)
        self.events += [
            {"id": boundary, "type": "user.message", "content": [{"type": "text", "text": "x"}]},
            {
                "id": f"evt_tool_{n}",
                "type": "agent.mcp_tool_use",
                "mcp_server_name": "linear",
                "name": "create_issue",
                "input": {},
                "evaluated_permission": "ask",
            },
            {
                "id": f"evt_pause_{n}",
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": [f"evt_tool_{n}"]},
            },
        ]

    def model(self) -> dict[str, Any]:
        return ma_session(
            id=_SESSION,
            agent=ma_agent(
                id=AGENT_ID,
                tools=[
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "linear",
                        "configs": [],
                        "default_config": {
                            "enabled": True,
                            "permission_policy": {"type": self.policy},
                        },
                    }
                ],
                mcp_servers=[{"name": "linear", "type": "url", "url": "https://l.example/mcp"}],
            ),
            status="idle",
            metadata={"daimon_agent_chat": "true", MA_METADATA_KEY_ACCOUNT: self.account},
        ).model_dump(mode="json")

    def route(self, router: MARouter) -> None:
        router.add(
            "GET",
            rf"/v1/sessions/{_SESSION}$",
            lambda _r, _m: httpx.Response(200, json=self.model()),
        )
        router.add("POST", rf"/v1/sessions/{_SESSION}$", self._update)
        router.add("GET", rf"/v1/sessions/{_SESSION}/events$", self._list)
        router.add("POST", rf"/v1/sessions/{_SESSION}/events$", self._send)

    def _list(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        events = list(self.events)
        types = request.url.params.get_list("types[]")
        if types:
            events = [e for e in events if e["type"] in types]
        if request.url.params.get("order") == "desc":
            events.reverse()
        limit = int(request.url.params.get("limit") or 100)
        return httpx.Response(200, json={"data": events[:limit], "next_page": None})

    def _update(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        agent = json_body(request)["agent"]
        self.updates.append(agent)
        self.policy = agent["tools"][0]["default_config"]["permission_policy"]["type"]
        return httpx.Response(200, json=self.model())

    def _send(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        echoed: list[dict[str, Any]] = []
        for event in json_body(request)["events"]:
            self.sent.append(event)
            if event["type"] == "user.message":
                boundary = f"evt_message_{len(self.sent)}"
                self._turn(boundary)
                echoed.append({**event, "id": boundary})
            else:
                n = len(self.events)
                self.events += [
                    {**event, "id": f"evt_sent_{n}"},
                    {
                        "id": f"evt_reply_{n}",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "done"}],
                    },
                    {
                        "id": f"evt_end_{n}",
                        "type": "session.status_idle",
                        "stop_reason": {"type": "end_turn"},
                    },
                ]
        return send_events_response(data=echoed)


async def _hub(
    db_session: AsyncSession,
    engine: AsyncEngine,
    *,
    owner_is_caller: bool,
    recorded: bool,
    fresh: bool = False,
) -> tuple[Any, str, _Paused, async_sessionmaker[AsyncSession]]:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if recorded:
        async with factory.begin() as db:
            await session_settle_state.record_unattended_turn(
                db, managed_session_id=_SESSION, turn_event_id="evt_user", rules_channel_id=None
            )
    paused = _Paused(
        str(principal.account_id) if owner_is_caller else str(uuid.uuid4()), fresh=fresh
    )
    router = MARouter()
    paused.route(router)
    build_turn_router(str(tenant.id), router=router)
    runtime = McpRuntime(
        session_factory=factory,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=None, jwt_secret=None),
            hub=HubSettings(),
            tool_safety=ToolSafetyPolicy(enabled=True),
        ),
        deployment_default=DeploymentDefault(agent_name="test-agent", environment_name="test-env"),
    )
    hub_tenant = HubTenant(
        tenant_id=tenant.id, account_id=principal.account_id, workspace_id="g1", workspace_name="W"
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[hub_tenant])
    auth = StaticTokenVerifier(
        tokens={_TOKEN: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)
    daimon_id = str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID))
    return (
        mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
        daimon_id,
        paused,
        factory,
    )


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("result", result)
    assert isinstance(payload, dict)
    return payload


async def _drain() -> None:
    workers = [w.task for w in safety._workers.values()]  # pyright: ignore[reportPrivateUsage]
    if workers:
        await asyncio.wait_for(asyncio.gather(*workers), timeout=5)


async def test_hub_get_session_reports_the_pause_and_answers_nothing(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=True, recorded=True
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="get_session",
            arguments={"daimon_id": daimon_id, "handle": _SESSION},
        )
    )
    info = result.get("structuredContent") or {}
    assert (info.get("status"), info.get("hint")) == ("paused", safety.PAUSED_HINT), result
    await asyncio.sleep(0.05)
    assert paused.sent == []
    assert not safety._workers  # pyright: ignore[reportPrivateUsage]


async def test_hub_continue_turn_resumes_the_owners_agent_chat_turn(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_POLL_S", 0.01)
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=True, recorded=True
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="continue_turn",
            arguments={"daimon_id": daimon_id, "handle": _SESSION},
        )
    )
    assert not result.get("isError"), result
    await _drain()
    assert [(e["type"], e.get("result")) for e in paused.sent] == [
        ("user.tool_confirmation", "deny")
    ]


@pytest.mark.parametrize(
    ("owner_is_caller", "recorded", "refusal"),
    [(False, True, "not found"), (True, False, "approval in the chat")],
    ids=["another-account", "attended-card"],
)
async def test_hub_continue_turn_never_answers_what_it_may_not(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    owner_is_caller: bool,
    recorded: bool,
    refusal: str,
) -> None:
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=owner_is_caller, recorded=recorded
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="continue_turn",
            arguments={"daimon_id": daimon_id, "handle": _SESSION, "message": "go on"},
        )
    )
    assert result.get("isError") and refusal in str(result.get("content")), result
    await asyncio.sleep(0.05)
    assert paused.sent == []


async def test_hub_start_turn_gates_the_new_session_and_refuses_its_write(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered wrapper, admission included; creation's own fakes are core's."""
    from unittest.mock import AsyncMock

    from daimon.adapters.mcp.tools import agent_chat

    monkeypatch.setattr(safety, "_POLL_S", 0.01)
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=True, recorded=False, fresh=True
    )
    created = AsyncMock(side_effect=lambda *_a, **_k: _session_obj(paused))
    monkeypatch.setattr(agent_chat, "create_session", created)
    result = _payload(
        await call_mcp_tool(
            app, token=_TOKEN, name="start_turn", arguments={"daimon_id": daimon_id, "message": "x"}
        )
    )
    assert not result.get("isError"), result
    await _drain()
    assert created.call_args.kwargs["tool_safety"].enabled
    assert paused.updates and paused.policy == "always_ask"
    assert [(e["type"], e.get("result")) for e in paused.sent] == [
        ("user.message", None),
        ("user.tool_confirmation", "deny"),
    ]


async def test_hub_ask_with_a_handle_resumes_then_answers_the_new_message(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_POLL_S", 0.01)
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=True, recorded=True
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="ask",
            arguments={"daimon_id": daimon_id, "handle": _SESSION, "message": "next"},
        )
    )
    assert not result.get("isError"), result
    await _drain()
    assert [(e["type"], e.get("result")) for e in paused.sent] == [
        ("user.tool_confirmation", "deny"),
        ("user.message", None),
        ("user.tool_confirmation", "deny"),
    ]


def _session_obj(paused: _Paused) -> Any:
    from anthropic.types.beta import BetaManagedAgentsSession

    return BetaManagedAgentsSession.model_validate(paused.model())
