"""Tests for the shared /agent-setup expiry helpers.

Covers ``build_expired_view``/``edit_expired_message`` in isolation, plus the
structural AST guard proving every view render site in ``agent_setup/`` and
``commands/agent_setup.py`` binds the interaction that rendered it.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.expiry import (
    build_expired_view,
    edit_expired_message,
)


def test_expired_view_names_the_command_and_carries_no_controls() -> None:
    view = build_expired_view()
    children = list(view.walk_children())

    assert not any(isinstance(c, discord.ui.Button) for c in children), (
        "the expired view must carry no interactive children"
    )
    assert not any(isinstance(c, discord.ui.Select) for c in children), (
        "the expired view must carry no interactive children"
    )

    text = "\n".join(c.content for c in children if isinstance(c, discord.ui.TextDisplay))
    assert "expired" in text.lower(), "the copy must say the panel expired"
    assert "/agent-setup" in text, "the copy must tell the user to re-run /agent-setup"


@pytest.mark.asyncio
async def test_edit_expired_message_does_nothing_without_a_bound_interaction() -> None:
    # A None interaction means the view was never rendered; nothing to touch.
    await edit_expired_message(build_expired_view(), interaction=None)


@pytest.mark.asyncio
async def test_edit_expired_message_swallows_not_found(mock_interaction: MagicMock) -> None:
    resp = MagicMock(status=404, reason="Not Found")
    mock_interaction.edit_original_response.side_effect = discord.NotFound(resp, "Unknown Webhook")

    await edit_expired_message(build_expired_view(), interaction=mock_interaction)

    mock_interaction.edit_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_edit_expired_message_propagates_other_http_errors(
    mock_interaction: MagicMock,
) -> None:
    resp = MagicMock(status=500, reason="Internal Server Error")
    mock_interaction.edit_original_response.side_effect = discord.HTTPException(resp, "boom")

    with pytest.raises(discord.HTTPException):
        await edit_expired_message(build_expired_view(), interaction=mock_interaction)


# ---------------------------------------------------------------------------
# Structural guard: every `view=` render site binds the render interaction.
# ---------------------------------------------------------------------------

_RENDER_SITE_MODULE_NAMES = [
    "daimon.adapters.discord.agent_setup.mcp_access",
    "daimon.adapters.discord.commands.agent_setup",
    "daimon.adapters.discord.agent_setup.roster_view",
    "daimon.adapters.discord.agent_setup.details_view",
    "daimon.adapters.discord.agent_setup.routing_view",
    "daimon.adapters.discord.agent_setup.new_agent",
    "daimon.adapters.discord.agent_setup.navigation",
    "daimon.adapters.discord.agent_setup.hydrate",
]


def _is_exempt_none(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def _is_exempt_static_view(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "static_view"
    )


def _is_bound(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "bind_render_interaction"
    )


def test_every_view_render_site_binds_the_render_interaction() -> None:
    """AST guard: every `view=` keyword is bound, `None`, or a static_view() call.

    This is the invariant this design depends on to make the AST guard's own
    scope enforceable: a render site that forgets to bind is invisible to
    pyright, so this test is the only thing standing in for that discipline.
    Do NOT narrow this guard's module list or add exemptions beyond the two
    named above to make a task pass — bind the missing site instead.
    """
    import importlib

    offenders: list[str] = []
    for module_name in _RENDER_SITE_MODULE_NAMES:
        module = importlib.import_module(module_name)
        source = pathlib.Path(str(module.__file__)).read_text()
        tree = ast.parse(source, filename=str(module.__file__))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "view":
                    continue
                value = kw.value
                if _is_exempt_none(value) or _is_exempt_static_view(value) or _is_bound(value):
                    continue
                offenders.append(f"{module.__file__}:{node.lineno}")

    assert not offenders, (
        f"every view= render site must chain .bind_render_interaction(...) — offenders: {offenders}"
    )
