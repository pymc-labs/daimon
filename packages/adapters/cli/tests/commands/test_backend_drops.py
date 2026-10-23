"""The operator confirms the real source preview, never a guessed loss list."""

from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.agents import agents_backend_change
from daimon.core.authz import Subject
from daimon.core.channel_backend import BackendActor, load_backend_agent, make_backend_service
from daimon.core.config import Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import security_audit
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_memory_store_handler,
)
from rich.console import Console
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


@pytest.fixture
async def operator(db_preflight, db_nullpool_engine, db_clean, request):
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("This proof requires an owned database or isolated CI")
    async with sessions.begin() as db:
        platform = getattr(request, "param", "cli")
        tenant = await make_tenant(
            db, platform=platform, workspace_id="2001" if platform == "discord" else "local"
        )
        principal = await get_or_create_cli_principal(db, tenant_id=tenant.id, os_user="operator")
    settings = Settings.model_validate(
        {
            "database": {"url": str(db_nullpool_engine.url)},
            "anthropic": {"api_key": "offline-owned"},
            "cli": {"local_user": "operator"},
            "turn": {"path": "mux", "channel_backends": True, "agent_backends": True},
        }
    )
    agent = ma_agent(tenant_id=tenant.id, name="Research")
    agent.metadata["daimon_name"] = "Research"
    environment = ma_environment(tenant_id=tenant.id)
    environment.metadata["daimon_name"] = "Workspace"
    router, requests = MARouter(), []
    router.add(
        "GET",
        rf"/v1/agents/{agent.id}",
        lambda *_: httpx.Response(200, json=agent.model_dump(mode="json")),
    )
    router.add(
        "GET",
        rf"/v1/environments/{environment.id}",
        lambda *_: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    # Copies made by `daimon agents fork`, and every memory store, live here too.
    copies: dict[str, dict[str, object]] = {}
    memory = make_fake_memory_store_handler(FakeMemoryStoreState())

    def create_copy(request, _match):
        body = json.loads(request.content)
        made = ma_agent(
            id=f"agent_copy_{len(copies)}",
            name=body["name"],
            metadata=body["metadata"],
            system=body.get("system"),
            # The request's tool params aren't the response's shape; the
            # source's are, and nothing here reads the copy's tools.
            tools=agent.tools,
        )
        copies[made.id] = made.model_dump(mode="json")
        return httpx.Response(200, json=copies[made.id])

    router.add("POST", r"/v1/agents", create_copy)
    router.add(
        "GET",
        r"/v1/agents/(agent_copy_\d+)",
        lambda _request, match: httpx.Response(200, json=copies[match.group(1)]),
    )
    for method in ("GET", "POST"):
        router.add(method, r"/v1/memory_stores(/.*)?", lambda request, _match: memory(request))
    router.add(
        "GET",
        r"/v1/agents",
        lambda *_: list_response([agent.model_dump(mode="json"), *copies.values()]),
    )
    router.add(
        "GET", r"/v1/environments", lambda *_: list_response([environment.model_dump(mode="json")])
    )

    def transport(request):
        requests.append(request)
        return router.dispatch(request)

    async with build_fake_anthropic(transport) as client:
        rt = replace(
            build_cli_runtime(sessions, anthropic=client, settings=settings),
            deployment_default=DeploymentDefault(
                agent_name="Research", environment_name="Workspace"
            ),
        )
        service = make_backend_service(sessions, settings.turn)
        actor = BackendActor(tenant.id, principal.account_id, Subject(is_admin=True))
        policy = await service.tenant_policy(tenant.id)
        await service.set_tenant_allowlist(
            tenant.id, ("anthropic", "openai", "gemini"), policy.revision, actor
        )
        yield rt, tenant, agent, environment, requests


async def run(operator, **options):
    rt, *_ = operator
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend="openai",
        yes=True,
        as_json=False,
        **options,
    )
    return output.getvalue()


async def revisions(operator):
    rt, tenant, *_ = operator
    source = await load_backend_agent(
        rt.anthropic, rt.sessionmaker, tenant_id=tenant.id, agent_name="Research"
    )
    return await make_backend_service(rt.sessionmaker, rt.settings.turn).current(source)


