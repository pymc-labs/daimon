"""Unchanged C06/C07 checks now cross the actual SDK, with typed C08/C09 limits."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ResolvedBackend
from mux.contracts.events import TurnEndedPayload
from mux.contracts.ids import PageRequest
from mux.contracts.resources import EnvironmentSpec, ToolSpec, WorkspaceSource
from mux.drivers.gemini.transport import NO_TOOLS_TOOL
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.testing.conformance.runner import PendingKind, run_fixture
from mux.testing.drivers.gemini.conformance_sdk import GeminiSDKScript, offline_sdk_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["C06", "C07"])
async def test_cancellation_and_usage_shared_oracles_cross_actual_sdk(case: str) -> None:
    async with offline_sdk_adapter() as adapter:
        result = await run_fixture(case, adapter)
        assert result.status == "pass", result.evidence
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript)
        posts = [r for r in script.http_requests if r.method == "POST"]
        assert len(posts) == (2 if case == "C06" else 1)
        body = json.loads(posts[0].content)
        assert body["tools"] == [NO_TOOLS_TOOL]
        assert body["agent_config"]["model"] == "gemini-3.8-flash"
        assert body["input"] == [{"type": "text", "text": "seed"}]
        assert posts[0].headers["Api-Revision"] == "2026-05-20"
        if case == "C06":
            assert posts[1].url.path.endswith("root/cancel") or posts[1].url.path.endswith(
                "root:cancel"
            )
            assert script.cancelled == ["root"]
        else:
            assert script.sdk is not None
            assert adapter.store is not None and not await adapter.store.pending_outbox()
            # HTTP GET snapshots, not edits to local projection, supplied every revision.
            assert len([r for r in script.http_requests if r.method == "GET"]) == 5


@pytest.mark.asyncio
async def test_cancel_receipt_and_empty_sdk_stream_cannot_release_root() -> None:
    async with offline_sdk_adapter() as adapter:
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript)
        scenario = await script.arrange("C06")
        receipt = await adapter.driver.events.cancel(
            scenario.scope, scenario.session.ref, turn_id="root", key="cancel"
        )
        assert receipt.status == "requested"
        assert [
            e async for e in adapter.driver.events.stream(scenario.scope, scenario.session.ref)
        ] == []
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        stop = await adapter.driver.events.wait_stopped(scenario.scope, receipt, deadline=deadline)
        assert not stop.stopped and stop.outcome is None
        current = await adapter.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
        assert current.state == "running" and current.active_root_turn == "root"
        journal = await adapter.driver.events.list(
            scenario.scope, scenario.session.ref, page=PageRequest()
        )
        assert not any(e.type == "session.turn_ended" for e in journal.data)
        script.fault("observed_stop")
        stop = await adapter.driver.events.wait_stopped(scenario.scope, receipt, deadline=deadline)
        assert stop.stopped and stop.outcome == "interrupted"
        await adapter.driver.events.wait_stopped(scenario.scope, receipt, deadline=deadline)
        journal = await adapter.driver.events.list(
            scenario.scope, scenario.session.ref, page=PageRequest()
        )
        ends = [e.typed_payload() for e in journal.data if e.type == "session.turn_ended"]
        assert len(ends) == 1 and isinstance(ends[0], TurnEndedPayload)
        assert ends[0].outcome == "interrupted"
        assert script.cancelled == ["root"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tools", "mounts"])
async def test_next_turn_tool_or_mount_change_refuses_with_zero_sdk_mutations(change: str) -> None:
    async with offline_sdk_adapter() as adapter:
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript)
        scenario = await script.arrange("C10")
        if change == "tools":
            agent = await adapter.driver.agents.retrieve(scenario.scope, scenario.desired.agent)
            changed = await adapter.driver.agents.create(
                scenario.scope,
                agent.spec.model_copy(
                    update={"tools": (ToolSpec(name="new_tool", kind="custom"),)},
                ),
                key="changed-agent",
            )
            desired = scenario.desired.model_copy(
                update={
                    "agent": changed.ref,
                    "agent_revision": changed.revision,
                    "config_revision": 2,
                }
            )
        else:
            changed_env = await adapter.driver.environments.create(
                scenario.scope,
                EnvironmentSpec(
                    name="changed",
                    sources=(
                        WorkspaceSource(
                            kind="repository",
                            target_path="new-mount",
                            repository_url="https://offline.invalid/new.git",
                        ),
                    ),
                ),
                key="changed-environment",
            )
            desired = scenario.desired.model_copy(
                update={
                    "environment": changed_env.ref,
                    "config_revision": 2,
                }
            )
        plan = await adapter.driver.sessions.plan_update(
            scenario.scope, scenario.session.ref, desired
        )
        assert plan.action == "refuse" and plan.unmet == ("session_update",)
        with pytest.raises(UnsupportedCapability):
            await adapter.driver.sessions.apply_update(
                scenario.scope, plan, expected=scenario.session.effective_revision, key="update"
            )
        assert not script.http_requests
        assert (
            await adapter.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
            == scenario.session
        )


@pytest.mark.asyncio
async def test_vault_admission_and_native_session_delete_are_explicitly_unavailable() -> None:
    async with offline_sdk_adapter() as adapter:
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript)
        scenario = await script.arrange("C10")
        config = ConfigRevision.create(
            scenario.session.binding.thread.channel,
            1,
            ResolvedBackend(
                backend="gemini",
                profile="gemini.inline_reuse",
                model="gemini-3.8-flash",
                requires={"vaults": CapabilityRequirement(level="required")},
            ),
        )
        with pytest.raises(UnsupportedCapability):
            adapter.driver.admit(config)
        with pytest.raises(UnsupportedCapability):
            await adapter.driver.sessions.delete(scenario.scope, scenario.session.ref, key="delete")
        assert not script.http_requests
        assert not script.deleted_resources


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kind"),
    [("C08", PendingKind.ADAPTER_DEPENDENCY), ("C09", PendingKind.CAPABILITY_UNAVAILABLE)],
)
async def test_sdk_component_does_not_promote_unavailable_full_cases(
    case: str, kind: PendingKind
) -> None:
    async with offline_sdk_adapter() as adapter:
        result = await run_fixture(case, adapter)
        assert result.status == "pending" and result.pending_reason is not None
        assert result.pending_reason.kind == kind
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript) and not script.http_requests


@pytest.mark.asyncio
async def test_binary_host_artifact_survives_restart_without_claiming_vault_or_native_upload() -> (
    None
):
    async with offline_sdk_adapter() as adapter:
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript) and adapter.store is not None
        scope = script.scope
        original = bytes(range(256)) + b"\x00\xff" * 100

        async def data() -> AsyncIterator[bytes]:
            yield original[:17]
            yield original[17:]

        uploaded = await adapter.driver.artifacts.upload(
            scope,
            data(),
            filename="binary.bin",
            media_type="application/octet-stream",
            key="binary",
        )
        script.restart_store(adapter.store)
        recovered = b"".join(
            [part async for part in script.ma.artifacts.download(scope, uploaded.ref)]
        )
        assert recovered == original
        with pytest.raises(ScopeViolation):
            await script.ma.artifacts.retrieve(
                scope.model_copy(update={"tenant_id": "foreign"}), uploaded.ref
            )
        assert not script.http_requests and not script.deleted_resources
        assert script.ma.capabilities().support_for("vaults") == "unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [f"C{i:02}" for i in range(1, 19) if i not in (6, 7)])
async def test_unimplemented_sdk_envelopes_are_typed_pending_before_requests(case: str) -> None:
    async with offline_sdk_adapter() as adapter:
        result = await run_fixture(case, adapter)
        assert result.status == "pending" and result.pending_reason is not None
        script = adapter.transport
        assert isinstance(script, GeminiSDKScript) and not script.http_requests
