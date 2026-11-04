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
from mux.contracts.ids import Revision, SkillRef
from mux.errors import UnsupportedCapability
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["Consented", "ConsentKeeper", "Renewer", "repin_residue", "with_skill_version"]


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


_REPIN_MARKS = frozenset({"version", "updated_at", "skills"})


def repin_residue(source: BackendAgent, skill_id: str) -> str:
    """The source's pin with only a repin's own marks blanked, so a repin keeps it.

    A repin moves the skill's version, the agent's revision and timestamps, and the
    digest of its whole record; any other edit changes the residue.
    """
    from daimon.core.turn.runtime_manifest import source_preservation_pin

    update: dict[str, object] = {}
    if (native := source.source) is not None and isinstance(raw := native.native, dict):
        # The record digest covers the native record, so it is blanked only alongside it:
        # an unreadable record keeps both, and any repin then changes the residue.
        update["configuration_digest"] = "repin"
        update["source"] = native.model_copy(
            update={
                "revision": Revision(local=0),
                "updated_at": None,
                "native": {k: v for k, v in raw.items() if k not in _REPIN_MARKS},
            }
        )
    blanked = with_skill_version(source.model_copy(update=update), skill_id, "")
    return source_preservation_pin(blanked)


@dataclass(frozen=True)
class Consented:
    """A consent to renew after a repin, and the source it vouches for."""

    revision: AgentBackendRevision
    residue: str


@dataclass(frozen=True)
class ConsentKeeper:
    """Checks a follower's consent before its repin and renews it after."""

    sessionmaker: async_sessionmaker[AsyncSession]
    renewer: Callable[[ServerSkillRow], Awaitable[Renewer | None]]
    """The mirror's renewer; None when no one can vouch for it."""
    load: Callable[[ServerSkillRow, str], Awaitable[BackendAgent]]
    """The agent, by its provider id, as the backend service reads it."""

    async def check(self, mirror: ServerSkillRow, agent_id: str, version: str) -> Consented | None:
        """The consent to renew after the repin, or None when there is none.

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
            kept = await renewer.backends.source_keeping_drops(
                agent,
                revision,
                lambda captured: with_skill_version(captured, mirror.skill_id, version),
            )
        except (AgentBackendRefused, UnsupportedCapability) as err:
            raise NeedsReconfirm(f"its source can't be read ({type(err).__name__})") from err
        if kept is None:
            raise NeedsReconfirm("its backend would drop different capabilities")
        return Consented(revision, repin_residue(kept, mirror.skill_id))

    async def renew(self, mirror: ServerSkillRow, agent_id: str, consented: Consented) -> None:
        """Renew the consent for the repinned source; `NeedsReconfirm` if it no longer holds.

        Any edit besides the repin since the check, by anyone, leaves it for the owner.
        """
        renewer = await self.renewer(mirror)
        if renewer is None:
            raise NeedsReconfirm("no one can renew its backend consent")
        agent = await self.load(mirror, agent_id)
        try:
            renewed = await renewer.backends.renew_consent(
                agent,
                expected=consented.revision,
                # The sync renews, not a person; the adder only lends their read rights.
                account_id=None,
                reason=(
                    f"actor=server_skill_sync; added_by={renewer.account_id}; "
                    f"agent={agent_id}; skill={mirror.skill_id}; digest={mirror.digest}"
                ),
                vouch=lambda captured: (
                    repin_residue(captured, mirror.skill_id) == consented.residue
                ),
            )
        except (AgentBackendRefused, UnsupportedCapability) as err:
            raise NeedsReconfirm(f"its source can't be read ({type(err).__name__})") from err
        if renewed is None:
            raise NeedsReconfirm("its source or its drops changed since the check")
