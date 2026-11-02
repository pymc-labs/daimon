"""An OpenAI-hosted chat turn (mux only: legacy cannot run it) interrupted and retried.

Legacy refuses a non-Anthropic profile, so there is no legacy cell to compare
with. These cells show what the mux contract itself does across a restart:
an uncertain send is never repeated by the interrupted turn, the retry is a new
input on a new key, and the interrupted turn's own usage is whatever its
process managed to record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, Literal, cast

import httpx
import pytest
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import openai_host
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import extract_final_response
from daimon.testing.factories import make_tenant
from daimon.testing.recovery import KillableSessionmaker, ledger, record
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ResourceRef, Revision
from mux.contracts.resources import SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ..turn.test_openai_host import Stream, Wire
from ..turn.test_provider_dispatch import (
    ACCOUNT,
    PROFILE,
    REVISION,
    SCOPE,
    TENANT,
    admission,
    deps_for_pinned_mux,
)

Mode = Literal["kill", "cancel"]
MODES: tuple[Mode, ...] = ("kill", "cancel")
PATIENCE_S = 3.0


class Hooks:
    """Where the provider stalls: after accepting the input, or before the answer is read."""

    def __init__(self) -> None:
        self.hang_after_accept = False
        self.hang_before_answer = False
        self.reached = asyncio.Event()
        self.never = asyncio.Event()


class StallingStream(Stream):
    hooks: Hooks

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        if self.hooks.hang_before_answer:
            await self.wire.sent.wait()
            self.hooks.reached.set()
            await self.hooks.never.wait()
        async for chunk in super().__aiter__():
            yield chunk


class NativeProcess:
    """One service process hosting OpenAI turns against the shared wire and database."""

    def __init__(self, engine: AsyncEngine, wire: Wire, hooks: Hooks, usage: list[Any]) -> None:
        self.sessionmaker = KillableSessionmaker(engine, expire_on_commit=False)
        self.wire = wire
        self.tasks: list[asyncio.Task[Any]] = []
        self.hooks = hooks
        self.usage = usage
        self.sdk: AsyncOpenAI | None = None

    def deps(self) -> Any:
        hooks, wire = self.hooks, self.wire

        async def transport(request: httpx.Request) -> httpx.Response:
            sends = request.method == "POST" and request.url.path.endswith("/events")
            if sends and hooks.hang_after_accept and b"input.cancel" not in request.content:
                wire.handle(request)  # the provider accepts it, the answer never comes back
                hooks.reached.set()
                await hooks.never.wait()
            return wire.handle(request)

        self.sdk = AsyncOpenAI(
            api_key="offline",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(cast(Any, transport))),
        )

        async def plan(request: Any) -> SessionSpec:
            return SessionSpec(
                agent=ResourceRef(
                    id="native-agent",
                    kind="agent",
                    provider="openai",
                    account_scope_id="project",
                    tenant_id=SCOPE.tenant_id,
                    account_id=SCOPE.account_id,
                ),
                agent_revision=Revision(local=0),
                config_revision=REVISION.local,
            )

        runtime = OpenAIHostRuntime(
            lambda revision, scope: SDKTransport(self.sdk),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            controls=SessionControls(
                model="gpt-6-luna",
                multi_agent_enabled=False,
                container_size="small",
                spend_limit_usd_cents=4,
            ),
        )
        return replace(
            deps_for_pinned_mux(None, self.sessionmaker),
            channel_backends=True,
            state_store=PostgresStateStore(self.sessionmaker),
            turn_runtimes={PROFILE: runtime},
        )

    def start_turn(self, text_: str, lifecycle: RecordingLifecycle) -> asyncio.Task[Any]:
        async def turn() -> Any:
            deps = self.deps()
            prepared = await bind_session_impl(
                deps,
                admission(),
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id="thread",
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )

            async def reseed() -> str:
                return text_

            return await run_prepared_turn(
                deps,
                prepared,
                tenant_id=TENANT,
                platform="slack",
                thread_id="thread",
                external_user_id="caller",
                user_message=text_,
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                reseed_user_message=reseed,
                recovery_lifecycle=lambda cancel: RecordingLifecycle(),
                render_interval_s=0.01,
            )

        task = asyncio.ensure_future(turn())
        self.tasks.append(task)
        return task

    async def interrupt(self, mode: Mode) -> None:
        if mode == "kill":
            self.sessionmaker.dead = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def expire_leases(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "update thread_lease set active = jsonb_set(active, '{expires_at}', "
                "to_jsonb(to_char((now() at time zone 'utc') - interval '1 second', "
                "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'))) where jsonb_typeof(active) = 'object'"
            )
        )


async def run_case(
    db_session: AsyncSession,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    *,
    point: str,
    hang_after_accept: bool,
    hang_before_answer: bool,
) -> None:
    wire = Wire()
    hooks = Hooks()
    hooks.hang_after_accept = hang_after_accept
    hooks.hang_before_answer = hang_before_answer
    StallingStream.hooks = hooks
    import sys

    monkeypatch.setattr(sys.modules[Wire.__module__], "Stream", StallingStream)
    usage: list[Any] = []

    async def record_usage(**kwargs: Any) -> bool:
        usage.append(kwargs)
        return False

    monkeypatch.setattr(openai_host, "record_provider_usage", record_usage)
    await make_tenant(db_session, id=TENANT)
    await db_session.commit()

    first = NativeProcess(engine, wire, hooks, usage)
    first_turn = first.start_turn("first question", RecordingLifecycle())
    await asyncio.wait_for(hooks.reached.wait(), timeout=20)
    await first.interrupt(mode)
    assert first_turn.done()
    posts_when_died = sum(
        r.method == "POST" and r.url.path.endswith("/events") for r in wire.requests
    )
    before = await ledger(engine, TENANT)
    usage_when_died = len(usage)

    hooks.hang_after_accept = False
    hooks.hang_before_answer = False
    second = NativeProcess(engine, wire, hooks, usage)
    lifecycle = RecordingLifecycle()
    retry = second.start_turn("second question", lifecycle)
    done, _ = await asyncio.wait({retry}, timeout=PATIENCE_S)
    blocked = not done
    if blocked:
        await expire_leases(engine)
        await asyncio.wait_for(retry, timeout=30)
    outcome = retry.result()
    await drain_outcomes()
    after = await ledger(engine, TENANT)
    texts = [
        json.loads(r.content)["events"][0]["input"][0]["content"][0]["text"]
        for r in wire.requests
        if r.method == "POST" and r.url.path.endswith("/events")
    ]
    assert outcome.state.error is None
    assert texts.count("first question") == 1, "the interrupted input is never repeated by Daimon"
    assert posts_when_died == 1
    record(
        "chat-openai",
        point,
        mode,
        "mux",
        provider_inputs_accepted=texts,
        leases_held_after_death=before.live_leases,
        operations_after_death=before.operations_by_status,
        operations_after_retry=after.operations_by_status,
        retry_blocked_by_lease=blocked,
        retry_reply=extract_final_response(outcome.state.content) or None,
        provider_usage_recorded_when_died=usage_when_died,
        provider_usage_recorded_after_retry=len(usage),
        provider_turns_run=len(wire.turns),
        leases_held_after_retry=after.live_leases,
    )


@pytest.mark.parametrize("mode", MODES)
async def test_n2_input_accepted_but_receipt_never_arrives(
    db_session: AsyncSession,
    db_committing_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
) -> None:
    await run_case(
        db_session,
        db_committing_engine,
        monkeypatch,
        mode,
        point="N2 input accepted, receipt lost",
        hang_after_accept=True,
        hang_before_answer=False,
    )


@pytest.mark.parametrize("mode", MODES)
async def test_n4_provider_finished_but_process_never_saw_it(
    db_session: AsyncSession,
    db_committing_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
) -> None:
    await run_case(
        db_session,
        db_committing_engine,
        monkeypatch,
        mode,
        point="N4 completed at provider, unseen",
        hang_after_accept=False,
        hang_before_answer=True,
    )
