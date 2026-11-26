"""Every switch surface captures the agent's source; read-only callers say so."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core import agent_changes, backend_capture
from daimon.core.agent_changes import AgentChangeDeps, check_provider_switch
from daimon.core.agent_editing import AgentActor
from daimon.core.authz import Subject, Surface
from daimon.core.channel_backend import (
    NO_CAPTURE,
    AgentBackendRefused,
    BackendActor,
    BackendAgent,
    make_backend_service,
)
from daimon.core.config import TurnSettings

PACKAGES = Path(__file__).resolve().parents[2]
WRITES = frozenset({"set_backend", "clear_backend", "set_settings", "revert_backend"})


def _sources() -> list[Path]:
    roots = [*PACKAGES.glob("*/daimon"), *PACKAGES.glob("adapters/*/daimon")]
    return [path for root in roots for path in root.rglob("*.py")]


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _capture_argument(call: ast.Call) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == "capture_source"), None)


def _is_no_capture(value: ast.expr) -> bool:
    return (isinstance(value, ast.Name) and value.id == "NO_CAPTURE") or (
        isinstance(value, ast.Attribute) and value.attr == "NO_CAPTURE"
    )


def test_every_backend_service_names_its_capture_source() -> None:
    missing: list[str] = []
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and _callee(node) == "make_backend_service"
                and _capture_argument(node) is None
            ):
                missing.append(f"{path.relative_to(PACKAGES)}:{node.lineno}")
    assert not missing, f"make_backend_service without capture_source: {missing}"


def test_read_only_callers_never_switch_an_agent() -> None:
    offenders: list[str] = []
    for path in _sources():
        for scope in ast.walk(ast.parse(path.read_text())):
            if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [n for n in ast.walk(scope) if isinstance(n, ast.Call)]
            read_only = any(
                _callee(c) == "make_backend_service"
                and (arg := _capture_argument(c)) is not None
                and _is_no_capture(arg)
                for c in calls
            )
            if read_only and any(_callee(c) in WRITES for c in calls):
                offenders.append(f"{path.relative_to(PACKAGES)}:{scope.name}")
    assert not offenders, f"NO_CAPTURE callers that write: {offenders}"


async def test_a_read_only_service_refuses_every_write() -> None:
    service = make_backend_service(None, TurnSettings(), capture_source=NO_CAPTURE)  # type: ignore[arg-type]
    tenant = uuid.uuid4()
    agent = BackendAgent(
        tenant_id=tenant, agent_id=uuid.uuid4(), name="helper", configuration_digest="c"
    )
    actor = BackendActor(tenant, uuid.uuid4(), Subject(is_admin=True))
    with pytest.raises(AgentBackendRefused) as refused:
        await service.set_backend(
            agent,
            "openai",
            0,
            actor,
        )
    assert refused.value.reason == "source_unavailable"
    with pytest.raises(AgentBackendRefused) as refused:
        await service.clear_backend(agent, 0, actor)
    assert refused.value.reason == "source_unavailable"


async def test_place_less_capture_without_turns_is_refused() -> None:
    capture = backend_capture.for_agent(None, account_id=uuid.uuid4(), subject=Subject())
    agent = BackendAgent(
        tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), name="helper", configuration_digest="c"
    )
    with pytest.raises(AgentBackendRefused) as refused:
        await capture(agent)
    assert refused.value.reason == "source_unavailable"


def _switch_deps(monkeypatch: pytest.MonkeyPatch, *, drops: tuple[str, ...]) -> AgentChangeDeps:
    preview = SimpleNamespace(
        allowed=True, declared_drops=drops, digest="digest-1", setting_revision=3
    )

    class Backends:
        enabled = True

        async def may_edit(self, *_: Any) -> bool:
            return True

        async def preview(self, *_: Any) -> Any:
            return preview

    async def load(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(name="helper")

    monkeypatch.setattr(agent_changes, "load_backend_agent", load)
    edit = SimpleNamespace(client=None, session_factory=None)
    return AgentChangeDeps(edit=edit, view=None, backends=Backends())  # type: ignore[arg-type]


async def _check(deps: AgentChangeDeps, **kwargs: Any) -> Any:
    actor = AgentActor(
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        subject=Subject(is_admin=True),
        surface=Surface.WEB,
    )
    agent = SimpleNamespace(id="agent-1", name="helper", metadata={})
    return await check_provider_switch(
        deps,
        actor=actor,
        agent=agent,  # type: ignore[arg-type]
        provider="openai",
        model_id="gpt-6-luna",
        **kwargs,
    )


@pytest.mark.parametrize("drops", [(), ("memory",)])
async def test_a_switch_is_checked_with_no_acceptance(
    monkeypatch: pytest.MonkeyPatch, drops: tuple[str, ...]
) -> None:
    """Losses or none, the check passes on its own; the preview says what is off."""
    switch = await _check(_switch_deps(monkeypatch, drops=drops))
    assert switch.preview.declared_drops == drops, "the preview carries what is off"
    assert switch.expected_revision == 3, "the preview's revision guards the write"
