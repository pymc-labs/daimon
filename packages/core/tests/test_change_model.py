"""`change_model`: one write for any catalog model, routed to the record or the provider."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core import agent_changes
from daimon.core.agent_changes import change_model
from daimon.core.agent_editing import AgentActor, AgentPatch
from daimon.core.authz import Subject, Surface
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.stores.domain import BackendAgent, TenantBackendPolicy
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec

TENANT = uuid.uuid4()
LUNA = ModelRef(provider="openai", id="gpt-6-luna")
SONNET = ModelRef(provider="anthropic", id="claude-sonnet-5-5")


class _Backends:
    """A provider service that records what it was asked to write."""

    enabled = True

    def __init__(self, *, on: str | None) -> None:
        self.on = on
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.previews = 0

    async def tenant_policy(self, tenant_id: uuid.UUID) -> TenantBackendPolicy:
        return TenantBackendPolicy(
            tenant_id=tenant_id, revision="1", enabled_backends=("anthropic", "openai")
        )

    async def current(self, _agent: BackendAgent) -> Any:
        return None if self.on is None else SimpleNamespace(backend=self.on)

    async def may_edit(self, *_: Any) -> bool:
        return True

    async def preview(self, _agent: BackendAgent, target: str | None, *_: Any) -> Any:
        self.previews += 1
        return SimpleNamespace(allowed=True, digest=f"digest-{target}", setting_revision=4)

    async def set_backend(self, _agent: BackendAgent, target: str, *args: Any, **kw: Any) -> str:
        self.writes.append(("set", {"target": target, "revision": args[0], **kw}))
        return "revision"

    async def clear_backend(self, _agent: BackendAgent, *args: Any, **kw: Any) -> str:
        self.writes.append(("clear", {"revision": args[0], **kw}))
        return "revision"


def _agent() -> BackendAgent:
    spec = AgentSpec(name="helper", model=SONNET)
    ref = ResourceRef(id="ma-agent-1", kind="agent", provider="anthropic", account_scope_id="ma")
    source = Agent(ref=ref, revision=Revision(local=1), spec=spec, created_at=datetime.now(UTC))
    return BackendAgent(
        tenant_id=TENANT,
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="config-1",
        source=source,
    )


def _actor() -> AgentActor:
    return AgentActor(
        tenant_id=TENANT,
        account_id=uuid.uuid4(),
        subject=Subject(is_admin=True),
        surface=Surface.CONFIG,
    )


def _deps(
    monkeypatch: pytest.MonkeyPatch, backends: _Backends | None
) -> tuple[dict[str, Any], list[tuple[str, AgentPatch]]]:
    edits: list[tuple[str, AgentPatch]] = []

    async def update_agent(_deps: Any, *, ma_agent_id: str, patch: AgentPatch, **_: Any) -> str:
        edits.append((ma_agent_id, patch))
        return "record"

    monkeypatch.setattr(agent_changes.agent_editing, "update_agent", update_agent)
    return {"edit": None, "backends": backends}, edits


async def test_a_claude_model_on_claude_edits_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude to Claude stays a record edit; the provider setting is untouched."""
    backends = _Backends(on=None)
    deps, edits = _deps(monkeypatch, backends)
    changed = await change_model(**deps, agent=_agent(), model=SONNET, actor=_actor())
    assert edits == [("ma-agent-1", AgentPatch(model=SONNET.id))], "the record names the model"
    assert backends.writes == [], "no provider write for a Claude-to-Claude change"
    assert changed.revision is None, "nothing was written beside the record"


async def test_a_claude_model_works_without_provider_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where provider choice is off, a Claude model is still a record edit."""
    deps, edits = _deps(monkeypatch, None)
    await change_model(**deps, agent=_agent(), model=SONNET, actor=_actor())
    assert len(edits) == 1, "the record edit runs without a provider service"


async def test_another_provider_writes_the_setting_with_its_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving to OpenAI writes the provider setting, consent and digest included."""
    backends = _Backends(on=None)
    deps, edits = _deps(monkeypatch, backends)
    await change_model(
        **deps, agent=_agent(), model=LUNA, actor=_actor(), accepted_drops=("skill:x",)
    )
    assert edits == [], "the record is untouched"
    assert backends.writes == [
        (
            "set",
            {
                "target": "openai",
                "revision": 4,
                "expected_preview_digest": "digest-openai",
                "accepted_drops": ("skill:x",),
                "model": "gpt-6-luna",
                "settings": None,
            },
        )
    ], "set_backend gets the fresh preview's digest and revision and the accepted drops"


async def test_a_digest_the_person_saw_is_the_one_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's digest and revision reach the write, so a moved preview is refused there."""
    backends = _Backends(on=None)
    deps, _ = _deps(monkeypatch, backends)
    await change_model(
        **deps,
        agent=_agent(),
        model=LUNA,
        actor=_actor(),
        expected_digest="seen",
        expected_revision=2,
    )
    write = backends.writes[0][1]
    assert (write["expected_preview_digest"], write["revision"]) == ("seen", 2), (
        "the caller's preview is what the service checks"
    )
    assert backends.previews == 0, "the write re-previews; no second read before it"


async def test_back_to_claude_edits_the_record_then_clears_the_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent on OpenAI moving to a Claude model gets the record edit and a cleared pin."""
    backends = _Backends(on="openai")
    deps, edits = _deps(monkeypatch, backends)
    changed = await change_model(**deps, agent=_agent(), model=SONNET, actor=_actor())
    assert len(edits) == 1, "the record names the Claude model"
    assert [kind for kind, _ in backends.writes] == ["clear"], "the provider pin is cleared"
    assert changed.record == "record", "both writes are reported"


@pytest.mark.parametrize(
    ("model", "backends", "reason"),
    [
        (
            ModelRef(provider="gemini", id="gemini-4-argon"),
            _Backends(on=None),
            "backend_not_allowed",
        ),
        (LUNA, None, "model_not_selectable"),
        (
            ModelRef(provider="anthropic", id="claude-nope"),
            _Backends(on=None),
            "model_not_selectable",
        ),
        (ModelRef(provider="openai", id="gpt-nope"), _Backends(on=None), "model_not_selectable"),
    ],
)
async def test_a_model_outside_the_catalog_is_refused(
    monkeypatch: pytest.MonkeyPatch, model: ModelRef, backends: _Backends | None, reason: str
) -> None:
    """A provider the tenant hasn't enabled, provider choice off, or an unknown id: refused."""
    deps, edits = _deps(monkeypatch, backends)
    with pytest.raises(AgentBackendRefused) as refused:
        await change_model(**deps, agent=_agent(), model=model, actor=_actor())
    assert refused.value.reason == reason, f"{model} is refused as {reason}"
    assert edits == [], "nothing is written"
