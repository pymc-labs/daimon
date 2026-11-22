"""Links from chat to pages of the web app. Pure but `links_at`, which reads one feature.

The route table mirrors `web/src/lib/deep-links.json`, which the web app builds its own
links from; a test keeps the two equal. A link is
`{public_url}/w/{workspace}{path}`, so the web app opens it in the right workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal, NamedTuple
from urllib.parse import quote

import structlog
from anthropic import AsyncAnthropic
from daimon.core.anthropic_guard import anthropic_configured, anthropic_disabled
from daimon.core.config import Settings, WebModuleKey, WebSettings
from daimon.core.defaults.owned import resolve_owned_agent
from daimon.core.features.snapshot import FeaturePlace, Sessions, place_feature_or_none
from daimon.core.ma_resolver import ResolverCache, find_agent_id_cached
from daimon.core.mux_backend import resource_scope
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.scoped_config_read import resolve
from mux.errors import ProviderError

log = structlog.get_logger(__name__)

OPEN_IN_WEB: Final = "Open in web"
"""The label of every panel button that opens the web app, on every platform."""

LinkKey = Literal[
    "agent",
    "agents",
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
    "agents": DeepLink("/agents", None, "agents", "agents.view"),
    "channel": DeepLink("/channels", "channel", "channels", "channels.view"),
    "routine": DeepLink("/routines", "routine", "routines", "routines.view"),
    "skill": DeepLink("/agents/skills/{id}", None, "agents", "workspace.manage"),
    "environment": DeepLink("/agents/environments", "env", "agents", "workspace.manage"),
    "person": DeepLink("/admin/people", "person", "admin", "people.manage"),
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

    def _path(
        self, key: LinkKey, segment: str | None = None, value: str | None = None
    ) -> str | None:
        link = DEEP_LINKS[key]
        if link.module not in self.modules:
            return None
        if not self.admin and link.cap not in MEMBER_CAPS:
            return None
        path = link.path.replace("{id}", _quote(segment)) if segment is not None else link.path
        if link.param is not None and value:
            path += f"?{link.param}={_quote(value)}"
        return path

    def _url(
        self, key: LinkKey, segment: str | None = None, value: str | None = None
    ) -> str | None:
        path = self._path(key, segment, value)
        return None if path is None else f"{self.root()}{path}"

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
        """The people page, with no person opened."""
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
        """Page key to path under `root()` for the pages this caller may open: the list
        pages of the modules that are on, plus the profile every signed-in person has."""
        found = {
            "chat": self._path("chat"),
            "agents": self._path("agents"),
            "channels": self._path("channel"),
            "routines": self._path("routine"),
            "spend": self._path("spend"),
            "integrations": self._path("integrations"),
            "github": self._path("github"),
            "admin": self._path("person"),
        }
        pages = {key: path for key, path in found.items() if path is not None}
        pages["profile"] = "/profile"
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


class Assignment(NamedTuple):
    """What names the agent assigned to a channel: the deployment default and the runtime's
    Managed Agents client and resolver cache."""

    default: DeploymentDefault
    anthropic: AsyncAnthropic
    cache: ResolverCache


async def _assigned_agent(
    sessions: Sessions, place: FeaturePlace, assignment: Assignment
) -> str | None:
    """The id (as the agent tier keys it) of the agent assigned to `place`'s channel: the
    channel's, else the workspace's, else the deployment default. None when none is live. A
    thread bound to another agent still follows its channel's assignment."""
    async with sessions() as session:
        context = ScopeContext(tenant_id=place.tenant_id, channel_id=place.channel_id)
        name = (await resolve(session, context=context, default=assignment.default)).agent_name
    if name is None:
        return None
    try:
        scope = resource_scope(tenant_id=str(place.tenant_id))
        return (await resolve_owned_agent(sessions, scope, name)).ref.id
    except ProviderError as miss:
        if miss.category != "not_found":
            raise
    if anthropic_disabled() or not anthropic_configured(assignment.anthropic):
        return None
    return await find_agent_id_cached(
        assignment.anthropic, assignment.cache, tenant_id=place.tenant_id, name=name
    )


async def aware_at(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    assignment: Assignment | None = None,
) -> bool:
    """Whether `web_awareness` is On at `place`; False when it is Off or unreadable. A place
    with a channel but no agent takes the agent assigned there, given `assignment`."""
    if place.agent_id is None and place.channel_id is not None and assignment is not None:
        # A card in a channel follows the agent assigned there (#1775).
        try:
            place = replace(place, agent_id=await _assigned_agent(sessions, place, assignment))
        except Exception:  # A failed lookup offers no link, as an unreadable feature does.
            log.warning("web_links.assigned_agent_failed", exc_info=True)
            return False
    found = await place_feature_or_none(sessions, settings, place, "web_awareness")
    return found is not None and found.on


async def links_at(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    links: TenantLinks | None,
    assignment: Assignment | None = None,
) -> TenantLinks | None:
    """`links` where `web_awareness` is On at `place` (`aware_at`), else None. Every path that
    tells the agent about the web app or puts a link to it on a card asks this (a turn's
    controls ask its snapshot in `render_turn_origin`)."""
    if links is None:
        return None
    return links if await aware_at(sessions, settings, place, assignment) else None
