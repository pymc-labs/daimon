"""An OpenAI agent template keeps Daimon's MCP tools with a per-turn credential (J6).

The code-default OpenAI root row carries `template_capabilities: "mcp"`, like
Gemini's. Consent keeps the agent's Daimon MCP toolset; each turn mints one
registered `provider_turn` caller credential in memory. The session's own vault
receives it on the first turn, and every later turn replaces that credential's
secret in place (the session binds its MCP tool to the credential id), then the
turn's end revokes it. Only the HTTP edges are scripted; the MCP server is the
real verifier and middleware.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core.mcp_auth import token_jti
from daimon.core.stores import mux_state
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.prepare import admitted_session_scope, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import ConfigRevision

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_openai_agent_template import KEY, template_row
from .test_openai_full_source import PARAMS, FullWire
from .test_openai_runtime_transport import clients_for

#: The switch and hand-off tools a copy needs on any provider (ROOT, J1/J6).
HANDOFF_TOOLS = ("fork_agent", "hand_off_task")


@asynccontextmanager
async def handoff_mcp(request) -> AsyncIterator[object]:
    """The real verifier, identity middleware and agent-chat search at the HTTP edge.

    The tools are offline stubs under their production names: what is checked
    here is that a provider-turn credential is offered and may call them.
    """
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
    from daimon.adapters.mcp.middleware.mcp_identity import (
        IdentityMiddleware,
        production_agent_id_resolver,
        production_internal_resolver,
        production_is_admin_resolver,
        production_role_resolver,
        production_subject_resolver,
        production_tenant_resolver,
    )
    from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
    from fastmcp import FastMCP

    mcp = FastMCP(
        "source-catalogue",
        auth=DaimonJWTVerifier(
            secret=request.deps.mcp.jwt_secret.get_secret_value().encode(),
            sessionmaker=request.deps.sessionmaker,
        ),
    )
    identity = IdentityMiddleware(
        subject_resolver=production_subject_resolver,
        tenant_resolver=production_tenant_resolver,
        role_resolver=production_role_resolver,
        agent_id_resolver=production_agent_id_resolver,
        is_admin_resolver=production_is_admin_resolver,
        internal_resolver=production_internal_resolver,
        sessionmaker=request.deps.sessionmaker,
    )
    mcp.add_middleware(identity)
    for name in ("describe_agent", *HANDOFF_TOOLS, "request_agent_backend_switch"):

        async def stub() -> str:
            return "offline"

        mcp.tool(stub, name=name, description=f"{name} offline", tags={"agent-chat"})
    mcp.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
    try:
        yield mcp.http_app()
    finally:
        await identity.drain_audit()


@pytest.fixture
async def source_mcp(consent_request):
    async with handoff_mcp(consent_request[0]) as app:
        yield app


def bearer(auth: dict[str, object]) -> str:
    token = auth["token"]
    assert isinstance(token, str)
    return token


@pytest.fixture
async def template(consent_request, source_mcp, monkeypatch, tmp_path, request):
    from daimon.core.authz import Place
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.scope import DeploymentDefault, ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.testing.asgi import mcp_session

    param = getattr(request, "param", "gpt-6-luna")
    # {"legacy": True}: consent was given before this deploy's template kept MCP.
    model = param if isinstance(param, str) else param.get("model", "gpt-6-luna")
    legacy = isinstance(param, dict) and param.get("legacy", False)
    request, _, source, actor, accepted, router, *_ = consent_request
    sessions = request.deps.sessionmaker
    router.add_environment_list(request.admission.environment)
    scope = admitted_session_scope(
        request.admission, tenant_id=request.tenant_id, session_id="preparation"
    )
    request = replace(
        request,
        scope=scope,
        admission=replace(
            request.admission,
            origin_channel_id=request.admission.backend_revision.channel.channel_id,
        ),
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        deps=replace(
            request.deps,
            deployment_default=DeploymentDefault(
                agent_name=source.name, environment_name="test-env"
            ),
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
            state_store=PostgresStateStore(sessions),
        ),
    )
    wire = FullWire(scope.tenant_id)
    clients = clients_for(monkeypatch, wire.handle)

    async def catalogue(call):
        assert call.url.host == "mcp.invalid"
        body = json.loads(call.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, object] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "offline", "version": "1"},
        }
        if body["method"] == "tools/list":
            token = call.headers["Authorization"].removeprefix("Bearer ")
            result = (await mcp_session(source_mcp, token=token, method="tools/list"))["result"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original = httpx.AsyncClient

    class RouteClient(original):
        def __init__(self, **kwargs):
            if "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(catalogue)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RouteClient)
    row = template_row(
        scope.tenant_id,
        template_capabilities="mcp",
        configuration_grant={
            "profiles": ["openai.persistent_workspace"],
            "models": ["gpt-6-luna", "gpt-6-astra"],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
    )
    manifest = tmp_path / "runtime.json"
    before = {key: value for key, value in row.items() if key != "template_capabilities"}
    manifest.write_text(json.dumps([before if legacy else row]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    monkeypatch.setenv(KEY, "offline")

    async def capture_bound_source(actual_source):
        return await capture_backend_source(
            request.deps,
            actual_source,
            context=ScopeContext(
                tenant_id=request.tenant_id,
                account_id=request.admission.account_id,
                platform=request.platform,
                channel_id=request.admission.origin_channel_id,
                thread_id=request.thread_id,
            ),
            subject=actor.subject,
            place=Place(
                channel_id=request.thread_id, parent_channel_id=request.admission.origin_channel_id
            ),
            is_dm=False,
            slack_turn_context_id=None,
            private_dm_id=None,
            external_user_id=request.external_user_id,
        )

    consenting = AgentBackendService(sessions, enabled=True, capture_source=capture_bound_source)
    revision = request.admission.backend_revision
    state = {"expected": accepted.local, "local": 1}

    async def consent(current):
        """Preview and confirm the switch here, as a person on the card would."""
        quote = await consenting.preview(source, "openai", model)
        confirmed = await consenting.set_backend(
            source,
            "openai",
            state["expected"],
            actor,
            model=model,
            accepted_drops=quote.declared_drops,
            expected_preview_digest=quote.digest,
        )
        state["expected"], state["local"] = confirmed.local, state["local"] + 1
        linked = ConfigRevision.create(
            revision.channel, state["local"], revision.model_copy(update={"model": model})
        )
        async with sessions.begin() as db:
            await mux_state.put_config_revision(
                db, linked, origin="agent", agent_revision=confirmed
            )
        return quote, replace(
            current, admission=replace(current.admission, backend_revision=linked)
        )

    quote, request = await consent(request)
    if legacy:
        manifest.write_text(json.dumps([row]))  # this deploy: the template keeps MCP
    wire.requests.clear()
    yield request, quote, wire, consent
    assert all(client.is_closed() for client in clients)


async def _turn(request, index):
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
        pytest.fail("the template turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="Use a Daimon tool.",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda _: RecordingLifecycle(),
        operation_key=f"template-mcp:{index}",
    )
    assert outcome.state.error is None and not outcome.recovered


async def _revoked(request, token: str) -> bool:
    async with request.deps.sessionmaker() as db:
        row = await get_mcp_token(db, jti=token_jti(token))
    assert row is not None and row.surface == "provider_turn"
    assert str(row.account_id) == request.scope.account_id
    return row.revoked_at is not None


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_consent_keeps_daimon_tools_and_drops_only_what_the_template_lacks(template):
    _, quote, *_ = template
    assert "mcp" not in quote.declared_drops
    assert {"memory", "skills"} <= set(quote.declared_drops)
    # J6b: its execution tools map to bash in the hosted environment; only web tools
    # (none on this source) would be named losses.
    assert "tools" not in quote.declared_drops


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("template", ["gpt-6-luna", "gpt-6-astra"], indirect=True)
async def test_each_turn_puts_its_own_credential_in_the_session_vault_and_revokes_it(
    template, source_mcp
):
    """J6 e2e: a copy on Luna or Astra keeps Daimon MCP across turns of one session."""
    from daimon.testing.asgi import mcp_session

    request, _, wire, _ = template
    await _turn(request, 0)
    assert len(wire.posts) == 1
    body = wire.posts[0]
    (mcp,) = [tool for tool in body["agent"]["tools"] if tool["type"] == "mcp"]
    assert body["vault_ids"] == ["caller-session-vault"]
    assert mcp["credential_id"] == "caller-mcp-credential"
    assert mcp["transport"]["server_url"] == "https://mcp.invalid/mcp"
    first = bearer(wire.vault_credential["auth"])
    assert first not in json.dumps(body) and "Bearer" not in json.dumps(wire.agent)
    assert await _revoked(request, first), "the turn's end revoked its credential"

    await _turn(request, 1)
    assert len(wire.posts) == 1, "the second turn reuses the session"
    # Every input writes the turn's bearer into the same credential id, in place
    # (the first turn's write repeats the bearer its session was created with).
    tokens = [bearer(rotation["auth"]) for rotation in wire.rotations]  # pyright: ignore[reportArgumentType]
    assert all(set(rotation) == {"auth"} for rotation in wire.rotations)
    assert tokens[0] == first and len(tokens) == 2
    second = tokens[1]
    assert second != first
    assert await _revoked(request, second)

    # What the provider holds during a turn calls Daimon's MCP: the copy's switch
    # and hand-off tools are offered and run. The switch runs for this credential
    # too: its confirm call posts Daimon's own card (`daimon.core.switch_cards`).
    live = await live_token(request)
    await offered_and_run(source_mcp, live)
    switched = await mcp_session(
        source_mcp,
        token=live,
        method="tools/call",
        params={"name": "request_agent_backend_switch", "arguments": {}},
    )
    assert "confirmation card" not in json.dumps(switched)
    assert switched["result"]["isError"] is False


async def offered_and_run(source_mcp, token: str) -> None:
    from daimon.testing.asgi import mcp_session

    for name in ("describe_agent", *HANDOFF_TOOLS):
        called = await mcp_session(
            source_mcp,
            token=token,
            method="tools/call",
            params={"name": "call_tool", "arguments": {"name": name, "arguments": {}}},
        )
        assert not called["result"].get("isError", False), name
        assert "offline" in json.dumps(called), name


async def live_token(request) -> str:
    """A credential exactly as a turn mints it (not yet revoked)."""
    from daimon.core.turn.runtime_manifest import read_preparation_counterpart
    from daimon.core.turn.runtimes import scope_channel_runtimes

    scoped, lookup = scope_channel_runtimes(request)
    selected = await read_preparation_counterpart(scoped)
    (connection,) = selected.target_spec.mcp_servers
    runtime = lookup.resolved
    # OpenAI's host holds the resolver; Gemini's transport carries it.
    resolve = getattr(runtime, "mcp_secrets", None) or (
        runtime.transport_factory(request.admission.backend_revision, request.scope).mcp_secrets
    )
    assert resolve is not None
    return await resolve(request.scope, connection.credential_ref, connection.url)


CONNECTOR_PARAMS = {
    **PARAMS,
    "mcp_servers": [{"type": "url", "name": "linear", "url": "https://mcp.linear.app/mcp"}],
    "tools": [
        *PARAMS["tools"],
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "configs": [],
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        },
    ],
}


@pytest.mark.parametrize("consent_request", [CONNECTOR_PARAMS], indirect=True)
async def test_a_user_connector_drops_only_itself_never_daimon_mcp(template):
    """An agent with its own connector keeps Daimon MCP; the card names the connector."""
    from daimon.core.confirmation import drop_words

    request, quote, wire, _ = template
    assert "mcp_server:linear" in quote.declared_drops
    assert "mcp" not in quote.declared_drops
    assert drop_words(("mcp_server:linear",)) == "its linear connector"
    await _turn(request, 0)
    (body,) = wire.posts
    servers = [tool["server_label"] for tool in body["agent"]["tools"] if tool["type"] == "mcp"]
    assert servers == ["daimon-mcp"], "Daimon MCP stays; the connector never reaches OpenAI"
    assert "mcp.linear.app" not in json.dumps([wire.agent, body])
    # The model is told plainly what this copy can't reach.
    assert "mcp_server:linear" in wire.agent["instructions"]


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("template", [{"legacy": True}], indirect=True)
async def test_a_copy_switched_before_this_deploy_is_asked_to_confirm_again_once(template):
    """Consent that declared MCP dropped no longer matches: re_confirm, never a silent gain."""
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.turn.errors import AdmissionDenied

    request, quote, wire, consent = template
    assert "mcp" in quote.declared_drops, "consented while the template had no MCP"
    with pytest.raises(AdmissionDenied) as refused:
        await _turn(request, 0)
    assert isinstance(refused.value.__cause__, AgentBackendRefused)
    assert refused.value.__cause__.reason == "re_confirm"
    assert wire.posts == [], "nothing reached OpenAI"
    again, request = await consent(request)
    assert "mcp" not in again.declared_drops
    await _turn(request, 0)
    (body,) = wire.posts
    assert [tool["type"] for tool in body["agent"]["tools"]].count("mcp") == 1


@pytest.mark.parametrize(
    "consent_request",
    [
        {
            **CONNECTOR_PARAMS,
            "mcp_servers": [
                {"type": "url", "name": "my linear", "url": "https://mcp.linear.app/mcp"}
            ],
            "tools": [
                *PARAMS["tools"],
                {**CONNECTOR_PARAMS["tools"][-1], "mcp_server_name": "my linear"},
            ],
        }
    ],
    indirect=True,
)
async def test_a_connector_name_no_drop_can_spell_fails_safe(template):
    """A name outside [A-Za-z0-9_.-] can't be a named loss: MCP drops whole, nothing leaks."""
    request, quote, wire, _ = template
    assert "mcp" in quote.declared_drops
    assert not any(drop.startswith("mcp_server:") for drop in quote.declared_drops)
    await _turn(request, 0)
    (body,) = wire.posts
    assert not [tool for tool in body["agent"].get("tools", []) if tool["type"] == "mcp"]
    assert "mcp.linear.app" not in json.dumps([wire.agent, body])


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_a_preparation_that_fails_after_minting_revokes_the_credential(template, monkeypatch):
    """A token minted for a turn that never starts is revoked now, not left to its TTL."""
    from daimon.core.turn import openai_host

    request, _, _, _ = template
    minted: list[str] = []

    async def mint_then_fail(preparation, runtime):
        connection = preparation.target_spec.mcp_servers[0]
        assert runtime.mcp_secrets is not None
        minted.append(
            await runtime.mcp_secrets(preparation.scope, connection.credential_ref, connection.url)
        )
        raise RuntimeError("preparation failed after the session create")

    monkeypatch.setattr(openai_host, "_prepare_with_runtime", mint_then_fail)
    with pytest.raises(Exception):  # noqa: B017 - whatever admission makes of it
        await _turn(request, 0)
    (token,) = minted
    assert await _revoked(request, token)
