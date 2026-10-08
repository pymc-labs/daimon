"""Deterministic mascot faces for tenant-scoped agent avatars.

The base derives from the production mascot. Eye and mouth layers derive from
canonical expression sprites, flattened and traced per ink at 1024 pixels.
Headwear and shades are the approved prototype layers. Assets are bundled with
this MIT-licensed package; no source art or network access is needed at runtime.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image, ImageDraw

type FaceCombo = tuple[int, str, str, int, bool, str]

_LAYER_DIR = Path(__file__).with_name("face_layers")
_SKIN = np.array([122, 189, 215], dtype=np.float32)
_PALETTE = (
    "#7AB8D6",
    "#7FA8E0",
    "#9ED8C3",
    "#F2B66B",
    "#B9A8E3",
    "#F5D76E",
    "#F6C9A8",
    "#A7C98A",
    "#9AA8D8",
    "#E8D7A6",
    "#8FC4A8",
    "#8FD0E6",
    "#C9B6E4",
    "#F0A97A",
    "#B5D88A",
    "#A9C2E8",
    "#E7A1B0",
    "#7FC7C0",
    "#D9C27A",
    "#B7B7E9",
    "#F2C5D3",
    "#9CCB6E",
    "#D7A4E0",
    "#7FB6A0",
    "#6FA8DC",
    "#F4B183",
    "#C5E0B4",
    "#FFD966",
    "#B4A7D6",
    "#EA9999",
    "#76A5AF",
    "#D5A6BD",
    "#A4C2F4",
    "#B6D7A8",
    "#F9CB9C",
    "#8E7CC3",
)
_EXPRESSIONS = (
    "angry",
    "excited",
    "happy",
    "sad",
    "sleepy",
    "thinking",
    "wink",
    "pointing",
    "waving",
    "typing",
    "shrug",
    "turn-front",
)
_CANON_EYES = ("happy", "excited", "wink", "angry", "sleepy", "thinking", "sad")
_CANON_MOUTHS = ("laugh", "happy", "excited", "angry", "sleepy", "thinking", "sad")
_NOVEL_EYES = tuple(name for name in _EXPRESSIONS if name not in _CANON_EYES)
_NOVEL_MOUTHS = tuple(name for name in _EXPRESSIONS if name not in _CANON_MOUTHS)
_VALID_MOUTHS = (*_EXPRESSIONS, "laugh")
_BROWS = ("default", "raised", "angry", "worried", "flat")
_HATS: tuple[tuple[str | None, str | None], ...] = (
    (None, None),
    ("hardhat", None),
    ("hardhat", "#F28C28"),
    ("hardhat", "#F4F4F0"),
    ("beanie", None),
    ("beanie", "#3E6FB0"),
    ("beanie", "#3F9E5A"),
    ("cap", None),
    ("cap", "#C23B3B"),
    ("cap", "#6A4BA8"),
    ("gradcap", None),
    ("headset", None),
)
_HAT_WIDTH = {"hardhat": 0.58, "gradcap": 0.70, "beanie": 0.58, "cap": 0.60}
_MOUTH_WIDTH = {
    "angry": 0.26,
    "excited": 0.42,
    "happy": 0.33,
    "laugh": 0.40,
    "sad": 0.26,
    "sleepy": 0.42,
    "thinking": 0.27,
    "wink": 0.34,
    "pointing": 0.35,
    "waving": 0.27,
    "typing": 0.32,
    "shrug": 0.32,
    "turn-front": 0.32,
}
CLASSIC: FaceCombo = (0, "happy", "laugh", 0, False, "default")
HAT_SHARE = 0.20
SHADES_SHARE = 0.10


def encode_combo(combo: FaceCombo) -> str:
    return json.dumps(combo, separators=(",", ":"))


def decode_combo(raw: str) -> FaceCombo:
    raw_value: object = json.loads(raw)
    if not isinstance(raw_value, list):
        raise ValueError("invalid face combination")
    value = cast(list[object], raw_value)
    if len(value) != 6:
        raise ValueError("invalid face combination")
    colour, eyes, mouth, hat, shades, brows = value
    if (
        type(colour) is not int
        or not 0 <= colour < len(_PALETTE)
        or not isinstance(eyes, str)
        or eyes not in _EXPRESSIONS
        or not isinstance(mouth, str)
        or mouth not in _VALID_MOUTHS
        or type(hat) is not int
        or not 0 <= hat < len(_HATS)
        or type(shades) is not bool
        or not isinstance(brows, str)
        or brows not in _BROWS
    ):
        raise ValueError("invalid face combination")
    return (colour, eyes, mouth, hat, shades, brows)


def _rgb(value: str) -> np.ndarray:
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


@lru_cache(maxsize=2)
def _assets(
    size: int,
) -> tuple[
    np.ndarray, np.ndarray, np.ndarray, dict[str, tuple[Image.Image, tuple[int, int, int, int]]]
]:
    with Image.open(_LAYER_DIR / "base.png") as source:
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
    for path in _LAYER_DIR.glob("*.png"):
        if path.name == "base.png":
            continue
        with Image.open(path) as source:
            layer = source.convert("RGBA")
        box = layer.getchannel("A").getbbox()
        if box is None:
            raise ValueError(f"empty face layer: {path.name}")
        # The headset is stored tightly cropped to keep the wheel small. Its
        # original placement is needed because it wraps around the whole face.
        placement = (
            (29, 64, 29 + box[2] - box[0], 64 + box[3] - box[1])
            if path.stem == "hat-headset"
            else box
        )
        layers[path.stem] = (layer.crop(box), placement)
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
    base, skin_weight, shade_weight, layers = _assets(canvas_size)
    colour, eyes, mouth, hat, shades, brows = combo
    target = _rgb(_PALETTE[colour])
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

    place(f"brows-{brows}", full=True)
    mouth_width = _MOUTH_WIDTH[mouth]
    part, _ = layers[f"mouth-{mouth}"]
    mouth_height = part.height * mouth_width / part.width
    place(f"mouth-{mouth}", width=mouth_width, cy=0.40 + mouth_height / 2)
    if not shades:
        place(f"eyes-{eyes}", width=0.66 if eyes == "happy" else 0.6072, cy=0.345)
    else:
        place("eyewear-shades", width=0.64, cy=0.345)
    hat_name, hat_colour = _HATS[hat]
    if hat_name == "headset":
        place("hat-headset", full=True)
    elif hat_name is not None:
        place(
            f"hat-{hat_name}", width=_HAT_WIDTH[hat_name], bottom=0.215, colour_override=hat_colour
        )
    offset = round(0.06 * canvas_size)
    result = Image.new("RGBA", (canvas_size, canvas_size), tuple(int(v) for v in target) + (255,))
    result.alpha_composite(canvas.crop((0, 0, canvas_size, canvas_size - offset)), (0, offset))
    return result.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)


def render(combo: FaceCombo, size: int = 512) -> bytes:
    """Render a deterministic, metadata-free PNG."""
    output = io.BytesIO()
    render_image(combo, size).save(output, format="PNG", compress_level=6)
    return output.getvalue()


@lru_cache(maxsize=32768)
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


def candidates(agent_key: str, count: int = 32) -> list[FaceCombo]:
    """Stable draws favour canonical faces; props and novelty stay uncommon."""
    result: list[FaceCombo] = []
    attempt = 0
    while len(result) < count:
        digest = hashlib.sha256(f"{agent_key}|{attempt}".encode()).digest()
        attempt += 1
        hat = 0 if digest[4] / 256 >= HAT_SHARE else 1 + digest[5] % (len(_HATS) - 1)
        shades = digest[6] / 256 < SHADES_SHARE
        expression_roll = digest[7]
        if expression_roll < 64:
            eyes, mouth = "happy", "laugh"
        elif expression_roll < 176:
            expression = _CANON_EYES[digest[1] % len(_CANON_EYES)]
            eyes = expression
            mouth = "happy" if expression == "wink" else expression
        elif expression_roll < 253:
            eyes = _CANON_EYES[digest[1] % len(_CANON_EYES)]
            mouth = _CANON_MOUTHS[digest[2] % len(_CANON_MOUTHS)]
        else:
            eyes = _NOVEL_EYES[digest[1] % len(_NOVEL_EYES)]
            mouth = _NOVEL_MOUTHS[digest[2] % len(_NOVEL_MOUTHS)]
        combo: FaceCombo = (
            digest[0] % len(_PALETTE),
            eyes,
            mouth,
            hat,
            shades,
            _BROWS[digest[3] % len(_BROWS)],
        )
        if combo != CLASSIC and combo not in result:
            result.append(combo)
    return result


def choose(
    agent_key: str, existing: list[FaceCombo], *, count: int = 32, min_plain: float = 5.0
) -> FaceCombo:
    """Balance colours first, then prefer distinct plain canonical faces."""
    used = set(existing)
    options = [combo for combo in candidates(agent_key, count) if combo not in used]
    if not options:
        raise ValueError("no unused face candidates")
    plain = [combo for combo in options if combo[3] == 0 and not combo[4]]
    colour_uses = Counter(combo[0] for combo in existing)
    if not existing:
        return min(plain or options, key=lambda combo: (not _is_classic(combo), combo[0]))
    previous = np.stack([thumbnail(combo) for combo in existing])
    prefer_classic = hashlib.sha256(f"{agent_key}|classic".encode()).digest()[0] < 128
    distances: dict[FaceCombo, float] = {}

    def distance(combo: FaceCombo) -> float:
        if combo not in distances:
            distances[combo] = float(np.abs(previous - thumbnail(combo)).mean(axis=1).min())
        return distances[combo]

    def pick(pool: list[FaceCombo]) -> FaceCombo:
        return min(
            pool,
            key=lambda combo: (
                colour_uses[combo[0]],
                prefer_classic and not _is_classic(combo),
                combo[1] in _NOVEL_EYES or combo[2] in _NOVEL_MOUTHS,
                -distance(combo),
            ),
        )

    distinct_plain = [combo for combo in plain if distance(combo) >= min_plain]
    if distinct_plain:
        return pick(distinct_plain)
    distinct = [combo for combo in options if distance(combo) >= 4]
    return pick(distinct) if distinct else max(options, key=distance)


def _is_classic(combo: FaceCombo) -> bool:
    return combo[1:3] == ("happy", "laugh") and combo[3] == 0 and not combo[4]


def assign(agent_keys: list[str]) -> dict[str, FaceCombo]:
    """Assign agents in creation order; previously chosen faces never move."""
    assigned: dict[str, FaceCombo] = {}
    for key in agent_keys:
        assigned[key] = choose(key, list(assigned.values()))
    return assigned