@pytest.mark.parametrize(
    "accepted",
    [
        None,
        "",
        "memory",
        "sandbox",
        "memory,sandbox,files",
        "memory,memory,sandbox",
        "credentials",
        "*",
        "Memory,sandbox",
    ],
)
async def test_yes_never_substitutes_for_exact_drop_acceptance(operator, accepted):
    with pytest.raises(typer.BadParameter, match="memory,sandbox"):
        await run(operator, accept_drops=accepted)
    assert await revisions(operator) is None
    assert all(r.method == "GET" for r in operator[-1])


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_actual_source_gets_and_exact_consent_reach_cas_readback(operator, backend):
    rt, tenant, agent, _, requests = operator
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend=backend,
        yes=True,
        as_json=True,
        accept_drops=" sandbox , memory ",
    )
    result = json.loads(output.getvalue())
    assert result["preview"]["drop_consent"]["declared_drops"] == ["memory", "sandbox"]
    revision = await revisions(operator)
    assert revision.backend == backend and revision.declared_drops == ("memory", "sandbox")
    async with rt.sessionmaker() as db:
        rows = await security_audit.list_events(db, tenant_id=tenant.id, tool_name="agent_backend")
    assert rows[-1].reason == "completed; declared_drops=memory,sandbox"
    assert sum(r.url.path == f"/v1/agents/{agent.id}" for r in requests) >= 4
    assert all(r.method == "GET" for r in requests)


@pytest.mark.parametrize("backend", ["openai", "gemini"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_public_cli_can_switch_a_named_nondefault_agent_without_rerouting(
    operator, backend, dry_run
):
    from daimon.core.scope import ScopeContext
    from daimon.core.stores.scoped_config_read import resolve

    rt, tenant, _, _, requests = operator
    rt = replace(
        rt,
        deployment_default=rt.deployment_default.model_copy(update={"agent_name": "OtherDefault"}),
    )
    changed = (rt, *operator[1:])
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend=backend,
        yes=not dry_run,
        as_json=True,
        accept_drops="memory,sandbox",
        dry_run=dry_run,
    )
    result = json.loads(output.getvalue())
    preview = result if dry_run else result["preview"]
    assert preview["drop_consent"]["declared_drops"] == ["memory", "sandbox"]
    current = await revisions(changed)
    if dry_run:
        assert current is None
    else:
        assert current.backend == backend and current.declared_drops == ("memory", "sandbox")
    async with rt.sessionmaker() as db:
        config = await resolve(
            db,
            context=ScopeContext(tenant_id=tenant.id, platform="cli"),
            default=rt.deployment_default,
        )
    assert config.agent_name == "OtherDefault" and config.configuration_target_name is None
    assert all(request.method == "GET" for request in requests)


async def test_nonexplicit_capture_still_requires_the_resolved_responder(operator):
    from daimon.core.channel_backend import AgentBackendRefused

    rt, *_ = operator
    rt = replace(
        rt,
        deployment_default=rt.deployment_default.model_copy(update={"agent_name": "OtherDefault"}),
    )
    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await capture((rt, *operator[1:]))
    assert await revisions(operator) is None


@pytest.mark.parametrize("fault", ["wrong_name", "chat", "bound_channel", "bound_thread", "dm"])
async def test_explicit_cli_target_cannot_override_a_chat_or_mismatched_target(operator, fault):
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.scope import ScopeContext
    from daimon.core.stores.identity import get_or_create_cli_principal

    rt, tenant, *_ = operator
    async with rt.sessionmaker() as db:
        principal = await get_or_create_cli_principal(db, tenant_id=tenant.id, os_user="operator")
    context = ScopeContext(
        tenant_id=tenant.id,
        account_id=principal.account_id,
        platform="discord" if fault == "chat" else "cli",
        channel_id="bound" if fault == "bound_channel" else None,
        thread_id="bound" if fault == "bound_thread" else None,
    )
    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await capture(
            operator,
            context=context,
            explicit_target_name="OtherDefault" if fault == "wrong_name" else "Research",
            is_dm=fault == "dm",
        )
    assert await revisions(operator) is None


@pytest.mark.parametrize("operator", ["discord"], indirect=True)
@pytest.mark.parametrize("binding", ["valid", "wrong_name", "wrong_id"])
async def test_nonexplicit_chat_capture_keeps_the_stored_configuration_target_pair(
    operator, binding
):
    from daimon.core.authz import Place
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.scope import ScopeContext
    from daimon.core.stores.thread_agent_bindings import create_binding

    rt, tenant, native, *_ = operator
    async with rt.sessionmaker.begin() as db:
        principal = await get_or_create_cli_principal(db, tenant_id=tenant.id, os_user="operator")
        await create_binding(
            db,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            responder_ma_agent_id="other-responder",
            responder_name="OtherDefault",
            configuration_target_name="OtherTarget" if binding == "wrong_name" else "Research",
            configuration_target_ma_agent_id="other-target" if binding == "wrong_id" else native.id,
        )
    options = dict(
        context=ScopeContext(
            tenant_id=tenant.id,
            account_id=principal.account_id,
            platform="discord",
            channel_id="parent",
            thread_id="thread",
        ),
        place=Place(channel_id="thread", parent_channel_id="parent"),
    )
    if binding == "valid":
        assert (await capture(operator, **options)).name == "Research"
    else:
        with pytest.raises(AgentBackendRefused, match="source unavailable"):
            await capture(operator, **options)
    assert await revisions(operator) is None


@pytest.mark.parametrize("accepted", [None, "memory,sandbox", "*", "credentials"])
async def test_dry_run_returns_before_confirmation_and_writer_even_with_yes(operator, accepted):
    text = await run(operator, accept_drops=accepted, dry_run=True)
    assert "Won't be available on openai: memory, sandbox" in text
    assert await revisions(operator) is None


async def test_json_dry_run_needs_no_yes_or_drop_consent(operator):
    rt, *_ = operator
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output),
        name="Research",
        backend="openai",
        yes=False,
        as_json=True,
        dry_run=True,
    )
    assert json.loads(output.getvalue())["drop_consent"]["declared_drops"] == ["memory", "sandbox"]
    assert await revisions(operator) is None


