"""J7 end to end, offline: limits are refused in plain words, and a typed stop ends a fleet.

The J10 harness (`test_j10_fork_e2e`): code defaults only, a NON-admin member in a
fresh channel, a real frozen root, the real fleet tools and orchestrator, and fake
provider transports. Each case is what TARGET-V2's J7 asks of the product:
(a) an overspend is refused with the real limits, and nothing is spawned;
(b) a disallowed model is refused, naming the allowed ones;
(c) a provider's daily cap at 0 refuses with the plain "used up" copy, and no provider
    request is sent;
(d) "@Daimon stop" by the person who started a live fleet stops it and its workers,
    and the card says so; another member is refused.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import discord
import httpx
import pytest
from daimon.adapters.mcp.tools.fleet import (
    HostFleetToolService,
    _refusals_logged,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.authz import Subject
from daimon.core.fleet import worker_error_text
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.stores import fleet
from daimon.core.stores.domain import Role
from daimon.core.stores.fleet import WorkerPool
from daimon.core.turn.runtime_manifest import read_frozen_root
from daimon.core.turn_origin import turn_origin
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.factories import make_platform_principal
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp.exceptions import ToolError

from packages.adapters.mcp.tests.tools.test_fleet import AUTH, runtime
from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import (
    AstraWire,
    GeminiCatalog,
    GeminiTransport,
    _settled,  # pyright: ignore[reportPrivateUsage]
)
from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import j10_root as j10_root
from packages.core.tests.turn.test_frozen_root import run_root_input


class GatedGemini(GeminiTransport):
    """A Gemini worker that stays in flight until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, request):
        self.started.set()
        await self.release.wait()
        return await super().create(request)


@dataclass
class Fleet:
    """The member's frozen root and the real fleet tools, as the root agent calls them."""

    deps: Any
    root: Any
    policy: Any
    owner: FleetOrchestrator
    ports: HostFleetPorts
    bridge: HostFleetToolService
    caller: Any
    origin: Any
    astra: AstraWire
    gemini: GeminiTransport

    async def spawn(self, key: str, model: str, *, planned: list[str] | None = None, **extra):
        values: dict[str, Any] = {
            "run_id": None,
            "origin_context_id": self.origin.id,
            "operation_key": key,
            "role": f"compare-{model}",
            "backend": None,
            "agent": None,
            "clone": None,
            "clone_revision": None,
            "overrides": None,
            "session": None,
            "budget_usd": None,
            "deadline": None,
            "max_turns": None,
            "result_contract": None,
            "reviewer_of": None,
            "task": "Compare X from your model's point of view.",
            "model": model,
            "planned_models": planned,
        }

        # Through the tool's own refusal wrapper: what the root agent actually reads.
        async def call():
            return dict(await self.bridge.spawn(self.caller, **{**values, **extra}))

        return await _refusals_logged(key, call)

    async def workers(self) -> tuple:
        _, rows = await self.ports.read_run(self.root.fleet.scope, self.root.fleet.run_id)
        return tuple(rows)

    def provider_posts(self) -> int:
        """Native requests that would start (and bill) a worker, on any provider."""
        return len(self.astra.session_bodies) + len(self.gemini.requests)


