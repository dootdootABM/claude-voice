"""The live output window (live_view.py): the event renderers on their own, and the app following a task's live log
(testdata/live_sample.jsonl), with no real terminal, claude or microphone.

Run: pytest test_live_view.py
"""

import asyncio
from pathlib import Path

import pytest

import live
import live_view as lv
import show_output

SAMPLE = Path(__file__).parent / "testdata" / "live_sample.jsonl"
LINES = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
KEY = "t9-test"


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def claims(monkeypatch):
    calls = []
    monkeypatch.setattr(live, "claim_window", lambda key: calls.append(("claim", key)))
    monkeypatch.setattr(live, "release_window", lambda key: calls.append(("release", key)))
    return calls


def write(lines, mode="w"):
    p = Path(live.path_for(KEY))
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode, encoding="utf-8") as f:
        f.writelines(lines)


def pal():
    return show_output.Palette(show_output.Theme())


# ---- renderers ---------------------------------------------------------------------------------------------------
def test_result_preview_and_expanded():
    text = "\n".join(f"line {i}" for i in range(10))
    short = lv.render_result(text, pal()).plain
    assert "line 2" in short and "line 3" not in short and "+7 lines (e to expand)" in short
    full = lv.render_result(text, pal(), expanded=True).plain
    assert "line 9" in full and "(e to collapse)" in full
    assert "(no output)" in lv.render_result("", pal()).plain


def test_diff_and_todos():
    changes = lv.diff_changes("Edit", {"old_string": "a\nb\nc", "new_string": "a\nB\nc"})
    assert changes == [("-", "b"), ("+", "B")]
    assert lv.diff_changes("Read", {}) is None
    head = lv.render_diff("Write", {"file_path": "x.py", "content": "1\n2\n"}, pal()).plain
    assert head.startswith("  ⎿  Wrote 2 lines to x.py")
    todos = lv.render_todos({"todos": [{"content": "plan", "status": "completed"},
                                       {"content": "build", "status": "in_progress"}]}, pal()).plain
    assert "☒ plan" in todos and "☐ build" in todos


def test_permission_and_round_end():
    line = lv.render_permission({"decision": "refused", "by": "step_checker", "tool": "Bash", "summary": "rm x",
                                 "reason": "out of scope"}, pal()).plain
    assert "refused by the step checker" in line and "Bash(rm x)" in line and "out of scope" in line
    assert lv.fmt_duration(75) == "1m 15s" and lv.fmt_duration(3.21) == "3.2s"


# ---- the app -----------------------------------------------------------------------------------------------------
def count(app, cls):
    return len(app.query(cls))


async def follow_sample(claims):
    write(LINES[:8])
    app = lv.LiveApp(KEY)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.6)
        body = app.query_one("#body")
        first = list(body.children)
        assert first and app.running  # a round is open: the spinner shows
        assert "Working…" in str(app.query_one("#spin").content)

        write(LINES[8:], mode="a")  # new lines: appended, nothing redrawn
        await pilot.pause(0.8)
        assert list(body.children)[:len(first)] == first
        assert count(app, lv.SubAgent) == 1
        sub = app.query_one(lv.SubAgent)
        assert sub.status and not sub.expanded  # finished: collapsed
        assert len(sub.steps.children) >= 1  # its Read step is nested inside it
        assert any("FAILED" in r.text and r.is_error for r in app.query(lv.ToolResult))
        assert app.closed and not app.running and str(app.query_one("#spin").content) == ""
        assert getattr(list(body.children)[-1].content, "label", "") == "task closed"

        await pilot.press("f")
        assert app.follow is False
        await pilot.press("G")
        assert app.follow is True
        await pilot.press("k")
        assert app.follow is False

        target = next(w for w in app.expandables if w.expandable)
        app.select(target)
        before = target.expanded
        await pilot.press("e")
        assert target.expanded is not before

        write(LINES[:3])  # the file was reset (shorter): everything is drawn again from the top
        await pilot.pause(0.6)
        assert count(app, lv.SubAgent) == 0 and not app.closed
        await pilot.press("q")
    assert claims[0] == ("claim", KEY)


def test_live_app(claims):
    asyncio.run(follow_sample(claims))


def test_space_pages_and_f_follows():
    keys = {k: b.action for b in lv.LiveApp.BINDINGS for k in b.key.split(",")}
    assert keys["f"] == "follow" and keys["space"] == "page(1)" and keys["e"] == "expand"
