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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def test_render_is_stable_and_round_trips_its_combination() -> None:
    combo: FaceCombo = (12, "arc", "smile", 3, False, "raised")
    png = render(combo, 512)
    assert png == render(combo, 512)
    assert decode_combo(encode_combo(combo)) == combo
    assert Image.open(BytesIO(png)).size == (512, 512)
    assert Image.open(BytesIO(render(CLASSIC, 128))).size == (128, 128)
    with pytest.raises(ValueError, match="invalid face"):
        decode_combo('[0,"arc","smile",0,false,"not-a-brow"]')


def test_props_reach_inside_the_circular_header_crop() -> None:
    plain = (0, "arc", "smile", 0, False, "default")
    hat = (0, "arc", "smile", 1, False, "default")
    headset = (0, "arc", "smile", 11, False, "default")
    shades = (0, "arc", "smile", 0, True, "default")
    mask = Image.new("L", (36, 36), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 35, 35), fill=255)
    inside = np.asarray(mask) > 0
    base = np.asarray(render_image(plain, 36), dtype=np.int16)
    for combo in (hat, headset, shades):
        delta = np.abs(np.asarray(render_image(combo, 36), dtype=np.int16) - base)
        assert float(delta[inside].mean()) > 2, "the prop must be visible inside a circular header"


def test_assignment_prefers_hue_separation_before_expression_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous: FaceCombo = (0, "arc", "laugh", 0, False, "default")
    same_colour: FaceCombo = (0, "plain", "smile", 0, False, "raised")
    fresh_colour: FaceCombo = (1, "arc", "laugh", 0, False, "default")
    pictures = {previous: 0, same_colour: 20, fresh_colour: 6}
    monkeypatch.setattr(agent_faces, "candidates", lambda _key, _count: [same_colour, fresh_colour])
    monkeypatch.setattr(agent_faces, "thumbnail", lambda combo: np.array([pictures[combo]]))
    assert choose("analyst", [previous], count=2) == fresh_colour


def test_thread_palette_spreads_hues_around_the_builtin_face() -> None:
    names = [
        "analyst",
        "research",
        "ops",
        "finance-bot",
        "support",
        "data-eng",
        "qa-sec-b-76b4ca",
        "sales-copilot",
    ]
    faces = assign(names)
    colours = [CLASSIC[0], *(combo[0] for combo in faces.values())]
    separation = agent_faces._HUE_SEPARATION[np.ix_(colours, colours)].copy()
    np.fill_diagonal(separation, np.inf)
    assert float(separation.min()) >= 20


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
        combo[1:3] == ("arc", "laugh") and combo[3] == 0 and not combo[4]
        for combo in faces.values()
    )
    assert 200 <= hats <= 325, "props provide distinction in a dense tenant"
    assert 75 <= shades <= 150
    assert len(colours) == 72
    assert min(colours.values()) >= 1
    assert max(colours.values()) <= 12
    assignment_by_weight = Counter(
        {
            weight: sum(
                count
                for index, count in colours.items()
                if agent_faces._COLOUR_WEIGHTS[index] == weight
            )
            for weight in (1, 2, 4)
        }
    )
    assert assignment_by_weight[4] > assignment_by_weight[2] > assignment_by_weight[1]
    assert assignment_by_weight[1] < 75, "yellow and lime should stay uncommon"
    assert classic >= 20, "the production face should be a common draw"
    assert set(eyes) == {"arc", "plain"}
    assert set(mouths) == {"laugh", "smile", "soft-open"}
    assert 370 <= eyes["arc"] <= 380
    assert 290 <= mouths["laugh"] <= 310


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
    db_engine: AsyncEngine,
    db_clean: None,
) -> None:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory.begin() as session:
        tenant = await make_tenant(session)

    async def create(name: str) -> FaceCombo | None:
        async with session_factory.begin() as session:
            row = await get_or_create_avatar(
                session, tenant_id=tenant.id, agent_name=name, face_enabled=True
            )
            return row.face_combo

    first, second = await asyncio.gather(create("Analyst"), create("Research"))
    assert first is not None and second is not None and first != second