@asynccontextmanager
async def member_fleet(
    j10_root, monkeypatch, *, gemini: GeminiTransport | None = None, **deps_update: Any
) -> AsyncIterator[Fleet]:
    """The member's one message in a fresh channel, then the fleet tools it may call."""
    import mux.drivers.gemini as gemini_sdk
    import mux.drivers.openai as openai_sdk
    from daimon.core.provider_keys import DEFAULT_KEY_ENV
    from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.isolated_sdk import build_async_openai, build_gemini_client

    deps, prepared, _, _, seed, scope, _, _ = j10_root
    deps = replace(deps, **deps_update)
    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    astra = AstraWire()
    astra.session_id = f"native-openai-session-{uuid4().hex[:8]}"
    astra.session_bodies = []
    gemini_wire = gemini or GeminiTransport()
    gemini_wire.counted = []
    monkeypatch.setattr(gemini_sdk, "configured_transport", lambda **kwargs: gemini_wire)
    gemini_client = build_gemini_client(
        transport=httpx.MockTransport(GeminiCatalog().handle),
        api_key="offline",
        base_url="https://gemini.invalid",
    )
    monkeypatch.setattr(
        gemini_sdk,
        "configured_catalog_transport",
        lambda **kwargs: SDKAgentCatalogTransport(gemini_client),
    )
    try:
        async with (
            build_async_openai(
                transport=httpx.MockTransport(astra.handle),
                api_key="offline",
                base_url="https://openai.invalid/v1",
            ) as sdk,
            turn_origin(
                deps.sessionmaker,
                tenant_id=seed.tenant_id,
                account_id=seed.account_id,
                platform="discord",
                parent_channel_id=seed.parent_channel_id,
                thread_id=seed.thread_id,
                responder_ma_agent_id=prepared.admission.agent.id,
                responder_name=prepared.admission.agent.name,
                role=Role.USER,
                sender_foreign=False,
                shared_channel=False,
                keep_fleet_root=True,
            ) as origin,
        ):
            monkeypatch.setattr(
                openai_sdk, "configured_transport", lambda **kwargs: SDKTransport(sdk)
            )
            await run_root_input((deps, *j10_root[1:4], origin, *j10_root[5:]))
            # Model calls below simulate an in-flight execution after the
            # scripted root driver returned and cleared its signed grant.
            from daimon.core.stores.turn_origins import bind_origin_session

            async with deps.sessionmaker.begin() as db:
                await bind_origin_session(
                    db, origin_id=origin.id, chat_session_id=AUTH.chat_session_id
                )
            root, policy = await read_frozen_root(
                deps, scope, origin_id=origin.id, now=datetime.now(UTC)
            )
            ports = HostFleetPorts(
                deps.sessionmaker,
                markup=Decimal(1),
                deps=deps,
                pool=WorkerPool(max_concurrent_workers=4, max_concurrent_workers_per_tenant=4),
            )
            owner = FleetOrchestrator(ports)
            bridge = HostFleetToolService(
                owner,
                runtime=replace(
                    runtime(), session_factory=deps.sessionmaker, client=deps.anthropic
                ),
                deps=deps,
            )
            caller = replace(
                AUTH,
                tenant_id=origin.tenant_id,
                account_id=origin.account_id,
                role=Role.USER,
                chat_agent_id=root.agent_id,
            )
            try:
                yield Fleet(
                    deps, root, policy, owner, ports, bridge, caller, origin, astra, gemini_wire
                )
            finally:
                if isinstance(gemini_wire, GatedGemini):
                    gemini_wire.release.set()
                await ports.close()
    finally:
        gemini_client.close()
        await gemini_client.aio.aclose()


# --------------------------------------------------------------------------- (a)


async def test_an_overspend_is_refused_with_the_real_limits_and_nothing_spawns(
    j10_root, monkeypatch
):
    planned = ["opus", "astra", "argon", "flash", "opus", "astra", "argon", "flash"]
    async with member_fleet(j10_root, monkeypatch) as run:
        with pytest.raises(ToolError) as refused:
            await run.spawn("W1", "opus", planned=planned)
        text = str(refused.value)
        # The real limits and the user's way forward, in plain words (#1421).
        assert "This fleet is limited to 4 workers and $2 a run." in text, text
        assert "Ask for at most that many workers." in text, text
        assert "_" not in text.split(":", 1)[-1], "no internal codes in the plain part"
        assert await run.workers() == () and run.provider_posts() == 0

        # Asking for more money than the default cap is refused the same way.
        with pytest.raises(ToolError) as costly:
            await run.spawn("W2", "opus", budget_usd=Decimal("10"))
        assert "budget" in str(costly.value).lower()
        assert await run.workers() == () and run.provider_posts() == 0


# --------------------------------------------------------------------------- (b)


async def test_a_disallowed_model_is_refused_naming_the_allowed_ones(j10_root, monkeypatch):
    async with member_fleet(j10_root, monkeypatch) as run:
        with pytest.raises(ToolError) as refused:
            await run.spawn("W1", "gemini pro", planned=["opus", "gemini pro"])
        text = str(refused.value)
        assert '"gemini pro" isn\'t allowed in this fleet' in text, text
        assert "allowed:" in text and "Claude Opus 5.5" in text, text
        assert await run.workers() == () and run.provider_posts() == 0


