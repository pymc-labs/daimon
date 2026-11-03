"""Scope-bound preparation inputs; only the product runner produces evidence."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import httpx
import yaml
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.stores.identity import find_platform_principal, get_or_create_platform_principal
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.deps import TurnDeps
from daimon.testing.anthropic_fixtures import (
    AnthropicTape,
    anthropic_turn_tape,
    load_anthropic_fixtures,
)
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.provider_fixtures import ScenarioFixture
from daimon.testing.provider_replay import Record, SourcePin, WireReply
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    combine_handlers,
    make_fake_memory_store_handler,
)
from mux.testing.isolated_sdk import build_async_anthropic
from pydantic import JsonValue, TypeAdapter, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from .anthropic_catalog_replay import AnthropicRecipe
from .catalog_product_context import ProductContext, ProductContextPending, ProductTurnRecipe
from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario, sha

_JSON = TypeAdapter[JsonValue](JsonValue)


class PreparationAvailability(Record):
    scenario_id: str
    backend: Literal["anthropic", "openai", "gemini"]
    status: Literal["AVAILABLE", "PENDING"]
    reason: str
    source: SourcePin
    authored_source: SourcePin
    native_input: SourcePin
    location: str | None = None


class PreparationIndex(Record):
    consumer: SourcePin
    publisher: SourcePin
    native_index: SourcePin
    cells: tuple[PreparationAvailability, ...]

    @model_validator(mode="after")
    def coverage(self) -> PreparationIndex:
        identities = {(c.scenario_id, c.backend) for c in self.cells}
        ids = {c.scenario_id for c in self.cells}
        if (
            len(self.cells) != 159
            or len(ids) != 53
            or identities
            != {(id_, backend) for id_ in ids for backend in ("anthropic", "openai", "gemini")}
        ):
            raise ValueError("preparation index must retain all 159 cells")
        return self


def build_preparation_index(*, catalog_root: Path, repository_root: Path) -> PreparationIndex:
    """Publish all availability decisions without DB/SDK allocation or verdicts."""
    manifest = repository_root / "packages/testing/fixtures/target53/anthropic/index.json"
    pack = load_anthropic_fixtures(manifest, source_root=repository_root)
    catalog = {e.id: e for e in load_catalog(catalog_root)}
    cells: list[PreparationAvailability] = []
    for entry in pack.index.entries:
        _, row = pack.select(entry.scenario_id)
        native = AnthropicRecipe(
            entry, row, pack.index.codec, manifest, repository_root, sha(manifest.read_bytes())
        )
        for backend in ("anthropic", "openai", "gemini"):
            plan = plan_scenario(
                catalog[entry.scenario_id].model_copy(update={"sha256": row.scenario_sha256}),
                catalog_root,
                backend,
                run_id="preparation-index",
            )
            cells.append(availability(plan, native, catalog_root=catalog_root))

    def pin(path: str) -> SourcePin:
        return SourcePin(path=path, sha256=sha((repository_root / path).read_bytes()))

    return PreparationIndex(
        consumer=pin("tests/judge/catalog_product_context.py"),
        publisher=pin("tests/judge/product_preparation_fixtures.py"),
        native_index=pin("packages/testing/fixtures/target53/anthropic/index.json"),
        cells=tuple(cells),
    )


def load_preparation_index(
    path: Path, *, catalog_root: Path, repository_root: Path
) -> PreparationIndex:
    saved = PreparationIndex.model_validate_json(path.read_bytes())
    saved.consumer.resolve(repository_root)
    saved.publisher.resolve(repository_root)
    saved.native_index.verify(repository_root)
    current = build_preparation_index(catalog_root=catalog_root, repository_root=repository_root)
    if saved.native_index != current.native_index or saved.cells != current.cells:
        raise ValueError("preparation index differs from current pinned source decisions")
    return saved


def availability(
    plan: ScenarioPlan, native: AnthropicRecipe, *, catalog_root: Path
) -> PreparationAvailability:
    """Conservative first-turn slice; AVAILABLE is input coverage, never PASS."""
    native.current()
    pinned = ScenarioFixture.model_validate_json(
        (native.manifest.parent.parent / native.entry.input.path).read_bytes()
    )
    path = f"scenarios/{plan.scenario.filename}"
    target = (catalog_root / path).resolve()
    if path != pinned.source.path or not target.is_relative_to(catalog_root.resolve()):
        raise ValueError("preparation source path differs from its pinned root")
    raw = target.read_bytes()
    digest = sha(raw)
    if (
        plan.scenario.sha256 != pinned.source.sha256
        or path != pinned.source.path
        or digest not in {pinned.source.sha256, pinned.projection.sha256}
    ):
        raise ValueError("preparation source pin changed or differs from the native input")
    source = SourcePin(path=path, sha256=digest)
    source.verify(catalog_root)
    original = plan.scenario
    document: object = yaml.safe_load(raw)
    if (
        original.load_error is not None
        or original.filename != f"{original.id}.yaml"
        or document != original.scenario
    ):
        raise ValueError("preparation document differs from its source bytes")
    canonical = plan_scenario(original, catalog_root, plan.backend, run_id="prep-validation")
    if (
        plan.invocations != canonical.invocations
        or plan.assertions != canonical.assertions
        or plan.global_assertions != canonical.global_assertions
        or plan.gaps != canonical.gaps
    ):
        raise ValueError("preparation plan differs from its authored operations")
    if plan.scenario.id != native.scenario.scenario_id:
        raise ValueError("preparation scenario differs from the pinned native source")
    reason = ""
    first = next((i for i in plan.invocations if i.operation == "host_turn"), None)
    if plan.backend != "anthropic":
        reason = "public preparation has no provider-native dispatch"
    else:
        native.validate(plan)
        if plan.scenario.scenario.get("setup"):
            reason = "authored setup needs a separately bound admin/resource recipe"
        elif any(
            i.operation == "unbound" for i in plan.invocations if i.location.startswith("steps[")
        ):
            reason = "authored resource/admin steps need a separately pinned setup recipe"
        elif first is None:
            reason = "no authored host turn in this scenario"
        elif first.turn != 1 or first.source.get("do") != "mention":
            reason = "first trigger requires an ingress/routing hook"
        elif any(
            i.operation != "new_channel" for i in plan.invocations[: plan.invocations.index(first)]
        ):
            reason = "pre-trigger operations need an owned execution binding"
        elif any(g.location in {"scenario", "human", first.location} for g in plan.gaps):
            reason = "first trigger has an unresolved catalog capability"
        elif any(
            g.location in {"scenario", "mcp", "deepwiki", first.location} for g in native.gaps
        ):
            reason = "first native turn has an unresolved fixture effect or completion gate"
        elif not any(b.turn == 1 for b in native.entry.bindings):
            reason = "first native session/root is unbound"
    return PreparationAvailability(
        scenario_id=plan.scenario.id,
        backend=plan.backend,
        status="PENDING" if reason else "AVAILABLE",
        reason=reason or "first turn has pinned native input and an empty authored setup",
        source=source,
        authored_source=pinned.source,
        native_input=native.entry.input,
        location=first.location if first else None,
    )


@asynccontextmanager
async def _rollback_sessions(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    require_owned_sessionmaker(sessions)  # Validate every routed bind before connecting.
    if sessions.kw.get("binds"):
        raise ProductContextPending("preparation recorder cannot roll back routed transactions")
    bind = sessions.kw.get("bind")
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as connection, _rollback_connection(connection) as scratch:
            yield scratch
    elif isinstance(bind, AsyncConnection):
        async with _rollback_connection(bind) as scratch:
            yield scratch
    else:
        raise ValueError("preparation recorder requires an owned asynchronous bind")


@asynccontextmanager
async def _rollback_connection(
    connection: AsyncConnection,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    transaction = (
        await connection.begin_nested() if connection.in_transaction() else await connection.begin()
    )
    try:
        yield async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
    finally:
        await transaction.rollback()


async def publish_preparation(
    context: ProductContext,
    plan: ScenarioPlan,
    native: AnthropicRecipe,
    *,
    catalog_root: Path,
    timeout_s: float = 30,
) -> SourcePin:
    """Record actual fresh public preparation, roll it back, publish exact input.

    The caller must provision credit and configure the owned channel through the
    public admin APIs. No tenant, destination, mapping or accepted root is invented.
    This first slice deliberately refuses follow-ups and unavailable setup/hooks.
    """
    selected = availability(plan, native, catalog_root=catalog_root)
    if selected.status == "PENDING":
        raise ProductContextPending(selected.reason)
    if context.admin.backend != plan.backend or context.repository_root != native.repository_root:
        raise ValueError("preparation context differs from the selected provider/repository")
    invocation = next(i for i in plan.invocations if i.location == selected.location)
    channel, thread = context.destination(invocation)
    source = native.turn(1)
    session_id = native.session_id(1)
    replies: list[WireReply] = []
    agent = ma_agent(
        id="agent_product_preparation",
        name=context.admin.default.agent_name or "daimon",
        model=native.scenario.agent_model,
        tenant_id=context.admin.scope.tenant_id,
    )
    environment = ma_environment(
        id="env_product_preparation",
        name=context.admin.default.environment_name or "default",
        tenant_id=context.admin.scope.tenant_id,
    )
    state = FakeSessionsState(ma=FakeMAState())
    state.ma.agents[agent.id] = agent.model_dump(mode="json")
    handle = combine_handlers(
        make_fake_sessions_handler(state),
        make_fake_memory_store_handler(),
        resolved_agent_env_router(agent, environment).dispatch,
    )

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            old = str(response.json()["id"])
            # This is the provider's CREATE response, not a seeded/adopted DB row.
            # Reuse the already authored native ID; do not rewrite send/SSE roots.
            state.sessions[session_id] = state.sessions.pop(old).model_copy(
                update={"id": session_id}
            )
            state.resources[session_id] = state.resources.pop(old, [])
            response = httpx.Response(200, json=state.sessions[session_id].model_dump(mode="json"))
        if request.method not in {"GET", "POST", "DELETE"}:
            raise ValueError("preparation fixture encountered an unbound SDK wire method")
        replies.append(
            WireReply(
                id=f"preparation-{len(replies)}",
                method=cast(Literal["GET", "POST", "DELETE"], request.method),
                path=request.url.path,
                query=tuple(request.url.params.multi_items()),
                headers=tuple(
                    (key, request.headers[key])
                    for key in ("anthropic-beta", "anthropic-version")
                    if key in request.headers
                ),
                request_json=_JSON.validate_json(request.content) if request.content else None,
                status=response.status_code,
                response_json=_JSON.validate_python(response.json()),
            )
        )
        return response

    # Shared SDK admission runs before acquiring a DB connection; no env values read.
    async with (
        build_async_anthropic(
            api_key="offline-preparation",
            base_url="https://preparation.invalid",
            headers={},
            max_retries=0,
            transport=httpx.MockTransport(dispatch),
        ) as client,
        _rollback_sessions(context.admin.sessions) as sessions,
    ):
        admin = replace(context.admin, sessions=sessions, resource_edge=None)
        scratch = replace(context, admin=admin, sources={})
        try:
            revision = await scratch.revision(invocation)
            async with sessions() as db:
                principal = await find_platform_principal(
                    db,
                    tenant_id=UUID(admin.scope.tenant_id),
                    platform=admin.platform,
                    external_id=context.external_user_id,
                )
            if principal is None:
                raise ProductContextPending(
                    "owned caller identity must be provisioned before recording"
                )
            async with sessions() as db:
                mapped = await get_live_thread_session(
                    db,
                    tenant_id=UUID(admin.scope.tenant_id),
                    platform=admin.platform,
                    thread_id=thread,
                    account_id=principal.account_id,
                )
            if mapped is not None:
                raise ProductContextPending("fresh preparation requires an empty owned thread")
            deps = TurnDeps(
                anthropic=client,
                sessionmaker=sessions,
                deployment_default=admin.default,
                resolver_cache=new_resolver_cache(),
                defaults_root=context.repository_root / "defaults",
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
            prepared = await scratch.prepare(deps, invocation, revision, timeout_s)
            if (
                prepared.reused
                or prepared.ma_session_id != session_id
                or not any(r.method == "POST" and r.path == "/v1/sessions" for r in replies)
            ):
                raise ProductContextPending("fresh preparation requires an empty owned thread")
        finally:
            await admin.close()
    # Main #824 (0733181847dc775bda444d51c2467016780b8d5d): the real turn
    # performs one scoped post-turn usage catch-up; replay it explicitly.
    turn = anthropic_turn_tape(
        native.entry,
        native.scenario,
        source.turn,
        codec=native.codec,
        post_turn_usage_catchup=True,
    )
    recipe = ProductTurnRecipe(
        scenario_id=plan.scenario.id,
        scenario_sha256=plan.scenario.sha256,
        location=invocation.location,
        turn=1,
        tenant_id=context.admin.scope.tenant_id,
        channel_id=channel,
        thread_id=thread,
        external_user_id=context.external_user_id,
        session_id=session_id,
        root_turn_id=str(source.turn.events[0]["id"]),
        tape=AnthropicTape(
            source=turn.source, codec=turn.codec, replies=(*replies, *turn.replies[1:])
        ),
    )
    # Check every dependency again before publishing a scope-bound input.
    availability(plan, native, catalog_root=catalog_root)
    context.source_root.mkdir(parents=True, exist_ok=True)
    path = context.source_root / f"{plan.scenario.id}-anthropic-turn-1.json"
    path.write_text(recipe.model_dump_json(indent=2) + "\n")
    return SourcePin(path=path.name, sha256=sha(path.read_bytes()))


async def provision_preparation_caller(
    context: ProductContext,
    plan: ScenarioPlan,
    native: AnthropicRecipe,
    *,
    catalog_root: Path,
) -> str:
    """Explicit setup: provision the real owned caller, never a session mapping.

    A rolled-back first admission would otherwise mint a different account UUID
    on replay and change the exact SDK metadata. Keep identity setup explicit.
    """
    selected = availability(plan, native, catalog_root=catalog_root)
    if selected.status == "PENDING":
        raise ProductContextPending(selected.reason)
    if context.admin.backend != plan.backend or context.repository_root != native.repository_root:
        raise ValueError("preparation context differs from the selected provider/repository")
    invocation = next(i for i in plan.invocations if i.location == selected.location)
    # Factory guards names before DB authorization; this client never sends.
    async with build_async_anthropic(
        api_key="offline-preparation",
        base_url="https://preparation.invalid",
        headers={},
        max_retries=0,
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    ):
        await context.revision(invocation)
        async with context.admin.sessions() as db, db.begin():
            row = await get_or_create_platform_principal(
                db,
                tenant_id=UUID(context.admin.scope.tenant_id),
                platform=context.admin.platform,
                external_id=context.external_user_id,
            )
    return str(row.account_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Source-pinned preparation availability, never verdicts"
    )
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog_root = cast(Path, args.catalog_root)
    repository_root = cast(Path, args.repository_root)
    output = cast(Path, args.output)
    index = build_preparation_index(catalog_root=catalog_root, repository_root=repository_root)
    output.write_text(index.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
