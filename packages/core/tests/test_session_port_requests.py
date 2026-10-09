"""Byte-identical lifecycle requests at the real SDK HTTP boundary."""

from __future__ import annotations

import uuid

import httpx
import pytest
from anthropic import omit
from daimon.core.sessions import create_isolated_session, create_session
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("mounted", [False, True])
@pytest.mark.parametrize("stamped", [False, True])
async def test_session_create_matches_legacy_wire_request(
    isolated: bool, mounted: bool, stamped: bool
) -> None:
    agent = ma_agent(id="ag_parity")
    environment = ma_environment(id="env_parity")
    tenant = uuid.UUID(int=1) if stamped else None
    account = uuid.UUID(int=2) if stamped else None
    resources = [{"type": "file", "file_id": "file_parity", "mount_path": "/bundle.tar.gz"}]
    if not mounted:
        resources = []
    metadata = {"daimon_account": str(account), "daimon_tenant": str(tenant)} if stamped else {}
    # Echo an extra field and explicit nulls: the M0 return codec must preserve
    # the original SDK model as well as what went over the wire.
    response = ma_session(id="sess_parity").model_dump(mode="json")
    response["provider_future_field"] = {"kept": True}
    legacy = ScriptedTransport()
    migrated = ScriptedTransport()
    for transport in (legacy, migrated):
        transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=response)))

    async with legacy.client() as old_client, migrated.client() as client:
        if isolated:
            old = await old_client.beta.sessions.create(
                agent=agent.id,
                environment_id=environment.id,
                metadata=metadata if metadata else omit,
                resources=resources,
            )
            result = await create_isolated_session(
                client,
                agent=agent,
                environment=environment,
                tenant_id=tenant,
                account_id=account,
                resources=resources,
            )
        else:
            old = await old_client.beta.sessions.create(
                agent=agent.id,
                environment_id=environment.id,
                metadata=metadata if metadata else omit,
                vault_ids=omit,
                resources=resources if resources else omit,
            )
            result = await create_session(
                client,
                agent=agent,
                environment=environment,
                tenant_id=tenant,
                account_id=account,
                extra_resources=resources,
            )
    legacy.assert_consumed()
    migrated.assert_consumed()
    assert migrated.requests == legacy.requests
    assert result.model_dump(mode="json", exclude_unset=True) == old.model_dump(
        mode="json", exclude_unset=True
    )
    body = migrated.requests[0].json()
    assert isinstance(body, dict)
    assert ("resources" in body) == (mounted or isolated)
    assert "vault_ids" not in body


async def test_last_access_check_runs_before_create_request() -> None:
    transport = ScriptedTransport()
    response = ma_session(id="sess_parity").model_dump(mode="json")
    transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=response)))
    checked = False

    async def before_create() -> None:
        nonlocal checked
        assert not transport.requests
        checked = True

    async with transport.client() as client:
        await create_session(
            client,
            agent=ma_agent(id="ag_parity"),
            environment=ma_environment(id="env_parity"),
            before_create=before_create,
        )
    transport.assert_consumed()
    assert checked


async def test_repository_tokens_stay_opaque_until_the_single_wire_request(monkeypatch) -> None:
    from anthropic.types.beta.session_create_params import Resource
    from daimon.core.session_ports_compat import create_session_record, session_scope
    from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions

    resources: list[Resource] = [
        {
            "type": "github_repository",
            "url": f"https://github.com/example/repo-{index}",
            "authorization_token": f"dummy-repo-token-{index}",
            "checkout": {"type": "branch", "name": "main"},
        }
        for index in range(2)
    ]
    scoped_specs: list[str] = []
    original = AnthropicSessions.create

    async def inspect_spec(self, scope, spec, **kwargs):
        scoped_specs.append(spec.model_dump_json())
        return await original(self, scope, spec, **kwargs)

    monkeypatch.setattr(AnthropicSessions, "create", inspect_spec)
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions",
                httpx.Response(200, json=ma_session().model_dump(mode="json")),
            )
        )
    scope = session_scope(
        tenant_id=uuid.UUID(int=1), account_id=uuid.UUID(int=2), call_site="test:create"
    )
    async with old.client() as legacy, new.client() as client:
        await legacy.beta.sessions.create(
            agent="ag_parity", environment_id="env_parity", resources=resources
        )
        await create_session_record(
            client,
            agent="ag_parity",
            environment_id="env_parity",
            scope=scope,
            metadata=None,
            resources=resources,
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
    assert len(scoped_specs) == 1
    for index in range(2):
        assert f"dummy-repo-token-{index}" not in scoped_specs[0]
    assert "authorization_token_ref" in scoped_specs[0]
