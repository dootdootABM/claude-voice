"""Headless test of the output window (show_output.py): renders a document that uses every kind of block at many
window sizes, resizes it while it runs, and checks the keys. No models.

Run: pytest test_viewer.py   — set CV_SHOTS=<dir> to also save an SVG per size.
"""

import asyncio
import os
from pathlib import Path

import pytest
from textual.containers import VerticalScroll

import show_output
from show_output import Heading, OutputApp, Wide

SAMPLE = str(Path(__file__).resolve().parent / "testdata" / "sample_output.md")
SIZES = [(34, 14), (60, 24), (80, 30), (100, 40), (140, 45), (220, 60)]


def check(label, ok, detail=""):
    assert ok, f"{label}: {detail}" if detail else label


@pytest.fixture(autouse=True)
def no_clipboard(monkeypatch):
    monkeypatch.setattr(show_output.platform_util, "copy_text", lambda text: True)  # never touch the real clipboard


async def at_size(w, h, shots):
    app = OutputApp(SAMPLE)
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause(0.6)  # let the blocks stream in
        body = app.query_one("#body", VerticalScroll)
        check(f"{w}x{h} all blocks mounted", len(body.children) >= 15, f"{len(body.children)} blocks")
        check(f"{w}x{h} sections found", [x.title for x in app.headings][:2] == ["The flow", "Where the time goes"])
        wide = app.query(Wide).first()
        if w < 120:  # the 118-column diagram can't fit: it scrolls sideways instead of breaking
            check(f"{w}x{h} wide diagram scrolls sideways", wide.max_scroll_x > 0, f"max_scroll_x={wide.max_scroll_x}")
        check(f"{w}x{h} nothing wider than the window", all(c.region.right <= w for c in body.children if c.region.width))
        if shots:
            app.save_screenshot(os.path.join(shots, f"viewer-{w}x{h}.svg"))
        for nw, nh in ((max(30, w // 2), h), (w * 2 if w < 110 else w // 3, h + 5), (w, h)):  # resize while running
            await pilot.resize_terminal(nw, nh)
            await pilot.pause(0.15)
            check(f"{w}x{h} → {nw}x{nh} still laid out", all(c.region.right <= nw for c in body.children
                                                              if c.region.width) and app.is_running)


async def keys():
    app = OutputApp(SAMPLE)
    async with app.run_test(size=(90, 26)) as pilot:
        await pilot.pause(0.6)
        body = app.query_one("#body", VerticalScroll)
        await pilot.press("right_square_bracket")
        await pilot.pause(0.4)
        y1 = body.scroll_y
        check("] jumps to the next section", y1 > 0, f"scroll_y={y1}")
        await pilot.press("G")
        await pilot.pause(0.4)
        check("G goes to the end", body.scroll_y == body.max_scroll_y)
        check("footer shows progress", "100%" in str(app.query_one("#foot").render()))
        await pilot.press("g")
        await pilot.pause(0.4)
        check("g goes to the top", body.scroll_y == 0)
        before = app.query(Wide).first().scroll_x
        await pilot.press("l")
        await pilot.pause(0.2)
        check("l scrolls wide blocks sideways", app.query(Wide).first().scroll_x > before)
        await pilot.press("t")
        check("t shows the contents", app.query_one("#toc").has_class("shown"))
        check("contents lists the sections", app.query_one("#toc").option_count == len(app.headings) >= 3)
        app.theme_.mtime = None
        app.theme_.colors["accent"] = (200, 80, 80)
        await app.build()  # a wallpaper change rebuilds everything in the new palette
        check("rebuild after a theme change", len(app.query(Heading)) == len(app.headings))
        await pilot.press("q")


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_sizes(size):
    asyncio.run(at_size(*size, os.environ.get("CV_SHOTS")))


def test_keys():
    asyncio.run(keys())
