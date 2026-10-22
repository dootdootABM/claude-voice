"""Colours that follow the wallpaper: the kitty palette matugen writes to ~/.config/kitty/matugen.conf."""

import os
import re

PATH = os.path.expanduser("~/.config/kitty/matugen.conf")
# Used if the file is missing or a colour isn't in it.
DEFAULTS = {"background": "#130d07", "foreground": "#eee0d5", "accent": "#fbb974", "accent2": "#feddbe",
            "accent3": "#dbe8b5", "error": "#ffb4ab", "dim": "#8a7f76"}
# Which kitty palette entries feed which role (first one present wins).
SOURCES = {"background": ["background"], "foreground": ["foreground"], "accent": ["cursor", "color4"],
           "accent2": ["color6", "color14"], "accent3": ["color5", "color13"], "error": ["color1", "color9"],
           "dim": ["color8"]}


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """Blend two RGB tuples, t in 0..1."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Theme:
    def __init__(self, path=PATH):
        self.path = path
        self.mtime = None
        self.colors = {k: hex_to_rgb(v) for k, v in DEFAULTS.items()}
        self.reload()

    def reload(self):
        """Re-read the palette if the file changed. Returns True when the colours changed."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        self.mtime = mtime
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = dict(re.findall(r"^\s*(\w+)\s+(#[0-9a-fA-F]{6})\s*$", f.read(), re.M))
        except (OSError, ValueError):  # vanished or unreadable since getmtime: keep the colours we have
            return False
        for role, keys in SOURCES.items():
            for key in keys:
                if key in raw:
                    self.colors[role] = hex_to_rgb(raw[key])
                    break
        if "color8" not in raw:  # no grey in the palette: derive one between background and foreground
            self.colors["dim"] = mix(self.colors["background"], self.colors["foreground"], 0.45)
        return True

    def hex(self, role):
        return "#%02x%02x%02x" % self.colors[role]
