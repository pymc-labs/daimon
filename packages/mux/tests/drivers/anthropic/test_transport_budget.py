"""Legacy turn calls bound SDK retry stacking and whole paginator walks."""

import asyncio

import httpx
import pytest
from anthropic import APIConnectionError, AsyncAnthropic, InternalServerError
from mux.drivers.anthropic import transport
from mux.drivers.anthropic.transport import LegacyTurnTransport, single_attempt_mutation


@pytest.mark.parametrize("operation", ["status", "replay", "latest_idle_is_settled"])
async def test_idempotent_reads_cancel_a_hung_request(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(transport, "READ_TIMEOUT_S", 0.1)
    cancelled = asyncio.Event()
    requests: list[httpx.Request] = []

    async def hang(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("request should have been cancelled")

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(hang)),
    ) as client:
        io = LegacyTurnTransport(client, "sess_1")
        with pytest.raises(TimeoutError):
            await getattr(io, operation)()
    assert cancelled.is_set()
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == 30.0


async def test_claimed_mutations_keep_zero_retries_and_original_timeout() -> None:
    attempts: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("offline", request=request)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        io = LegacyTurnTransport(client, "sess_1")
        with single_attempt_mutation(), pytest.raises(APIConnectionError):
            await io.send([{"type": "user.message", "content": "hi"}])
    assert len(attempts) == 1
    assert attempts[0].extensions["timeout"]["read"] == 600.0


async def test_status_uses_two_sdk_retries_even_when_shared_client_uses_eight() -> None:
    attempts: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500, headers={"retry-after-ms": "1"}, json={"error": "oops"})

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        with pytest.raises(InternalServerError):
            await LegacyTurnTransport(client, "sess_1").status()
        assert client.max_retries == 8
    assert len(attempts) == 3
    assert all(request.extensions["timeout"]["read"] == 30.0 for request in attempts)


async def test_explicit_zero_retry_client_stays_single_attempt() -> None:
    attempts: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("offline", request=request)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        with pytest.raises(APIConnectionError):
            await LegacyTurnTransport(client, "sess_1").send(
                [{"type": "user.message", "content": "hi"}]
            )
    assert len(attempts) == 1


@pytest.mark.parametrize("operation", ["message", "confirmation", "interrupt", "archive"])
async def test_accepted_mutation_with_lost_ack_is_one_post_at_original_timeout(
    operation: str,
) -> None:
    attempts: list[httpx.Request] = []

    async def accept_then_lose_ack(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("accepted; acknowledgement lost", request=request)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(accept_then_lose_ack)),
    ) as client:
        io = LegacyTurnTransport(client, "sess_1")
        from anthropic import APITimeoutError

        with pytest.raises(APITimeoutError):
            if operation == "archive":
                await io.archive()
            elif operation == "interrupt":
                await io.send([{"type": "user.interrupt"}])
            elif operation == "confirmation":
                await io.send(
                    [{"type": "user.tool_confirmation", "tool_use_id": "tu_1", "result": "allow"}]
                )
            else:
                await io.send([{"type": "user.message", "content": "hi"}])
    assert len(attempts) == 1
    assert attempts[0].method == "POST"
    assert attempts[0].extensions["timeout"]["read"] == 600.0


@pytest.mark.parametrize("operation", ["send", "confirmation", "cancel", "archive", "native-tool"])
async def test_all_turn_mutation_ports_disable_sdk_retries(operation: str) -> None:
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


async def test_attended_mutation_never_follows_an_accepted_post_redirect() -> None:
    from anthropic import APIStatusError

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
        with pytest.raises(APIStatusError) as failure:
            await LegacyTurnTransport(client, "sess_1").send(
                [{"type": "user.message", "content": "hi"}]
            )
    assert failure.value.status_code == 307
    assert len(attempts) == 1
    assert attempts[0].url.path == "/v1/sessions/sess_1/events"
    assert attempts[0].extensions["timeout"]["read"] == 600.0
