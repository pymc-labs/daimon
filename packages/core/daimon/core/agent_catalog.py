"""What the agent editor offers to add: models, skills and channels, as the viewer may see them.

Pure; the caller lists the skills (`list_skills_lenient`), the channel summary
(`load_tenant_summary`) and the providers the workspace may use, and passes them in.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence

from anthropic.types.beta.skill_list_response import SkillListResponse
from daimon.core.agent_model_names import AgentModel, model_name
from daimon.core.agent_view import PlaceDoc, SkillDoc
from daimon.core.constants import MODEL_DISPLAY_NAMES
from daimon.core.defaults.metadata import strip_tenant_prefix
from daimon.core.platform_names import UNNAMED_CHANNEL
from daimon.core.tenant_summary import ChannelSummary
from mux.contracts.ids import Provider
from pydantic import BaseModel

_FAST = "-fast"


class CatalogModel(BaseModel):
    id: str
    label: str
    backend: Provider
    in_usd: float
    out_usd: float
    # A fast variant of the model exists; the editor offers it as a switch.
    fast: bool = False


def models_of(
    entries: Sequence[AgentModel], *, providers: Collection[Provider], priced: Collection[str]
) -> list[CatalogModel]:
    """Every runnable model on a provider the workspace may use, in catalog order.

    `priced` is every metered Anthropic id: a model with a `<id>-fast` row there is marked
    fast instead of being listed twice.
    """
    return [
        CatalogModel(
            id=entry.model.id,
            label=MODEL_DISPLAY_NAMES.get(entry.model.id) or model_name(entry.model),
            backend=entry.model.provider,
            in_usd=entry.input_usd,
            out_usd=entry.output_usd,
            fast=entry.model.id + _FAST in priced,
        )
        for entry in entries
        if entry.model.provider in providers and not entry.model.id.endswith(_FAST)
    ]


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
    gone: Collection[str] = frozenset(),
    is_admin: bool,
) -> list[PlaceDoc]:
    """The workspace's configured channels; ones kept to their own agents only for admins.

    A channel deleted on the platform is left out: its settings stay, but nothing can run there.
    """
    return [
        PlaceDoc(
            id=channel.channel_id,
            kind="channel",
            platform=platform,
            name=names.get(channel.channel_id, UNNAMED_CHANNEL),
            confidential=True if channel.own_agents_only else None,
        )
        for channel in channels
        if (is_admin or not channel.own_agents_only) and channel.channel_id not in gone
    ]
