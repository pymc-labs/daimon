"""Links from chat to pages of the web app. Pure: no I/O, no clock.

The route table mirrors `web/src/lib/deep-links.json`, which the web app builds its own
links from; a test keeps the two equal. A link is
`{public_url}/w/{workspace}{path}`, so the web app opens it in the right workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, NamedTuple
from urllib.parse import quote

from daimon.core.config import WebModuleKey, WebSettings

OPEN_IN_WEB: Final = "Open in web"
"""The label of every panel button that opens the web app, on every platform."""

LinkKey = Literal[
    "agent",
    "channel",
    "routine",
    "skill",
    "environment",
    "person",
    "invites",
    "audit",
    "feedback",
    "credits",
    "spend",
    "allowance",
    "mcp_server",
    "integrations",
    "github",
    "chat",
]


class DeepLink(NamedTuple):
    """One web page: its path (`{id}` is a path segment), the query parameter that opens
    an item or tab on it, the module it belongs to and the capability it needs."""

    path: str
    param: str | None
    module: WebModuleKey
    cap: str


DEEP_LINKS: Final[Mapping[LinkKey, DeepLink]] = {
    "agent": DeepLink("/agents/{id}", "node", "agents", "agents.view"),
    "channel": DeepLink("/channels", "channel", "channels", "channels.view"),
    "routine": DeepLink("/routines", "routine", "routines", "routines.view"),
    "skill": DeepLink("/agents/skills/{id}", None, "agents", "workspace.manage"),
    "environment": DeepLink("/agents/environments", "env", "agents", "workspace.manage"),
    "person": DeepLink("/admin", "person", "admin", "people.manage"),
    "invites": DeepLink("/admin/invites", "invite", "admin", "people.manage"),
    "audit": DeepLink("/admin/audit", None, "admin", "audit.view"),
    "feedback": DeepLink("/admin/feedback", None, "admin", "audit.view"),
    "credits": DeepLink("/credits", None, "credits", "credits.view_own"),
    "spend": DeepLink("/credits/spend", None, "credits", "credits.view_own"),
    "allowance": DeepLink("/credits/allowances", "allowance", "credits", "credits.manage"),
    "mcp_server": DeepLink("/integrations/mcp", "server", "integrations", "integrations.view"),
    "integrations": DeepLink("/integrations", None, "integrations", "integrations.view"),
    "github": DeepLink("/integrations/github", None, "integrations", "integrations.view"),
    "chat": DeepLink("/chat", "agent", "chat", "chat.use"),
}

# What a plain member may open; an owner or admin may open every page. The web app's own
# role table (`daimon.adapters.web.access`) is the source: a test keeps this equal to it.
MEMBER_CAPS: Final[frozenset[str]] = frozenset(
    {
        "chat.use",
        "agents.view",
        "channels.view",
        "routines.view",
        "credits.view_own",
    }
)


def _quote(value: str) -> str:
    return quote(value, safe="")


@dataclass(frozen=True)
class TenantLinks:
    """Links into one workspace, limited to the pages `admin` may open and the modules on.

    Every method returns None when the page is off or the person could not open it, so a
    caller leaves the link out instead of offering one that bounces.
    """

    base: str
    tenant_id: uuid.UUID
    modules: frozenset[WebModuleKey]
    admin: bool

    def root(self) -> str:
        """The workspace's front door, `{public_url}/w/{workspace}`."""
        return f"{self.base}/w/{self.tenant_id}"

    def _url(
        self, key: LinkKey, segment: str | None = None, value: str | None = None
    ) -> str | None:
        link = DEEP_LINKS[key]
        if link.module not in self.modules:
            return None
        if not self.admin and link.cap not in MEMBER_CAPS:
            return None
        path = link.path.replace("{id}", _quote(segment)) if segment is not None else link.path
        url = f"{self.root()}{path}"
        if link.param is not None and value:
            url += f"?{link.param}={_quote(value)}"
        return url

    def agent(self, agent_id: str, node: str | None = None) -> str | None:
        """An agent's editor; `node` opens one part of it (`instructions`, `skill:<id>`)."""
        return self._url("agent", agent_id, node)

    def channel(self, channel_id: str | None = None) -> str | None:
        """A channel's settings; the id is encoded, as a Teams id holds `:` and `@`."""
        return self._url("channel", None, channel_id)

    def routine(self, routine_id: uuid.UUID | str | None = None) -> str | None:
        return self._url("routine", None, None if routine_id is None else str(routine_id))

    def skill(self, skill_id: str) -> str | None:
        return self._url("skill", skill_id)

    def environment(self, environment_id: str) -> str | None:
        return self._url("environment", None, environment_id)

    def person(self, person_id: str) -> str | None:
        return self._url("person", None, person_id)

    def person_list(self) -> str | None:
        """The admin page, with no person opened."""
        return self._url("person")

    def invites(self, invite_id: str | None = None) -> str | None:
        return self._url("invites", None, invite_id)

    def audit(self) -> str | None:
        return self._url("audit")

    def feedback(self) -> str | None:
        return self._url("feedback")

    def credits(self) -> str | None:
        """The credits page: the overview for an admin, a person's own use for a member."""
        return self._url("credits")

    def spend(self) -> str | None:
        return self._url("spend")

    def allowance(self, allowance_id: str | None = None) -> str | None:
        return self._url("allowance", None, allowance_id)

    def mcp_server(self, server_key: str | None = None) -> str | None:
        return self._url("mcp_server", None, server_key)

    def github(self) -> str | None:
        return self._url("github")

    def integrations(self) -> str | None:
        return self._url("integrations")

    def chat(self, agent_id: str | None = None) -> str | None:
        return self._url("chat", None, agent_id)

    def pages(self) -> dict[str, str]:
        """Page key to URL for the pages this caller may open: the list pages of the
        modules that are on, plus the profile every signed-in person has."""
        found = {
            "chat": self.chat(),
            "channels": self.channel(),
            "routines": self.routine(),
            "spend": self.spend(),
            "integrations": self.integrations(),
            "github": self.github(),
            "admin": self.person_list(),
        }
        pages = {key: url for key, url in found.items() if url is not None}
        pages["profile"] = f"{self.root()}/profile"
        return pages


@dataclass(frozen=True)
class WebLinks:
    """The web app's public address and which of its modules are on."""

    base: str
    modules: frozenset[WebModuleKey]

    @classmethod
    def from_settings(cls, web: WebSettings | None) -> WebLinks | None:
        """None for a deployment with no web app: nothing links anywhere."""
        if web is None:
            return None
        return cls(base=str(web.public_url).rstrip("/"), modules=frozenset(web.modules))

    def for_tenant(self, tenant_id: uuid.UUID, *, admin: bool) -> TenantLinks:
        """Links into `tenant_id`'s workspace for a caller who is an admin or not."""
        return TenantLinks(self.base, tenant_id, self.modules, admin)


def panel_links(
    web: WebSettings | None, tenant_id: uuid.UUID, *, admin: bool
) -> TenantLinks | None:
    """The links a chat panel may offer in `tenant_id`; None with no web app."""
    links = WebLinks.from_settings(web)
    return None if links is None else links.for_tenant(tenant_id, admin=admin)
