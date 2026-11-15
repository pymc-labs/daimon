"""Custom and channel skills a template carries to a provider that installs them per session.

Capture measures each source skill once per version (`skill_sizes`); a skill over
the profile's limit is a named `skill:<id>` loss, never a silent one. The turn
uploads each remaining skill once per version (`skill_copies`) and attaches the
copies to its session. A skill left out at the turn (a channel's, or one whose
upload failed) is told to the agent (`missing_skills_notice`).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import structlog
from anthropic import AnthropicError, AsyncAnthropic
from daimon.core.defaults.loader import parse_skill_markdown
from daimon.core.defaults.ma_index import download_skill_version
from daimon.core.errors import DaimonError, TurnError
from daimon.core.skill_zip import MAX_UNCOMPRESSED_BYTES
from daimon.core.skills.ingest import SkillIngestError, retained_archive_files
from daimon.core.skills.latest_versions import LATEST_VERSIONS
from daimon.core.stores.agent_notices import record_agent_notices
from daimon.core.stores.domain import SkillCopyRow, SkillSizeRow
from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
from daimon.core.stores.skill_copies import (
    get_skill_copy,
    get_skill_sizes,
    put_skill_copy,
    put_skill_size,
)
from daimon.core.turn.agent_notices import missing_skills_notice
from mux.contracts.ids import ResourceRef, Scope, SkillRef
from mux.contracts.ports import Skills
from mux.contracts.profile import Profile
from mux.contracts.resources import Agent, AgentSpec, EnvironmentSpec, SkillUpload, SkillUploadFile
from mux.errors import MuxError, ProviderError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.session_snapshot import MaSkill
    from daimon.core.turn.prepare import ProviderPreparationRequest

log = structlog.get_logger(__name__)
_DROP_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_MANIFEST_BYTES = 1024 * 1024


def read_blip(error: BaseException) -> TurnError | None:
    """The busy error a skill read's blip (a timeout, a 5xx or 429) becomes, else None.

    A blip proves no loss, so it propagates and the caller retries, as a turn does.
    """
    from daimon.core.channel_backend import transient_failure

    busy = TurnError(kind="upstream", cause=error)
    busy.__cause__ = error
    return busy if transient_failure(busy) else None


def attaches_session_skills(profile: Profile) -> bool:
    """Whether the profile installs a template's custom and channel skills per session."""
    supported = profile.support_for("skills_bundle") != "unsupported"
    return profile.skill_limits is not None and supported


def carried_skills(spec: AgentSpec) -> tuple[SkillRef, ...]:
    """Skills with bytes to carry: custom and channel ones, not a provider's built-ins."""
    return tuple(s for s in spec.skills or () if s.source in (None, "custom"))


def skill_names(sizes: Mapping[tuple[str, str], SkillSizeRow]) -> dict[str, str]:
    """Each measured skill's own name by id, for people; a drop keys it by id."""
    return {row.skill_id: row.name for row in sizes.values() if _DROP_NAME.fullmatch(row.name)}


def fitting_skills(
    profile: Profile,
    pins: Sequence[SkillRef],
    sizes: Mapping[tuple[str, str], SkillSizeRow],
    *,
    reserved: int = 0,
) -> tuple[SkillRef, ...]:
    """The measured pins the profile installs, in order, together within `combined_bytes`.

    `reserved` is what the environment's own inline files (Gemini's package hook)
    already spend of that budget; the skills get the rest.
    """
    limits = profile.skill_limits
    combined = (
        limits.combined_bytes - reserved
        if limits is not None and limits.combined_bytes is not None
        else None
    )
    kept: list[SkillRef] = []
    used = 0
    for pin in pins:
        row = sizes.get((pin.id, str(pin.version)))
        if row is None or not skill_fits(profile, row):
            continue
        if combined is not None and used + row.total_bytes > combined:
            continue
        used += row.total_bytes
        kept.append(pin)
    return tuple(kept)


def environment_inline_bytes(profile: Profile, environment: EnvironmentSpec | None) -> int:
    """What `environment`'s own inline files spend of the budget skills share with them.

    Only Gemini mounts both inline into one environment (`validate_targets`).
    """
    from mux.drivers.gemini import compile_environment, inline_bytes
    from mux.profiles import INLINE_REUSE

    if environment is None or profile.profile_id != INLINE_REUSE.profile_id:
        return 0
    return inline_bytes(compile_environment(environment))


