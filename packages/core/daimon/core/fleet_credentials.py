"""What logins a fleet's workers would run without, and who may share each one.

A worker ceiling leaves out everything with its own login (#1357). Before that loss
is silent, the requester is asked per login: share it with the workers, skip it, or
ask each time. This module only describes the logins and decides who may answer;
it never reads a secret value. Labels are what a person already sees: a connector's
server name, a repository's name, a stored key's NAME.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from daimon.core.stores.domain import BackendAgent
    from mux.contracts.ids import Provider
    from mux.contracts.resources import ResourceBinding
    from sqlalchemy.ext.asyncio import AsyncSession

LoginKind = Literal["agent_mcp", "caller_mcp", "github_app", "env", "repository", "unknown"]
Installer = Literal["owner", "admin", "caller", "unknown"]
Choice = Literal["share", "skip", "ask"]
CHOICES: tuple[Choice, ...] = ("share", "skip", "ask")

#: Only a native Anthropic worker session can receive a shared login today
#: (its own vault and .env). OpenAI/Gemini workers run without connectors.
SHAREABLE_PROVIDERS: frozenset[str] = frozenset({"anthropic"})


@dataclass(frozen=True)
class ConnectorLogin:
    """One login a worker would otherwise run without."""

    key: str
    kind: LoginKind
    label: str
    installed_by: Installer
    account_label: str | None = None

    @property
    def shareable(self) -> bool:
        """An unknown kind is never shareable (fail closed); it is listed as skipped."""
        return self.kind != "unknown"


def may_share(
    login: ConnectorLogin,
    *,
    actor_account_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    is_admin: bool,
    is_owner: bool,
) -> bool:
    """Whether this person may give the workers this login.

    Only the requester answers. Their own connections are theirs to share. A login
    the agent's owner set is the owner's (or an admin's) to share. Anything an admin
    installed, or whose installer is unknown, needs an admin.
    """
    if not login.shareable or actor_account_id != requester_account_id:
        return False
    if login.installed_by == "caller":
        return True
    if login.installed_by == "owner":
        return is_owner or is_admin
    return is_admin


def shareable_with(login: ConnectorLogin, provider: Provider) -> bool:
    """Whether a worker on this provider can receive this login at all."""
    return login.shareable and provider in SHAREABLE_PROVIDERS


def login_key_of(binding: ResourceBinding) -> str | None:
    """The login key a projected credential binding carries, as `fleet_logins` names it.

    None for anything that isn't one of the known login kinds (never shareable).
    """
    reference = binding.credential_ref
    if binding.kind != "credential" or reference is None or binding.id != reference:
        return None
    if binding.native is not None or binding.resource is not None:
        return None
    if reference.startswith("agent-mcp:") and binding.target_path is None:
        return reference
    if reference.startswith("caller-mcp:") and binding.target_path is None:
        return "caller"
    if reference.startswith("agent-env:") and binding.target_path is not None:
        return "env:" + reference.rsplit(":", 1)[-1]
    return None


def _repository_name(url: str) -> str:
    tail = url.rstrip("/").removesuffix(".git").split("/")
    return "/".join(tail[-2:]) if len(tail) >= 2 else url


async def fleet_logins(session: AsyncSession, source: BackendAgent) -> tuple[ConnectorLogin, ...]:
    """Every login in this captured source that a worker ceiling leaves out, labelled.

    Read-only, and never decodes a value: stored connector logins are matched to
    their server by URL, stored keys are named, repositories by their path.
    """
    from daimon.core.mcp_server_url import canonical_mcp_url
    from daimon.core.stores import agent_owners
    from daimon.core.stores.fleet_logins import mcp_credential_url, stored_key_setters

    owner = await agent_owners.owner_of(session, source.tenant_id, source.agent_id)
    spec = source.effective_spec
    servers = (
        {canonical_mcp_url(server.url): server.name for server in (spec.mcp_servers or ())}
        if spec is not None
        else {}
    )
    logins: dict[str, ConnectorLogin] = {}

    def add(login: ConnectorLogin) -> None:
        logins.setdefault(login.key, login)

    for binding in source.resource_requirements:
        reference = binding.credential_ref
        if binding.kind != "credential" or reference is None:
            continue
        if reference.startswith("agent-mcp:"):
            try:
                credential_id = uuid.UUID(reference.removeprefix("agent-mcp:"))
            except ValueError:
                add(ConnectorLogin(reference, "unknown", "a stored login", "unknown"))
                continue
            url = await mcp_credential_url(
                session,
                tenant_id=source.tenant_id,
                agent_id=source.agent_id,
                credential_id=credential_id,
            )
            name = servers.get(canonical_mcp_url(url)) if url is not None else None
            # No record of who stored it: an admin's to share (fail closed).
            add(ConnectorLogin(reference, "agent_mcp", name or "a connected tool", "unknown"))
        elif reference.startswith("caller-mcp:"):
            add(ConnectorLogin("caller", "caller_mcp", "your own connections", "caller"))
        elif reference.startswith("agent-env:") and binding.target_path is not None:
            key = reference.rsplit(":", 1)[-1]
            setters = await stored_key_setters(
                session, tenant_id=source.tenant_id, agent_id=source.agent_id, key=key
            )
            by_owner = (
                owner is not None
                and setters is not None
                and setters[0] == owner
                and setters[1] in (None, owner)
            )
            add(
                ConnectorLogin(
                    f"env:{key}",
                    "env",
                    f"the stored key {key}",
                    "owner" if by_owner else "unknown",
                )
            )
        else:
            add(ConnectorLogin(reference, "unknown", "a stored login", "unknown"))
    for server in (spec.mcp_servers or ()) if spec is not None else ():
        if server.credential_ref and server.credential_ref not in logins:
            add(ConnectorLogin(server.credential_ref, "agent_mcp", server.name, "unknown"))
    for repository in source.repository_sources:
        if repository.credential_ref is not None:
            name = _repository_name(repository.repository_url or repository.target_path)
            add(ConnectorLogin(f"repo:{name}", "repository", f"the repository {name}", "unknown"))
    from daimon.core.stores.github_access import list_live_grant_repositories

    repositories = await list_live_grant_repositories(
        session, tenant_id=source.tenant_id, agent_id=source.agent_id
    )
    if repositories:
        count = len(repositories)
        add(
            ConnectorLogin(
                "github_app",
                "github_app",
                f"GitHub ({count} {'repository' if count == 1 else 'repositories'})",
                "admin",
            )
        )
    return tuple(logins.values())


def card_line(logins: tuple[ConnectorLogin, ...]) -> str:
    """The question's lead line, with labels and account labels only."""
    names = [
        login.label + (f" (signed in as {login.account_label})" if login.account_label else "")
        for login in logins
        if login.shareable
    ]
    return "This agent uses " + ", ".join(names) + ". Give the workers access?" if names else ""


def run_without_line(login: ConnectorLogin, choice: str | None, *, sharing_live: bool) -> str:
    """One login the workers run without, by name, with the plain reason."""
    if not login.shareable:
        return f"{login.label} (can't be shared)"
    if choice == "skip":
        return f"{login.label} (skipped)"
    if choice == "share" and not sharing_live:
        return f"{login.label} (sharing with workers isn't available yet)"
    return f"{login.label} (not shared)"
