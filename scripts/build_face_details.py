"""Draw small, restrained eye and mouth layers using the mascot's ink colours."""

from pathlib import Path

from PIL import Image, ImageDraw

OUTPUT = Path(__file__).resolve().parents[1] / "packages/core/daimon/core/face_layers"
DARK = "#6d2d23"
CORAL = "#e37b6e"
EYE_INK = "#183b4b"


def draw_mouth(name: str, left_y: int, middle_y: int, right_y: int, width: int) -> None:
    canvas = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(canvas)
    points = []
    for step in range(101):
        position = step / 100
        x = round(320 + 384 * position)
        y = round(
            (1 - position) ** 2 * left_y
            + 2 * (1 - position) * position * middle_y
            + position**2 * right_y
        )
        points.append((x, y))
    for colour, stroke in ((DARK, width), (CORAL, round(width * 0.55))):
        draw.line(points, fill=colour, width=stroke, joint="curve")
        for x, y in (points[0], points[-1]):
            radius = stroke / 2
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colour)
    canvas.save(OUTPUT / f"mouth-{name}.png", format="PNG")


def main() -> None:
    draw_mouth("smile", 410, 590, 410, 38)
    mouth = Image.new("RGBA", (1024, 1024))
    mouth_draw = ImageDraw.Draw(mouth)
    mouth_draw.ellipse((345, 420, 679, 505), fill=DARK)
    mouth_draw.ellipse((375, 455, 649, 493), fill=CORAL)
    mouth.save(OUTPUT / "mouth-soft-open.png", format="PNG")

    eyes = Image.new("RGBA", (1024, 1024))
    eye_draw = ImageDraw.Draw(eyes)
    eye_draw.ellipse((338, 315, 406, 399), fill=EYE_INK)
    eye_draw.ellipse((618, 315, 686, 399), fill=EYE_INK)
    eyes.save(OUTPUT / "eyes-plain.png", format="PNG")


if __name__ == "__main__":
    main()
