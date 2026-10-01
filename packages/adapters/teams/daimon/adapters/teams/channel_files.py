"""Channel files for one deployment: whether they work per channel, uploads, shared-file links.

The shell around `sharepoint.SharePoint`. Each channel's folder is found once.
What it learns per channel is kept: a folder lookup or upload that succeeds
marks the channel's files available; a failed lookup, or an upload refused
(401/403), marks them unavailable for `RECHECK_S`, so a grant an admin adds
later is picked up without a restart. Per channel, not per team: a private or
shared channel's folder is never found, though the team's is.
The turn context shows the agent that answer.
"""

from __future__ import annotations

import dataclasses
import functools
import time
from collections.abc import Awaitable, Callable, Mapping

import structlog
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.graph import GraphUnavailable, TeamGroups
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.sharepoint import DriveFolder, DriveItem, SharePoint

log = structlog.get_logger(__name__)

RECHECK_S = 600.0
# The General channel's Bot Framework id is the team's, and its folder this name.
_GENERAL = "General"

# Bot Framework team id -> {channel id: name}; the General channel's name is None.
ChannelNames = Callable[[str], Awaitable[Mapping[str, str | None]]]


@dataclasses.dataclass(frozen=True)
class _Access:
    is_available: bool
    checked_at: float


class ChannelFiles:
    """Channel folders, uploads and shared-file links, with what each team allows."""

    def __init__(
        self,
        sharepoint: SharePoint,
        teams: TeamGroups,
        channel_names: ChannelNames,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sharepoint = sharepoint
        self._teams = teams
        self._channel_names = channel_names
        self._clock = clock
        self._folders: dict[tuple[str, str], DriveFolder] = {}
        # (group, channel id) -> what the last folder lookup or upload there learned.
        self._access: dict[tuple[str, str], _Access] = {}

    def _known(self, group: str, inbound: TeamsInbound) -> bool | None:
        access = self._access.get((group, inbound.channel_id))
        if access is None or (
            not access.is_available and self._clock() - access.checked_at >= RECHECK_S
        ):
            return None
        return access.is_available

    def _record(self, group: str, inbound: TeamsInbound, *, is_available: bool) -> None:
        self._access[group, inbound.channel_id] = _Access(is_available, self._clock())

    async def _group(self, inbound: TeamsInbound) -> str:
        return await self._teams.group_id(inbound.team_id, known=inbound.team_group_id)

    async def is_available(self, inbound: TeamsInbound) -> bool:
        """Whether a file saved now would reach this channel, as last learned."""
        if inbound.kind != "channel":
            return False
        try:
            group = await self._group(inbound)
        except GraphUnavailable:
            return False
        if (known := self._known(group, inbound)) is not None:
            return known
        try:
            await self._folder(inbound, group)
        except GraphUnavailable as err:
            log.info("teams.channel_files.unavailable", status=err.status, reason=err.reason)
            self._record(group, inbound, is_available=False)
            return False
        self._record(group, inbound, is_available=True)
        return True

    async def upload(self, inbound: TeamsInbound, name: str, content: bytes) -> DriveItem:
        """Save `content` in the channel's folder; `GraphUnavailable` if it cannot go there."""
        group = await self._group(inbound)
        if self._known(group, inbound) is False:
            raise GraphUnavailable("no SharePoint access", status=403)
        try:
            item = await self._sharepoint.upload(await self._folder(inbound, group), name, content)
        except GraphUnavailable as err:
            if err.status in (401, 403):
                self._record(group, inbound, is_available=False)
                self._folders.pop((group, inbound.channel_id), None)
            raise
        self._record(group, inbound, is_available=True)
        return item

    async def resolve(self, media: ChannelMedia, *, group_id: str) -> ChannelMedia:
        """`media` with a download URL on each shared file Graph can reach in the team's site."""
        files: list[SharedFile] = []
        for file in media.files:
            if file.content_url and not file.download_url:
                try:
                    url = await self._sharepoint.download_url(file.content_url, group_id=group_id)
                    file = dataclasses.replace(file, download_url=url)
                except GraphUnavailable as err:
                    log.warning(
                        "teams.channel_file.unreachable", status=err.status, reason=err.reason
                    )
            files.append(file)
        return dataclasses.replace(media, files=tuple(files))

    async def _folder(self, inbound: TeamsInbound, group: str) -> DriveFolder:
        key = (group, inbound.channel_id)
        if (folder := self._folders.get(key)) is None:
            name = functools.partial(self._channel_name, inbound)
            folder = await self._sharepoint.channel_folder(
                group, inbound.channel_id, channel_name=name
            )
            self._folders[key] = folder
        return folder

    async def _channel_name(self, inbound: TeamsInbound) -> str:
        team = inbound.team_id
        if team is None:
            raise GraphUnavailable("no team")
        if inbound.channel_id == team:
            return _GENERAL
        if not (name := (await self._channel_names(team)).get(inbound.channel_id)):
            raise GraphUnavailable("channel not found")
        return name
