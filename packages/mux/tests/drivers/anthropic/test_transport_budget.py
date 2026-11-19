"""A turn mutation is one SDK attempt at its original timeout that never follows a redirect."""

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from mux.drivers.anthropic.transport import mutation_client, single_attempt_mutation


async def test_a_claimed_mutation_never_retries_or_follows_an_accepted_post_redirect() -> None:
    attempts: list[httpx.Request] = []

    async def accepted_redirect(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(307, headers={"location": "/v1/sessions/other/events"})

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(accepted_redirect), follow_redirects=True
        ),
    ) as client:
        with single_attempt_mutation(), pytest.raises(APIStatusError) as failure:
            await mutation_client(client).beta.sessions.events.send(
                "sess_1", events=[{"type": "user.message", "content": "hi"}]
            )
        assert client.max_retries == 8, "the shared client is unchanged"
    assert failure.value.status_code == 307
    assert [request.url.path for request in attempts] == ["/v1/sessions/sess_1/events"]


@pytest.mark.parametrize("operation", ["send", "confirmation", "cancel", "archive", "native-tool"])
async def test_all_turn_mutation_ports_disable_sdk_retries(operation: str) -> None:
    """Each mux mutation port is one POST on its own: no single_attempt_mutation() around it."""
    from mux.contracts.actions import UserMessage, UserToolConfirmation
    from mux.contracts.events import TextPart
    from mux.contracts.ids import ResourceRef, Scope
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
    from mux.drivers.anthropic.resources.session_tools import AnthropicSessionTools, SessionSend
    from mux.drivers.anthropic.resources.sessions_admin import AnthropicSessionAdmin
    from mux.drivers.anthropic.turn import AnthropicEvents
    from mux.errors import ProviderError

    scope = Scope(
        tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
    )
    session = ResourceRef(
        id="sess_1",
        kind="session",
        provider="anthropic",
        account_scope_id="workspace",
        tenant_id="tenant",
        account_id="account",
    )
    auth = ResourceAuthorization(scope, frozenset({("session", "sess_1")}))
    attempts: list[httpx.Request] = []

    async def accept_then_lose_ack(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("accepted; acknowledgement lost", request=request)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(accept_then_lose_ack)),
    ) as client:
        events = AnthropicEvents(client, "workspace", auth)
        if operation in {"send", "confirmation"}:
            result = await events.send(
                scope,
                session,
                [UserMessage(content=(TextPart(text="hi"),))]
                if operation == "send"
                else [UserToolConfirmation(action_id="tu_1", decision="allow")],
                key="accepted",
            )
            assert result.status == "outcome_unknown"
        elif operation == "cancel":
            cancelled = await events.cancel(scope, session, turn_id="turn", key="accepted")
            assert cancelled.status == "outcome_unknown"
        else:
            with pytest.raises(ProviderError):
                if operation == "archive":
                    await AnthropicSessionAdmin(client, "workspace", authorization=auth).archive(
                        scope, session, key="accepted"
                    )
                else:
                    await AnthropicSessionTools(client, "workspace", auth).send(
                        scope,
                        session,
                        SessionSend.model_validate(
                            {
                                "events": [
                                    {
                                        "type": "user.message",
                                        "content": [{"type": "text", "text": "hi"}],
                                    }
                                ]
                            }
                        ),
                        key="accepted",
                    )
    assert len(attempts) == 1
    assert attempts[0].method == "POST"
    assert attempts[0].extensions["timeout"]["read"] == 600.0
