"""Give the bundled avatar sets party-hud's look: the character cut out of its plain background, a glowing
"gacha" background and the small gold octagon frame, 192x216 RGBA (transparent outside the frame).

Run once after adding or changing a set (needs Pillow + NumPy, not needed at runtime):
    uv run --with pillow --with numpy tools/frame_sets.py [set ...]
Frames every PNG under avatars/<set>/<state>/ in place; party-* sets are already framed and skipped.
"""

import os
import sys

from PIL import Image, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decor import COLORS, OUT_H, OUT_W, cutout, gacha_bg, gold_frame  # noqa: E402

AVATARS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "avatars")
CLASS = {  # set -> party-hud class colour (background, glow)
    "rpg-swordsman": "swordsman", "red": "swordsman",
    "rpg-rogue": "thief", "cipher": "thief",
    "rpg-sister": "cleric", "rpg-paladin": "cleric", "default": "cleric",
    "rpg-scholar": "scholar", "researcher": "scholar", "quill": "scholar",
    "rpg-ranger": "hunter", "moss": "hunter",
    "rpg-arcanist": "apothecary", "tester": "apothecary", "nova": "apothecary",
    "designer": "dancer", "oana": "dancer", "aza_choi": "dancer",
    "developer": "merchant", "operator": "merchant", "forge": "merchant", "sprocket": "merchant",
}


def frame(path, set_id):
    base, glow = COLORS[CLASS.get(set_id, "scholar")]
    src = Image.open(path).convert("RGBA")
    # 不裁成 8:9 (横图会只剩一张大脸): 整张放进 8:9 画布的底部, 上面补背景色; 透明像素也填背景色, cutout 才看得到干净的边
    w, h = src.size
    H = max(h, w * 9 // 8)
    im = Image.new("RGB", (w, H), src.getpixel((0, 0))[:3])
    im.paste(src, (0, H - h), src)
    small = im.height < 128  # pixel art: keep the pixels sharp
    work = im.resize((OUT_W * 4, OUT_H * 4), Image.Resampling.NEAREST if small else Image.Resampling.LANCZOS)
    mask = cutout(work)
    bg = gacha_bg(work.size, base, glow, seed=sum(map(ord, set_id)))  # same seed for every frame of a set: the background does not flicker
    halo = mask.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(10))
    bg = Image.composite(Image.new("RGB", work.size, glow), bg, halo.point(lambda v: int(v * 0.35)))
    out = gold_frame(Image.composite(work, bg, mask), glow, small=True).resize((OUT_W, OUT_H), Image.Resampling.LANCZOS)
    out.quantize(256, method=Image.Quantize.FASTOCTREE).convert("RGBA").save(path, optimize=True)


def main(sets):
    for s in sets or sorted(os.listdir(AVATARS)):
        d = os.path.join(AVATARS, s)
        if not os.path.isdir(d) or s.startswith("party-"):
            continue
        n = 0
        for root, _, files in os.walk(d):
            for f in sorted(files):
                if f.endswith(".png"):
                    frame(os.path.join(root, f), s)
                    n += 1
        print(f"{s}: {n} frames")


if __name__ == "__main__":
    main(sys.argv[1:])