# --------------------------------------------------------------------------- (c)


@pytest.mark.parametrize("model", ["astra", "argon"])
async def test_a_spent_daily_cap_refuses_plainly_and_sends_nothing(j10_root, monkeypatch, model):
    async with member_fleet(
        j10_root,
        monkeypatch,
        openai_daily_model_cap_usd=Decimal(0),
        gemini_daily_model_cap_usd=Decimal(0),
    ) as run:
        try:
            reply = await run.spawn("W1", model, planned=[model])
        except ToolError as refused:
            text = str(refused.value)
        else:
            assert reply["status"] == "pending", reply
            (worker,) = await _settled(run.ports, run.root, 1)
            assert worker.status == "failed", worker
            # The real cause, not "it couldn't start on that model here".
            assert worker.error_code == "provider_daily_cap_reached", worker.error_code
            text = worker_error_text(worker.error_code) or ""
        assert "used up" in text, text
        assert run.provider_posts() == 0, "a spent cap never reaches the provider"


# --------------------------------------------------------------------------- (d)


def _presser(user_id: str, *, guild: Any, thread: Any) -> Any:
    """The author of "@Daimon stop" in the run's own thread, as the bot passes them."""
    from daimon.adapters.discord.posted_controls.fleet import TypedPresser

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    return TypedPresser(user=user, guild_id=guild, channel_id=thread)


@pytest.mark.parametrize("who", ["requester", "stranger"])
async def test_stop_by_the_fleets_starter_ends_it_and_another_member_is_refused(
    j10_root, monkeypatch, who
):
    from daimon.adapters.discord.posted_controls.fleet import (
        FLEET_STOP_NOT_ALLOWED,
        FLEET_STOPPING,
        DiscordFleetCard,
    )

    deps, _, _, _, seed, _, _, _ = j10_root
    if who == "stranger":
        async with deps.sessionmaker.begin() as db:
            await make_platform_principal(
                db, tenant=await _tenant(db, seed.tenant_id), platform="discord", external_id=who
            )
    gated = GatedGemini()
    async with member_fleet(j10_root, monkeypatch, gemini=gated) as run:
        reply = await run.spawn("W1", "argon", planned=["argon"])
        assert reply["status"] == "pending", reply
        async with asyncio.timeout(30):
            await gated.started.wait()  # the worker is in flight at Gemini
        shown: list = []

        async def show(*args, **kwargs):
            shown.append(kwargs["view"])

        async def load_parent(presser):
            # As the host's loader: the live person acting (else the starter), no admin.
            if presser is None:
                return run.root, run.policy
            subject = Subject(platform_user_id=str(presser.user.id))
            return replace(run.root, subject=subject), run.policy

        card = DiscordFleetCard(
            run_id=run.root.fleet.run_id,
            service=run.owner,
            load_parent=load_parent,
            send=show,
            edit=show,
        )
        assert await card.refresh()
        async with deps.sessionmaker() as db:
            tenant = await _tenant(db, seed.tenant_id)
        presser = _presser(who, guild=tenant.external_id, thread=run.root.fleet.root.thread_id)
        answer = await card.stop_typed(presser, message=None)
        if who == "stranger":
            assert answer == FLEET_STOP_NOT_ALLOWED
            (worker,) = await run.workers()
            assert worker.status not in ("cancelled", "failed"), "a refused stop changes nothing"
            return
        assert answer == FLEET_STOPPING
        # Gemini never answers: the stop itself must end the in-flight worker (releasing
        # it here would race a late success against the cancel).
        (worker,) = await _settled(run.ports, run.root, 1)
        # Stopped, said as such: never "it couldn't start on that model here".
        assert (worker.status, worker.error_code) == ("cancelled", "stop_requested"), worker
        final, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=run.origin.tenant_id, run_id=run.root.fleet.run_id
        )
        assert final.status in ("cancelled", "collecting", "completed"), final.status
        text = "\n".join(
            item.content
            for item in shown[-1].walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        assert "stop" in text.lower() or "cancel" in text.lower(), text


async def _tenant(db, tenant_id):
    from daimon.core.stores.tenants import get_tenant

    return await get_tenant(db, tenant_id)
