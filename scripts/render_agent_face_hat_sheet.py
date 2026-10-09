"""Render the five shipped hats at Slack header and source sizes."""

from pathlib import Path

from daimon.core.agent_faces import _catalogue, classic_combo, render_image
from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path(__file__).resolve().parents[1] / "docs/assets/agent-face-hats.png"
HATS = (
    ("cap", "hat-cap-base"),
    ("beanie", "hat-beanie-base"),
    ("hardhat", "hat-hardhat-base"),
    ("graduation cap", "hat-gradcap-base"),
    ("headset", "hat-headset-base"),
)


def rounded(image: Image.Image) -> Image.Image:
    scale = 4
    mask = Image.new("L", (image.width * scale, image.height * scale))
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, mask.width - 1, mask.height - 1),
        radius=round(image.width * scale * 0.2),
        fill=255,
    )
    result = image.convert("RGBA")
    result.putalpha(mask.resize(image.size, Image.Resampling.LANCZOS))
    return result


def main() -> None:
    sheet = Image.new("RGB", (5 * 540, 2 * 540 + 100), "#f4f4f1")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    small_font = ImageFont.load_default(size=16)
    for column, (label, layer_id) in enumerate(HATS):
        combo = list(classic_combo())
        combo[3] = _catalogue().hat_index[layer_id]
        picture = render_image(tuple(combo), 512)  # type: ignore[arg-type]
        small = render_image(tuple(combo), 36)  # type: ignore[arg-type]
        x = column * 540 + 14
        draw.text((x, 4), label, fill="#242b31", font=font)
        sheet.paste(picture, (x, 34))
        rounded_picture = rounded(picture)
        sheet.paste(rounded_picture, (x, 574), rounded_picture)
        draw.text((x, 1093), "512 px square / rounded", fill="#242b31", font=small_font)
        sheet.paste(small, (x, 1124))
        rounded_small = rounded(small)
        sheet.paste(rounded_small, (x + 54, 1124), rounded_small)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUTPUT, format="PNG", optimize=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
