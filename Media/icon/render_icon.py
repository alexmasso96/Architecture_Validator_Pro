"""Make every icon file from the two master SVGs in this folder.

    icon.svg       -> icon_1024.png  (Linux, and the app's own uses)
                      app.ico        (Windows: the app's executable)
                      app.icns       (macOS: the app bundle)
    icon_sync.svg  -> app_sync.ico   (Windows: the Sync Worker's executable)

Each size is drawn from the vector, rather than shrunk from one big picture,
so the 16 and 32 px icons in the taskbar and Task Manager stay sharp.

Only needed when the icon changes, so its renderer isn't an app dependency:

    .venv/bin/pip install resvg-py
    .venv/bin/python Media/icon/render_icon.py
"""
from __future__ import annotations

import io
import os
import sys

from PIL import Image

try:
    import resvg_py
except ImportError:
    sys.exit("This needs resvg-py: .venv/bin/pip install resvg-py")

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def render(svg: str, size: int) -> Image.Image:
    data = resvg_py.svg_to_bytes(svg_path=os.path.join(HERE, svg), width=size, height=size)
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def save_ico(svg: str, name: str) -> None:
    images = [render(svg, s) for s in ICO_SIZES]
    images[-1].save(os.path.join(HERE, name), format="ICO",
                    sizes=[(s, s) for s in ICO_SIZES], append_images=images[:-1])


def main() -> None:
    render("icon.svg", 1024).save(os.path.join(HERE, "icon_1024.png"))
    save_ico("icon.svg", "app.ico")
    save_ico("icon_sync.svg", "app_sync.ico")
    icns = [render("icon.svg", s) for s in ICNS_SIZES]
    icns[-1].save(os.path.join(HERE, "app.icns"), format="ICNS", append_images=icns[:-1])
    print("Wrote icon_1024.png, app.ico, app_sync.ico and app.icns")


if __name__ == "__main__":
    main()