async def test_same_category_source_drift_refuses_before_any_revision(operator, monkeypatch):
    from daimon.adapters.cli.commands import agents
    from daimon.core.channel_backend import AgentBackendRefused

    def changed(*args, **kwargs):
        operator[2].system = "Changed instructions, with the same capability categories"

    monkeypatch.setattr(agents, "confirm_or_abort", changed)
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await run(operator, accept_drops="memory,sandbox")
    assert await revisions(operator) is None
    assert all(r.method == "GET" for r in operator[-1])


async def test_cancelled_confirmation_does_not_change_the_setting(operator, monkeypatch):
    from daimon.adapters.cli.commands import agents

    def cancelled(*args, **kwargs):
        raise typer.Abort()

    monkeypatch.setattr(agents, "confirm_or_abort", cancelled)
    with pytest.raises(typer.Abort):
        await run(operator, accept_drops="memory,sandbox")
    assert await revisions(operator) is None


@pytest.mark.parametrize("dry_run", [False, True])
async def test_first_switch_revert_restores_default_or_only_previews_it(
    operator, monkeypatch, dry_run
):
    from daimon.adapters.cli.commands import agents

    rt, tenant, *_ = operator
    await run(operator, accept_drops="memory,sandbox")
    before = await revisions(operator)
    assert before.local == 1 and before.previous_local is None
    async with rt.sessionmaker() as db:
        audit_before = await security_audit.list_events(
            db, tenant_id=tenant.id, tool_name="agent_backend"
        )
    if dry_run:

        def unexpected_confirmation(*args, **kwargs):
            pytest.fail("A revert dry-run must return before confirmation")

        monkeypatch.setattr(agents, "confirm_or_abort", unexpected_confirmation)
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend=None,
        yes=not dry_run,
        as_json=True,
        revert=True,
        dry_run=dry_run,
    )
    result = json.loads(output.getvalue())
    restored = await revisions(operator)
    async with rt.sessionmaker() as db:
        audit_after = await security_audit.list_events(
            db, tenant_id=tenant.id, tool_name="agent_backend"
        )
    if dry_run:
        assert result["target_backend"] == "anthropic"
        assert restored == before and audit_after == audit_before
    else:
        assert result["backend"] is None and result["revision"] == before.local + 1
        assert restored.backend is None and restored.previous_local == before.local
        assert restored.declared_drops == () and restored.drop_consent is None
        assert len(audit_after) == len(audit_before) + 1
    assert all(request.method == "GET" for request in operator[-1])


async def test_first_switch_revert_keeps_fresh_source_digest_guard(operator, monkeypatch):
    from daimon.adapters.cli.commands import agents
    from daimon.core.channel_backend import AgentBackendRefused

    rt, _, native, *_ = operator
    await run(operator, accept_drops="memory,sandbox")
    before = await revisions(operator)

    def changed(*args, **kwargs):
        native.system = "Source changed while confirming the first revert"

    monkeypatch.setattr(agents, "confirm_or_abort", changed)
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            backend=None,
            yes=True,
            as_json=False,
            revert=True,
        )
    assert await revisions(operator) == before


