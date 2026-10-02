"""Microsoft Graph access for a team, app-only, for the Teams adapter and the MCP server.

Three parts: the app token (behind `GraphToken`, cached per scope by its owner),
`TeamGroups` (the team's Entra group id Graph addresses it by, looked up once
per team) and `GraphClient`, whose requests only ever go to `GRAPH_HOST` and
never follow a redirect. The app's resource-specific consent
`ChannelMessage.Read.Group`, granted by a team owner at install, covers the
message reads here; `teams_sharepoint` sends its file calls through
`send`. Each read is one page; `next_page` follows a page's `next_link`. Any
failure (no consent, throttling, a timeout, an odd body) raises
`GraphUnavailable`, whose fields carry no message content.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from html.parser import HTMLParser
from urllib.parse import quote

import httpx
from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

GRAPH_HOST = "graph.microsoft.com"
GRAPH_SCOPE = f"https://{GRAPH_HOST}/.default"
GRAPH_ROOT = f"https://{GRAPH_HOST}/v1.0"
# Graph's ceiling for `$top` on replies and channel messages.
MAX_PAGE = 50
# History is a nicety; a slow Graph must not hold the turn long.
GRAPH_TIMEOUT_S = 10.0
_SHAREPOINT_SUFFIXES = (".sharepoint.com", ".sharepoint.us", ".sharepoint-mil.us", ".sharepoint.cn")

#: Attachment types of a file shared in a message.
FILE_ATTACHMENT_TYPES = frozenset(
    {"reference", "application/vnd.microsoft.teams.file.download.info"}
)
CARD_ATTACHMENT_TYPE = "application/vnd.microsoft.card.adaptive"

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
    content_url: str | None = None
    #: A card's JSON, as a string.
    content: str | None = None


class GraphMessage(_Model):
    """The fields of a Graph `chatMessage` daimon reads."""

    id: str
    message_type: str = "message"
    created_date_time: str | None = None
    deleted_date_time: str | None = None
    reply_to_id: str | None = None
    subject: str | None = None
    web_url: str | None = None
    sender: GraphSender | None = Field(default=None, alias="from")
    body: GraphBody = Field(default_factory=GraphBody)
    attachments: list[GraphAttachment] = Field(default_factory=list[GraphAttachment])
    #: Set only by `list_channel_messages(expand_replies=True)`, in Graph's order.
    replies: list[GraphMessage] = Field(default_factory=list["GraphMessage"])
    replies_next_link: str | None = Field(default=None, alias="replies@odata.nextLink")


class GraphPage(_Model):
    """One page of messages, in Graph's order; `next_link` set when more exist."""

    value: list[GraphMessage] = Field(default_factory=list[GraphMessage])
    next_link: str | None = Field(default=None, alias="@odata.nextLink")


def is_graph_url(url: httpx.URL) -> bool:
    return url.scheme == "https" and url.host == GRAPH_HOST


def is_sharepoint_host(url: httpx.URL) -> bool:
    return url.scheme == "https" and url.host.endswith(_SHAREPOINT_SUFFIXES)


def skiptoken_of(next_link: str | None) -> str | None:
    """The `$skiptoken` of a next link: a cursor that cannot name another resource."""
    if not next_link:
        return None
    return httpx.URL(next_link).params.get("$skiptoken") or None


