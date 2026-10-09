"""Temporary M0 codecs for session output consumers; no provider I/O here."""

from secrets import token_hex

from anthropic import AsyncAnthropic
from anthropic._models import construct_type_unchecked
from anthropic.types.beta import FileMetadata
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.mux_compat import legacy_call
from mux.contracts.ids import Scope
from mux.drivers.anthropic.outputs import Outputs


async def list_output_records(
    client: AsyncAnthropic, session_id: str, *, scope: Scope
) -> tuple[FileMetadata, ...]:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(Outputs, namespace="anthropic.outputs", version=1)
    records = await legacy_call(
        port.list_outputs(scope, resource_ref(backend, "session", session_id, scope=scope))
    )
    return tuple(
        construct_type_unchecked(value=record.native, type_=FileMetadata) for record in records
    )


async def read_output_record(client: AsyncAnthropic, file_id: str, *, scope: Scope) -> bytes:
    backend = managed_agents(client, scope=scope, resources=frozenset({("file", file_id)}))
    port = backend.extension(Outputs, namespace="anthropic.outputs", version=1)
    return await legacy_call(
        port.read_output(scope, resource_ref(backend, "file", file_id, scope=scope))
    )


async def delete_output_record(client: AsyncAnthropic, file_id: str, *, scope: Scope) -> None:
    backend = managed_agents(client, scope=scope, resources=frozenset({("file", file_id)}))
    port = backend.extension(Outputs, namespace="anthropic.outputs", version=1)
    await legacy_call(
        port.delete_output(
            scope, resource_ref(backend, "file", file_id, scope=scope), key=token_hex(16)
        )
    )
