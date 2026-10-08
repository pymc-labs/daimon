"""Render the documented 50-face sheet from the bundled core generator."""

from pathlib import Path

from daimon.core.agent_faces import assign, render_image
from PIL import Image, ImageDraw, ImageFont

NAMES = [f"agent_{index:04d}" for index in range(50)]
OUTPUT = Path(__file__).resolve().parents[1] / "docs/assets/agent-faces-50.png"


def circle(image: Image.Image) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, image.width - 1, image.height - 1), fill=255)
    result = image.convert("RGBA")
    result.putalpha(mask)
    return result


def main() -> None:
    faces = assign(NAMES)
    sheet = Image.new("RGB", (1500, 1000), "#f7f5ef")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=15)
    for index, name in enumerate(NAMES):
        x, y = index % 10 * 150, index // 10 * 200
        combo = faces[name]
        sheet.paste(render_image(combo, 128), (x + 10, y + 5))
        draw.text((x + 10, y + 136), name, fill="#18232c", font=font)
        for size, left in ((36, 10), (20, 56)):
            preview = circle(render_image(combo, size))
            sheet.paste(preview, (x + left, y + 156), preview)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUTPUT, format="PNG", optimize=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