async def test_first_switch_revert_keeps_owner_revision_cas(operator, monkeypatch):
    from daimon.core.channel_backend import AgentBackendRefused, AgentBackendService

    rt, *_ = operator
    await run(operator, accept_drops="memory,sandbox")
    before = await revisions(operator)
    original = AgentBackendService.revert

    async def concurrent_clear(self, agent, actor, **options):
        await self.clear_backend(
            agent,
            options["expected_revision"],
            actor,
            expected_preview_digest=options["expected_preview_digest"],
        )
        return await original(self, agent, actor, **options)

    monkeypatch.setattr(AgentBackendService, "revert", concurrent_clear)
    with pytest.raises(AgentBackendRefused, match="stale revision"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            backend=None,
            yes=True,
            as_json=False,
            revert=True,
        )
    raced = await revisions(operator)
    assert raced.local == before.local + 1 and raced.backend is None


async def test_revert_refuses_an_explicitly_referenced_missing_revision(operator, monkeypatch):
    from daimon.core.channel_backend import AgentBackendRefused, AgentBackendService

    rt, *_ = operator
    await run(operator, accept_drops="memory,sandbox")
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend="gemini",
        yes=True,
        as_json=False,
        accept_drops="memory,sandbox",
    )
    before = await revisions(operator)
    assert before.previous_local is not None

    async def missing(self, agent, local):
        assert local == before.previous_local
        return None

    monkeypatch.setattr(AgentBackendService, "revision", missing)
    with pytest.raises(AgentBackendRefused, match="no previous revision"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            backend=None,
            yes=True,
            as_json=False,
            revert=True,
        )
    assert await revisions(operator) == before


async def test_revert_restores_the_real_previous_setting_through_the_owner(operator):
    rt, *_ = operator
    await run(operator, accept_drops="memory,sandbox")
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend="gemini",
        yes=True,
        as_json=False,
        accept_drops="memory,sandbox",
    )
    before = await revisions(operator)
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend=None,
        yes=True,
        as_json=False,
        revert=True,
    )
    restored = await revisions(operator)
    assert restored.backend == "openai" and restored.local == before.local + 1
    assert restored.previous_local == before.local
    assert restored.declared_drops == ("memory", "sandbox")


async def test_revert_drift_and_missing_history_are_owner_refusals(operator):
    from daimon.core.channel_backend import AgentBackendRefused

    rt, _, native, *_ = operator
    with pytest.raises(AgentBackendRefused, match="no previous revision"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            backend=None,
            yes=True,
            as_json=False,
            revert=True,
        )
    await run(operator, accept_drops="memory,sandbox")
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend="gemini",
        yes=True,
        as_json=False,
        accept_drops="memory,sandbox",
    )
    before = await revisions(operator)
    native.system = "The source changed since the retained consent"
    with pytest.raises(AgentBackendRefused, match="re confirm"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            backend=None,
            yes=True,
            as_json=False,
            revert=True,
        )
    assert await revisions(operator) == before


async def capture(operator, **changes):
    from daimon.core.authz import Place
    from daimon.core.scope import ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.deps import build_turn_deps

    rt, tenant, *_ = operator
    async with rt.sessionmaker() as db:
        principal = await get_or_create_cli_principal(db, tenant_id=tenant.id, os_user="operator")
    source = await load_backend_agent(
        rt.anthropic, rt.sessionmaker, tenant_id=tenant.id, agent_name="Research"
    )
    deps = build_turn_deps(
        rt.settings,
        rt.anthropic,
        rt.sessionmaker,
        deployment_default=rt.deployment_default,
        resolver_cache=rt.resolver_cache,
        billing_config=None,
    )
    args = dict(
        context=ScopeContext(tenant_id=tenant.id, account_id=principal.account_id, platform="cli"),
        subject=Subject(is_admin=True),
        place=Place(),
        is_dm=False,
        slack_turn_context_id=None,
        private_dm_id=None,
        external_user_id=principal.os_user,
    )
    args.update(changes)
    return await capture_backend_source(deps, source, **args)


@pytest.mark.parametrize("private", ["slack_turn_context_id", "private_dm_id"])
async def test_actual_private_resource_intent_never_disappears_into_a_droppable_category(
    operator, private
):
    import uuid

    from daimon.core.turn.runtime_manifest import preview_source_drops
    from mux.contracts.ids import ModelRef
    from mux.errors import UnsupportedCapability

    baseline = await capture(operator)
    assert not any(r.kind == "native" for r in baseline.resource_requirements)
    captured = await capture(
        operator, **{private: uuid.uuid4() if private == "slack_turn_context_id" else "owned-dm"}
    )
    assert any(
        r.id == "caller-resources" and r.kind == "native" for r in captured.resource_requirements
    )
    with pytest.raises(UnsupportedCapability):
        preview_source_drops(captured, ModelRef(provider="openai", id="gpt-6-luna"))


