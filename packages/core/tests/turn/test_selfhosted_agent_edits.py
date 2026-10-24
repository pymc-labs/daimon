"""An agent edit reaches an open self-hosted thread at its next message.

Same harness session (so the thread's workspace stays), new configuration; a
turn already running when the edit lands finishes on the old one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import ProviderPreparationRequest, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.selfhosted_host import UPDATE_LEASE_TTL
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ModelRef, ResourceRef, Revision, ThreadRef
from mux.contracts.resources import MCPConnection, SessionSpec, SkillRef, ToolSpec
from mux.drivers.selfhosted import wire
from mux.errors import ContinuityLost, UnsupportedCapability
from mux.state.lease import LeaseBusy, Slot
from mux.state.memory import MemoryStateStore

from .conformance_support import ACCOUNT, TENANT
from .test_selfhosted_host import BASE, PROFILE, REVISION, Wire, composed, pinned_leg  # noqa: F401

NEW_MODEL = "gpt-6-terra"
NEW_REVISION = ConfigRevision.create(
    BASE.channel,
    2,
    resolve_default(BackendConfig(backend="selfhosted", profile=PROFILE, model=NEW_MODEL)),
)


@pytest.fixture
def harness_gate(monkeypatch):
    """Echo the requested model in usage; optionally hold a turn inside its harness POST."""
    state = SimpleNamespace(hold=False, reached=asyncio.Event(), release=asyncio.Event())
    original = Wire.handle

    async def handle(self, request):
        if request.method == "POST" and state.hold:
            state.reached.set()
            await state.release.wait()
        known = len(self.events)
        response = original(self, request)
        for event in self.events[known:]:
            if event["type"] == "usage":
                event["data"]["model"] = next(reversed(self.turns.values()))["model"]
        return response

    monkeypatch.setattr(Wire, "handle", handle)
    return state


@dataclass(frozen=True)
class Tools:
    mcp_servers: tuple[wire.MCPServer, ...]
    skills: tuple[wire.InlineSkill, ...]


async def resolve_tools(scope, spec):
    return Tools(
        mcp_servers=tuple(
            wire.MCPServer(name=server.name, url=server.url) for server in spec.mcp_servers or ()
        ),
        skills=tuple(
            wire.InlineSkill(name=skill.id, files={"SKILL.md": skill.id})
            for skill in spec.skills or ()
        ),
    )


class Thread:
    """One open thread whose agent (and, for a model change, channel config) is edited."""

    def __init__(self, request: ProviderPreparationRequest, wire_, store, observations) -> None:
        runtime = request.deps.turn_runtimes[PROFILE]
        self.agents, self.environments = runtime.agents, runtime.environments
        self.revision = REVISION
        self.wire, self.store, self.observations = wire_, store, observations
        self.request = request
        self.use(replace(runtime, session_plan=self.plan, tools_resolver=resolve_tools))
        self.turns = 0

    async def plan(self, preparation):
        agent, environment = self.agents.value, self.environments.value
        return SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=self.revision.local,
        )

    def use(self, runtime) -> None:
        deps = replace(self.request.deps, turn_runtimes={PROFILE: runtime})
        self.request = replace(
            self.request,
            deps=deps,
            admission=replace(self.request.admission, backend_revision=self.revision),
        )

    def edit_agent(self, **spec_changes) -> None:
        agent = self.agents.value
        self.agents.value = agent.model_copy(
            update={
                "revision": Revision(local=agent.revision.local + 1),
                "spec": agent.spec.model_copy(update=spec_changes),
            }
        )

    def switch_model(self, model: str, revision: ConfigRevision) -> None:
        price = replace(self.request.deps.turn_runtimes[PROFILE].price, model=model)
        self.revision = revision
        self.use(replace(self.request.deps.turn_runtimes[PROFILE], model=model, price=price))
        self.edit_agent(model=ModelRef(provider="selfhosted", id=model))

    async def prepare(self):
        return await bind_session_impl(
            self.request.deps,
            self.request.admission,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )

    async def run(self, prepared):
        self.turns += 1

        async def reseed():
            return "question"

        outcome = await run_prepared_turn(
            self.request.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message=f"question {self.turns}",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key=f"edit-{self.turns}",
        )
        await drain_outcomes()
        assert outcome.state.error is None
        return outcome

    async def turn(self):
        return await self.run(await self.prepare())

    def bodies(self):
        return list(self.wire.turns.values())

    async def binding(self):
        slot = Slot(
            thread=ThreadRef(channel=self.revision.channel, thread_id="thread"),
            account_id=str(ACCOUNT),
        )
        return await self.store.get_binding(slot)


@pytest.fixture
def thread(harness_gate, composed):  # noqa: F811
    request, wire_, store, observations = composed
    return Thread(request, wire_, store, observations)


EDITS = {
    "instructions": (
        {"system": "edited instructions"},
        lambda body: body["instructions"] == "edited instructions",
    ),
    "tools": (
        {"tools": (ToolSpec(name="bash", kind="builtin", permission="ask"),)},
        lambda body: (
            body["tool_policy"].get("bash") == "ask" and body["tool_policy"].get("read") == "deny"
        ),
    ),
    "skills": (
        {"skills": (SkillRef(id="analysis"),)},
        lambda body: [skill["name"] for skill in body["skills"]] == ["analysis"],
    ),
    "mcp": (
        {"mcp_servers": (MCPConnection(name="docs", url="https://mcp.example/docs"),)},
        lambda body: [server["name"] for server in body["mcp_servers"]] == ["docs"],
    ),
}


@pytest.mark.parametrize("edit", list(EDITS))
async def test_agent_edit_reaches_the_next_turn_on_the_same_session(thread, edit):
    changes, applied = EDITS[edit]
    first = await thread.prepare()
    await thread.run(first)
    before = await thread.binding()
    thread.edit_agent(**changes)
    second = await thread.prepare()
    assert second.reused and second.session_ref == first.session_ref
    await thread.run(second)
    old, new = thread.bodies()
    assert not applied(old) and applied(new)
    assert new["session_id"] == old["session_id"] == first.session_ref.id
    after = await thread.binding()
    assert (after.id, after.native_refs["session"]) == (before.id, before.native_refs["session"])
    assert after.generation == before.generation + 1
    assert Revision.model_validate_json(after.native_refs["agent_revision_pin"]).local == 2
    assert thread.wire.posts == 2


async def test_model_edit_reaches_the_next_turn_and_bills_each_turn_on_its_own_config(thread):
    first = await thread.prepare()
    await thread.run(first)
    before = await thread.binding()
    thread.switch_model(NEW_MODEL, NEW_REVISION)
    second = await thread.prepare()
    await thread.run(second)
    old, new = thread.bodies()
    assert (old["model"], new["model"]) == ("gpt-6-luna", NEW_MODEL)
    assert new["session_id"] == old["session_id"]
    after = await thread.binding()
    assert after.config_revision == NEW_REVISION.local
    assert after.native_refs["model"] == NEW_MODEL
    assert after.generation == before.generation + 1
    assert [row["model_id"] for row in thread.observations] == ["gpt-6-luna", NEW_MODEL]
    assert [row["provider_price"].model for row in thread.observations] == ["gpt-6-luna", NEW_MODEL]
    # The first turn's rows are never rewritten onto the new binding config or model.
    assert thread.observations[0]["binding_id"] == thread.observations[1]["binding_id"] == before.id


async def test_a_turn_running_when_the_edit_lands_finishes_on_the_old_config(thread, harness_gate):
    harness_gate.hold = True
    prepared = await thread.prepare()
    running = asyncio.create_task(thread.run(prepared))
    await asyncio.wait_for(harness_gate.reached.wait(), 5)
    before = await thread.binding()
    thread.switch_model(NEW_MODEL, NEW_REVISION)
    thread.edit_agent(system="edited instructions")
    # The next message is deferred, and the binding under the running turn is untouched.
    with pytest.raises(SessionBusyError) as busy:
        await thread.prepare()
    assert busy.value.pending_reasons == ("session_active",)
    assert await thread.binding() == before
    harness_gate.release.set()
    await running
    old = thread.bodies()[0]
    assert (old["instructions"], old["model"]) == ("", "gpt-6-luna")
    assert [row["model_id"] for row in thread.observations] == ["gpt-6-luna"]
    harness_gate.hold = False
    await thread.run(await thread.prepare())
    new = thread.bodies()[1]
    assert (new["instructions"], new["model"]) == ("edited instructions", NEW_MODEL)
    assert new["session_id"] == old["session_id"]
    assert [row["model_id"] for row in thread.observations] == ["gpt-6-luna", NEW_MODEL]
    assert (await thread.binding()).generation == before.generation + 1


async def test_an_edit_between_preparation_and_send_waits_for_the_next_turn(thread):
    await thread.turn()
    prepared = await thread.prepare()
    thread.edit_agent(system="edited after preparation")
    await thread.run(prepared)
    assert thread.bodies()[1]["instructions"] == ""
    await thread.turn()
    assert thread.bodies()[2]["instructions"] == "edited after preparation"


async def test_a_second_turn_without_an_edit_leaves_the_binding_alone(thread):
    await thread.turn()
    before = await thread.binding()
    await thread.turn()
    assert await thread.binding() == before


def other(kind, id_):
    return ResourceRef(
        id=id_,
        kind=kind,
        provider="selfhosted",
        account_scope_id=str(TENANT),
        tenant_id=str(TENANT),
        account_id=None,
    )


async def test_changed_environment_is_still_refused(thread):
    await thread.turn()
    environment = thread.environments.value
    thread.environments.value = environment.model_copy(
        update={"ref": other("environment", "other")}
    )
    with pytest.raises(ContinuityLost, match="bound self-hosted configuration changed"):
        await thread.prepare()


async def test_binding_of_another_account_scope_is_still_refused(thread):
    await thread.turn()
    bound = await thread.binding()
    foreign = bound.model_copy(
        update={
            "generation": bound.generation + 1,
            "native_refs": {**bound.native_refs, "account_scope": "another-scope"},
        }
    )
    await thread.store.put_binding(foreign, expected_generation=bound.generation)
    with pytest.raises(ContinuityLost, match="bound self-hosted configuration changed"):
        await thread.prepare()


async def test_changed_environment_revision_is_still_refused(thread):
    await thread.turn()
    environment = thread.environments.value
    thread.environments.value = environment.model_copy(update={"revision": Revision(local=2)})
    thread.edit_agent(system="edited")
    with pytest.raises(ContinuityLost) as lost:
        await thread.prepare()
    assert "environment changed" in str(lost.value)


async def test_archived_agent_is_still_refused(thread):
    await thread.turn()
    thread.edit_agent(system="edited")
    thread.agents.value = thread.agents.value.model_copy(update={"lifecycle": "archived"})
    with pytest.raises(ContinuityLost) as lost:
        await thread.prepare()
    assert "agent_archived" in str(lost.value)
    assert thread.wire.posts == 1


async def test_a_rival_turn_cannot_take_the_lease_while_the_binding_advances(thread, monkeypatch):
    await thread.turn()
    before = await thread.binding()
    slot = Slot(thread=before.thread, account_id=before.legacy_account_id)
    rival = {}
    put_binding = thread.store.put_binding

    async def rival_then_put(binding, *, expected_generation):
        # The moment after the busy check: a rival turn tries to start.
        try:
            lease = await thread.store.acquire_lease(
                slot,
                holder="rival",
                turn_id="rival",
                now=datetime.now(UTC),
                ttl=timedelta(seconds=30),
            )
        except LeaseBusy:
            rival["blocked"] = True
        else:
            rival["blocked"] = False
            await thread.store.release_lease(lease)
        return await put_binding(binding, expected_generation=expected_generation)

    monkeypatch.setattr(thread.store, "put_binding", rival_then_put)
    thread.edit_agent(system="edited instructions")
    await thread.prepare()
    assert rival == {"blocked": True}
    assert (await thread.binding()).generation == before.generation + 1
    # The update released its hold: the next turn runs normally on the new config.
    await thread.run(await thread.prepare())
    assert thread.bodies()[1]["instructions"] == "edited instructions"


async def test_an_agent_archived_between_preparation_and_send_does_not_run(thread):
    await thread.turn()
    prepared = await thread.prepare()
    thread.agents.value = thread.agents.value.model_copy(update={"lifecycle": "archived"})
    with pytest.raises((ContinuityLost, UnsupportedCapability)):
        await thread.run(prepared)
    assert thread.wire.posts == 1


async def test_a_second_update_cannot_share_the_first_updates_hold(thread, monkeypatch):
    await thread.turn()
    before = await thread.binding()
    second = {}
    put_binding = thread.store.put_binding

    async def second_update_then_put(binding, *, expected_generation):
        # While the first update holds the slot, another message prepares the same edit.
        try:
            await thread.prepare()
        except SessionBusyError as error:
            second["busy"] = error.pending_reasons
        return await put_binding(binding, expected_generation=expected_generation)

    monkeypatch.setattr(thread.store, "put_binding", second_update_then_put)
    thread.edit_agent(system="edited instructions")
    await thread.prepare()
    assert second == {"busy": ("session_active",)}
    assert (await thread.binding()).generation == before.generation + 1


async def test_an_update_whose_lease_expired_does_not_publish(thread, monkeypatch):
    if not isinstance(thread.store, MemoryStateStore):
        pytest.skip("the Postgres store takes lease time from the database clock")
    await thread.turn()
    before = await thread.binding()
    slot = Slot(thread=before.thread, account_id=before.legacy_account_id)
    rival = {}
    renew_lease = thread.store.renew_lease

    async def rival_takes_over_then_renew(lease, *, now, ttl):
        # The update stalled past its lease; a rival turn takes the slot first.
        rival["lease"] = await thread.store.acquire_lease(
            slot, holder="rival", turn_id="rival", now=now + UPDATE_LEASE_TTL * 2, ttl=ttl
        )
        return await renew_lease(lease, now=now, ttl=ttl)

    monkeypatch.setattr(thread.store, "renew_lease", rival_takes_over_then_renew)
    thread.edit_agent(system="edited instructions")
    with pytest.raises(SessionBusyError):
        await thread.prepare()
    assert (await thread.binding()).generation == before.generation
    # The rival still owns the slot: the failed update did not release it.
    monkeypatch.setattr(thread.store, "renew_lease", renew_lease)
    await thread.store.renew_lease(
        rival["lease"], now=rival["lease"].expires_at - timedelta(seconds=1), ttl=UPDATE_LEASE_TTL
    )


async def test_the_driver_rebuilt_after_an_edit_keeps_the_persisted_cursor(thread):
    await thread.turn()
    thread.edit_agent(system="edited instructions")
    prepared = await thread.prepare()
    # The rebuilt driver reads the persisted high-water mark, so its next request
    # fences at the thread's cursor instead of restarting from zero.
    assert prepared.backend.events.high_water is not None
    await thread.run(prepared)
    assert thread.bodies()[1]["instructions"] == "edited instructions"