def skill_losses(pins: Sequence[SkillRef], kept: Sequence[SkillRef]) -> tuple[str, ...]:
    """A `skill:` loss for each pin left out, by id: measured or not, the same loss."""
    return tuple(sorted({f"skill:{p.id}" for p in pins if p not in kept}))


def skill_fits(profile: Profile, row: SkillSizeRow) -> bool:
    limits = profile.skill_limits
    return limits is not None and limits.fits(
        total_bytes=row.total_bytes,
        largest_file_bytes=row.largest_file_bytes,
        files=row.file_count,
        text=row.text_only,
    )


def measure_archive(tenant_id: uuid.UUID, skill_id: str, version: str, data: bytes) -> SkillSizeRow:
    """Size one source archive; one Daimon can't read counts as over every limit."""
    try:
        files = retained_archive_files(data)
    except SkillIngestError:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                manifest = next(
                    (
                        info
                        for info in infos
                        if PurePosixPath(info.filename).name == "SKILL.md"
                        and len(PurePosixPath(info.filename).parts) <= 2
                        and info.file_size <= _MANIFEST_BYTES
                    ),
                    None,
                )
                head = archive.read(manifest) if manifest is not None else b""
        except zipfile.BadZipFile:
            raise ValueError("source skill archive is unreadable") from None
        sizes = [info.file_size for info in infos]
        return SkillSizeRow(
            tenant_id=tenant_id,
            skill_id=skill_id,
            version=version,
            name=_manifest_name(head, skill_id),
            total_bytes=max(sum(sizes), MAX_UNCOMPRESSED_BYTES + 1),
            largest_file_bytes=max(sizes, default=0),
            file_count=len(sizes),
            text_only=False,
        )
    return SkillSizeRow(
        tenant_id=tenant_id,
        skill_id=skill_id,
        version=version,
        name=_manifest_name(files["SKILL.md"], skill_id),
        total_bytes=sum(len(body) for body in files.values()),
        largest_file_bytes=max(len(body) for body in files.values()),
        file_count=len(files),
        text_only=all(_is_text(body) for body in files.values()),
    )


def _manifest_name(manifest: bytes, skill_id: str) -> str:
    try:
        return parse_skill_markdown(manifest.decode(), source="SKILL.md")[0].name
    except (UnicodeDecodeError, DaimonError):
        return skill_id


def _is_text(body: bytes) -> bool:
    try:
        body.decode()
    except UnicodeDecodeError:
        return False
    return True


async def source_skill_archive(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    scope: Scope,
    record: Agent | None,
    skill_id: str,
    version: str,
) -> bytes:
    """A source skill version's archive: a record's own catalog copy, else Managed Agents.

    A converted record keeps its skills after the Managed Agents original is gone.
    """
    if record is not None and record.ref.provider == "selfhosted":
        catalog = PostgresNeutralCatalog(
            sessionmaker, provider="selfhosted", account_scope_id=scope.tenant_id
        )
        pin = SkillRef(id=skill_id, version=version, source="custom")
        try:
            stored = await catalog.get_agent_skill(scope, record, pin)
        except (MuxError, SQLAlchemyError) as error:
            log.warning(
                "session_skills.catalog_unread", skill_id=skill_id, error=type(error).__name__
            )
            stored = None
        if stored is not None:
            return stored.bundle.zip_bytes
    return await download_skill_version(anthropic, skill_id=skill_id, version=version, scope=scope)


