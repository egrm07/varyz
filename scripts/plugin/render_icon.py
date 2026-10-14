"""Rasterize the existing Varyz SVG mark for plugin hosts that need PNG assets."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    size = 512
    scale = size * 4 / 64
    image = Image.new("RGBA", (size * 4, size * 4))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1),
                           radius=14 * scale, fill="#11120f")
    # Exact quadratic path from plugins/assets/icon.svg, with its original transform.
    curves = [((0, 4), (14, -8), (31, 17)),
              ((31, 17), (48, -8), (62, 4)),
              ((62, 4), (46, 2), (31, 34)),
              ((31, 34), (16, 2), (0, 4))]
    points = []
    for start, control, end in curves:
        for step in range(257):
            t = step / 256
            x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t ** 2 * end[0]
            y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t ** 2 * end[1]
            points.append(((6 + .84 * x) * scale, (18 + .84 * y) * scale))
    draw.polygon(points, fill="#ffc23d")
    image.resize((size, size), Image.Resampling.LANCZOS).save(ROOT / "plugins/assets/icon.png")


if __name__ == "__main__":
    main()
