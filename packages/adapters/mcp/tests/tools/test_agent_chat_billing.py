"""Headless agent-chat turns are debited once a read sees them end.

`start_turn`/`continue_turn` send a message and return; no stream is driven,
so nothing meters the turn live. Each send leaves a settle request, and the
status and transcript reads settle the session once the turn has ended.
Exercised on the stateful sessions fake and a real ledger.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_chat import (
    _ask_impl,
    _continue_turn_impl,
    _get_session_impl,
    _list_events_impl,
    _settle_when_finished,
    _start_turn_impl,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import session_settle_state, tenant_ledger
from daimon.core.stores.domain import TenantLedgerRow
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    MARouter,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
)
from mux.testing.drivers.anthropic.ma_models import EMPTY_CLOUD_CONFIG, ma_model_usage, ma_session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MA_AGENT_ID = "ag_billing001"
_ENV_NAME = "production"
_MODEL = "claude-sonnet-4-6"
_MARKUP = Decimal("1.1")
_NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)


class _Agent:
    """Runs each sent turn to the end: `calls_per_turn` model calls, then idle."""

    def __init__(self, state: FakeSessionsState, *, calls_per_turn: int) -> None:
        self.state = state
        self.calls_per_turn = calls_per_turn
        self.ids = itertools.count(1)
        # False: the turn is still running when the send returns.
        self.finishes = True
        # A status code: Managed Agents answers the send with it and stores nothing.
        self.reject: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/sessions/([^/]+)/events", request.url.path)
        if request.method != "POST" or m is None:
            raise NotHandled
        if self.reject is not None:
            return httpx.Response(
                self.reject, json={"type": "error", "error": {"type": "api_error", "message": "no"}}
            )
        response = make_fake_sessions_handler(self.state)(request)
        if not self.finishes:
            return response
        sid = m.group(1)
        events = self.state.events[sid]
        for _ in range(self.calls_per_turn):
            n = next(self.ids)
            events.append(
                BetaManagedAgentsSpanModelRequestEndEvent(
                    id=f"evt_call_{n}",
                    is_error=False,
                    model_request_start_id=f"start_{n}",
                    model_usage=ma_model_usage(
                        input_tokens=1000 * n, output_tokens=400, cache_read_input_tokens=100
                    ),
                    processed_at=_NOW + dt.timedelta(seconds=n),
                    type="span.model_request_end",
                ).model_dump(mode="json")
            )
        events.append(
            {
                "id": f"evt_reply_{len(events)}",
                "type": "agent.message",
                "content": [{"type": "text", "text": "done"}],
                "processed_at": (_NOW + dt.timedelta(seconds=50)).isoformat(),
            }
        )
        events.append(
            BetaManagedAgentsSessionStatusIdleEvent(
                id=f"evt_idle_{next(self.ids)}",
                processed_at=_NOW + dt.timedelta(minutes=1),
                stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
                type="session.status_idle",
            ).model_dump(mode="json")
        )
        self.state.sessions[sid] = self.state.sessions[sid].model_copy(update={"status": "idle"})
        return response


def _directory(tenant_id: uuid.UUID) -> MARouter:
    env = {
        "id": "env_billing001",
        "type": "environment",
        "name": _ENV_NAME,
        "config": EMPTY_CLOUD_CONFIG.model_dump(mode="json"),
        "description": "",
        "metadata": {"daimon_tenant": str(tenant_id), "daimon_name": _ENV_NAME},
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
    }
    agent = ma_agent(
        id=_MA_AGENT_ID,
        name="test-agent",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "test-agent"},
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    router.add("GET", r"/v1/environments", lambda _r, _m: list_response([env]))
    return router


class _World:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        exempt: bool = False,
    ) -> None:
        self.tenant_id = tenant_id
        self.sessionmaker = sessionmaker
        self.state = FakeSessionsState(ma=FakeMAState())
        self.agent = _Agent(self.state, calls_per_turn=2)
        self.client: AsyncAnthropic = build_fake_anthropic(
            combine_handlers(
                self.agent,
                make_fake_sessions_handler(self.state),
                _directory(tenant_id).dispatch,
            )
        )
        settings = MagicMock()
        settings.mcp.jwt_secret = None
        settings.github.fallback_pat = None
        settings.billing.markup = _MARKUP
        self.runtime = McpRuntime(
            session_factory=sessionmaker,
            client=self.client,
            settings=settings,  # type: ignore[arg-type]
            deployment_default=DeploymentDefault(environment_name=_ENV_NAME),
        )
        self.auth = AuthIdentity(
            account_id=account_id,
            tenant_id=tenant_id,
            role=Role.USER,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID),
            platform="discord",
            platform_user_id=None if exempt else "user-1",
        )
        self.sid = f"sesn_{uuid.uuid4().hex[:12]}"
        metadata = {
            MA_METADATA_KEY_TENANT: str(tenant_id),
            "daimon_agent_chat": "true",
            MA_METADATA_KEY_ACCOUNT: str(account_id),
            MA_METADATA_KEY_BUDGET_CHANNEL: "chan-1",
        }
        if exempt:
            metadata[MA_METADATA_KEY_BILLING_EXEMPT] = "mcp-internal-caller"
        self.session: BetaManagedAgentsSession = ma_session(
            id=self.sid, agent_id=_MA_AGENT_ID, model=_MODEL, status="running", metadata=metadata
        )
        self.on_read = _settle_when_finished(self.runtime)

    def created(self) -> Any:
        async def create(*_a: Any, **_k: Any) -> BetaManagedAgentsSession:
            self.state.sessions[self.session.id] = self.session
            return self.session

        return patch(
            "daimon.adapters.mcp.tools.agent_chat.create_session", new=AsyncMock(side_effect=create)
        )

    async def start(self) -> dict[str, str]:
        with self.created():
            return await _start_turn_impl(self.runtime, self.auth, "hi")

    async def continue_(self) -> dict[str, str]:
        return await _continue_turn_impl(self.runtime, self.auth, self.sid, "more")

    async def poll(self) -> str:
        info = await _get_session_impl(self.runtime, self.auth, self.sid, on_read=self.on_read)
        return info.status

    async def pending(self) -> str | None:
        """The pending request's turn ("unnamed" before the send names it), or None."""
        async with self.sessionmaker() as s:
            request = await session_settle_state.settle_request(s, managed_session_id=self.sid)
        return None if request is None else request.turn_event_id or "unnamed"