async def measure_skills(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    pins: Iterable[tuple[str, str]],
    record: Agent | None = None,
) -> dict[tuple[str, str], SkillSizeRow]:
    """Each `(skill_id, version)`'s size, downloading only versions never measured.

    A version that can't be read is absent from the result, never cached: it costs
    only its own skill, and the next capture tries it again. A blip propagates
    instead (`read_blip`): it proves no loss. Each size is stored as
    soon as it is measured, by a task the caller's timeout or cancel never stops,
    so a retry downloads only what a timed-out try left unmeasured.
    """
    wanted = tuple(dict.fromkeys(pins))
    # Taken before the read: one that finishes meanwhile is still joined, not redone.
    owner = record.ref if record is not None else None
    running = {
        pin: task
        for pin in wanted
        if (task := _running((tenant_id, scope, owner, *pin))) is not None
    }
    async with sessionmaker() as db:
        known = await get_skill_sizes(db, tenant_id=tenant_id, pins=wanted)
    tasks = [
        running.get(pin)
        or _measuring(
            sessionmaker, anthropic, scope=scope, tenant_id=tenant_id, record=record, pin=pin
        )
        for pin in wanted
        if pin not in known
    ]
    measured = await asyncio.gather(*(asyncio.shield(task) for task in tasks))
    fresh = {(row.skill_id, row.version): row for row in measured if row is not None}
    return {**known, **fresh}


#: A measurement's caller and source: the scope and record it reads with, by skill and version.
_MeasureKey = tuple[uuid.UUID, Scope, ResourceRef | None, str, str]
#: Measurements in flight: a strong reference keeps an abandoned one running, and a
#: second caller reading the same way joins it instead of downloading again.
_IN_FLIGHT: dict[_MeasureKey, asyncio.Task[SkillSizeRow | None]] = {}


def cancel_pending_measures() -> None:
    """Cancel every unfinished measurement; for test isolation only."""
    for task in list(_IN_FLIGHT.values()):
        task.cancel()
    _IN_FLIGHT.clear()


def _running(key: _MeasureKey) -> asyncio.Task[SkillSizeRow | None] | None:
    task = _IN_FLIGHT.get(key)
    return task if task is not None and task.get_loop() is asyncio.get_running_loop() else None


def _measuring(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    record: Agent | None,
    pin: tuple[str, str],
) -> asyncio.Task[SkillSizeRow | None]:
    key = (tenant_id, scope, record.ref if record is not None else None, *pin)
    running = _running(key)
    if running is not None:
        return running
    task = asyncio.create_task(
        _measure_one(
            sessionmaker,
            anthropic,
            scope=scope,
            tenant_id=tenant_id,
            record=record,
            skill_id=pin[0],
            version=pin[1],
        )
    )
    _IN_FLIGHT[key] = task

    def settle(done: asyncio.Task[SkillSizeRow | None]) -> None:
        if _IN_FLIGHT.get(key) is done:
            del _IN_FLIGHT[key]
        if not done.cancelled() and (error := done.exception()) is not None:
            log.warning("skill_size.measure_failed", skill_id=pin[0], error=type(error).__name__)

    task.add_done_callback(settle)
    return task


async def _measure_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    record: Agent | None,
    skill_id: str,
    version: str,
) -> SkillSizeRow | None:
    try:
        data = await source_skill_archive(
            sessionmaker, anthropic, scope=scope, record=record, skill_id=skill_id, version=version
        )
    except (AnthropicError, DaimonError, MuxError) as error:
        if (busy := read_blip(error)) is not None:
            raise busy from error
        log.warning("skill_size.measure_failed", skill_id=skill_id, error=type(error).__name__)
        return None
    row = measure_archive(tenant_id, skill_id, version, data)
    async with sessionmaker.begin() as db:
        await put_skill_size(db, row)
    return row


