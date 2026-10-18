"""Channel files through Graph under `Files.ReadWrite.All`: folders, uploads, shared-file links.

Graph and SharePoint are one `MockTransport` with payloads shaped like Graph's.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Callable, Mapping

import httpx
import pytest
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.channel_files import RECHECK_S, ChannelFiles
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.output_delivery import MAX_BYTES_PER_FILE
from daimon.core.teams_graph import GraphClient, GraphUnavailable, TeamGroups
from daimon.core.teams_sharepoint import (
    UPLOAD_TIMEOUT_S,
    DriveFolder,
    SharePoint,
    download_url_of,
)

from .conftest import make_inbound

GROUP = "11111111-1111-1111-1111-111111111111"
TEAM = "19:team@thread.tacv2"
CHANNEL = "19:channel-1@thread.tacv2"
PRIVATE = "19:private@thread.tacv2"
FILES_FOLDER = f"/v1.0/teams/{GROUP}/channels/{CHANNEL}/filesFolder"
LIBRARY = "https://example.sharepoint.com/sites/team/Shared%20Documents"
FOLDER = DriveFolder(drive_id="b!drive-1", item_id="01FOLDER")
FOLDER_ITEM = {"id": "01FOLDER", "name": "Planning", "parentReference": {"driveId": "b!drive-1"}}
ITEM = {
    "id": "01ITEM",
    "name": "report.csv",
    "webUrl": f"{LIBRARY}/Planning/report.csv",
    "parentReference": {"driveId": "b!drive-1", "id": "01FOLDER"},
}
DOWNLOAD_URL = (
    "https://example.sharepoint.com/sites/team/_layouts/15/download.aspx?UniqueId=1&tempauth=x"
)
# Folders of the team site's library by id, each read for its parent: the channel's
# `Planning` and its siblings at the library's root, which has no parent.
FOLDERS = {
    folder: {"id": folder, "parentReference": {"driveId": "b!drive-1", "id": "01ROOT"}}
    for folder in ("01FOLDER", "01BOARD", "01PLANNING2")
} | {"01ROOT": {"id": "01ROOT", "parentReference": {"driveId": "b!drive-1"}}}

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

    return SharePoint(
        GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _token)
    )


def _ok(body: object, status: int = 200) -> Handler:
    return lambda _: httpx.Response(status, json=body)


def _denied(_: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"error": {"code": "accessDenied", "message": "Access denied"}})


def _shares(url: str) -> str:
    """Graph's `/shares` path for a file URL: `u!` and the URL in unpadded base64url."""
    return f"/v1.0/shares/u!{base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')}/driveItem"


def _file_routes(
    url: str, *, drive: str = "b!drive-1", parent: str = "01FOLDER"
) -> dict[tuple[str, str], Handler]:
    """A shared file at `url`, in `drive` under the folder with id `parent`, plus `FOLDERS`."""
    found = {"id": "01Q3", "parentReference": {"driveId": drive}}
    item = {
        "id": "01Q3",
        "eTag": '"{Q3},4"',
        "parentReference": {"driveId": drive, "id": parent},
        "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
    }
    folders = {("GET", f"/v1.0/drives/b!drive-1/items/{k}"): _ok(v) for k, v in FOLDERS.items()}
    return {
        ("GET", _shares(url)): _ok(found),
        ("GET", f"/v1.0/drives/{drive}/items/01Q3"): _ok(item),
        **folders,
    }


def _channel(**changes: object) -> TeamsInbound:
    inbound = make_inbound(kind="channel", conversation=f"{CHANNEL};messageid=1700000000001")
    facts: dict[str, object] = {"channel_id": CHANNEL, "team_id": TEAM, "team_group_id": GROUP}
    return dataclasses.replace(inbound, **(facts | changes))


async def _no_group(_: str) -> str | None:
    raise AssertionError("the activity's group id is used")


def _files(sharepoint: SharePoint, *, clock: Callable[[], float] | None = None) -> ChannelFiles:
    if clock is None:
        return ChannelFiles(sharepoint, TeamGroups(_no_group))
    return ChannelFiles(sharepoint, TeamGroups(_no_group), clock=clock)


