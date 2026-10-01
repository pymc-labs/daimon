"""Channel files through Graph under `Sites.Selected`: folders, uploads, shared-file links.

Graph and SharePoint are one `MockTransport` with payloads shaped like Graph's.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping

import httpx
import pytest
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.channel_files import RECHECK_S, ChannelFiles
from daimon.adapters.teams.graph import GraphClient, GraphUnavailable, TeamGroups
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.sharepoint import (
    SIMPLE_UPLOAD_MAX,
    UPLOAD_CHUNK,
    DriveFolder,
    SharePoint,
)

from .conftest import make_inbound

GROUP = "11111111-1111-1111-1111-111111111111"
TEAM = "19:team@thread.tacv2"
CHANNEL = "19:channel-1@thread.tacv2"
FILES_FOLDER = f"/v1.0/teams/{GROUP}/channels/{CHANNEL}/filesFolder"
SITE_ID = "example.sharepoint.com,2c1f0a9e-0000-4000-8000-000000000001,7d3b-web"
LIBRARY = "https://example.sharepoint.com/sites/team/Shared%20Documents"
FOLDER = DriveFolder(drive_id="b!drive-1", item_id="01FOLDER")
ITEM = {
    "id": "01ITEM",
    "name": "report.csv",
    "webUrl": f"{LIBRARY}/Planning/report.csv",
    "parentReference": {"driveId": "b!drive-1", "id": "01FOLDER"},
}
UPLOAD_URL = (
    "https://example.sharepoint.com/sites/team/_api/v2.0/drives/b!drive-1/uploadSession?guid=1"
)
DOWNLOAD_URL = (
    "https://example.sharepoint.com/sites/team/_layouts/15/download.aspx?UniqueId=1&tempauth=x"
)

Handler = Callable[[httpx.Request], httpx.Response]


async def _token() -> str:
    return "graph-token"


def _sharepoint(routes: Mapping[tuple[str, str], Handler], seen: list[httpx.Request]) -> SharePoint:
    """Routes by (method, decoded path); anything else is a 404, as Graph answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes.get((request.method, request.url.path))
        return (
            route(request)
            if route
            else httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SharePoint(GraphClient(http, _token), http)


def _ok(body: object, status: int = 200) -> Handler:
    return lambda _: httpx.Response(status, json=body)


def _denied(_: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"error": {"code": "accessDenied", "message": "Access denied"}})


def _channel(**changes: object) -> TeamsInbound:
    inbound = make_inbound(kind="channel", conversation=f"{CHANNEL};messageid=1700000000001")
    facts: dict[str, object] = {"channel_id": CHANNEL, "team_id": TEAM, "team_group_id": GROUP}
    return dataclasses.replace(inbound, **(facts | changes))


async def _no_names(_: str) -> dict[str, str | None]:
    raise AssertionError("no channel lookup expected")


async def test_upload_puts_a_small_file_in_one_call_when_under_the_simple_limit() -> None:
    """Up to 4 MiB: one PUT to Graph with the token, renaming rather than overwriting."""
    seen: list[httpx.Request] = []
    path = "/v1.0/drives/b!drive-1/items/01FOLDER:/report.csv:/content"
    sharepoint = _sharepoint({("PUT", path): _ok(ITEM, 201)}, seen)

    item = await sharepoint.upload(FOLDER, "report.csv", b"a,b\n")

    assert item.web_url == ITEM["webUrl"], "the link comes from Graph's driveItem"
    [put] = seen
    assert put.url.params["@microsoft.graph.conflictBehavior"] == "rename", "never overwrite"
    assert put.headers["Authorization"] == "Bearer graph-token"
    assert put.content == b"a,b\n", "the bytes go up as the body"


async def test_upload_streams_a_large_file_through_a_session_without_the_token() -> None:
    """Over 4 MiB: an upload session, then chunks to its SharePoint URL with no bearer token."""
    seen: list[httpx.Request] = []
    content = b"x" * (SIMPLE_UPLOAD_MAX + 1)
    session = "/v1.0/drives/b!drive-1/items/01FOLDER:/big.bin:/createUploadSession"

    def chunk(request: httpx.Request) -> httpx.Response:
        end = int(request.headers["Content-Range"].split("-")[1].split("/")[0])
        if end + 1 < len(content):
            return httpx.Response(202, json={"nextExpectedRanges": [f"{end + 1}-"]})
        return httpx.Response(201, json=ITEM)

    routes = {
        ("POST", session): _ok(
            {"uploadUrl": UPLOAD_URL, "expirationDateTime": "2026-10-02T00:00:00Z"}
        ),
        ("PUT", httpx.URL(UPLOAD_URL).path): chunk,
    }
    sharepoint = _sharepoint(routes, seen)

    item = await sharepoint.upload(FOLDER, "big.bin", content)

    assert item.id == "01ITEM", "the last chunk's reply is the driveItem"
    create, *chunks = seen
    assert json.loads(create.content) == {"item": {"@microsoft.graph.conflictBehavior": "rename"}}
    assert [c.headers["Content-Range"] for c in chunks] == [
        f"bytes {start}-{min(start + UPLOAD_CHUNK, len(content)) - 1}/{len(content)}"
        for start in range(0, len(content), UPLOAD_CHUNK)
    ], "chunks are whole multiples of 320 KiB, in order"
    assert all("Authorization" not in c.headers for c in chunks), (
        "the session URL is pre-authorised"
    )
    assert b"".join(c.content for c in chunks) == content


