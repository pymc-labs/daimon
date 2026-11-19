"""A claimed turn mutation is one SDK attempt that never follows a redirect."""

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
