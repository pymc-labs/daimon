"""Files on Slack messages, surfaced through the signed file proxy.

A Slack ``url_private`` needs the workspace bot token, so handing it to the
agent gives it nothing it can fetch. The Slack adapter solves this for the
turn context by minting a signed, expiring token and pointing the agent at
the MCP ``/slack/file/{token}`` route, which re-authenticates and streams
the bytes. The read tools mint the same URLs here, with the same signer
(``daimon.core.slack_file_token``) and the same secret the route verifies
with, so a file the agent sees in ``read_thread`` is reachable exactly the
way a file attached to the mention is.

When the deployment has no public URL or no signing secret, the route is not
mounted and no URL can be minted. Files are still listed, with ``url`` unset,
so the agent knows a file exists rather than seeing a message with only text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, cast

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy
from daimon.adapters.mcp.tools.slack._leak_policy import TURN_CONTEXT_TTL, is_dm_destination
from daimon.adapters.mcp.tools.slack._models import SlackFileRow
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.core.slack_file_token import (
    SlackFileRef,
    mint_file_token,
    verify_file_token,
)
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

_FILE_PATH = "/slack/file/"

# Anyone holding a link can fetch the bytes, and no check made when it was
# minted applies to the fetch. A read can surface files from private channels,
# group DMs and sealed threads, so its links go stale with the turn grant
# rather than lasting the 24 hours a turn's own attachments get.
_READ_URL_TTL_S = int(TURN_CONTEXT_TTL.total_seconds())

_SOURCE_REFUSED_MSG = (
    "that file is not shared anywhere the requester can read from here — a file "
    "from a sealed thread or a 1:1 DM can only be used where it was shared"
)

# Slack keeps a placeholder entry for a deleted file or one hidden by the
# workspace's retention limit; neither has bytes behind it.
_UNREADABLE_MODES = frozenset({"tombstone", "hidden_by_limit"})


@dataclass(frozen=True)
class FileUrlMinter:
    base_url: str
    secret: str
    team_id: str
    now: int

    def url(self, file_id: str) -> str:
        token = mint_file_token(
            team_id=self.team_id,
            file_id=file_id,
            exp=self.now + _READ_URL_TTL_S,
            secret=self.secret,
        )
        return f"{self.base_url.rstrip('/')}{_FILE_PATH}{token}"


def file_url_minter(runtime: McpRuntime, *, team_id: str, now: int) -> FileUrlMinter | None:
    """The minter for this deployment, or None when the proxy route is not mounted."""
    base_url = runtime.settings.mcp.app_root_url
    secret = runtime.settings.mcp.jwt_secret
    if base_url is None or secret is None:
        return None
    return FileUrlMinter(
        base_url=base_url, secret=secret.get_secret_value(), team_id=team_id, now=now
    )


FileLinkRefusal = Literal["unconfigured", "not_a_link", "rejected"]


def resolve_file_link(
    runtime: McpRuntime, url: str, *, team_id: str
) -> SlackFileRef | FileLinkRefusal:
    """The file a link minted by this deployment names, or why it names none.

    ``rejected`` covers a bad signature, an expired link and another
    workspace's file alike: the caller's fix for each is a fresh read.
    """
    base_url = runtime.settings.mcp.app_root_url
    secret = runtime.settings.mcp.jwt_secret
    if base_url is None or secret is None:
        return "unconfigured"
    prefix = f"{base_url.rstrip('/')}{_FILE_PATH}"
    if not url.startswith(prefix):
        return "not_a_link"
    token = url.removeprefix(prefix).split("?", 1)[0]
    ref = verify_file_token(token, secret=secret.get_secret_value(), now=int(time.time()))
    if ref is None or ref.team_id != team_id:
        return "rejected"
    return ref


def to_file_rows(
    raw_files: list[dict[str, Any]], minter: FileUrlMinter | None
) -> list[SlackFileRow]:
    rows: list[SlackFileRow] = []
    for f in raw_files:
        file_id = f.get("id")
        if not file_id or f.get("mode") in _UNREADABLE_MODES:
            continue
        size = f.get("size")
        rows.append(
            SlackFileRow(
                id=str(file_id),
                name=str(f.get("name") or f.get("title") or "file"),
                mimetype=str(f.get("mimetype") or "unknown"),
                size=int(size) if size is not None else None,
                url=minter.url(str(file_id)) if minter is not None else None,
            )
        )
    return rows


async def require_file_source(
    client: AsyncWebClient,
    *,
    file_id: str,
    requester_id: str,
    read_policy: ChannelReadPolicy,
    dm_ok: bool,
    in_place: tuple[str, str | None] | None = None,
) -> None:
    """Refuse a linked file the requester cannot read where Slack shows it.

    A file link is a bearer token: it proves some turn in this workspace saw
    the file, not that this requester may read it or that the channel policy
    lets this call read it, so every caller that turns a link into bytes
    checks here first. One share passing both checks is enough. A share at
    ``in_place`` (channel, thread) passes outright, since using the file there
    shows it to nobody new. A share in a 1:1 DM counts only when ``dm_ok``,
    as the leak policy keeps DM content in DMs with daimon.
    """
    try:
        info = await client.files_info(file=file_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except SlackApiError as err:
        code = str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        raise ToolError(f"cannot read file {file_id!r} ({code})") from err
    file_obj = cast(dict[str, Any], info["file"])
    shares = cast(dict[str, dict[str, list[dict[str, Any]]]], file_obj.get("shares") or {})
    viewable: dict[str, bool] = {}
    for by_channel in (shares.get("public") or {}, shares.get("private") or {}):
        for channel_id, entries in by_channel.items():
            for entry in entries:
                thread_ts = str(entry.get("thread_ts") or entry.get("ts") or "")
                if (channel_id, thread_ts) == in_place:
                    return
                if is_dm_destination(channel_id) and not dm_ok:
                    continue
                if not read_policy.allows(f"{channel_id}:{thread_ts}", channel_id):
                    continue
                if channel_id not in viewable:
                    viewable[channel_id] = await _can_view(
                        client, channel_id=channel_id, requester_id=requester_id
                    )
                if viewable[channel_id]:
                    return
    raise ToolError(_SOURCE_REFUSED_MSG)


async def _can_view(client: AsyncWebClient, *, channel_id: str, requester_id: str) -> bool:
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel = cast(dict[str, Any], info["channel"])
        await check_channel_access(client, channel=channel, user_id=requester_id, allow_own_im=True)
    except (ToolError, SlackApiError):
        return False
    return True
