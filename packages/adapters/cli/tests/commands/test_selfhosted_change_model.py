"""Moving a selfhosted deployment to another model moves its existing conversations too.

Registration runs as `register.sh` runs it: the chart's `provision.py`, then
`initialize` for each tenant, then `runtime_grants.py` rows in the runtime file.
Each message goes through real admission and selfhosted preparation; only the
tools and skills a session plan assembles are left out, and the harness has no events.
"""

from __future__ import annotations

import asyncio
import json
import runpy
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from daimon.adapters.cli.commands import selfhosted
from daimon.adapters.cli.commands.selfhosted import (
    ModelChange,
    ModelChangePlan,
    ModelChangeRefused,
    apply_model_change,
    plan_model_change,
)
from daimon.adapters.cli.main import app
from daimon.core.channel_backend import channel_ref, clear_channel_backend, set_channel_backend
from daimon.core.config import SelfhostedBillingSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.purge import purge_account
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.stores import mux_state
from daimon.core.stores.access_policy import policy_write_transaction
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.thread_release import pending_releases
from daimon.core.turn import selfhosted_host
from daimon.core.turn.admission import admit
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from daimon.core.turn.selfhosted_deployment import build_selfhosted_runtime, selfhosted_config
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.turn_fakes import deps_for_pinned_mux
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentPatch, ProviderBinding, SessionSpec
from mux.drivers.selfhosted import HarnessConfig
from mux.errors import ScopeViolation
from mux.state.store import is_released
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

SCRIPTS = Path(__file__).resolve().parents[5] / "deploy/helm/daimon-selfhost/scripts"
OLD, NEW, OTHER = "gpt-5.4-mini", "gpt-6.1-sol", "gpt-other"
ENTRA_TENANT = "33333333-3333-4333-8333-333333333333"
TEAMS = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT)
CHANNEL = "19:pilot@thread.tacv2"
LATER = "19:later@thread.v2"
CLEARED = "19:cleared@thread.tacv2"
CHAT = "a:1personal-chat"
THREAD = f"{CHANNEL};messageid=1"
PINNED = "19:anthropic@thread.tacv2"
ELSEWHERE = "19:elsewhere@thread.tacv2"
PERSON = "0f4e2c1a-9b7d-4e5f-8a6b-1c2d3e4f5a6b"
OTHER_PERSON = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
HARNESS = HarnessConfig(
    harness_url="https://harness.example", auth_ref="DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY"
)
CHANGE = ModelChange(tenant_id=TEAMS, from_model=OLD, model=NEW)
# Everything the model change may write.
TABLES = (
    "channel_config_revision",
    "provider_binding_slot",
    "provider_binding",
    "thread_provider_release",
    "selfhosted_agents",
    "selfhosted_resource_operations",
)