async def _world(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], **kwargs: Any
) -> _World:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return _World(sessionmaker, tenant.id, account.id, **kwargs)


@pytest.fixture
async def world(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> _World:
    return await _world(db_session, db_session_factory)


async def _debits(world: _World, db_session: AsyncSession) -> list[TenantLedgerRow]:
    rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=world.tenant_id)
    return [r for r in rows if r.reason == "turn_debit"]


async def _ledger(world: _World, db_session: AsyncSession) -> dict[str, Decimal]:
    return {r.idempotency_key: r.delta_usd for r in await _debits(world, db_session)}


def _expected_debit(n: int) -> Decimal:
    """The chat path's price for the fake's n-th call (`record_turn_usage`)."""
    usage = ma_model_usage(input_tokens=1000 * n, output_tokens=400, cache_read_input_tokens=100)
    return -debit_amount(cost_of(usage, MODEL_PRICING[_MODEL]), markup=_MARKUP)


async def test_a_send_leaves_a_settle_request_for_its_turn(
    world: _World, db_session: AsyncSession
) -> None:
    started = await world.start()

    assert await world.pending() == started["turn_event_id"]
    assert await _ledger(world, db_session) == {}, "nothing is billed at send time"


async def test_the_first_read_after_the_turn_ends_debits_it_once(
    world: _World, db_session: AsyncSession
) -> None:
    await world.start()

    assert await world.poll() == "idle"
    billed = await _ledger(world, db_session)
    assert await world.poll() == "idle"

    assert billed == {
        f"turn:{world.sid}:evt_call_1": _expected_debit(1),
        f"turn:{world.sid}:evt_call_2": _expected_debit(2),
    }, "each call once, at the chat path's price, markup and key"
    assert {r.channel_id for r in await _debits(world, db_session)} == {"chan-1"}
    assert await _ledger(world, db_session) == billed, "a repeated poll debits nothing"
    assert await world.pending() is None


