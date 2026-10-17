"""The installation pin an agent's grants imply, and its agreement with `stage_grant`."""

from __future__ import annotations

import uuid
from typing import Literal

import pytest
from daimon.core._models import AgentGitHubGrant, Tenant, TenantGitHubRepo
from daimon.core.stores import github_access, github_app_installations
from sqlalchemy.ext.asyncio import AsyncSession

_Status = Literal["active", "suspended", "revoked"]


async def _tenant(session: AsyncSession) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await session.flush()
    for installation_id, login in ((71, "first"), (72, "second")):
        await github_app_installations.upsert(
            session,
            installation_id=installation_id,
            account_login=login,
            repo_full_names=[f"{login}/one", f"{login}/two"],
        )
    return tenant_id


def _repo(
    tenant_id: uuid.UUID,
    repo_id: int,
    installation_id: int,
    name: str,
    *,
    scope: uuid.UUID | None = None,
    status: _Status = "active",
) -> TenantGitHubRepo:
    return TenantGitHubRepo(
        tenant_id=tenant_id,
        repo_id=repo_id,
        scope_agent_id=scope,
        owner_id=installation_id,
        installation_id=installation_id,
        repo_full_name=name,
        max_access="write",
        authorized_by_github_user_id=2,
        status=status,
        version=1,
    )


def _grant(tenant_id: uuid.UUID, agent_id: uuid.UUID, repo_id: int) -> AgentGitHubGrant:
    return AgentGitHubGrant(
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=repo_id,
        baseline_access="write",
        ceiling_access="write",
        version=1,
    )


async def _pin(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    excluding_repo_id: int | None = None,
) -> github_access.AgentInstallationPin:
    return await github_access.agent_installation_pin(
        session, tenant_id=tenant_id, agent_id=agent_id, excluding_repo_id=excluding_repo_id
    )


@pytest.mark.asyncio
async def test_pin_is_empty_single_or_multiple(db_session: AsyncSession) -> None:
    tenant_id, agent_id = await _tenant(db_session), uuid.uuid4()
    db_session.add_all(
        [
            _repo(tenant_id, 101, 71, "first/one", scope=agent_id),
            _repo(tenant_id, 102, 71, "first/two", scope=agent_id),
            _repo(tenant_id, 201, 72, "second/one", scope=agent_id),
        ]
    )
    await db_session.flush()

    empty = await _pin(db_session, tenant_id, agent_id)
    assert empty.installation_ids == frozenset() and empty.single is None
    assert not empty.is_multiple

    db_session.add_all([_grant(tenant_id, agent_id, 101), _grant(tenant_id, agent_id, 102)])
    await db_session.flush()
    one = await _pin(db_session, tenant_id, agent_id)
    assert one.installation_ids == {71} and one.single == 71 and one.owners == {71: "first"}

    db_session.add(_grant(tenant_id, agent_id, 201))
    await db_session.flush()
    several = await _pin(db_session, tenant_id, agent_id)
    assert several.installation_ids == {71, 72} and several.single is None
    assert several.is_multiple and several.owners == {71: "first", 72: "second"}
    assert (await _pin(db_session, tenant_id, agent_id, excluding_repo_id=201)).single == 71


@pytest.mark.asyncio
async def test_pin_ignores_other_agents_hidden_and_dangling_grants(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id, other_id = await _tenant(db_session), uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [
            _repo(tenant_id, 101, 71, "first/one", scope=agent_id),
            # Only another agent's own confirmation exists for this repo.
            _repo(tenant_id, 201, 72, "second/one", scope=other_id),
            _repo(tenant_id, 202, 72, "second/two", scope=agent_id),
            _grant(tenant_id, agent_id, 101),
            _grant(tenant_id, agent_id, 201),
            _grant(tenant_id, agent_id, 999),  # no repo row at all
        ]
    )
    hidden = _grant(tenant_id, agent_id, 202)
    hidden.app_visible = False
    db_session.add(hidden)
    await db_session.flush()

    assert (await _pin(db_session, tenant_id, agent_id)).installation_ids == {71}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "suspended", "revoked"])
@pytest.mark.parametrize("shared", [False, True])
async def test_pin_counts_grants_like_stage_grant_does(
    db_session: AsyncSession, status: _Status, shared: bool
) -> None:
    """A revoked or server-wide repo pins the agent, and `stage_grant` agrees."""
    tenant_id, agent_id = await _tenant(db_session), uuid.uuid4()
    scope = None if shared else agent_id
    db_session.add_all(
        [
            _repo(tenant_id, 101, 71, "first/one", scope=scope, status=status),
            _grant(tenant_id, agent_id, 101),
            _repo(tenant_id, 102, 71, "first/two", scope=agent_id),
            _repo(tenant_id, 201, 72, "second/one", scope=agent_id),
        ]
    )
    await db_session.flush()

    for candidate, installation_id in ((102, 71), (201, 72)):
        others = await _pin(db_session, tenant_id, agent_id, excluding_repo_id=candidate)
        pin_refuses = bool(others.installation_ids - {installation_id})
        refused = False
        try:
            async with db_session.begin_nested():
                await github_access.stage_grant(
                    db_session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=candidate,
                    baseline_access="write",
                    ceiling_access="write",
                    granted_by_account_id=None,
                )
        except github_access.SecondInstallationError:
            refused = True
        assert refused is pin_refuses
        assert pin_refuses is (installation_id == 72)


@pytest.mark.asyncio
async def test_legacy_multi_installation_agent_can_be_cleaned_up_by_name(
    db_session: AsyncSession,
) -> None:
    """Chat's `remove_repo` finds and removes repos by name whatever installation holds them."""
    tenant_id, agent_id = await _tenant(db_session), uuid.uuid4()
    db_session.add_all(
        [
            _repo(tenant_id, 101, 71, "first/one", scope=agent_id),
            _repo(tenant_id, 201, 72, "second/one", scope=agent_id),
            _grant(tenant_id, agent_id, 101),
            _grant(tenant_id, agent_id, 201),
        ]
    )
    await db_session.flush()
    assert (await _pin(db_session, tenant_id, agent_id)).is_multiple

    repo_id = await github_access.repo_id_for_agent_removal(
        db_session, tenant_id=tenant_id, agent_id=agent_id, repo_name="Second/One"
    )
    assert repo_id == 201
    assert await github_access.remove_agent_repo(
        db_session, tenant_id=tenant_id, agent_id=agent_id, repo_id=201, account_id=None
    )
    assert (await _pin(db_session, tenant_id, agent_id)).single == 71
