"""Render a Slack-size agent thread using the shipped face generator."""

from __future__ import annotations

import argparse
from pathlib import Path

from daimon.core.agent_faces import assign, classic_combo, render_image
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/assets/agent-face-slack-thread.png"
ROWS = (
    ("Daimon", None, "I pulled the project context into this thread."),
    ("Analyst", "analyst", "The estimate holds across the validation folds."),
    ("Research", "research", "Two assumptions need checking before we share the model."),
    ("Operations", "ops", "The scheduled run completed with the expected inputs."),
    ("Finance", "finance-bot", "I reconciled the forecast with the latest actuals."),
    ("Support", "support", "The customer examples are ready for review."),
    ("Data Engineering", "data-eng", "The source tables passed the freshness checks."),
    ("QA Security", "qa-sec-b-76b4ca", "I found no new permissions in this release."),
    ("Sales Copilot", "sales-copilot", "The proposal now uses the approved figures."),
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(filename, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _round_avatar(source: Image.Image) -> Image.Image:
    mask = Image.new("L", (144, 144), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 143, 143), fill=255)
    avatar = ImageOps.fit(source.convert("RGB"), (36, 36), method=Image.Resampling.LANCZOS)
    result = avatar.convert("RGBA")
    result.putalpha(mask.resize((36, 36), Image.Resampling.LANCZOS))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--built-in-image", type=Path)
    args = parser.parse_args()
    output: Path = args.output
    width, header, row_height = 920, 78, 58
    image = Image.new("RGB", (width, header + len(ROWS) * row_height), "#ffffff")
    draw = ImageDraw.Draw(image)
    regular, bold, heading, small = _font(13), _font(14, bold=True), _font(17, bold=True), _font(11)
    draw.text((32, 16), "# agent-qa", fill="#1d1c1d", font=heading)
    draw.text((32, 43), "Thread", fill="#616061", font=regular)
    draw.line((0, 77, width, 77), fill="#dddddd", width=1)
    faces = assign([key for _, key, _ in ROWS if key is not None])
    if args.built_in_image is None:
        built_in = _round_avatar(render_image(classic_combo(), 36))
    else:
        with Image.open(args.built_in_image) as reference:
            built_in = _round_avatar(reference)
    for index, (name, key, message) in enumerate(ROWS):
        top = header + index * row_height
        if index % 2:
            draw.rectangle((0, top, width, top + row_height - 1), fill="#fafafa")
        avatar = built_in if key is None else _round_avatar(render_image(faces[key], 36))
        image.paste(avatar, (32, top + 10), avatar)
        draw.text((80, top + 7), name, fill="#1d1c1d", font=bold)
        name_width = draw.textlength(name, font=bold)
        if key is None:
            badge_x = 87 + name_width
            draw.rounded_rectangle(
                (badge_x, top + 8, badge_x + 29, top + 22), radius=3, fill="#e8f5fa"
            )
            draw.text((badge_x + 4, top + 9), "APP", fill="#1264a3", font=small)
            name_width += 36
        draw.text((91 + name_width, top + 9), f"09:{42 + index:02d}", fill="#616061", font=small)
        draw.text((80, top + 29), message, fill="#1d1c1d", font=regular)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)
    print(output)


if __name__ == "__main__":
    main()
