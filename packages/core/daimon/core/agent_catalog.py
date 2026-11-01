"""What the agent editor offers to add: skills and channels, as the viewer may see them.

Pure; the caller lists the skills (`list_skills_lenient`) and the channel summary
(`load_tenant_summary`) and passes the rows in.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from anthropic.types.beta.skill_list_response import SkillListResponse
from daimon.core.agent_view import PlaceDoc, SkillDoc
from daimon.core.defaults.metadata import strip_tenant_prefix
from daimon.core.tenant_summary import ChannelSummary


def skills_of(
    rows: Sequence[SkillListResponse], *, tenant_id: uuid.UUID, descriptions: Mapping[str, str]
) -> list[SkillDoc]:
    """The skills an agent here may carry: Anthropic's, and this workspace's own by bare name.

    Another workspace's skills (a title without this tenant's prefix) are left out.
    A description comes from the shipped skills' frontmatter where there is one; the
    body is not fetched.
    """
    out: list[SkillDoc] = []
    for row in rows:
        if row.display_title is None:
            continue
        if row.source == "custom":
            bare = strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title)
            if bare is None:
                continue
            title = bare
        else:
            title = row.display_title
        out.append(
            SkillDoc(
                type=row.source,
                skill_id=row.id,
                title=title,
                description=descriptions.get(title, ""),
                version=row.latest_version or "",
            )
        )
    return sorted(out, key=lambda skill: (skill.type, skill.title))


def channels_of(
    channels: Sequence[ChannelSummary],
    *,
    platform: str,
    names: Mapping[str, str],
    is_admin: bool,
) -> list[PlaceDoc]:
    """The workspace's configured channels; ones kept to their own agents only for admins."""
    return [
        PlaceDoc(
            id=channel.channel_id,
            kind="channel",
            platform=platform,
            name=names.get(channel.channel_id, "Unnamed channel"),
            confidential=True if channel.own_agents_only else None,
        )
        for channel in channels
        if is_admin or not channel.own_agents_only
    ]
