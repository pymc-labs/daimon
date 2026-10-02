"""Files send_message posts on Teams: saved to a channel's Files, offered in a 1:1 chat.

In a channel each file is saved to the channel's Files folder through Graph
under `Sites.Selected`, as the adapter saves session outputs, and the message
links to it. A bot can put a file in a 1:1 chat only through a
FileConsentCard: the person accepts and the adapter uploads the staged bytes
to their OneDrive (`daimon.core.teams_file_offers`). A group chat takes neither.
"""

from __future__ import annotations

import re
import uuid

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._directory import graph_for, locate_channel, split_thread
from daimon.core.media.filenames import sanitize_title
from daimon.core.stores.domain import FileUploadRow
from daimon.core.teams_file_offers import UploadOffer, consent_attachment
from daimon.core.teams_graph import GraphUnavailable
from daimon.core.teams_sharepoint import SharePoint, file_link
from fastmcp.exceptions import ToolError

MAX_FILES = 10
# A channel's conversation id; a group chat's ends in `@thread.v2`.
CHANNEL = re.compile(r"19:[^;]+@thread\.(?:tacv2|skype)")
_NO_FILES = (
    "daimon cannot save files in that channel: its team's SharePoint site is not granted "
    "to daimon, or it is a private or shared channel. Nothing was posted"
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

    async def channel_name() -> str:
        return ref.channel_name

    sharepoint = SharePoint(graph_for(client), client.http)
    links: list[str] = []
    try:
        folder = await sharepoint.channel_folder(
            ref.group_id, ref.channel_id, channel_name=channel_name
        )
        for upload, data in staged:
            item = await sharepoint.upload(folder, upload.display_filename, data)
            links.append(
                file_link(sanitize_title(item.name or upload.display_filename), item.web_url)
            )
    except GraphUnavailable as err:
        saved = f" ({len(links)} file(s) were saved to its Files first)" if links else ""
        raise ToolError(f"{_NO_FILES}{saved}.") from err
    return await client.send(conversation_id, "\n\n".join(filter(None, [content, *links])))


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
