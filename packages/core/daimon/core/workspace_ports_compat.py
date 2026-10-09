"""Request-local native export/restore bridge; no provider requests or storage."""

from secrets import token_hex

from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_file_resource_params import (
    BetaManagedAgentsFileResourceParams,
)
from daimon.core.mux_backend import managed_agents, resource_ref
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import Scope
from mux.drivers.anthropic.sessions_lifecycle import WorkspaceTransfer


def restore_transfer_records(
    client: AsyncAnthropic, source_session_id: str, *, scope: Scope, inline: ExtensionConfig
) -> tuple[BetaManagedAgentsFileResourceParams, ...]:
    grants = {("session", source_session_id)}
    # The host grants the standalone archive it just uploaded. The closed
    # schema and all reference checks run in the driver before restore.
    outcome = inline.value.get("outcome")
    if isinstance(outcome, dict):
        file_id = outcome.get("file_id")
        if isinstance(file_id, str):
            grants.add(("file", file_id))
    backend = managed_agents(client, scope=scope, resources=frozenset(grants))
    port = backend.extension(WorkspaceTransfer, namespace="anthropic.workspace_transfer", version=1)
    export = port.export(
        scope,
        resource_ref(backend, "session", source_session_id, scope=scope),
        inline=inline,
        key=token_hex(16),
    )
    # Today's host ladder explicitly accepts these degradations and renders
    # the exact loss notice in the successor's first turn. No silent fresh start.
    bindings = port.restore(
        scope,
        export,
        inline=inline,
        accept_losses=frozenset(export.losses),
        key=token_hex(16),
    )
    result: list[BetaManagedAgentsFileResourceParams] = []
    for binding in bindings:
        if binding.resource is None or binding.target_path is None:
            raise ValueError("native transfer restore omitted its file or mount path")
        result.append(
            {"type": "file", "file_id": binding.resource.id, "mount_path": binding.target_path}
        )
    return tuple(result)
