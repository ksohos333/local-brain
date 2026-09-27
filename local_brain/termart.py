"""Terminal image rendering using Rich half-block unicode.

Converts an image to a Rich markup string where each output row represents
two pixel rows of the source image: the foreground color is the top pixel,
the background color is the bottom pixel.

This is a brand-neutral extraction of the half-block rendering technique
discussed in Textualize/rich#384.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from PIL import Image

try:
    from rich.console import Console
    from rich.text import Text

    HAVE_RICH = True
except Exception:
    HAVE_RICH = False


def use_color() -> bool:
    """Return True if terminal color output seems appropriate."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and os.environ.get("TERM", "dumb") not in ("dumb", "")


def image_to_rich_markup(path: Path, width: int = 42) -> str:
    """Convert an image to a Rich markup string using half-block unicode.

    Each output row represents two pixel rows of the image: the foreground
    color is the top pixel, the background color is the bottom pixel.
    """
    img = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    img = Image.alpha_composite(bg, img).convert("RGB")

    orig_w, orig_h = img.size
    scale = orig_w / width
    height = int(orig_h / scale)
    if height % 2 != 0:
        height += 1
    img = img.resize((width, height), Image.Resampling.LANCZOS)

    lines = []
    for y in range(0, height, 2):
        row = []
        for x in range(width):
            r1, g1, b1 = img.getpixel((x, y))
            r2, g2, b2 = img.getpixel((x, y + 1))
            row.append(f"[rgb({r1},{g1},{b1}) on rgb({r2},{g2},{b2})]▀[/]")
        lines.append("".join(row))
    return "\n".join(lines)


def render_richtext(path: Path, width: int = 42) -> Optional[Text]:
    """Return a Rich Text renderable of the image, or None on failure."""
    if not HAVE_RICH:
        return None
    if not path.exists():
        return None
    try:
        markup = image_to_rich_markup(path, width=width)
        return Text.from_markup(markup)
    except Exception:
        return None


def print_image(path: Path, width: Optional[int] = None):
    """Print the image to stdout using Rich if available, else print a fallback."""
    if not HAVE_RICH:
        print(f"terminal image: {path}", flush=True)
        return
    if not path.exists():
        print(f"terminal image not found: {path}", flush=True)
        return

    effective_width = width or (56 if use_color() else 28)
    icon = render_richtext(path, width=effective_width)
    if icon is None:
        print(f"terminal image: {path}", flush=True)
        return

    console = Console(force_terminal=True)
    console.print(icon, highlight=False, crop=False, overflow="ignore")


__all__ = [
    "use_color",
    "image_to_rich_markup",
    "render_richtext",
    "print_image",
]
