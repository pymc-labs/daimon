"""Full workspace enumeration preserves the SDK paginator and native snapshot."""

import httpx
import pytest
from anthropic import NotFoundError
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions, SessionWalk
from mux.errors import ProviderError, ScopeViolation

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)


@pytest.mark.parametrize(
    "empty,cursor", [(False, "next"), (True, "next"), (False, ""), (False, None)]
)
async def test_walk_matches_sdk_requests_and_terminal_rules(empty, cursor):
    first = [] if empty else [ma_session(id="sess_1").model_dump(mode="json")]
    if first:
        first[0]["future_field"] = {"kept": True}
    second = [
        ma_session(id="sess_2", metadata={"daimon_tenant": "another"}).model_dump(mode="json")
    ]
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions",
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
        if not empty and cursor:
            transport.queue(
                ScriptedReply(
                    "GET",
                    "/v1/sessions",
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
    scope = Scope.platform(reason="workspace-wide billing sweep", authorization_id="billing")
    async with old.client() as legacy, new.client() as client:
        expected = [
            item.model_dump(mode="json", exclude_unset=True)
            async for item in legacy.beta.sessions.list()
        ]
        backend = AnthropicManagedAgents(client, account_scope_id="org")
        port = backend.extension(SessionWalk, namespace="anthropic.session_walk", version=1)
        actual = [item.native async for item in port.walk(scope)]
        assert actual == expected
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


async def test_walk_rejects_scope_before_io_and_filters_tenant_results():
    transport = ScriptedTransport()
    async with transport.client() as client:
        port = AnthropicSessions(client, "org", ResourceAuthorization(SCOPE))
        with pytest.raises(ScopeViolation):
            _ = [item async for item in port.walk(SCOPE.model_copy(update={"tenant_id": "other"}))]
    assert not transport.requests
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions",
            httpx.Response(
                200,
                json={
                    "data": [
                        ma_session(id="allowed").model_dump(mode="json"),
                        ma_session(id="ungranted").model_dump(mode="json"),
                        ma_session(id="foreign", metadata={"daimon_tenant": "other"}).model_dump(
                            mode="json"
                        ),
                    ],
                    "next_page": None,
                    "prev_page": None,
                },
            ),
        )
    )
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(
                SCOPE,
                frozenset({("session", "allowed"), ("session", "foreign")}),
            ),
        )
        assert [item.ref.id async for item in port.walk(SCOPE)] == ["allowed"]
    transport.assert_consumed()


async def test_walk_normalizes_sdk_error_without_an_extra_lookup():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions",
            httpx.Response(
                404,
                json={
                    "error": {"type": "not_found_error", "message": "gone"},
                },
            ),
        )
    )
    async with transport.client() as client:
        port = AnthropicSessions(client, "org", ResourceAuthorization(SCOPE))
        with pytest.raises(ProviderError) as error:
            _ = [item async for item in port.walk(SCOPE)]
        assert isinstance(error.value.__cause__, NotFoundError)
    transport.assert_consumed()
