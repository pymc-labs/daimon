"""Convert an agent between Managed Agents and a Daimon-owned record, either way.

Convert by delete: the new agent is created under the same name, every row keyed
by the old one moves to it in one transaction (`stores.agent_rekey`), then the old
one is archived through the raw port. Lifecycle archive is never used, because its
`forget_archived_agent` drops rules by name and would strip the new agent's.
Each step is safe to repeat, so a re-run after a crash finishes the move.
"""

from __future__ import annotations

import uuid
import zipfile
from dataclasses import dataclass
from uuid import uuid4

import structlog
from anthropic import AnthropicError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsURLMCPServerParams
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import strip_credentialed_mcp_servers
from daimon.core.agent_ownership import agent_name_lock
from daimon.core.agent_record_ports import DAIMON_ID_PREFIXES
from daimon.core.anthropic_guard import require_anthropic
from daimon.core.authz import Subject
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.ma_index import download_skill_version, find_agents_by_daimon_tag
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CONVERTED_FROM,
    tenant_scoped_display_title,
)
from daimon.core.defaults.owned import create_owned_record, keyless_provider, owned_scope
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import create_agent, legacy_call, retrieve_agent, source_agent_record
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import SkillIngestError, bundle_from_files, retained_archive_files
from daimon.core.specs import merge_default_agent_toolset
from daimon.core.stores import thread_models
from daimon.core.stores.agent_rekey import open_agent_work, rekey_agent
from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.turn.session_skills import latest_version
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.errors import MuxError
from mux.contracts.ids import ModelRef, Provider, Scope, SkillRef
from mux.contracts.resources import Agent, AgentPatch
from mux.errors import ProviderError
from mux.profiles import selectable_models
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


_NO_PERSON = "not copied: no person ran the move"
_SKILL_ERRORS = (AnthropicError, DaimonError, MuxError, ValueError, zipfile.BadZipFile)


@dataclass(frozen=True)
class ConvertedAgent:
    """The new agent, the skills it carries over, and the ones left off with why."""

    agent: BetaManagedAgentsAgent
    dropped_skills: tuple[str, ...]
    copied_skills: tuple[str, ...] = ()
    kept_skills: tuple[str, ...] = ()
    """Skills an earlier run of the same move already copied."""


def skill_receipt(converted: ConvertedAgent) -> tuple[str, ...]:
    """The lines telling the person which skills came along and which did not."""
    lines: list[str] = []
    if converted.copied_skills:
        lines.append(f"Skills copied: {', '.join(converted.copied_skills)}.")
    if converted.kept_skills:
        lines.append(f"Skills already copied: {', '.join(converted.kept_skills)}.")
    if converted.dropped_skills:
        lines.append(f"Skills left off: {', '.join(converted.dropped_skills)}.")
    return tuple(lines)


