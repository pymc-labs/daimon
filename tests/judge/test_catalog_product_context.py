"""Real DB context preparation and fail-closed opt-in consumer controls."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.deps import TurnDeps
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.anthropic_fixtures import (
    anthropic_turn_tape,
    load_anthropic_fixtures,
)
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import load_provider_fixtures
from daimon.testing.provider_replay import SourcePin
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    combine_handlers,
    make_fake_memory_store_handler,
)
from mux.testing.isolated_sdk import build_async_anthropic

from .catalog_product_context import ProductContext, ProductContextPending, ProductTurnRecipe
from .catalog_runner import Invocation, load_catalog, plan_scenario, sha
from .catalog_steps import run_steps
from .headless_executor import execute_cell, execute_gemini_cell, execute_openai_cell
from .test_anthropic_catalog import CATALOG, INDEX, REPO, replay, selected
from .test_catalog_admin import (
    CHANNEL,
    VALUES,
    admin_row,
    admin_source,
    no_channel,
    no_turn,
    require,
)
from .test_catalog_admin import (
    binding as binding,
)
from .test_catalog_runner import planned

pytest_plugins = ["daimon.testing.db"]


def context(bound: AdminBindings, tmp_path: Path) -> ProductContext:
    return ProductContext(
        bound,
        {"A": CHANNEL},
        {
            ("A", "T"): "323456789012345678",
            ("A", "turn1"): "323456789012345678",
            ("A", ""): "323456789012345678",
        },
        "423456789012345678",
        {},
        tmp_path,
        REPO,
    )


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
async def test_opt_in_without_pinned_preparation_never_executes_synthetic_turn(
    tmp_path: Path,
    binding: AdminBindings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = context(binding, tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("product opt-in allocated a memory store")

    if binding.backend == "anthropic":
        from . import headless_executor as host

        monkeypatch.setattr(host, "MemoryStateStore", forbidden)
        from daimon.testing.anthropic_fixtures import load_anthropic_fixtures

        plan, recipe = selected(load_anthropic_fixtures(INDEX, source_root=REPO))
        # Bind every actual source slot explicitly, including a setup with no thread.
        ctx.channels = {b.ref: CHANNEL for b in plan.bindings}
        ctx.threads = {
            (i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations
        }
        result = await execute_cell(
            plan,
            replay(recipe),
            native_recipe=recipe,
            admin=binding,
            admin_values=VALUES,
            product_context=ctx,
        )
    else:
        from . import gemini_catalog_replay as ge
        from . import openai_catalog_replay as oa

        module = oa if binding.backend == "openai" else ge
        monkeypatch.setattr(module, "MemoryStateStore", forbidden)
        pack = load_provider_fixtures(INDEX.parent.parent / "index.json", catalog_root=CATALOG)
        row, fixture = pack.select("QA-NEW37-MARKDOWN-TABLE-READABLE", binding.backend)
        entry = next(e for e in load_catalog(CATALOG) if e.id == row.scenario_id).model_copy(
            update={"sha256": row.source.sha256}
        )
        plan = plan_scenario(entry, CATALOG, binding.backend, run_id="product-context")
        ctx.channels = {b.ref: CHANNEL for b in plan.bindings}
        ctx.threads = {
            (i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations
        }
        result = await (
            execute_openai_cell if binding.backend == "openai" else execute_gemini_cell
        )(
            plan,
            row,
            fixture,
            source_root=CATALOG,
            repository_root=REPO,
            admin=binding,
            admin_values=VALUES,
            product_context=ctx,
        )
    require(result.reported_status == "PENDING", "unbound context acquired full-cell credit")
    require(
        not result.evidence.turns and result.request_count == 0,
        "pending context acquired native evidence",
    )
    require(
        any(g.code == "ADMIN_HOST_CONTEXT_UNBOUND" for g in result.gaps),
        "context barrier disappeared",
    )
    require(not binding.trace.requests, "unbound context called a provider")


async def test_opt_in_cannot_bypass_context_with_a_forged_host_receipt(
    tmp_path: Path,
    binding: AdminBindings,
) -> None:
    row = admin_row(
        [
            admin_source("daimon tenants turn-cap discord {guild_id} 2"),
            {"do": "mention", "channel": "A", "text": "hello"},
        ]
    )
    plan = planned(tmp_path, row, binding.backend)
    ctx = context(binding, tmp_path)

    async def forged(i: Invocation, _timeout: float) -> TurnEvidence:
        return TurnEvidence(
            turn=i.turn or 1, slot_id="fake", session_id="fake", root_turn_id="fake", started_s=0
        )

    with pytest.raises(ValueError, match="no real prepared-turn capture"):
        await run_steps(
            plan,
            invoke=forged,
            prepare_channel=no_channel,
            admin=binding,
            admin_values=VALUES,
            product_context=ctx,
        )


async def test_context_refuses_foreign_admin_account_before_db_or_native_turn(
    tmp_path: Path,
    binding: AdminBindings,
) -> None:
    ctx = context(binding, tmp_path)
    plan = planned(
        tmp_path, admin_row([{"do": "mention", "channel": "A", "text": "hello"}]), binding.backend
    )
    i = plan.invocations[0]
    binding.scope = binding.scope.model_copy(
        update={"account_id": "00000000-0000-0000-0000-000000000001"}
    )
    with pytest.raises(ScopeViolation):
        await ctx.revision(i)
    require(not binding.trace.requests, "foreign account reached a provider")


@pytest.mark.parametrize("missing", ["channel", "thread", "caller"])
async def test_context_without_owned_destination_stays_pending(
    tmp_path: Path,
    binding: AdminBindings,
    missing: str,
) -> None:
    ctx = context(binding, tmp_path)
    if missing == "channel":
        ctx.channels = {}
    if missing == "thread":
        ctx.threads = {}
    if missing == "caller":
        ctx.external_user_id = ""
    plan = planned(
        tmp_path, admin_row([{"do": "mention", "channel": "A", "text": "hello"}]), binding.backend
    )
    with pytest.raises(ProductContextPending):
        ctx.destination(plan.invocations[0])
    require(not binding.trace.requests, "unowned destination called a provider")


def deps(binding: AdminBindings, client: AsyncAnthropic) -> TurnDeps:
    return TurnDeps(
        anthropic=client,
        sessionmaker=binding.sessions,
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
        channel_backends=True,
        tool_safety=OPEN_TOOL_SAFETY,
    )


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
async def test_public_admission_reads_the_real_writers_policy(
    tmp_path: Path,
    binding: AdminBindings,
) -> None:
    ctx = context(binding, tmp_path)
    plan = planned(
        tmp_path, admin_row([{"do": "mention", "channel": "A", "text": "hello"}]), binding.backend
    )
    i = plan.invocations[0]
    await ctx.configure(i)
    receipt = await binding.execute(
        admin_source("daimon channels rule set discord {guild_id} {channel_id} --writers none"),
        VALUES,
    )
    require(receipt.status == "executed", "real writer policy was not saved")
    with pytest.raises(ProductContextPending, match="real admission/preparation refused") as error:
        await ctx.prepare(deps(binding, binding.anthropic), i, await ctx.revision(i), 10)
    require(
        error.value.__cause__ is not None
        and getattr(error.value.__cause__, "reason", None) == "writers_none",
        "real policy refusal lost",
    )
    require(not binding.trace.requests, "writers-none called a native provider")


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
async def test_public_preparation_creates_and_reuses_the_real_owned_mapping(
    tmp_path: Path,
    binding: AdminBindings,
) -> None:
    import uuid

    ctx = context(binding, tmp_path)
    plan = planned(
        tmp_path, admin_row([{"do": "mention", "channel": "A", "text": "hello"}]), binding.backend
    )
    i = plan.invocations[0]
    await ctx.configure(i)
    from daimon.core.defaults.provisioning import provision_tenant

    await provision_tenant(
        binding.sessions,
        platform="discord",
        workspace_id=binding.workspace_id,
        signup_credit=Decimal("10"),
    )
    agent = ma_agent(
        id="agent_product",
        name="daimon",
        model="claude-haiku-5-5",
        tenant_id=binding.scope.tenant_id,
    )
    environment = ma_environment(
        id="env_product", name="default", tenant_id=binding.scope.tenant_id
    )
    state = FakeSessionsState(ma=FakeMAState())
    state.ma.agents[agent.id] = agent.model_dump(mode="json")
    router = resolved_agent_env_router(agent, environment)
    handler = combine_handlers(
        make_fake_sessions_handler(state), make_fake_memory_store_handler(), router.dispatch
    )
    async with build_async_anthropic(
        api_key="offline",
        base_url="https://offline.invalid",
        headers={},
        max_retries=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        actual_deps = deps(binding, client)
        revision = await ctx.revision(i)
        fresh = await ctx.prepare(actual_deps, i, revision, 30)
        reused = await ctx.prepare(actual_deps, i, revision, 30)
    require(
        fresh.mapping_id is not None and not fresh.reused,
        "public preparation did not create a mapping",
    )
    require(
        reused.reused
        and reused.mapping_id == fresh.mapping_id
        and reused.ma_session_id == fresh.ma_session_id,
        "public preparation did not reuse its real mapping",
    )
    require(
        fresh.admission.grant is not None and fresh.admission.backend_revision == revision,
        "admission grant/config not real",
    )
    async with binding.sessions() as db:
        mapped = await get_live_thread_session(
            db,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform=binding.platform,
            thread_id="323456789012345678",
            account_id=fresh.session_account_id,
        )
    require(
        mapped is not None
        and mapped.id == fresh.mapping_id
        and mapped.ma_session_id == fresh.ma_session_id,
        "product mapping is not readable from owned DB",
    )
    require(
        mapped is not None
        and mapped.effective_config is not None
        and mapped.effective_config.model_id == "claude-haiku-5-5",
        "real frozen model missing",
    )
    with pytest.raises(ValueError, match="no real prepared-turn capture"):
        ctx.verify_execution(
            i,
            TurnEvidence(
                turn=i.turn or 1,
                slot_id="unbound",
                session_id=fresh.ma_session_id,
                root_turn_id="unbound",
                started_s=0,
            ),
        )


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "mutant",
    [
        "hash",
        "tenant",
        "channel",
        "thread",
        "caller",
        "session",
        "root",
        "turn",
        "location",
        "scenario",
        "codec",
    ],
)
async def test_foreign_preparation_recipe_never_reaches_native_io(
    tmp_path: Path,
    binding: AdminBindings,
    mutant: str,
) -> None:
    plan, native_recipe = selected(load_anthropic_fixtures(INDEX, source_root=REPO))
    invocation = next(i for i in plan.invocations if i.operation == "host_turn")
    ctx = context(binding, tmp_path)
    ctx.channels = {b.ref: CHANNEL for b in plan.bindings}
    ctx.threads = {
        (i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations
    }
    turn = native_recipe.turn(invocation.turn or 1)
    record = ProductTurnRecipe(
        scenario_id=plan.scenario.id,
        scenario_sha256=plan.scenario.sha256,
        location=invocation.location,
        turn=invocation.turn or 1,
        tenant_id=binding.scope.tenant_id,
        channel_id=CHANNEL,
        thread_id="323456789012345678",
        external_user_id=ctx.external_user_id,
        session_id=native_recipe.session_id(invocation.turn or 1),
        root_turn_id=str(turn.turn.events[0]["id"]),
        tape=anthropic_turn_tape(
            native_recipe.entry, native_recipe.scenario, turn.turn, codec=native_recipe.codec
        ),
    )
    field = {
        "tenant": "tenant_id",
        "channel": "channel_id",
        "thread": "thread_id",
        "caller": "external_user_id",
        "session": "session_id",
        "root": "root_turn_id",
        "turn": "turn",
        "location": "location",
        "scenario": "scenario_id",
    }.get(mutant)
    if field:
        record = record.model_copy(update={field: 999 if mutant == "turn" else "foreign"})
    if mutant == "codec":
        record = record.model_copy(
            update={
                "tape": record.tape.model_copy(
                    update={"codec": SourcePin(path="foreign", sha256="0" * 64)}
                )
            }
        )
    path = tmp_path / "product.json"
    path.write_text(record.model_dump_json())
    ctx.sources = {
        invocation.turn or 1: SourcePin(
            path=path.name, sha256="0" * 64 if mutant == "hash" else sha(path.read_bytes())
        )
    }
    with pytest.raises(ValueError, match="source pin changed|product recipe differs"):
        await ctx.run(plan, invocation, turn, 10)
    require(not binding.trace.requests, "foreign product tape reached a native SDK")


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "scenario_id",
    ["QA-NEW37-MARKDOWN-TABLE-READABLE", "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY"],
)
async def test_pinned_product_turn_executes_real_admission_preparation_and_postgres_journal(
    tmp_path: Path,
    binding: AdminBindings,
    scenario_id: str,
) -> None:
    import json

    from daimon.testing.anthropic_fixtures import AnthropicTape
    from daimon.testing.provider_replay import WireReply

    from .catalog_product_context import ProductTurnRecipe

    pack = load_anthropic_fixtures(INDEX, source_root=REPO)
    plan, native_recipe = selected(pack, scenario_id)
    invocation = next(i for i in plan.invocations if i.operation == "host_turn")
    native = native_recipe.turn(invocation.turn or 1)
    ctx = context(binding, tmp_path)
    ctx.channels = {b.ref: CHANNEL for b in plan.bindings}
    ctx.threads = {
        (i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations
    }
    binding.default = DeploymentDefault(agent_name="daimon", environment_name="default")
    await ctx.configure(invocation)
    from daimon.core.defaults.provisioning import provision_tenant

    await provision_tenant(
        binding.sessions,
        platform="discord",
        workspace_id=binding.workspace_id,
        signup_credit=Decimal("10"),
    )
    agent = ma_agent(
        id="agent_product",
        name="daimon",
        model="claude-haiku-5-5",
        tenant_id=binding.scope.tenant_id,
    )
    environment = ma_environment(
        id="env_product", name="default", tenant_id=binding.scope.tenant_id
    )
    state = FakeSessionsState(ma=FakeMAState())
    state.ma.agents[agent.id] = agent.model_dump(mode="json")
    handle = combine_handlers(
        make_fake_sessions_handler(state),
        make_fake_memory_store_handler(),
        resolved_agent_env_router(agent, environment).dispatch,
    )
    pinned_session = native_recipe.session_id(invocation.turn or 1)
    replies: list[WireReply] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            old = str(response.json()["id"])
            # The scripted provider assigns the source recipe's native ID in
            # its CREATE acknowledgement; no product row is seeded/adopted.
            state.sessions[pinned_session] = state.sessions.pop(old).model_copy(
                update={"id": pinned_session}
            )
            state.resources[pinned_session] = state.resources.pop(old, [])
            response = httpx.Response(
                200, json=state.sessions[pinned_session].model_dump(mode="json")
            )
        if request.method not in {"GET", "POST", "DELETE"}:
            raise AssertionError("unknown preparation wire method")
        replies.append(
            WireReply(
                id=f"prep-{len(replies)}",
                method=cast(Literal["GET", "POST", "DELETE"], request.method),
                path=request.url.path,
                query=tuple(request.url.params.multi_items()),
                headers=tuple(
                    (key, request.headers[key])
                    for key in ("anthropic-beta", "anthropic-version")
                    if key in request.headers
                ),
                request_json=json.loads(request.content) if request.content else None,
                status=response.status_code,
                response_json=response.json(),
            )
        )
        return response

    async with build_async_anthropic(
        api_key="offline",
        base_url="https://offline.invalid",
        headers={},
        max_retries=0,
        transport=httpx.MockTransport(dispatch),
    ) as client:
        actual_deps = deps(binding, client)
        revision = await ctx.revision(invocation)
        await ctx.prepare(actual_deps, invocation, revision, 30)
        replies.clear()
        await ctx.prepare(deps(binding, client), invocation, revision, 30)
    # Main #824 (0733181847dc775bda444d51c2467016780b8d5d): exactly one
    # session-scoped model-usage GET is part of the real product turn.
    turn_tape = anthropic_turn_tape(
        native_recipe.entry,
        native_recipe.scenario,
        native.turn,
        codec=native_recipe.codec,
        post_turn_usage_catchup=True,
    )
    catchups = [
        r
        for r in turn_tape.replies
        if r.method == "GET" and r.path == f"/v1/sessions/{pinned_session}/events"
    ]
    require(len(catchups) == 1, "usage catch-up missing or broadened")
    require(
        catchups[0].query
        == (
            ("beta", "true"),
            ("limit", "100"),
            ("order", "desc"),
            ("types[]", "span.model_request_end"),
        )
        and catchups[0].request_json is None,
        "usage catch-up request changed",
    )
    require(
        catchups[0].response_json
        == {
            "data": [e for e in native.turn.events if e["type"] == "span.model_request_end"],
            "next_page": None,
        },
        "usage catch-up lost recorded model-request spans",
    )
    whole_tape = AnthropicTape(
        source=turn_tape.source, codec=turn_tape.codec, replies=(*replies, *turn_tape.replies[1:])
    )
    record = ProductTurnRecipe(
        scenario_id=plan.scenario.id,
        scenario_sha256=plan.scenario.sha256,
        location=invocation.location,
        turn=invocation.turn or 1,
        tenant_id=binding.scope.tenant_id,
        channel_id=CHANNEL,
        thread_id="323456789012345678",
        external_user_id=ctx.external_user_id,
        session_id=pinned_session,
        root_turn_id=str(native.turn.events[0]["id"]),
        tape=whole_tape,
    )
    path = tmp_path / "product.json"
    path.write_text(record.model_dump_json())
    ctx.sources = {invocation.turn or 1: SourcePin(path=path.name, sha256=sha(path.read_bytes()))}
    result = await execute_cell(
        plan,
        replay(native_recipe),
        native_recipe=native_recipe,
        admin=binding,
        admin_values=VALUES,
        product_context=ctx,
    )
    require(len(result.evidence.turns) == 1, "public executor omitted the real prepared turn")
    evidence = result.evidence.turns[0]
    require(
        evidence.session_id == pinned_session and evidence.root_turn_id == record.root_turn_id,
        "public turn lost the pinned native identity",
    )
    require(
        evidence.terminal_capture_complete and evidence.terminals[0].outcome == "completed",
        "public turn lacks durable completion",
    )
    require(result.request_count == len(whole_tape.replies), "product requests not fully accounted")
    require(
        not any(g.code == "ADMIN_HOST_CONTEXT_UNBOUND" for g in result.gaps),
        "real prepared path was not bound",
    )
    ctx.verify_execution(invocation, evidence)
    if scenario_id == "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY":
        # Main #615 returns the entire real final state, not the old 1000-char tail.
        expected = "".join(
            str(block["text"])
            for event in native.turn.events
            if event["type"] == "agent.message"
            for block in event["content"]
            if isinstance(block, dict) and block.get("type") == "text"
        )
        require(len(expected) > 1000, "regression lacks long native final content")
        require(len(result.headless_outputs) == 1, "real headless return capture is missing")
        returned = result.headless_outputs[0].text
        require(
            returned == expected,
            "product return truncated or changed the real native final response",
        )


@pytest.mark.parametrize(
    "scenario_id", ["QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", "QA-D7-BIG-WORKSPACE-CHECKPOINT"]
)
async def test_exact_d1_d7_pair_uses_n6_resource_edge_and_real_readback(
    tmp_path: Path,
    binding: AdminBindings,
    scenario_id: str,
) -> None:
    import uuid

    from daimon.core.defaults.metadata import tenant_scoped_display_title
    from daimon.core.mux_compat import create_skill
    from daimon.core.skills.ingest import bundle_from_markdown
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    ctx = context(binding, tmp_path)
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id)
    original = entry.scenario
    operations = [original["steps"][3], original["teardown"][0]]
    values = {**VALUES, "env.qa_library_skill": "qa-library"}
    if binding.backend == "anthropic":
        payload = {
            "id": "skill_library_product",
            "type": "custom",
            "source": "custom",
            "display_title": tenant_scoped_display_title(
                tenant_id=uuid.UUID(binding.scope.tenant_id), name="qa-library"
            ),
            "latest_version": "1",
            "created_at": "2026-10-10T00:00:00Z",
            "updated_at": "2026-10-10T00:00:00Z",
        }
        binding.trace.queue(
            ScriptedReply("POST", "/v1/skills", httpx.Response(200, json=payload)),
            ScriptedReply(
                "GET",
                "/v1/skills",
                httpx.Response(200, json={"data": [payload], "next_page": None}),
            ),
        )
        bundle = bundle_from_markdown(
            "---\nname: qa-library\ndescription: Offline echo\n---\nEcho.\n"
        )
        created = await create_skill(
            binding.anthropic,
            display_title=str(payload["display_title"]),
            data=bundle.zip_bytes,
            scope=binding.scope,
        )
        values["env.qa_library_skill"] = created.id
    plan = planned(tmp_path, admin_row(operations), binding.backend)
    steps = await run_steps(
        plan,
        invoke=no_turn,
        prepare_channel=no_channel,
        admin=binding,
        admin_values=values,
        product_context=ctx,
    )
    if binding.backend == "anthropic":
        require(
            [r.status for r in steps.receipts] == ["executed", "executed"],
            "real source skill pair did not execute",
        )
        require(
            steps.receipts[0].admin is not None
            and steps.receipts[0].admin.observations["attached"] is True,
            "real attachment readback missing",
        )
        require(
            steps.receipts[1].admin is not None
            and steps.receipts[1].admin.observations["attached"] is False,
            "real removal readback missing",
        )
        require(
            not binding.trace.replies and not binding.trace.violations,
            "resource script not fully consumed",
        )
    else:
        require(
            steps.receipts[0].status == "pending"
            and steps.receipts[1].code == "DEPENDENCY_BLOCKED",
            "unsupported native resource host claimed execution",
        )
        require(not binding.trace.requests, "native resource gap called Anthropic")
    require(
        any(g.code == "NO_SCRIPTABLE_HOST_TURN" for g in steps.gaps),
        "skill pair acquired full-cell credit",
    )


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
async def test_shared_preparation_without_shared_binding_remains_pending(
    tmp_path: Path, binding: AdminBindings
) -> None:
    from daimon.core.channel_backend import set_channel_backend
    from mux.contracts.config import BackendConfig

    ctx = context(binding, tmp_path)
    plan = planned(
        tmp_path, admin_row([{"do": "mention", "channel": "A", "text": "hello"}]), binding.backend
    )
    i = plan.invocations[0]
    await ctx.configure(i)
    revision = await ctx.revision(i)
    async with binding.sessions() as db, db.begin():
        await set_channel_backend(db, revision.channel, BackendConfig(thread_mode="shared"))
    with pytest.raises(ProductContextPending, match="shared product preparation is unbound"):
        await ctx.prepare(deps(binding, binding.anthropic), i, await ctx.revision(i), 10)
    require(not binding.trace.requests, "shared binding gap called a native provider")
