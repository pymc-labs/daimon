"""Lazy .env discovery and mount identity preserve exact SDK paging and requests."""

from datetime import UTC, datetime

import httpx
import pytest
from anthropic import APIStatusError
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from daimon.core.session_ports_compat import add_session_file_record
from daimon.core.session_update_ops import _find_env_resource_id
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)
NOW = datetime(2026, 10, 9, tzinfo=UTC).isoformat()


def file_mount(resource_id, path):
    return {
        "id": resource_id,
        "type": "file",
        "file_id": f"file_{resource_id}",
        "mount_path": path,
        "created_at": NOW,
        "updated_at": NOW,
    }


@pytest.mark.parametrize("scenario", ["first", "second", "none", "empty", "empty_cursor"])
async def test_find_first_env_matches_the_original_lazy_paginator(scenario):
    first = [file_mount("other", "/report.txt")]
    cursor = "page2"
    second = [file_mount("env2", "/nested/.env"), file_mount("env3", "/other/.env")]
    if scenario == "first":
        first = [
            file_mount("env1", "/first/.env"),
            {"type": "memory_store", "memory_store_id": "mem"},
        ]
    elif scenario == "none":
        second = [file_mount("no_env", "/nested/config.txt")]
    elif scenario == "empty":
        first = []
    elif scenario == "empty_cursor":
        cursor = ""
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/sess_1/resources",
                httpx.Response(
                    200,
                    json={
                        "data": first,
                        "next_page": cursor,
                        "prev_page": None,
                    },
                ),
            )
        )
        if scenario in {"second", "none"}:
            transport.queue(
                ScriptedReply(
                    "GET",
                    "/v1/sessions/sess_1/resources",
                    httpx.Response(
                        200,
                        json={
                            "data": second,
                            "next_page": None,
                            "prev_page": None,
                        },
                    ),
                )
            )
    async with old.client() as legacy, new.client() as client:
        expected = None
        async for resource in legacy.beta.sessions.resources.list("sess_1"):
            if isinstance(resource, BetaManagedAgentsFileResource) and resource.mount_path.endswith(
                ".env"
            ):
                expected = resource.id
                break
        actual = await _find_env_resource_id(client, "sess_1", scope=SCOPE)
    old.assert_consumed()
    new.assert_consumed()
    assert actual == expected
    assert old.requests == new.requests


@pytest.mark.parametrize("status", [200, 400, 404, 500])
async def test_add_file_preserves_wire_errors_and_the_returned_resource_identity(status):
    # The old caller only needs id; partial SDK responses must remain usable.
    body = (
        {"id": "new_mount"}
        if status == 200
        else {"error": {"type": "api_error", "message": "failed"}}
    )
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "POST", "/v1/sessions/sess_1/resources", httpx.Response(status, json=body)
            )
        )
    async with old.client() as legacy, new.client() as client:
        if status == 200:
            expected = await legacy.beta.sessions.resources.add(
                "sess_1", file_id="file_env", type="file", mount_path=".env"
            )
            actual = await add_session_file_record(
                client, "sess_1", file_id="file_env", mount_path=".env", scope=SCOPE
            )
            assert actual.id == expected.id
            assert actual.resource.id == "file_env"
            assert actual.target_path == ".env"
        else:
            with pytest.raises(APIStatusError) as expected:
                await legacy.beta.sessions.resources.add(
                    "sess_1", file_id="file_env", type="file", mount_path=".env"
                )
            with pytest.raises(type(expected.value)) as actual:
                await add_session_file_record(
                    client, "sess_1", file_id="file_env", mount_path=".env", scope=SCOPE
                )
            assert str(actual.value) == str(expected.value)
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