@pytest.mark.parametrize("size", [4, MAX_BYTES_PER_FILE])
async def test_upload_puts_any_file_up_to_the_cap_in_one_call(size: int) -> None:
    """One PUT to Graph with the token, renaming rather than overwriting, up to 20 MiB."""
    seen: list[httpx.Request] = []
    path = "/v1.0/drives/b!drive-1/items/01FOLDER:/report.csv:/content"
    sharepoint = _sharepoint({("PUT", path): _ok(ITEM, 201)}, seen)
    content = b"x" * size

    item = await sharepoint.upload(FOLDER, "report.csv", content)

    assert item.web_url == ITEM["webUrl"], "the link comes from Graph's driveItem"
    [put] = seen
    assert put.url.params["@microsoft.graph.conflictBehavior"] == "rename", "never overwrite"
    assert put.headers["Authorization"] == "Bearer graph-token"
    assert put.content == content, "the bytes go up as the body, whole"
    assert put.extensions["timeout"]["read"] == UPLOAD_TIMEOUT_S, "a 20 MiB body gets time"


async def test_thumbnail_url_reads_the_large_first_page_thumbnail_of_an_upload() -> None:
    """The reply's drive is filled in from the folder, so the thumbnail lookup can address it."""
    seen: list[httpx.Request] = []
    put = "/v1.0/drives/b!drive-1/items/01FOLDER:/deck.pptx:/content"
    thumbnails = "/v1.0/drives/b!drive-1/items/01ITEM/thumbnails"
    large = "https://example.sharepoint.com/_api/v2.1/thumbnail?tempauth=x"
    body = {"value": [{"id": "0", "large": {"url": large, "width": 800, "height": 600}}]}
    routes = {("PUT", put): _ok({"id": "01ITEM", "name": "deck.pptx"}, 201)}
    sharepoint = _sharepoint({**routes, ("GET", thumbnails): _ok(body)}, seen)

    item = await sharepoint.upload(FOLDER, "deck.pptx", b"pk")

    assert await sharepoint.thumbnail_url(item) == large, "the first set's large image"
    assert seen[-1].headers["Authorization"] == "Bearer graph-token", "the token goes to Graph"
    empty = _sharepoint({("GET", thumbnails): _ok({"value": []})}, [])
    assert await empty.thumbnail_url(item) is None, "none while Graph is still rendering it"


async def test_channel_folder_is_files_folder_with_no_fallback_when_refused() -> None:
    """Only `filesFolder`: a refusal is the answer, with no lookup by the channel's name."""
    found = _sharepoint({("GET", FILES_FOLDER): _ok(FOLDER_ITEM)}, [])
    assert await found.channel_folder(GROUP, CHANNEL) == FOLDER, "the folder and its drive"

    seen: list[httpx.Request] = []
    with pytest.raises(GraphUnavailable) as refused:
        await _sharepoint({("GET", FILES_FOLDER): _denied}, seen).channel_folder(GROUP, CHANNEL)
    assert refused.value.status == 403, "the refusal reaches the caller"
    assert [r.url.path for r in seen] == [FILES_FOLDER], "no site or library lookup"


async def test_shared_item_finds_a_shared_file_in_the_channels_folder_through_shares() -> None:
    """The URL resolves through `/shares`, then by drive and id; no site or library lookup."""
    seen: list[httpx.Request] = []
    url = f"{LIBRARY}/Planning/q3%20plan.xlsx"
    sharepoint = _sharepoint(_file_routes(url), seen)

    for _ in range(2):
        item = await sharepoint.shared_item(url, folder=FOLDER)
        assert download_url_of(item) == DOWNLOAD_URL, "the item inside the folder has one"
    paths = [r.url.path for r in seen]
    assert not any(p.startswith(("/v1.0/sites", "/v1.0/groups")) for p in paths), (
        "only Files.ReadWrite.All calls"
    )
    assert paths.count("/v1.0/drives/b!drive-1/items/01Q3") == 2, "each link reads the item anew"
    assert "/v1.0/drives/b!drive-1/items/01FOLDER" not in paths, "its parent's id answers"


async def test_shared_item_shares_the_encoded_form_of_the_unencoded_path_teams_sends() -> None:
    """As captured, a channel file's `contentUrl` is not percent-encoded: `#` is in the name."""
    raw = "https://example.sharepoint.com/sites/team/Shared Documents/Planning/q3 #2?.xlsx"
    encoded = f"{LIBRARY}/Planning/q3%20%232%3F.xlsx"

    item = await _sharepoint(_file_routes(encoded), []).shared_item(raw, folder=FOLDER)

    assert download_url_of(item) == DOWNLOAD_URL, "the share id carries the file's whole name"