@pytest.fixture(autouse=True)
async def runtime_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Selfhosted admission on, both models priced, and the grants file the turns read."""
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    runtime = tmp_path / "runtime.json"
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(runtime))
    tariff = [{"effective_from": "2026-01-01", "input": "1", "output": "4", "cache_read": "0.1"}]
    billing = SelfhostedBillingSettings.model_validate(
        {
            "allow_test_tariffs": True,
            "deployments": {model: f"{model}-TEST" for model in (OLD, NEW)},
            "models": {f"{model}-TEST": tariff for model in (OLD, NEW)},
        }
    )
    monkeypatch.setattr(
        "daimon.core.config.load_settings",
        lambda: SimpleNamespace(billing=SimpleNamespace(selfhosted=billing)),
    )
    yield runtime
    await drain_outcomes()


async def register(
    sf: async_sessionmaker[AsyncSession], model: str, grants: dict[str, list[str]], runtime: Path
) -> dict[str, Any]:
    """What `register.sh` writes, short of upgrading the release."""
    # Installing the Teams app created the tenant with its sign-up credit.
    await provision_tenant(
        sf, platform="teams", workspace_id=ENTRA_TENANT, signup_credit=Decimal("10")
    )
    registration = await runpy.run_path(str(SCRIPTS / "provision.py"))["provision"](
        sf,
        {
            "SELFHOSTED_MODEL": model,
            "DAIMON_TEAMS__TENANT_ID": ENTRA_TENANT,
            "TEAMS_GRANTS": json.dumps(grants),
        },
    )
    rows = runpy.run_path(str(SCRIPTS / "runtime_grants.py"))
    ids = {
        tenant: list(
            await selfhosted.initialize(
                sf,
                tenant_id=UUID(tenant),
                name="daimon",
                model=model,
                system=None,
                environment_name="default",
                harness=HARNESS,
            )
        )
        for tenant in rows["tenants"](registration)
    }
    runtime.write_text(json.dumps(rows["rows"](registration, ids)))
    return registration


class NoEvents:
    """A harness with nothing to replay, which is all a reused session reads here."""

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        assert method == "GET" and "/events?" in path
        return {"events": [], "complete": True}


class NoAnthropic:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"a selfhosted message reached Anthropic: {name}")


async def answer(
    sf: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
    caller: str,
    *,
    is_dm: bool = False,
) -> PreparedTurn:
    """Admit one message and prepare its selfhosted session, as the turn path does."""
    deps = deps_for_pinned_mux(NoAnthropic(), sf)
    now = datetime.now(UTC)
    admission = await admit(
        deps,
        tenant_id=tenant_id,
        platform=platform,
        external_user_id=caller,
        channel_id=channel_id,
        thread_id=thread_id,
        now=now,
        requester_org=RequesterOrg.HOST,
        origin="chat",
        is_dm=is_dm,
    )
    request = ProviderPreparationRequest(
        deps=deps,
        admission=admission,
        scope=resource_scope(
            tenant_id=str(tenant_id), account_id=str(admission.account_id), authorization_id="turn"
        ),
        tenant_id=tenant_id,
        platform=platform,
        external_user_id=caller,
        thread_id=thread_id,
        session_account_id=admission.account_id,
        reuse_existing=True,
        capabilities=DEFAULT_MA_CAPABILITIES,
        transfer=None,
        deadline=now + timedelta(minutes=5),
        now=lambda: datetime.now(UTC),
    )
    runtime = build_selfhosted_runtime(request)

    async def plan(preparation: ProviderPreparationRequest) -> SessionSpec:
        """The real plan's record checks, without its tools and skills."""
        revision, scope = preparation.admission.backend_revision, preparation.scope
        assert revision is not None

        def ref(kind: str, id_: str) -> ResourceRef:
            return ResourceRef(
                id=id_,
                kind=kind,
                provider="selfhosted",
                account_scope_id=scope.tenant_id,
                tenant_id=scope.tenant_id,
            )

        agent = await runtime.agents.retrieve(scope, ref("agent", preparation.admission.agent.id))
        environment = await runtime.environments.retrieve(
            scope, ref("environment", preparation.admission.environment.id)
        )
        if agent.spec.model.id != revision.model:
            raise AdmissionDenied(reason="backend_unsupported")
        return SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=revision.local,
        )

    runtime = replace(runtime, session_plan=plan, transport_factory=lambda *_: NoEvents())
    deps = replace(deps, turn_runtimes={"selfhosted.loop": runtime})
    return await selfhosted_host.prepare_selfhosted(replace(request, deps=deps))


def model_of(prepared: PreparedTurn) -> tuple[str | None, int]:
    revision = prepared.admission.backend_revision
    assert revision is not None
    return revision.model, revision.local


async def snapshot(sf: async_sessionmaker[AsyncSession]) -> dict[str, list[str]]:
    async with sf() as session:
        return {
            table: sorted(map(repr, await session.execute(text(f"SELECT * FROM {table}"))))
            for table in TABLES
        }


async def move(sf: async_sessionmaker[AsyncSession], change: ModelChange) -> ModelChangePlan:
    plan = await plan_model_change(sf, change)
    await apply_model_change(sf, change, plan)
    return plan