@pytest.mark.parametrize("missing", ["target", "environment"])
async def test_unknown_target_or_environment_refuses_without_default_provisioning(
    operator, missing
):
    from daimon.core.channel_backend import AgentBackendRefused

    rt, *_ = operator
    if missing == "target":
        with pytest.raises(AgentBackendRefused, match="agent not found"):
            await agents_backend_change(
                rt=rt,
                console=Console(file=StringIO()),
                name="Unknown",
                backend="openai",
                yes=True,
                as_json=False,
                accept_drops="memory,sandbox",
            )
    else:
        settings = rt.deployment_default.model_copy(update={"environment_name": "Unknown"})
        changed = (replace(rt, deployment_default=settings), *operator[1:])
        with pytest.raises(AgentBackendRefused, match="source unavailable"):
            await run(changed, accept_drops="memory,sandbox")
    assert await revisions(operator) is None
    assert all(r.method == "GET" for r in operator[-1])


async def test_unresolved_host_place_and_foreign_account_are_typed_refusals(operator):
    import uuid

    from daimon.core.authz import Place
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.scope import ScopeContext

    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await capture(operator, place=Place(category_unresolved=True))
    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await capture(
            operator,
            context=ScopeContext(tenant_id=operator[1].id, account_id=uuid.uuid4(), platform="cli"),
        )
    assert await revisions(operator) is None


async def test_flag_off_refuses_before_source_or_dependency_construction(operator):
    from daimon.core.config import TurnSettings
    from daimon.core.errors import StoreError

    rt, *rest = operator
    rt = replace(rt, settings=rt.settings.model_copy(update={"turn": TurnSettings()}))
    operator[-1].clear()
    with pytest.raises(StoreError, match="disabled"):
        await run((rt, *rest), accept_drops="memory,sandbox")
    assert not operator[-1]


async def test_real_encrypted_key_is_lost_only_when_declared_without_any_secret_read(
    operator, monkeypatch
):
    import click
    from cryptography.fernet import Fernet
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_files

    rt, tenant, native, *_ = operator
    async with rt.sessionmaker.begin() as db:
        db.info["crypto_keys"] = (Fernet.generate_key().decode(),)
        await agent_files.put_agent_file(
            db,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=native.id),
            key="OWNED_API_KEY",
            content="offline-owned-credential",
            set_by_account_id=None,
        )

    async def forbidden_read(*args, **kwargs):
        pytest.fail("A switch preview must not resolve a secret")

    monkeypatch.setattr(agent_files, "get_agent_file", forbidden_read)
    # The saved key is a declared `credentials` loss: accepting less changes nothing.
    with pytest.raises(click.BadParameter, match="credentials"):
        await run(operator, accept_drops="memory,sandbox")
    assert await revisions(operator) is None


@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "not_found",
        "foreign_environment",
        "archived",
        "foreign_agent",
        "renamed_agent",
        "archived_agent",
    ],
)
async def test_actual_sdk_unreadable_or_foreign_source_is_a_typed_refusal(operator, fault):
    import uuid

    from daimon.core.channel_backend import AgentBackendRefused

    rt, _, agent, environment, requests = operator

    def broken(request):
        if request.url.path == f"/v1/agents/{agent.id}" and fault == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if request.url.path == f"/v1/agents/{agent.id}" and fault == "not_found":
            return httpx.Response(
                404,
                json={"type": "error", "error": {"type": "not_found_error", "message": "missing"}},
            )
        if request.url.path == f"/v1/environments/{environment.id}":
            data = environment.model_dump(mode="json")
            if fault == "foreign_environment":
                data["metadata"]["daimon_tenant"] = str(uuid.uuid4())
            elif fault == "archived":
                data["archived_at"] = "2026-10-14T00:00:00Z"
            return httpx.Response(200, json=data)
        if request.url.path == "/v1/agents":
            return list_response([agent.model_dump(mode="json")])
        if request.url.path == "/v1/environments":
            return list_response([environment.model_dump(mode="json")])
        if request.url.path == f"/v1/agents/{agent.id}":
            data = agent.model_dump(mode="json")
            if fault == "foreign_agent":
                data["metadata"]["daimon_tenant"] = str(uuid.uuid4())
            elif fault == "renamed_agent":
                data["metadata"]["daimon_name"] = "OtherDefault"
            elif fault == "archived_agent":
                data["archived_at"] = "2026-10-14T00:00:00Z"
            return httpx.Response(200, json=data)
        pytest.fail("Unexpected request")

    async with build_fake_anthropic(broken) as client:
        changed = (replace(rt, anthropic=client), *operator[1:])
        reason = (
            "agent not found"
            if fault in ("renamed_agent", "archived_agent")
            else "source unavailable"
        )
        with pytest.raises(AgentBackendRefused, match=reason):
            await run(changed, accept_drops="memory,sandbox")
    assert await revisions(operator) is None