@pytest.mark.parametrize(
    ("drive", "parent", "where"),
    [
        ("b!finance", "01PAYROLL", "another site"),
        ("b!drive-2", "01FOLDER", "another library of the team site"),
        ("b!drive-1", "01BOARD", "another folder of the same library"),
        ("b!drive-1", "01PLANNING2", "a sibling sharing its name"),
        ("b!drive-1", "01ROOT", "the library's root"),
    ],
)
async def test_shared_item_refuses_a_file_outside_the_channels_folder(
    drive: str, parent: str, where: str
) -> None:
    """The tenant-wide permission reads any file: the channel's folder is the only boundary."""
    url = "https://example.sharepoint.com/sites/finance/Shared%20Documents/Payroll/pay.xlsx"

    with pytest.raises(GraphUnavailable, match="outside the channel's folder") as refused:
        await _sharepoint(_file_routes(url, drive=drive, parent=parent), []).shared_item(
            url, folder=FOLDER
        )
    assert refused.value.status == 200, f"{where}: unreachable, not a missing permission"


async def test_shared_item_refuses_another_drive_before_reading_the_item() -> None:
    """A file in another drive is refused on the share's answer, without a second read."""
    seen: list[httpx.Request] = []
    url = "https://example.sharepoint.com/sites/finance/Shared%20Documents/pay.xlsx"

    with pytest.raises(GraphUnavailable, match="outside the channel's folder"):
        await _sharepoint(_file_routes(url, drive="b!finance"), seen).shared_item(
            url, folder=FOLDER
        )
    assert [r.url.path for r in seen] == [_shares(url)], "nothing in that drive is read"


@pytest.mark.parametrize(
    "content_url",
    [
        "https://evil.example/sites/team/Shared%20Documents/q3.xlsx",
        "http://example.sharepoint.com/sites/team/Shared%20Documents/q3.xlsx",
        "https://example.sharepoint.com/personal/u/Documents/q3.xlsx",
        "https://example.sharepoint.com/sites/team/Shared%20Documents/%2e%2e/%2e%2e/q3.xlsx",
        "https://example.sharepoint.com/sites/team/Shared Documents/q3\x00.xlsx",
    ],
)
async def test_shared_item_refuses_a_link_off_a_sharepoint_site_without_a_request(
    content_url: str,
) -> None:
    """Only a team site's file on a SharePoint host is looked up; the URL is untrusted input."""
    seen: list[httpx.Request] = []
    with pytest.raises(GraphUnavailable):
        await _sharepoint({}, seen).shared_item(content_url, folder=FOLDER)
    assert seen == [], "a refused link costs no call"


async def test_download_url_refuses_a_download_url_off_sharepoint() -> None:
    """The pre-authorised URL is fetched later, so it too must be on SharePoint."""
    url = f"{LIBRARY}/Planning/q3.xlsx"
    routes = _file_routes(url)
    item = {
        "id": "01Q3",
        "parentReference": {"driveId": "b!drive-1", "id": "01FOLDER"},
        "@microsoft.graph.downloadUrl": "https://evil.example/q3.xlsx",
    }
    routes["GET", "/v1.0/drives/b!drive-1/items/01Q3"] = _ok(item)

    with pytest.raises(GraphUnavailable, match="no SharePoint download URL"):
        download_url_of(await _sharepoint(routes, []).shared_item(url, folder=FOLDER))


async def test_a_refused_upload_marks_the_channel_unavailable_until_the_recheck() -> None:
    """A 403 is remembered, so later files skip Graph, and re-probed after `RECHECK_S`."""
    seen: list[httpx.Request] = []
    now = [0.0]
    put = "/v1.0/drives/b!drive-1/items/01FOLDER:/a.csv:/content"
    sharepoint = _sharepoint({("GET", FILES_FOLDER): _ok(FOLDER_ITEM), ("PUT", put): _denied}, seen)
    files = _files(sharepoint, clock=lambda: now[0])
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
    assert await files.is_available(inbound), "a permission consented to later is picked up"
    assert len(seen) == calls + 1, "the recheck probes the folder again"


async def test_files_are_off_until_the_permission_is_consented_then_on() -> None:
    """No consent: `filesFolder` is refused, files are off, and the recheck turns them on."""
    now = [0.0]
    consented = [False]

    def folder(request: httpx.Request) -> httpx.Response:
        return _ok(FOLDER_ITEM)(request) if consented[0] else _denied(request)

    files = _files(_sharepoint({("GET", FILES_FOLDER): folder}, []), clock=lambda: now[0])

    assert not await files.is_available(_channel()), "refused: files are off in the channel"
    consented[0] = True
    assert not await files.is_available(_channel()), "still off until the recheck"
    now[0] = RECHECK_S
    assert await files.is_available(_channel()), "on once Graph allows the folder"


