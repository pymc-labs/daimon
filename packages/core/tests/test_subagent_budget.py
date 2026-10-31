"""Tests for daimon.core.subagent_budget: each request's own spending limit."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from daimon.core.config import SubagentsSettings
from daimon.core.errors import TurnError
from daimon.core.subagent_budget import BudgetExhausted, raise_turn_budget
from daimon.core.subagents import subagents_override
from daimon.testing.turn_leg import LEG_SCOPE
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from mux.testing.drivers.anthropic.ma_models import ma_session

SESSION = "sesn_coord"


def _session_body(*, budget: bool = True, spent: str = "37") -> dict[str, Any]:
    body = ma_session(id=SESSION).model_dump(mode="json")
    body["usage"] = {**body.get("usage", {}), "list_cost": {"amount": spent, "currency": "USD"}}
    if budget:
        body["budget"] = {"type": "limit", "max_list_cost": {"amount": "1000", "currency": "USD"}}
    return body


def _client(session: dict[str, Any], updates: list[dict[str, Any]], *, update_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{SESSION}"
        if request.method == "GET":
            return httpx.Response(200, json=session)
        updates.append(json.loads(request.content))
        if update_status != 200:
            return httpx.Response(
                update_status,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "no"}},
            )
        return httpx.Response(200, json=session)

    return build_fake_anthropic(handler)


async def test_the_limit_is_what_the_session_spent_plus_one_requests_worth() -> None:
    updates: list[dict[str, Any]] = []
    with subagents_override(SubagentsSettings(enabled=True, turn_budget_usd=Decimal("10"))):
        await raise_turn_budget(
            _client(_session_body(spent="37"), updates),
            SESSION,
            scope=LEG_SCOPE,
        )
    assert updates == [
        {"budget": {"type": "limit", "max_list_cost": {"amount": "1037", "currency": "USD"}}}
    ]


async def test_a_session_without_a_limit_never_runs_a_request() -> None:
    updates: list[dict[str, Any]] = []
    with pytest.raises(TurnError, match="no spending limit"):
        await raise_turn_budget(
            _client(_session_body(budget=False), updates),
            SESSION,
            scope=LEG_SCOPE,
        )
    assert updates == []


async def test_a_limit_the_provider_refuses_refuses_the_turn() -> None:
    updates: list[dict[str, Any]] = []
    with pytest.raises(TurnError, match="spending limit") as raised:
        await raise_turn_budget(
            _client(_session_body(), updates, update_status=400),
            SESSION,
            scope=LEG_SCOPE,
        )
    assert raised.value.kind == "upstream"


async def test_a_session_already_at_its_limit_is_never_raised() -> None:
    """A raise would resume the helpers its limit paused."""
    updates: list[dict[str, Any]] = []
    with pytest.raises(BudgetExhausted):
        await raise_turn_budget(
            _client(_session_body(spent="1000"), updates), SESSION, scope=LEG_SCOPE
        )
    assert updates == []
