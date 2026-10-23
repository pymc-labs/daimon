"""Logins a fleet's workers would run without: labels, installers, who may share."""

from __future__ import annotations

import json
import uuid

import pytest
from daimon.core.fleet_credentials import (
    ConnectorLogin,
    card_line,
    fleet_logins,
    may_share,
    shareable_with,
)
from daimon.core.stores import agent_mcp_credentials, agent_owners
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import BackendAgent
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ResourceBinding, WorkspaceSource

from .test_agent_backend import owned as owned


def credential(reference: str, target: str | None = None) -> ResourceBinding:
    return ResourceBinding(
        id=reference, kind="credential", credential_ref=reference, target_path=target
    )


async def source_with_every_login(owned):
    service, agent, actor, sessions = owned
    async with sessions.begin() as db:
        await agent_owners.set_owner(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actor.account_id,
            source="admin_assigned",
        )
        stored = await agent_mcp_credentials.upsert_credential(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            mcp_server_url="https://linear.example.test/mcp",
            encrypted_token=b"private-ciphertext",
        )
        await put_agent_file(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            key="OWNER_KEY",
            content="owner-secret-value",
            set_by_account_id=actor.account_id,
        )
        await put_agent_file(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            key="ADMIN_KEY",
            content="admin-secret-value",
            set_by_account_id=None,
        )
    env = f"agent-env:{agent.tenant_id}:{agent.agent_id}"
    source = BackendAgent(
        tenant_id=agent.tenant_id,
        agent_id=agent.agent_id,
        name=agent.name,
        configuration_digest="c",
        effective_spec=AgentSpec(
            name=agent.name,
            model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
            mcp_servers=(MCPConnection(name="linear", url="https://linear.example.test/mcp"),),
        ),
        resource_requirements=(
            credential(f"agent-mcp:{stored.id}"),
            credential(f"caller-mcp:{agent.tenant_id}:{actor.account_id}:{agent.agent_id}"),
            credential(f"{env}:OWNER_KEY", ".env"),
            credential(f"{env}:ADMIN_KEY", ".env"),
            credential("vault:something-new"),
        ),
        repository_sources=(
            WorkspaceSource(
                kind="repository",
                target_path="/workspace/app",
                repository_url="https://github.com/pymc-labs/app.git",
                credential_ref="repo-login",
            ),
        ),
    )
    return source, stored, sessions, actor


async def test_every_login_is_named_without_its_value(owned):
    source, stored, sessions, _ = await source_with_every_login(owned)
    async with sessions() as db:
        logins = await fleet_logins(db, source)
    by_key = {login.key: login for login in logins}
    assert by_key[f"agent-mcp:{stored.id}"].label == "linear"
    assert by_key[f"agent-mcp:{stored.id}"].installed_by == "unknown"
    assert by_key["caller"].installed_by == "caller"
    assert by_key["env:OWNER_KEY"].installed_by == "owner"
    assert by_key["env:ADMIN_KEY"].installed_by == "unknown"
    assert by_key["env:OWNER_KEY"].label == "the stored key OWNER_KEY"
    assert by_key["repo:pymc-labs/app"].kind == "repository"
    assert "github_app" not in by_key  # no live GitHub grants on this agent
    unknown = by_key["vault:something-new"]
    assert unknown.kind == "unknown" and not unknown.shareable
    dumped = json.dumps([login.__dict__ for login in logins])
    for secret in ("private-ciphertext", "owner-secret-value", "admin-secret-value"):
        assert secret not in dumped
    line = card_line(logins)
    assert "linear" in line and "OWNER_KEY" in line and "a stored login" not in line
    assert "secret" not in line


@pytest.mark.parametrize(
    ("installed_by", "requester", "admin", "owner", "allowed"),
    [
        ("caller", True, False, False, True),  # your own connections
        ("caller", False, True, True, False),  # only the requester answers
        ("owner", True, False, True, True),  # the owner's own stored login
        ("owner", True, False, False, False),  # a plain member can't
        ("owner", True, True, False, True),
        ("unknown", True, False, True, False),  # an owner can't share an admin's
        ("unknown", True, True, False, True),
        ("admin", True, False, True, False),
        ("admin", True, True, False, True),
    ],
)
def test_who_may_share(installed_by, requester, admin, owner, allowed):
    me, someone = uuid.uuid4(), uuid.uuid4()
    login = ConnectorLogin("k", "agent_mcp", "linear", installed_by)
    assert (
        may_share(
            login,
            actor_account_id=me,
            requester_account_id=me if requester else someone,
            is_admin=admin,
            is_owner=owner,
        )
        is allowed
    )


def test_unknown_kinds_and_foreign_workers_never_get_a_login():
    unknown = ConnectorLogin("x", "unknown", "a stored login", "admin")
    me = uuid.uuid4()
    assert not may_share(
        unknown, actor_account_id=me, requester_account_id=me, is_admin=True, is_owner=True
    )
    login = ConnectorLogin("k", "agent_mcp", "linear", "admin")
    assert shareable_with(login, "anthropic")
    assert not shareable_with(login, "openai") and not shareable_with(login, "gemini")


from .test_agent_owner_settings import people as people  # noqa: E402


def logins():
    return (
        ConnectorLogin("env:OWNER_KEY", "env", "the stored key OWNER_KEY", "owner"),
        ConnectorLogin("agent-mcp:x", "agent_mcp", "linear", "unknown"),
        ConnectorLogin("caller", "caller_mcp", "your own connections", "caller"),
    )


