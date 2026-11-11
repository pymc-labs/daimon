"""Every refused provider change has one line and one way out, from one place."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core import agent_changes, agent_convert, backend_refusals
from daimon.core.agent_editing import AgentEditError
from daimon.core.authz import build_subject
from daimon.core.backend_refusals import (
    DETAIL_LINES,
    REFUSAL_REASONS,
    refusal_card,
    switch_back_to_claude,
)
from daimon.core.channel_backend import (
    REFUSAL_DETAILS,
    AgentBackendRefused,
    BackendActor,
    BackendUnsupported,
    channel_ref,
    current_backend,
    set_channel_backend,
)
from daimon.testing.factories import make_tenant
from mux.contracts.config import BackendConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PACKAGES = Path(__file__).parents[2]
FALLBACK = "This change was refused."


def _sources() -> list[tuple[Path, ast.Module]]:
    files = [
        path
        for path in PACKAGES.glob("*/**/daimon/**/*.py")
        if "/tests/" not in str(path) and "node_modules" not in str(path)
    ]
    return [(path, ast.parse(path.read_text())) for path in files]


def _raised_reasons() -> set[str]:
    reasons: set[str] = set()
    for _, tree in _sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func).split(".")[-1] == "AgentBackendRefused"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                reasons.add(node.args[0].value)
    return reasons


def test_every_reason_the_code_raises_has_a_line() -> None:
    """A reason added anywhere must be worded here, or it reads as the bare fallback."""
    raised = _raised_reasons()
    assert raised, "the scan found no AgentBackendRefused reasons"
    assert raised <= REFUSAL_REASONS, f"unworded reasons: {sorted(raised - REFUSAL_REASONS)}"
    for reason in sorted(REFUSAL_REASONS):
        line, _ = refusal_card(AgentBackendRefused(reason))
        assert line and line != FALLBACK, f"{reason} has no line of its own"
        assert "_" not in line, f"{reason}'s line shows a label: {line}"


def test_every_refusal_detail_has_a_line() -> None:
    """A closed detail never falls back to the label."""
    assert set(DETAIL_LINES) == REFUSAL_DETAILS
    for detail in sorted(REFUSAL_DETAILS):
        assert DETAIL_LINES[detail].endswith("."), detail
        line, action = refusal_card(AgentBackendRefused("re_confirm", detail=detail))
        assert line == "Confirm this switch again." and action == "confirm_again", detail
        said, _ = refusal_card(AgentBackendRefused("source_unavailable", detail=detail))
        assert "_" not in said and said != FALLBACK, detail


@pytest.mark.parametrize(
    ("err", "kwargs", "expected"),
    [
        (
            AgentBackendRefused("backend_not_allowed", provider="openai"),
            {"admin": True},
            ("OpenAI isn't turned on here.", "allowlist"),
        ),
        (
            AgentBackendRefused("backend_not_allowed", provider="openai"),
            {},
            ("OpenAI isn't turned on here. Ask an admin.", None),
        ),
        (
            AgentBackendRefused("feature_disabled", provider="selfhosted"),
            {},
            ("Self-hosted models aren't turned on here.", None),
        ),
        (
            AgentBackendRefused("admin_required_for_shared_threads"),
            {},
            ("A shared thread needs an admin to switch.", None),
        ),
        (
            AgentBackendRefused("drops_mismatch"),
            {},
            ("Review what changes first.", "fresh_preview"),
        ),
        (
            BackendUnsupported("profile_not_runnable"),
            {"provider": "openai"},
            ("OpenAI can't run right now.", "switch_back_to_claude"),
        ),
        (
            BackendUnsupported("gemini-4-argon's price window ended on 2026-11-30, so it can't."),
            {},
            (
                "gemini-4-argon's price window ended on 2026-11-30, so it can't.",
                "pick_another_model",
            ),
        ),
    ],
)
def test_the_table_lines_and_actions(
    err: AgentBackendRefused | BackendUnsupported, kwargs: dict[str, Any], expected: tuple
) -> None:
    assert refusal_card(err, **kwargs) == expected


def _refused_names(tree: ast.Module) -> list[tuple[int, str]]:
    """Names bound by `except ... AgentBackendRefused as name` that the handler formats."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ExceptHandler)
            and node.name
            and node.type is not None
            and "AgentBackendRefused" in ast.unparse(node.type)
        ):
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                formatted = (
                    isinstance(inner, ast.Call)
                    and ast.unparse(inner.func) in ("str", "repr")
                    and len(inner.args) == 1
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id == node.name
                ) or (
                    isinstance(inner, ast.FormattedValue)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == node.name
                )
                if formatted:
                    found.append((inner.lineno, node.name))
    return found


