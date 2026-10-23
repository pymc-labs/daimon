"""Logins a fleet's workers would run without: labels, installers, who may share."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.fleet_credentials import (
    ConnectorLogin,
    _repository_name,  # pyright: ignore[reportPrivateUsage]
    card_line,
    fleet_logins,
    may_share,
    parse_sharing,
    shareable_with,
)
from daimon.core.stores import agent_mcp_credentials, agent_owners, security_audit
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import BackendAgent
from daimon.core.turn.runtime_manifest import frozen_logins
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ResourceBinding, WorkspaceSource

from .test_agent_backend import owned as owned
from .test_agent_owner_settings import people as people


def credential(reference: str, target: str | None = None) -> ResourceBinding:
    return ResourceBinding(
        id=reference, kind="credential", credential_ref=reference, target_path=target
    )


async def make_source(sessions, agent, owner_account_id):
    """The agent with every login kind; OWNER_KEY is the owner's, ADMIN_KEY nobody's."""
    async with sessions.begin() as db:
        await agent_owners.set_owner(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=owner_account_id,
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
            set_by_account_id=owner_account_id,
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
            credential(f"caller-mcp:{agent.tenant_id}:{owner_account_id}:{agent.agent_id}"),
            credential(f"{env}:OWNER_KEY", ".env"),
            credential(f"{env}:ADMIN_KEY", ".env"),
            credential("vault:something-new"),
        ),
        repository_sources=(
            WorkspaceSource(
                kind="repository",
                target_path="/workspace/app",
                repository_url="https://user:token@github.com/pymc-labs/app.git?ref=main",
                credential_ref="repo-login",
            ),
        ),
    )
    return source, stored


async def test_every_login_is_named_without_its_value(owned):
    _, agent, actor, sessions = owned
    source, stored = await make_source(sessions, agent, actor.account_id)
    async with sessions() as db:
        logins = await fleet_logins(db, source)
    by_key = {login.key: login for login in logins}
    assert by_key[f"agent-mcp:{stored.id}"].label == "linear"
    assert by_key[f"agent-mcp:{stored.id}"].installed_by == "unknown"
    assert by_key["caller"].installed_by == "caller"
    assert by_key["env:OWNER_KEY"].installed_by == "owner"
    assert by_key["env:ADMIN_KEY"].installed_by == "unknown"
    assert by_key["env:OWNER_KEY"].label == "the stored key OWNER_KEY"
    assert by_key["env:OWNER_KEY"].version and by_key[f"agent-mcp:{stored.id}"].version
    # The repo's name never carries its userinfo or query.
    assert by_key["repo:pymc-labs/app"].label == "the repository pymc-labs/app"
    assert "github_app" not in by_key  # no live GitHub grants on this agent
    assert not by_key["caller"].shareable and not by_key["caller"].savable
    assert not by_key["repo:pymc-labs/app"].shareable
    assert by_key["env:OWNER_KEY"].shareable and by_key[f"agent-mcp:{stored.id}"].shareable
    unknown = by_key["vault:something-new"]
    assert unknown.kind == "unknown" and not unknown.shareable
    dumped = json.dumps([login.__dict__ for login in logins])
    for secret in ("private-ciphertext", "owner-secret-value", "admin-secret-value", "token@"):
        assert secret not in dumped
    line = card_line(logins)
    assert "linear" in line and "OWNER_KEY" in line and "a stored login" not in line
    assert "secret" not in line


def test_repository_names_never_carry_credentials():
    assert _repository_name("https://u:p@github.com/o/r.git?x=1#y") == "o/r"
    assert _repository_name("git@github.com:o/r.git") == "o/r"
    assert "p@" not in _repository_name("https://u:p@host/o/r")


@pytest.mark.parametrize(
    ("installed_by", "requester", "admin", "owner", "allowed"),
    [
        ("owner", True, False, True, True),  # the owner's own stored login
        ("owner", False, True, True, False),  # only the requester answers
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


def test_a_malformed_stored_map_reads_as_no_choice():
    good = {"choice": "skip", "by": str(uuid.uuid4()), "role": "owner", "at": "t", "version": "v"}
    parsed = parse_sharing(
        {"a": good, "b": "share", "c": {"choice": "share"}, "d": {**good, "role": "member"}}
    )
    assert set(parsed) == {"a"}
    assert parse_sharing(["not", "a", "map"]) == {}
    assert parse_sharing(None) == {}


async def test_choices_are_saved_by_owner_or_admin_and_audited_by_key(people):
    service, agent, actors, sessions = people
    owner, admin, member = actors["owner"], actors["admin"], actors["member"]
    source, stored = await make_source(sessions, agent, owner.account_id)
    mcp = f"agent-mcp:{stored.id}"

    async def save(actor, choices, run=None):
        return await service.set_credential_sharing(
            agent,
            choices,
            source,
            actor,
            requester_account_id=actor.account_id,
            run_id=run,
        )

    saved = await save(owner, {"env:OWNER_KEY": "share", "env:ADMIN_KEY": "skip"})
    entries = parse_sharing(saved.fleet_credential_sharing)
    assert entries["env:OWNER_KEY"].choice == "share" and entries["env:OWNER_KEY"].role == "owner"
    assert entries["env:OWNER_KEY"].by == owner.account_id and entries["env:OWNER_KEY"].version
    # An owner can't share a login an admin (or someone unknown) stored.
    with pytest.raises(AgentBackendRefused, match="credential share not allowed"):
        await save(owner, {mcp: "share"})
    # The requester's own connections are never stored on the agent, whatever the choice.
    for choice in ("share", "skip", "ask"):
        with pytest.raises(AgentBackendRefused, match="invalid credential choice"):
            await save(owner, {"caller": choice})
    # An admin shares it; then the owner can't change the admin's choice, even to skip.
    run = uuid.uuid4()
    await save(admin, {mcp: "share"}, run)
    with pytest.raises(AgentBackendRefused, match="credential choice admin set"):
        await save(owner, {mcp: "skip"})
    # A plain member saves nothing.
    with pytest.raises(AgentBackendRefused):
        await save(member, {"env:ADMIN_KEY": "skip"})
    # A key that isn't one of this agent's logins is refused.
    with pytest.raises(AgentBackendRefused, match="invalid credential choice"):
        await save(admin, {"vault:other": "share"})
    async with sessions() as db:
        events = await security_audit.list_events(db, tenant_id=agent.tenant_id)
    reasons = {
        (event.operation, event.reason)
        for event in events
        if event.tool_name == "fleet_credentials"
    }
    assert reasons >= {
        ("share", "login=env:OWNER_KEY; role=owner"),
        ("skip", "login=env:ADMIN_KEY; role=owner"),
        ("share", f"login={mcp}; role=admin; run={run}"),
    }
    dumped = json.dumps([event.model_dump(mode="json") for event in events])
    assert "secret" not in dumped and "ciphertext" not in dumped


async def test_a_saved_choice_survives_other_agent_changes(people):
    service, agent, actors, sessions = people
    admin = actors["admin"]
    source, _ = await make_source(sessions, agent, actors["owner"].account_id)
    saved = await service.set_credential_sharing(
        agent,
        {"env:OWNER_KEY": "share"},
        source,
        admin,
        requester_account_id=admin.account_id,
        run_id=None,
    )
    switched = await service.set_backend(agent, "openai", saved.local, admin)
    assert parse_sharing(switched.fleet_credential_sharing)["env:OWNER_KEY"].choice == "share"


async def test_a_freeze_re_checks_every_saved_share_against_the_row_now(people):
    """An owner shares env:K; an admin replaces K; the next run freezes it NOT shared."""
    service, agent, actors, sessions = people
    owner, admin = actors["owner"], actors["admin"]
    source, stored = await make_source(sessions, agent, owner.account_id)
    deps = SimpleNamespace(sessionmaker=sessions)
    await service.set_credential_sharing(
        agent,
        {"env:OWNER_KEY": "share"},
        source,
        owner,
        requester_account_id=owner.account_id,
        run_id=None,
    )
    await service.set_credential_sharing(
        agent,
        {f"agent-mcp:{stored.id}": "share", "env:ADMIN_KEY": "ask"},
        source,
        admin,
        requester_account_id=admin.account_id,
        run_id=None,
    )
    decision = await frozen_logins(deps, source)
    assert decision.shared == {"env:OWNER_KEY", f"agent-mcp:{stored.id}"}
    assert decision.handed == frozenset()  # nothing reaches a worker while not live
    async with sessions.begin() as db:
        await put_agent_file(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            key="OWNER_KEY",
            content="replaced-by-admin",
            set_by_account_id=admin.account_id,
        )
    after = await frozen_logins(deps, source)
    assert after.shared == {f"agent-mcp:{stored.id}"}
    assert "the stored key OWNER_KEY (not shared)" in after.without
    # A share of a login the agent no longer has is not frozen either.
    fewer = source.model_copy(
        update={
            "resource_requirements": tuple(
                binding for binding in source.resource_requirements if "agent-mcp" not in binding.id
            )
        }
    )
    assert (await frozen_logins(deps, fewer)).shared == frozenset()


async def test_an_owner_key_needs_the_owner_as_its_last_setter(people):
    service, agent, actors, sessions = people
    owner = actors["owner"]
    source, _ = await make_source(sessions, agent, owner.account_id)
    async with sessions.begin() as db:
        # Created by the owner, then set again by nobody known.
        await put_agent_file(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            key="OWNER_KEY",
            content="rotated",
            set_by_account_id=None,
        )
    async with sessions() as db:
        logins = {login.key: login for login in await fleet_logins(db, source)}
    assert logins["env:OWNER_KEY"].installed_by == "unknown"


async def test_every_left_out_login_is_named_with_its_reason(people):
    service, agent, actors, sessions = people
    owner, admin = actors["owner"], actors["admin"]
    source, stored = await make_source(sessions, agent, owner.account_id)
    await service.set_credential_sharing(
        agent,
        {"env:OWNER_KEY": "share", f"agent-mcp:{stored.id}": "skip"},
        source,
        admin,
        requester_account_id=admin.account_id,
        run_id=None,
    )
    decision = await frozen_logins(SimpleNamespace(sessionmaker=sessions), source)
    assert set(decision.without) == {
        "linear (skipped)",
        "your own connections (can't be shared with workers yet)",
        "the stored key OWNER_KEY (sharing with workers isn't available yet)",
        "the stored key ADMIN_KEY (not shared)",
        "a stored login (can't be shared)",
        "the repository pymc-labs/app (can't be shared with workers yet)",
    }
