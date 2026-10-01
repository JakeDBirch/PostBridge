"""Regenerate the app icons from assets/meateater_mark.png.

    python assets/icon/make_icons.py      (needs Pillow; build-time only)

Writes PostBridge.icns (macOS bundle), PostBridge.ico (Windows exe) and
PostBridge.png (window / Dock icon at runtime).  The outputs are
committed, so builds don't need Pillow.
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = os.path.join(HERE, "..", "meateater_mark.png")
BG   = (28, 28, 30, 255)          # near-black, matches the app's dark UI
N    = 1024


def render():
    logo = Image.open(MARK).convert("RGBA")
    logo = logo.crop(logo.getbbox())
    icon = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    # macOS icon grid: an 824 px rounded square inset 100 px on a 1024 canvas.
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle((100, 100, 924, 924),
                                           radius=185, fill=255)
    icon.paste(Image.new("RGBA", (N, N), BG), (0, 0), mask)
    w = int(824 * 0.80)
    h = int(logo.height * w / logo.width)
    logo = logo.resize((w, h), Image.LANCZOS)
    icon.alpha_composite(logo, ((N - w) // 2, (N - h) // 2 + 10))
    return icon


if __name__ == "__main__":
    icon = render()
    icon.save(os.path.join(HERE, "PostBridge.icns"))
    # Windows: the rounded-square inset is macOS convention; crop it away
    # so the mark fills the taskbar slot like other Windows apps.
    win = icon.crop((100, 100, 924, 924))
    win.save(os.path.join(HERE, "PostBridge.ico"),
             sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    icon.resize((256, 256), Image.LANCZOS).save(
        os.path.join(HERE, "PostBridge.png"), optimize=True)
    print("icons written to", HERE)