@pytest.mark.parametrize("register_first", [False, True])
async def test_old_threads_new_threads_and_new_conversations_answer_on_the_new_model(
    db_session_factory: async_sessionmaker[AsyncSession], runtime_file: Path, register_first: bool
) -> None:
    sf = db_session_factory
    cli = UUID((await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file))["tenant_id"])
    old_thread = await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
    operator = await answer(sf, cli, "cli", "selfhosted-operator", "aks-proof", "operator")
    assert model_of(old_thread) == model_of(operator) == (OLD, 1)
    if register_first:
        # Registering the new model first moves the granted conversations, then
        # refuses because the agent is still on the old model.
        with pytest.raises(ValueError, match="different configuration"):
            await register(sf, NEW, {CHANNEL: [PERSON]}, runtime_file)

    for change in (replace(CHANGE, tenant_id=cli), CHANGE):
        plan = await move(sf, change)
        assert [len(c.threads) for c in plan.conversations] == [1]
        assert [agent.name for agent in plan.agents] == ["daimon"]
        # Run again, the dry run finds nothing left on the old model.
        left = await plan_model_change(sf, change)
        assert (left.agents, left.conversations) == ((), ())
    await register(sf, NEW, {CHANNEL: [PERSON], LATER: [PERSON]}, runtime_file)

    # The old thread starts a new session on the new model, on the one revision
    # the conversation has for it.
    again = await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
    assert model_of(again) == (NEW, 2)
    assert again.ma_session_id != old_thread.ma_session_id
    new_thread = await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=2", PERSON)
    assert model_of(new_thread) == (NEW, 2)
    new_conversation = await answer(sf, TEAMS, "teams", LATER, LATER, PERSON)
    assert model_of(new_conversation) == (NEW, 1)
    operator_again = await answer(sf, cli, "cli", "selfhosted-operator", "aks-proof", "operator")
    assert model_of(operator_again) == (NEW, 2)
    assert operator_again.ma_session_id != operator.ma_session_id


async def test_a_dry_run_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], runtime_file: Path
) -> None:
    sf = db_session_factory
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
    before = await snapshot(sf)
    plan = await plan_model_change(sf, CHANGE)
    assert [(c.channel_id, len(c.threads)) for c in plan.conversations] == [(CHANNEL, 1)]
    assert await snapshot(sf) == before