@pytest.mark.parametrize("field", ["tenant_id", "agent_id"])
async def test_public_cli_refuses_a_captured_identity_mismatch(operator, monkeypatch, field):
    import uuid

    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.turn import runtime_manifest

    original = runtime_manifest.capture_preparation_source

    async def mismatched(*args, **kwargs):
        captured = await original(*args, **kwargs)
        return captured.model_copy(update={field: uuid.uuid4()})

    monkeypatch.setattr(runtime_manifest, "capture_preparation_source", mismatched)
    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await run(operator, accept_drops="memory,sandbox")
    assert await revisions(operator) is None


@pytest.mark.parametrize("operator", ["discord"], indirect=True)
async def test_cli_guild_selection_uses_its_real_local_operator_principal(operator):
    from daimon.adapters.cli.tenant import TenantSelector

    rt, tenant, *_ = operator
    # An explicit tenant selection exercises the same CLI-principal convention;
    # it must not create or promote a Discord platform principal.
    await run(operator, accept_drops="memory,sandbox", selector=TenantSelector(guild_id="2001"))
    assert (await revisions(operator)).declared_drops == ("memory", "sandbox")


async def test_default_denial_observation_keeps_the_real_external_user_id(operator):
    from datetime import UTC, datetime

    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores import access_policy
    from daimon.core.stores.domain import Role
    from daimon.core.turn.admission import admit
    from daimon.core.turn.deps import build_turn_deps
    from daimon.core.turn.errors import AdmissionDenied

    rt, tenant, *_ = operator
    async with rt.sessionmaker.begin() as db:
        await access_policy.set_access_policy(
            db, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_user_ids=("allowed-human",))
        )
    deps = build_turn_deps(
        rt.settings,
        rt.anthropic,
        rt.sessionmaker,
        deployment_default=rt.deployment_default,
        resolver_cache=rt.resolver_cache,
        billing_config=None,
    )
    deps = replace(deps, turn_path=None, channel_backends=False, agent_backends=False)
    decisions = []
    operator[-1].clear()
    with pytest.raises(AdmissionDenied):
        await admit(
            deps,
            tenant_id=tenant.id,
            requester_org=RequesterOrg.HOST,
            platform="discord",
            external_user_id="denied-human",
            channel_id="900",
            now=datetime.now(UTC),
            role=Role.USER,
            observer=decisions.append,
        )
    assert len(decisions) == 1 and decisions[0].external_user_id == "denied-human"
    assert decisions[0].reason == "invoker_not_allowed"
    assert not operator[-1]


async def test_missing_actual_cli_observation_label_is_a_typed_refusal(operator):
    from daimon.core.channel_backend import AgentBackendRefused

    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await capture(operator, external_user_id=None)
    assert await revisions(operator) is None


