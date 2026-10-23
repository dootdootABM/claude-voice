"""The live output window (live_view.py): the event renderers on their own, and the app following a task's live log
(testdata/live_sample.jsonl), with no real terminal, claude or microphone.

Run: pytest test_live_view.py
"""

import asyncio
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

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


def shown(widget):
    """What a Static shows, as plain text."""
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(widget.content)
    return console.export_text()


def pal():
    return show_output.Palette(show_output.Theme())


# ---- renderers ---------------------------------------------------------------------------------------------------
def test_claude_code_names():
    cwd = "/home/u/proj"
    assert lv.display({"name": "Edit", "summary": "/home/u/proj/a/b.py"}, cwd) == ("Update", "a/b.py")
    assert lv.display({"name": "Read", "summary": "/elsewhere/x"}, cwd) == ("Read", "/elsewhere/x")
    assert lv.display({"name": "Grep", "summary": 'pattern: "x"'}, cwd) == ("Search", 'pattern: "x"')
    assert lv.display({"name": "Grep", "summary": 'pattern: "x", path: "/home/u/proj/sidecar"'}, cwd) == \
        ("Search", 'pattern: "x", path: "sidecar"')
    assert lv.display({"name": "TodoWrite", "summary": "3 todos"}) == ("Update Todos", None)
    assert lv.display({"name": "WebSearch", "summary": "kokoro"}) == ("Web Search", '"kokoro"')
    assert lv.display({"name": "mcp__voiceapp__mark_done", "summary": ""}) == ("voiceapp - mark_done (MCP)", "")
    assert lv.display({"name": "Agent", "summary": "Review", "subagent_type": "reviewer"}) == ("reviewer", "Review")
    assert lv.tool_label({"name": "Bash", "summary": "ls"}) == "Bash(ls)"
    assert live.summarize("Grep", {"pattern": "duck", "path": "sidecar"}) == 'pattern: "duck", path: "sidecar"'


def test_result_summaries():
    assert lv.result_summary("Read", "1\ta\n2\tb", {"lines": 48}) == "Read 48 lines"
    assert lv.result_summary("Read", "1\ta", {}) == "Read 1 line"
    assert lv.result_summary("Glob", "a\nb", {"files": 2}) == "Found 2 files"
    assert lv.result_summary("Grep", "x", {"files": 1, "lines": 7}) == "Found 7 lines"
    assert lv.result_summary("Bash", "out", {}) is None
    assert lv.fmt_tokens(950) == "950" and lv.fmt_tokens(36777) == "36.8k"
    assert lv.fmt_duration(75) == "1m 15s" and lv.fmt_duration(3.21) == "3.2s"


def test_numbered_diff_from_a_patch_and_the_fallback():
    rows = lv.patch_rows([{"old": 40, "new": 40, "lines": [" a", "-b", "+B", "+C", " d"]},
                          {"old": 90, "new": 91, "lines": ["-z"]}])
    assert rows == [(40, " ", "a"), (41, "-", "b"), (41, "+", "B"), (42, "+", "C"), (43, " ", "d"),
                    (None, " ", "…"), (90, "-", "z")]
    assert lv.diff_summary("Edit", rows, "x.py") == "Updated x.py with 2 additions and 2 removals"
    assert lv.diff_summary("Write", [(1, "+", "a")], "n.txt", created=True) == "Wrote 1 line to n.txt"
    assert lv.diff_changes("Edit", {"old_string": "a\nb\nc", "new_string": "a\nB\nc"}) == [("-", "b"), ("+", "B")]
    assert lv.diff_changes("Read", {}) is None


def test_result_meta_from_claude_codes_tool_result():
    assert live.result_meta("Read", {"file": {"numLines": 48, "startLine": 1, "totalLines": 48, "content": "x"}}) == \
        {"lines": 48, "start": 1, "total": 48}
    meta = live.result_meta("Edit", {"structuredPatch": [{"oldStart": 3, "newStart": 3, "lines": ["-a", "+b"]}]})
    assert meta == {"patch": [{"old": 3, "new": 3, "lines": ["-a", "+b"]}]}
    new = live.result_meta("Write", {"type": "create", "structuredPatch": [], "content": "1\n2"})
    assert new["created"] and new["patch"][0]["lines"] == ["+1", "+2"]
    assert live.result_meta("Bash", {"stdout": "x"}) is None and live.result_meta("Read", "nope") is None