async def test_upload_refuses_a_session_url_off_sharepoint() -> None:
    """An upload URL is followed only to SharePoint, so bytes never leave for another host."""
    seen: list[httpx.Request] = []
    session = "/v1.0/drives/b!drive-1/items/01FOLDER:/big.bin:/createUploadSession"
    sharepoint = _sharepoint(
        {("POST", session): _ok({"uploadUrl": "https://evil.example/u"})}, seen
    )

    with pytest.raises(GraphUnavailable, match="not on SharePoint"):
        await sharepoint.upload(FOLDER, "big.bin", b"x" * (SIMPLE_UPLOAD_MAX + 1))
    assert {r.url.host for r in seen} == {"graph.microsoft.com"}, "nothing went to the other host"


async def test_channel_folder_falls_back_to_the_team_site_when_files_folder_is_refused() -> None:
    """`filesFolder` does not list Sites.Selected: the folder is then found by name in the site."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", FILES_FOLDER): _denied,
        ("GET", f"/v1.0/groups/{GROUP}/sites/root"): _ok({"id": SITE_ID, "name": "team"}),
        ("GET", f"/v1.0/sites/{SITE_ID}/drive/root:/Planning"): _ok(
            {
                "id": "01FOLDER",
                "name": "Planning",
                "folder": {},
                "parentReference": {"driveId": "b!drive-1"},
            }
        ),
    }
    sharepoint = _sharepoint(routes, seen)

    async def name() -> str:
        return "Planning"

    folder = await sharepoint.channel_folder(GROUP, CHANNEL, channel_name=name)

    assert folder == FOLDER, "the channel's folder in the site's default library"


async def test_download_url_finds_a_shared_file_through_its_site_library() -> None:
    """`/shares` needs Files.ReadWrite.All, so the URL is resolved by site, library and path."""
    seen: list[httpx.Request] = []
    drives = {
        "value": [
            {"id": "b!drive-2", "webUrl": "https://example.sharepoint.com/sites/team/Other"},
            {"id": "b!drive-1", "webUrl": LIBRARY},
        ]
    }
    routes = {
        ("GET", "/v1.0/sites/example.sharepoint.com:/sites/team"): _ok({"id": SITE_ID}),
        ("GET", f"/v1.0/sites/{SITE_ID}/drives"): _ok(drives),
        ("GET", "/v1.0/drives/b!drive-1/root:/Planning/q3 plan.xlsx"): _ok(
            {"id": "01Q3", "name": "q3 plan.xlsx", "@microsoft.graph.downloadUrl": DOWNLOAD_URL}
        ),
    }
    sharepoint = _sharepoint(routes, seen)

    url = await sharepoint.download_url(f"{LIBRARY}/Planning/q3%20plan.xlsx")
    again = await sharepoint.download_url(f"{LIBRARY}/Planning/q3%20plan.xlsx")

    assert url == again == DOWNLOAD_URL
    assert len(seen) == 4, "the site's libraries are looked up once"


@pytest.mark.parametrize(
    "content_url",
    [
        "https://evil.example/sites/team/Shared%20Documents/q3.xlsx",
        "http://example.sharepoint.com/sites/team/Shared%20Documents/q3.xlsx",
        "https://example.sharepoint.com/personal/u/Documents/q3.xlsx",
        "https://example.sharepoint.com/sites/team/Shared%20Documents/%2e%2e/%2e%2e/q3.xlsx",
    ],
)
async def test_download_url_refuses_a_link_off_a_sharepoint_site_without_a_request(
    content_url: str,
) -> None:
    """Only a team site's file on a SharePoint host is looked up; the URL is untrusted input."""
    seen: list[httpx.Request] = []
    with pytest.raises(GraphUnavailable):
        await _sharepoint({}, seen).download_url(content_url)
    assert seen == [], "a refused link costs no call"


