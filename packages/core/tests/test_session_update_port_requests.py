"""The M0 session update codec preserves native wire bytes and busy errors."""

from typing import cast

import httpx
import pytest
from anthropic import BadRequestError
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
)
from daimon.core.session_ports_compat import update_session_record
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Revision, Scope

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)


async def test_update_codec_preserves_native_request_bytes() -> None:
    agent = cast(
        BetaManagedAgentsSessionAgentUpdateParam,
        {
            "tools": [
                {
                    "configs": [],
                    "default_config": {"enabled": False},
                    "type": "agent_toolset_20260401",
                }
            ],
            "mcp_servers": [],
        },
    )
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions/sess_1",
                httpx.Response(200, json=ma_session(id="sess_1").model_dump(mode="json")),
            )
        )
    async with old.client() as legacy, new.client() as client:
        await legacy.beta.sessions.update("sess_1", agent=agent)
        await update_session_record(
            client, "sess_1", scope=SCOPE, agent_id="ag_1", revision=Revision(local=7), agent=agent
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


async def test_update_codec_preserves_existing_busy_exception() -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions/sess_1",
            httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Cannot update agent while session is running",
                    }
                },
            ),
        )
    )
    async with transport.client() as client:
        with pytest.raises(BadRequestError, match="while session is running"):
            await update_session_record(
                client,
                "sess_1",
                scope=SCOPE,
                agent_id="ag_1",
                revision=Revision(local=7),
                agent={"tools": [], "mcp_servers": []},
            )
    transport.assert_consumed()


async def test_resource_delete_codec_matches_legacy_request() -> None:
    from daimon.core.session_ports_compat import remove_session_resource_record

    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "DELETE",
                "/v1/sessions/sess_1/resources/resource_1",
                httpx.Response(200, json={"id": "resource_1", "type": "session_resource_deleted"}),
            )
        )
    async with old.client() as legacy, new.client() as client:
        await legacy.beta.sessions.resources.delete("resource_1", session_id="sess_1")
        await remove_session_resource_record(client, "sess_1", "resource_1", scope=SCOPE)
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


async def test_repository_rotation_matches_legacy_request() -> None:
    from daimon.core.mux_compat import rotate_session_repo_token

    body = {
        "id": "repo_1",
        "type": "github_repository",
        "mount_path": "/repo",
        "url": "https://github.com/example/repo",
        "created_at": "2026-10-09T00:00:00Z",
        "updated_at": "2026-10-09T00:00:00Z",
    }
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions/sess_1/resources/repo_1",
                httpx.Response(200, json=body),
            )
        )
    async with old.client() as legacy, new.client() as client:
        await legacy.beta.sessions.resources.update(
            "repo_1", session_id="sess_1", authorization_token="dummy-rotation-token"
        )
        await rotate_session_repo_token(
            client, "sess_1", "repo_1", "dummy-rotation-token", scope=SCOPE
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
