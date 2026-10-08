"""Tenant-scoped avatar storage and deterministic default artwork.

Agent names are immutable; archive cleanup deletes the associated avatar.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from daimon.core._models import AgentAvatar, Tenant
from daimon.core.agent_faces import (
    FaceCombo,
    choose,
    classic_combo,
    decode_combo,
    encode_combo,
    render,
    thumbnail,
)
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

_PALETTE = ("#355c7d", "#6c5b7b", "#c06c84", "#2a9d8f", "#e76f51", "#577590")
_MAX_PNG_BYTES = 256 * 1024


@dataclass(frozen=True)
class AvatarRow:
    token: str
    sha256: str
    png: bytes
    source: str
    face_combo: FaceCombo | None = None
    has_face_assignment: bool = False
    png_128: bytes | None = None
    png_512: bytes | None = None
    previous_sha256: str | None = None
    previous_png: bytes | None = None
    previous_png_128: bytes | None = None
    previous_png_512: bytes | None = None


@dataclass(frozen=True)
class AvatarLink:
    token: str
    sha256: str
    source: str
    has_face_assignment: bool


def normalize_agent_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def generate_default_png(name: str) -> bytes:
    words = name.replace("-", " ").replace("_", " ").split()
    glyphs = [word[0].upper() for word in words[:2]]
    initials = (
        "".join(
            glyph if len(glyph) == 1 and glyph.isascii() and glyph.isalnum() else "?"
            for glyph in glyphs
        )
        or "?"
    )
    colour = _PALETTE[
        int.from_bytes(hashlib.sha256(normalize_agent_name(name).encode()).digest()[:4], "big")
        % len(_PALETTE)
    ]
    image = Image.new("RGB", (256, 256), colour)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=100)
    try:
        box = draw.textbbox((0, 0), initials, font=font)
    except UnicodeError:
        initials = "?"
        box = draw.textbbox((0, 0), initials, font=font)
    draw.text(
        ((256 - (box[2] - box[0])) / 2 - box[0], (256 - (box[3] - box[1])) / 2 - box[1]),
        initials,
        font=font,
        fill="white",
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _sized_pngs(png: bytes) -> tuple[bytes, bytes]:
    """Render supported public sizes once when artwork is written."""
    with Image.open(io.BytesIO(png)) as original:
        rgb = original.convert("RGB")
        result: list[bytes] = []
        for size in (128, 512):
            if rgb.size == (size, size):
                result.append(png)
                continue
            output = io.BytesIO()
            rgb.resize((size, size), Image.Resampling.LANCZOS).save(output, format="PNG")
            result.append(output.getvalue())
        return result[0], result[1]


def _row(orm: AgentAvatar) -> AvatarRow:
    try:
        combo = decode_combo(orm.face_combo) if orm.face_combo else None
    except (ValueError, OSError):
        # The stored PNG remains serviceable if a catalogue entry is damaged.
        combo = None
    return AvatarRow(
        token=orm.token,
        sha256=orm.sha256,
        png=orm.png,
        source=orm.source,
        face_combo=combo,
        has_face_assignment=orm.face_combo is not None,
        png_128=orm.png_128,
        png_512=orm.png_512,
        previous_sha256=orm.previous_sha256,
        previous_png=orm.previous_png,
        previous_png_128=orm.previous_png_128,
        previous_png_512=orm.previous_png_512,
    )


async def _choose_face(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str
) -> FaceCombo:
    rows = await session.execute(
        select(AgentAvatar.agent_name, AgentAvatar.face_combo, AgentAvatar.face_thumbnail).where(
            AgentAvatar.tenant_id == tenant_id,
            AgentAvatar.agent_name != normalize_agent_name(agent_name),
            AgentAvatar.face_combo.is_not(None),
        )
    )
    classic = classic_combo()
    existing = [classic]
    pictures = [await asyncio.to_thread(thumbnail, classic)]
    for name, raw, picture in rows:
        try:
            if raw is None:
                continue
            combo = decode_combo(raw)
            if picture is None or len(picture) != 1200:
                picture = bytes((await asyncio.to_thread(thumbnail, combo)).astype(np.uint8))
                await session.execute(
                    update(AgentAvatar)
                    .where(AgentAvatar.tenant_id == tenant_id, AgentAvatar.agent_name == name)
                    .values(face_thumbnail=picture)
                )
            existing.append(combo)
            pictures.append(np.frombuffer(picture, dtype=np.uint8).astype(np.float32))
        except (ValueError, OSError):
            continue
    return await asyncio.to_thread(
        choose, normalize_agent_name(agent_name), existing, existing_thumbnails=pictures
    )


async def _lock_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    key = int.from_bytes(
        hashlib.sha256(tenant_id.bytes + b"agent_faces").digest()[:8], "big", signed=True
    )
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    await session.execute(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(key_share=True)
    )


async def get_or_create_avatar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    face_enabled: bool = False,
) -> AvatarRow:
    key = normalize_agent_name(agent_name)
    existing = await session.get(AgentAvatar, (tenant_id, key))
    if existing is not None and (
        not face_enabled or existing.source == "upload" or existing.face_combo is not None
    ):
        return _row(existing)
    if face_enabled:
        # Assign under the tenant advisory lock so concurrent agents see the
        # same committed set of variants and thumbnails.
        await _lock_tenant(session, tenant_id)
        existing = await session.get(AgentAvatar, (tenant_id, key), populate_existing=True)
        if existing is not None and (existing.source == "upload" or existing.face_combo):
            return _row(existing)
        combo = await _choose_face(session, tenant_id=tenant_id, agent_name=agent_name)
        png = await asyncio.to_thread(render, combo, 512)
        face_picture = bytes((await asyncio.to_thread(thumbnail, combo)).astype(np.uint8))
    else:
        combo = None
        png = generate_default_png(agent_name)
        face_picture = None
    png_128, png_512 = await asyncio.to_thread(_sized_pngs, png)
    token = existing.token if face_enabled and existing is not None else secrets.token_urlsafe(24)
    sha = hashlib.sha256(png).hexdigest()
    previous_sha = existing.sha256 if face_enabled and existing is not None else None
    previous_png = existing.png if face_enabled and existing is not None else None
    previous_png_128 = existing.png_128 if face_enabled and existing is not None else None
    previous_png_512 = existing.png_512 if face_enabled and existing is not None else None
    await session.execute(
        pg_insert(AgentAvatar)
        .values(
            tenant_id=tenant_id,
            agent_name=key,
            token=token,
            sha256=sha,
            png=png,
            png_128=png_128,
            png_512=png_512,
            previous_sha256=previous_sha,
            previous_png=previous_png,
            previous_png_128=previous_png_128,
            previous_png_512=previous_png_512,
            source="default",
            face_combo=encode_combo(combo) if combo else None,
            face_thumbnail=face_picture,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "agent_name"],
            set_={
                "token": token,
                "sha256": sha,
                "png": png,
                "png_128": png_128,
                "png_512": png_512,
                "previous_sha256": previous_sha,
                "previous_png": previous_png,
                "previous_png_128": previous_png_128,
                "previous_png_512": previous_png_512,
                "source": "default",
                "face_combo": encode_combo(combo) if combo else None,
                "face_thumbnail": face_picture,
                "updated_at": datetime.now(UTC),
            },
            where=AgentAvatar.source == "default",
        )
    )
    result = await session.get(AgentAvatar, (tenant_id, key), populate_existing=True)
    if result is None:
        raise RuntimeError("avatar insert did not persist")
    return _row(result)


async def replace_avatar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    png: bytes,
    source: str,
    updated_by_account_id: uuid.UUID | None = None,
    face_combo: FaceCombo | None = None,
) -> AvatarRow:
    limit = 1024 * 1024 if face_combo is not None and source == "default" else _MAX_PNG_BYTES
    if source not in ("default", "upload") or len(png) > limit:
        raise ValueError(
            "avatar must be a PNG of at most 1 MB"
            if limit > _MAX_PNG_BYTES
            else "avatar must be a PNG of at most 256 KB"
        )
    try:
        with Image.open(io.BytesIO(png)) as image:
            expected_size = (512, 512) if face_combo is not None else (256, 256)
            if image.format != "PNG" or image.size != expected_size:
                raise ValueError("avatar has the wrong PNG size")
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("avatar must be a valid PNG") from exc
    key = normalize_agent_name(agent_name)
    token = secrets.token_urlsafe(24)
    sha = hashlib.sha256(png).hexdigest()
    png_128, png_512 = await asyncio.to_thread(_sized_pngs, png)
    face_picture = (
        bytes((await asyncio.to_thread(thumbnail, face_combo)).astype(np.uint8))
        if face_combo
        else None
    )
    await session.execute(
        pg_insert(AgentAvatar)
        .values(
            tenant_id=tenant_id,
            agent_name=key,
            token=token,
            sha256=sha,
            png=png,
            png_128=png_128,
            png_512=png_512,
            previous_sha256=None,
            previous_png=None,
            previous_png_128=None,
            previous_png_512=None,
            source=source,
            face_combo=encode_combo(face_combo) if face_combo else None,
            face_thumbnail=face_picture,
            updated_by_account_id=updated_by_account_id,
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "agent_name"],
            set_={
                "token": token,
                "sha256": sha,
                "png": png,
                "png_128": png_128,
                "png_512": png_512,
                "previous_sha256": None,
                "previous_png": None,
                "previous_png_128": None,
                "previous_png_512": None,
                "source": source,
                "face_combo": AgentAvatar.face_combo
                if source == "upload"
                else (encode_combo(face_combo) if face_combo else None),
                "face_thumbnail": AgentAvatar.face_thumbnail
                if source == "upload"
                else face_picture,
                "updated_by_account_id": updated_by_account_id,
                "updated_at": datetime.now(UTC),
            },
        )
    )
    saved = await session.get(AgentAvatar, (tenant_id, key), populate_existing=True)
    if saved is None:
        raise RuntimeError("avatar replacement did not persist")
    return _row(saved)


async def reset_avatar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    updated_by_account_id: uuid.UUID | None = None,
    face_enabled: bool = False,
) -> AvatarRow:
    combo: FaceCombo | None = None
    await _lock_tenant(session, tenant_id)
    if face_enabled:
        existing = await session.get(
            AgentAvatar, (tenant_id, normalize_agent_name(agent_name)), populate_existing=True
        )
        try:
            combo = decode_combo(existing.face_combo) if existing and existing.face_combo else None
        except (ValueError, OSError):
            combo = None
        if combo is None:
            combo = await _choose_face(session, tenant_id=tenant_id, agent_name=agent_name)
    return await replace_avatar(
        session,
        tenant_id=tenant_id,
        agent_name=agent_name,
        png=await asyncio.to_thread(render, combo, 512)
        if combo
        else generate_default_png(agent_name),
        source="default",
        updated_by_account_id=updated_by_account_id,
        face_combo=combo,
    )


async def get_avatar_by_token(session: AsyncSession, *, token: str) -> AvatarRow | None:
    orm = (
        await session.scalars(select(AgentAvatar).where(AgentAvatar.token == token))
    ).one_or_none()
    return None if orm is None else _row(orm)


async def get_agent_avatar(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str
) -> AvatarLink | None:
    """Read a current avatar without generating artwork on the turn path."""
    row = (
        await session.execute(
            select(
                AgentAvatar.token,
                AgentAvatar.sha256,
                AgentAvatar.source,
                AgentAvatar.face_combo,
            )
            .where(
                AgentAvatar.tenant_id == tenant_id,
                AgentAvatar.agent_name == normalize_agent_name(agent_name),
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    return AvatarLink(row.token, row.sha256, row.source, row.face_combo is not None)


async def delete_avatar(session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str) -> None:
    await session.execute(
        delete(AgentAvatar).where(
            AgentAvatar.tenant_id == tenant_id,
            AgentAvatar.agent_name == normalize_agent_name(agent_name),
        )
    )
