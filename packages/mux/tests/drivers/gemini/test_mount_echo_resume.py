"""A resumed or restarted turn clears only its own interaction's recorded mount echo.

Storage tokens are minted per dispatch and live only in transport memory, so a
later turn (or a restart) re-reading an earlier interaction cannot know the
token it echoes. The session record keeps a SHA-256 of each mount header sent,
keyed by interaction id; that digest, and only for that interaction, clears
the echo. The token itself is never persisted.
"""

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.mcp_auth import RedactedCredential
from mux.drivers.gemini.transport import Object, SDKTransport, mount_header_digest
from mux.errors import ProviderError
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage

OLD = RedactedCredential("offline-turn-one-mount-token")
NEW = RedactedCredential("offline-turn-two-mount-token")


def header(token: str) -> str:
    return "Bearer " + token


def completed(identity: str = "native", token: str = OLD) -> Object:
    return {
        "id": identity,
        "status": "completed",
        "created": "2026-10-22T13:02:25Z",
        "updated": "2026-10-22T13:08:12Z",
        "environment_id": "workspace",
        "environment": {
            "type": "remote",
            "network": {
                "allowlist": [
                    {
                        "domain": "storage.googleapis.com",
                        "transform": {"Authorization": header(token)},
                    }
                ]
            },
        },
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "answer"}]}],
    }


@asynccontextmanager
async def restarted(
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    bound: Mapping[str, str] | None = None,
) -> AsyncIterator[SDKTransport]:
    """A fresh protected transport: no dispatched tokens, only recorded digests."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        try:
            sdk = SDKTransport(client, protect_storage_credentials=True)
            for interaction, sent in (bound or {}).items():
                sdk.bind_mount_echo(interaction, {mount_header_digest(sent)})
            yield sdk
        finally:
            await client.aio.aclose()


def responder(body: Object, operation: str) -> Callable[[httpx.Request], httpx.Response]:
    if operation == "stream":
        frame = {"event_type": "interaction.completed", "interaction": body}
        return lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(frame) + "\n\n").encode(),
        )
    return lambda request: httpx.Response(200, json=body)


async def read(sdk: SDKTransport, operation: str) -> Object:
    if operation == "stream":
        return await anext(await sdk.open_stream("native"))
    return await getattr(sdk, operation)("native")


READS = ["get", "cancel", "stream"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_recorded_digest_clears_that_interactions_echo_after_a_restart(
    operation: str,
) -> None:
    async with restarted(responder(completed(), operation), bound={"native": header(OLD)}) as sdk:
        result = await read(sdk, operation)
    assert OLD not in json.dumps(result) and "Authorization" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_without_a_recorded_digest_a_restart_still_refuses(operation: str) -> None:
    async with restarted(responder(completed(), operation)) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await read(sdk, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_a_digest_for_interaction_a_never_clears_an_echo_on_b(operation: str) -> None:
    # Recorded for "turn-one", but the read (and echo) is interaction "native".
    async with restarted(responder(completed(), operation), bound={"turn-one": header(OLD)}) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await read(sdk, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_an_echo_owned_by_another_interaction_id_is_not_cleared(operation: str) -> None:
    # Bound for the read id, but the echoing interaction object claims another id.
    async with restarted(
        responder(completed(identity="someone-else"), operation),
        bound={"native": header(OLD)},
    ) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await read(sdk, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_a_digest_mismatch_refuses(operation: str) -> None:
    async with restarted(responder(completed(), operation), bound={"native": header(NEW)}) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await read(sdk, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_the_digest_matched_token_anywhere_else_still_refuses(operation: str) -> None:
    body = completed()
    body["steps"][0]["content"][0]["text"] = "leak " + OLD  # type: ignore[index]
    async with restarted(responder(body, operation), bound={"native": header(OLD)}) as sdk:
        with pytest.raises(ProviderError, match="gcs_token_echo") as error:
            await read(sdk, operation)
    assert OLD not in str(error.value)


SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="t", platform="discord", channel_id="c"), thread_id="th"
)


class EchoTransport(FakeTransport):
    """A fake with the transport's digest surface, recording what Base binds."""

    def __init__(self, dispatched: dict[str, set[str]] | None = None) -> None:
        super().__init__()
        self.dispatched = dispatched or {}
        self.bound: list[tuple[str, frozenset[str]]] = []

    def mount_echo_digests(self, interaction_id: str) -> frozenset[str]:
        return frozenset(self.dispatched.get(interaction_id, ()))

    def bind_mount_echo(self, interaction_id: str, digests: frozenset[str]) -> None:
        self.bound.append((interaction_id, frozenset(digests)))


def native(identity: str, status: str) -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": identity,
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "workspace",
        "steps": [],
    }


async def backend(
    transport: EchoTransport, storage: MemoryStorage, state: MemoryStateStore
) -> GeminiManagedAgents:
    return GeminiManagedAgents(
        transport, storage=storage, state_store=state, account_scope_id="project"
    )


