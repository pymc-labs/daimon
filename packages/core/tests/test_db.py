from __future__ import annotations

from daimon.core.db import build_engine


def test_build_engine_hides_bound_parameters_in_errors() -> None:
    """SQL errors must not carry bound values (agent keys, tokens) into logs."""
    engine = build_engine("postgresql+asyncpg://u:p@localhost:1/d")

    assert engine.sync_engine.hide_parameters is True