def test_no_surface_formats_the_refusal_itself() -> None:
    """The exception's text is a label: only `refusal_card` words a refusal."""
    offenders = [
        f"{path.relative_to(PACKAGES)}:{line}"
        for path, tree in _sources()
        for line, _ in _refused_names(tree)
    ]
    assert not offenders, f"format AgentBackendRefused through refusal_card: {offenders}"


def test_the_scan_catches_a_formatted_refusal() -> None:
    bad = ast.parse(
        "try:\n    pass\nexcept (AgentBackendRefused, Other) as exc:\n    say(str(exc))\n"
    )
    assert _refused_names(bad) == [(4, "exc")], "the scan must flag str(exc)"


def _actor(tenant_id: uuid.UUID) -> BackendActor:
    return BackendActor(
        tenant_id=tenant_id,
        account_id=uuid.uuid4(),
        subject=build_subject(is_admin=True, platform_user_id="admin", client=False),
    )


async def test_switch_back_clears_a_channel_pin_and_leaves_the_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a pin the channel is the cause, so only the pin goes."""
    tenant = await make_tenant(db_session)
    channel = channel_ref(tenant.id, "discord", "chan-1")
    await set_channel_backend(
        db_session,
        channel,
        BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-5"),
    )
    await db_session.commit()
    calls: list[object] = []

    async def fake_change_model(*args: object, **kwargs: object) -> None:
        calls.append(args)

    monkeypatch.setattr(agent_changes, "change_model", fake_change_model)
    said = await switch_back_to_claude(
        db_session_factory,
        object(),  # type: ignore[arg-type]
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        actor=object(),  # type: ignore[arg-type]
        backend_actor=_actor(tenant.id),
        edit=object(),  # type: ignore[arg-type]
        backends=None,
    )
    assert said == "Cleared this channel's provider pin." and not calls
    async with db_session_factory() as session:
        pin = await current_backend(session, channel)
    assert pin is not None and pin.backend == "anthropic", "the pin reads as cleared"


async def test_switch_back_moves_an_unpinned_agent_to_the_default_claude_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    calls: list[tuple[Any, dict[str, Any]]] = []

    async def fake_change_model(agent: object, model: Any, **kwargs: Any) -> None:
        calls.append((model, kwargs))

    monkeypatch.setattr(agent_changes, "change_model", fake_change_model)
    said = await switch_back_to_claude(
        db_session_factory,
        SimpleNamespace(source=None),  # type: ignore[arg-type]
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-2",
        actor="actor",  # type: ignore[arg-type]
        backend_actor=_actor(tenant.id),
        edit="edit",  # type: ignore[arg-type]
        backends=None,
    )
    assert said == "Switched back to Anthropic."
    ((model, kwargs),) = calls
    assert (model.provider, model.id) == ("anthropic", backend_refusals.DEFAULT_AGENT_MODEL)
    assert kwargs["actor"] == "actor" and kwargs["edit"] == "edit"


def _record_agent(tenant_id: uuid.UUID) -> Any:
    return SimpleNamespace(
        tenant_id=tenant_id,
        name="analyst",
        source=SimpleNamespace(ref=SimpleNamespace(id="dm_agent_x")),
    )


async def test_switch_back_converts_a_daimon_record_on_a_keyed_install(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Daimon record has no Claude twin: an admin's press converts it instead."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    names: list[str] = []

    async def fake_convert(*_args: object, name: str, **_kwargs: object) -> None:
        names.append(name)

    monkeypatch.setattr(agent_convert, "convert_agent_to_anthropic", fake_convert)
    said = await switch_back_to_claude(
        db_session_factory,
        _record_agent(tenant.id),
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-3",
        actor=SimpleNamespace(subject=build_subject(is_admin=True, platform_user_id="a")),  # type: ignore[arg-type]
        backend_actor=_actor(tenant.id),
        edit=SimpleNamespace(client=object(), public_url=None),  # type: ignore[arg-type]
        backends=None,
    )
    assert said == "Moved analyst to Anthropic.", f"the press converts: {said}"
    assert names == ["analyst"], "the record is converted once"


async def test_switch_back_refuses_a_daimon_record_without_a_key(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No Claude key: the press is refused with who can help."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(AgentEditError, match="Ask an admin"):
        await switch_back_to_claude(
            db_session_factory,
            _record_agent(tenant.id),
            tenant_id=tenant.id,
            platform="discord",
            channel_id="chan-4",
            actor=SimpleNamespace(subject=build_subject(is_admin=True, platform_user_id="a")),  # type: ignore[arg-type]
            backend_actor=_actor(tenant.id),
            edit=SimpleNamespace(client=None, public_url=None),  # type: ignore[arg-type]
            backends=None,
        )
