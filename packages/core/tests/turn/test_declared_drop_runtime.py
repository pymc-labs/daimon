"""Canonical preparation reaches the existing lazy runtime owner once."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from daimon.core.mux_backend import TurnRuntime
from daimon.core.scope import ResolvedConfig
from daimon.core.turn.admission import Admission
from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
from daimon.core.turn.runtimes import ScopedTurnRuntimes
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, ModelRef, Scope
from mux.contracts.resources import AgentSpec
from mux.errors import ScopeViolation
from mux.profiles import backend_selection


@pytest.fixture
def prepared_request():
    tenant, account = uuid4(), uuid4()
    revision = ConfigRevision.create(
        ChannelRef(tenant_id=str(tenant), platform="slack", channel_id="channel"),
        1,
        backend_selection("openai"),
    )
    admitted = Admission(
        account_id=account,
        agent=ma_agent(),
        environment=ma_environment(),
        config=ResolvedConfig(agent_name="source", environment_name="source"),
        backend_revision=revision,
        backend_revision_origin="agent",
    )
    now = datetime(2026, 10, 14, tzinfo=UTC)
    return ProviderPreparationRequest(
        deps=deps_for(),
        admission=admitted,
        scope=Scope(
            tenant_id=str(tenant),
            account_id=str(account),
            principal_id="caller",
            authorization_id="admitted",
        ),
        tenant_id=tenant,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=account,
        reuse_existing=True,
        capabilities=DEFAULT_MA_CAPABILITIES,
        transfer=None,
        deadline=now + timedelta(minutes=1),
        now=lambda: now,
    )


def canonical(request):
    return replace(
        request,
        target_spec=AgentSpec(
            name="source",
            model=ModelRef(provider="openai", id="gpt-6-luna"),
            system="Source instruction\n\nUnavailable capabilities: memory, sandbox.",
            declared_drops=("memory", "sandbox"),
            tools=(),
            skills=(),
            mcp_servers=(),
        ),
    )


def runtime():
    def no_transport(*args):
        raise AssertionError("lazy ownership test performed native I/O")

    return TurnRuntime(transport_factory=no_transport, journal=object(), usage_store=object())


def test_canonical_preparation_reaches_one_factory_and_runtime_is_retained(prepared_request):
    calls, result = [], runtime()

    def construct(request):
        calls.append(request)
        return result

    lookup = ScopedTurnRuntimes(prepared_request, construct)
    selected = canonical(prepared_request)
    lookup.bind_preparation(selected)
    assert calls == [] and lookup.resolved is None
    assert lookup[selected.admission.backend_revision.profile] is result
    assert calls == [selected] and calls[0].target_spec is selected.target_spec
    lookup.bind_preparation(prepared_request)
    assert lookup[selected.admission.backend_revision.profile] is result
    assert lookup.resolved is result and calls == [selected]


@pytest.mark.parametrize("change", ["scope", "revision", "platform", "thread", "account"])
def test_preparation_cannot_rebind_foreign_runtime_authority(prepared_request, change):
    calls = []

    def construct(request):
        calls.append(request)
        return runtime()

    lookup = ScopedTurnRuntimes(prepared_request, construct)
    changes = {
        "scope": {
            "scope": prepared_request.scope.model_copy(update={"authorization_id": "foreign"})
        },
        "revision": {
            "admission": replace(
                prepared_request.admission,
                backend_revision=prepared_request.admission.backend_revision.model_copy(
                    update={"local": 2}
                ),
            )
        },
        "platform": {"platform": "discord"},
        "thread": {"thread_id": "foreign"},
        "account": {"session_account_id": uuid4()},
    }
    with pytest.raises(ScopeViolation):
        lookup.bind_preparation(replace(canonical(prepared_request), **changes[change]))
    assert calls == [] and lookup.resolved is None


def test_explicit_runtime_stays_injected_while_canonical_facts_are_verified(prepared_request):
    result = runtime()
    profile = prepared_request.admission.backend_revision.profile
    original = replace(
        prepared_request, deps=replace(prepared_request.deps, turn_runtimes={profile: result})
    )

    def no_factory(request):
        raise AssertionError("explicit runtime was reconstructed")

    lookup = ScopedTurnRuntimes(original, no_factory)
    lookup.bind_preparation(canonical(original))
    assert lookup[profile] is result and lookup.resolved is result
