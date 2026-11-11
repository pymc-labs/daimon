"""Custom and channel skills a template carries to a provider that installs them per session.

Capture measures each source skill once per version (`skill_sizes`); a skill over
the profile's limit is a named `skill:<name>` loss, never a silent one. The turn
uploads each remaining skill once per version (`skill_copies`) and attaches the
copies to its session.
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
from daimon.core.errors import DaimonError
from daimon.core.skill_zip import MAX_UNCOMPRESSED_BYTES
from daimon.core.skills.ingest import SkillIngestError, retained_archive_files
from daimon.core.stores.domain import SkillCopyRow, SkillSizeRow
from daimon.core.stores.skill_copies import (
    get_skill_copy,
    get_skill_sizes,
    put_skill_copy,
    put_skill_size,
)
from mux.contracts.ids import Scope, SkillRef
from mux.contracts.ports import Skills
from mux.contracts.profile import Profile
from mux.contracts.resources import AgentSpec, SkillUpload, SkillUploadFile
from mux.errors import MuxError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.session_snapshot import MaSkill
    from daimon.core.turn.prepare import ProviderPreparationRequest

log = structlog.get_logger(__name__)
_DROP_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_MANIFEST_BYTES = 1024 * 1024


def attaches_session_skills(profile: Profile) -> bool:
    """Whether the profile installs a template's custom and channel skills per session."""
    supported = profile.support_for("skills_bundle") != "unsupported"
    return profile.skill_limits is not None and supported


def carried_skills(spec: AgentSpec) -> tuple[SkillRef, ...]:
    """Skills with bytes to carry: custom and channel ones, not a provider's built-ins."""
    return tuple(s for s in spec.skills or () if s.source in (None, "custom"))


def skill_drop(row: SkillSizeRow) -> str:
    return f"skill:{row.name if _DROP_NAME.fullmatch(row.name) else row.skill_id}"


def fitting_skills(
    profile: Profile, pins: Sequence[SkillRef], sizes: Mapping[tuple[str, str], SkillSizeRow]
) -> tuple[SkillRef, ...]:
    """The measured pins the profile installs, in order, together within `combined_bytes`."""
    combined = profile.skill_limits.combined_bytes if profile.skill_limits else None
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


def skill_losses(
    pins: Sequence[SkillRef],
    sizes: Mapping[tuple[str, str], SkillSizeRow],
    kept: Sequence[SkillRef],
) -> tuple[str, ...]:
    """A named `skill:` loss for each pin left out; one never measured is named by its id."""
    return tuple(
        sorted(
            {
                skill_drop(row) if (row := sizes.get((p.id, str(p.version)))) else f"skill:{p.id}"
                for p in pins
                if p not in kept
            }
        )
    )


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


async def measure_skills(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    pins: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], SkillSizeRow]:
    """Each `(skill_id, version)`'s size, downloading only versions never measured.

    A version that can't be read is absent from the result, never cached: it costs
    only its own skill, and the next capture tries it again.
    """
    wanted = tuple(dict.fromkeys(pins))
    async with sessionmaker() as db:
        known = await get_skill_sizes(db, tenant_id=tenant_id, pins=wanted)

    async def measure(skill_id: str, version: str) -> SkillSizeRow | None:
        try:
            data = await download_skill_version(
                anthropic, skill_id=skill_id, version=version, scope=scope
            )
        except (AnthropicError, DaimonError, MuxError) as error:
            log.warning("skill_size.measure_failed", skill_id=skill_id, error=type(error).__name__)
            return None
        return measure_archive(tenant_id, skill_id, version, data)

    measured = await asyncio.gather(*(measure(*pin) for pin in wanted if pin not in known))
    fresh = [row for row in measured if row is not None]
    if fresh:
        async with sessionmaker.begin() as db:
            for row in fresh:
                await put_skill_size(db, row)
    return {**known, **{(row.skill_id, row.version): row for row in fresh}}


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
) -> tuple[SkillRef, ...]:
    """The provider's copy of each source skill version, uploading a version only once."""

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
                await download_skill_version(
                    anthropic, skill_id=pin.id, version=pin.version, scope=scope
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

    # One at a time: a provider's local record store may take a single writer.
    return tuple([await copy(pin) for pin in pins])


async def latest_version(anthropic: AsyncAnthropic, scope: Scope, attached: SkillRef) -> str:
    """The concrete current version of a skill attached at `latest`."""
    from daimon.core.mux_backend import managed_agents
    from daimon.core.mux_compat import legacy_call

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
    return pin.version


async def channel_session_skills(
    request: ProviderPreparationRequest, profile: Profile, scope: Scope
) -> tuple[SkillRef, ...]:
    """The channel's custom skills a channel-origin template installs.

    It has no capture, so they are measured here; one over the profile's limits,
    or unreadable, is left out and logged.
    """
    deps, skills = request.deps, request.admission.channel_skills
    custom = tuple(s for s in skills if s.type == "custom")
    if not custom or not attaches_session_skills(profile):
        return ()

    async def pin(skill: MaSkill) -> SkillRef | None:
        attached = SkillRef(id=skill.skill_id, version=skill.version, source="custom")
        try:
            version = (
                await latest_version(deps.anthropic, scope, attached)
                if skill.version in (None, "latest")
                else skill.version
            )
        except (AnthropicError, DaimonError, MuxError, ValueError) as error:
            log.warning(
                "session_skills.unresolved", skill_id=skill.skill_id, error=type(error).__name__
            )
            return None
        return SkillRef(id=skill.skill_id, version=version)

    pins = tuple(p for p in await asyncio.gather(*(pin(s) for s in custom)) if p is not None)
    sizes = await measure_skills(
        deps.sessionmaker,
        deps.anthropic,
        scope=scope,
        tenant_id=request.tenant_id,
        pins=((p.id, str(p.version)) for p in pins),
    )
    kept = fitting_skills(profile, pins, sizes)
    if left := skill_losses(pins, sizes, kept):
        log.warning("session_skills.left_out", tenant_id=str(request.tenant_id), skills=list(left))
    return kept


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
    else:
        pins = await channel_session_skills(request, profile, scope)
    if not pins:
        return ()
    return await copy_skills(
        request.deps.sessionmaker,
        request.deps.anthropic,
        skills(),
        scope=scope,
        tenant_id=request.tenant_id,
        provider=profile.provider,
        account_scope_id=account_scope_id,
        pins=pins,
    )