def test_permission_spinner_and_round_end():
    line = lv.render_permission({"decision": "refused", "by": "step_checker", "tool": "Bash", "summary": "rm x",
                                 "reason": "out of scope"}, pal()).plain
    assert "refused by the plan check" in line and "Bash(rm x)" in line and "out of scope" in line
    assert "blocked by the job rules" in lv.render_permission({"decision": "refused", "by": "rules", "tool": "Agent",
                                                                "summary": "s"}, pal()).plain
    spin = lv.spinner_line("Pondering", 12.3, 1240, "Bash(ls)", pal(), now=0).plain
    assert spin == "· Pondering… (12.3s · ↓ 1.2k tokens) · Bash(ls)"
    assert lv.verb_for(1700000000.0) == lv.verb_for(1700000000.0) in lv.VERBS
    end = lv.render_round_end({"duration_s": 62, "ok": True, "out_tokens": 2140}, pal()).plain
    assert end == "✻ Worked for 1m 02s · 2.1k tokens"
    assert "Failed after" in lv.render_round_end({"duration_s": 3, "ok": False}, pal()).plain
    todos = lv.render_todos({"todos": [{"content": "plan", "status": "completed"},
                                       {"content": "build", "status": "in_progress"}]}, pal()).plain
    assert "☒ plan" in todos and "☐ build" in todos


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
        assert f"{app.verb}…" in str(app.query_one("#spin").content)

        write(LINES[8:], mode="a")  # new lines: appended, nothing redrawn
        await pilot.pause(0.8)
        assert list(body.children)[:len(first)] == first
        assert count(app, lv.SubAgent) == 1
        sub = app.query_one(lv.SubAgent)
        assert sub.status == "completed" and not sub.expanded and sub.tool_uses == 3  # finished: Done (…)
        assert sub.agent == "reviewer" and "Review the clipping fix" in sub.description
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
    assert keys["enter"] == "open_worker" and keys["escape"] == "back" and keys["q"] == "quit"


JOB = (Path(__file__).parent / "testdata" / "live_job.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)


async def watch_job(claims):
    write(JOB)
    app = lv.LiveApp(KEY)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.8)
        subs = {s.agent: s for s in app.query(lv.SubAgent)}
        scout, builder, reviewer = subs["scout"], subs["builder"], subs["reviewer"]
        assert scout.status == "completed" and scout.tokens == 21400 and scout.report.startswith("Status: done")
        assert builder.running and [label for label, _ in builder.recent] == [
            "Update(sidecar/audio.py)", "Bash(git push --force)", "Bash(pytest -q sidecar/test_echo.py)"]
        verdicts = [v and (v["decision"], v["by"]) for _, v in builder.recent]
        assert verdicts == [("approved", "step_checker"), ("refused", "step_checker"), ("approved", "read_only")]
        preview = shown(builder.preview)
        assert "+9 more tool uses" in preview and "30.1k tokens" in preview
        assert reviewer.status == "blocked" and "a builder is editing" in shown(reviewer.preview)
        assert app.job and [w.agent for w in app.flow.workers()] == ["builder"]  # the blocked one isn't running
        assert "builder" in shown(app.query_one("#workers")) and app.query_one("#workers").display
        assert "1 worker running" in shown(app.query_one("#titles"))
        refused = [b for b in builder.query(lv.ToolBlock) if b.state == "error"]
        assert len(refused) == 1 and refused[0].ev["summary"] == "git push --force"
        plan = next(w for w in app.expandables if isinstance(w, lv.ToolResult) and "Echo gate" in w.text)
        assert plan.expandable

        app.select(scout)  # enter: the worker on its own, esc: back
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.focus_sub is scout and not app.query_one("#body").display and app.query_one("#focus").display
        focus = app.query_one("#focus")
        assert str(focus.children[0].content).startswith("> Read-only. Find where")
        assert len(focus.query(lv.ToolBlock)) == 2 and "esc back" in str(app.hint())
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.focus_flow is None and app.query_one("#body").display and app.is_running

        write([json.dumps({"v": 1, "ts": 1700001020.0, "kind": "subagent_end", "id": "s2", "name": "builder",
                           "status": "completed", "parent": "a2", "tokens": 40000, "tool_uses": 14, "ms": 90000}) + "\n",
               json.dumps({"v": 1, "ts": 1700001021.0, "kind": "job", "what": "piece_done", "piece": "Echo gate",
                           "summary": "added", "files": ["sidecar/audio.py"]}) + "\n",
               json.dumps({"v": 1, "ts": 1700001022.0, "kind": "job", "what": "hand_on", "tokens": 41000}) + "\n"],
              mode="a")
        await pilot.pause(0.6)
        assert "Done (14 tool uses · 40.0k tokens · 1m 30s)" in shown(builder.preview)
        assert not app.flow.workers() and not app.query_one("#workers").display and app.pieces == 1
        assert "1 piece done" in shown(app.query_one("#titles"))
        labels = [getattr(w.content, "label", "") for w in app.query(lv.Line)]
        assert "fresh orchestrator · the last one had 41.0k tokens" in labels
        await pilot.press("q")


def test_job_view(claims):
    asyncio.run(watch_job(claims))
