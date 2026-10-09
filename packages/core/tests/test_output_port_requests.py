"""Session output migration preserves MA headers, page boundaries and SDK errors."""

from datetime import UTC, datetime
from typing import Literal

import httpx
import pytest
from anthropic import APIStatusError
from anthropic.types.beta import FileMetadata
from daimon.core import output_delivery
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.output_ports_compat import (
    delete_output_record,
    list_output_records,
    read_output_record,
)
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ResourceRef
from mux.drivers.anthropic.outputs import Outputs
from mux.errors import ScopeViolation
from pydantic import JsonValue

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SCOPE = resource_scope(tenant_id="tenant-a", account_id="account-a")
MA_BETA = "managed-agents-2026-04-01"


@pytest.mark.parametrize("operation", ["list", "read", "delete"])
@pytest.mark.parametrize("status", [200, 400, 404, 500])
async def test_output_requests_and_errors_match_the_original(
    operation: Literal["list", "read", "delete"], status: int
) -> None:
    metadata = FileMetadata(
        id="file_output",
        filename="output.txt",
        mime_type="text/plain",
        size_bytes=3,
        created_at=NOW,
        type="file",
        downloadable=True,
    )
    method, path = {
        "list": ("GET", "/v1/files"),
        "read": ("GET", "/v1/files/file_output/content"),
        "delete": ("DELETE", "/v1/files/file_output"),
    }[operation]
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        if status != 200:
            response = httpx.Response(
                status, json={"error": {"type": "api_error", "message": "failed"}}
            )
        elif operation == "read":
            response = httpx.Response(200, content=b"abc")
        elif operation == "delete":
            response = httpx.Response(200, json={"id": metadata.id, "type": "file_deleted"})
        else:
            # A next page must not be fetched: the old host reads this one page only.
            response = httpx.Response(
                200,
                json={
                    "data": [metadata.model_dump(mode="json")],
                    "has_more": True,
                    "first_id": metadata.id,
                    "last_id": metadata.id,
                },
            )
        transport.queue(ScriptedReply(method, path, response))
    async with old.client() as legacy, new.client() as client:
        if status == 200:
            if operation == "list":
                original = await legacy.beta.files.list(
                    scope_id="sess_output", betas=[MA_BETA], limit=1000
                )
                result = await list_output_records(client, "sess_output", scope=SCOPE)
                assert [r.model_dump(mode="json", exclude_unset=True) for r in result] == [
                    r.model_dump(mode="json", exclude_unset=True) for r in original.data
                ]
            elif operation == "read":
                original = await legacy.beta.files.download(metadata.id, betas=[MA_BETA])
                assert (
                    await read_output_record(client, metadata.id, scope=SCOPE)
                    == await original.read()
                )
            else:
                await legacy.beta.files.delete(metadata.id, betas=[MA_BETA])
                await delete_output_record(client, metadata.id, scope=SCOPE)
        else:
            with pytest.raises(APIStatusError) as original_error:
                if operation == "list":
                    await legacy.beta.files.list(
                        scope_id="sess_output", betas=[MA_BETA], limit=1000
                    )
                elif operation == "read":
                    await legacy.beta.files.download(metadata.id, betas=[MA_BETA])
                else:
                    await legacy.beta.files.delete(metadata.id, betas=[MA_BETA])
            with pytest.raises(type(original_error.value)) as migrated_error:
                if operation == "list":
                    await list_output_records(client, "sess_output", scope=SCOPE)
                elif operation == "read":
                    await read_output_record(client, metadata.id, scope=SCOPE)
                else:
                    await delete_output_record(client, metadata.id, scope=SCOPE)
            assert str(migrated_error.value) == str(original_error.value)
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


@pytest.mark.parametrize("status", [404, 500])
async def test_output_delete_keeps_the_host_suppression_boundary(status: int) -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "DELETE",
            "/v1/files/file_output",
            httpx.Response(
                status,
                json={"error": {"type": "api_error", "message": "gone or failed"}},
            ),
        )
    )
    async with transport.client() as client:
        await output_delivery.delete_output_file(
            client,
            session_id="sess_output",
            file_id="file_output",
            scope=SCOPE,
        )
    transport.assert_consumed()


