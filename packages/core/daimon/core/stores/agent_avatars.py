"""Tenant-scoped avatar storage and deterministic default artwork."""

from __future__ import annotations

import hashlib
import io
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.core._models import AgentAvatar
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from sqlalchemy import delete, select, update
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


def normalize_agent_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def generate_default_png(name: str) -> bytes:
    words = name.replace("-", " ").replace("_", " ").split()
    initials = "".join(word[0].upper() for word in words[:2]) or "?"
    colour = _PALETTE[
        int.from_bytes(hashlib.sha256(normalize_agent_name(name).encode()).digest()[:4], "big")
        % len(_PALETTE)
    ]
    image = Image.new("RGB", (256, 256), colour)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=100)
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


def _row(orm: AgentAvatar) -> AvatarRow:
    return AvatarRow(token=orm.token, sha256=orm.sha256, png=orm.png, source=orm.source)


async def get_or_create_avatar(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str
) -> AvatarRow:
    key = normalize_agent_name(agent_name)
    existing = await session.get(AgentAvatar, (tenant_id, key))
    if existing is not None:
        return _row(existing)
    png = generate_default_png(agent_name)
    await session.execute(
        pg_insert(AgentAvatar)
        .values(
            tenant_id=tenant_id,
            agent_name=key,
            token=secrets.token_urlsafe(24),
            sha256=hashlib.sha256(png).hexdigest(),
            png=png,
            source="default",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "agent_name"])
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
) -> AvatarRow:
    if source not in ("default", "upload") or len(png) > _MAX_PNG_BYTES:
        raise ValueError("avatar must be a PNG of at most 256 KB")
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG" or image.size != (256, 256):
                raise ValueError("avatar must be a 256×256 PNG")
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("avatar must be a valid PNG") from exc
    key = normalize_agent_name(agent_name)
    token = secrets.token_urlsafe(24)
    sha = hashlib.sha256(png).hexdigest()
    await session.execute(
        pg_insert(AgentAvatar)
        .values(
            tenant_id=tenant_id,
            agent_name=key,
            token=token,
            sha256=sha,
            png=png,
            source=source,
            updated_by_account_id=updated_by_account_id,
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "agent_name"],
            set_={
                "token": token,
                "sha256": sha,
                "png": png,
                "source": source,
                "updated_by_account_id": updated_by_account_id,
                "updated_at": datetime.now(UTC),
            },
        )
    )
    return AvatarRow(token=token, sha256=sha, png=png, source=source)


async def reset_avatar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    updated_by_account_id: uuid.UUID | None = None,
) -> AvatarRow:
    return await replace_avatar(
        session,
        tenant_id=tenant_id,
        agent_name=agent_name,
        png=generate_default_png(agent_name),
        source="default",
        updated_by_account_id=updated_by_account_id,
    )


async def get_avatar_by_token(session: AsyncSession, *, token: str) -> AvatarRow | None:
    orm = (
        await session.scalars(select(AgentAvatar).where(AgentAvatar.token == token))
    ).one_or_none()
    return None if orm is None else _row(orm)


async def delete_avatar(session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str) -> None:
    await session.execute(
        delete(AgentAvatar).where(
            AgentAvatar.tenant_id == tenant_id,
            AgentAvatar.agent_name == normalize_agent_name(agent_name),
        )
    )


async def rename_avatar(
    session: AsyncSession, *, tenant_id: uuid.UUID, old_name: str, new_name: str
) -> None:
    old_key, new_key = normalize_agent_name(old_name), normalize_agent_name(new_name)
    if old_key != new_key:
        await session.execute(
            update(AgentAvatar)
            .where(AgentAvatar.tenant_id == tenant_id, AgentAvatar.agent_name == old_key)
            .values(agent_name=new_key)
        )