def path_segment(value: str) -> str:
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
        return f"{GRAPH_ROOT}/teams/{path_segment(group_id)}/channels/{path_segment(channel_id)}"

    async def get_message(
        self, group_id: str, channel_id: str, message_id: str, *, root_id: str | None = None
    ) -> GraphMessage:
        """A root post, or with `root_id` a reply under it."""
        path = f"{self._channel(group_id, channel_id)}/messages/"
        if root_id is not None and root_id != message_id:
            path += f"{path_segment(root_id)}/replies/"
        return _parse(GraphMessage, await self._get(path + path_segment(message_id)))

    async def list_replies(
        self,
        group_id: str,
        channel_id: str,
        root_id: str,
        *,
        top: int = MAX_PAGE,
        skiptoken: str | None = None,
    ) -> GraphPage:
        """The newest `top` replies to `root_id`, newest first; `skiptoken` pages back."""
        url = f"{self._channel(group_id, channel_id)}/messages/{path_segment(root_id)}/replies"
        extra = {"$skiptoken": skiptoken} if skiptoken else {}
        return _parse(GraphPage, await self._get(url, top=top, extra=extra))

    async def list_channel_messages(
        self,
        group_id: str,
        channel_id: str,
        *,
        top: int = MAX_PAGE,
        expand_replies: bool = False,
        skiptoken: str | None = None,
    ) -> GraphPage:
        """The `top` most recently active root posts; with `expand_replies`, their replies.

        `skiptoken` (from `skiptoken_of` a page's `next_link`) reads the next page.
        """
        url = f"{self._channel(group_id, channel_id)}/messages"
        extra = {"$expand": "replies"} if expand_replies else {}
        if skiptoken:
            extra["$skiptoken"] = skiptoken
        return _parse(GraphPage, await self._get(url, top=top, extra=extra))

    async def next_page(self, next_link: str) -> GraphPage:
        """The page a `next_link` names; refused unless it is a Graph URL."""
        return _parse(GraphPage, await self.send("GET", next_link))

    async def _get(
        self, url: str, *, top: int | None = None, extra: Mapping[str, str] | None = None
    ) -> object:
        params = {**({"$top": str(min(top, MAX_PAGE))} if top else {}), **(extra or {})}
        return await self.send("GET", url, params=params or None)

    async def send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        body: object = None,
        content: bytes | None = None,
        timeout: float = GRAPH_TIMEOUT_S,
    ) -> object:
        """One Graph call: `body` as JSON or `content` as bytes; the JSON reply."""
        # `params=None` would drop a query already in `url`, such as a next link's.
        target = httpx.URL(url, params=params) if params else httpx.URL(url)
        if not is_graph_url(target):
            raise GraphUnavailable("not a Graph URL")
        try:
            token = await self.token()
        except (ValueError, OSError) as err:  # MSAL's error, or its transport's
            raise GraphUnavailable(f"token: {type(err).__name__}") from err
        if not token:
            raise GraphUnavailable("no token")
        try:
            headers = {"Authorization": f"Bearer {token}"}
            if content is not None:
                headers["Content-Type"] = "application/octet-stream"
            response = await self._http.request(
                method,
                target,
                headers=headers,
                json=body,
                content=content,
                follow_redirects=False,
                timeout=timeout,
            )
        except httpx.HTTPError as err:
            raise GraphUnavailable(type(err).__name__) from err
        if response.status_code not in (200, 201):
            raise GraphUnavailable("http error", status=response.status_code)
        try:
            return response.json()
        except ValueError as err:
            raise GraphUnavailable("not JSON", status=200) from err


_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "blockquote", "pre", "codeblock", "h1", "h2", "h3", "h4"}
)
_SKIPPED_TAGS = frozenset({"script", "style", "attachment"})


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self._skip = 0
        self._pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")
        if tag in ("pre", "codeblock"):
            self._pre += 1
        elif tag == "at":
            self.parts.append("@")
        elif tag == "img" and "emoji" not in (values.get("itemtype") or "").lower():
            self.parts.append("[image]")
            if src := values.get("src"):
                self.images.append(src)
        elif tag in ("emoji", "img"):
            self.parts.append(values.get("alt") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in ("pre", "codeblock"):
            self._pre = max(0, self._pre - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            # Code keeps its spaces through the line strip below as NULs.
            self.parts.append(data.replace(" ", "\0") if self._pre else re.sub(r"\s+", " ", data))


def _parse_html(html: str) -> _Text:
    parser = _Text()
    parser.feed(html)
    parser.close()
    return parser


def html_to_text(html: str) -> str:
    """Readable text from a Teams HTML body: blocks become lines, `<at>` becomes `@name`."""
    text = "".join(_parse_html(html.replace("\0", "")).parts).replace("\xa0", " ")
    lines = [line.strip(" \t").replace("\0", " ") for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def image_sources(html: str) -> list[str]:
    """The `src` of each non-emoji `<img>` in a Teams HTML body."""
    return _parse_html(html).images


def message_text(message: GraphMessage) -> str:
    """A message's body as readable text."""
    content = message.body.content or ""
    return html_to_text(content) if message.body.content_type == "html" else content.strip()


def _parse[T: BaseModel](model: type[T], data: object) -> T:
    """`model` from a Graph body, or `GraphUnavailable` for a shape we do not know."""
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise GraphUnavailable("unexpected body", status=200) from err
