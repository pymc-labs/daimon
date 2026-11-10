"""Every provider host replaces a mismatched binding through one helper, never refuses it.

Hosts come from the live preparation registry; each is checked for the shared
decision and replacement calls, then every binding field is changed on each one.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.core.session_compat import ReplaceSession, UpdateInPlace
from daimon.core.turn import prepare as preparation
from daimon.core.turn.replace_binding import (
    BINDING_FIELDS,
    BindingFacts,
    BindingField,
    OldSession,
    binding_decision,
    recorded_facts,
    replace_binding,
)
from mux.contracts.events import AgentMessagePayload, TextPart, UserMessagePayload
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import UnsupportedCapability
from mux.state.lease import Slot
from mux.state.memory import MemoryStateStore
from mux.state.store import ERA_REF

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="daimon", authorization_id="turn")
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="t", platform="slack", channel_id="c"), thread_id="x"
)
SLOT = Slot(thread=THREAD, account_id="a")


def _registered_hosts() -> dict[str, object]:
    from daimon.core.turn import gemini_deployment, openai_host, selfhosted_host

    openai_host.register_host()
    selfhosted_host.register_host()
    gemini_deployment.register_runtime()
    return {
        profile: prepare
        for profile, prepare in preparation._PROVIDER_PREPARATIONS.items()  # pyright: ignore[reportPrivateUsage]
        if getattr(prepare, "__module__", "").startswith("daimon.core.")
    }


def _updatable(profile: str) -> frozenset[BindingField]:
    from daimon.core.turn import selfhosted_host

    return selfhosted_host.UPDATABLE_FIELDS if profile.startswith("selfhosted.") else frozenset()


HOSTS = _registered_hosts()


def test_registry_finds_every_provider_host() -> None:
    providers = {profile.split(".", 1)[0] for profile in HOSTS}
    assert providers == {"openai", "gemini", "selfhosted"}


@pytest.mark.parametrize("profile", sorted(HOSTS))
def test_host_decides_and_replaces_through_the_core_helper(profile: str) -> None:
    module = inspect.getmodule(HOSTS[profile])
    assert module is not None
    called = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"binding_decision", "replace_binding"} <= called, module.__name__


@dataclass
class _Backend:
    """An old session's Events, Sessions and Artifacts ports, counting terminations."""

    ended: list[str] = field(default_factory=list[str])
    archive_supported: bool = False

    def __post_init__(self) -> None:
        self.events = SimpleNamespace(list=self._list)
        self.sessions = SimpleNamespace(archive=self._archive, delete=self._delete)

    async def _list(self, scope: Scope, ref: ResourceRef, *, page: object) -> object:
        payloads = (
            AgentMessagePayload(item_id="i", content=(TextPart(text="It is 42."),)),
            UserMessagePayload(input_id="u", content=(TextPart(text="What is the answer?"),)),
        )
        events = [SimpleNamespace(typed_payload=lambda p=p: p) for p in payloads]
        return SimpleNamespace(data=events, has_more=False, next_cursor=None)

    async def _archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> None:
        if not self.archive_supported:
            raise UnsupportedCapability(("archive",), "test")
        self.ended.append(ref.id)

    async def _delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> None:
        self.ended.append(ref.id)


def _facts(profile: str) -> BindingFacts:
    return BindingFacts(
        profile=profile,
        provider=profile.split(".", 1)[0],
        config_revision=1,
        agent="agent-1",
        account_scope="scope-1",
        model="model-1",
        plan_digest="plan-1",
    )


def _changed(facts: BindingFacts, name: BindingField) -> BindingFacts:
    value = getattr(facts, name)
    return replace(facts, **{name: value + 1 if isinstance(value, int) else f"{value}-new"})


def _binding(facts: BindingFacts) -> ProviderBinding:
    return ProviderBinding(
        id="binding-1",
        thread=THREAD,
        provider=facts.provider,  # pyright: ignore[reportArgumentType]
        profile=facts.profile,
        native_refs={
            "session": "old-session",
            "agent": facts.agent,
            "account_scope": facts.account_scope,
            "model": facts.model,
            "plan_digest": facts.plan_digest,
        },
        generation=1,
        config_revision=facts.config_revision,
        legacy_account_id="a",
    )