async def convert_agent_to_daimon(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subject: Subject,
    provider: Provider | None = None,
    account_id: uuid.UUID | None = None,
) -> ConvertedAgent:
    """Move the MA agent `name` to a Daimon record on `provider` (default: first keyed).

    Custom skills are copied into the records catalog as `account_id`'s, as if that
    person had added them; with no person they are only listed.
    """
    if not subject.is_admin:
        raise UserFacingError("Only an admin can convert an agent.")
    require_anthropic(anthropic)
    target = provider or keyless_provider()
    async with agent_name_lock(sessionmaker, tenant_id=tenant_id, name=name):
        tagged = await find_agents_by_daimon_tag(
            anthropic, tenant_id=tenant_id, name=name, records=sessionmaker
        )
        # A record from a crashed run shares the name; the MA agent is still the source.
        source = next((a for a in tagged if not a.id.startswith(DAIMON_ID_PREFIXES)), None)
        if source is None:
            if tagged:
                raise UserFacingError(f"{name} already runs on Daimon.")
            raise UserFacingError(f"No agent named {name}.")
        await _refuse_open_work(sessionmaker, tenant_id=tenant_id, name=name, agent_id=source.id)
        scope = resource_scope(tenant_id=str(tenant_id))
        projected = source_agent_record(source, scope)
        if projected is None:
            raise UserFacingError(f"{name} can't be converted.")
        spec = projected.spec
        servers, _ = await strip_credentialed_mcp_servers(
            sessionmaker=sessionmaker,
            tenant_id=tenant_id,
            source_agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=source.id),
            mcp_servers=[
                {"type": "url", "name": s.name, "url": s.url} for s in spec.mcp_servers or ()
            ],
            tools=None,
        )
        kept = {str(server["name"]) for server in servers or ()}
        record = spec.model_copy(
            update={
                "model": ModelRef(provider=target, id=selectable_models(target)[0]),
                "mcp_servers": tuple(s for s in spec.mcp_servers or () if s.name in kept) or None,
                # The target host supplies its own tools; Claude skills and model
                # config have no meaning there.
                "tools": None,
                "skills": None,
                "extensions": None,
                # Lets a token signed for the old id keep working (see the MCP verifier).
                "metadata": dict(spec.metadata or {}) | {MA_METADATA_KEY_CONVERTED_FROM: source.id},
            }
        )
        created = await create_owned_record(
            sessionmaker,
            type(spec).model_validate(record.model_dump(mode="json")),
            scope=owned_scope(tenant_id, None),
            tenant_id=tenant_id,
            key=f"convert:{source.id}",
        )
        copied, kept, dropped = await _copy_skills_to_record(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            name=name,
            source=source,
            record_id=created.id,
            account_id=account_id,
        )
        await _move_rows(
            sessionmaker, tenant_id=tenant_id, name=name, old_id=source.id, new_id=created.id
        )
        backend = managed_agents(
            anthropic, scope=scope, resources=frozenset({("agent", source.id)})
        )
        await legacy_call(
            backend.agents.archive(
                scope, resource_ref(backend, "agent", source.id, scope=scope), key=str(uuid4())
            )
        )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=name,
        metadata=created.metadata,
        default_agent_name=None,
    )
    _log.info(
        "agent.converted", name=name, source_id=source.id, agent_id=created.id, provider=target
    )
    return ConvertedAgent(created, dropped, copied, kept)


async def convert_agent_to_anthropic(
    anthropic: AsyncAnthropic | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subject: Subject,
    public_url: str | None,
    account_id: uuid.UUID | None = None,
) -> ConvertedAgent:
    """Move the Daimon record `name` back to a Managed Agents agent on Claude.

    The new agent carries a marker naming the record, so a re-run after a crash
    adopts it instead of creating a second one. The record's catalog skills are
    added to it as `account_id`'s; with no person they are only listed.
    """
    if not subject.is_admin:
        raise UserFacingError("Only an admin can convert an agent.")
    require_anthropic(anthropic)
    scope = resource_scope(tenant_id=str(tenant_id))
    async with agent_name_lock(sessionmaker, tenant_id=tenant_id, name=name):
        try:
            record = await SelfHostedAgents(sessionmaker).resolve(scope, name)
        except ProviderError as exc:
            if exc.category != "not_found":
                raise
            raise UserFacingError(f"No Daimon agent named {name}.") from exc
        await _refuse_open_work(
            sessionmaker, tenant_id=tenant_id, name=name, agent_id=record.ref.id
        )
        tagged = await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=name)
        created = next(
            (a for a in tagged if a.metadata.get(MA_METADATA_KEY_CONVERTED_FROM) == record.ref.id),
            None,
        )
        if created is None:
            created = await create_agent(
                anthropic, _anthropic_payload(record, public_url=public_url), scope=scope
            )
        assert anthropic is not None  # require_anthropic refused a missing client
        copied, dropped = await _copy_skills_to_claude(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            name=name,
            record=record,
            created=created,
            account_id=account_id,
        )
        await _move_rows(
            sessionmaker, tenant_id=tenant_id, name=name, old_id=record.ref.id, new_id=created.id
        )
        records, writer = SelfHostedAgents(sessionmaker), owned_scope(tenant_id, None)
        await records.archive(
            writer, records.record_ref(writer, record.ref.id), key=f"convert:{record.ref.id}"
        )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=name,
        metadata=created.metadata,
        default_agent_name=None,
    )
    _log.info(
        "agent.converted",
        name=name,
        source_id=record.ref.id,
        agent_id=created.id,
        provider=DEFAULT_BACKEND,
    )
    return ConvertedAgent(created, dropped, copied)


