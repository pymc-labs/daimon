"""A team's SharePoint files through Graph, under the application permission `Sites.Selected`.

`Sites.Selected` reaches only the sites a tenant admin granted the app, and
only through `/sites/...` and `/drives/...` paths; neither the Teams
`filesFolder` call nor `/shares` lists it, so each has a site-path fallback.
A channel's folder is `filesFolder` when Graph allows it, else the folder
named after the channel in the team site's default library. A shared file is
found by its URL: the site, which must be the team's own, then the library
whose URL prefixes it, then the item, whose short-lived `downloadUrl` is
pre-authorised. Uploads never
overwrite (`conflictBehavior=rename`); one over `SIMPLE_UPLOAD_MAX` goes
through an upload session, whose URL must be on SharePoint and gets no token.
Any failure raises `GraphUnavailable`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from daimon.adapters.teams.attachments import is_sharepoint_host
from daimon.adapters.teams.graph import (
    GRAPH_ROOT,
    GraphClient,
    GraphUnavailable,
    path_segment,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

# Graph's simple upload takes up to 4 MiB; larger files need an upload session.
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
# Upload-session chunks must be multiples of 320 KiB.
UPLOAD_CHUNK = 16 * 320 * 1024
UPLOAD_TIMEOUT_S = 60.0
_CONFLICT = "@microsoft.graph.conflictBehavior"

# The channel's name, which Teams names a standard channel's folder after.
ChannelName = Callable[[], Awaitable[str]]


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _Parent(_Model):
    drive_id: str | None = None


class DriveItem(_Model):
    """The fields of a Graph `driveItem` daimon reads."""

    id: str
    name: str | None = None
    web_url: str | None = None
    parent_reference: _Parent | None = None
    download_url: str | None = Field(default=None, alias="@microsoft.graph.downloadUrl")


class DriveFolder(_Model):
    drive_id: str
    item_id: str


class _Site(_Model):
    id: str


class _Drive(_Model):
    id: str
    web_url: str


class _Drives(_Model):
    value: list[_Drive] = Field(default_factory=list[_Drive])


class _UploadSession(_Model):
    upload_url: str


def _parse[T: BaseModel](model: type[T], data: object) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise GraphUnavailable("unexpected body", status=200) from err


def _is_refusal(err: GraphUnavailable) -> bool:
    return err.status in (401, 403, 404)


class SharePoint:
    """Channel folders, uploads and shared-file lookups over `graph`; `http` for upload sessions."""

    def __init__(self, graph: GraphClient, http: httpx.AsyncClient) -> None:
        self._graph = graph
        self._http = http
        # Group id -> its team site id; site path (`host/sites/x`) -> site id; site id -> libraries.
        self._team_sites: dict[str, str] = {}
        self._sites: dict[str, str] = {}
        self._drives: dict[str, list[_Drive]] = {}

    async def _team_site(self, group_id: str) -> str:
        if (site := self._team_sites.get(group_id)) is None:
            data = await self._graph.send(
                "GET", f"{GRAPH_ROOT}/groups/{path_segment(group_id)}/sites/root"
            )
            site = self._team_sites[group_id] = _parse(_Site, data).id
        return site

    async def channel_folder(
        self, group_id: str, channel_id: str, *, channel_name: ChannelName
    ) -> DriveFolder:
        """The channel's Files folder: `filesFolder`, else by name in the team site."""
        channel = f"{GRAPH_ROOT}/teams/{path_segment(group_id)}/channels/{path_segment(channel_id)}"
        try:
            item = _parse(DriveItem, await self._graph.send("GET", f"{channel}/filesFolder"))
        except GraphUnavailable as err:
            if not _is_refusal(err):
                raise
            site = await self._team_site(group_id)
            name = path_segment(await channel_name())
            drive = f"{GRAPH_ROOT}/sites/{path_segment(site)}/drive"
            item = _parse(DriveItem, await self._graph.send("GET", f"{drive}/root:/{name}"))
        if item.parent_reference is None or item.parent_reference.drive_id is None:
            raise GraphUnavailable("folder without a drive", status=200)
        return DriveFolder(drive_id=item.parent_reference.drive_id, item_id=item.id)

    async def upload(self, folder: DriveFolder, name: str, content: bytes) -> DriveItem:
        """Save `content` as `name` in `folder`, renamed rather than overwriting."""
        target = (
            f"{GRAPH_ROOT}/drives/{path_segment(folder.drive_id)}"
            f"/items/{path_segment(folder.item_id)}:/{path_segment(name)}:"
        )
        if len(content) <= SIMPLE_UPLOAD_MAX:
            params = {_CONFLICT: "rename"}
            data = await self._graph.send(
                "PUT", f"{target}/content", params=params, content=content, timeout=UPLOAD_TIMEOUT_S
            )
            return _parse(DriveItem, data)
        session = await self._graph.send(
            "POST", f"{target}/createUploadSession", body={"item": {_CONFLICT: "rename"}}
        )
        return await self._upload_chunks(_parse(_UploadSession, session).upload_url, content)

    async def _upload_chunks(self, upload_url: str, content: bytes) -> DriveItem:
        url = _url(upload_url)
        if not is_sharepoint_host(url):
            raise GraphUnavailable("upload URL is not on SharePoint")
        total = len(content)
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = content[start : start + UPLOAD_CHUNK]
            end = start + len(chunk) - 1
            try:
                # Pre-authorised: a bearer token here is refused, and must not leak.
                response = await self._http.put(
                    url,
                    content=chunk,
                    headers={"Content-Range": f"bytes {start}-{end}/{total}"},
                    follow_redirects=False,
                    timeout=UPLOAD_TIMEOUT_S,
                )
            except httpx.HTTPError as err:
                raise GraphUnavailable(type(err).__name__) from err
            if end + 1 < total and response.status_code == 202:
                continue
            if end + 1 == total and response.status_code in (200, 201):
                try:
                    return _parse(DriveItem, response.json())
                except ValueError as err:
                    raise GraphUnavailable("not JSON", status=response.status_code) from err
            raise GraphUnavailable("upload failed", status=response.status_code)
        raise GraphUnavailable("empty upload")

    async def download_url(self, content_url: str, *, group_id: str) -> str:
        """The pre-authorised download URL of the shared file at `content_url` in the team site."""
        # Teams sends the path unencoded: a `#` or `?` there is part of the file's name.
        url = _url(content_url.replace("#", "%23").replace("?", "%3F"))
        parts = url.path.strip("/").split("/")
        if not is_sharepoint_host(url) or len(parts) < 3 or parts[0] not in ("sites", "teams"):
            raise GraphUnavailable("not a SharePoint site file")
        if {".", ".."} & set(parts):
            raise GraphUnavailable("a relative path segment")
        # First, so a 403 means the team's own site is not granted (a grant fixes that).
        team_site = await self._team_site(group_id)
        try:
            site = await self._site(url.host, "/".join(parts[:2]))
        except GraphUnavailable as err:
            if err.status != 403:
                raise
            raise GraphUnavailable("not the team's site", status=200) from err
        # The app may be granted other sites: only the team's own is this channel's.
        if site.casefold() != team_site.casefold():
            raise GraphUnavailable("not the team's site", status=200)
        drive, relative = await self._library(site, url.path)
        path = "/".join(path_segment(part) for part in relative.split("/"))
        data = await self._graph.send(
            "GET", f"{GRAPH_ROOT}/drives/{path_segment(drive)}/root:/{path}"
        )
        download = _parse(DriveItem, data).download_url
        if download is None or not is_sharepoint_host(_url(download)):
            raise GraphUnavailable("no SharePoint download URL", status=200)
        return download

    async def _site(self, host: str, site_path: str) -> str:
        key = f"{host}/{site_path}"
        if (site := self._sites.get(key)) is None:
            segments = "/".join(path_segment(part) for part in site_path.split("/"))
            data = await self._graph.send("GET", f"{GRAPH_ROOT}/sites/{host}:/{segments}")
            site = self._sites[key] = _parse(_Site, data).id
        return site

    async def _list_drives(self, site: str) -> list[_Drive]:
        listed = await self._graph.send("GET", f"{GRAPH_ROOT}/sites/{path_segment(site)}/drives")
        drives = self._drives[site] = _parse(_Drives, listed).value
        return drives

    async def _library(self, site: str, file_path: str) -> tuple[str, str]:
        """`(drive id, path inside it)` for the library holding `file_path`."""
        cached = self._drives.get(site)
        drives = cached if cached is not None else await self._list_drives(site)
        found = _library_path(drives, file_path)
        if found is None and cached is not None:
            found = _library_path(await self._list_drives(site), file_path)  # a newer library
        if found is None:
            raise GraphUnavailable("no library holds the file", status=200)
        return found


def _url(value: str) -> httpx.URL:
    """`value` parsed; Graph's URLs are untrusted, so a malformed one is unavailable."""
    try:
        return httpx.URL(value)
    except httpx.InvalidURL as err:
        raise GraphUnavailable("not a URL", status=200) from err


def _library_path(drives: list[_Drive], file_path: str) -> tuple[str, str] | None:
    for drive in drives:
        root = _url(drive.web_url).path.rstrip("/") + "/"
        if file_path.casefold().startswith(root.casefold()):
            return drive.id, file_path[len(root) :]
    return None
