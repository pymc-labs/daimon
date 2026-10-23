"""What logins a fleet's workers would run without, and who may share each one.

A worker ceiling leaves out everything with its own login (#1357). Before that loss
is silent, the requester is asked per login: share it with the workers, skip it, or
ask each time. This module describes the logins, decides who may answer, and reads
saved choices back fail closed; it never reads a secret value. Labels are what a
person already sees: a connector's server name, a repository's name, a stored key's
NAME.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from mux.profiles import MANAGED_AGENTS

if TYPE_CHECKING:
    from daimon.core.stores.domain import BackendAgent
    from mux.contracts.ids import Provider
    from mux.contracts.resources import ResourceBinding
    from pydantic import JsonValue
    from sqlalchemy.ext.asyncio import AsyncSession

LoginKind = Literal["agent_mcp", "caller_mcp", "github_app", "env", "repository", "unknown"]
Installer = Literal["owner", "admin", "caller", "unknown"]
Choice = Literal["share", "skip", "ask"]
SaverRole = Literal["admin", "owner"]
CHOICES: tuple[Choice, ...] = ("share", "skip", "ask")
#: Stored keys that are GitHub's own credentials: never handed to a worker (GitHub
#: access goes through the app, never a copied token).
UNSHAREABLE_KEYS: frozenset[str] = frozenset({"env:GITHUB_TOKEN", "env:GH_TOKEN"})
#: A stored login whose server isn't on the agent any more: never shareable.
UNMATCHED_CONNECTOR = "a connected tool"

#: Only a native worker session can receive a shared login: in v1 its .env only
#: (connector logins through a per-worker vault are A-3).
#: OpenAI/Gemini workers run without connectors.
SHAREABLE_PROVIDERS: frozenset[str] = frozenset({MANAGED_AGENTS.provider})


@dataclass(frozen=True)
class ConnectorLogin:
    """One login a worker would otherwise run without.

    `version` changes whenever the stored row behind it is replaced, so a share saved
    for one version never carries over to another.
    """

    key: str
    kind: LoginKind
    label: str
    installed_by: Installer
    account_label: str | None = None
    version: str | None = None
    # The agent's connector this login belongs to (matched by URL), if any.
    server: str | None = None

    @property
    def shareable(self) -> bool:
        """What a worker can be handed in v1: stored keys (not GitHub's own tokens).

        Unknown kinds never (fail closed). Stored connector logins need native-worker
        MCP admission (A-3); the requester's own connections live in the vault that
        also carries Daimon's own credential; GitHub access needs its repo helpers
        and identity. All of those read "can't be shared with workers yet".
        """
        return self.kind == "env" and self.key not in UNSHAREABLE_KEYS

    @property
    def savable(self) -> bool:
        """Whether a choice for it may be stored on the agent at all.

        Never the requester's own connections: they are a person's, and the agent's
        config is shared by everyone who uses it.
        """
        return self.kind != "caller_mcp"


@dataclass(frozen=True)
class SavedChoice:
    """A stored per-login choice, with who saved it, in what role, when, for what row."""

    choice: Choice
    by: uuid.UUID
    role: SaverRole
    at: str
    version: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "choice": self.choice,
            "by": str(self.by),
            "role": self.role,
            "at": self.at,
            "version": self.version,
        }


def parse_sharing(raw: object) -> dict[str, SavedChoice]:
    """Read a stored sharing map; anything malformed reads as no choice ("ask")."""
    import structlog

    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, SavedChoice] = {}
    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
        try:
            assert isinstance(key, str) and isinstance(value, dict)
            choice, role = value["choice"], value["role"]  # pyright: ignore[reportUnknownVariableType]
            assert choice in CHOICES and role in ("admin", "owner")
            version = value.get("version")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            assert version is None or isinstance(version, str)
            parsed[key] = SavedChoice(
                choice=choice,
                by=uuid.UUID(str(value["by"])),  # pyright: ignore[reportUnknownArgumentType]
                role=role,
                at=str(value["at"]),  # pyright: ignore[reportUnknownArgumentType]
                version=version,
            )
        except (AssertionError, KeyError, ValueError, TypeError):
            structlog.get_logger(__name__).warning("fleet.sharing_entry_unreadable")
    return parsed


def may_share(
    login: ConnectorLogin,
    *,
    actor_account_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    is_admin: bool,
    is_owner: bool,
) -> bool:
    """Whether this person may give the workers this login.

    Only the requester answers. A login the agent's owner set (and nobody else since)
    is the owner's or an admin's to share. Anything an admin installed, or whose
    installer is unknown, needs an admin.
    """
    if not login.shareable or actor_account_id != requester_account_id:
        return False
    if login.installed_by == "owner":
        return is_owner or is_admin
    return is_admin


def still_shared(
    login: ConnectorLogin,
    saved: SavedChoice | None,
    *,
    owner: uuid.UUID | None,
    admin_now: Callable[[uuid.UUID], bool],
) -> bool:
    """Whether a saved "share" still holds for this login as it is NOW.

    Re-checked at every freeze: a stored row that has a version, the same version it
    was saved for, and the saver may still share it under the current installer
    classification. An admin's share needs that person to be an admin NOW
    (`admin_now`); an owner's needs them to still be the owner.
    """
    if saved is None or saved.choice != "share" or not login.shareable:
        return False
    if not login.version or saved.version != login.version:
        return False
    is_admin = saved.role == "admin" and admin_now(saved.by)
    return may_share(
        login,
        actor_account_id=saved.by,
        requester_account_id=saved.by,
        is_admin=is_admin,
        is_owner=saved.role == "owner" and owner == saved.by,
    )


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
    """owner/repo from a repository URL, never its userinfo, query or fragment."""
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        path = parts.path
    else:
        # scp-style (user@host:owner/repo) or a bare path: no userinfo, query or fragment.
        path = url.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1].replace(":", "/")
    tail = path.rstrip("/").removesuffix(".git").split("/")
    tail = [part for part in tail if part]
    return "/".join(tail[-2:]) if len(tail) >= 2 else (tail[-1] if tail else "a repository")


def _version(*parts: object) -> str:
    return ":".join(part.isoformat() if isinstance(part, datetime) else str(part) for part in parts)


def stored_key_version(last_set_by: uuid.UUID | None, updated_at: datetime) -> str:
    """The version a stored key's share is pinned to: who last set it, and when."""
    return _version(last_set_by, updated_at)


async def fleet_logins(session: AsyncSession, source: BackendAgent) -> tuple[ConnectorLogin, ...]:
    """Every login in this captured source that a worker ceiling leaves out, labelled.

    Read-only, and never decodes a value: stored connector logins are matched to
    their server by URL, stored keys are named, repositories by their path.
    """
    from daimon.core.mcp_server_url import canonical_mcp_url
    from daimon.core.stores import agent_owners
    from daimon.core.stores.fleet_logins import mcp_credential_url, stored_key_setters
    from daimon.core.stores.github_access import list_live_grant_repositories

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
            stored = await mcp_credential_url(
                session,
                tenant_id=source.tenant_id,
                agent_id=source.agent_id,
                credential_id=credential_id,
            )
            name = servers.get(canonical_mcp_url(stored[0])) if stored is not None else None
            # No record of who stored it: an admin's to share (fail closed).
            add(
                ConnectorLogin(
                    reference,
                    "agent_mcp",
                    name or UNMATCHED_CONNECTOR,
                    "unknown",
                    version=_version(stored[1]) if stored is not None else None,
                    server=name,
                )
            )
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
                and setters[1] == owner
            )
            add(
                ConnectorLogin(
                    f"env:{key}",
                    "env",
                    f"the stored key {key}",
                    "owner" if by_owner else "unknown",
                    version=stored_key_version(setters[1], setters[2])
                    if setters is not None
                    else None,
                )
            )
        else:
            add(ConnectorLogin(reference, "unknown", "a stored login", "unknown"))
    for server in (spec.mcp_servers or ()) if spec is not None else ():
        if server.credential_ref and server.credential_ref not in logins:
            add(ConnectorLogin(server.credential_ref, "agent_mcp", server.name, "unknown"))
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
    else:
        # With GitHub app access the logged-in repositories are its; listed once there.
        for repository in source.repository_sources:
            if repository.credential_ref is not None:
                name = _repository_name(repository.repository_url or repository.target_path)
                add(
                    ConnectorLogin(
                        f"repo:{name}", "repository", f"the repository {name}", "unknown"
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
        return (
            f"{login.label} (can't be shared)"
            if login.kind == "unknown"
            else f"{login.label} (can't be shared with workers yet)"
        )
    if choice == "skip":
        return f"{login.label} (skipped)"
    if choice == "reconfirm":
        return f"{login.label} (needs re-confirming)"
    if choice == "share" and not sharing_live:
        return f"{login.label} (sharing with workers isn't available yet)"
    return f"{login.label} (not shared)"