async def copy_skills(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    skills: Skills,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    provider: str,
    account_scope_id: str,
    pins: tuple[SkillRef, ...],
    record: Agent | None = None,
) -> tuple[tuple[SkillRef, ...], tuple[str, ...]]:
    """The provider's copy of each source skill version, uploading a version only once.

    Also returns a named `skill:<id>` loss for each pin that could not be copied:
    one bad archive or refused upload costs that skill alone, never the reply. A
    retryable provider error propagates so the reply is retried.
    """

    async def copy(pin: SkillRef) -> SkillRef:
        if pin.version in (None, "latest"):
            raise ValueError("a carried skill needs a concrete version")
        assert pin.version is not None
        async with sessionmaker() as db:
            row = await get_skill_copy(
                db,
                tenant_id=tenant_id,
                provider=provider,
                account_scope_id=account_scope_id,
                skill_id=pin.id,
                version=pin.version,
            )
        if row is None:
            files = retained_archive_files(
                await source_skill_archive(
                    sessionmaker,
                    anthropic,
                    scope=scope,
                    record=record,
                    skill_id=pin.id,
                    version=pin.version,
                )
            )
            upload = SkillUpload(
                files=tuple(
                    SkillUploadFile(path=path, content=body) for path, body in sorted(files.items())
                )
            )
            # One key per source version and project: a retried upload is the same one.
            seed = f"{tenant_id}:{account_scope_id}:{pin.id}:{pin.version}"
            created = await skills.create(
                scope, upload, key="skill-copy-" + hashlib.sha256(seed.encode()).hexdigest()[:32]
            )
            if created.latest_version is None or created.latest_version.version is None:
                raise ValueError("copied skill has no version")
            async with sessionmaker.begin() as db:
                row = await put_skill_copy(
                    db,
                    SkillCopyRow(
                        tenant_id=tenant_id,
                        provider=provider,
                        account_scope_id=account_scope_id,
                        skill_id=pin.id,
                        version=pin.version,
                        copy_id=created.id,
                        copy_version=created.latest_version.version,
                    ),
                )
        return SkillRef(id=row.copy_id, version=row.copy_version)

    copies: list[SkillRef] = []
    losses: list[str] = []
    # One at a time: a provider's local record store may take a single writer.
    for pin in pins:
        try:
            copies.append(await copy(pin))
        except (AnthropicError, DaimonError, MuxError, ValueError) as error:
            # A transient failure retries with the reply; only a permanent one is a loss,
            # because the session is created once with whatever copied.
            if isinstance(error, ProviderError) and error.retryable:
                raise
            log.warning("session_skills.copy_failed", skill_id=pin.id, error=type(error).__name__)
            losses.append(f"skill:{pin.id}")
    return tuple(copies), tuple(losses)


async def latest_version(anthropic: AsyncAnthropic, scope: Scope, attached: SkillRef) -> str:
    """The concrete current version of a skill attached at `latest`.

    Remembered for `LATEST_VERSION_TTL_S`: an agent with many skills at `latest`
    otherwise spends one paced Skills API request per skill on every read.
    """
    from daimon.core.mux_backend import managed_agents
    from daimon.core.mux_compat import legacy_call

    # Keyed by source: a hit is only for a ref whose source a lookup already matched.
    cached = LATEST_VERSIONS.get(scope.tenant_id, attached.id, attached.source)
    if cached is not None:
        return cached
    generation = LATEST_VERSIONS.generation()
    # Grant reads only for this id: a scoped catalogue walk hides custom skills.
    backend = managed_agents(anthropic, scope=scope, resources=frozenset({("skill", attached.id)}))
    record = await legacy_call(backend.skills.retrieve(scope, attached.id))
    pin = record.latest_version
    if (
        record.id != attached.id
        or record.source != attached.source
        or pin is None
        or pin.id != attached.id
        or pin.version in (None, "latest")
    ):
        raise ValueError("source skill has no concrete current version")
    assert pin.version is not None
    LATEST_VERSIONS.put(
        scope.tenant_id, attached.id, pin.version, generation=generation, source=attached.source
    )
    return pin.version


async def measure_carried_skills(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    carried: Sequence[SkillRef],
    record: Agent | None,
) -> tuple[tuple[SkillRef, ...], dict[tuple[str, str], SkillSizeRow]]:
    """A capture's carried skills pinned to concrete versions, and each one's size.

    A skill whose latest version can't be read keeps `latest` and goes unmeasured:
    a named loss of that skill alone. A blip propagates instead (`read_blip`).
    """

    async def pinned(attached: SkillRef) -> SkillRef:
        if attached.version not in (None, "latest"):
            return SkillRef(id=attached.id, version=attached.version)
        try:
            version = await latest_version(anthropic, scope, attached)
        except (AnthropicError, DaimonError, MuxError, ValueError) as error:
            if (busy := read_blip(error)) is not None:
                raise busy from error
            log.warning(
                "source_capture.skill_unresolved", skill_id=attached.id, error=type(error).__name__
            )
            return SkillRef(id=attached.id, version=attached.version)
        return SkillRef(id=attached.id, version=version)

    pins = tuple(await asyncio.gather(*(pinned(s) for s in carried)))
    sizes = await measure_skills(
        sessionmaker,
        anthropic,
        scope=scope,
        tenant_id=tenant_id,
        pins=((p.id, str(p.version)) for p in pins if p.version not in (None, "latest")),
        record=record,
    )
    return pins, sizes


