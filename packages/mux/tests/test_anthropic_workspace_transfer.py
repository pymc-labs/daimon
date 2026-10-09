"""Native inline transfers declare losses and prepare mounts without SDK I/O."""

import pickle

import pytest
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import WorkspaceTransfer, WorkspaceTransferConfig
from mux.errors import ScopeViolation
from pydantic import JsonValue, ValidationError

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)
PAYLOADS: list[dict[str, JsonValue]] = [
    {
        "type": "full",
        "file_id": "file_bundle",
        "mount_path": "/daimon-handoff.tar.gz",
        "bytes_transferred": 12,
        "transcript": "quoted participant text",
        "unpreserved": ["uncommitted repository changes were left in the old checkout"],
    },
    {
        "type": "transcript",
        "transcript": "quoted participant text",
        "gap_reason": "checkpoint_timeout",
    },
    {"type": "history", "gap_reason": "events_unavailable"},
]


@pytest.mark.parametrize("payload", PAYLOADS)
async def test_transfer_export_restore_roundtrip_declares_losses_without_requests(
    payload: dict[str, JsonValue],
) -> None:
    transport = ScriptedTransport()
    inline = ExtensionConfig(
        namespace="anthropic.workspace_transfer", version=1, value={"outcome": payload}
    )
    assert ExtensionConfig.model_validate_json(inline.model_dump_json()) == inline
    config = WorkspaceTransferConfig.model_validate(dict(inline.value))
    assert WorkspaceTransferConfig.model_validate_json(config.model_dump_json()) == config
    assert pickle.loads(pickle.dumps(config)) == config
    async with transport.client() as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="org",
            authorization=ResourceAuthorization(
                SCOPE,
                frozenset({("session", "sess_old"), ("file", "file_bundle")}),
            ),
        )
        port = backend.extension(
            WorkspaceTransfer, namespace="anthropic.workspace_transfer", version=1
        )
        ref = ResourceRef(
            id="sess_old",
            kind="session",
            provider="anthropic",
            account_scope_id="org",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
        export = port.export(SCOPE, ref, inline=inline, key="export")
        assert export.consistency == "best_effort"
        assert export.journal_cursor == ""  # no invented journal/read
        assert "runtime" in export.losses
        with pytest.raises(ValueError, match="not been accepted"):
            port.restore(SCOPE, export, inline=inline, accept_losses=frozenset(), key="restore")
        mounts = port.restore(
            SCOPE, export, inline=inline, accept_losses=frozenset(export.losses), key="restore"
        )
        if payload["type"] == "full":
            assert export.included == {"workspace", "transcript"}
            assert mounts[0].resource == export.artifacts[0]
            assert mounts[0].target_path == payload["mount_path"]
            unpreserved = payload["unpreserved"]
            assert isinstance(unpreserved, list)
            assert unpreserved[0] in export.losses
        else:
            assert mounts == ()
            assert "workspace" in export.excluded
            assert f"workspace:{payload['gap_reason']}" in export.losses
            assert ("transcript" in export.included) == (payload["type"] == "transcript")
        damaged = export.model_copy(update={"manifest_digest": "bad"})
        with pytest.raises(ValueError, match="differs"):
            port.restore(
                SCOPE, damaged, inline=inline, accept_losses=frozenset(export.losses), key="restore"
            )
    assert transport.requests == []


async def test_transfer_rejects_forged_scope_ungranted_archive_and_mutated_inline_data() -> None:
    transport = ScriptedTransport()
    inline = ExtensionConfig(
        namespace="anthropic.workspace_transfer", version=1, value={"outcome": PAYLOADS[0]}
    )
    async with transport.client() as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="org",
            authorization=ResourceAuthorization(
                SCOPE,
                frozenset({("session", "sess_old")}),
            ),
        )
        port = backend.extension(
            WorkspaceTransfer, namespace="anthropic.workspace_transfer", version=1
        )
        ref = ResourceRef(
            id="sess_old",
            kind="session",
            provider="anthropic",
            account_scope_id="org",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
        with pytest.raises(ScopeViolation):
            port.export(
                SCOPE, ref.model_copy(update={"tenant_id": "other"}), inline=inline, key="export"
            )
        with pytest.raises(ScopeViolation):
            port.export(SCOPE, ref, inline=inline, key="export")
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="org",
            authorization=ResourceAuthorization(
                SCOPE,
                frozenset({("session", "sess_old"), ("file", "file_bundle")}),
            ),
        )
        port = backend.extension(
            WorkspaceTransfer, namespace="anthropic.workspace_transfer", version=1
        )
        export = port.export(SCOPE, ref, inline=inline, key="export")
        modified = ExtensionConfig(
            namespace=inline.namespace,
            version=1,
            value={
                "outcome": {**PAYLOADS[0], "transcript": "changed"},
            },
        )
        with pytest.raises(ValueError, match="differs"):
            port.restore(
                SCOPE,
                export,
                inline=modified,
                accept_losses=frozenset(export.losses),
                key="restore",
            )
    assert transport.requests == []


@pytest.mark.parametrize(
    "patch", [{"extra_kwarg": "no"}, {"type": "fresh"}, {"bytes_transferred": -1}]
)
def test_native_transfer_schema_is_closed_and_never_accepts_a_silent_fresh_start(
    patch: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceTransferConfig.model_validate({"outcome": {**PAYLOADS[0], **patch}})
