from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "miniprogram" / "assets" / "prompt-lens-avatar.png"
SIZE = 1024
SCALE = 3


def radial_background(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    center = size / 2
    radius = math.sqrt(2) * center
    for y in range(size):
        for x in range(size):
            distance = math.hypot(x - center, y - center) / radius
            glow = max(0.0, 1.0 - distance) ** 2
            pixels[x, y] = (
                int(12 + 17 * glow),
                int(13 + 18 * glow),
                int(12 + 16 * glow),
            )
    return image


def point(cx: float, cy: float, radius: float, angle: float) -> tuple[float, float]:
    radians = math.radians(angle)
    return cx + radius * math.cos(radians), cy + radius * math.sin(radians)


def draw_logo(canvas: Image.Image) -> None:
    size = canvas.width
    draw = ImageDraw.Draw(canvas, "RGBA")
    cx = cy = size / 2

    # Soft depth beneath the optical mark.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
    shadow_draw.ellipse((cx - 330, cy - 300, cx + 330, cy + 360), fill=(0, 0, 0, 150))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(65)))

    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.ellipse((cx - 318, cy - 318, cx + 318, cy + 318), outline=(217, 211, 199, 38), width=4)
    draw.ellipse((cx - 282, cy - 282, cx + 282, cy + 282), outline=(217, 211, 199, 18), width=2)

    # Six aperture blades. The restrained tonal variation reads as machined metal.
    blade_colors = [
        (244, 241, 232, 255),
        (221, 216, 204, 255),
        (195, 191, 182, 255),
        (238, 233, 222, 255),
        (209, 204, 194, 255),
        (249, 246, 238, 255),
    ]
    outer_radius = 252
    inner_radius = 118
    overlap = 15
    for index, color in enumerate(blade_colors):
        start = -90 + index * 60
        points = [
            point(cx, cy, outer_radius, start - overlap),
            point(cx, cy, outer_radius, start + 49),
            point(cx, cy, inner_radius, start + 78),
            point(cx, cy, inner_radius, start + 18),
        ]
        draw.polygon(points, fill=color)

    # Inner lens and focus point.
    draw.ellipse((cx - 126, cy - 126, cx + 126, cy + 126), fill=(21, 23, 21, 255))
    draw.ellipse((cx - 112, cy - 112, cx + 112, cy + 112), outline=(255, 255, 255, 24), width=3)

    accent = (189, 77, 53, 255)
    draw.arc((cx - 153, cy - 153, cx + 153, cy + 153), 298, 346, fill=accent, width=15)
    focus_x, focus_y = point(cx, cy, 150, 322)
    draw.ellipse((focus_x - 13, focus_y - 13, focus_x + 13, focus_y + 13), fill=accent)

    font_path = Path("C:/Windows/Fonts/seguisb.ttf")
    font = ImageFont.truetype(str(font_path), 84)
    text = "PL"
    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    draw.text(
        (cx - text_width / 2, cy - text_height / 2 - box[1] - 4),
        text,
        font=font,
        fill=(249, 246, 238, 255),
        stroke_width=0,
    )

    # Precision ticks provide a subtle lens-instrument character.
    for angle in range(0, 360, 30):
        length = 17 if angle % 90 else 27
        start = point(cx, cy, 292, angle)
        end = point(cx, cy, 292 + length, angle)
        draw.line((start, end), fill=(235, 229, 216, 72), width=4)


def main() -> None:
    render_size = SIZE * SCALE
    background = radial_background(render_size).convert("RGBA")
    draw_logo(background)
    output = background.resize((SIZE, SIZE), Image.Resampling.LANCZOS).convert("RGB")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    output.save(OUTPUT, "PNG", optimize=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