async def test_a_read_before_the_turn_ends_bills_nothing_yet(
    world: _World, db_session: AsyncSession
) -> None:
    world.agent.finishes = False
    started = await world.start()
    # The session still reads idle from before the send: the previous turn's state.
    world.state.sessions[world.sid] = world.session.model_copy(update={"status": "idle"})

    assert await world.poll() == "idle"

    assert await world.pending() == started["turn_event_id"]
    assert await _ledger(world, db_session) == {}


async def test_a_transcript_read_also_settles_the_finished_turn(
    world: _World, db_session: AsyncSession
) -> None:
    await world.start()

    await _list_events_impl(
        world.runtime, world.auth, world.sid, None, None, "asc", on_read=world.on_read
    )

    assert len(await _ledger(world, db_session)) == 2


async def test_a_continued_turn_bills_only_its_new_calls(
    world: _World, db_session: AsyncSession
) -> None:
    await world.start()
    await world.poll()
    await world.continue_()
    await world.poll()

    ledger = await _ledger(world, db_session)
    assert set(ledger) == {f"turn:{world.sid}:evt_call_{n}" for n in (1, 2, 4, 5)}, (
        "two calls per turn, each once (the fake's idle events take ids 3 and 6)"
    )
    assert ledger[f"turn:{world.sid}:evt_call_5"] == _expected_debit(5)


async def test_ask_settles_the_turn_it_waited_for(world: _World, db_session: AsyncSession) -> None:
    with (
        world.created(),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.deliver_hosted_charts",
            new=AsyncMock(return_value=MagicMock(message="ok", chart_urls=(), image_blocks=())),
        ),
    ):
        await _ask_impl(world.runtime, world.auth, "hi", on_read=world.on_read)

    assert len(await _ledger(world, db_session)) == 2
    assert await world.pending() is None


async def test_an_exempt_callers_session_is_never_debited(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory, exempt=True)
    await world.start()

    await world.poll()

    assert await _ledger(world, db_session) == {}
    assert await world.pending() is None


async def test_the_request_is_written_before_the_send(
    world: _World, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that cannot be written stops the turn before anything is sent."""
    monkeypatch.setattr(
        session_settle_state,
        "request_settle",
        AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
    )

    with pytest.raises(SQLAlchemyError):
        await world.start()

    assert world.state.sent_batches == [], "nothing was sent, so nothing can go unbilled"


async def test_a_send_whose_turn_could_not_be_named_is_still_billed(
    world: _World, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        session_settle_state,
        "name_settle_turn",
        AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
    )
    started = await world.start()
    monkeypatch.undo()
    assert await world.pending() == "unnamed", "the request written before the send stands"
    # Ten minutes on, the send is no longer in flight.
    async with world.sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "UPDATE usage_reconcile_progress "
                "SET settle_requested_at = settle_requested_at - interval '11 minutes' "
                "WHERE managed_session_id = :sid"
            ),
            {"sid": world.sid},
        )

    # The turn's own message is listed after the request: the fake's echo has no
    # processed time yet, as Managed Agents echoes a send.
    await world.poll()
    await sweep_headless_usage(world.client, world.sessionmaker, markup=_MARKUP)

    assert started["turn_event_id"]
    assert len(await _ledger(world, db_session)) == 2
    assert await world.pending() is None


@pytest.mark.parametrize(("status", "kept"), [(400, False), (503, True)])
async def test_a_failed_send_withdraws_its_request_only_when_rejected(
    world: _World, db_session: AsyncSession, status: int, kept: bool
) -> None:
    """A 4xx was not accepted; a 5xx may hide an accepted send, so its request stays."""
    world.agent.reject = status

    with pytest.raises(anthropic.APIStatusError):
        await world.start()

    assert (await world.pending() == "unnamed") is kept
