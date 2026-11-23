"""Give a single-image avatar set (party-*) the state frames the panel animates, without redrawing the character:
  think/ (working): the picture plus twinkling four-point stars and a slight brightening, 2 frames
  hi/ (needs you):  a glow around the frame in the set's class colour, strong / weak, 2 frames
Output frames are 216x216 RGBA, the picture centred: the panel pads the 192x216 idle frame to that square too,
so every frame lines up.

    uv run --with pillow --with numpy tools/animate_static.py party-cleric [...]
"""

import os
import sys

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decor import COLORS, star  # noqa: E402

AVATARS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "avatars")
K = 4  # draw at 4x, then shrink: smooth stars and glow
STARS = (  # (x, y, radius) as fractions of the picture, two alternating sets so the twinkle moves
    ((0.22, 0.20, 0.11), (0.82, 0.36, 0.08), (0.72, 0.80, 0.09), (0.30, 0.70, 0.05)),
    ((0.78, 0.17, 0.10), (0.18, 0.56, 0.08), (0.44, 0.86, 0.07), (0.62, 0.50, 0.05)),
)


def load(set_id):
    return Image.open(os.path.join(AVATARS, set_id, "idle", "portrait.png")).convert("RGBA")


def square(im, size=216):
    out = Image.new("RGBA", (size * K, size * K), (0, 0, 0, 0))
    big = im.resize((im.width * K, im.height * K), Image.Resampling.LANCZOS)
    out.alpha_composite(big, ((out.width - big.width) // 2, (out.height - big.height) // 2))
    return out


def shrink(im):
    return im.resize((im.width // K, im.height // K), Image.Resampling.LANCZOS)


def think(im, k):
    pic = square(im)
    rgb = ImageEnhance.Brightness(pic.convert("RGB")).enhance(1.0 + 0.05 * k)
    pic = Image.merge("RGBA", (*rgb.split(), pic.getchannel("A")))
    d = ImageDraw.Draw(pic)
    ox, oy = (pic.width - im.width * K) // 2, (pic.height - im.height * K) // 2
    for fx, fy, fr in STARS[k]:
        star(d, ox + fx * im.width * K, oy + fy * im.height * K, fr * im.width * K)
    return shrink(pic)


def hi(im, glow, k):
    pic = square(im)
    halo = pic.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    halo = halo.filter(ImageFilter.MaxFilter(6 * K + 1)).filter(ImageFilter.GaussianBlur(5 * K))
    layer = Image.new("RGBA", pic.size, (*glow, 0))
    layer.putalpha(halo.point(lambda v: min(255, int(v * (1.6 if k == 0 else 0.25)))))
    layer.alpha_composite(pic)
    return shrink(layer)


def save(img, set_id, state, n):
    d = os.path.join(AVATARS, set_id, state)
    os.makedirs(d, exist_ok=True)
    img.quantize(256, method=Image.Quantize.FASTOCTREE).convert("RGBA").save(os.path.join(d, f"{state}_{n}.png"), optimize=True)


def main(sets):
    for s in sets:
        im, glow = load(s), COLORS[s.removeprefix("party-")][1]
        for k in (0, 1):
            save(think(im, k), s, "think", k + 1)
            save(hi(im, glow, k), s, "hi", k + 1)
        print(f"{s}: think 2, hi 2")


if __name__ == "__main__":
    main(sys.argv[1:] or sorted(d for d in os.listdir(AVATARS) if d.startswith("party-")))
