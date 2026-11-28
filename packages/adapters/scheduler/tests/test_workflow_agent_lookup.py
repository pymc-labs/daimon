"""A step's agent lookup heals only the default agent, named or not, never a renamed-away seeded one."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from daimon.adapters.scheduler.workflows import HeadlessLaneTurn, lane_turn
from daimon.testing.workflows import make_settings


@pytest.mark.parametrize(("agent", "healed"), [("aria", False), (None, True), ("default", True)])
async def test_agent_id_when_agent_is_missing_then_applies_defaults_only_for_the_default_agent(
    agent: str | None, healed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: list[str] = []

    async def reconcile(*_a: object, **_kw: object) -> None:
        applied.append("reconcile")

    async def resolve_agent(
        *_a: object, apply_callable: Callable[[], Awaitable[object]], **_kw: object
    ) -> str:
        await apply_callable()  # a total miss in the resolver
        return "agent_1"

    monkeypatch.setattr(lane_turn, "reconcile_tenant_defaults", reconcile)
    monkeypatch.setattr(lane_turn, "resolve_agent", resolve_agent)
    turn = HeadlessLaneTurn(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        make_settings(),
        SimpleNamespace(agent_name="Default"),  # type: ignore[arg-type]
        {},  # type: ignore[arg-type]
    )

    await turn.agent_id(uuid.uuid4(), agent)

    assert bool(applied) is healed, f"agent {agent!r}: defaults applied={applied}"
