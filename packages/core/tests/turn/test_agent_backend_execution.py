"""Component readback cannot authorize unproven source execution."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from daimon.core.mux_backend import TurnRuntime
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
from daimon.core.turn.runtime_manifest import (
    fixed_plan_origin_allowed,
    read_preparation_counterpart,
)
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_provider_dispatch import ACCOUNT, REVISION, SCOPE, TENANT, admission, deps_for

NOW = datetime(2026, 10, 12, tzinfo=UTC)


@pytest.fixture(params=["openai", "gemini"])
def counterpart(request: pytest.FixtureRequest) -> AgentRevision:
    provider = request.param
    return AgentRevision(
        catalog_id="retained-counterpart",
        local_revision=3,
        principal_id=SCOPE.principal_id,
        agent=Agent(
            ref=ResourceRef(
                id="verified-native-agent",
                kind="agent",
                provider=provider,
                account_scope_id="project",
                tenant_id=SCOPE.tenant_id,
                account_id=SCOPE.account_id,
            ),
            revision=Revision(local=3),
            spec=AgentSpec(
                name="display-label",
                model=ModelRef(
                    provider=provider,
                    id="gpt-6-luna" if provider == "openai" else "gemini-3.8-flash",
                ),
            ),
            created_at=NOW,
        ),
    )


def request_for(row: AgentRevision, runtime: TurnRuntime) -> ProviderPreparationRequest:
    config = ConfigRevision.create(
        REVISION.channel,
        2,
        resolve_default(
            BackendConfig(
                backend=row.agent.ref.provider,
                profile=(
                    "openai.persistent_workspace"
                    if row.agent.ref.provider == "openai"
                    else "gemini.inline_reuse"
                ),
                model=row.agent.spec.model.id,
            )
        ),
    )
    deps = replace(
        deps_for(None, async_sessionmaker[AsyncSession]()),
        channel_backends=True,
        agent_backends=True,
        turn_runtimes={config.profile: runtime},
    )
    return ProviderPreparationRequest(
        deps=deps,
        admission=replace(admission(), backend_revision=config, backend_revision_origin="agent"),
        scope=SCOPE,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=True,
        capabilities=DEFAULT_MA_CAPABILITIES,
        transfer=None,
        deadline=NOW + timedelta(seconds=30),
        now=lambda: NOW,
    )


def no_transport(*args: object) -> object:
    raise AssertionError("proof gate opened a native transport")


@pytest.mark.parametrize("origin", [None, "", "unknown", "agent", "channel"])
def test_only_channel_origin_bypasses_source_preservation(origin: str | None) -> None:
    assert fixed_plan_origin_allowed(origin) is (origin == "channel")


async def test_component_readback_is_awaited_once_and_retained(counterpart: AgentRevision) -> None:
    calls: list[ProviderPreparationRequest] = []

    async def read(request: ProviderPreparationRequest) -> AgentRevision:
        assert request.counterpart is None
        calls.append(request)
        return counterpart

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    assert runtime.read_agent_counterpart is not None
    assert await runtime.read_agent_counterpart(request) is counterpart
    assert calls == [request]
    assert request.counterpart is None, "proof completion cannot mutate an earlier request"


@pytest.mark.parametrize("disabled", ["agent", "channel", "legacy", "no-runtime", "no-reader"])
async def test_unconfigured_or_unproven_selection_refuses_before_hook_or_transport(
    counterpart: AgentRevision, disabled: str
) -> None:
    async def read(request: ProviderPreparationRequest) -> AgentRevision:
        raise AssertionError("disabled selection invoked a counterpart hook")

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    if disabled == "agent":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif disabled == "channel":
        request = replace(request, deps=replace(request.deps, channel_backends=False))
    elif disabled == "legacy":
        request = replace(request, deps=replace(request.deps, turn_path="legacy"))
    elif disabled == "no-runtime":
        request = replace(request, deps=replace(request.deps, turn_runtimes={}))
    else:
        request = request_for(counterpart, TurnRuntime(no_transport, object(), object()))
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert refused.value.reason == "backend_unsupported"


@pytest.mark.parametrize("defect", ["missing", "tenant", "account", "provider", "model", "error"])
async def test_failed_or_foreign_proof_is_a_typed_refusal_without_execution(
    counterpart: AgentRevision, defect: str
) -> None:
    async def read(request: ProviderPreparationRequest) -> AgentRevision:
        if defect == "error":
            raise RuntimeError("private proof error")
        if defect == "missing":
            return cast(AgentRevision, None)
        if defect in ("tenant", "account", "provider"):
            field = {"tenant": "tenant_id", "account": "account_id", "provider": "provider"}[defect]
            agent = counterpart.agent.model_copy(
                update={"ref": counterpart.agent.ref.model_copy(update={field: "foreign"})}
            )
        else:
            agent = counterpart.agent.model_copy(
                update={
                    "spec": counterpart.agent.spec.model_copy(
                        update={
                            "model": ModelRef(provider=counterpart.agent.ref.provider, id="other")
                        }
                    )
                }
            )
        return replace(counterpart, agent=agent)

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert refused.value.reason == "backend_unsupported"
    assert "private" not in str(refused.value)


async def test_public_bind_refuses_component_readback_as_execution_authority(
    monkeypatch, counterpart
):
    from daimon.core.turn import prepare as module
    from daimon.core.turn.prepare import PreparedTurn

    from .test_provider_dispatch import record_usage

    stages = []

    async def read(request):
        assert request.counterpart is None
        stages.append("proof")
        return counterpart

    async def prepare(request):
        assert request.counterpart is counterpart
        stages.append("prepare")
        ref = ResourceRef(
            id="verified-session",
            kind="session",
            provider=counterpart.agent.ref.provider,
            account_scope_id="project",
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
        )
        return PreparedTurn(
            request.admission, ref.id, None, None, False, ACCOUNT, record_usage, session_ref=ref
        )

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    monkeypatch.setitem(
        module._PROVIDER_PREPARATIONS, request.admission.backend_revision.profile, prepare
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await module.bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=TENANT,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=ACCOUNT,
            reuse_existing=request.reuse_existing,
            deadline=request.deadline,
            now=request.now,
        )
    assert stages == [], "source refusal must precede readback and native preparation"


@pytest.mark.parametrize(
    "defect", ["flag-off", "legacy", "missing-reader", "error", "foreign", "changed-proof"]
)
async def test_public_bind_refuses_before_native_preparer_or_transport(
    monkeypatch, counterpart, defect
):
    from daimon.core.turn import prepare as module

    calls = []

    async def read(request):
        calls.append("proof")
        if defect == "error":
            raise RuntimeError("private provider diagnostic")
        if defect == "foreign":
            return replace(
                counterpart,
                agent=counterpart.agent.model_copy(
                    update={
                        "ref": counterpart.agent.ref.model_copy(update={"account_id": "foreign"})
                    }
                ),
            )
        return counterpart

    async def prepare(request):
        raise AssertionError("failed proof invoked native preparation")

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    if defect == "flag-off":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif defect == "legacy":
        request = replace(request, deps=replace(request.deps, turn_path="legacy"))
    elif defect == "missing-reader":
        request = request_for(counterpart, TurnRuntime(no_transport, object(), object()))
    elif defect == "changed-proof":
        request = replace(
            request,
            admission=replace(
                request.admission,
                backend_counterpart=replace(
                    counterpart, local_revision=counterpart.local_revision + 1
                ),
            ),
        )
    monkeypatch.setitem(
        module._PROVIDER_PREPARATIONS, request.admission.backend_revision.profile, prepare
    )
    with pytest.raises(AdmissionDenied) as refused:
        await module.bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=TENANT,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=ACCOUNT,
            reuse_existing=request.reuse_existing,
            deadline=request.deadline,
            now=request.now,
        )
    assert refused.value.reason == "backend_unsupported"
    assert "private" not in str(refused.value)
    assert calls == [], "no component callback can bypass source-preservation refusal"


async def test_source_projection_timeout_refuses_and_cancels_capture_without_preparation(
    monkeypatch, counterpart
):
    import asyncio

    from daimon.core.turn import prepare as module

    cancelled = asyncio.Event()

    async def read(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def prepare(request):
        raise AssertionError("expired proof reached native preparation")

    from daimon.core import sessions
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_compat import source_agent_record
    from daimon.core.stores.domain import BackendAgent

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    source = BackendAgent(
        tenant_id=TENANT,
        agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=request.admission.agent.id),
        name="source",
        configuration_digest="source-snapshot",
        source=source_agent_record(request.admission.agent, SCOPE),
    )
    request = replace(request, source=source, deadline=NOW + timedelta(milliseconds=20))
    monkeypatch.setattr(
        sessions, "project_session_resources", lambda *args, **kwargs: read(request)
    )
    monkeypatch.setitem(
        module._PROVIDER_PREPARATIONS, request.admission.backend_revision.profile, prepare
    )
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert refused.value.reason == "backend_unsupported"
    assert cancelled.is_set()


async def test_cancellation_drains_pending_source_capture_without_preparation(
    monkeypatch, counterpart
):
    import asyncio

    from daimon.core.turn import prepare as module

    entered, closed = asyncio.Event(), asyncio.Event()

    async def read(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def prepare(request):
        raise AssertionError("cancelled proof reached native preparation")

    runtime = TurnRuntime(no_transport, object(), object(), read_agent_counterpart=read)
    request = request_for(counterpart, runtime)
    monkeypatch.setitem(
        module._PROVIDER_PREPARATIONS, request.admission.backend_revision.profile, prepare
    )
    from daimon.core import sessions
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_compat import source_agent_record
    from daimon.core.stores.domain import BackendAgent

    source = BackendAgent(
        tenant_id=TENANT,
        agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=request.admission.agent.id),
        name="source",
        configuration_digest="source-snapshot",
        source=source_agent_record(request.admission.agent, SCOPE),
    )
    request = replace(request, source=source)
    monkeypatch.setattr(
        sessions, "project_session_resources", lambda *args, **kwargs: read(request)
    )
    task = asyncio.create_task(read_preparation_counterpart(request))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
