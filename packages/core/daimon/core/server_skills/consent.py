"""Carry an agent's backend consent across a followed skill update, or hold the update.

An agent on another provider runs under a consent pinned to its exact source, so a
repin would otherwise leave it refusing turns. The consent is renewed only when the
updated source declares exactly the drops the owner accepted; else nothing moves.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from daimon.core.channel_backend import AgentBackendRefused, AgentBackendService
from daimon.core.server_skills.sync import NeedsReconfirm
from daimon.core.stores import agent_backends
from daimon.core.stores.domain import AgentBackendRevision, BackendAgent, ServerSkillRow
from mux.contracts.ids import SkillRef
from mux.errors import UnsupportedCapability
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["ConsentKeeper", "Renewer", "with_skill_version"]


@dataclass(frozen=True)
class Renewer:
    """Who vouches for a mirror's renewals: its service captures sources as them."""

    backends: AgentBackendService
    account_id: uuid.UUID


def _swap(
    skills: tuple[SkillRef, ...] | None, skill_id: str, version: str
) -> tuple[SkillRef, ...] | None:
    if skills is None:
        return None
    return tuple(
        s.model_copy(update={"version": version, "digest": None}) if s.id == skill_id else s
        for s in skills
    )


def with_skill_version(source: BackendAgent, skill_id: str, version: str) -> BackendAgent:
    """The captured source as it reads once the skill is pinned to `version`."""
    update: dict[str, object] = {}
    if source.effective_spec is not None:
        spec = source.effective_spec
        update["effective_spec"] = spec.model_copy(
            update={"skills": _swap(spec.skills, skill_id, version)}
        )
    if source.source is not None:
        native = source.source
        update["source"] = native.model_copy(
            update={
                "spec": native.spec.model_copy(
                    update={"skills": _swap(native.spec.skills, skill_id, version)}
                )
            }
        )
    return source.model_copy(update=update)


@dataclass(frozen=True)
class ConsentKeeper:
    """Checks a follower's consent before its repin and renews it after."""

    sessionmaker: async_sessionmaker[AsyncSession]
    renewer: Callable[[ServerSkillRow], Awaitable[Renewer | None]]
    """The mirror's renewer; None when no one can vouch for it."""
    load: Callable[[ServerSkillRow, str], Awaitable[BackendAgent]]
    """The agent, by its provider id, as the backend service reads it."""

    async def check(
        self, mirror: ServerSkillRow, agent_id: str, version: str
    ) -> AgentBackendRevision | None:
        """The consent revision to renew after the repin, or None when there is none.

        Raises `NeedsReconfirm` when the consent would not hold after the update.
        """
        renewer = await self.renewer(mirror)
        agent = await self.load(mirror, agent_id)
        if renewer is None:
            async with self.sessionmaker() as session:
                revision = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
            if revision is not None and revision.drop_consent is not None:
                raise NeedsReconfirm("no one can renew its backend consent")
            return None
        revision = await renewer.backends.current(agent)
        if revision is None or revision.drop_consent is None:
            return None
        try:
            kept = await renewer.backends.keeps_drops(
                agent,
                revision,
                lambda captured: with_skill_version(captured, mirror.skill_id, version),
            )
        except (AgentBackendRefused, UnsupportedCapability) as err:
            raise NeedsReconfirm(f"its source can't be read ({type(err).__name__})") from err
        if not kept:
            raise NeedsReconfirm("its backend would drop different capabilities")
        return revision

    async def renew(
        self, mirror: ServerSkillRow, agent_id: str, revision: AgentBackendRevision
    ) -> None:
        """Renew the consent for the repinned source; `NeedsReconfirm` if it no longer holds."""
        renewer = await self.renewer(mirror)
        if renewer is None:
            raise NeedsReconfirm("no one can renew its backend consent")
        agent = await self.load(mirror, agent_id)
        try:
            renewed = await renewer.backends.renew_consent(
                agent,
                expected=revision,
                account_id=renewer.account_id,
                reason=f"server skill update; skill={mirror.skill_id}; digest={mirror.digest}",
            )
        except (AgentBackendRefused, UnsupportedCapability) as err:
            raise NeedsReconfirm(f"its source can't be read ({type(err).__name__})") from err
        if renewed is None:
            raise NeedsReconfirm("its backend would drop different capabilities")
