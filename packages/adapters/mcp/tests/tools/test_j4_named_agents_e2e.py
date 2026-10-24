"""J4 end to end, offline: "run a fleet with the fleet-claude, fleet-openai and fleet-gemini
agents", from a member in a fresh channel, on the code defaults.

Each named agent is a saved on-server agent on its own backend (fleet-claude on Managed
Agents, fleet-openai switched to OpenAI, fleet-gemini switched to Gemini), as the smoke
uses them. The root calls the real fleet_spawn tool with agent=<name> for each; from
there everything is the production path: grant -> worker agent admission -> spawn ->
run_worker -> native dispatch, with fake provider transports, on to the fleet card.
Any refusal on this path is a product bug, not a test detail.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import discord
import httpx
from daimon.adapters.mcp.tools.fleet import HostFleetToolService
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendService, BackendActor, load_backend_agent
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.stores.domain import Role
from daimon.core.stores.fleet import WorkerPool
from daimon.core.turn.runtime_manifest import read_frozen_root
from daimon.core.turn_origin import turn_origin
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.ma_models import ma_agent
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from mux.testing.drivers.anthropic.ma import list_response
from structlog.testing import capture_logs

from packages.adapters.mcp.tests.tools.test_fleet import AUTH, runtime
from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import (
    AstraWire,
    GeminiCatalog,
    GeminiTransport,
)
from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import j10_root as j10_root
from packages.core.tests.turn.test_frozen_root import run_root_input

NAMED = {"fleet-claude": None, "fleet-openai": "openai", "fleet-gemini": "gemini"}


async def _save_named_agents(j10_root) -> dict[str, str]:
    """The three saved agents, each on its own backend; returns name -> MA id."""
    deps, prepared, *_ = j10_root
    transport = j10_root[7]
    tenant_id = prepared.admission.grant.tenant_id
    native = {
        name: ma_agent(
            id=f"agent_{name.replace('-', '_')}",
            tenant_id=str(tenant_id),
            name=name,
            system=f"You are {name}.",
            # As a saved agent has them: the base toolset and Daimon's MCP toolset.
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                    # A URL tool: Managed Agents echoes url_sources on its config (#1432).
                    "configs": [
                        {
                            "name": "web_fetch",
                            "enabled": True,
                            "permission_policy": {"type": "always_allow"},
                        }
                    ],
                },
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                    "configs": [],
                },
            ],
            mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": "https://mcp.invalid"}],
            description=f"{name}, a saved agent",
        )
        for name in NAMED
    }
    root_agent = prepared.admission.agent.model_dump(mode="json")
    routes = [
        (
            "GET",
            re.compile(r"/v1/agents"),
            lambda call, match: list_response(
                [root_agent, *(agent.model_dump(mode="json") for agent in native.values())]
            ),
        ),
    ]
    for agent in native.values():
        body = agent.model_dump(mode="json")
        routes.append(
            (
                "GET",
                re.compile(rf"/v1/agents/{re.escape(agent.id)}"),
                lambda call, match, body=body: httpx.Response(200, json=body),
            )
        )
    # A never-run agent gets its memory store on first use: the real provisioning
    # path, against Managed Agents' memory stores.
    from mux.testing.drivers.anthropic.ma import (
        FakeMemoryStoreState,
        make_fake_memory_store_handler,
    )

    memory = make_fake_memory_store_handler(FakeMemoryStoreState())
    routes.append(("POST", re.compile(r"/v1/memory_stores"), lambda call, match: memory(call)))
    # The Claude worker's clone readback, as Managed Agents answers it: url_sources
    # echoed on the web_fetch config (SMOKE d75 J4, #1432).
    opus = transport.opus

    def echoed_readback(call, match):
        raw = opus["agent"].model_dump(mode="json", exclude_unset=True)
        for tool in raw.get("tools") or ():
            for config in tool.get("configs") or ():
                if config.get("name") == "web_fetch":
                    config["url_sources"] = {"type": "any"}
                    opus["echoed"] = True
        return httpx.Response(200, json=raw)

    routes.append(("GET", re.compile(r"/v1/agents/opus-worker"), echoed_readback))
    transport.router.routes[:0] = routes
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    admin = BackendActor(tenant_id, prepared.admission.account_id, Subject(is_admin=True))
    for name, backend in NAMED.items():
        if backend is None:
            continue
        agent = await load_backend_agent(
            deps.anthropic, deps.sessionmaker, tenant_id=tenant_id, agent_name=name
        )
        preview = await service.preview(agent, backend)
        await service.set_backend(
            agent,
            backend,
            preview.setting_revision,
            admin,
            expected_preview_digest=preview.digest,
            accepted_drops=preview.declared_drops,
        )
    return {name: agent.id for name, agent in native.items()}


async def _settled_within(ports, root, count, seconds):
    async with asyncio.timeout(seconds):
        while True:
            _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
            if len(rows) == count and all(
                row.status in ("failed", "succeeded", "cancelled") for row in rows
            ):
                return rows
            await asyncio.sleep(0.05)


async def _run_named_fleet(j10_root, monkeypatch, *, tenant_cap: int = 4, settle_s: float = 30):
    """Spawn the three named agents as one member's fleet and run it to the collected card."""
    import mux.drivers.gemini as gemini_sdk
    import mux.drivers.openai as openai_sdk
    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard
    from daimon.core.provider_keys import DEFAULT_KEY_ENV
    from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.isolated_sdk import build_async_openai, build_gemini_client

    deps, prepared, _, _, seed, scope, _, transport = j10_root
    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    await _save_named_agents(j10_root)
    wire = AstraWire()
    wire.session_id = f"native-openai-session-{uuid4().hex[:8]}"
    wire.session_bodies = []
    gemini_wire = GeminiTransport()
    gemini_wire.counted = []
    monkeypatch.setattr(gemini_sdk, "configured_transport", lambda **kwargs: gemini_wire)
    catalog = GeminiCatalog()
    gemini_client = build_gemini_client(
        transport=httpx.MockTransport(catalog.handle),
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
                transport=httpx.MockTransport(wire.handle),
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
            await run_root_input((*j10_root[:4], origin, *j10_root[5:]))
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
                pool=WorkerPool(
                    max_concurrent_workers=4, max_concurrent_workers_per_tenant=tenant_cap
                ),
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
                replies = []
                for index, name in enumerate(NAMED, start=1):
                    replies.append(
                        dict(
                            await bridge.spawn(
                                caller,
                                run_id=None,
                                origin_context_id=origin.id,
                                operation_key=f"W{index}",
                                role=name,
                                backend=None,
                                agent=name,
                                clone=None,
                                clone_revision=None,
                                overrides=None,
                                session=None,
                                budget_usd=None,
                                deadline=None,
                                max_turns=None,
                                result_contract=None,
                                reviewer_of=None,
                                task="Compare X from your point of view.",
                            )
                        )
                    )
                settled = await _settled_within(ports, root, len(replies), settle_s)
                collected = dict(
                    await bridge.collect(
                        caller, run_id=root.fleet.run_id, origin_context_id=origin.id
                    )
                )
                shown: list = []

                async def show(*args, **kwargs):
                    shown.append(kwargs["view"])

                async def load_parent(interaction):
                    return root, policy

                card = DiscordFleetCard(
                    run_id=root.fleet.run_id,
                    service=owner,
                    load_parent=load_parent,
                    send=show,
                    edit=show,
                )
                assert await card.refresh()
                gate = await card.summary_gate(render=True, seen=True)
            finally:
                await ports.close()
    finally:
        gemini_client.close()
        await gemini_client.aio.aclose()
    return replies, settled, collected, gate, shown, transport, gemini_wire


async def test_j4_a_member_runs_a_fleet_of_the_named_agents(j10_root, monkeypatch):
    replies, settled, collected, gate, shown, transport, gemini_wire = await _run_named_fleet(
        j10_root, monkeypatch
    )
    # All three named agents were admitted, allocated, dispatched and succeeded.
    assert [reply["status"] for reply in replies] == ["pending"] * 3
    assert [(row.role, row.backend, row.status) for row in settled] == [
        ("fleet-claude", "anthropic", "succeeded"),
        ("fleet-openai", "openai", "succeeded"),
        ("fleet-gemini", "gemini", "succeeded"),
    ]
    # fleet-claude's clone readback passed with Managed Agents' url_sources echo (#1432).
    assert transport.opus.get("echoed")
    # fleet-gemini runs as itself: its own instructions, on a capped model request
    # (where an empty tool list is correct, #1428).
    (sent,) = gemini_wire.requests
    assert "agent_config" not in sent and "tools" in sent
    assert "You are fleet-gemini." in str(sent["system_instruction"])
    # fleet_collect has their three results; the card ends collected, the run completed.
    assert collected["status"] == "ready" and len(collected["results"]) == 3
    assert gate == (True, None)
    text = [
        item.content
        for item in shown[-1].walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert text[0] == "**Fleet — collected**"
    for name in NAMED:
        assert any(name in line and "succeeded" in line for line in "\n".join(text).splitlines())


async def test_j4_slow_budget_reads_and_full_slots_never_stop_or_strand_a_worker(
    j10_root, monkeypatch
):
    """SMOKE 26250cd6 (J3/J10): under a fleet spawn burst the run's budget watch read
    slower than its 2 s timeout three polls in a row and stopped every worker ("its
    spending couldn't be measured"), and workers still waiting for a tenant slot were
    left pending until the deadline sweep ("ran out of time"). With two tenant slots (so
    one worker waits) and every budget read slowed to 4 s, every worker still
    runs and succeeds, none is stopped for unmeasured spending or left to expire. (Fails
    on the old watch: three 4 s reads in a row stopped every worker.)"""
    import time

    from daimon.core import channel_budget

    def slowed(read):
        # Every budget read, lock-free or locked, takes about as long as it did on staging
        # under the spawn burst: past the old 2 s read timeout, inside the new minimum.
        async def slow(*args, **kwargs):
            await asyncio.sleep(4)
            return await read(*args, **kwargs)

        return slow

    for name in ("read_fleet_budget_snapshot", "produce_fleet_budget"):
        monkeypatch.setattr(channel_budget, name, slowed(getattr(channel_budget, name)))
    started = time.monotonic()
    with capture_logs() as logs:
        _, settled, _, gate, _, _, _ = await _run_named_fleet(
            j10_root, monkeypatch, tenant_cap=2, settle_s=180
        )
    elapsed = time.monotonic() - started
    assert [(row.role, row.status, row.error_code) for row in settled] == [
        ("fleet-claude", "succeeded", None),
        ("fleet-openai", "succeeded", None),
        ("fleet-gemini", "succeeded", None),
    ]
    assert gate == (True, None)
    assert not [e for e in logs if e.get("event") == "fleet.budget_read_unavailable"]
    assert not [
        e
        for e in logs
        if e.get("event") == "fleet.worker_cancel" and e.get("reason") == "usage_unavailable"
    ]
    # One-sentence workers, two at a time, every budget read 4 s: inside the worker deadline.
    assert elapsed < 240