async def test_choices_are_saved_by_owner_or_admin_and_audited_by_key(people):
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.stores import security_audit

    service, agent, actors, sessions = people
    owner, admin, member = actors["owner"], actors["admin"], actors["member"]
    saved = await service.set_credential_sharing(
        agent,
        {"env:OWNER_KEY": "share", "caller": "share", "agent-mcp:x": "skip"},
        logins(),
        owner,
        requester_account_id=owner.account_id,
        run_id=None,
    )
    assert saved.fleet_credential_sharing == {
        "env:OWNER_KEY": "share",
        "caller": "share",
        "agent-mcp:x": "skip",
    }
    # An owner can't share a login an admin (or someone unknown) stored.
    with pytest.raises(AgentBackendRefused, match="credential share not allowed"):
        await service.set_credential_sharing(
            agent,
            {"agent-mcp:x": "share"},
            logins(),
            owner,
            requester_account_id=owner.account_id,
            run_id=None,
        )
    # An admin may, when they are the requester.
    run = uuid.uuid4()
    await service.set_credential_sharing(
        agent,
        {"agent-mcp:x": "share"},
        logins(),
        admin,
        requester_account_id=admin.account_id,
        run_id=run,
    )
    # A plain member saves nothing, not even "skip".
    with pytest.raises(AgentBackendRefused):
        await service.set_credential_sharing(
            agent,
            {"caller": "skip"},
            logins(),
            member,
            requester_account_id=member.account_id,
            run_id=None,
        )
    # Unknown keys or choices are refused.
    with pytest.raises(AgentBackendRefused, match="invalid credential choice"):
        await service.set_credential_sharing(
            agent,
            {"vault:other": "share"},
            logins(),
            admin,
            requester_account_id=admin.account_id,
            run_id=None,
        )
    async with sessions() as db:
        events = await security_audit.list_events(db, tenant_id=agent.tenant_id)
    shares = [event for event in events if event.tool_name == "fleet_credentials"]
    assert {(event.operation, event.reason) for event in shares} >= {
        ("share", "login=env:OWNER_KEY"),
        ("share", "login=caller"),
        ("skip", "login=agent-mcp:x"),
        ("share", f"login=agent-mcp:x; run={run}"),
    }
    dumped = json.dumps([event.model_dump(mode="json") for event in shares])
    assert "secret" not in dumped and "ciphertext" not in dumped


async def test_a_saved_choice_survives_other_agent_changes(people):
    service, agent, actors, _ = people
    admin = actors["admin"]
    saved = await service.set_credential_sharing(
        agent,
        {"caller": "share"},
        logins(),
        admin,
        requester_account_id=admin.account_id,
        run_id=None,
    )
    switched = await service.set_backend(agent, "openai", saved.local, admin)
    assert switched.fleet_credential_sharing == {"caller": "share"}


async def test_the_run_freezes_only_shared_present_and_shareable_logins(owned):
    from types import SimpleNamespace

    from daimon.core.turn.runtime_manifest import frozen_shared_logins

    service, agent, actor, sessions = owned
    source, stored, _, _ = await source_with_every_login(owned)
    deps = SimpleNamespace(sessionmaker=sessions)
    assert await frozen_shared_logins(deps, source) == frozenset()  # nothing chosen yet
    async with sessions() as db:
        present = await fleet_logins(db, source)
    await service.set_credential_sharing(
        agent,
        {
            "env:OWNER_KEY": "share",
            "caller": "share",
            f"agent-mcp:{stored.id}": "skip",
            "env:ADMIN_KEY": "ask",
        },
        present,
        actor,  # an admin in this fixture
        requester_account_id=actor.account_id,
        run_id=None,
    )
    frozen = await frozen_shared_logins(deps, source)
    assert frozen == {"env:OWNER_KEY", "caller"}
    # A shared login the agent no longer has is not frozen.
    fewer = source.model_copy(
        update={
            "resource_requirements": tuple(
                binding
                for binding in source.resource_requirements
                if not binding.id.endswith(":OWNER_KEY")
            )
        }
    )
    assert await frozen_shared_logins(deps, fewer) == {"caller"}


async def test_every_left_out_login_is_named_with_its_reason(owned):
    from types import SimpleNamespace

    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.runtime_manifest import frozen_logins

    service, agent, actor, sessions = owned
    source, stored, _, _ = await source_with_every_login(owned)
    deps = SimpleNamespace(sessionmaker=sessions)
    async with sessions() as db:
        present = await fleet_logins(db, source)
    await service.set_credential_sharing(
        agent,
        {"env:OWNER_KEY": "share", f"agent-mcp:{stored.id}": "skip"},
        present,
        actor,
        requester_account_id=actor.account_id,
        run_id=None,
    )
    shared, without = await frozen_logins(deps, source)
    assert shared == {"env:OWNER_KEY"}  # frozen, not handed over while sharing isn't live
    assert not runtime_manifest.LOGIN_SHARING_LIVE
    assert set(without) == {
        "linear (skipped)",
        "your own connections (not shared)",
        "the stored key OWNER_KEY (sharing with workers isn't available yet)",
        "the stored key ADMIN_KEY (not shared)",
        "a stored login (can't be shared)",
        "the repository pymc-labs/app (not shared)",
    }
