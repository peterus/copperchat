from __future__ import annotations

import colorsys
import hashlib

# theme -> (lightness, saturation); "agent" renders on a dark background.
_THEMES: dict[str, tuple[float, float]] = {
    "agent": (0.62, 0.75),
    "dark": (0.62, 0.75),
    "light": (0.38, 0.80),
}


def net_color(net_name: str, theme: str) -> tuple[int, int, int]:
    """Deterministic net colour: sha1(name) -> hue; lightness depends on the theme."""
    digest = hashlib.sha1(net_name.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65536.0
    lightness, saturation = _THEMES.get(theme, _THEMES["agent"])
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    return round(r * 255), round(g * 255), round(b * 255)
