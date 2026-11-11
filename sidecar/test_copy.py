"""Copying from the output window: the ⧉ copy button on code blocks, right-click, and copy-on-select."""
import asyncio

import pytest
from textual import events

import show_output
from show_output import CodeBlock, CopyButton, OutputApp, Wide

SAMPLE = """# t1 · Copy test

Run this to install it:

```bash
sudo pacman -S texlive-latex
cd ~/src/claude-voice && uv sync --extra gpu
```

Some text after the block that you can select with the mouse.

```diagram
╭──╮
╰──╯
```
"""
CODE = "sudo pacman -S texlive-latex\ncd ~/src/claude-voice && uv sync --extra gpu"


@pytest.fixture
def copied(monkeypatch):
    got = []
    monkeypatch.setattr(show_output.platform_util, "copy_text", lambda text: got.append(text) or True)
    return got


@pytest.fixture
def sample(tmp_path):
    p = tmp_path / "out.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return str(p)


def run(sample, size, steps):
    async def go():
        app = OutputApp(sample)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.5)
            await steps(app, pilot)
    asyncio.run(go())


def test_code_blocks_get_a_copy_button_but_diagrams_dont(sample, copied):
    async def steps(app, pilot):
        blocks = list(app.query(CodeBlock))
        assert len(blocks) == 1 and blocks[0].code == CODE
        assert len(app.query(Wide)) == 2  # the code's and the diagram's: both still scroll sideways
        block, button = blocks[0], app.query_one(CopyButton)
        assert button.region.y == block.region.y  # on the frame's top border
        wide = block.query_one(Wide)
        assert block.region.x < button.region.x and button.region.right <= block.region.x + wide.natural
    run(sample, (100, 30), steps)


@pytest.mark.parametrize("width", [100, 40])  # 40: the block is wider than the window, the button stays visible
def test_copy_button_copies_the_whole_block(sample, copied, width):
    async def steps(app, pilot):
        button = app.query_one(CopyButton)
        assert button.region.right <= width
        await pilot.click(CopyButton)
        await pilot.pause(0.1)
        assert copied == [CODE]
        assert "copied" in str(button.render()) and app.toast_text == "copied the block"
    run(sample, (width, 30), steps)


def test_right_click_on_a_code_block_copies_it(sample, copied):
    async def steps(app, pilot):
        await pilot.click(CodeBlock, offset=(4, 2), button=3)
        await pilot.pause(0.1)
        assert copied == [CODE]
    run(sample, (100, 30), steps)


def test_selecting_text_copies_it(sample, copied, monkeypatch):
    async def steps(app, pilot):
        monkeypatch.setattr(type(app.screen), "get_selected_text", lambda self: "Some text after the block")
        app.screen.post_message(events.TextSelected())
        await pilot.pause(0.1)
        assert copied == ["Some text after the block"]
        assert app.toast_text == "copied 25 characters"
    run(sample, (100, 30), steps)


def test_an_empty_selection_copies_nothing(sample, copied, monkeypatch):
    async def steps(app, pilot):
        monkeypatch.setattr(type(app.screen), "get_selected_text", lambda self: "  ")
        app.screen.post_message(events.TextSelected())
        await pilot.pause(0.1)
        assert copied == []
    run(sample, (100, 30), steps)


def test_without_a_clipboard_tool_it_falls_back_to_the_terminal(sample, monkeypatch):
    monkeypatch.setattr(show_output.platform_util, "copy_text", lambda text: False)

    async def steps(app, pilot):
        sent = []
        monkeypatch.setattr(app, "copy_to_clipboard", sent.append)
        await pilot.click(CopyButton)
        await pilot.pause(0.1)
        assert sent == [CODE]  # OSC 52: kitty puts it on the clipboard itself
    run(sample, (100, 30), steps)


def test_dragging_over_text_copies_it(sample, copied):
    async def steps(app, pilot):
        para = next(b for b in app.query(show_output.Block) if "Some text" in str(b.render()))
        x, y = para.region.x, para.region.y
        await pilot.mouse_down(offset=(x, y))
        await pilot.hover(offset=(x + 9, y))
        await pilot.mouse_up(offset=(x + 9, y))
        await pilot.pause(0.2)
        assert copied and copied[-1].startswith("Some text")
    run(sample, (100, 30), steps)
