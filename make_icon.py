"""Draws icon.ico (and docs/icon.png): amber rounded square with a white waveform (five rounded bars).

Every size is rendered on its own (16x supersampled, then reduced with Lanczos) so small sizes
stay sharp: thicker bars below 32 px, no margin below 24 px.
"""
from PIL import Image, ImageDraw

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
TOP, BOTTOM = (251, 191, 36), (217, 119, 6)  # vertical gradient
SS = 16
BARS = (0.34, 0.62, 0.9, 0.62, 0.34)  # relative heights


def render(n: int) -> Image.Image:
    big = n * SS
    margin = 0 if n < 24 else round(big / 16)
    side = big - 2 * margin
    grad = Image.new("RGBA", (1, side))
    for y in range(side):
        t = y / max(1, side - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    grad = grad.resize((side, side))
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=round(side * 0.23), fill=255)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(grad, (margin, margin), mask)

    d = ImageDraw.Draw(img)
    width = side * (0.12 if n <= 20 else 0.1 if n <= 32 else 0.085)
    span = 0.56 if n <= 20 else 0.52
    for i, h in enumerate(BARS):
        cx = margin + side * (0.5 + (i - 2) * span / 4)
        half = side * 0.58 * h / 2
        cy = margin + side / 2
        d.rounded_rectangle((cx - width / 2, cy - half, cx + width / 2, cy + half), radius=width / 2, fill="white")
    return img.resize((n, n), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    frames = [render(n) for n in SIZES]
    frames[-1].save("icon.ico", format="ICO", sizes=[(n, n) for n in SIZES], append_images=frames[:-1])
    frames[-1].save("docs/icon.png")