@pytest.mark.parametrize("kind", ["private", "shared"])
async def test_a_private_or_shared_channel_saves_to_its_own_files_folder(kind: str) -> None:
    """`filesFolder` names the channel's own site's folder, so no team-site folder is used."""
    seen: list[httpx.Request] = []
    own = {"id": "01PRIVATE", "parentReference": {"driveId": "b!private"}}
    put = "/v1.0/drives/b!private/items/01PRIVATE:/a.csv:/content"
    routes = {
        ("GET", f"/v1.0/teams/{GROUP}/channels/{PRIVATE}/filesFolder"): _ok(own),
        ("PUT", put): _ok(ITEM, 201),
    }
    files = _files(_sharepoint(routes, seen))
    inbound = _channel(channel_id=PRIVATE, channel_type=kind)

    assert await files.is_available(inbound), "its own folder is reachable"
    await files.upload(inbound, "a.csv", b"a")

    assert [r.url.path for r in seen] == [
        f"/v1.0/teams/{GROUP}/channels/{PRIVATE}/filesFolder",
        put,
    ], "the folder is looked up once, then the file goes there"


async def test_access_is_per_channel_not_per_team() -> None:
    """A refused channel neither inherits a working channel's answer nor marks the team off."""
    routes = {
        ("GET", FILES_FOLDER): _ok(FOLDER_ITEM),
        ("GET", f"/v1.0/teams/{GROUP}/channels/{PRIVATE}/filesFolder"): _denied,
    }
    files = _files(_sharepoint(routes, []))

    assert await files.is_available(_channel()), "the standard channel's folder is found"
    assert not await files.is_available(_channel(channel_id=PRIVATE)), "the refused one is off"
    assert await files.is_available(_channel()), "and the team stays on"


async def test_resolve_links_a_file_in_the_channels_folder_and_names_the_rest() -> None:
    """Only the file inside the channel's folder gets a URL; one elsewhere is named only."""
    inside = SharedFile("q3.xlsx", f"{LIBRARY}/Planning/q3.xlsx")
    elsewhere = SharedFile("pay.xlsx", "https://example.sharepoint.com/sites/hr/Docs/pay.xlsx")
    routes = {
        ("GET", FILES_FOLDER): _ok(FOLDER_ITEM),
        **_file_routes(inside.content_url or ""),
        ("GET", _shares(elsewhere.content_url or "")): _ok(
            {"id": "01PAY", "parentReference": {"driveId": "b!hr"}}
        ),
    }
    files = _files(_sharepoint(routes, []))

    media = await files.resolve(
        ChannelMedia(files=(inside, elsewhere)), group_id=GROUP, channel_id=CHANNEL
    )

    linked = dataclasses.replace(inside, download_url=DOWNLOAD_URL, version="{Q3},4")
    assert media.files == (linked, elsewhere), (
        "the channel's file is linked, with its bare version a replace checks; the other, which "
        "its members may not share, is not"
    )


async def test_resolve_links_a_file_read_without_an_etag_with_no_version() -> None:
    """Graph leaving the eTag out costs only the version: the file is still linked."""
    seen: list[httpx.Request] = []
    shared = SharedFile("q3.xlsx", f"{LIBRARY}/Planning/q3.xlsx")
    routes = {("GET", FILES_FOLDER): _ok(FOLDER_ITEM), **_file_routes(shared.content_url or "")}
    item_path = "/v1.0/drives/b!drive-1/items/01Q3"
    routes[("GET", item_path)] = _ok(
        {
            "id": "01Q3",
            "parentReference": {"driveId": "b!drive-1", "id": "01FOLDER"},
            "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
        }
    )

    media = await _files(_sharepoint(routes, seen)).resolve(
        ChannelMedia(files=(shared,)), group_id=GROUP, channel_id=CHANNEL
    )

    assert media.files == (dataclasses.replace(shared, download_url=DOWNLOAD_URL),), (
        "linked as before, with no version to offer a replace"
    )
    assert [r.url.path for r in seen] == [
        FILES_FOLDER,
        _shares(shared.content_url or ""),
        item_path,
    ], "no extra Graph call for the version"


async def test_resolve_marks_shared_files_refused_without_the_permission() -> None:
    """No consent: the folder is refused, so each file is named as refused, never fetched."""
    seen: list[httpx.Request] = []
    shared = SharedFile("q3.xlsx", f"{LIBRARY}/q3.xlsx")
    files = _files(_sharepoint({("GET", FILES_FOLDER): _denied}, seen))

    media = await files.resolve(ChannelMedia(files=(shared,)), group_id=GROUP, channel_id=CHANNEL)

    assert media.files == (dataclasses.replace(shared, refused=True),), "named, never linked"
    assert [r.url.path for r in seen] == [FILES_FOLDER], "no share is looked up"
