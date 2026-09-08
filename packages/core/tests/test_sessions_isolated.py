from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.session_create_params import Resource
from daimon.core.sessions import create_isolated_session
from daimon.testing.ma import EMPTY_CLOUD_CONFIG
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http


def _make_agent(*, anthropic_id: str = "ag_reader", name: str = "reader") -> BetaManagedAgentsAgent:
    """Inline BetaManagedAgentsAgent construction — no DB needed."""
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": anthropic_id,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {},
            "description": None,
            "archived_at": None,
            "created_at": "2026-04-21T00:00:00Z",
            "updated_at": "2026-04-21T00:00:00Z",
            "version": 1,
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "system": None,
        }
    )


def _make_env(*, anthropic_id: str = "env_reader", name: str = "e") -> BetaEnvironment:
    """Inline BetaEnvironment construction — no DB needed."""
    return BetaEnvironment(
        id=anthropic_id,
        type="environment",
        name=name,
        config=EMPTY_CLOUD_CONFIG,
        metadata={},
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    )


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
    return BetaManagedAgentsSession.model_validate(
        {
            "id": session_id,
            "agent": {
                "id": agent_id,
                "mcp_servers": [],
                "model": {"id": "claude-opus-4-7"},
                "name": "reader",
                "skills": [],
                "tools": [],
                "type": "agent",
                "version": 1,
            },
            "created_at": "2026-04-21T00:00:00Z",
            "outcome_evaluations": [],
            "environment_id": environment_id,
            "metadata": metadata or {},
            "resources": [],
            "stats": {},
            "status": "idle",
            "type": "session",
            "updated_at": "2026-04-21T00:00:00Z",
            "usage": {},
            "vault_ids": [],
        }
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
    )

    paths = [r.url.path for r in requests]
    assert all("vault" not in p for p in paths), f"no vault endpoint should be called; got {paths}"
    assert all("files" not in p for p in paths), f"no files.upload call should be made; got {paths}"
    assert paths == ["/v1/sessions"], "exactly one call, to session create, and nothing else"
