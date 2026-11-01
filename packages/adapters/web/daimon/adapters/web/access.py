"""Roles, modules and capabilities: pure rules, no I/O."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Literal

from daimon.core.config import WebModuleKey
from daimon.core.stores.domain import WebPersonKind, WebRoleGrant
from daimon.core.web_identity import WebRoleName

Role = WebRoleName
Capability = Literal[
    "chat.use",
    "agents.view",
    "agents.edit",
    "agents.create",
    "credits.view_own",
    "credits.manage",
    "people.manage",
    "audit.view",
    "workspace.manage",
]

_ALL: tuple[Capability, ...] = (
    "chat.use",
    "agents.view",
    "agents.edit",
    "agents.create",
    "credits.view_own",
    "credits.manage",
    "people.manage",
    "audit.view",
    "workspace.manage",
)
_BY_ROLE: dict[Role, frozenset[Capability]] = {
    "owner": frozenset(_ALL),
    "admin": frozenset(_ALL),
    "member": frozenset(
        {"chat.use", "agents.view", "agents.edit", "agents.create", "credits.view_own"}
    ),
    "visitor": frozenset({"chat.use", "credits.view_own"}),
}
# Each capability lives in one module; turning the module off removes it.
_MODULE_OF: dict[Capability, WebModuleKey] = {
    "chat.use": "chat",
    "agents.view": "agents",
    "agents.edit": "agents",
    "agents.create": "agents",
    "credits.view_own": "credits",
    "credits.manage": "credits",
    "people.manage": "admin",
    "audit.view": "admin",
    "workspace.manage": "admin",
}
_MODULES_BY_ROLE: dict[Role, frozenset[WebModuleKey]] = {
    "owner": frozenset({"chat", "agents", "credits", "admin"}),
    "admin": frozenset({"chat", "agents", "credits", "admin"}),
    "member": frozenset({"chat", "agents", "credits"}),
    "visitor": frozenset({"chat"}),
}
_MODULE_ORDER: tuple[WebModuleKey, ...] = ("chat", "agents", "credits", "admin")


def role_for(
    *,
    kind: WebPersonKind,
    workspace_grant: WebRoleGrant | None,
    platform_admin: bool,
    chat_admin: bool = False,
) -> Role:
    """The highest role the person holds. A visitor never rises above visitor.

    A visitor reaches only the agents they were invited to, so no grant or platform admin
    lifts them: promoting one is an admin turning them into a member.
    `chat_admin` is an admin of the chat workspace itself, as its member list says.
    """
    if kind == "visitor":
        return "visitor"
    if workspace_grant == "owner":
        return "owner"
    if workspace_grant == "admin" or platform_admin or chat_admin:
        return "admin"
    return "member"


def is_admin(role: Role) -> bool:
    """Whether the role is the workspace's owner or an admin."""
    return role in ("owner", "admin")


_RANK: dict[Role, int] = {"visitor": 0, "member": 1, "admin": 2, "owner": 3}


def at_or_above(role: Role, other: Role) -> bool:
    """Whether `role` ranks the same as `other` or higher."""
    return _RANK[role] >= _RANK[other]


def modules_for(role: Role, enabled: Iterable[WebModuleKey]) -> list[WebModuleKey]:
    """The sections this role sees, among those the deployment turned on."""
    allowed = _MODULES_BY_ROLE[role] & set(enabled)
    return [m for m in _MODULE_ORDER if m in allowed]


def capabilities_for(role: Role, modules: Collection[WebModuleKey]) -> frozenset[Capability]:
    """What the role may do, limited to the modules that are on."""
    return frozenset(c for c in _BY_ROLE[role] if _MODULE_OF[c] in modules)
