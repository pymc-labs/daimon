"""Files send_message posts on Teams: saved to a channel's Files, offered in a 1:1 chat.

In a channel each file is saved to the channel's Files folder (`filesFolder`)
through Graph under `Files.ReadWrite.All`, as the adapter saves session
outputs, and the message links to it. A bot can put a file in a 1:1 chat only
through a FileConsentCard: the person accepts and the adapter uploads the
staged bytes to their OneDrive (`daimon.core.teams_file_offers`). A group chat
takes neither.

`replace_in_channel` saves a new version over a file already in the channel's
Files folder (`SharePoint.replace`), found by its link as the read tools find
one and only at the version the agent read. A write is never retried, so its
error says whether the file may have been saved: nothing went out, Graph
refused it, or the answer was lost. It is Teams-only: a Slack or
Discord file cannot take a new version, so there a changed file is posted
again (`tests/parity/test_replace_channel_file_teams_only.py`).

The read tools link a message's shared files (`file_links`): Graph's download
URL, only for a file inside the channel's Files folder. The permission reads
past SharePoint's per-item permissions, so a file posted from another site,
library or folder is listed by name only. The URL is pre-authorised for about
an hour, as a Slack read's proxy link lasts the turn grant, and only messages
the read already returns get one, so it shows nothing the caller could not read.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from pathlib import PurePath

import httpx
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._directory import (
    TeamsChannelRef,
    graph_for,
    locate_channel,
    split_thread,
)
from daimon.core.media.filenames import sanitize_title
from daimon.core.stores.domain import FileUploadRow
from daimon.core.teams_file_offers import UploadOffer, consent_attachment
from daimon.core.teams_graph import (
    FILE_ATTACHMENT_TYPES,
    GraphMessage,
    GraphNotSent,
    GraphUnavailable,
    linked_files,
)
from daimon.core.teams_sharepoint import (
    DriveItem,
    SharePoint,
    download_url_of,
    file_link,
    version_of,
)
from fastmcp.exceptions import ToolError

_log = structlog.get_logger(__name__)

MAX_FILES = 10
# Links resolved per read: each costs a Graph call, the rest stay names only.
MAX_LINKS = 20
# A channel's conversation id; a group chat's ends in `@thread.v2`.
CHANNEL = re.compile(r"19:[^;]+@thread\.(?:tacv2|skype)")
_NO_FILES = (
    "daimon cannot save files in that channel: Graph refused it the channel's Files folder. "
    "A Microsoft 365 admin must grant daimon's app the Files.ReadWrite.All application "
    "permission"
)
_LOCKED = (
    "{name} is locked, most likely open in a desktop Office app, so SharePoint kept it as it "
    "was. Ask whoever has it open to close it, or post your version as a new file with "
    "send_message"
)
_CHANGED = (
    "{name} changed since you read it, so it was not overwritten. Download it again from a "
    "fresh read, redo your edit on that copy and replace it with the new version"
)
_REFUSED = (
    "SharePoint refused daimon the write to {name} (HTTP {status}), so nothing was changed: "
    "daimon's app needs the Files.ReadWrite.All application permission, and a retention "
    "policy or a sensitivity label on the file can also block an app's write. Post your "
    "version as a new file with send_message instead"
)
_UNCLEAR = (
    "saving {name} did not finish ({reason}); it may or may not have been saved. Read the "
    "channel for its current version before trying again"
)

Staged = list[tuple[FileUploadRow, bytes]]


async def post_files(
    runtime: McpRuntime,
    auth: AuthIdentity,
    client: TeamsBotClient,
    conversation_id: str,
    content: str,
    staged: Staged,
) -> str:
    """Post `content` with the files; returns the first activity id."""
    if conversation_id.startswith("a:"):
        return await _offer(auth, client, conversation_id, content, staged)
    if CHANNEL.fullmatch(split_thread(conversation_id)[0]):
        return await _save_in_channel(runtime, auth, client, conversation_id, content, staged)
    raise ToolError("Teams takes files in a channel or a 1:1 chat with daimon, not a group chat")


async def _save_in_channel(
    runtime: McpRuntime,
    auth: AuthIdentity,
    client: TeamsBotClient,
    conversation_id: str,
    content: str,
    staged: Staged,
) -> str:
    ref = await locate_channel(runtime, auth, client, split_thread(conversation_id)[0])
    sharepoint = SharePoint(graph_for(client))
    links: list[str] = []
    try:
        folder = await sharepoint.channel_folder(ref.group_id, ref.channel_id)
        for upload, data in staged:
            item = await sharepoint.upload(folder, upload.display_filename, data)
            links.append(
                file_link(sanitize_title(item.name or upload.display_filename), item.web_url)
            )
    except GraphUnavailable as err:
        saved = f" ({len(links)} file(s) were saved to its Files first)" if links else ""
        raise ToolError(f"{_NO_FILES}. Nothing was posted{saved}.") from err
    try:
        return await client.send(conversation_id, "\n\n".join(filter(None, [content, *links])))
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError(
            f"the {len(links)} file(s) were saved to the channel's Files, but posting the "
            f"message failed ({type(err).__name__}); post it again without file_handles, "
            "with these links:\n" + "\n".join(links)
        ) from err


async def replace_in_channel(
    runtime: McpRuntime,
    auth: AuthIdentity,
    client: TeamsBotClient,
    conversation_id: str,
    *,
    link: str,
    version: str,
    upload: FileUploadRow,
    content: bytes,
) -> DriveItem:
    """Save `content` over the channel file at `link` as a new version; the saved item.

    Only a file inside the channel's Files folder, of the same type, and still at
    `version` (quotes aside, `version_of`): a later edit someone made is never
    overwritten. The write carries the eTag just read as `If-Match`.
    """
    ref = await locate_channel(runtime, auth, client, split_thread(conversation_id)[0])
    sharepoint = SharePoint(graph_for(client))
    try:
        folder = await sharepoint.channel_folder(ref.group_id, ref.channel_id)
        item = await sharepoint.shared_item(link, folder=folder)
    except GraphUnavailable as err:
        if err.status in (401, 403):
            raise ToolError(f"{_NO_FILES}. Nothing was changed.") from err
        if err.status is None or err.status >= 500:
            raise ToolError(
                f"looking up that file failed ({err.reason}), so nothing was changed. Try again"
            ) from err
        raise ToolError(
            f"that link is not a file in this channel's Files folder ({err.reason}), so nothing "
            "was changed. Pass the link of a file read from this channel, or post a new file "
            "with send_message"
        ) from err
    name = sanitize_title(item.name or "the file")
    if item.folder is not None:
        raise ToolError(
            f"{name} is a folder, so nothing was changed. Pass the link of the file itself"
        )
    if PurePath(item.name or "").suffix.lower() != PurePath(upload.display_filename).suffix.lower():
        raise ToolError(
            f"{name} cannot be replaced by {upload.display_filename}: the new version must "
            "be the same type of file"
        )
    if version_of(item.e_tag) is None or version_of(item.e_tag) != version_of(version):
        raise ToolError(_CHANGED.format(name=name))
    try:
        return await sharepoint.replace(item, content)
    except GraphNotSent as err:
        raise ToolError(
            f"saving {name} failed before anything was sent ({err.reason}), so nothing was "
            "changed. Try again"
        ) from err
    except GraphUnavailable as err:
        if err.status == 423:
            raise ToolError(_LOCKED.format(name=name)) from err
        if err.status == 412:
            raise ToolError(_CHANGED.format(name=name)) from err
        if err.status in (401, 403):
            raise ToolError(_REFUSED.format(name=name, status=err.status)) from err
        if err.status is None or err.status >= 500:
            # Never retried: the write may have landed before the answer was lost.
            reason = err.reason if err.status is None else f"HTTP {err.status}"
            raise ToolError(_UNCLEAR.format(name=name, reason=reason)) from err
        raise ToolError(
            f"SharePoint did not save {name} (HTTP {err.status}); nothing was changed"
        ) from err


async def _offer(
    auth: AuthIdentity, client: TeamsBotClient, chat: str, content: str, staged: Staged
) -> str:
    user = str(uuid.UUID(auth.platform_user_id or ""))
    ids = [await client.send(chat, content)] if content.strip() else []
    for upload, data in staged:
        token = client.file_offer_token(UploadOffer(upload.id, user, chat))
        card = consent_attachment(upload.display_filename, len(data), token)
        try:
            ids.append(await client.send_activity(chat, {"type": "message", "attachments": [card]}))
        except (httpx.HTTPError, ValueError) as err:
            raise ToolError(
                f"offering {upload.display_filename!r} failed after {len(ids)} message(s) "
                f"({type(err).__name__})"
            ) from err
    return ids[0]


async def file_links(
    client: TeamsBotClient,
    ref: TeamsChannelRef,
    messages: Iterable[GraphMessage],
) -> dict[str, DriveItem]:
    """The messages' files inside the channel's folder, with a download URL, by content URL.

    Both shared files and the SharePoint links of a bot's message (`linked_files`),
    so the agent can open what it delivered earlier.
    """
    wanted = list(
        dict.fromkeys(
            url
            for m in messages
            for url in (
                *(
                    a.content_url
                    for a in m.attachments
                    if a.content_type in FILE_ATTACHMENT_TYPES and a.content_url
                ),
                *(link for _, link in linked_files(m)),
            )
        )
    )[:MAX_LINKS]
    if not wanted:
        return {}
    sharepoint = SharePoint(graph_for(client))
    try:
        folder = await sharepoint.channel_folder(ref.group_id, ref.channel_id)
    except GraphUnavailable as err:
        _log.info("teams.file_link.unresolved", status=err.status, reason=err.reason)
        return {}
    links: dict[str, DriveItem] = {}
    for url in wanted:
        try:
            item = await sharepoint.shared_item(url, folder=folder)
            download_url_of(item)  # Linked only with a download URL on SharePoint.
            links[url] = item
        except GraphUnavailable as err:
            _log.info("teams.file_link.unresolved", status=err.status, reason=err.reason)
            if err.status in (401, 403):
                break  # The permission is not granted: no other file would resolve.
    return links
