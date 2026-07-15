"""Tests for daimon.core.defaults.preflight."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.defaults.preflight import check_model_accepted, check_models_accepted
from daimon.testing.ma import MARouter, build_fake_anthropic


def _err(status: int, type_: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": type_, "message": message}}
    )


pytestmark = pytest.mark.asyncio


_AGENT_RESPONSE = {
    "id": "agent_probe",
    "type": "agent",
    "name": "daimon-preflight-xxxx",
    "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
    "metadata": {"daimon_preflight": "true"},
    "description": None,
    "archived_at": None,
    "created_at": "2026-05-21T18:00:00Z",
    "updated_at": "2026-05-21T18:00:00Z",
    "version": 1,
    "mcp_servers": [],
    "skills": [],
    "tools": [],
    "system": None,
}


async def test_check_model_accepted_returns_none_when_create_succeeds() -> None:
    """Happy path: MA accepts the model. Probe creates + archives, returns None."""
    router = MARouter()
    archive_calls: list[str] = []

    def on_create(req, _m):
        from httpx import Response

        return Response(200, json=_AGENT_RESPONSE)

    def on_archive(req, _m):
        from httpx import Response

        archive_calls.append(req.url.path)
        return Response(200, json=_AGENT_RESPONSE)

    router.add("POST", r"/v1/agents$", on_create)
    router.add("POST", r"/v1/agents/agent_probe/archive", on_archive)

    client = build_fake_anthropic(router.dispatch)

    result = await check_model_accepted(client, "claude-sonnet-4-6")
    assert result is None, "accepted model must return None"
    assert archive_calls == ["/v1/agents/agent_probe/archive"], (
        "probe agent must be archived to avoid leaking"
    )


async def test_check_model_accepted_returns_reason_on_400_model_not_supported() -> None:
    """The exact failure mode from 2026-05-21: 400 with 'model is not supported'.
    Probe captures the reason and returns it — caller decides whether to abort.
    """
    router = MARouter()
    router.add(
        "POST",
        r"/v1/agents$",
        lambda req, _m: _err(
            400,
            "invalid_request_error",
            '`model.id`: model "claude-sonnet-4-6": model is not supported',
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    result = await check_model_accepted(client, "claude-sonnet-4-6")
    assert result is not None, "rejection must return a reason string, not None"
    assert "claude-sonnet-4-6" in result, "reason must include the rejected model id"
    assert "not supported" in result, "reason must surface MA's message"


async def test_check_model_accepted_reraises_on_unrelated_400() -> None:
    """A 400 that isn't model-allowlist-related should propagate, not be swallowed."""
    from anthropic import APIStatusError

    router = MARouter()
    router.add(
        "POST",
        r"/v1/agents$",
        lambda req, _m: _err(400, "invalid_request_error", "name: too long"),
    )
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(APIStatusError):
        await check_model_accepted(client, "claude-sonnet-4-6")


async def test_check_models_accepted_aggregates_mixed_results() -> None:
    """Multiple models in one pass: dict has one entry per model with its verdict."""
    router = MARouter()
    create_count = {"n": 0}

    def on_create(req, _m):
        import json as _json

        from httpx import Response

        create_count["n"] += 1
        body = _json.loads(req.content)
        if "rejected" in body["model"]:
            return _err(
                400,
                "invalid_request_error",
                f'`model.id`: model "{body["model"]}": model is not supported',
            )
        # ship a response that pretends id matches probe name uniqueness isn't enforced
        return Response(200, json={**_AGENT_RESPONSE, "id": f"agent_probe_{create_count['n']}"})

    def on_archive(req, _m):
        from httpx import Response

        return Response(200, json=_AGENT_RESPONSE)

    router.add("POST", r"/v1/agents$", on_create)
    router.add("POST", r"/v1/agents/[^/]+/archive", on_archive)

    client = build_fake_anthropic(router.dispatch)

    results = await check_models_accepted(client, {"claude-sonnet-4-6", "claude-rejected-x"})
    assert results["claude-sonnet-4-6"] is None
    assert results["claude-rejected-x"] is not None
    assert "claude-rejected-x" in results["claude-rejected-x"]