async def test_a_list_must_name_every_conversation_sharing_the_moving_agents(
    db_session_factory: async_sessionmaker[AsyncSession], runtime_file: Path
) -> None:
    sf = db_session_factory
    await register(sf, OLD, {CHANNEL: [PERSON], LATER: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    before = await snapshot(sf)
    with pytest.raises(ModelChangeRefused, match=f"list every conversation on {OLD}; missing: "):
        await plan_model_change(sf, replace(CHANGE, conversations=(LATER,)))
    assert await snapshot(sf) == before
    assert model_of(await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)) == (OLD, 1)
    plan = await move(sf, replace(CHANGE, conversations=(LATER, CHANNEL)))
    assert [c.channel_id for c in plan.conversations] == [LATER, CHANNEL]


async def edit_agent(sf: async_sessionmaker[AsyncSession]) -> None:
    scope = Scope(
        tenant_id=str(TEAMS),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )
    agents = SelfHostedAgents(sf)
    agent = await agents.resolve(scope, "daimon")
    await agents.update(
        scope, agent.ref, AgentPatch(system=agent.spec.system), expected=agent.revision, key="edit"
    )


async def move_thread(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf.begin() as session:
        (binding,) = await mux_state.channel_bindings(session, channel_ref(TEAMS, "teams", CHANNEL))
        await mux_state.put_binding(
            session,
            binding.model_copy(update={"generation": binding.generation + 1}),
            expected_generation=binding.generation,
        )


async def clear_conversation(sf: async_sessionmaker[AsyncSession]) -> None:
    async with policy_write_transaction(sf, tenant_id=TEAMS) as session:
        await clear_channel_backend(session, channel_ref(TEAMS, "teams", CHANNEL))


async def configure(sf: async_sessionmaker[AsyncSession], config: BackendConfig) -> None:
    async with policy_write_transaction(sf, tenant_id=TEAMS) as session:
        await set_channel_backend(session, channel_ref(TEAMS, "teams", CHANNEL), config)


async def configure_other_model(sf: async_sessionmaker[AsyncSession]) -> None:
    await configure(sf, selfhosted_config(OTHER))


@pytest.mark.parametrize("since", [move_thread, edit_agent, clear_conversation])
async def test_apply_refuses_a_plan_that_changed_since_the_dry_run(
    db_session_factory: async_sessionmaker[AsyncSession], runtime_file: Path, since: Any
) -> None:
    sf = db_session_factory
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
    plan = await plan_model_change(sf, CHANGE)
    await since(sf)
    before = await snapshot(sf)
    with pytest.raises(ModelChangeRefused, match="changed since the dry run; nothing was written"):
        await apply_model_change(sf, CHANGE, plan)
    assert await snapshot(sf) == before


async def test_an_agent_edited_during_the_change_is_left_for_the_next_dry_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = db_session_factory
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
    plan = await plan_model_change(sf, CHANGE)
    update = SelfHostedAgents.update

    async def edited_first(self: SelfHostedAgents, *args: Any, **kwargs: Any) -> object:
        monkeypatch.setattr(SelfHostedAgents, "update", update)
        await edit_agent(sf)
        return await update(self, *args, **kwargs)

    monkeypatch.setattr(SelfHostedAgents, "update", edited_first)
    with pytest.raises(ModelChangeRefused, match="run the dry run again to move the agent"):
        await apply_model_change(sf, CHANGE, plan)
    left = await move(sf, CHANGE)
    assert ([agent.name for agent in left.agents], left.conversations) == (["daimon"], ())
    assert (await plan_model_change(sf, CHANGE)).agents == ()


async def test_cleared_anthropic_and_other_model_conversations_stay_as_they_are(
    db_session_factory: async_sessionmaker[AsyncSession], runtime_file: Path
) -> None:
    sf = db_session_factory
    registration = await register(sf, OLD, {CHANNEL: [PERSON], CLEARED: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
    await answer(sf, TEAMS, "teams", CLEARED, f"{CLEARED};messageid=1", PERSON)
    async with policy_write_transaction(sf, tenant_id=TEAMS) as session:
        # An admin clears one conversation, pins one to Anthropic and one to another model.
        await clear_channel_backend(session, channel_ref(TEAMS, "teams", CLEARED))
        await set_channel_backend(session, channel_ref(TEAMS, "teams", PINNED), BackendConfig())
        await set_channel_backend(
            session, channel_ref(TEAMS, "teams", ELSEWHERE), selfhosted_config(OTHER)
        )
        # On a release with an Anthropic key, a thread answered before the
        # conversation moved to the selfhosted model keeps its Anthropic session.
        caller = await get_or_create_platform_principal(
            session, tenant_id=TEAMS, platform="teams", external_id=OTHER_PERSON
        )
        await mux_state.put_binding(
            session,
            ProviderBinding(
                id="anthropic-thread",
                thread=ThreadRef(
                    channel=channel_ref(TEAMS, "teams", CHANNEL),
                    thread_id=f"{CHANNEL};messageid=0",
                ),
                provider="anthropic",
                profile="anthropic.managed_agents",
                native_refs={"session": "sesn_anthropic"},
                generation=1,
                config_revision=0,
                legacy_account_id=str(caller.account_id),
            ),
            expected_generation=0,
        )

    async def untouched() -> list[object]:
        async with sf() as session:
            found: list[object] = []
            for channel_id in (CLEARED, PINNED, ELSEWHERE):
                channel = channel_ref(TEAMS, "teams", channel_id)
                found.append(await mux_state.latest_config_revision(session, channel))
                found.append(await mux_state.channel_bindings(session, channel))
            moved = await mux_state.channel_bindings(session, channel_ref(TEAMS, "teams", CHANNEL))
            found.append([binding for binding in moved if binding.provider == "anthropic"])
            return found

    before = await untouched()
    with pytest.raises(ModelChangeRefused, match=f"not on {OLD}: {CLEARED}"):
        await plan_model_change(sf, replace(CHANGE, conversations=(CHANNEL, CLEARED)))
    plan = await move(sf, CHANGE)
    assert [(c.channel_id, len(c.threads)) for c in plan.conversations] == [(CHANNEL, 1)]
    assert await untouched() == before
    async with sf() as session:
        moved = await mux_state.channel_bindings(session, channel_ref(TEAMS, "teams", CHANNEL))
        assert await pending_releases(session, tenant_id=TEAMS) == []
    assert sorted((binding.provider, is_released(binding)) for binding in moved) == [
        ("anthropic", False),
        ("selfhosted", True),
    ]

    # Registration sets every conversation it grants, so a cleared one stays
    # cleared only while it is left out of the grants. It registers the CLI
    # tenant too, so that tenant moves first.
    await move(sf, replace(CHANGE, tenant_id=UUID(registration["tenant_id"])))
    await register(sf, NEW, {CHANNEL: [PERSON]}, runtime_file)
    assert await untouched() == before
    await register(sf, NEW, {CHANNEL: [PERSON], CLEARED: [PERSON]}, runtime_file)
    async with sf() as session:
        regranted = await mux_state.latest_config_revision(
            session, channel_ref(TEAMS, "teams", CLEARED)
        )
    assert regranted is not None and regranted.model == NEW


def test_cli_prints_a_plan_and_applies_only_that_plan(
    db_nullpool_engine: Any, db_clean: None, monkeypatch: pytest.MonkeyPatch, runtime_file: Path
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)

    async def seed() -> None:
        await register(sessions, OLD, {CHANNEL: [PERSON]}, runtime_file)
        await answer(sessions, TEAMS, "teams", CHANNEL, f"{CHANNEL};messageid=1", PERSON)
        await drain_outcomes()

    asyncio.run(seed())
    monkeypatch.setattr(selfhosted, "build_engine", lambda url: db_nullpool_engine)
    runner = CliRunner()
    args = [
        "selfhosted",
        "change-model",
        "--tenant-id",
        str(TEAMS),
        "--from-model",
        OLD,
        "--model",
        NEW,
        "--database-url",
        "postgresql+asyncpg://localhost/test",
    ]
    dry = runner.invoke(app, args)
    assert dry.exit_code == 0, dry.output
    assert "nothing was written" in dry.stderr
    plan = ModelChangePlan.model_validate_json(dry.stdout)
    stale = plan.model_copy(update={"agents": ()}).model_dump_json()
    refused = runner.invoke(app, [*args, "--apply", "-"], input=stale)
    assert refused.exit_code == 1
    assert "Refused: what is on gpt-5.4-mini changed since the dry run" in refused.stderr
    applied = runner.invoke(app, [*args, "--apply", "-"], input=dry.stdout)
    assert applied.exit_code == 0, applied.output
    assert applied.stdout.startswith(
        "Moved to gpt-6.1-sol: 1 agent(s), 1 conversation(s); "
        "1 thread(s) start a new session at their next message."
    )
    again = runner.invoke(app, args)
    assert ModelChangePlan.model_validate_json(again.stdout).conversations == ()
    same = runner.invoke(app, [*args, "--model", OLD])
    assert same.exit_code == 2
    assert "name two different models" in same.stderr


@pytest.mark.parametrize("register_first", [False, True])
async def test_a_one_to_one_chat_starts_a_new_session_and_leaves_no_request(
    db_session_factory: async_sessionmaker[AsyncSession], runtime_file: Path, register_first: bool
) -> None:
    sf = db_session_factory
    registration = await register(sf, OLD, {CHAT: [PERSON]}, runtime_file)
    old = await answer(sf, TEAMS, "teams", CHAT, CHAT, PERSON, is_dm=True)
    if register_first:
        with pytest.raises(ValueError, match="different configuration"):
            await register(sf, NEW, {CHAT: [PERSON]}, runtime_file)
    for change in (replace(CHANGE, tenant_id=UUID(registration["tenant_id"])), CHANGE):
        await move(sf, change)
    await register(sf, NEW, {CHAT: [PERSON]}, runtime_file)
    again = await answer(sf, TEAMS, "teams", CHAT, CHAT, PERSON, is_dm=True)
    assert model_of(again) == (NEW, 2)
    assert again.ma_session_id != old.ma_session_id
    async with sf() as session:
        assert await pending_releases(session, tenant_id=TEAMS) == []


@pytest.fixture
async def committed(db_nullpool_engine: Any, db_clean: None) -> async_sessionmaker[AsyncSession]:
    """Sessions on their own connections, so concurrent transactions really contend."""
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


async def move_everything(
    sf: async_sessionmaker[AsyncSession], registration: dict[str, Any], change: ModelChange = CHANGE
) -> None:
    """`change` for both registered tenants."""
    for tenant_id in (UUID(registration["tenant_id"]), TEAMS):
        await move(sf, replace(change, tenant_id=tenant_id))


async def held_before_binding(
    sf: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Task[PreparedTurn], asyncio.Event]:
    """A thread's first message, admitted and planned, held before it binds until resumed."""
    reached, resume = asyncio.Event(), asyncio.Event()
    publish = selfhosted_host._publish_first_binding  # pyright: ignore[reportPrivateUsage]

    async def held(*args: Any) -> object:
        reached.set()
        await resume.wait()
        return await publish(*args)

    monkeypatch.setattr(selfhosted_host, "_publish_first_binding", held)
    first = asyncio.create_task(answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON))
    await asyncio.wait_for(reached.wait(), 10)
    return first, resume


async def test_a_first_message_planned_before_the_change_is_not_run(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    registration = await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    first, resume = await held_before_binding(sf, monkeypatch)
    await move_everything(sf, registration)
    await register(sf, NEW, {CHANNEL: [PERSON]}, runtime_file)
    resume.set()
    # Not bound to the old model, and the person is asked to send it again.
    with pytest.raises(SessionPreparationFailed) as failed:
        await asyncio.wait_for(first, 10)
    assert failed.value.reasons == ("model",)
    async with sf() as session:
        assert await mux_state.channel_bindings(session, channel_ref(TEAMS, "teams", CHANNEL)) == []
    assert model_of(await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)) == (NEW, 2)


# What an admin can set the conversation to.
SETTINGS = {
    "old model": selfhosted_config(OLD),
    "other model": selfhosted_config(OTHER),
    "another backend": BackendConfig(),
}


async def write_step(
    step: str, sf: async_sessionmaker[AsyncSession], registration: dict[str, Any], runtime: Path
) -> None:
    """One write to the conversation's configuration, as an admin or the operator makes it."""
    match step:
        case "old model" | "other model" | "another backend":
            await configure(sf, SETTINGS[step])
        case "clear":
            await clear_conversation(sf)
        case "change" | "change back":
            change = CHANGE if step == "change" else replace(CHANGE, from_model=NEW, model=OLD)
            await move_everything(sf, registration, change)
            await register(sf, change.model, {CHANNEL: [PERSON]}, runtime)
        case "register again":
            await register(sf, OLD, {CHANNEL: [PERSON]}, runtime)
        case "register the new model first":
            # It moves the conversation, then stops at the agent still on the old model.
            with pytest.raises(ValueError, match="different configuration"):
                await register(sf, NEW, {CHANNEL: [PERSON]}, runtime)
        case _:
            raise ValueError(f"no such step: {step}")


def sequence(steps: tuple[str, ...]) -> str:
    return ", then ".join(steps) or "nothing"


@pytest.mark.parametrize(
    "steps",
    [
        (),
        ("clear",),
        ("old model",),
        ("clear", "old model"),
        ("another backend",),
        ("another backend", "old model"),
        ("register again",),
        ("clear", "register again"),
    ],
    ids=sequence,
)
async def test_a_first_message_binds_on_its_revision_while_no_other_model_is_set(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: tuple[str, ...],
) -> None:
    sf = committed
    registration = await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    first, resume = await held_before_binding(sf, monkeypatch)
    for step in steps:
        await write_step(step, sf, registration, runtime_file)
    resume.set()
    assert model_of(await asyncio.wait_for(first, 10)) == (OLD, 1)
    async with sf() as session:
        bound = await mux_state.channel_bindings(session, channel_ref(TEAMS, "teams", CHANNEL))
    assert [(binding.config_revision, binding.native_refs["model"]) for binding in bound] == [
        (1, OLD)
    ]


@pytest.mark.parametrize(
    "steps",
    [
        ("change",),
        ("change", "clear"),
        ("change", "old model"),
        ("change", "another backend"),
        ("change", "change back"),
        ("other model",),
        ("other model", "clear"),
        ("clear", "other model"),
        ("other model", "old model"),
        ("register the new model first",),
    ],
    ids=sequence,
)
async def test_a_first_message_is_not_bound_once_another_model_was_set(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: tuple[str, ...],
) -> None:
    sf = committed
    registration = await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    first, resume = await held_before_binding(sf, monkeypatch)
    for step in steps:
        await write_step(step, sf, registration, runtime_file)
    before = await snapshot(sf)
    resume.set()
    # Whatever came after the other model, the message does not run and writes nothing.
    with pytest.raises(SessionPreparationFailed) as refused:
        await asyncio.wait_for(first, 10)
    assert (refused.value.reasons, refused.value.stage) == (("model",), "create")
    assert await snapshot(sf) == before


async def test_the_change_waits_for_a_first_message_binding_then_refuses(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    plan = await plan_model_change(sf, CHANGE)
    reached, resume = asyncio.Event(), asyncio.Event()
    put = mux_state.put_binding

    async def held(session: AsyncSession, binding: ProviderBinding, **kwargs: Any) -> object:
        reached.set()
        await resume.wait()
        return await put(session, binding, **kwargs)

    monkeypatch.setattr(mux_state, "put_binding", held)
    first = asyncio.create_task(answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON))
    await asyncio.wait_for(reached.wait(), 10)
    # The binding holds the policy fence: the change waits for it, then sees its thread.
    apply = asyncio.create_task(apply_model_change(sf, CHANGE, plan))
    await asyncio.sleep(0.3)
    assert not apply.done()
    resume.set()
    assert model_of(await asyncio.wait_for(first, 10)) == (OLD, 1)
    with pytest.raises(ModelChangeRefused, match="changed since the dry run"):
        await asyncio.wait_for(apply, 10)
    assert [len(c.threads) for c in (await move(sf, CHANGE)).conversations] == [1]


@pytest.mark.parametrize("bound_first", [False, True])
async def test_a_first_binding_an_erasure_and_an_admin_clear_all_finish(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_first: bool,
) -> None:
    sf = committed
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    reached, resume = asyncio.Event(), asyncio.Event()
    bindings: list[ProviderBinding] = []
    put = mux_state.put_binding

    async def held(session: AsyncSession, binding: ProviderBinding, **kwargs: Any) -> object:
        bindings.append(binding)
        if bound_first:
            binding = await put(session, binding, **kwargs)
        reached.set()
        await resume.wait()
        return binding if bound_first else await put(session, binding, **kwargs)

    monkeypatch.setattr(mux_state, "put_binding", held)
    first = asyncio.create_task(answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON))
    await asyncio.wait_for(reached.wait(), 10)
    account_id = UUID(bindings[0].legacy_account_id)
    erasure = asyncio.create_task(purge_account(sm=sf, account_id=account_id))
    admin = asyncio.create_task(clear_conversation(sf))
    if bound_first:
        await asyncio.sleep(0.3)
        assert not erasure.done() and not admin.done()  # both wait for the binding to commit
    else:
        await asyncio.wait_for(asyncio.shield(erasure), 10)
        assert not admin.done()  # the binding's transaction still holds the policy fence
    resume.set()
    await asyncio.wait_for(asyncio.gather(erasure, admin), 10)
    if bound_first:
        await first
    else:
        with pytest.raises(ScopeViolation):
            await first  # the erased account is never bound again
    async with sf() as session:
        assert await mux_state.channel_bindings(session, channel_ref(TEAMS, "teams", CHANNEL)) == []


async def test_a_thread_advancing_during_the_change_retries_on_the_new_model(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    registration = await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    await edit_agent(sf)  # its next message moves the thread's binding on the old model
    reached, resume = asyncio.Event(), asyncio.Event()
    put = mux_state.PostgresStateStore.put_binding

    async def held(
        self: mux_state.PostgresStateStore, binding: ProviderBinding, **kwargs: Any
    ) -> object:
        if binding.generation == 2:
            reached.set()
            await resume.wait()
        return await put(self, binding, **kwargs)

    monkeypatch.setattr(mux_state.PostgresStateStore, "put_binding", held)
    advancing = asyncio.create_task(answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON))
    await asyncio.wait_for(reached.wait(), 10)
    await move_everything(sf, registration)
    await register(sf, NEW, {CHANNEL: [PERSON]}, runtime_file)
    resume.set()
    with pytest.raises(SessionBusyError):
        await asyncio.wait_for(advancing, 10)
    assert model_of(await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)) == (NEW, 2)


async def test_a_thread_moving_inside_the_change_refuses_it_whole(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    plan = await plan_model_change(sf, CHANGE)
    write = selfhosted.set_channel_backend
    before: list[dict[str, list[str]]] = []

    async def moved_first(*args: Any) -> object:
        await move_thread(sf)
        before.append(await snapshot(sf))
        return await write(*args)

    monkeypatch.setattr(selfhosted, "set_channel_backend", moved_first)
    with pytest.raises(ModelChangeRefused, match="changed since the dry run"):
        await apply_model_change(sf, CHANGE, plan)
    assert await snapshot(sf) == before[0]


async def test_a_failure_inside_the_change_writes_nothing(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    plan = await plan_model_change(sf, CHANGE)
    before = await snapshot(sf)
    put = mux_state.put_binding

    async def released_then_failed(*args: Any, **kwargs: Any) -> object:
        await put(*args, **kwargs)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(mux_state, "put_binding", released_then_failed)
    with pytest.raises(RuntimeError, match="interrupted"):
        await apply_model_change(sf, CHANGE, plan)
    assert await snapshot(sf) == before


async def test_a_change_stopped_before_its_agents_moved_finishes_on_the_next_run(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    registration = await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    update = SelfHostedAgents.update

    async def stopped(*args: Any, **kwargs: Any) -> object:
        raise RuntimeError("stopped")

    monkeypatch.setattr(SelfHostedAgents, "update", stopped)
    with pytest.raises(RuntimeError, match="stopped"):
        await move(sf, CHANGE)
    left = await plan_model_change(sf, CHANGE)
    assert ([agent.name for agent in left.agents], left.conversations) == (["daimon"], ())
    # Half moved, the thread is refused rather than answered on either model.
    with pytest.raises(AdmissionDenied):
        await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    monkeypatch.setattr(SelfHostedAgents, "update", update)
    await move_everything(sf, registration)
    await register(sf, NEW, {CHANNEL: [PERSON]}, runtime_file)
    assert model_of(await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)) == (NEW, 2)


def held_off_by_the_change(
    sf: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    write: Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]],
) -> list[asyncio.Task[None]]:
    """Start `write` once the change holds its locks, and check that it waits."""
    move_conversation = selfhosted.set_channel_backend
    started: list[asyncio.Task[None]] = []

    async def meanwhile(*args: Any) -> object:
        started.append(asyncio.create_task(write(sf)))
        await asyncio.sleep(0.3)
        assert not started[0].done()  # held off until the conversations have moved
        return await move_conversation(*args)

    monkeypatch.setattr(selfhosted, "set_channel_backend", meanwhile)
    return started


async def test_an_agent_edit_waits_for_the_conversations_to_move(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = committed
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    plan = await plan_model_change(sf, CHANGE)
    edits = held_off_by_the_change(sf, monkeypatch, edit_agent)
    with pytest.raises(ModelChangeRefused, match="run the dry run again to move the agent"):
        await apply_model_change(sf, CHANGE, plan)
    await edits[0]
    left = await plan_model_change(sf, CHANGE)
    assert ([agent.name for agent in left.agents], left.conversations) == (["daimon"], ())


@pytest.mark.parametrize(
    ("admin", "backend"),
    [(clear_conversation, "anthropic"), (configure_other_model, "selfhosted")],
)
async def test_an_admin_change_during_the_change_lands_after_it(
    committed: async_sessionmaker[AsyncSession],
    runtime_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    admin: Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]],
    backend: str,
) -> None:
    sf = committed
    await register(sf, OLD, {CHANNEL: [PERSON]}, runtime_file)
    await answer(sf, TEAMS, "teams", CHANNEL, THREAD, PERSON)
    plan = await plan_model_change(sf, CHANGE)
    edits = held_off_by_the_change(sf, monkeypatch, admin)
    await apply_model_change(sf, CHANGE, plan)
    await edits[0]
    async with sf() as session:
        latest = await mux_state.latest_config_revision(
            session, channel_ref(TEAMS, "teams", CHANNEL), overrides_only=True
        )
    assert latest is not None and (latest.local, latest.backend) == (3, backend)
