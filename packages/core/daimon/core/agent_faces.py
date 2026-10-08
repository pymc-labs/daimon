"""Deterministic mascot faces for tenant-scoped agent avatars.

The base and laugh mouth derive from the production mascot. Closed eye arcs
derive from a canonical expression sprite; plain eyes and restrained smiles use
the same ink colours.
Headwear and shades are the approved prototype layers. Assets are bundled with
this MIT-licensed package; no source art or network access is needed at runtime.
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

type FaceCombo = tuple[int, str, str, int, str, str, str]

_LAYER_DIR = Path(__file__).with_name("face_layers")
_SKIN = np.array([122, 189, 215], dtype=np.float32)


class _Colour(BaseModel):
    id: str
    hex: str
    weight: int
    group: str
    retired: bool
    legacy_index: int | None = None


class _Layer(BaseModel):
    id: str
    file: str | None
    sha256: str | None = None
    kind: str
    weight: int
    retired: bool
    legacy_index: int | None = None
    width: float | None = None
    cy: float | None = None
    top: float | None = None
    bottom: float | None = None
    full: bool = False
    colour_override: str | None = None
    placement: tuple[int, int] | None = None


class _Draw(BaseModel):
    headwear_share: float
    shades_share: float
    colour_group_targets: dict[str, float]


class _Classic(BaseModel):
    colour: str
    eyes: str
    mouth: str
    hat: str
    eyewear: str
    brows: str
    base: str


class _Manifest(BaseModel):
    schema_version: int = Field(alias="schema")
    draw: _Draw
    classic: _Classic
    colours: list[_Colour]
    layers: list[_Layer]


@dataclass(frozen=True)
class _Catalogue:
    manifest: _Manifest
    colours: tuple[_Colour, ...]
    colour_index: dict[str, int]
    colour_legacy: dict[int, str]
    colour_slots: tuple[int, ...]
    palette_rgb: np.ndarray
    hue_separation: np.ndarray
    rgb_separation: np.ndarray
    layers: dict[str, _Layer]
    by_kind: dict[str, tuple[_Layer, ...]]
    hats: tuple[_Layer, ...]
    hat_index: dict[str, int]
    hat_legacy: dict[int, str]


@lru_cache(maxsize=1)
def _catalogue() -> _Catalogue:
    """Load the shipped catalogue once; a deployment restart picks up edits."""
    manifest = _Manifest.model_validate_json((_LAYER_DIR / "manifest.json").read_text())
    if manifest.schema_version != 1:
        raise ValueError("unsupported face manifest schema")
    if not (
        0 <= manifest.draw.headwear_share <= 1
        and 0 <= manifest.draw.shades_share <= 1
        and abs(sum(manifest.draw.colour_group_targets.values()) - 1) < 0.001
    ):
        raise ValueError("invalid face draw shares")
    colours = tuple(manifest.colours)
    layers = {entry.id: entry for entry in manifest.layers}
    if len(colours) != len({entry.id for entry in colours}) or len(layers) != len(manifest.layers):
        raise ValueError("face manifest ids must be unique")
    if any(entry.weight < 0 for entry in (*colours, *manifest.layers)):
        raise ValueError("face manifest weights must be nonnegative")
    if any(
        layers.get(identifier) is None
        or layers[identifier].retired
        or layers[identifier].file is not None
        for identifier in ("hat-none", "eyewear-none")
    ):
        raise ValueError("face manifest must keep the empty hat and eyewear entries")
    if not colours or not any(entry.weight > 0 and not entry.retired for entry in colours):
        raise ValueError("face manifest needs active colours")
    if any(
        entry.group not in manifest.draw.colour_group_targets
        for entry in colours
        if not entry.retired
    ) or any(target < 0 for target in manifest.draw.colour_group_targets.values()):
        raise ValueError("invalid face colour groups")
    hashes: dict[str, str] = {}
    for layer in manifest.layers:
        if layer.file is not None and (
            Path(layer.file).name != layer.file or not (_LAYER_DIR / layer.file).is_file()
        ):
            raise ValueError(f"missing face layer: {layer.id}")
        if layer.file is not None:
            if layer.file not in hashes:
                hashes[layer.file] = hashlib.sha256(
                    (_LAYER_DIR / layer.file).read_bytes()
                ).hexdigest()
            if layer.sha256 != hashes[layer.file]:
                raise ValueError(f"face layer bytes changed: {layer.id}")
    by_kind = {
        kind: tuple(layer for layer in manifest.layers if layer.kind == kind)
        for kind in {layer.kind for layer in manifest.layers}
    }
    hats = by_kind.get("hat", ())
    palette_rgb = np.stack([_rgb(entry.hex) for entry in colours])
    hues = np.array([colorsys.rgb_to_hls(*(rgb / 255))[0] * 360 for rgb in palette_rgb])
    raw_hues = np.abs(hues[:, None] - hues[None, :])
    return _Catalogue(
        manifest=manifest,
        colours=colours,
        colour_index={entry.id: index for index, entry in enumerate(colours)},
        colour_legacy={
            entry.legacy_index: entry.id for entry in colours if entry.legacy_index is not None
        },
        colour_slots=tuple(
            index
            for index, entry in enumerate(colours)
            if not entry.retired
            for _ in range(entry.weight)
        ),
        palette_rgb=palette_rgb,
        hue_separation=np.minimum(raw_hues, 360 - raw_hues),
        rgb_separation=np.linalg.norm(palette_rgb[:, None, :] - palette_rgb[None, :, :], axis=2),
        layers=layers,
        by_kind=by_kind,
        hats=hats,
        hat_index={entry.id: index for index, entry in enumerate(hats)},
        hat_legacy={
            entry.legacy_index: entry.id for entry in hats if entry.legacy_index is not None
        },
    )


def _rgb(value: str) -> np.ndarray:
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


def _from_ids(ids: tuple[str, str, str, str, str, str, str]) -> FaceCombo:
    catalogue = _catalogue()
    colour, eyes, mouth, hat, eyewear, brows, base = ids
    try:
        if (
            catalogue.layers[eyes].kind != "eyes"
            or catalogue.layers[mouth].kind != "mouth"
            or catalogue.layers[hat].kind != "hat"
            or catalogue.layers[eyewear].kind != "eyewear"
            or catalogue.layers[brows].kind != "brows"
            or catalogue.layers[base].kind != "base"
        ):
            raise ValueError("invalid face combination")
        return (
            catalogue.colour_index[colour],
            eyes.removeprefix("eyes-"),
            mouth.removeprefix("mouth-"),
            catalogue.hat_index[hat],
            eyewear,
            brows.removeprefix("brows-"),
            base,
        )
    except KeyError as exc:
        raise ValueError("invalid face combination") from exc


def _to_ids(combo: FaceCombo) -> tuple[str, str, str, str, str, str, str]:
    catalogue = _catalogue()
    colour, eyes, mouth, hat, eyewear, brows, base = combo
    try:
        ids = (
            catalogue.colours[colour].id,
            f"eyes-{eyes}",
            f"mouth-{mouth}",
            catalogue.hats[hat].id,
            eyewear,
            f"brows-{brows}",
            base,
        )
        if _from_ids(ids) != combo:
            raise ValueError("invalid face combination")
        return ids
    except (IndexError, KeyError) as exc:
        raise ValueError("invalid face combination") from exc


@lru_cache(maxsize=1)
def classic_combo() -> FaceCombo:
    """Load the fixed built-in face only when face generation is enabled."""
    spec = _catalogue().manifest.classic
    return _from_ids(
        (spec.colour, spec.eyes, spec.mouth, spec.hat, spec.eyewear, spec.brows, spec.base)
    )


def encode_combo(combo: FaceCombo) -> str:
    """Persist stable catalogue ids, never tuple positions or draw weights."""
    return json.dumps({"v": 2, "variant": _to_ids(combo)}, separators=(",", ":"))


def decode_combo(raw: str) -> FaceCombo:
    value: object = json.loads(raw)
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        if mapping.get("v") == 2:
            variant = mapping.get("variant")
            if isinstance(variant, list):
                items = cast(list[object], variant)
                if len(items) in (6, 7) and all(isinstance(item, str) for item in items):
                    ids = cast(tuple[str, ...], tuple(items))
                    return _from_ids(
                        cast(
                            tuple[str, str, str, str, str, str, str],
                            (*ids[:6], ids[6] if len(ids) == 7 else "base"),
                        )
                    )
    elif isinstance(value, list):
        # The first PR draft used positional JSON. Keep it readable if a
        # staging row was created before the id-based encoding shipped.
        items = cast(list[object], value)
        if len(items) != 6:
            raise ValueError("invalid face combination")
        colour, eyes, mouth, hat, shades, brows = items
        if (
            type(colour) is int
            and type(hat) is int
            and type(shades) is bool
            and isinstance(eyes, str)
            and isinstance(mouth, str)
            and isinstance(brows, str)
        ):
            catalogue = _catalogue()
            try:
                return _from_ids(
                    (
                        catalogue.colour_legacy[colour],
                        f"eyes-{eyes}",
                        f"mouth-{mouth}",
                        catalogue.hat_legacy[hat],
                        "eyewear-shades" if shades else "eyewear-none",
                        f"brows-{brows}",
                        "base",
                    )
                )
            except KeyError as exc:
                raise ValueError("invalid face combination") from exc
    raise ValueError("invalid face combination")


def _active(kind: str) -> tuple[_Layer, ...]:
    return tuple(
        entry
        for entry in _catalogue().by_kind.get(kind, ())
        if not entry.retired and entry.weight > 0
    )


def _weighted_byte(value: int, entries: tuple[_Layer, ...]) -> _Layer:
    total = sum(entry.weight for entry in entries)
    if total <= 0:
        raise ValueError("face manifest has no active layers")
    ticket = value * total // 256
    for entry in entries:
        if ticket < entry.weight:
            return entry
        ticket -= entry.weight
    raise AssertionError("unreachable weighted layer draw")


def _weighted_modulo(value: int, entries: tuple[_Layer, ...]) -> _Layer:
    total = sum(entry.weight for entry in entries)
    if total <= 0:
        raise ValueError("face manifest has no active layers")
    ticket = value % total
    for entry in entries:
        if ticket < entry.weight:
            return entry
        ticket -= entry.weight
    raise AssertionError("unreachable weighted layer draw")


@lru_cache(maxsize=4)
def _assets(
    size: int,
    base_id: str,
) -> tuple[
    np.ndarray, np.ndarray, np.ndarray, dict[str, tuple[Image.Image, tuple[int, int, int, int]]]
]:
    catalogue = _catalogue()
    base_spec = catalogue.layers[base_id]
    assert base_spec.file is not None
    with Image.open(_LAYER_DIR / base_spec.file) as source:
        base = np.asarray(
            source.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
        ).astype(np.float32)
    distance = np.linalg.norm(base - _SKIN, axis=2)
    skin_weight = np.clip(1 - (distance - 22) / 18, 0, 1)[..., None]
    shade = np.array([77, 150, 190], dtype=np.float32)
    shade_weight = np.clip(1 - (np.linalg.norm(base - shade, axis=2) - 30) / 25, 0, 1)[
        ..., None
    ] * (1 - skin_weight)
    layers: dict[str, tuple[Image.Image, tuple[int, int, int, int]]] = {}
    files: dict[str, tuple[Image.Image, tuple[int, int, int, int]]] = {}
    for spec in catalogue.manifest.layers:
        if spec.kind == "base" or spec.file is None:
            continue
        if spec.file not in files:
            with Image.open(_LAYER_DIR / spec.file) as source:
                layer = source.convert("RGBA")
            box = layer.getchannel("A").getbbox()
            if box is None:
                raise ValueError(f"empty face layer: {spec.id}")
            files[spec.file] = (layer.crop(box), box)
        part, box = files[spec.file]
        placement = (
            (
                spec.placement[0],
                spec.placement[1],
                spec.placement[0] + box[2] - box[0],
                spec.placement[1] + box[3] - box[1],
            )
            if spec.placement is not None
            else box
        )
        layers[spec.id] = (part, placement)
    return base, skin_weight, shade_weight, layers


def _recolour(part: Image.Image, colour: str) -> Image.Image:
    pixels = np.asarray(part).astype(np.float32)
    rgb = pixels[..., :3]
    luminosity = rgb.mean(axis=2, keepdims=True)
    body = luminosity > 70
    lit = luminosity[body].mean() if body.any() else 255.0
    shade = np.clip(luminosity / max(lit, 1), 0.6, 1.3)
    new_rgb = np.where(body, np.clip(_rgb(colour) * shade, 0, 255), rgb)
    return Image.fromarray(np.dstack([new_rgb, pixels[..., 3]]).astype(np.uint8), "RGBA")


def render_image(combo: FaceCombo, size: int = 512) -> Image.Image:
    """Render a face at an exact square pixel size."""
    if not 20 <= size <= 1024:
        raise ValueError("face size must be between 20 and 1024 pixels")
    canvas_size = 128 if size <= 128 else 1024
    catalogue = _catalogue()
    base, skin_weight, shade_weight, layers = _assets(canvas_size, combo[6])
    colour, eyes, mouth, hat, eyewear, brows, _base_id = combo
    target = catalogue.palette_rgb[colour]
    recoloured = base * (1 - skin_weight) + target * skin_weight
    recoloured = recoloured * (1 - shade_weight) + target * 0.72 * shade_weight
    canvas = Image.fromarray(recoloured.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")
    scale = canvas_size / 1024

    def place(
        name: str,
        *,
        width: float | None = None,
        cy: float | None = None,
        bottom: float | None = None,
        colour_override: str | None = None,
        full: bool = False,
    ) -> None:
        part, box = layers[name]
        if colour_override is not None:
            part = _recolour(part, colour_override)
        if full:
            part = part.resize(
                (max(1, round(part.width * scale)), max(1, round(part.height * scale))),
                Image.Resampling.LANCZOS,
            )
            canvas.alpha_composite(part, (round(box[0] * scale), round(box[1] * scale)))
            return
        assert width is not None
        factor = width * canvas_size / part.width
        part = part.resize(
            (max(1, round(part.width * factor)), max(1, round(part.height * factor))),
            Image.Resampling.LANCZOS,
        )
        x = round(canvas_size / 2 - part.width / 2)
        y = (
            round(bottom * canvas_size - part.height)
            if bottom is not None
            else round((cy or 0) * canvas_size - part.height / 2)
        )
        canvas.alpha_composite(part, (x, y))

    brows_spec = catalogue.layers[f"brows-{brows}"]
    place(brows_spec.id, full=brows_spec.full)
    mouth_spec = catalogue.layers[f"mouth-{mouth}"]
    mouth_width = mouth_spec.width
    mouth_top = mouth_spec.top
    assert mouth_width is not None and mouth_top is not None
    part, _ = layers[f"mouth-{mouth}"]
    mouth_height = part.height * mouth_width / part.width
    place(f"mouth-{mouth}", width=mouth_width, cy=mouth_top + mouth_height / 2)
    if eyewear == "eyewear-none":
        eye_spec = catalogue.layers[f"eyes-{eyes}"]
        place(eye_spec.id, width=eye_spec.width, cy=eye_spec.cy)
    else:
        eyewear_spec = catalogue.layers[eyewear]
        place(eyewear_spec.id, width=eyewear_spec.width, cy=eyewear_spec.cy)
    offset = round(0.06 * canvas_size)
    result = Image.new("RGBA", (canvas_size, canvas_size), tuple(int(v) for v in target) + (255,))
    result.alpha_composite(canvas.crop((0, 0, canvas_size, canvas_size - offset)), (0, offset))
    canvas = result
    hat_spec = catalogue.hats[hat]
    if hat_spec.full:
        place(hat_spec.id, full=True)
    elif hat_spec.file is not None:
        place(
            hat_spec.id,
            width=hat_spec.width,
            bottom=hat_spec.bottom,
            colour_override=hat_spec.colour_override,
        )
    return canvas.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)


def render(combo: FaceCombo, size: int = 512) -> bytes:
    """Render a deterministic, metadata-free PNG."""
    output = io.BytesIO()
    render_image(combo, size).save(output, format="PNG", compress_level=6)
    return output.getvalue()


@lru_cache(maxsize=4096)
def thumbnail(combo: FaceCombo) -> np.ndarray:
    mask = Image.new("L", (80, 80), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 79, 79), fill=255)
    circle = (
        np.asarray(mask.resize((20, 20), Image.Resampling.LANCZOS), dtype=np.float32)[..., None]
        / 255
    )
    picture = np.asarray(
        render_image(combo, 128).resize((20, 20), Image.Resampling.LANCZOS), dtype=np.float32
    )
    return (picture * circle).reshape(-1)


def candidates(agent_key: str, count: int = 48) -> list[FaceCombo]:
    """Stable draws favour the classic face and use restrained canonical variants."""
    result: list[FaceCombo] = []
    catalogue = _catalogue()
    classic = classic_combo()
    eyes_options = _active("eyes")
    mouth_options = _active("mouth")
    brow_options = _active("brows")
    hat_options = tuple(layer for layer in _active("hat") if layer.file is not None)
    base_options = _active("base")
    eyewear_options = tuple(layer for layer in _active("eyewear") if layer.file is not None)
    if (
        (catalogue.manifest.draw.headwear_share > 0 and not hat_options)
        or not base_options
        or not catalogue.colour_slots
    ):
        raise ValueError("face manifest has no drawable variants")
    attempt = 0
    while len(result) < count:
        digest = hashlib.sha256(f"{agent_key}|{attempt}".encode()).digest()
        attempt += 1
        hat = (
            catalogue.hat_index["hat-none"]
            if digest[4] / 256 >= catalogue.manifest.draw.headwear_share
            else catalogue.hat_index[_weighted_modulo(digest[5], hat_options).id]
        )
        eyewear = (
            _weighted_modulo(digest[10], eyewear_options).id
            if eyewear_options and digest[6] / 256 < catalogue.manifest.draw.shades_share
            else "eyewear-none"
        )
        eyes = _weighted_byte(digest[7], eyes_options).id.removeprefix("eyes-")
        mouth = _weighted_byte(digest[8], mouth_options).id.removeprefix("mouth-")
        classic_brows = catalogue.layers[f"brows-{classic[5]}"]
        brows = (
            classic[5]
            if mouth == classic[2] and not classic_brows.retired and classic_brows.weight > 0
            else _weighted_byte(digest[3], brow_options).id.removeprefix("brows-")
        )
        combo: FaceCombo = (
            catalogue.colour_slots[int.from_bytes(digest[:2]) % len(catalogue.colour_slots)],
            eyes,
            mouth,
            hat,
            eyewear,
            brows,
            _weighted_byte(digest[9], base_options).id,
        )
        if combo != classic and combo not in result:
            result.append(combo)
    return result


def choose(
    agent_key: str,
    existing: list[FaceCombo],
    *,
    count: int | None = None,
    min_plain: float = 4.0,
    existing_thumbnails: list[np.ndarray] | None = None,
) -> FaceCombo:
    """Balance colours first, then prefer distinct plain canonical faces."""
    used = set(existing)
    catalogue = _catalogue()
    classic = classic_combo()
    candidate_count = count if count is not None else (96 if len(existing) < 24 else 48)
    options = [combo for combo in candidates(agent_key, candidate_count) if combo not in used]
    if not options:
        raise ValueError("no unused face candidates")
    plain = [combo for combo in options if combo[3] == classic[3] and combo[4] == "eyewear-none"]
    colour_uses = Counter(combo[0] for combo in existing)
    if not existing:
        return min(
            plain or options,
            key=lambda combo: (
                -catalogue.colours[combo[0]].weight,
                not _is_classic(combo),
                combo[0],
            ),
        )
    previous = np.stack(
        existing_thumbnails
        if existing_thumbnails is not None
        else [thumbnail(combo) for combo in existing]
    )
    previous_colours = [combo[0] for combo in existing]
    category_uses = Counter(catalogue.colours[colour].group for colour in previous_colours)
    eye_uses = Counter(combo[1] for combo in existing)
    mouth_uses = Counter(combo[2] for combo in existing)
    hue_spread = catalogue.hue_separation[:, previous_colours].min(axis=1)
    rgb_spread = catalogue.rgb_separation[:, previous_colours].min(axis=1)
    distances: dict[FaceCombo, float] = {}

    def distance(combo: FaceCombo) -> float:
        if combo not in distances:
            distances[combo] = float(np.abs(previous - thumbnail(combo)).mean(axis=1).min())
        return distances[combo]

    def pick(pool: list[FaceCombo]) -> FaceCombo:
        def eye_error(combo: FaceCombo) -> float:
            total = sum(layer.weight for layer in _active("eyes"))
            return (
                sum(
                    abs(
                        eye_uses[layer.id.removeprefix("eyes-")]
                        + (combo[1] == layer.id.removeprefix("eyes-"))
                        - (len(existing) + 1) * layer.weight / total
                    )
                    for layer in _active("eyes")
                )
                / 2
            )

        def mouth_error(combo: FaceCombo) -> float:
            total = sum(layer.weight for layer in _active("mouth"))
            return sum(
                abs(
                    mouth_uses[layer.id.removeprefix("mouth-")]
                    + (combo[2] == layer.id.removeprefix("mouth-"))
                    - (len(existing) + 1) * layer.weight / total
                )
                for layer in _active("mouth")
            )

        def category_error(combo: FaceCombo) -> float:
            category = catalogue.colours[combo[0]].group
            return sum(
                abs(category_uses[group] + (group == category) - (len(existing) + 1) * target)
                for group, target in catalogue.manifest.draw.colour_group_targets.items()
            )

        return min(
            pool,
            key=lambda combo: (
                -hue_spread[combo[0]],
                eye_error(combo),
                mouth_error(combo),
                category_error(combo),
                -rgb_spread[combo[0]],
                colour_uses[combo[0]]
                * max(colour.weight for colour in catalogue.colours)
                / catalogue.colours[combo[0]].weight,
                not _is_classic(combo),
                -distance(combo),
            ),
        )

    # Keep props in the minority even when a dense tenant needs distinction.
    hat_share = sum(combo[3] != classic[3] for combo in existing) / (len(existing) + 1)
    shade_share = sum(combo[4] != "eyewear-none" for combo in existing) / (len(existing) + 1)
    balanced = [
        combo
        for combo in options
        if (combo[3] == classic[3] or hat_share < catalogue.manifest.draw.headwear_share)
        and (combo[4] == "eyewear-none" or shade_share < catalogue.manifest.draw.shades_share)
    ]
    options = balanced or options
    plain = [combo for combo in options if combo[3] == classic[3] and combo[4] == "eyewear-none"]
    classic_share = sum(_is_classic(combo) for combo in existing) / (len(existing) + 1)
    classic_plain = [combo for combo in plain if _is_classic(combo)]
    if classic_plain and len(existing) < 24 and classic_share < 0.55:
        distinct_classic = [combo for combo in classic_plain if distance(combo) >= 3]
        if distinct_classic:
            return pick(distinct_classic)
    distinct_plain = [combo for combo in plain if distance(combo) >= min_plain]
    if distinct_plain:
        return pick(distinct_plain)
    distinct = [combo for combo in options if distance(combo) >= 4]
    return pick(distinct) if distinct else max(options, key=distance)


def _is_classic(combo: FaceCombo) -> bool:
    classic = classic_combo()
    return (
        combo[1:3] == classic[1:3]
        and combo[3] == classic[3]
        and combo[4] == "eyewear-none"
        and combo[6] == classic[6]
    )


def assign(agent_keys: list[str]) -> dict[str, FaceCombo]:
    """Assign in creation order while reserving the built-in face's colour."""
    assigned: dict[str, FaceCombo] = {}
    existing = [classic_combo()]
    for key in agent_keys:
        assigned[key] = choose(key, existing)
        existing.append(assigned[key])
    return assigned
