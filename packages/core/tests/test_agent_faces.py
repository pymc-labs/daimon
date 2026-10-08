"""Face rendering and tenant assignment at actual message-header size."""

from __future__ import annotations

import asyncio
from collections import Counter
from io import BytesIO

import numpy as np
import pytest
from daimon.core import agent_faces
from daimon.core.agent_faces import (
    CLASSIC,
    FaceCombo,
    assign,
    choose,
    decode_combo,
    encode_combo,
    render,
    render_image,
    thumbnail,
)
from daimon.core.stores.agent_avatars import get_or_create_avatar, replace_avatar, reset_avatar
from daimon.testing.factories import make_tenant
from PIL import Image, ImageDraw
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_render_is_stable_and_round_trips_its_combination() -> None:
    combo: FaceCombo = (12, "wink", "sad", 3, False, "raised")
    png = render(combo, 512)
    assert png == render(combo, 512)
    assert decode_combo(encode_combo(combo)) == combo
    assert Image.open(BytesIO(png)).size == (512, 512)
    assert Image.open(BytesIO(render(CLASSIC, 128))).size == (128, 128)
    with pytest.raises(ValueError, match="invalid face"):
        decode_combo('[0,"happy","happy",0,false,"not-a-brow"]')


def test_props_reach_inside_the_circular_header_crop() -> None:
    plain = (0, "happy", "happy", 0, False, "default")
    hat = (0, "happy", "happy", 1, False, "default")
    headset = (0, "happy", "happy", 11, False, "default")
    shades = (0, "happy", "happy", 0, True, "default")
    mask = Image.new("L", (36, 36), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 35, 35), fill=255)
    inside = np.asarray(mask) > 0
    base = np.asarray(render_image(plain, 36), dtype=np.int16)
    for combo in (hat, headset, shades):
        delta = np.abs(np.asarray(render_image(combo, 36), dtype=np.int16) - base)
        assert float(delta[inside].mean()) > 2, "the prop must be visible inside a circular header"


def test_assignment_prefers_underused_colour_before_expression_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous: FaceCombo = (0, "happy", "laugh", 0, False, "default")
    same_colour: FaceCombo = (0, "angry", "angry", 0, False, "default")
    fresh_colour: FaceCombo = (1, "happy", "laugh", 0, False, "default")
    pictures = {previous: 0, same_colour: 20, fresh_colour: 6}
    monkeypatch.setattr(agent_faces, "candidates", lambda _key, _count: [same_colour, fresh_colour])
    monkeypatch.setattr(agent_faces, "thumbnail", lambda combo: np.array([pictures[combo]]))
    assert choose("analyst", [previous], count=2) == fresh_colour


def test_five_hundred_faces_are_distinct_at_twenty_pixels() -> None:
    names = [f"agent_{i:04d}" for i in range(500)]
    faces = assign(names)
    assert len(set(faces.values())) == 500
    pictures = np.stack([thumbnail(faces[name]) for name in names])
    distances = np.abs(pictures[:, None, :] - pictures[None, :, :]).mean(axis=2)
    np.fill_diagonal(distances, np.inf)
    assert not np.any(distances < 4), "near-identical faces remain at 20 px"
    hats = sum(combo[3] != 0 for combo in faces.values())
    shades = sum(combo[4] for combo in faces.values())
    colours = Counter(combo[0] for combo in faces.values())
    eyes = Counter(combo[1] for combo in faces.values())
    mouths = Counter(combo[2] for combo in faces.values())
    classic = sum(
        combo[1:3] == ("happy", "laugh") and combo[3] == 0 and not combo[4]
        for combo in faces.values()
    )
    assert 75 <= hats <= 150, "headwear should remain a minority of assigned faces"
    assert 25 <= shades <= 75, "shades should remain a minority of assigned faces"
    assert len(colours) == 36
    assert max(colours.values()) - min(colours.values()) <= 3
    assert classic >= 20, "the production face should be a common draw"
    assert sum(eyes[name] for name in ("pointing", "waving", "typing", "shrug", "turn-front")) < 50
    assert mouths["wink"] < 30, "the tongue-out expression should be uncommon"


@pytest.mark.asyncio
async def test_face_assignment_persists_and_reset_keeps_the_same_combination(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first = await get_or_create_avatar(
        db_session, tenant_id=tenant.id, agent_name="Analyst", face_enabled=True
    )
    second = await get_or_create_avatar(
        db_session, tenant_id=tenant.id, agent_name="Research", face_enabled=True
    )
    assert first.face_combo is not None and second.face_combo is not None
    assert first.face_combo != second.face_combo
    assert Image.open(BytesIO(first.png)).size == (512, 512)
    upload = BytesIO()
    Image.new("RGB", (256, 256), "red").save(upload, format="PNG")
    uploaded = await replace_avatar(
        db_session,
        tenant_id=tenant.id,
        agent_name="Analyst",
        png=upload.getvalue(),
        source="upload",
    )
    assert uploaded.face_combo == first.face_combo
    reset = await reset_avatar(
        db_session, tenant_id=tenant.id, agent_name="Analyst", face_enabled=True
    )
    assert reset.face_combo == first.face_combo
    assert reset.png == first.png
    assert reset.token != first.token


@pytest.mark.asyncio
async def test_face_switch_off_keeps_initials_and_creates_no_face_combination(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    avatar = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Analyst")
    assert avatar.face_combo is None
    assert Image.open(BytesIO(avatar.png)).size == (256, 256)


@pytest.mark.asyncio
async def test_concurrent_first_uses_assign_distinct_tenant_faces(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)

    async def create(name: str) -> FaceCombo | None:
        async with db_session_factory.begin() as session:
            row = await get_or_create_avatar(
                session, tenant_id=tenant.id, agent_name=name, face_enabled=True
            )
            return row.face_combo

    first, second = await asyncio.gather(create("Analyst"), create("Research"))
    assert first is not None and second is not None and first != second
