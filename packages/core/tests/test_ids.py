"""Unit tests for daimon.core.ids — the core-side request-id source.

Tests cover:
  - generate_request_id returns a non-empty string
  - two successive calls return distinct values (uniqueness)
"""

from __future__ import annotations

from daimon.core.ids import generate_request_id


def test_generate_request_id_returns_nonempty_string() -> None:
    """generate_request_id should yield a non-empty str usable as a correlation id."""
    rid = generate_request_id()

    assert isinstance(rid, str), "generate_request_id must return a str"
    assert rid, "generate_request_id must return a non-empty string"


def test_generate_request_id_returns_distinct_values_on_successive_calls() -> None:
    """Two successive calls must produce distinct ids (per-fire uniqueness)."""
    first = generate_request_id()
    second = generate_request_id()

    assert first != second, "successive generate_request_id calls must return distinct ids"