def test_disabled_revert_command_refuses_before_runtime_construction(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import click
    from daimon.adapters.cli.commands import agents
    from daimon.core.config import TurnSettings
    from daimon.core.errors import StoreError

    monkeypatch.setattr(agents, "load_settings", lambda: SimpleNamespace(turn=TurnSettings()))

    def forbidden_runtime(*args, **kwargs):
        pytest.fail("A disabled new command must not construct a runtime")

    monkeypatch.setattr(agents, "build_runtime", forbidden_runtime)
    monkeypatch.setattr(agents, "run_cli", lambda work, **kwargs: asyncio.run(work))
    with pytest.raises(StoreError, match="disabled"):
        agents.agents_backend_revert_command(typer.Context(click.Command("backend")), "Research")


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_recorded_portable_model_controls_are_visible_and_require_exact_consent(
    operator, backend
):
    from anthropic.types.beta import BetaManagedAgentsModelConfig

    rt, _, agent, _, _ = operator
    agent.model = BetaManagedAgentsModelConfig.model_validate(
        {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    )
    text = await run(operator, dry_run=True)
    assert "Won't be available on openai: memory, model_controls, sandbox" in text
    with pytest.raises(typer.BadParameter, match="memory,model_controls,sandbox"):
        await run(operator, accept_drops="memory,sandbox")
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend=backend,
        yes=True,
        as_json=True,
        accept_drops="memory,model_controls,sandbox",
    )
    result = json.loads(output.getvalue())
    assert result["preview"]["drop_consent"]["declared_drops"] == [
        "memory",
        "model_controls",
        "sandbox",
    ]
    assert (await revisions(operator)).declared_drops == ("memory", "model_controls", "sandbox")


@pytest.mark.parametrize("accepted", [None, "", " "])
async def test_native_no_drop_switch_binds_empty_consent_without_loss_flags(operator, accepted):
    from anthropic.types.beta import BetaManagedAgentsModelConfig

    rt, _, agent, _, requests = operator
    agent.model = BetaManagedAgentsModelConfig.model_validate(
        {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    )
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend="anthropic",
        yes=True,
        as_json=True,
        accept_drops=accepted,
    )
    assert json.loads(output.getvalue())["preview"]["drop_consent"]["declared_drops"] == []
    revision = await revisions(operator)
    assert revision.backend == "anthropic" and revision.drop_consent is not None
    assert revision.declared_drops == ()
    assert all(request.method == "GET" for request in requests)


async def test_revert_restores_the_previous_model_through_its_own_preview_digest(operator):
    rt, *_ = operator

    async def change(**options):
        output = StringIO()
        await agents_backend_change(
            rt=rt,
            console=Console(file=output, width=240),
            name="Research",
            yes=True,
            as_json=True,
            **options,
        )
        return json.loads(output.getvalue())

    argon = await change(backend="gemini", model="gemini-4-argon", accept_drops="memory,sandbox")
    assert argon["model"] == "gemini-4-argon"
    assert argon["preview"]["target_model"] == "gemini-4-argon"
    await change(backend="openai", accept_drops="memory,sandbox")
    reverted = await change(backend=None, revert=True)
    # The revert's preview is the previous model's, so its digest matches the write.
    assert reverted["preview"]["target_model"] == "gemini-4-argon"
    restored = await revisions(operator)
    assert (restored.backend, restored.model) == ("gemini", "gemini-4-argon")


@pytest.mark.parametrize(
    "options",
    [
        {"backend": None, "model": "gemini-4-argon"},
        {"backend": None, "model": "gemini-4-argon", "revert": True},
        {"backend": "gemini", "model": "gemini-3.5-flash-lite"},
        {"backend": "openai", "model": "gemini-4-argon"},
    ],
)
async def test_model_is_refused_where_it_does_not_apply(operator, options):
    rt, *_ = operator
    with pytest.raises(typer.BadParameter, match="--model"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            yes=True,
            as_json=False,
            **options,
        )
    assert await revisions(operator) is None


async def test_agent_settings_are_previewed_written_and_reverted(operator):
    from daimon.core.channel_backend import AgentSettings
    from mux.contracts.config import CapabilityRequirement

    rt, *_ = operator
    output = StringIO()
    await agents_backend_change(
        rt=rt,
        console=Console(file=output, width=240),
        name="Research",
        backend=None,
        yes=True,
        as_json=False,
        settings=AgentSettings(thread_mode="shared"),
    )
    assert 'Agent settings: {"thread_mode": "shared"}' in output.getvalue()
    shared = await revisions(operator)
    assert shared.backend is None and shared.thread_mode == "shared"
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend=None,
        yes=True,
        as_json=False,
        settings=AgentSettings(),
    )
    await run(operator, accept_drops="memory,sandbox")
    # On OpenAI a settings change names the provider's drops again.
    needs = AgentSettings(requires={"tool_loop": CapabilityRequirement(level="required")})
    with pytest.raises(typer.BadParameter, match="memory,sandbox"):
        await agents_backend_change(
            rt=rt,
            console=Console(file=StringIO()),
            name="Research",
            backend=None,
            yes=True,
            as_json=False,
            settings=needs,
        )
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend=None,
        yes=True,
        as_json=False,
        settings=needs,
        accept_drops="memory,sandbox",
    )
    required = await revisions(operator)
    assert required.backend == "openai" and required.requires == needs.requires
    await agents_backend_change(
        rt=rt,
        console=Console(file=StringIO()),
        name="Research",
        backend=None,
        yes=True,
        as_json=False,
        revert=True,
    )
    reverted = await revisions(operator)
    assert reverted.backend == "openai" and reverted.requires is None


