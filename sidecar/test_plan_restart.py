"""A plan waiting for the user's answer: shown in full in the output window, and kept across a cv restart (tasks.json
and the live log), where answering it resumes the saved session. No claude, no models, no windows.

Run: pytest test_plan_restart.py
"""

import asyncio
import collections
import io
import json
import types

import pytest
from rich.console import Console

import agents
import live
import live_view as lv
import orchestrator

PLAN = "# Plan\n\n1. Add the parser\n2. Test it"


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(agents, "TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(agents, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(orchestrator, "STATE_DIR", str(tmp_path), raising=False)
    return tmp_path


class FakeBackend:
    def __init__(self, app, task, resume=False, plan=False):
        self.app, self.task, self.resume, self.plan = app, task, resume, plan
        self.sent, self.approved = [], []
        app.backends.append(self)

    async def start(self, message=None):
        self.sent.append(message)

    def plan_approved(self, plan):
        self.approved.append(plan)


def make_app():
    app = agents.App.__new__(agents.App)
    app.tasks, app.decisions, app.mode = {}, collections.deque(), "ask"
    app.front_notes, app.backends, app.shown, app.cards = [], [], [], []
    app.output_was_open = False
    app.ui = types.SimpleNamespace(status=lambda m: None, error=lambda m: None, dim=lambda m: None,
                                   card=app.cards.append)
    app.front = types.SimpleNamespace(notify=lambda m: app.front_notes.append(m), user_turns=0)
    app.show_output = lambda task=None: app.shown.append(task) or None
    app.backend_for = lambda task, **kw: FakeBackend(app, task, **kw)
    return app


def saved_task(kind="quick"):
    t = agents.Task("t3", {"title": "parser", "prompt": "p", "project_dir": "/", "route": kind})
    t.session_id, t.state = "sess-1", "planning"
    return t


def events(t):
    return [e for e in live.read_from(t.live_key, 0)[0] if e["kind"] == "plan"]


async def ask_plan(t, app, answer):
    """The live path: the session calls ExitPlanMode and the user answers it."""
    b = object.__new__(agents.Backend)
    b.app, b.task = app, t
    t.backend = b
    run = asyncio.create_task(b._permission("ExitPlanMode", {"plan": PLAN}, {}))
    await asyncio.sleep(0)
    # while it waits: in tasks.json and in the live log, in full, and the window was asked for
    saved = json.loads(open(agents.TASKS_FILE, encoding="utf-8").read())
    assert saved[0]["pending_plan"] == {"id": "t3-plan", "text": PLAN}
    assert events(t) == [{**events(t)[0], "id": "t3-plan", "status": "pending", "text": PLAN}]
    assert app.shown == [t]
    if answer is None:
        run.cancel()  # cv exits while it waits
        return None
    app.decisions[0].resolve(*answer)
    return await run


def test_plan_is_saved_while_pending_and_cleared_when_answered():
    async def go():
        app, t = make_app(), saved_task()
        app.tasks[t.id] = t
        resp = await ask_plan(t, app, (True, ""))
        assert resp["behavior"] == "allow" and t.pending_plan is None
        assert [e["status"] for e in events(t)] == ["pending", "approved"]
        assert json.load(open(agents.TASKS_FILE, encoding="utf-8"))[0]["pending_plan"] is None
    asyncio.run(go())


def restart(app):
    """What a cv restart keeps: tasks.json (and the live log, which is a file)."""
    new = make_app()
    new.load_saved_tasks = agents.App.load_saved_tasks.__get__(new)
    new.load_saved_tasks()
    return new


@pytest.mark.parametrize("kind", ["quick", "job"])
def test_restored_plan_approved_resumes_the_session_out_of_plan_mode(kind):
    async def go():
        app, t = make_app(), saved_task(kind)
        app.tasks[t.id] = t
        await ask_plan(t, app, None)
        app = restart(app)
        t = app.tasks["t3"]
        assert t.state == "saved" and t.pending_plan == {"id": "t3-plan", "text": PLAN} and t.plans == 1
        assert app.restore_pending_plans() == [t]
        await asyncio.sleep(0)
        d = app.decisions[0]
        assert (d.id, d.kind, d.body) == ("t3-plan", "plan", PLAN) and app.cards == [d]
        assert "cv restarted" in app.front_notes[0] and PLAN in app.front_notes[0] and app.shown == [t]
        d.resolve(True, "use SQLite", "rest_of_task")
        await asyncio.sleep(0.01)
        b = app.backends[-1]
        assert b.resume and not b.plan and t.state == "building" and t.auto_edits
        assert "approved your plan, with this note: use SQLite" in b.sent[0]
        assert b.approved == ([PLAN] if kind == "job" else [])
        assert t.pending_plan is None and [e["status"] for e in events(t)] == ["pending", "approved"]
        assert json.load(open(agents.TASKS_FILE, encoding="utf-8"))[0]["pending_plan"] is None
    asyncio.run(go())


def test_restored_plan_declined_resumes_in_plan_mode_with_the_changes():
    async def go():
        app, t = make_app(), saved_task()
        app.tasks[t.id] = t
        await ask_plan(t, app, None)
        app = restart(app)
        app.restore_pending_plans()
        await asyncio.sleep(0)
        app.decisions[0].resolve(False, "skip the tests")
        await asyncio.sleep(0.01)
        t, b = app.tasks["t3"], app.backends[-1]
        assert b.resume and b.plan and t.state == "planning" and t.review_next_plan
        assert "changes to your plan: skip the tests" in b.sent[0] and "ExitPlanMode" in b.sent[0]
        assert events(t)[-1]["status"] == "declined" and events(t)[-1]["note"] == "skip the tests"
    asyncio.run(go())


def test_closing_a_task_drops_its_restored_plan():
    async def go():
        app, t = make_app(), saved_task()
        app.tasks[t.id] = t
        await ask_plan(t, app, None)
        app = restart(app)
        app.restore_pending_plans()
        await asyncio.sleep(0)
        t = app.tasks["t3"]
        t.state = "closed"
        app.drop_decisions(t)
        await asyncio.sleep(0.01)
        assert not app.backends and not app.decisions and t.pending_plan is None
    asyncio.run(go())


def test_next_plan_after_a_restart_gets_a_new_id():
    async def go():
        app, t = make_app(), saved_task()
        app.tasks[t.id] = t
        await ask_plan(t, app, None)
        t = restart(app).tasks["t3"]
        b = object.__new__(agents.Backend)
        b.app, b.task = make_app(), t
        b.app.tasks[t.id] = t
        run = asyncio.create_task(b._permission("ExitPlanMode", {"plan": "v2"}, {}))
        await asyncio.sleep(0)
        assert b.app.decisions[0].id == "t3-plan2"
        run.cancel()
    asyncio.run(go())


# ---- the window --------------------------------------------------------------------------------------------------
def test_plan_events_are_valid():
    assert live.problems(live.event("plan", id="t1-plan", status="pending", text="x")) == []
    assert live.problems(live.event("plan", id="t1-plan", status="maybe"))


def test_plan_heading():
    p = lv.show_output.Palette(lv.show_output.Theme())
    assert "waiting for your approval" in lv.render_plan_head("pending", None, p).plain
    assert lv.render_plan_head("approved", None, p).plain == "● Plan approved"
    assert "skip the tests" in lv.render_plan_head("declined", "skip the tests", p).plain


def test_window_shows_the_pending_plan_in_full_and_updates_the_heading(monkeypatch):
    monkeypatch.setattr(live, "claim_window", lambda key: None)
    monkeypatch.setattr(live, "release_window", lambda key: None)
    key = "t3-view"
    for ev in (live.event("round_start", prompt="plan it"),
               live.event("plan", id="t3-plan", status="pending", text=PLAN)):
        live.append(key, ev)

    async def go():
        app = lv.LiveApp(key)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            head, text = app.flow.plans["t3-plan"]
            assert text == PLAN and "waiting for your approval" in str(head.content)
            console = Console(width=200, record=True, file=io.StringIO())
            for w in app.query("#body *"):
                if getattr(w, "content", None) is not None:
                    console.print(w.content)
            body = console.export_text()
            assert "Add the parser" in body and "Test it" in body
            assert "plan waiting for your approval" in app.step
            live.append(key, live.event("plan", id="t3-plan", status="approved"))
            live.append(key, live.event("job", what="plan_approved", plan=PLAN))
            await pilot.pause(0.6)
            assert "Plan approved" in str(head.content) and app.step == ""
            assert not any(isinstance(w, lv.ToolResult) and "Add the parser" in w.text for w in app.expandables)
            await pilot.press("q")

    asyncio.run(go())
