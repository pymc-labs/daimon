"""Channel files for one deployment: whether they work per channel, uploads, shared-file links.

The shell around `sharepoint.SharePoint`. Each channel's folder is found once,
through `filesFolder`. What it learns per channel is kept: a folder lookup or
upload that succeeds marks the channel's files available; a failed lookup, or
an upload refused (401/403), marks them unavailable for `RECHECK_S`, so a
permission an admin consents to later is picked up without a restart. Per
channel, not per team: a private or shared channel's folder is on a site of
its own. The turn context shows the agent that answer.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

import structlog
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.teams_graph import GraphUnavailable, TeamGroups
from daimon.core.teams_sharepoint import (
    DriveFolder,
    DriveItem,
    SharePoint,
    download_url_of,
    version_of,
)

log = structlog.get_logger(__name__)

RECHECK_S = 600.0


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
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sharepoint = sharepoint
        self._teams = teams
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

    async def team_group(self, inbound: TeamsInbound) -> str:
        """The Entra group of the channel's team; `GraphUnavailable` if it cannot be found."""
        return await self._teams.group_id(inbound.team_id, known=inbound.team_group_id)

    async def is_available(self, inbound: TeamsInbound) -> bool:
        """Whether a file saved now would reach this channel, as last learned."""
        if inbound.kind != "channel":
            return False
        try:
            group = await self.team_group(inbound)
        except GraphUnavailable:
            return False
        if (known := self._known(group, inbound)) is not None:
            return known
        try:
            await self._folder(group, inbound.channel_id)
        except GraphUnavailable as err:
            log.info("teams.channel_files.unavailable", status=err.status, reason=err.reason)
            self._record(group, inbound, is_available=False)
            return False
        self._record(group, inbound, is_available=True)
        return True

    async def upload(self, inbound: TeamsInbound, name: str, content: bytes) -> DriveItem:
        """Save `content` in the channel's folder; `GraphUnavailable` if it cannot go there."""
        group = await self.team_group(inbound)
        if self._known(group, inbound) is False:
            raise GraphUnavailable("no SharePoint access", status=403)
        try:
            item = await self._sharepoint.upload(
                await self._folder(group, inbound.channel_id), name, content
            )
        except GraphUnavailable as err:
            if err.status in (401, 403):
                self._record(group, inbound, is_available=False)
                self._folders.pop((group, inbound.channel_id), None)
            raise
        self._record(group, inbound, is_available=True)
        return item

    async def thumbnail_url(self, item: DriveItem) -> str | None:
        """Graph's first-page thumbnail URL of an uploaded `item`; None until it renders one."""
        return await self._sharepoint.thumbnail_url(item)

    async def resolve(self, media: ChannelMedia, *, group_id: str, channel_id: str) -> ChannelMedia:
        """`media` with a download URL and version on each shared file in the channel's Files."""
        if not any(file.content_url and not file.download_url for file in media.files):
            return media
        try:
            folder = await self._folder(group_id, channel_id)
        except GraphUnavailable as err:
            log.warning("teams.channel_file.unreachable", status=err.status, reason=err.reason)
            refused = err.status == 403
            files = tuple(
                dataclasses.replace(f, refused=refused)
                if f.content_url and not f.download_url
                else f
                for f in media.files
            )
            return dataclasses.replace(media, files=files)
        resolved: list[SharedFile] = []
        for file in media.files:
            if file.content_url and not file.download_url:
                try:
                    item = await self._sharepoint.shared_item(file.content_url, folder=folder)
                    file = dataclasses.replace(
                        file, download_url=download_url_of(item), version=version_of(item.e_tag)
                    )
                except GraphUnavailable as err:
                    log.warning(
                        "teams.channel_file.unreachable", status=err.status, reason=err.reason
                    )
                    file = dataclasses.replace(file, refused=err.status == 403)
            resolved.append(file)
        return dataclasses.replace(media, files=tuple(resolved))

    async def _folder(self, group: str, channel_id: str) -> DriveFolder:
        key = (group, channel_id)
        if (folder := self._folders.get(key)) is None:
            folder = self._folders[key] = await self._sharepoint.channel_folder(group, channel_id)
        return folder
