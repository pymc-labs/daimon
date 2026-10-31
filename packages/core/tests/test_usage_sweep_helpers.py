"""A coordinator session's helpers are billed at their own model, once, then archived.

Recorded on staging (`fixtures/ma_subagents/`): a Sonnet 5.5 lead delegated to two
Haiku 5.5 helpers. The session's own event list carries only the lead's six model
calls; each helper's three are listed under its thread. Before this settle, OS
billed the lead and nothing else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.tenant_balance import debit_amount
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import bind_recorder
from daimon.core.usage_sweep import settle_session
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

FIXTURES = Path(__file__).parent / "fixtures" / "ma_subagents"
SESSION = "sesn_01QZ6tcns4Sajq25PiciqxTg"
LEAD = "claude-sonnet-5-5"
HELPER = "claude-haiku-5-5"
MARKUP = Decimal("1.1")


def _primary_events() -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / "fanout.jsonl").read_text().splitlines()]


def _thread_events() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for path in FIXTURES.glob("thread_events_*.json"):
        data = json.loads(path.read_text())["data"]
        thread = next(t for t in _threads() if t["id"].endswith(path.stem.rsplit("_", 1)[1]))
        out[thread["id"]] = data
    return out


def _threads() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads((FIXTURES / "threads.json").read_text())["data"])


class _CoordinatorMA:
    """The session's events, its threads and each thread's events, as MA lists them."""

    def __init__(self) -> None:
        self.threads = _threads()
        self.thread_events = _thread_events()
        self.archived: list[str] = []
        self.thread_reads: list[str] = []
        self.fail_archive: set[str] = set()
        router = MARouter()
        router.add(
            "POST",
            r"/v1/sessions/(?P<sid>[^/]+)/threads/(?P<tid>[^/]+)/archive",
            cast(Any, self._archive),
        )
        router.add(
            "GET",
            r"/v1/sessions/(?P<sid>[^/]+)/threads/(?P<tid>[^/]+)/events",
            cast(Any, self._thread_list),
        )
        router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/threads", cast(Any, self._threads))
        router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events", cast(Any, self._events))
        self.client = build_fake_anthropic(router.dispatch)

    def _events(self, request: httpx.Request, _match: Any) -> httpx.Response:
        types = set(request.url.params.get_list("types[]"))
        data = [e for e in _primary_events() if not types or e["type"] in types]
        return httpx.Response(200, json={"data": data[::-1], "next_page": None})

    def _threads(self, _request: httpx.Request, _match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": self.threads, "next_page": None})

    def _thread_list(self, _request: httpx.Request, match: Any) -> httpx.Response:
        self.thread_reads.append(match["tid"])
        return httpx.Response(
            200, json={"data": self.thread_events[match["tid"]], "next_page": None}
        )

    def _archive(self, _request: httpx.Request, match: Any) -> httpx.Response:
        if match["tid"] in self.fail_archive:
            return httpx.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}
            )
        self.archived.append(match["tid"])
        thread = next(t for t in self.threads if t["id"] == match["tid"])
        thread.update(archived_at=datetime.now(UTC).isoformat(), status="terminated")
        return httpx.Response(200, json=thread)


def _recorder(sessionmaker: async_sessionmaker[AsyncSession], tenant_id: Any, ma: _CoordinatorMA):
    deps = TurnDeps(
        anthropic=ma.client,
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=MARKUP,
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )
    return bind_recorder(
        deps,
        tenant_id=tenant_id,
        external_user_id="user-1",
        ma_session_id=SESSION,
        model_id=LEAD,
        channel_id="chan-1",
    )


def _debit(event: dict[str, Any], model: str) -> Decimal:
    span = BetaManagedAgentsSpanModelRequestEndEvent.model_validate(event)
    return -debit_amount(cost_of(span.model_usage, MODEL_PRICING[model]), markup=MARKUP)


async def test_helper_calls_are_billed_at_the_helpers_model_once_and_then_archived(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _CoordinatorMA()
    record = _recorder(db_session_factory, tenant.id, ma)

    first = await settle_session(
        ma.client, db_session_factory, SESSION, record=record, helpers=True
    )

    lead_calls = [e for e in _primary_events() if e["type"] == "span.model_request_end"]
    helper_calls = {
        tid: [e for e in events if e["type"] == "span.model_request_end"]
        for tid, events in ma.thread_events.items()
    }
    assert (len(lead_calls), sorted(len(c) for c in helper_calls.values())) == (6, [3, 3])
    ledger = {
        r.idempotency_key: r.delta_usd
        for r in (await db_session.execute(select(TenantLedger))).scalars()
    }
    expected = {f"turn:{SESSION}:{e['id']}": _debit(e, LEAD) for e in lead_calls}
    for calls in helper_calls.values():
        expected |= {f"turn:{SESSION}:{e['id']}": _debit(e, HELPER) for e in calls}
    assert ledger == expected
    models = {r.event_id: r.model for r in (await db_session.execute(select(UsageEvent))).scalars()}
    assert sorted(set(models.values())) == [HELPER, LEAD]
    assert sum(1 for m in models.values() if m == HELPER) == 6
    assert (first.debited, first.complete) == (12, True)
    # Both helpers were idle and fully billed: archived, which frees their place
    # under the provider's 25-thread cap and marks them settled.
    assert sorted(ma.archived) == sorted(helper_calls)

    again = await settle_session(
        ma.client, db_session_factory, SESSION, record=record, helpers=True
    )
    assert (again.debited, again.unbilled) == (0, ())
    assert len(ma.thread_reads) == 2, "an archived thread is never read again"


async def test_a_session_without_helpers_never_lists_threads(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _CoordinatorMA()
    record = _recorder(db_session_factory, tenant.id, ma)

    result = await settle_session(ma.client, db_session_factory, SESSION, record=record)

    assert result.debited == 6, "the lead's own calls, as before"
    assert (ma.thread_reads, ma.archived) == ([], [])


async def test_a_read_only_settle_reports_helper_calls_and_archives_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma = _CoordinatorMA()

    result = await settle_session(ma.client, db_session_factory, SESSION, record=None, helpers=True)

    assert (len(result.unbilled), result.debited) == (12, 0)
    assert ma.archived == []


async def test_a_helper_still_working_keeps_the_settle_open_and_one_bad_thread_stops_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _CoordinatorMA()
    first, second = ma.threads[1]["id"], ma.threads[2]["id"]
    ma.threads[1]["status"] = "running"  # a ceiling interrupt not yet taken effect
    ma.threads[2]["status"] = "terminated"  # an errored helper: billed, then archived
    record = _recorder(db_session_factory, tenant.id, ma)

    result = await settle_session(
        ma.client, db_session_factory, SESSION, record=record, helpers=True
    )

    assert result.complete is False, "the drain finishes it once the helper stops"
    assert result.debited == 12, "every listed call is billed, running thread included"
    assert ma.archived == [second]
    assert first not in ma.archived

    # A failed archive leaves the thread for the next settle and stops no other.
    ma.threads[1]["status"] = "idle"
    ma.fail_archive.add(first)
    again = await settle_session(
        ma.client, db_session_factory, SESSION, record=record, helpers=True
    )
    assert (again.complete, again.debited) == (False, 0)
