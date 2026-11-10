"""An install with only an OpenAI or Gemini key: defaults, creation, edits and turns.

Every Anthropic HTTP request fails the test: the client carries the keyless
placeholder and a transport that records and refuses each call.
"""

from pathlib import Path

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.agent_editing import (
    AgentActor,
    AgentEditDeps,
    AgentPatch,
    create_agent,
    update_agent,
)
from daimon.core.anthropic_guard import ANTHROPIC_UNCONFIGURED_PLACEHOLDER
from daimon.core.authz import Subject, Surface
from daimon.core.channel_backend import derive_agent_uuid
from daimon.core.defaults.provisioning import derive_guild_account_uuid, reconcile_tenant_defaults
from daimon.core.defaults.report import Action
from daimon.core.mux_backend import resource_scope
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_backends
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from daimon.testing.factories import make_account, make_tenant
from mux.profiles import selectable_models

DEFAULTS_ROOT = Path(__file__).resolve().parents[3] / "defaults"
PROVIDERS = ("openai", "gemini")


class AnthropicCalls:
    """A transport that records every Anthropic request and refuses it."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def client(self) -> AsyncAnthropic:
        def refuse(request: httpx.Request) -> httpx.Response:
            self.requests.append(f"{request.method} {request.url.path}")
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})

        transport = httpx.MockTransport(refuse)
        return AsyncAnthropic(
            api_key=ANTHROPIC_UNCONFIGURED_PLACEHOLDER,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=transport),
        )


@pytest.fixture
def only_key(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> str:
    provider: str = request.param
    for name in ("DAIMON_ANTHROPIC__API_KEY", "ANTHROPIC_API_KEY", *DEFAULT_KEY_ENV.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "test-key")  # type: ignore[index]
    return provider


@pytest.mark.parametrize("only_key", PROVIDERS, indirect=True)
async def test_defaults_create_and_edit_without_anthropic(db_session, db_session_factory, only_key):
    calls = AnthropicCalls()
    client = calls.client()
    tenant = await make_tenant(db_session)
    await make_account(db_session, tenant=tenant, id=derive_guild_account_uuid(tenant.id))
    editor = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    scope = resource_scope(tenant_id=str(tenant.id))

    report = await reconcile_tenant_defaults(
        client, db_session_factory, DEFAULTS_ROOT, tenant_id=tenant.id
    )
    actions = {(o.kind, o.name): o.action for o in [*report.environments, *report.agents]}
    assert actions == {
        ("environment", "default"): Action.CREATED,
        ("agent", "daimon"): Action.CREATED,
    }, f"keyless defaults must seed Daimon records: {report}"
    seeded = await SelfHostedAgents(db_session_factory).resolve(scope, "daimon")
    default_model = selectable_models(only_key)[0]  # type: ignore[arg-type]
    assert (seeded.spec.model.provider, seeded.spec.model.id) == (only_key, default_model), (
        "the seeded agent runs on the keyed provider's default model"
    )
    assert seeded.ref.id.startswith("dm_agent_"), "an owned agent has a Daimon id"
    await SelfHostedEnvironments(db_session_factory).resolve(scope, "default")
    async with db_session_factory() as session:
        born = await agent_backends.current(
            session,
            tenant.id,
            derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=seeded.ref.id),
        )
    assert born is not None and born.backend == only_key, (
        "a Daimon agent carries its provider's backend revision, never Managed Agents"
    )

    again = await reconcile_tenant_defaults(
        client, db_session_factory, DEFAULTS_ROOT, tenant_id=tenant.id
    )
    assert {o.action for o in [*again.environments, *again.agents]} == {Action.SKIPPED}, "a rerun changes nothing"

    deps = AgentEditDeps(client=client, session_factory=db_session_factory, public_url=None)
    actor = AgentActor(
        tenant_id=tenant.id,
        account_id=editor.id,
        subject=Subject(is_admin=True),
        surface=Surface.WEB,
    )
    created_id = await create_agent(
        deps,
        actor=actor,
        spec=AgentSpec(name="helper", model="claude-sonnet-5-5", system="Help."),
        owner=False,
    )
    created = await SelfHostedAgents(db_session_factory).resolve(scope, created_id)
    assert created.spec.model.provider == only_key, "a keyless create picks the keyed provider"

    await update_agent(
        deps,
        actor=actor,
        ma_agent_id=created_id,
        patch=AgentPatch(system="Help briefly."),
        expected_version=None,
    )
    edited = await SelfHostedAgents(db_session_factory).resolve(scope, "helper")
    assert edited.spec.system is not None and "Help briefly." in edited.spec.system, (
        "an edit reaches the Daimon record"
    )
    assert calls.requests == [], f"no Anthropic request on a keyless install: {calls.requests}"
