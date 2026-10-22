"""The breathing orb, drawn with braille dots (2×4 per terminal cell, which come out roughly square).

render_arrays(...) is a pure function of the state, a smoothed sound level and the time, so it's cheap to test.
The radius breathes, swells with sound and wobbles a little; the edge is dithered so it looks soft;
colour is a radial gradient from the theme with a highlight, a rotating band while thinking, and orbiting dots while work runs in the background.
"""

import functools
import math

import numpy as np

# braille bit for the dot at (x, y) inside a cell
_BITS = np.array([[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]], dtype=np.uint16)  # [y][x]
_BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0
QUANT = 20  # colour step per channel: ~12 shades across the gradient instead of hundreds of distinct colours


@functools.lru_cache(maxsize=8)
def _grid(H, W):
    ys, xs = np.mgrid[0:H, 0:W]
    ys, xs = ys.astype(np.float32), xs.astype(np.float32)
    dy, dx = ys - (H - 1) / 2, xs - (W - 1) / 2
    return ys, xs, np.hypot(dx, dy), np.arctan2(dy, dx)


@functools.lru_cache(maxsize=8)
def _cells(cols, rows, room):
    """Per-cell geometry: cell centres, distance from the orb's centre (in radii), highlight and angle."""
    cx, cy = (cols * 2 - 1) / 2, (rows * 4 - 1) / 2
    ccx = (np.arange(cols) * 2 + 0.5)[None, :]
    ccy = (np.arange(rows) * 4 + 1.5)[:, None]
    cd = np.hypot(ccx - cx, ccy - cy) / max(room, 1e-6)
    hl = np.clip(1 - np.hypot(ccx - (cx - room * 0.35), ccy - (cy - room * 0.4)) / (room * 0.75), 0, 1)[..., None]
    return cd, hl, np.arctan2(ccy - cy, ccx - cx)


@functools.lru_cache(maxsize=8)
def _BAYER_TILE(H, W):
    return np.tile(_BAYER, (H // 4 + 1, W // 4 + 1))[:H, :W]

# Per state: base radius (fraction of the space), breath amplitude and period (s), how much sound swells it.
STATES = {
    "waking":    dict(base=0.55, breath=0.03, period=3.0, swell=0.0),
    "listening": dict(base=0.70, breath=0.035, period=4.0, swell=0.0),
    "talking":   dict(base=0.70, breath=0.01, period=4.0, swell=0.28),
    "speaking":  dict(base=0.70, breath=0.01, period=4.0, swell=0.22),
    "thinking":  dict(base=0.68, breath=0.02, period=2.5, swell=0.0),
    "waiting":   dict(base=0.70, breath=0.06, period=1.6, swell=0.0),
    "working":   dict(base=0.66, breath=0.03, period=4.0, swell=0.0),
    "muted":     dict(base=0.55, breath=0.0, period=4.0, swell=0.0),
}
_MAX_SWELL = max(c["swell"] for c in STATES.values())  # the most any state swells, to leave room for it


def render_arrays(state, energy, t, cols, rows, colors, fade=1.0):
    """→ (bits, rgb): braille bits per cell (rows×cols, 0 = blank) and colours (rows×cols×3, uint8).

    Everything is vectorized, and colours are snapped to QUANT steps per channel so neighbouring cells share a
    colour: the UI can then draw long runs with one style, which keeps both Python and the terminal light.
    """
    cfg = STATES.get(state, STATES["listening"])
    W, H = cols * 2, rows * 4
    cx, cy = (W - 1) / 2, (H - 1) / 2
    room = min(W, H) / 2 * 0.92 / (1 + _MAX_SWELL + 0.08)  # leave space to swell
    if state == "working":
        room *= 0.86  # leave space for the orbiting dots
    energy = max(0.0, min(1.0, energy))

    ys, xs, d, theta = _grid(H, W)
    breath = cfg["breath"] * math.sin(2 * math.pi * t / cfg["period"])
    wob = 0.012 + 0.07 * energy * (cfg["swell"] > 0)  # a little organic wobble always, more with sound
    r = room * (cfg["base"] / 0.7) * (1 + breath + cfg["swell"] * energy
                                      + wob * np.sin(3 * theta + 1.7 * t) + wob * 0.6 * np.sin(5 * theta - 2.3 * t))
    edge = 1.6 + 1.2 * energy  # width of the dithered rim, in dots
    rim = np.clip((r - d) / edge, 0, 1)  # 1 inside, fading to 0 across the rim
    on = rim > _BAYER_TILE(H, W) * 0.999

    if state == "working":  # three small dots orbiting the orb
        R = room * 1.32
        for k in range(3):
            a = t * 1.3 + k * 2 * math.pi / 3
            ox, oy = cx + R * math.cos(a), cy + R * math.sin(a)
            on |= np.hypot(xs - ox, ys - oy) < 1.3

    # pack dots into braille cells
    bits = (on.reshape(rows, 4, cols, 2) * _BITS[None, :, None, :]).sum(axis=(1, 3))

    # colour per cell, all at once
    cd, hl, cth = _cells(cols, rows, room)
    u = np.minimum(cd, 1.0)[..., None]

    C = {k: np.array(v, dtype=np.float32) for k, v in colors.items()}
    # lighter centre, deeper edge, so it reads as a sphere rather than a flat disc
    inner = C["accent2"] + (C["foreground"] - C["accent2"]) * 0.35
    outer = C["accent"] + (C["background"] - C["accent"]) * 0.3
    bright = (0.78 + 0.22 * energy) * fade
    if state == "muted":
        inner = outer = C["dim"]
        bright = 0.8 * fade
    elif state == "waiting":
        outer = outer + (C["accent3"] - outer) * (0.35 + 0.35 * math.sin(2 * math.pi * t / cfg["period"]))
    elif state == "waking":
        bright *= 0.6

    rgb = inner + (outer - inner) * u ** 1.2
    if state != "muted":
        rgb = rgb + (C["foreground"] - rgb) * (0.65 * hl ** 1.5)
    if state == "thinking":
        band = (np.clip(np.cos(cth - t * 2.4), 0, 1) ** 6)[..., None]
        rgb = rgb + (C["accent3"] - rgb) * (0.8 * band)
    rgb = np.where((cd > 1.05)[..., None], C["accent3"], rgb)  # orbit dots
    rgb = C["background"] + (rgb - C["background"]) * bright
    rgb = (np.round(np.clip(rgb, 0, 255) / QUANT) * QUANT).clip(0, 255).astype(np.uint8)
    return bits, rgb

