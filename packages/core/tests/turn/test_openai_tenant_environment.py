"""A tenant's hosted OpenAI environment is created once, on its first real GPT root turn."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import openai_deployment, openai_environment
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.openai_environment import (
    STALE_AFTER,
    environment_name,
    operation_key,
    tenant_environment,
)
from daimon.core.turn.runtime_defaults import DEFAULT_PROJECT
from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.resources import Environment, EnvironmentFilter, EnvironmentSpec
from mux.state.memory import MemoryStateStore

from .test_declared_drop_preparation import consent_request as consent_request
from .test_declared_drop_preparation import owned as owned

TENANT = "11111111-1111-4111-8111-111111111111"
PROJECT = "proj_test"
START = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class FakeEnvironments:
    """`OpenAIEnvironments.create`/`list`, counting creates; a create may fail or block."""

    def __init__(self, *, fail: int = 0, gate: asyncio.Event | None = None) -> None:
        self.created: list[Environment] = []
        self.keys: list[str] = []
        self.creates = 0
        self.fail = fail
        self.gate = gate

    def _environment(self, tenant_id: str, name: str) -> Environment:
        return Environment(
            ref=ResourceRef(
                id=f"envtmpl_{len(self.created) + 1}",
                kind="environment",
                provider="openai",
                account_scope_id=PROJECT,
                tenant_id=tenant_id,
                account_id="daimon-openai-environment",
            ),
            revision=Revision(local=1),
            spec=EnvironmentSpec(name=name),
            created_at=START,
        )

    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        assert scope.is_platform and key.startswith("openai_tenant_environment:")
        self.keys.append(key)
        self.creates += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            self.fail -= 1
            raise TimeoutError("no answer")
        made = self._environment(scope.tenant_id, spec.name)
        self.created.append(made)
        return made

    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]:
        assert scope.is_platform
        return Page(data=tuple(self.created))


DEPLOYMENT = "example-staging"
PRODUCTION = "example-prod"


async def _get(store, environments, clock, tenant_id=TENANT, deployment=DEPLOYMENT):
    return await tenant_environment(
        store,
        environments,
        tenant_id=tenant_id,
        project=PROJECT,
        deployment=deployment,
        now=clock,
        sleep=clock.sleep,
    )


async def test_the_first_turn_creates_it_and_every_later_turn_reuses_it():
    store, environments, clock = MemoryStateStore(), FakeEnvironments(), Clock()
    first = await _get(store, environments, clock)
    assert await _get(store, environments, clock) == first
    assert environments.creates == 1
    assert environments.created[0].spec.name == environment_name(DEPLOYMENT, TENANT)


async def test_tenants_never_share_an_environment():
    store, environments, clock = MemoryStateStore(), FakeEnvironments(), Clock()
    other = "22222222-2222-4222-8222-222222222222"
    assert await _get(store, environments, clock) != await _get(
        store, environments, clock, tenant_id=other
    )
    assert environments.creates == 2


async def test_concurrent_first_turns_create_exactly_one():
    gate = asyncio.Event()
    store, environments, clock = MemoryStateStore(), FakeEnvironments(gate=gate), Clock()
    turns = [asyncio.create_task(_get(store, environments, clock)) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    ids = await asyncio.gather(*turns)
    assert environments.creates == 1
    assert len(set(ids)) == 1


async def test_a_failed_create_refuses_plainly_and_is_never_resent_blindly():
    store, environments, clock = MemoryStateStore(), FakeEnvironments(fail=1), Clock()
    with pytest.raises(AdmissionDenied) as refused:
        await _get(store, environments, clock)
    assert refused.value.reason == "provider_setup_pending"
    # The create's outcome is unknown: a turn soon after waits, then refuses again.
    with pytest.raises(AdmissionDenied):
        await _get(store, environments, clock)
    assert environments.creates == 1
    # Once it's older than any in-flight request, reconciling proves it absent: create again.
    clock.at += STALE_AFTER
    environment_id = await _get(store, environments, clock)
    assert environments.creates == 2
    assert await _get(store, environments, clock) == environment_id


async def test_a_create_that_landed_after_a_timeout_is_reconciled_not_duplicated():
    store, clock = MemoryStateStore(), Clock()
    environments = FakeEnvironments(fail=1)
    with pytest.raises(AdmissionDenied):
        await _get(store, environments, clock)
    # OpenAI did make it, after all.
    landed = environments._environment(TENANT, environment_name(DEPLOYMENT, TENANT))
    environments.created.append(landed)
    clock.at += STALE_AFTER
    assert await _get(store, environments, clock) == landed.ref.id
    assert environments.creates == 1


def test_the_refusal_copy_says_to_try_again():
    nouns = RefusalNouns(scope="server", admin="a server admin", billing="/billing")
    text = admission_refusal_text("provider_setup_pending", nouns)
    assert text == "Couldn't set up GPT for this server yet.\n\nTry again shortly."


# Through a real turn's preparation, on Postgres.


@pytest.fixture
def provisioning(consent_request, monkeypatch, tmp_path):
    request, *_ = consent_request
    request = replace(
        request,
        deps=replace(request.deps, state_store=PostgresStateStore(request.deps.sessionmaker)),
    )
    environments = FakeEnvironments()
    monkeypatch.setattr(openai_deployment, "_environments", lambda key: environments)
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "offline-key")
    monkeypatch.setattr(openai_deployment, "_deployment_id", AsyncMock(return_value=DEPLOYMENT))
    manifest = tmp_path / "runtime.json"
    manifest.write_text("[]")
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    return request, environments, manifest


async def test_a_gpt_root_turn_on_a_tenant_with_no_row_gets_its_environment(provisioning):
    request, environments, _ = provisioning
    assert request.admission.backend_revision.profile == "openai.persistent_workspace"
    prepared = await openai_deployment.prepare_tenant_environment(request)
    again = await openai_deployment.prepare_tenant_environment(request)
    assert prepared.openai_environment_id == again.openai_environment_id == "envtmpl_1"
    assert environments.creates == 1
    # Recorded under the code defaults' own label, like the row that will use it.
    assert environments.keys == [operation_key(DEFAULT_PROJECT)]


async def test_concurrent_first_gpt_turns_on_postgres_create_exactly_one(provisioning):
    request, environments, _ = provisioning
    prepared = await asyncio.gather(
        *(openai_deployment.prepare_tenant_environment(request) for _ in range(4))
    )
    assert {item.openai_environment_id for item in prepared} == {"envtmpl_1"}
    assert environments.creates == 1


async def test_a_tenant_with_its_own_init_row_keeps_it(provisioning):
    request, environments, manifest = provisioning
    revision = request.admission.backend_revision
    manifest.write_text(
        json.dumps(
            [
                {
                    "profile": "openai.persistent_workspace",
                    "tenant_id": request.scope.tenant_id,
                    "platform": "*",
                    "channel_id": "*",
                    "account_id": "*",
                    "configuration_grant": {
                        "profiles": ["openai.persistent_workspace"],
                        "models": [revision.model or "gpt-6-luna"],
                        "thread_modes": ["per_caller", "shared"],
                        "requires": {},
                    },
                    "project": "proj_operator",
                    # The tenant-wide template `daimon openai init` + an operator
                    # install today, with its own environment (as on staging).
                    "root_materialization": True,
                    "spend_limit_usd_cents": 50,
                    "environment_id": "envtmpl_operator",
                    "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
                }
            ]
        )
    )
    assert await openai_deployment.prepare_tenant_environment(request) is request
    assert environments.creates == 0


async def test_a_tenant_row_scoped_elsewhere_provisions_nothing(provisioning):
    """The operator configured this profile, just not for this channel: selection never
    falls back to the code defaults there, so no environment is made for them."""
    request, environments, manifest = provisioning
    revision = request.admission.backend_revision
    manifest.write_text(
        json.dumps(
            [
                {
                    "profile": "openai.persistent_workspace",
                    "tenant_id": request.scope.tenant_id,
                    "platform": "*",
                    "channel_id": "another-channel",
                    "account_id": "*",
                    "configuration_grant": {
                        "profiles": ["openai.persistent_workspace"],
                        "models": [revision.model or "gpt-6-luna"],
                        "thread_modes": ["per_caller"],
                        "requires": {},
                    },
                    "project": "proj_operator",
                    "root_materialization": True,
                    "spend_limit_usd_cents": 50,
                    "environment_id": "envtmpl_operator",
                    "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
                }
            ]
        )
    )
    assert revision.channel.channel_id != "another-channel"
    assert await openai_deployment.prepare_tenant_environment(request) is request
    assert environments.creates == 0


async def test_an_install_without_an_openai_key_provisions_nothing(provisioning, monkeypatch):
    request, environments, _ = provisioning
    monkeypatch.delenv("DAIMON_CHANNEL_OPENAI_KEY")
    assert await openai_deployment.prepare_tenant_environment(request) is request
    assert environments.creates == 0


async def test_a_worker_turn_provisions_nothing(provisioning):
    request, environments, _ = provisioning
    worker = object()
    assert (
        await openai_deployment.prepare_tenant_environment(replace(request, worker=worker))
    ).worker is worker
    assert environments.creates == 0


async def test_the_operation_record_never_holds_the_key(provisioning):
    request, _, _ = provisioning
    await openai_deployment.prepare_tenant_environment(request)
    record = await request.deps.state_store.get_operation(
        openai_environment.operation_scope(request.scope.tenant_id),
        operation_key(DEFAULT_PROJECT),
    )
    assert record is not None and record.operation.status == "processed"
    assert "offline-key" not in json.dumps(record.model_dump(mode="json"))


async def test_concurrent_reconciles_never_create_a_duplicate():
    store, environments, clock = MemoryStateStore(), FakeEnvironments(fail=1), Clock()
    with pytest.raises(AdmissionDenied):
        await _get(store, environments, clock)
    clock.at += STALE_AFTER
    scope = openai_environment.operation_scope(TENANT)
    stale = await store.get_operation(scope, operation_key(PROJECT))
    assert stale is not None and stale.operation.status == "outcome_unknown"
    # T1 reconciles (absent), claims and creates.
    created = await _get(store, environments, clock)
    # T2 listed before T1's create landed: its late "absent" must not reset T1's outcome.
    late = await openai_environment._reconcile(
        store,
        FakeEnvironments(),
        stale,
        scope,
        TENANT,
        environment_name(DEPLOYMENT, TENANT),
        clock,
    )
    assert late.operation.status == "processed"
    assert await _get(store, environments, clock) == created
    assert environments.creates == 2  # the failed first try and T1's; never a third


async def test_a_late_create_returns_the_recorded_id_not_its_own():
    gate = asyncio.Event()
    store, clock = MemoryStateStore(), Clock()
    slow = FakeEnvironments(gate=gate)
    first = asyncio.create_task(_get(store, slow, clock))
    await asyncio.sleep(0)
    # Meanwhile the operation was settled (e.g. by a reconcile) with another id.
    scope = openai_environment.operation_scope(TENANT)
    await store.advance_operation(
        scope,
        operation_key(PROJECT),
        "processed",
        now=clock(),
        fence=None,
        result={"environment_id": "envtmpl_recorded"},
    )
    gate.set()
    assert await first == "envtmpl_recorded"


async def test_a_settled_tenant_reads_without_beginning_an_operation():
    store, environments, clock = MemoryStateStore(), FakeEnvironments(), Clock()
    first = await _get(store, environments, clock)

    async def no_begin(*_args, **_kwargs):
        raise AssertionError("a settled tenant took the operation's row lock")

    store.begin_operation = no_begin  # type: ignore[method-assign]
    assert await _get(store, environments, clock) == first


async def test_another_deployment_never_adopts_this_ones_environment():
    store, clock = MemoryStateStore(), Clock()
    environments = FakeEnvironments(fail=1)
    production = MemoryStateStore()
    with pytest.raises(AdmissionDenied):
        await _get(store, environments, clock)
    # Staging's environment exists in the shared project; production's create failed too.
    environments.created.append(
        environments._environment(TENANT, environment_name(DEPLOYMENT, TENANT))
    )
    environments.fail = 1
    with pytest.raises(AdmissionDenied):
        await _get(production, environments, clock, deployment=PRODUCTION)
    clock.at += STALE_AFTER
    mine = await _get(production, environments, clock, deployment=PRODUCTION)
    assert mine != "envtmpl_1"  # production made its own, never staging's
    assert environments.created[-1].spec.name == environment_name(PRODUCTION, TENANT)


async def test_the_setup_refusal_reaches_the_person_through_preparation(provisioning, monkeypatch):
    from daimon.core.turn.runtime_manifest import read_preparation_counterpart
    from daimon.core.turn.runtimes import build_channel_runtime, scope_channel_runtimes

    request, environments, _ = provisioning
    environments.fail = 1
    request = replace(
        request,
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        deps=replace(request.deps, turn_runtimes={}, turn_runtime_factory=build_channel_runtime),
    )
    request, _ = scope_channel_runtimes(request)
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert refused.value.reason == "provider_setup_pending"
    assert environments.creates == 1


# End to end: a fresh tenant (no runtime-file rows) runs its agent's GPT turns.


@pytest.fixture
async def fresh_tenant(consent_request, monkeypatch, tmp_path):
    from daimon.core.turn.prepare import admitted_session_scope
    from daimon.core.turn.runtime_defaults import DEFAULT_PROJECT
    from daimon.core.turn.runtimes import build_channel_runtime

    from .test_openai_full_source import FullWire
    from .test_openai_runtime_transport import clients_for

    request, *_ = consent_request
    sessions = request.deps.sessionmaker
    request = replace(
        request,
        scope=admitted_session_scope(
            request.admission, tenant_id=request.tenant_id, session_id="preparation"
        ),
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        deps=replace(
            request.deps,
            state_store=PostgresStateStore(sessions),
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
        ),
    )
    wire = FullWire(request.scope.tenant_id)
    clients = clients_for(monkeypatch, wire.handle)
    manifest = tmp_path / "runtime.json"
    manifest.write_text("[]")  # the operator installed nothing for this tenant
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "offline")
    monkeypatch.setattr(openai_deployment, "_deployment_id", AsyncMock(return_value=DEPLOYMENT))

    class NativeTemplate(FakeEnvironments):
        def _environment(self, tenant_id: str, name: str) -> Environment:
            made = super()._environment(tenant_id, name)
            ref = made.ref.model_copy(
                update={"id": "native-template", "account_scope_id": DEFAULT_PROJECT}
            )
            return made.model_copy(update={"ref": ref})

    environments = NativeTemplate()
    monkeypatch.setattr(openai_deployment, "_environments", lambda key: environments)
    yield request, wire, environments
    assert all(client.is_closed() for client in clients)


async def test_a_fresh_tenants_gpt_turns_run_on_its_provisioned_environment(fresh_tenant):
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.turn_fakes import RecordingLifecycle

    from .test_openai_agent_template import agent_creates

    request, wire, environments = fresh_tenant
    for index in range(2):
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=index != 0,
        )
        assert prepared.session_ref.provider == "openai"

        async def no_recovery():
            pytest.fail("the turn changed its admitted source/session")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key=f"fresh:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
    assert environments.creates == 1, "the tenant's environment is created once"
    assert environments.created[0].spec.name == environment_name(
        DEPLOYMENT, request.scope.tenant_id
    )
    assert len(agent_creates(wire)) == 1
    assert wire.posts[0]["environment"]["environment_template_id"] == "native-template"


async def test_an_unknown_deployment_creates_nothing(provisioning, monkeypatch):
    request, environments, _ = provisioning
    monkeypatch.setattr(openai_deployment, "_deployment_id", AsyncMock(return_value=None))
    assert await openai_deployment.prepare_tenant_environment(request) is request
    assert environments.creates == 0


def test_a_name_needs_a_deployment():
    with pytest.raises(ValueError):
        environment_name("", TENANT)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, "example-staging\n", "example-staging"),
        (404, "example-staging", None),
        (200, "", None),
        (200, "Not A Project!", None),
    ],
)
async def test_the_deployment_is_this_hosts_gcp_project(monkeypatch, status, body, expected):
    import httpx

    monkeypatch.setattr(openai_environment, "_deployment", None)
    monkeypatch.setattr(openai_environment, "_missed_until", None)
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    found = await openai_environment.deployment_id(transport=httpx.MockTransport(answer))
    assert found == expected
    assert seen[0].url == openai_environment.METADATA_PROJECT_URL
    assert seen[0].headers["Metadata-Flavor"] == "Google"
    # Kept once read; a miss is remembered briefly, so the next turn doesn't ask again.
    assert openai_environment._deployment == expected
    again = await openai_environment.deployment_id(transport=httpx.MockTransport(answer))
    assert again == expected and len(seen) == 1


async def test_off_gcp_the_deployment_is_unknown(monkeypatch):
    import httpx

    monkeypatch.setattr(openai_environment, "_deployment", None)
    monkeypatch.setattr(openai_environment, "_missed_until", None)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no metadata server", request=request)

    assert (
        await openai_environment.deployment_id(transport=httpx.MockTransport(unreachable)) is None
    )


async def test_a_miss_is_asked_again_after_it_expires(monkeypatch):
    import httpx

    monkeypatch.setattr(openai_environment, "_deployment", None)
    monkeypatch.setattr(
        openai_environment, "_missed_until", datetime.now(UTC) - timedelta(seconds=1)
    )
    answered = httpx.MockTransport(lambda request: httpx.Response(200, text="example-prod"))
    assert await openai_environment.deployment_id(transport=answered) == "example-prod"


@pytest.mark.parametrize("late", ["fails", "succeeds"])
async def test_a_stalled_create_that_was_superseded_never_overwrites_it(late):
    gate = asyncio.Event()
    store, clock = MemoryStateStore(), Clock()
    stalled = FakeEnvironments(gate=gate, fail=1 if late == "fails" else 0)
    first = asyncio.create_task(_get(store, stalled, clock))
    await asyncio.sleep(0)
    # The first create hangs past STALE_AFTER: another turn reconciles (absent) and creates.
    clock.at += STALE_AFTER
    other = FakeEnvironments()
    winner = await _get(store, other, clock)
    gate.set()
    if late == "fails":
        with pytest.raises(AdmissionDenied) as refused:
            await first
        assert refused.value.reason == "provider_setup_pending"
    else:
        assert await first == winner  # the recorded id, not its own late one
    record = await store.get_operation(
        openai_environment.operation_scope(TENANT), operation_key(PROJECT)
    )
    assert record is not None and record.operation.status == "processed"
    assert await _get(store, other, clock) == winner