@pytest.mark.parametrize("operation", ["list", "read", "delete"])
async def test_output_scope_rejections_precede_io(
    operation: Literal["list", "read", "delete"],
) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        backend = managed_agents(client, scope=SCOPE)
        port = backend.extension(Outputs, namespace="anthropic.outputs", version=1)
        kind = "session" if operation == "list" else "file"
        ref = resource_ref(backend, kind, "ungranted", scope=SCOPE)
        with pytest.raises(ScopeViolation):
            if operation == "list":
                await port.list_outputs(SCOPE, ref)
            elif operation == "read":
                await port.read_output(SCOPE, ref)
            else:
                await port.delete_output(SCOPE, ref, key="delete")
        granted = managed_agents(client, scope=SCOPE, resources=frozenset({(kind, "foreign")}))
        port = granted.extension(Outputs, namespace="anthropic.outputs", version=1)
        foreign = ResourceRef(
            id="foreign",
            kind=kind,
            provider="anthropic",
            account_scope_id=granted.account_scope_id,
            tenant_id="tenant-b",
            account_id=SCOPE.account_id,
        )
        with pytest.raises(ScopeViolation):
            if operation == "list":
                await port.list_outputs(SCOPE, foreign)
            elif operation == "read":
                await port.read_output(SCOPE, foreign)
            else:
                await port.delete_output(SCOPE, foreign, key="delete")
    assert transport.requests == []


@pytest.mark.parametrize(
    "record",
    [
        {"id": "file_output", "downloadable": False},
        {"downloadable": False},
        {"id": "file_output", "downloadable": True},
        {
            "id": "file_output",
            "downloadable": True,
            "filename": "output.txt",
            "mime_type": "text/plain",
            "size_bytes": 3,
            "provider_extra": {"retained": True},
        },
    ],
)
async def test_partial_listing_keeps_sdk_fields_and_poll_filtering(
    record: dict[str, JsonValue],
) -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        for _ in range(3):
            transport.queue(
                ScriptedReply(
                    "GET",
                    "/v1/files",
                    httpx.Response(200, json={"data": [record], "has_more": True}),
                )
            )

    async def sleep(_delay: float) -> None:
        pass

    async with old.client() as legacy, new.client() as client:
        originals: list[FileMetadata] = []
        for _ in range(3):
            page = await legacy.beta.files.list(scope_id="sess_output", betas=[MA_BETA], limit=1000)
            originals = page.data
        polled = await output_delivery._poll_until_settled(  # pyright: ignore[reportPrivateUsage]
            client, session_id="sess_output", sleep=sleep, scope=SCOPE
        )
        if record["downloadable"] is True:
            assert polled["file_output"].model_dump(mode="json", exclude_unset=True) == originals[
                0
            ].model_dump(mode="json", exclude_unset=True)
        else:
            assert polled == {}
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


@pytest.mark.parametrize("downloadable", [False, True])
async def test_public_sweep_preserves_partial_metadata_and_delivery(downloadable: bool) -> None:
    record: dict[str, JsonValue] = {"id": "file_output", "downloadable": downloadable}
    if downloadable:
        record.update(filename="output.txt", mime_type="text/plain", size_bytes=3)
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        for _ in range(3):
            transport.queue(
                ScriptedReply(
                    "GET",
                    "/v1/files",
                    httpx.Response(200, json={"data": [record], "has_more": False}),
                )
            )
        if downloadable:
            transport.queue(
                ScriptedReply(
                    "GET", "/v1/files/file_output/content", httpx.Response(200, content=b"abc")
                )
            )
            transport.queue(
                ScriptedReply(
                    "DELETE",
                    "/v1/files/file_output",
                    httpx.Response(200, json={"id": "file_output", "type": "file_deleted"}),
                )
            )
    posted: list[output_delivery.DeliverableFile] = []

    async def post(file: output_delivery.DeliverableFile) -> None:
        posted.append(file)

    async def sleep(_delay: float) -> None:
        pass

    async with old.client() as legacy, new.client() as client:
        for _ in range(3):
            await legacy.beta.files.list(scope_id="sess_output", betas=[MA_BETA], limit=1000)
        if downloadable:
            response = await legacy.beta.files.download("file_output", betas=[MA_BETA])
            assert await response.read() == b"abc"
            await legacy.beta.files.delete("file_output", betas=[MA_BETA])
        count = await output_delivery.sweep_session_outputs(
            client, session_id="sess_output", post=post, sleep=sleep, scope=SCOPE
        )
    assert count == int(downloadable)
    assert posted == (
        [output_delivery.DeliverableFile("file_output", "output.txt", "text/plain", 3, b"abc")]
        if downloadable
        else []
    )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