async def test_download_url_refuses_a_download_url_off_sharepoint() -> None:
    """The pre-authorised URL is fetched later, so it too must be on SharePoint."""
    routes = {
        ("GET", "/v1.0/sites/example.sharepoint.com:/sites/team"): _ok({"id": SITE_ID}),
        ("GET", f"/v1.0/sites/{SITE_ID}/drives"): _ok(
            {"value": [{"id": "b!drive-1", "webUrl": LIBRARY}]}
        ),
        ("GET", "/v1.0/drives/b!drive-1/root:/q3.xlsx"): _ok(
            {"id": "01Q3", "@microsoft.graph.downloadUrl": "https://evil.example/q3.xlsx"}
        ),
    }
    with pytest.raises(GraphUnavailable, match="no SharePoint download URL"):
        await _sharepoint(routes, []).download_url(f"{LIBRARY}/q3.xlsx")


async def test_a_refused_upload_marks_the_team_unavailable_until_the_recheck() -> None:
    """A 403 is remembered, so later files skip Graph, and re-probed after `RECHECK_S`."""
    seen: list[httpx.Request] = []
    now = [0.0]
    put = "/v1.0/drives/b!drive-1/items/01FOLDER:/a.csv:/content"
    folder = {"id": "01FOLDER", "parentReference": {"driveId": "b!drive-1"}}
    sharepoint = _sharepoint({("GET", FILES_FOLDER): _ok(folder), ("PUT", put): _denied}, seen)
    files = ChannelFiles(sharepoint, TeamGroups(_no_group), _no_names, clock=lambda: now[0])
    inbound = _channel()

    assert await files.is_available(inbound), "the folder is reachable, so files look available"
    with pytest.raises(GraphUnavailable):
        await files.upload(inbound, "a.csv", b"a")
    calls = len(seen)
    assert not await files.is_available(inbound), "the refusal is what the next turn is told"
    with pytest.raises(GraphUnavailable):
        await files.upload(inbound, "a.csv", b"a")
    assert len(seen) == calls, "known unavailable: no Graph call until the recheck"

    now[0] = RECHECK_S
    assert await files.is_available(inbound), "a grant added later is picked up"
    assert len(seen) == calls + 1, "the recheck probes the folder again"


@pytest.mark.parametrize("private_first", [False, True])
async def test_a_private_channel_is_unavailable_while_the_teams_standard_channel_works(
    private_first: bool,
) -> None:
    """Access is per channel: a private channel has no folder in the team site, whatever ran first."""
    private = "19:private@thread.tacv2"
    routes = {
        ("GET", f"/v1.0/groups/{GROUP}/sites/root"): _ok({"id": SITE_ID}),
        ("GET", f"/v1.0/sites/{SITE_ID}/drive/root:/Planning"): _ok(
            {"id": "01FOLDER", "parentReference": {"driveId": "b!drive-1"}}
        ),
        ("GET", FILES_FOLDER): _denied,
        ("GET", f"/v1.0/teams/{GROUP}/channels/{private}/filesFolder"): _denied,
    }

    async def standard_only(_: str) -> dict[str, str | None]:
        return {CHANNEL: "Planning"}  # what Bot Framework lists once private ones are dropped

    files = ChannelFiles(_sharepoint(routes, []), TeamGroups(_no_group), standard_only)
    order = [_channel(channel_id=private), _channel()] if private_first else [_channel()]
    results = [await files.is_available(inbound) for inbound in order]

    assert results[-1], "the standard channel's folder is found"
    assert not await files.is_available(_channel(channel_id=private)), (
        "the private channel never inherits the standard channel's answer"
    )
    assert await files.is_available(_channel()), "nor does it mark the whole team unavailable"


async def test_the_general_channel_folder_is_named_without_a_channel_lookup() -> None:
    """The General channel's id is the team's; its folder is `General`, no Bot Framework call."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", f"/v1.0/teams/{GROUP}/channels/{TEAM}/filesFolder"): _denied,
        ("GET", f"/v1.0/groups/{GROUP}/sites/root"): _ok({"id": SITE_ID}),
        ("GET", f"/v1.0/sites/{SITE_ID}/drive/root:/General"): _ok(
            {"id": "01GEN", "parentReference": {"driveId": "b!drive-1"}}
        ),
    }
    files = ChannelFiles(_sharepoint(routes, seen), TeamGroups(_no_group), _no_names)

    assert await files.is_available(_channel(channel_id=TEAM)), "General resolves by its name"


async def test_resolve_leaves_an_unreachable_shared_file_without_a_link() -> None:
    """No site grant: the file keeps no download URL, so it is named, never fetched."""
    shared = SharedFile("q3.xlsx", f"{LIBRARY}/q3.xlsx")
    routes = {("GET", "/v1.0/sites/example.sharepoint.com:/sites/team"): _denied}
    files = ChannelFiles(_sharepoint(routes, []), TeamGroups(_no_group), _no_names)

    media = await files.resolve(ChannelMedia(files=(shared,)))

    assert media.files == (shared,), "a refusal leaves the file as it was"


async def _no_group(_: str) -> str | None:
    raise AssertionError("the activity's group id is used")
