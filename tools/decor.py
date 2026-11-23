# Vendored from https://github.com/mangow314/party-hud (scripts/art/decor.py), MIT License, Copyright (c) 2026 Mango.
# Unchanged except this header; tools/frame_sets.py imports its helpers.

"""Turn raw/portrait-<class>.png into the framed portraits in art/."""

import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
OUT = os.path.join(HERE, "..", "..", "art")
OUT_W, OUT_H = 192, 216
DARK = (14, 16, 28)
BIG_SCALE = 2
GOLD_HI, GOLD, GOLD_MID, GOLD_LO = (255, 236, 170), (240, 200, 105), (214, 156, 64), (170, 110, 44)
GOLD_LINE = (42, 26, 8)
FRAME_BANDS = (GOLD_LINE, GOLD_LO, GOLD_MID, GOLD, GOLD_HI, GOLD, GOLD_MID, GOLD_LINE)
BAND, GLOW_MARGIN, FRAME_CUT = 12, 60, 230
SMALL_BANDS = (GOLD_LINE, GOLD, GOLD_HI, GOLD_MID)
SMALL_MARGIN, SMALL_CUT, SMALL_GEM = 6, 120, 52
STAR = (255, 250, 225)
COLORS = {  # class: (background color, glow color)
    "swordsman": ((150, 40, 48), (255, 170, 120)),
    "merchant": ((150, 110, 30), (255, 226, 140)),
    "thief": ((80, 40, 130), (200, 150, 255)),
    "cleric": ((170, 140, 70), (255, 248, 210)),
    "scholar": ((40, 70, 150), (150, 200, 255)),
    "hunter": ((40, 110, 60), (190, 255, 170)),
    "apothecary": ((20, 110, 110), (150, 250, 230)),
    "dancer": ((160, 50, 110), (255, 170, 220)),
}


def crop89(im):
    w, h = im.size
    cw = h * 8 // 9
    x = (w - cw) // 2
    return im.crop((x, 0, x + cw, h))