async def _copy_skills_to_record(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    source: BetaManagedAgentsAgent,
    record_id: str,
    account_id: uuid.UUID | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Copy `source`'s custom skills into the catalog and pin them: (copied, kept, dropped).

    A skill the record already pins with a stored copy (an earlier run's) is kept as is.
    """
    dropped: list[str] = []
    custom: list[tuple[str, str]] = []
    for skill in source.skills or ():
        if skill.type != "custom":
            dropped.append(f"{skill.skill_id} (built in)")
        elif account_id is None:
            dropped.append(f"{skill.skill_id} ({_NO_PERSON})")
        else:
            custom.append((skill.skill_id, skill.version))
    if account_id is None or not custom:
        return (), (), tuple(dropped)
    person = resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
    catalog = PostgresNeutralCatalog(
        sessionmaker, provider="selfhosted", account_scope_id=str(tenant_id)
    )
    records = SelfHostedAgents(sessionmaker)
    record = await records.retrieve(person, records.record_ref(person, record_id))
    copied: dict[str, SkillRef] = {}
    kept: list[str] = []
    for skill_id, version in custom:
        held = await _held_copy(catalog, person, record, skill_id)
        if held is not None:
            kept.append(held)
            continue
        outcome = await _copy_one_to_catalog(
            anthropic, catalog, person, name=name, skill_id=skill_id, version=version
        )
        if isinstance(outcome, str):
            dropped.append(f"{skill_id} ({outcome})")
        else:
            copied[outcome[0]] = outcome[1]
    if not copied:
        return (), tuple(kept), tuple(dropped)
    try:
        await _pin_record_skills(sessionmaker, person, record_id, tuple(copied.values()))
    except _SKILL_ERRORS as error:
        _log.warning("agent.convert_skills_unpinned", name=name, error=type(error).__name__)
        return (), tuple(kept), (*dropped, *(f"{skill} (couldn't be added)" for skill in copied))
    return tuple(copied), tuple(kept), tuple(dropped)


async def _held_copy(
    catalog: PostgresNeutralCatalog, person: Scope, record: Agent, skill_id: str
) -> str | None:
    """The name of `skill_id`'s copy the record already pins and the catalog holds."""
    for pin in record.spec.skills or ():
        if pin.id != skill_id:
            continue
        try:
            stored = await catalog.get_agent_skill(person, record, pin)
        except _SKILL_ERRORS as error:
            _log.warning(
                "agent.convert_skill_unread", skill_id=skill_id, error=type(error).__name__
            )
            continue
        if stored is not None:
            return stored.bundle.preview.name
    return None


async def _copy_one_to_catalog(
    anthropic: AsyncAnthropic,
    catalog: PostgresNeutralCatalog,
    person: Scope,
    *,
    name: str,
    skill_id: str,
    version: str,
) -> tuple[str, SkillRef] | str:
    """Copy one MA skill as `person`'s: (skill name, pin), or why it was left off."""
    service = resource_scope(tenant_id=person.tenant_id)
    try:
        if version in ("", "latest"):
            version = await latest_version(anthropic, service, SkillRef(id=skill_id))
        data = await download_skill_version(
            anthropic, skill_id=skill_id, version=version, scope=service
        )
    except _SKILL_ERRORS as error:
        _log.warning("agent.convert_skill_skipped", skill_id=skill_id, error=type(error).__name__)
        return "couldn't be downloaded"
    try:
        bundle = bundle_from_files(retained_archive_files(data))
    except (SkillIngestError, zipfile.BadZipFile) as error:
        _log.warning("agent.convert_skill_skipped", skill_id=skill_id, error=type(error).__name__)
        return "too large" if "at most" in str(error) else "couldn't be read"
    try:
        await catalog.put_skill(
            person,
            skill_id=skill_id,
            version=version,
            agent_name=name,
            display_title=tenant_scoped_display_title(
                tenant_id=uuid.UUID(person.tenant_id), name=bundle.preview.name, agent_name=name
            ),
            bundle=bundle,
        )
    except _SKILL_ERRORS as error:
        _log.warning("agent.convert_skill_skipped", skill_id=skill_id, error=type(error).__name__)
        return "couldn't be copied"
    return bundle.preview.name, SkillRef(id=skill_id, version=version, source="custom")


async def _pin_record_skills(
    sessionmaker: async_sessionmaker[AsyncSession],
    person: Scope,
    record_id: str,
    pins: tuple[SkillRef, ...],
) -> None:
    """Pin `pins` on the record as `person`, so the catalog ties them to that person.

    A pin replaces any other version of the same skill: one pin per skill. A pin
    the record already holds, whose copy the catalog could not read (its first
    owner is gone), is dropped and pinned again, so the attachment is made anew as
    `person`'s; the store keeps an unchanged pin's old attachment.
    """
    records = SelfHostedAgents(sessionmaker)
    ref = records.record_ref(person, record_id)
    current = await records.retrieve(person, ref)
    held = current.spec.skills or ()
    ids = {pin.id for pin in pins}
    skills = (*(pin for pin in held if pin.id not in ids), *pins)
    revision = current.revision
    if len(skills) == len(held) and all(pin in held for pin in pins):
        unpinned = await records.update(
            person,
            ref,
            AgentPatch(skills=tuple(pin for pin in held if pin not in pins)),
            expected=revision,
            key=f"convert-skills-unpin:{record_id}:{revision.local}",
        )
        revision = unpinned.revision
    await records.update(
        person,
        ref,
        AgentPatch(skills=skills),
        expected=revision,
        key=f"convert-skills:{record_id}:{revision.local}",
    )


async def _no_recheck(_agent: BetaManagedAgentsAgent) -> None:
    """The move was authorized as a whole and no one reaches the new agent yet."""


async def _copy_skills_to_claude(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    record: Agent,
    created: BetaManagedAgentsAgent,
    account_id: uuid.UUID | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Add the record's catalog skills to `created` as `account_id`'s: (copied, dropped)."""
    copied: list[str] = []
    dropped: list[str] = []
    catalog = PostgresNeutralCatalog(
        sessionmaker, provider="selfhosted", account_scope_id=str(tenant_id)
    )
    service = resource_scope(tenant_id=str(tenant_id))
    for ref in record.spec.skills or ():
        if ref.version is None:
            dropped.append(f"{ref.id} (built in)")
            continue
        if account_id is None:
            dropped.append(f"{ref.id} ({_NO_PERSON})")
            continue
        person = resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
        try:
            stored = await catalog.get_agent_skill(person, record, ref)
        except _SKILL_ERRORS as error:
            _log.warning("agent.convert_skill_skipped", skill_id=ref.id, error=type(error).__name__)
            stored = None
        if stored is None:
            dropped.append(f"{ref.id} (couldn't be found)")
            continue
        try:
            added = await add_agent_skill(
                anthropic,
                sessionmaker,
                tenant_id=tenant_id,
                agent=await retrieve_agent(anthropic, created.id, scope=service),
                agent_name=name,
                bundle=stored.bundle,
                origin=f"moved from {name} on Daimon",
                added_by_account_id=account_id,
                recheck=_no_recheck,
            )
        except _SKILL_ERRORS as error:
            _log.warning("agent.convert_skill_skipped", skill_id=ref.id, error=type(error).__name__)
            dropped.append(f"{stored.bundle.preview.name} (couldn't be added)")
            continue
        copied.append(added.name)
    return tuple(copied), tuple(dropped)


def _anthropic_payload(record: Agent, *, public_url: str | None) -> dict[str, object]:
    servers: list[BetaManagedAgentsURLMCPServerParams] = [
        {"type": "url", "name": server.name, "url": server.url}
        for server in record.spec.mcp_servers or ()
    ]
    return {
        "name": record.spec.name,
        "description": record.spec.description,
        "system": record.spec.system,
        "model": DEFAULT_AGENT_MODEL,
        "mcp_servers": merge_default_mcp_server(servers or None, public_url),
        # The record's tools are the other host's; Claude gets its own base set.
        "tools": merge_default_agent_toolset(merge_default_mcp_toolset(None, public_url)),
        "metadata": dict(record.spec.metadata or {})
        | {MA_METADATA_KEY_CONVERTED_FROM: record.ref.id},
    }


async def _refuse_open_work(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    agent_id: str,
) -> None:
    async with sessionmaker() as session:
        if await open_agent_work(session, tenant_id=tenant_id, agent_id=agent_id):
            raise UserFacingError(f"{name} is still working. Try again when it's done.")


async def _move_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    old_id: str,
    new_id: str,
) -> None:
    # Checked again inside the move: a wake queued since the first check would
    # otherwise fire at an agent that no longer exists.
    async with sessionmaker() as session, session.begin():
        if await open_agent_work(session, tenant_id=tenant_id, agent_id=old_id):
            raise UserFacingError(f"{name} is still working. Try again when it's done.")
        moved = await rekey_agent(session, tenant_id=tenant_id, old_id=old_id, new_id=new_id)
        # Consent covered the old record, archived next: no delete would ever find it.
        await thread_models.delete_consents_for_agent(
            session, agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=old_id)
        )
    _log.info("agent.rekeyed", name=name, old_id=old_id, new_id=new_id, rows=moved)