@pytest.mark.parametrize("name", BINDING_FIELDS)
@pytest.mark.parametrize("profile", sorted(HOSTS))
async def test_every_binding_field_change_continues_without_raising(
    profile: str, name: BindingField
) -> None:
    recorded = _facts(profile)
    old = _binding(recorded)
    assert recorded_facts(old) == recorded
    decision = binding_decision(recorded, _changed(recorded, name), updatable=_updatable(profile))
    if name in _updatable(profile):
        assert isinstance(decision, UpdateInPlace)
        return
    assert isinstance(decision, ReplaceSession) and decision.reasons

    store = MemoryStateStore()
    await store.put_binding(old, expected_generation=0)
    stored = await store.get_binding(SLOT)
    assert stored is not None
    backend = _Backend(archive_supported=recorded.provider == "openai")
    created: list[str] = []

    async def create(era: str, mounts: object, lease: object) -> ProviderBinding:
        created.append(era)
        return stored.model_copy(update={"native_refs": {"session": f"new{era}"}})

    replaced = await replace_binding(
        store=store,
        slot=SLOT,
        scope=SCOPE,
        old=OldSession(
            binding=stored,
            ref=ResourceRef(
                id="old-session",
                kind="session",
                provider=recorded.provider,  # pyright: ignore[reportArgumentType]
                account_scope_id="scope-1",
            ),
            backend=backend,  # pyright: ignore[reportArgumentType]
        ),
        reasons=decision.reasons,
        create=create,
        now=datetime.now(UTC),
        model_id="gpt-5",
        agent_name="Daimon",
    )
    assert created == [f":g{stored.generation}"]
    assert backend.ended == ["old-session"], "one terminate per replacement"
    bound = await store.get_binding(SLOT)
    assert bound is not None and bound == replaced.binding
    assert bound.generation == stored.generation + 1
    assert bound.native_refs[ERA_REF] == created[0]
    continuity = replaced.continuity
    assert continuity.state == "replaced" and continuity.applied == decision.reasons
    assert "What is the answer?" in continuity.user_prefix
    assert "Not carried over" in continuity.user_prefix
    assert "summar" not in continuity.user_prefix.lower()


async def test_unreadable_old_session_gets_the_lost_workspace_framing() -> None:
    store = MemoryStateStore()
    old = _binding(_facts("openai.persistent_workspace"))
    await store.put_binding(old, expected_generation=0)
    stored = await store.get_binding(SLOT)
    assert stored is not None

    async def create(era: str, mounts: object, lease: object) -> ProviderBinding:
        return stored.model_copy(update={"native_refs": {"session": "new"}})

    replaced = await replace_binding(
        store=store,
        slot=SLOT,
        scope=SCOPE,
        old=OldSession(binding=stored, ref=None, backend=None),
        reasons=("environment",),
        create=create,
        now=datetime.now(UTC),
        model_id="gpt-5",
        agent_name="Daimon",
    )
    assert replaced.continuity.state == "replaced_after_loss"
    assert "was lost" in replaced.continuity.user_prefix


# Each `raise ContinuityLost` left in core, and why it is not a binding mismatch.
CONTINUITY_LOST_ALLOWED = {
    ("turn/openai_host.py", "OpenAI session terminated during creation"): "provider ended it",
    ("turn/openai_host.py", "OpenAI session terminated"): "provider ended it",
    ("turn/selfhosted_host.py", "self-hosted session terminated"): "provider ended it",
    ("turn/gemini.py", "Gemini stream history has an unrecoverable gap."): "stream gap",
}


def test_no_binding_mismatch_raises_continuity_lost_outside_mux_drivers() -> None:
    root = Path(preparation.__file__).parents[1]
    found: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "ContinuityLost"
            ):
                continue
            evidence = next(
                (
                    item.value
                    for arg in node.exc.args
                    if isinstance(arg, ast.Tuple)
                    for item in arg.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ),
                "",
            )
            found.add((path.relative_to(root).as_posix(), evidence))
    assert found == set(CONTINUITY_LOST_ALLOWED)
