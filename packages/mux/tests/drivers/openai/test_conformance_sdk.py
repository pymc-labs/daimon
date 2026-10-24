"""SDK-native C06-C09 evidence, refusals and controls; no live certificates."""

import json
import os
from datetime import UTC, datetime, timedelta

import mux.testing.drivers.openai.conformance_sdk as conformance_sdk
import pytest
from mux.contracts.ids import ModelRef, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt, DeletionReceipt, StopObservation
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec, WorkspaceSource
from mux.drivers.openai.artifacts import OpenAIArtifacts
from mux.drivers.openai.sessions import OpenAISessions
from mux.drivers.openai.turn import OpenAIEvents
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.testing.conformance.runner import PendingKind, run_fixture
from mux.testing.drivers.openai.conformance_sdk import OpenAISDKScript, offline_sdk_adapter
from mux.testing.drivers.openai.offline_sdk import build_offline_sdk
from mux.testing.isolated_sdk import build_async_openai

_INHERITED_SETTINGS = (
    "OPENAI_ADMIN_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_CUSTOM_HEADERS",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [*_INHERITED_SETTINGS, "all"])
@pytest.mark.parametrize("value", ["", "fixture-poison"])
async def test_inherited_settings_refuse_before_client_construction(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    for name in _INHERITED_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    names = _INHERITED_SETTINGS if setting == "all" else (setting,)
    for name in names:
        monkeypatch.setenv(name, value)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("environment refusal must precede client and script construction")

    monkeypatch.setattr(conformance_sdk, "AsyncOpenAI", forbidden)
    monkeypatch.setattr(conformance_sdk.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(conformance_sdk, "OpenAISDKScript", forbidden)
    with pytest.raises(ValueError) as failure:
        async with offline_sdk_adapter(sdk_factory=build_async_openai):
            pytest.fail("inherited settings must refuse admission")
    assert str(failure.value) == (
        "offline SDK adapter refuses inherited OpenAI credential/account settings"
    )
    # Presence alone is inspected; refusal never erases the caller's settings.
    for name in names:
        assert os.environ[name] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", _INHERITED_SETTINGS)
@pytest.mark.parametrize("value", ["", "fixture-poison"])
async def test_reconstruction_refuses_new_inherited_settings_before_sdk_copy(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    for name in _INHERITED_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        await script.arrange("C06")
        requests_before = len(script.requests)
        driver_before = script.driver
        monkeypatch.setenv(setting, value)

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("environment refusal must precede SDKTransport.with_options copying")

        monkeypatch.setattr(conformance_sdk, "SDKTransport", forbidden)
        with pytest.raises(ValueError) as failure:
            script.reconstruct_driver()
        assert str(failure.value) == (
            "offline SDK adapter refuses inherited OpenAI credential/account settings"
        )
        assert script.driver is driver_before
        assert len(script.requests) == requests_before
        assert os.environ[setting] == value


@pytest.mark.asyncio
async def test_standalone_credentials_and_routing_override_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _INHERITED_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-poison-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fixture-poison.invalid/v1")
    monkeypatch.setenv("HTTPS_PROXY", "https://fixture-poison.invalid")
    async with offline_sdk_adapter(sdk_factory=build_offline_sdk) as adapter:
        result = await run_fixture("C06", adapter)
        assert result.status == "pass", result.evidence
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        assert script.requests
        for request in script.requests:
            assert request.url.host == "openai.invalid"
            assert request.headers["authorization"] == "Bearer offline-fixture"
            assert not request.headers.get("OpenAI-Organization")
            assert not request.headers.get("OpenAI-Project")
            assert "fixture-poison" not in str(request.headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["C06", "C07", "C09"])
async def test_unchanged_oracles_read_real_driver_sdk_state(case: str) -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        result = await run_fixture(case, adapter)
        assert result.status == "pass", result.evidence
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        assert all(request.headers["OpenAI-Beta"] == "agents=v1" for request in script.requests)
        mutations = [r for r in script.requests if r.method != "GET"]
        if case == "C06":
            assert len(mutations) == 1
            assert json.loads(mutations[0].content) == {
                "events": [{"type": "agent.session.input.cancel"}]
            }
            assert mutations[0].headers["Idempotency-Key"] == "cancel"
        elif case == "C07":
            assert not mutations and script.usage_reads == 4
            assert not await script.store.pending_outbox()
        else:
            assert [r.method for r in mutations] == ["DELETE"]
            assert mutations[0].url.path.endswith("/sessions/session")
            assert all(resource.kind != "vault" for resource in script.deleted_resources)
    assert script.closed


@pytest.mark.asyncio
async def test_cancel_and_eof_hold_root_until_terminal_then_deduplicate() -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        scenario = await script.arrange("C06")
        receipt = await adapter.driver.events.cancel(
            scenario.scope, scenario.session.ref, turn_id="root", key="cancel"
        )
        assert receipt.status == "requested"
        assert [
            e async for e in adapter.driver.events.stream(scenario.scope, scenario.session.ref)
        ] == []
        stop = await adapter.driver.events.wait_stopped(
            scenario.scope, receipt, deadline=datetime.now(UTC) + timedelta(seconds=1)
        )
        assert not stop.stopped and stop.outcome is None
        current = await adapter.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
        assert current.state == "running" and current.active_root_turn == "root"
        script.reconstruct_driver()
        assert script.driver is not None
        current = await script.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
        assert current.state == "running" and current.active_root_turn == "root"
        script.fault("observed_stop")
        for _ in range(2):
            stopped = await script.driver.events.wait_stopped(
                scenario.scope, receipt, deadline=datetime.now(UTC) + timedelta(seconds=1)
            )
            assert stopped.stopped and stopped.outcome == "interrupted"
        history = await script.driver.events.list(
            scenario.scope, scenario.session.ref, page=PageRequest()
        )
        ends = [e for e in history.data if e.type == "session.turn_ended"]
        assert len(ends) == 1 and ends[0].payload["outcome"] == "interrupted"
        current = await script.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
        assert current.state == "idle" and current.active_root_turn is None
        assert sum(r.method == "POST" for r in script.requests) == 1


@pytest.mark.asyncio
async def test_usage_revisions_reconstruction_and_outbox_never_fabricate_zero() -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        scenario = await script.arrange("C07")
        observations = await adapter.driver.usage.reconcile(scenario.scope, scenario.session.ref)
        assert [(o.revision, o.output_tokens) for o in observations] == [
            (1, None),
            (2, 100),
            (3, 120),
            (4, 110),
        ]
        assert len({o.id for o in observations}) == 1
        assert observations[0].completeness == "unknown"
        rows = [
            await script.store.record_usage(scenario.session.binding.id, o) for o in observations
        ]
        assert all(row is not None for row in rows)
        assert [row.deltas["output_tokens"] for row in rows if row is not None] == [
            None,
            100,
            20,
            -10,
        ]
        script.restart_store(script.store)
        assert script.driver is not None
        repeated = await script.driver.usage.reconcile(scenario.scope, scenario.session.ref)
        assert all(o.revision == 4 and o.output_tokens == 110 for o in repeated)
        assert all(
            [
                await script.store.record_usage(scenario.session.binding.id, o) is None
                for o in repeated
            ]
        )
        pending = await script.store.pending_outbox()
        assert len(pending) == 4
        for row in pending:
            assert await script.store.mark_outbox_applied(row)
            assert not await script.store.mark_outbox_applied(row)
        assert not await script.store.pending_outbox()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tools", "mounts"])
async def test_changed_tool_or_file_mount_refuses_update_without_revision_or_write(
    change: str,
) -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        scenario = await script.arrange("C08")
        if change == "tools":
            agent = await adapter.driver.agents.create(
                scenario.scope,
                AgentSpec(
                    name="changed",
                    model=ModelRef(provider="openai", id="gpt-6-luna"),
                    tools=(ToolSpec(name="new_tool", kind="custom"),),
                ),
                key="changed-agent",
            )
            desired = scenario.desired.model_copy(
                update={"agent": agent.ref, "agent_revision": agent.revision, "config_revision": 2}
            )
            body = json.loads(script.requests[-1].content)
            assert body["model"] == "gpt-6-luna"
            assert body["tools"][0]["name"] == "new_tool"
        else:
            source = WorkspaceSource(
                kind="file",
                target_path="/workspace/new.bin",
                artifact=scenario.session.ref.model_copy(
                    update={"kind": "artifact", "id": "file:uploaded"}
                ),
            )
            environment = await adapter.driver.environments.create(
                scenario.scope,
                EnvironmentSpec(name="changed", sources=(source,)),
                key="changed-environment",
            )
            desired = scenario.desired.model_copy(
                update={"environment": environment.ref, "config_revision": 2}
            )
            assert json.loads(script.requests[-1].content)["files"] == [
                {"type": "file_id", "file_id": "uploaded", "path": "/workspace/new.bin"}
            ]
        before = script.mutation_count
        plan = await adapter.driver.sessions.plan_update(
            scenario.scope, scenario.session.ref, desired
        )
        assert plan.action == "refuse" and plan.unmet == ("session_conditional_update",)
        with pytest.raises(UnsupportedCapability) as error:
            await adapter.driver.sessions.apply_update(
                scenario.scope, plan, expected=scenario.session.effective_revision, key="update"
            )
        assert error.value.missing == ("session_conditional_update",)
        assert script.mutation_count == before
        assert (
            await adapter.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
            == scenario.session
        )


@pytest.mark.asyncio
async def test_repository_mount_remains_unsupported_before_requests() -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        scenario = await script.arrange("C08")
        before = len(script.requests)
        with pytest.raises(UnsupportedCapability) as error:
            await adapter.driver.environments.create(
                scenario.scope,
                EnvironmentSpec(
                    name="git",
                    sources=(
                        WorkspaceSource(
                            kind="repository",
                            target_path="/workspace/repo",
                            repository_url="https://offline.invalid/repo.git",
                        ),
                    ),
                ),
                key="git",
            )
        assert error.value.missing == ("workspace_source",)
        assert len(script.requests) == before


@pytest.mark.asyncio
async def test_binary_artifact_native_routing_scope_and_reconstruction() -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        script = adapter.transport
        assert isinstance(script, OpenAISDKScript)
        scenario = await script.arrange("C09")
        page = await adapter.driver.artifacts.list(
            scenario.scope, scenario.session.ref, page=PageRequest(limit=1)
        )
        artifact = page.data[0]
        script.reconstruct_driver()
        assert script.driver is not None
        original = b"".join(
            [
                chunk
                async for chunk in script.driver.artifacts.download(scenario.scope, artifact.ref)
            ]
        )
        assert original == bytes(range(256))
        before = len(script.requests)
        with pytest.raises(ScopeViolation):
            await script.driver.artifacts.retrieve(scenario.foreign_scope, artifact.ref)
        assert len(script.requests) == before
        script.fault("download_interrupted")
        with pytest.raises(ProviderError) as error:
            _ = b"".join(
                [
                    chunk
                    async for chunk in script.driver.artifacts.download(
                        scenario.scope, artifact.ref
                    )
                ]
            )
        assert error.value.category == "transient_network"
        assert not script.deleted_resources


@pytest.mark.asyncio
async def test_unobserved_stop_mutant_fails_shared_occupancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stopped(
        self: OpenAIEvents, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        return StopObservation(
            receipt_operation_id=receipt.operation_id,
            stopped=True,
            outcome="interrupted",
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(OpenAIEvents, "wait_stopped", stopped)
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        result = await run_fixture("C06", adapter)
        assert result.status == "fail"
        assert "EOF is not observed termination" in result.evidence[0]


@pytest.mark.asyncio
async def test_missing_revision_allocation_fails_the_unchanged_usage_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def frozen(*args: object) -> int:
        return 1

    monkeypatch.setattr(MemoryUsageRevisions, "revise", frozen)
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        result = await run_fixture("C07", adapter)
        assert result.status == "fail"
        assert "full correction sequence" in result.evidence[0]


@pytest.mark.asyncio
async def test_truncated_binary_mutant_fails_shared_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def truncated(*args: object):
        yield b"short"

    monkeypatch.setattr(OpenAIArtifacts, "download", truncated)
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        result = await run_fixture("C09", adapter)
        assert result.status == "fail"
        assert "binary checksum" in result.evidence[0]


@pytest.mark.asyncio
async def test_deleted_shared_vault_mutant_fails_independent_http_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = OpenAISessions.delete

    class Destructive(OpenAISessions):
        async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
            receipt = await original(self, scope, ref, key=key)
            await self._c.call("DELETE", "/vaults/shared-vault", key="bad-delete")
            return receipt

    monkeypatch.setattr(OpenAISessions, "delete", Destructive.delete)
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        result = await run_fixture("C09", adapter)
        assert result.status == "fail"
        assert "not actually delete shared resources" in result.evidence[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [f"C{i:02}" for i in range(1, 19) if i not in (6, 7, 9)])
async def test_component_pending_precedes_all_native_requests(case: str) -> None:
    async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
        result = await run_fixture(case, adapter)
        assert result.status == "pending" and result.pending_reason is not None
        assert result.pending_reason.kind == (
            PendingKind.CAPABILITY_UNAVAILABLE
            if case in ("C08", "C12")
            else PendingKind.ADAPTER_DEPENDENCY
        )
        assert isinstance(adapter.transport, OpenAISDKScript) and not adapter.transport.requests