async def test_the_operator_assigns_shows_and_clears_an_owner(operator):
    from daimon.adapters.cli.commands.agents import agents_owner
    from daimon.core.agent_ownership import agent_owner
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.testing.factories import make_account, make_platform_principal

    rt, tenant, agent, *_ = operator
    async with rt.sessionmaker.begin() as db:
        person = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, tenant=tenant, account=person, platform="discord", external_id="42"
        )
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    output = StringIO()
    console = Console(file=output, width=240)
    await agents_owner(rt=rt, console=console, name="Research")
    assert "no owner (admins only)" in output.getvalue()
    with pytest.raises(typer.BadParameter):
        await agents_owner(rt=rt, console=console, name="Research", change="set", user="42")
    await agents_owner(
        rt=rt,
        console=console,
        name="Research",
        change="set",
        platform="discord",
        user="42",
        yes=True,
    )
    assert await agent_owner(rt.sessionmaker, tenant_id=tenant.id, agent_id=agent_id) == person.id
    await agents_owner(rt=rt, console=console, name="Research", change="clear", yes=True)
    assert await agent_owner(rt.sessionmaker, tenant_id=tenant.id, agent_id=agent_id) is None
    async with rt.sessionmaker() as db:
        rows = await security_audit.list_events(db, tenant_id=tenant.id, tool_name="agent_owner")
    assert sorted(row.operation for row in rows) == ["assign", "clear"]


async def test_a_cli_fork_of_a_switched_agent_with_memory_keeps_a_consent_its_turns_accept(
    operator,
):
    """The copy's consent is pinned to the copy as its turns will capture it,
    shared memory store included, after everything the fork does (its face too)."""
    from daimon.adapters.cli.commands.agents import agents_fork
    from daimon.core.authz import Place
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.memory_resource import ensure_memory_store_and_mount
    from daimon.core.mux_backend import resource_scope
    from daimon.core.mux_compat import write_memory
    from daimon.core.scope import ScopeContext
    from daimon.core.stores import agent_backends
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.deps import build_turn_deps
    from daimon.core.turn.runtime_manifest import source_preservation_pin

    rt, tenant, agent, *_ = operator
    await run(operator, accept_drops="memory,sandbox")
    source_setting = await revisions(operator)
    assert source_setting.backend == "openai" and source_setting.drop_consent is not None
    mount = await ensure_memory_store_and_mount(
        rt.anthropic,
        rt.sessionmaker,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id),
        agent_name="Research",
    )
    await write_memory(
        rt.anthropic,
        mount["memory_store_id"],
        "/conventions.md",
        "tabs",
        scope=resource_scope(tenant_id=str(tenant.id)),
    )
    output = StringIO()
    await agents_fork(rt=rt, console=Console(file=output, width=240), src="Research", dst="Copy")

    copy = await load_backend_agent(
        rt.anthropic, rt.sessionmaker, tenant_id=tenant.id, agent_name="Copy"
    )
    async with rt.sessionmaker() as db:
        setting = await agent_backends.current(db, tenant.id, copy.agent_id)
        principal = await get_or_create_cli_principal(db, tenant_id=tenant.id, os_user="operator")
    assert setting is not None and setting.backend == "openai"
    assert setting.drop_consent is not None
    assert setting.declared_drops == ("memory", "sandbox")
    deps = build_turn_deps(
        rt.settings,
        rt.anthropic,
        rt.sessionmaker,
        deployment_default=rt.deployment_default,
        resolver_cache=rt.resolver_cache,
        billing_config=None,
    )
    captured = await capture_backend_source(
        deps,
        copy,
        context=ScopeContext(tenant_id=tenant.id, account_id=principal.account_id, platform="cli"),
        subject=Subject(is_admin=True),
        place=Place(),
        is_dm=False,
        slack_turn_context_id=None,
        private_dm_id=None,
        external_user_id=principal.os_user,
        explicit_target_name="Copy",
    )
    assert source_preservation_pin(captured) == setting.drop_consent.source_pin
    async with rt.sessionmaker() as db:
        rows = await security_audit.list_events(
            db, tenant_id=tenant.id, agent_id=copy.agent_id, tool_name="agent_backend"
        )
    assert [(r.operation, r.account_id) for r in rows] == [("copy", principal.account_id)]
    assert "It runs on OpenAI, like Research." in output.getvalue()
    assert "It has a copy of Research's shared memory." in output.getvalue()