def cutout(im):
    """Character mask (L, 255 = character); the top, left and right edges must be the plain dark background."""
    a = np.asarray(im.convert("RGB")).astype(int)
    h, w, _ = a.shape
    corners = np.concatenate([a[:8, :8].reshape(-1, 3), a[:8, -8:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    near = (np.abs(a - bg).max(axis=2) < 34).astype(np.uint8) * 255
    flood = Image.fromarray(
        near, "L"
    ).copy()  # copy: an image made from a numpy array is read-only and floodfill does nothing
    for x in range(0, w, 4):
        if flood.getpixel((x, 0)) == 255:
            ImageDraw.floodfill(flood, (x, 0), 128)
    for y in range(0, h, 4):
        for seed in ((0, y), (w - 1, y)):
            if flood.getpixel(seed) == 255:
                ImageDraw.floodfill(flood, seed, 128)
    mask = Image.fromarray(np.where(np.asarray(flood) == 128, 0, 255).astype(np.uint8), "L")
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    return mask.filter(ImageFilter.GaussianBlur(1.2))


def gacha_bg(size, base, glow, seed):
    """Dark base, a big glow above center, light rays, bokeh dots and four-point stars."""
    w, h = size
    rnd = random.Random(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w * 0.5, h * 0.38
    d = np.sqrt(((xx - cx) / w) ** 2 + ((yy - cy) / h) ** 2)
    t = np.clip(1 - d / 0.75, 0, 1) ** 1.6
    dark = np.array(DARK, float)
    col = dark + (np.array(base, float) - dark) * np.clip(t * 1.4, 0, 1)[..., None]
    col += (np.array(glow, float) - col) * (t**3 * 0.75)[..., None]
    img = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8), "RGB")
    rays = Image.new("L", size, 0)
    rd = ImageDraw.Draw(rays)
    n = 18
    for k in range(n):
        ang = 2 * math.pi * k / n + rnd.uniform(-0.05, 0.05)
        spread = 0.06 if k % 2 else 0.035
        r = max(w, h) * 1.2
        rd.polygon(
            [
                (cx, cy),
                (cx + r * math.cos(ang - spread), cy + r * math.sin(ang - spread)),
                (cx + r * math.cos(ang + spread), cy + r * math.sin(ang + spread)),
            ],
            fill=70 if k % 2 else 110,
        )
    rays = rays.filter(ImageFilter.GaussianBlur(w * 0.01))
    img = Image.composite(Image.new("RGB", size, glow), img, rays.point(lambda v: int(v * 0.55)))
    bokeh = Image.new("L", size, 0)
    bd = ImageDraw.Draw(bokeh)
    for _ in range(14):
        r = rnd.uniform(w * 0.015, w * 0.05)
        x, y = rnd.uniform(0, w), rnd.uniform(0, h * 0.85)
        bd.ellipse([x - r, y - r, x + r, y + r], fill=rnd.randint(40, 110))
    img = Image.composite(Image.new("RGB", size, glow), img, bokeh.filter(ImageFilter.GaussianBlur(w * 0.006)))
    stars = Image.new("L", size, 0)
    sd = ImageDraw.Draw(stars)
    for _ in range(9):
        x, y, r = rnd.uniform(w * 0.05, w * 0.95), rnd.uniform(h * 0.05, h * 0.7), rnd.uniform(w * 0.018, w * 0.04)
        q = r * 0.22
        sd.polygon(
            [
                (x, y - r),
                (x + q, y - q),
                (x + r, y),
                (x + q, y + q),
                (x, y + r),
                (x - q, y + q),
                (x - r, y),
                (x - q, y - q),
            ],
            fill=255,
        )
    return Image.composite(Image.new("RGB", size, (255, 255, 245)), img, stars.filter(ImageFilter.GaussianBlur(0.8)))


def octagon(t, w, h, cut):
    k = cut + 0.414 * t
    return [(k, t), (w - k, t), (w - t, k), (w - t, h - k), (w - k, h - t), (k, h - t), (t, h - k), (t, k)]


def gem(d, cx, cy, r, col):
    for rr, fill, line, lw in (
        (r, GOLD_MID, GOLD_LINE, 8),
        (r * 0.75, GOLD_HI, GOLD_LO, 4),
        (r * 0.56, col, GOLD_LINE, 4),
    ):
        d.polygon([(cx, cy - rr), (cx + rr, cy), (cx, cy + rr), (cx - rr, cy)], fill=fill, outline=line, width=lw)
    g = r * 0.56
    d.ellipse([cx - g * 0.5, cy - g * 0.72, cx - g * 0.05, cy - g * 0.27], fill=(255, 255, 255))


def rivet(d, cx, cy, r):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD, outline=GOLD_LINE, width=8)
    g = r * 0.62
    d.ellipse([cx - g, cy - g, cx + g, cy + g], fill=GOLD_LO, outline=GOLD_LINE, width=4)
    d.ellipse([cx - g * 0.6, cy - g * 0.65, cx - g * 0.1, cy - g * 0.15], fill=GOLD_HI)


def star(d, cx, cy, r):
    s = r * 0.26
    d.polygon(
        [
            (cx, cy - r),
            (cx + s, cy - s),
            (cx + r, cy),
            (cx + s, cy + s),
            (cx, cy + r),
            (cx - s, cy + s),
            (cx - r, cy),
            (cx - s, cy - s),
        ],
        fill=STAR,
    )


def gold_frame(im, glow, small=False):
    """RGBA gold frame on the 4x work canvas; small is the sidebar version."""
    w, h = im.size
    m = SMALL_MARGIN if small else GLOW_MARGIN
    cut = SMALL_CUT if small else FRAME_CUT
    bands = SMALL_BANDS if small else FRAME_BANDS

    def shape(t):
        return octagon(t + m, w, h, cut)

    halo = Image.new("L", im.size, 0)
    ImageDraw.Draw(halo).polygon(shape(-6 if small else -22), fill=255)
    out = Image.new("RGBA", im.size, (*glow, 0))
    out.putalpha(halo.filter(ImageFilter.GaussianBlur(6 if small else 30)).point(lambda v: int(v * 0.95)))
    frame_img = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(frame_img)
    for i, col in enumerate(bands):
        d.polygon(shape(i * BAND), fill=col)
    win = Image.new("L", im.size, 0)
    ImageDraw.Draw(win).polygon(shape(len(bands) * BAND), fill=255)
    frame_img.paste(im, (0, 0), win)
    d = ImageDraw.Draw(frame_img)
    gc, gr = (66, SMALL_GEM) if small else (150, 78)
    for cx, cy in ((gc + m, gc + m), (w - gc - m, gc + m), (gc + m, h - gc - m), (w - gc - m, h - gc - m)):
        gem(d, cx, cy, gr, glow)
    if small:
        out.alpha_composite(frame_img)
        return out
    for x in (w * 0.33, w * 0.67):
        for y in (50 + m, h - 50 - m):
            rivet(d, x, y, 28)
    for y in (h * 0.33, h * 0.5, h * 0.67):
        for x in (50 + m, w - 50 - m):
            rivet(d, x, y, 28)
    out.alpha_composite(frame_img)
    d = ImageDraw.Draw(out)
    for x, y, r in (
        (m + 40, m + 40, 96),
        (w - m - 90, m + 200, 80),
        (m + 20, h - m - 260, 72),
        (w - m - 40, h - m - 40, 96),
    ):
        star(d, x, y, r)
    return out


def portrait(cls, big=False):
    base, glow = COLORS[cls]
    k = BIG_SCALE if big else 1
    im = crop89(Image.open(os.path.join(RAW, f"portrait-{cls}.png")).convert("RGB"))
    work = im.resize((OUT_W * k * 4, OUT_H * k * 4), Image.Resampling.LANCZOS)
    mask = cutout(work)
    bg = gacha_bg(work.size, base, glow, seed=sum(map(ord, cls)))
    halo = mask.filter(ImageFilter.MaxFilter(8 * k + 1)).filter(ImageFilter.GaussianBlur(10 * k))
    bg = Image.composite(Image.new("RGB", work.size, glow), bg, halo.point(lambda v: int(v * 0.35)))
    comp = Image.composite(work, bg, mask)
    out = gold_frame(comp, glow, small=not big)
    return out.resize((OUT_W * k, OUT_H * k), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for cls in COLORS:
        small = portrait(cls).quantize(256, method=Image.Quantize.FASTOCTREE).convert("RGBA")
        small.save(os.path.join(OUT, f"portrait-{cls}.png"), optimize=True)
        print(f"portrait-{cls}.png")
        big = portrait(cls, big=True).quantize(256, method=Image.Quantize.FASTOCTREE).convert("RGBA")
        big.save(os.path.join(OUT, f"portrait-big-{cls}.png"), optimize=True)
        print(f"portrait-big-{cls}.png")
