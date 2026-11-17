"""The 心响 app icon.

    python macapp/make_icon.py                      # from macapp/icon-source.* if present, else the drawn heart
    python macapp/make_icon.py --from picture.png   # use any picture (cropped square, rounded, macOS margins)

Writes macapp/AppIcon.icns and AppIcon.png (1024 px; also used by the 心响 page as its logo).

Needs Pillow (already installed for the Windows tray) and macOS `iconutil`.
"""
import argparse
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
TOP, BOTTOM = (232, 104, 52), (180, 52, 12)      # warm orange → deep red-orange
LINE = (194, 65, 12)


def draw(size=1024):
    scale = 4  # draw large, shrink for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # background: macOS-style rounded square with a soft vertical gradient
    inset, radius = int(s * 0.098), int(s * 0.18)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s
        gd.line([(0, y), (s, y)], fill=tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, s - inset, s - inset], radius, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # the heart (classic parametric curve), centred a little below the middle
    cx, cy, k = s / 2, s * 0.53, s * 0.0185
    heart = [(cx + k * 16 * math.sin(t) ** 3,
              cy - k * (13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)))
             for t in [i * 2 * math.pi / 720 for i in range(720)]]
    d.polygon(heart, fill=(255, 255, 255, 255))
    # the heartbeat line across the heart
    w = s * 0.022
    pts = [(-0.33, 0.0), (-0.12, 0.0), (-0.06, -0.13), (0.02, 0.16), (0.08, -0.05), (0.12, 0.0), (0.33, 0.0)]
    line = [(cx + x * s * 0.62, cy - s * 0.02 + y * s * 0.62) for x, y in pts]
    d.line(line, fill=LINE + (255,), width=int(w), joint="curve")
    for x, y in (line[0], line[-1]):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=LINE + (255,))
    return img.resize((size, size), Image.LANCZOS)


def from_picture(path, size=1024, content=0.86, radius=0.2):
    """Square-crop a picture, cut its own outer corners with a rounded mask, and leave macOS margins."""
    src = Image.open(path).convert("RGBA")
    side = min(src.size)
    left, top = (src.width - side) // 2, (src.height - side) // 2
    src = src.crop((left, top, left + side, top + side))
    inner = int(size * content)
    scale = 4
    big = src.resize((inner * scale, inner * scale), Image.LANCZOS)
    mask = Image.new("L", big.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big.width - 1, big.height - 1], int(big.width * radius), fill=255)
    big.putalpha(mask)
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    offset = (size * scale - big.width) // 2
    canvas.paste(big, (offset, offset), big)
    return canvas.resize((size, size), Image.LANCZOS)


def main():
    p = argparse.ArgumentParser(description="心响 app icon")
    p.add_argument("--from", dest="source", type=Path)
    args = p.parse_args()
    source = args.source or next(iter(sorted(HERE.glob("icon-source.*"))), None)
    master = from_picture(source) if source else draw(1024)
    master.save(HERE / "AppIcon.png")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for px in (16, 32, 128, 256, 512):
            master.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
            master.resize((px * 2, px * 2), Image.LANCZOS).save(iconset / f"icon_{px}x{px}@2x.png")
        if shutil.which("iconutil"):
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "AppIcon.icns")], check=True)
    print(HERE / "AppIcon.icns")


if __name__ == "__main__":
    main()
