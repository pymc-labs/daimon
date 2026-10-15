"""C14 on real host/drivers: retained shared Anthropic, new private OpenAI.

Only SDK HTTP transports are fake. Admission, session preparation, config
revisions, shared/native bindings and the OpenAI turn journal use owned Postgres.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from daimon.core import channel_backend, mux_backend
from daimon.core._models import AgentGitHubMode
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.shared_threads import shared_slot
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import AdmissionDecision, admit
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest, bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_tenant_config,
)
from daimon.testing.isolated_sdk import build_async_anthropic, build_async_openai
from daimon.testing.ma import json_body, make_fake_memory_store_handler
from daimon.testing.ma_models import ma_session
from daimon.testing.turn_fakes import RecordingLifecycle
from daimon.testing.turn_router import build_turn_router
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ResourceRef, Revision, ThreadRef
from mux.contracts.resources import SessionSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.state.lease import Slot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_openai_host import Wire
from .test_provider_dispatch import ACCOUNT, PROFILE, REVISION, SCOPE, TENANT, deps_for


async def test_c14_anthropic_thread_retains_a_new_thread_runs_b_and_missing_a_refuses(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shared db_preflight admits the target before either fixture connects;
    # this second check validates actual registered engine and every bind.
    require_owned_sessionmaker(db_session_factory)
    store = PostgresStateStore(db_session_factory)
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, id=TENANT)
        account = await make_account(db, tenant=tenant, id=ACCOUNT)
        await make_platform_principal(
            db, platform="slack", external_id="caller", tenant=tenant, account=account
        )
        await make_tenant_config(
            db, tenant=tenant, agent_name="test-agent", environment_name="test-env"
        )
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal(100))
        await set_access_policy(
            db,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai")),
        )
        db.add(
            AgentGitHubMode(
                tenant_id=TENANT,
                agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id="ag_parity_test"),
                mode="app",
            )
        )
        original = await channel_backend.set_channel_backend(
            db, REVISION.channel, BackendConfig(thread_mode="shared")
        )

    wire = Wire()
    wire.expected_spend_limit = None
    anthropic_requests: list[httpx.Request] = []
    router = build_turn_router(str(TENANT))
    memory = make_fake_memory_store_handler()
    created: list[str] = []

    def create(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json_body(request)
        native = ma_session(
            id=f"anthropic-c14-{len(created) + 1}",
            agent_id="ag_parity_test",
            environment_id="env_parity_test",
            metadata=body.get("metadata"),
            resources=body.get("resources", ()),
        )
        created.append(native.id)
        router.add_session(native)
        return httpx.Response(200, json=native.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions", create)
    router.add(
        "POST",
        r"/v1/vaults$",
        lambda request, match: httpx.Response(
            200,
            json={
                "id": "vlt_c14",
                "type": "vault",
                "display_name": json_body(request)["display_name"],
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-10-12T00:00:00Z",
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/vaults/[^/]+/credentials",
        lambda request, match: httpx.Response(200, json={"data": [], "next_page": None}),
    )

    def anthropic_http(request: httpx.Request) -> httpx.Response:
        anthropic_requests.append(request)
        return (
            memory(request)
            if request.url.path.startswith("/v1/memory_stores")
            else router.dispatch(request)
        )

    async def plan(request: ProviderPreparationRequest) -> SessionSpec:
        revision = request.admission.backend_revision
        assert revision is not None and revision.backend == "openai"
        return SessionSpec(
            agent=ResourceRef(
                id="native-agent",
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=request.scope.tenant_id,
                account_id=request.scope.account_id,
            ),
            agent_revision=Revision(local=0),
            config_revision=revision.local,
        )

    async with (
        build_async_anthropic(
            api_key="offline",
            base_url="https://anthropic.invalid",
            transport=httpx.MockTransport(anthropic_http),
        ) as anthropic,
        build_async_openai(
            api_key="offline",
            base_url="https://openai.invalid/v1",
            transport=httpx.MockTransport(wire.handle),
        ) as openai,
    ):
        runtime = OpenAIHostRuntime(
            lambda revision, scope: SDKTransport(openai),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            controls=SessionControls(
                model="gpt-6-luna", multi_agent_enabled=False, container_size="small"
            ),
        )
        deps = replace(
            deps_for(anthropic, db_session_factory),
            channel_backends=True,
            state_store=store,
            turn_runtimes={PROFILE: runtime},
        )
        decisions: list[AdmissionDecision] = []

        async def prepare(thread: str) -> PreparedTurn:
            current = await admit(
                deps,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                channel_id="channel",
                thread_id=thread,
                now=datetime.now(UTC),
                observer=decisions.append,
            )
            return await bind_session(
                deps,
                current,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id=thread,
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )

        first = await prepare("T1")
        first_slot = shared_slot(TENANT, "slack", "channel", "T1")
        binding = await store.get_binding(first_slot)
        assert binding is not None and binding.provider == "anthropic"
        assert binding.config_revision == original.local == 1
        assert binding.native_refs["session"] == first.ma_session_id
        assert not wire.requests  # A has not touched B's SDK.

        async with db_session_factory.begin() as db:
            current = await channel_backend.set_channel_backend(
                db,
                REVISION.channel,
                BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"),
            )
        retained = await prepare("T1")
        assert retained.admission.backend_revision == original
        assert retained.ma_session_id == first.ma_session_id and retained.reused
        assert retained.session_account_id == first.session_account_id != ACCOUNT
        assert await store.get_binding(first_slot) == binding
        assert not wire.requests

        new = await prepare("T2")
        assert new.admission.backend_revision == current and current.local == 2
        assert new.admission.shared_owner is None and new.session_account_id == ACCOUNT
        assert isinstance(new.backend, OpenAIDriver)
        assert new.session_ref is not None and new.session_ref.provider == "openai"
        assert new.ma_session_id != retained.ma_session_id
        second = await store.get_binding(
            Slot(thread=ThreadRef(channel=current.channel, thread_id="T2"), account_id=str(ACCOUNT))
        )
        assert second is not None and second.provider == "openai"
        assert second.config_revision == current.local
        assert second.native_refs["session"] == new.ma_session_id

        async def reseed() -> str:
            return "question"

        for selected, thread, expected in (
            (retained, "T1", "Hello from the agent!"),
            (new, "T2", "answer turn-1"),
        ):
            result = await run_prepared_turn(
                deps,
                selected,
                tenant_id=TENANT,
                platform="slack",
                thread_id=thread,
                external_user_id="caller",
                user_message="question",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                reseed_user_message=reseed,
                recovery_lifecycle=lambda cancel: RecordingLifecycle(),
                render_interval_s=0.01,
                operation_key="c14-provider-turn:" + thread,
            )
            assert result.state.error is None and expected in str(result.state.content)
            assert any(
                event.type == "session.turn_ended"
                for event in await store.read_events(selected.ma_session_id)
            )
        assert all(source.closed for source in wire.sources)
        assert created == [first.ma_session_id]
        assert all(req.url.host == "openai.invalid" for req in wire.requests)
        assert all(req.url.host == "anthropic.invalid" for req in anthropic_requests)

        # Remove the original host registration; B remains runnable. The
        # observer is the public adapter-facing typed refusal evidence.
        assert "anthropic.managed_agents" in mux_backend._TURN_BACKENDS
        monkeypatch.delitem(mux_backend._TURN_BACKENDS, "anthropic.managed_agents")
        monkeypatch.setattr(
            channel_backend,
            "RUNNABLE_PROFILES",
            channel_backend.RUNNABLE_PROFILES - {"anthropic.managed_agents"},
        )
        before = (len(anthropic_requests), len(wire.requests))
        with pytest.raises(AdmissionDenied) as error:
            await prepare("T1")
        assert error.value.reason == "backend_unsupported"
        assert decisions[-1].thread_id == "T1"
        assert decisions[-1].reason == "backend_unsupported" and not decisions[-1].admitted
        assert (len(anthropic_requests), len(wire.requests)) == before
        assert await store.get_binding(first_slot) == binding
        assert (
            await store.get_binding(Slot(thread=second.thread, account_id=str(ACCOUNT))) == second
        )
        assert channel_backend.check_backend(current).profile_id == PROFILE
    await drain_outcomes()
