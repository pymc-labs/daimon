"""Tests for daimon.core.sessions.SessionContext value object.

Frozen, kw-only, hashable dataclass with a single required ``is_admin`` field.
The optional-ness of the scope lives at the call site
(``ctx: SessionContext | None``); the type itself has no defaults and no None.
"""

from __future__ import annotations

import dataclasses

import pytest
from daimon.core.sessions import SessionContext


def test_session_context_is_frozen() -> None:
    ctx = SessionContext(is_admin=False)

    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.is_admin = True  # type: ignore[misc]


def test_session_context_is_hashable() -> None:
    a = SessionContext(is_admin=False)
    b = SessionContext(is_admin=False)

    assert a == b, "two SessionContext with same fields must compare equal"
    assert hash(a) == hash(b), "equal SessionContext must hash equal"

    holder = {a: "value"}
    assert holder[b] == "value", "SessionContext must work as a dict key"


def test_session_context_requires_is_admin() -> None:
    """is_admin is required at construction — no defaults."""
    with pytest.raises(TypeError):
        SessionContext()  # type: ignore[call-arg]


def test_session_context_importable_from_session_context_module() -> None:
    """Direct import path from the relocation module must work.

    Plan 37-02 moves the class out of ``sessions.py`` into a sibling module
    ``session_context.py`` so ``mcp_vault.py`` can import the type without
    creating a ``mcp_vault → sessions → mcp_vault`` import cycle.
    ``sessions`` re-exports the same class for adapter ergonomics — both
    paths must resolve to the same object.
    """
    from daimon.core.session_context import SessionContext as Direct
    from daimon.core.sessions import SessionContext as ReExported

    assert Direct is ReExported, (
        "session_context.SessionContext and sessions.SessionContext must be "
        "the same class (re-export, not duplicate)"
    )
