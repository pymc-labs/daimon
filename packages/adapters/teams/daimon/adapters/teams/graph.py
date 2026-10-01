"""Microsoft Graph access for a team, app-only: the piece other Graph reads extend.

Three parts: the app token (MSAL, behind `GraphToken`, caches it per scope),
`TeamGroups` (the team's Entra group id Graph addresses it by, looked up once
per team) and `GraphClient`, whose requests only ever go to `GRAPH_HOST` and
never follow a redirect. The app's resource-specific consent
`ChannelMessage.Read.Group`, granted by a team owner at install, covers every
call here. Each read is one page: a long thread is marked truncated rather
than paged inside a turn. Any failure (no consent, throttling, a timeout, an
odd body) raises `GraphUnavailable`, whose fields carry no message content.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx
from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

GRAPH_HOST = "graph.microsoft.com"
GRAPH_SCOPE = f"https://{GRAPH_HOST}/.default"
_ROOT = f"https://{GRAPH_HOST}/v1.0"
# Graph's ceiling for `$top` on replies and channel messages.
MAX_PAGE = 50
# History is a nicety; a slow Graph must not hold the turn long.
GRAPH_TIMEOUT_S = 10.0

GraphToken = Callable[[], Awaitable[str | None]]
# Bot Framework team id -> Entra group id, or None when it cannot be found.
TeamGroupLookup = Callable[[str], Awaitable[str | None]]


class GraphUnavailable(DaimonError):
    """A Graph read that did not succeed. `status` is the HTTP status, if any."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class GraphIdentity(_Model):
    id: str | None = None
    display_name: str | None = None


class GraphSender(_Model):
    user: GraphIdentity | None = None
    application: GraphIdentity | None = None


class GraphBody(_Model):
    content_type: str = "text"
    content: str | None = None


class GraphAttachment(_Model):
    id: str | None = None
    content_type: str | None = None
    name: str | None = None


class GraphMessage(_Model):
    """The fields of a Graph `chatMessage` daimon reads."""

    id: str
    message_type: str = "message"
    created_date_time: str | None = None
    deleted_date_time: str | None = None
    sender: GraphSender | None = Field(default=None, alias="from")
    body: GraphBody = Field(default_factory=GraphBody)
    attachments: list[GraphAttachment] = Field(default_factory=list[GraphAttachment])


class GraphPage(_Model):
    """One page of messages, in Graph's order; `next_link` set when more exist."""

    value: list[GraphMessage] = Field(default_factory=list[GraphMessage])
    next_link: str | None = Field(default=None, alias="@odata.nextLink")


def is_graph_url(url: httpx.URL) -> bool:
    return url.scheme == "https" and url.host == GRAPH_HOST


def _segment(value: str) -> str:
    # Ids go in one path segment each, so a `/` or `..` in one cannot move the request.
    return quote(value, safe="")


class TeamGroups:
    """Entra group ids by Bot Framework team id, cached for the process.

    Channel activities carry only the Bot Framework team id; Graph wants the
    group id, which one Bot Framework call per team turns it into.
    """

    def __init__(self, lookup: TeamGroupLookup) -> None:
        self._lookup = lookup
        self._groups: dict[str, str] = {}

    async def group_id(self, team_id: str | None, *, known: str | None = None) -> str:
        """`known` (an activity's own `aadGroupId`) when given, else the cached lookup."""
        if known:
            return known
        if team_id is None:
            raise GraphUnavailable("no team")
        if (cached := self._groups.get(team_id)) is None:
            if not (cached := await self._lookup(team_id)):
                raise GraphUnavailable("team group id not found")
            self._groups[team_id] = cached
        return cached


class GraphClient:
    """Channel-message reads over `http` with an app-only token from `token`."""

    def __init__(self, http: httpx.AsyncClient, token: GraphToken) -> None:
        self._http = http
        self.token = token

    def _channel(self, group_id: str, channel_id: str) -> str:
        return f"{_ROOT}/teams/{_segment(group_id)}/channels/{_segment(channel_id)}"

    async def get_message(
        self, group_id: str, channel_id: str, message_id: str, *, root_id: str | None = None
    ) -> GraphMessage:
        """A root post, or with `root_id` a reply under it."""
        path = f"{self._channel(group_id, channel_id)}/messages/"
        if root_id is not None and root_id != message_id:
            path += f"{_segment(root_id)}/replies/"
        return _parse(GraphMessage, await self._get(path + _segment(message_id)))

    async def list_replies(
        self, group_id: str, channel_id: str, root_id: str, *, top: int = MAX_PAGE
    ) -> GraphPage:
        """The newest `top` replies to `root_id`, newest first."""
        url = f"{self._channel(group_id, channel_id)}/messages/{_segment(root_id)}/replies"
        return _parse(GraphPage, await self._get(url, top=top))

    async def list_channel_messages(
        self, group_id: str, channel_id: str, *, top: int = MAX_PAGE
    ) -> GraphPage:
        """The `top` most recently active root posts, without their replies."""
        url = f"{self._channel(group_id, channel_id)}/messages"
        return _parse(GraphPage, await self._get(url, top=top))

    async def _get(self, url: str, *, top: int | None = None) -> object:
        target = httpx.URL(url, params={"$top": str(min(top, MAX_PAGE))} if top else None)
        if not is_graph_url(target):
            raise GraphUnavailable("not a Graph URL")
        try:
            token = await self.token()
        except (ValueError, OSError) as err:  # MSAL's error, or its transport's
            raise GraphUnavailable(f"token: {type(err).__name__}") from err
        if not token:
            raise GraphUnavailable("no token")
        try:
            response = await self._http.get(
                target,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
                timeout=GRAPH_TIMEOUT_S,
            )
        except httpx.HTTPError as err:
            raise GraphUnavailable(type(err).__name__) from err
        if response.status_code != 200:
            raise GraphUnavailable("http error", status=response.status_code)
        try:
            return response.json()
        except ValueError as err:
            raise GraphUnavailable("not JSON", status=200) from err


def _parse[T: BaseModel](model: type[T], data: object) -> T:
    """`model` from a Graph body, or `GraphUnavailable` for a shape we do not know."""
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise GraphUnavailable("unexpected body", status=200) from err
