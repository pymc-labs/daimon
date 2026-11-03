from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.session_create_params import Resource
from daimon.core.sessions import create_isolated_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import build_fake_anthropic as build_fake_anthropic_http
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent


def _make_agent(*, anthropic_id: str = "ag_reader", name: str = "reader") -> BetaManagedAgentsAgent:
    """An agent with this file's defaults — no DB needed."""
    return ma_agent(id=anthropic_id, name=name, model="claude-opus-4-7")


def _make_env(*, anthropic_id: str = "env_reader", name: str = "e") -> BetaEnvironment:
    """An environment with this file's defaults — no DB needed."""
    return ma_environment(id=anthropic_id, name=name)


def _bundle_resource(file_id: str = "file_bundle") -> Resource:
    return {"type": "file", "file_id": file_id, "mount_path": "/bundle.tar.gz"}


def _session_body(
    *,
    session_id: str,
    agent_id: str,
    environment_id: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a BetaManagedAgentsSession JSON body via validated SDK models."""
    return ma_session(
        id=session_id,
        agent=ma_session_agent(id=agent_id, name="reader", model="claude-opus-4-7"),
        environment_id=environment_id,
        metadata=metadata,
    ).model_dump(mode="json")


async def test_create_isolated_session_sends_exactly_the_passed_resources() -> None:
    agent = _make_agent()
    env = _make_env()
    resources = [_bundle_resource("file_abc")]
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_iso", agent_id=body["agent"], environment_id=body["environment_id"]
            ),
        )

    client = build_fake_anthropic_http(_handler)

    result = await create_isolated_session(
        client,
        agent=agent,
        environment=env,
        account_id=None,
        tenant_id=None,
        resources=resources,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert isinstance(result, BetaManagedAgentsSession), (
        "create_isolated_session must return BetaManagedAgentsSession"
    )
    assert len(requests) == 1, "must issue exactly one POST /v1/sessions call"
    body = json.loads(requests[0].content)
    assert body["resources"] == [dict(resources[0])], (
        "session-create body must carry exactly the resources the caller passed, nothing else"
    )


async def test_create_isolated_session_never_sends_vault_ids_key() -> None:
    """T-21-02-A: the absence of the vault_ids kwarg is the security property —
    assert on the raw captured JSON body, not on a kwarg the fake reconstructs."""
    agent = _make_agent()
    env = _make_env()
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_novault",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_isolated_session(
        client,
        agent=agent,
        environment=env,
        account_id=None,
        tenant_id=None,
        resources=[_bundle_resource()],
        tool_safety=OPEN_TOOL_SAFETY,
    )

    body = json.loads(requests[0].content)
    assert "vault_ids" not in body, (
        "create_isolated_session must never pass vault_ids, not even as omit"
    )


async def test_create_isolated_session_stamps_account_and_tenant_when_given() -> None:
    agent = _make_agent()
    env = _make_env()
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_md",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
                metadata=body.get("metadata"),
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_isolated_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        tenant_id=tenant_id,
        resources=[_bundle_resource()],
        tool_safety=OPEN_TOOL_SAFETY,
    )

    body = json.loads(requests[0].content)
    assert body["metadata"]["daimon_account"] == str(account_id)
    assert body["metadata"]["daimon_tenant"] == str(tenant_id)


async def test_create_isolated_session_omits_metadata_keys_when_ids_are_none() -> None:
    agent = _make_agent()
    env = _make_env()
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_nomd",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_isolated_session(
        client,
        agent=agent,
        environment=env,
        account_id=None,
        tenant_id=None,
        resources=[_bundle_resource()],
        tool_safety=OPEN_TOOL_SAFETY,
    )

    body = json.loads(requests[0].content)
    assert "daimon_account" not in body.get("metadata", {})
    assert "daimon_tenant" not in body.get("metadata", {})


async def test_create_isolated_session_makes_zero_vault_or_file_upload_calls() -> None:
    """Strongest available proof of no hidden mounts: the fake records zero
    calls to any vaults endpoint and zero calls to files.upload."""
    agent = _make_agent()
    env = _make_env()
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_novoups",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_isolated_session(
        client,
        agent=agent,
        environment=env,
        account_id=None,
        tenant_id=None,
        resources=[_bundle_resource()],
        tool_safety=OPEN_TOOL_SAFETY,
    )

    paths = [r.url.path for r in requests]
    assert all("vault" not in p for p in paths), f"no vault endpoint should be called; got {paths}"
    assert all("files" not in p for p in paths), f"no files.upload call should be made; got {paths}"
    assert paths == ["/v1/sessions"], "exactly one call, to session create, and nothing else"


async def test_create_isolated_session_gates_third_party_tools_with_safety_on() -> None:
    """The bundle path's session asks on a third-party toolset, default and per-tool."""
    from daimon.core.tool_safety import ToolSafetyPolicy

    agent = ma_agent(
        id="ag_reader",
        name="reader",
        model="claude-opus-4-7",
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "configs": [
                    {
                        "name": "create_issue",
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}],
    )
    bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_gated", agent_id="ag_reader", environment_id="env_reader"
            ),
        )

    await create_isolated_session(
        build_fake_anthropic_http(_handler),
        agent=agent,
        environment=_make_env(),
        account_id=None,
        tenant_id=None,
        resources=[_bundle_resource()],
        tool_safety=ToolSafetyPolicy(enabled=True),
    )

    [body] = bodies
    assert body["agent"]["type"] == "agent_with_overrides"
    [toolset] = body["agent"]["tools"]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert [c["permission_policy"] for c in toolset["configs"]] == [{"type": "always_ask"}]
    assert "vault_ids" not in body