@pytest.mark.asyncio
async def test_base_persists_digests_at_acceptance_and_binds_them_after_a_restart() -> None:
    storage, state = MemoryStorage(), MemoryStateStore()
    first = EchoTransport({"turn-one": {mount_header_digest(header(OLD))}})
    ma = await backend(first, storage, state)
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    env = await ma.environments.create(SCOPE, EnvironmentSpec(name="e"), key="env")
    session = await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"thread": THREAD.model_dump(mode="json"), "binding_id": "b"},
                )
            },
        ),
        key="session",
    )
    first.responses.append(native("turn-one", "in_progress"))
    await ma.events.send(
        SCOPE, session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="one"
    )
    async with storage.transaction() as records:
        persisted = records.sessions[session.ref.id].mount_echo_digests
    assert persisted == {"turn-one": [mount_header_digest(header(OLD))]}

    # Restart: a fresh transport with no dispatched tokens or digests.
    second = EchoTransport()
    second.saved["turn-one"] = native("turn-one", "completed")
    restarted_ma = await backend(second, storage, state)
    await restarted_ma.events.reconcile(SCOPE, session.ref)
    await restarted_ma.sessions.retrieve(SCOPE, session.ref)
    expected = ("turn-one", frozenset({mount_header_digest(header(OLD))}))
    assert second.bound and all(binding == expected for binding in second.bound)
    # Only digests are persisted; the token appears in no stored record.
    async with storage.transaction() as records:
        assert OLD not in repr(records)


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param({"Authorization": "bearer " + OLD}, id="lowercase-scheme"),
        pytest.param({"authorization": "Bearer " + OLD}, id="lowercase-key"),
        pytest.param({"Authorization": "Bearer  " + OLD}, id="double-space"),
        pytest.param({"Authorization": " Bearer " + OLD}, id="leading-space"),
        pytest.param({"Authorization": "Bearer " + OLD + " "}, id="trailing-space"),
        pytest.param({"Authorization": "Bearer " + OLD + "\n"}, id="trailing-newline"),
        pytest.param({"Authorization": "Bearer " + OLD.upper()}, id="token-case"),
        pytest.param({"Authorization": "Bearer " + OLD, "X-Extra": "1"}, id="extra-key"),
        pytest.param({"Authorization": ["Bearer " + OLD]}, id="list-value"),
        pytest.param("Bearer " + OLD, id="not-an-object"),
        pytest.param({"Authorization": "Bearer "}, id="empty-token"),
    ],
)
@pytest.mark.asyncio
async def test_only_the_exact_recorded_header_clears_an_echo(transform: object) -> None:
    body = completed()
    body["environment"]["network"]["allowlist"][0]["transform"] = transform  # type: ignore[index]
    async with restarted(responder(body, "get"), bound={"native": header(OLD)}) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo") as error:
            await sdk.get("native")
    assert OLD not in str(error.value) and OLD.upper() not in str(error.value)


def test_mount_echo_book_is_a_bounded_lru() -> None:
    from mux.drivers.gemini.transport import MountEchoBook

    book = MountEchoBook(limit=3)
    for name in ("a", "b", "c"):
        book.add(name, {name + "-digest"})
    assert book.get("a") == {"a-digest"}  # use refreshes "a"
    book.add("d", {"d-digest"})  # evicts the least recently used: "b"
    assert len(book) == 3
    assert book.get("b") == frozenset()
    assert {name: book.get(name) for name in ("a", "c", "d")} == {
        "a": {"a-digest"},
        "c": {"c-digest"},
        "d": {"d-digest"},
    }
    book.add("a", {"a-second"})  # merges and refreshes, never grows past the limit
    assert book.get("a") == {"a-digest", "a-second"} and len(book) == 3
    book.add("e", set())  # nothing to record
    assert len(book) == 3 and book.get("e") == frozenset()
    with pytest.raises(ValueError):
        MountEchoBook(limit=0)


@pytest.mark.asyncio
async def test_an_evicted_digest_fails_closed_until_the_driver_rebinds_it() -> None:
    from mux.drivers.gemini.transport import MountEchoBook

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder(completed(), "get"))
    ) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        sdk = SDKTransport(
            client, protect_storage_credentials=True, mount_echoes=MountEchoBook(limit=1)
        )
        sdk.bind_mount_echo("native", {mount_header_digest(header(OLD))})
        sdk.bind_mount_echo("another", {mount_header_digest(header(NEW))})  # evicts "native"
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await sdk.get("native")
        # Base binds the session's persisted digests before every read.
        sdk.bind_mount_echo("native", {mount_header_digest(header(OLD))})
        assert OLD not in json.dumps(await sdk.get("native"))
        await client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_recorded_digest_clears_the_real_list_shaped_echo_after_a_restart(
    operation: str,
) -> None:
    # Gemini echoes the transform as a list of header objects (F2-G r13).
    body = completed()
    body["environment"]["network"]["allowlist"][0]["transform"] = [  # type: ignore[index]
        {"Authorization": header(OLD)}
    ]
    async with restarted(responder(body, operation), bound={"native": header(OLD)}) as sdk:
        result = await read(sdk, operation)
    assert OLD not in json.dumps(result) and "Authorization" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_a_list_shaped_echo_needs_its_own_interactions_digest(operation: str) -> None:
    body = completed()
    body["environment"]["network"]["allowlist"][0]["transform"] = [  # type: ignore[index]
        {"Authorization": header(OLD)}
    ]
    async with restarted(responder(body, operation), bound={"turn-one": header(OLD)}) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await read(sdk, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READS)
async def test_a_digest_matched_token_used_as_a_key_refuses_and_is_never_logged(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    body = completed()
    body["environment"]["network"]["allowlist"][0]["transform"] = [  # type: ignore[index]
        {"Authorization": header(OLD)}
    ]
    body["steps"][0]["arguments"] = {"network": {header(OLD): {"transform": 1}}}  # type: ignore[index]
    caplog.set_level(logging.DEBUG)
    async with restarted(responder(body, operation), bound={"native": header(OLD)}) as sdk:
        with pytest.raises(ProviderError, match="gcs_token_echo") as error:
            await read(sdk, operation)
    assert OLD not in caplog.text and OLD not in str(error.value)