async def channel_session_skills(
    request: ProviderPreparationRequest, profile: Profile, scope: Scope
) -> tuple[tuple[SkillRef, ...], tuple[str, ...], dict[str, str]]:
    """The channel's custom skills a channel-origin template installs, those left out, names.

    It has no capture, so they are measured here; one over the profile's limits,
    or unreadable, is a named `skill:` loss.
    """
    deps, skills = request.deps, request.admission.channel_skills
    custom = tuple(s for s in skills if s.type == "custom")
    if not custom or not attaches_session_skills(profile):
        return (), (), {}

    async def pin(skill: MaSkill) -> SkillRef | None:
        attached = SkillRef(id=skill.skill_id, version=skill.version, source="custom")
        try:
            version = (
                await latest_version(deps.anthropic, scope, attached)
                if skill.version in (None, "latest")
                else skill.version
            )
        except (AnthropicError, DaimonError, MuxError, ValueError) as error:
            if (busy := read_blip(error)) is not None:
                raise busy from error
            log.warning(
                "session_skills.unresolved", skill_id=skill.skill_id, error=type(error).__name__
            )
            return None
        return SkillRef(id=skill.skill_id, version=version)

    resolved = await asyncio.gather(*(pin(s) for s in custom))
    pins = tuple(p for p in resolved if p is not None)
    unresolved = tuple(
        f"skill:{s.skill_id}" for s, p in zip(custom, resolved, strict=True) if p is None
    )
    sizes = await measure_skills(
        deps.sessionmaker,
        deps.anthropic,
        scope=scope,
        tenant_id=request.tenant_id,
        pins=((p.id, str(p.version)) for p in pins),
    )
    kept = fitting_skills(profile, pins, sizes)
    return kept, (*unresolved, *skill_losses(pins, kept)), skill_names(sizes)


async def turn_session_skills(
    request: ProviderPreparationRequest,
    skills: Callable[[], Skills],
    *,
    profile: Profile,
    scope: Scope,
    account_scope_id: str,
) -> tuple[SkillRef, ...]:
    """The provider's copies of the custom and channel skills a template's session carries.

    An agent-origin template carries what its confirmed capture measured; a
    channel-origin one, the channel's own skills measured now. `skills` is
    called only when there is something to copy: no credential is read before.
    """
    from daimon.core.turn.runtime_manifest import represented_source

    if request.admission.backend_revision_origin == "agent":
        source = request.source
        represented = (
            represented_source(source, request.target_spec) if source is not None else None
        )
        pins = represented.session_skills if represented is not None else ()
        record = source.source if source is not None else None
        left: tuple[str, ...] = ()
        names = dict(represented.skill_names) if represented is not None else {}
    else:
        pins, left, names = await channel_session_skills(request, profile, scope)
        record = None
    copies: tuple[SkillRef, ...] = ()
    if pins:
        copies, failed = await copy_skills(
            request.deps.sessionmaker,
            request.deps.anthropic,
            skills(),
            scope=scope,
            tenant_id=request.tenant_id,
            provider=profile.provider,
            account_scope_id=account_scope_id,
            pins=pins,
            record=record,
        )
        left = (*left, *failed)
    if left:
        await _tell_missing_skills(request, left, names)
    return copies


async def _tell_missing_skills(
    request: ProviderPreparationRequest, losses: Sequence[str], names: Mapping[str, str]
) -> None:
    """Queue the agent a notice naming the skills its session goes without."""
    ids = sorted({loss.removeprefix("skill:") for loss in losses})
    log.warning("session_skills.left_out", tenant_id=str(request.tenant_id), skills=ids)
    try:
        async with request.deps.sessionmaker.begin() as db:
            await record_agent_notices(
                db,
                tenant_id=request.tenant_id,
                platform=request.platform,
                thread_id=request.thread_id,
                agent_id=request.admission.agent.id,
                texts=[missing_skills_notice(sorted({names.get(i, i) for i in ids}))],
            )
    except SQLAlchemyError as error:  # the notice is advisory; never block the reply
        log.warning("session_skills.notice_failed", error=type(error).__name__)
