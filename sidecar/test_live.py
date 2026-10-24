"""The live output log (live.py) and the Backend hooks that write it, without a terminal or claude.

Run: pytest test_live.py
"""

import asyncio
import json
import os
import types

import pytest

import agents
import live
import orchestrator as o


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def terminal(monkeypatch):
    """A fake spawn_terminal and process table: windows[pid] = alive."""
    spawned, alive = [], {}

    def spawn(argv, cls, title=None, wait=False, cwd=None):
        spawned.append(argv)
        pid = 40000 + len(spawned)
        alive[pid] = True
        return types.SimpleNamespace(pid=pid)

    monkeypatch.setattr(live.platform_util, "spawn_terminal", spawn)
    monkeypatch.setattr(live.platform_util, "pid_alive", lambda pid: alive.get(pid, False))
    return spawned, alive


def test_schema_round_trip():
    evs = [live.event("round_start", prompt="héllo 🗣\nline2"), live.event("text", text="hi"),
           live.event("tool_use", id="u1", name="Edit", summary="a.py", input={"old_string": "a", "new_string": "b"}),
           live.event("tool_result", tool_use_id="u1", text="ok", is_error=False),
           live.event("permission", tool="Bash", decision="refused", by="step_checker", reason="no"),
           live.event("subagent_start", id="s1", name="scout", parent="u2"),
           live.event("subagent_end", id="s1", name="scout", status="completed"),
           live.event("round_end", duration_s=1.5, turns=3), live.event("task_closed")]
    for e in evs:
        assert live.problems(e) == [], e
        assert live.append("t1", e)
    assert live.read_all("t1") == evs
    assert live.problems({"kind": "nope"}) and live.problems(live.event("text"))
    assert live.problems(live.event("permission", tool="x", decision="maybe", by="user"))
    assert "parent" not in live.event("text", text="x", parent=None)


def test_summarize():
    assert live.summarize("Bash", {"command": "git diff\n  --stat"}) == "git diff --stat"
    assert live.summarize("Edit", {"file_path": "/a/b.py", "old_string": "x"}) == "/a/b.py"
    assert live.summarize("X", {}) == ""


def test_append_and_offsets():
    for i in range(3):
        live.append("t1", live.event("text", text=f"é{i}"))
    got = list(live.iter_from("t1"))
    assert [e["text"] for e, _ in got] == ["é0", "é1", "é2"]
    mid = got[0][1]
    assert [e["text"] for e, _ in live.iter_from("t1", mid)] == ["é1", "é2"]
    events, end = live.read_from("t1", got[-1][1])
    assert events == [] and end == got[-1][1]
    # a half-written last line waits for its newline; a garbage line is skipped
    with open(live.path_for("t1"), "ab") as f:
        f.write(b'garbage\n{"kind":"text","te')
    events, end2 = live.read_from("t1", end)
    assert events == [] and end2 > end
    with open(live.path_for("t1"), "ab") as f:
        f.write(b'xt":"done"}\n')
    assert [e["text"] for e in live.read_from("t1", end2)[0]] == ["done"]
    # a file that was reset restarts at 0
    live.remove("t1")
    live.append("t1", live.event("text", text="new"))
    events, end3 = live.read_from("t1", end2 + 1000)
    assert [e["text"] for e in events] == ["new"] and end3 < end2 + 1000
    with open(live.path_for("t1"), "rb") as f:
        assert b"\r\n" not in f.read()


def test_restart_appends_to_same_file():
    live.append("t1", live.event("text", text="before"))
    import importlib
    importlib.reload(live)  # a new process: nothing in memory
    live.append("t1", live.event("text", text="after"))
    assert [e["text"] for e in live.read_all("t1")] == ["before", "after"]
    assert os.listdir(live.live_dir()) == ["t1.jsonl"]


def test_open_window_once_then_again_after_death(terminal):
    spawned, alive = terminal
    live.append("t1", live.event("text", text="history"))
    assert live.open_window("t1") is True
    assert spawned[0][-2:] == ["--live", "t1"] and spawned[0][1].endswith("show_output.py")
    assert live.open_window("t1") is False  # placeholder pid is alive: no second window
    live.claim_window("t1")  # the viewer takes over with its own pid
    alive[os.getpid()] = True
    assert live.open_window("t1") is False and len(spawned) == 1
    alive[os.getpid()] = False  # window closed
    assert live.open_window("t1") is True and len(spawned) == 2  # replays the same file
    assert [e["text"] for e in live.read_all("t1")] == ["history"]
    assert live.open_window("t1", force=True) is True and len(spawned) == 3
    live.open_window("t2")
    assert len(spawned) == 4  # another task, another window


def test_release_window(terminal):
    spawned, alive = terminal
    live.append("t1", live.event("text", text="x"))
    live.claim_window("t1")
    alive[os.getpid()] = True
    live.release_window("t1")
    assert not os.path.exists(live.pid_path("t1")) and os.path.exists(live.path_for("t1"))
    live.append("t1", live.event("task_closed"))
    live.claim_window("t1")
    live.release_window("t1")  # closed task, window gone: the log goes too
    assert os.listdir(live.live_dir()) == []


def test_cleanup_on_close(terminal, monkeypatch):
    spawned, alive = terminal
    clock = [1000.0]
    monkeypatch.setattr(live, "_now", lambda: clock[0])
    live.append("t1", live.event("text", text="x"))
    live.close_task("t1")  # no window: gone at once
    assert not os.path.exists(live.path_for("t1"))
    live.append("t2", live.event("text", text="x"))
    live.open_window("t2")
    live.close_task("t2")
    live.close_task("t2")  # twice: still one task_closed
    assert [e["kind"] for e in live.read_all("t2")] == ["text", "task_closed"]
    alive.clear()  # the window goes away (and the placeholder has run out)
    clock[0] += 60
    live.close_task("t2")
    assert not os.path.exists(live.path_for("t2"))


def test_prune(terminal):
    live.append("t1", live.event("text", text="x"))
    live.append("t2", live.event("text", text="x"))
    live.prune(["t1"])
    assert sorted(os.listdir(live.live_dir())) == ["t1.jsonl"]


def test_writer_errors_do_not_propagate(monkeypatch, state):
    (state / "live").write_text("a file where the folder should be", encoding="utf-8")
    assert live.append("t1", live.event("text", text="x")) is False
    assert live.emit("t1", "text", text=object()) is False  # not serialisable
    backend = object.__new__(agents.Backend)
    backend.task = types.SimpleNamespace(id="t1", live_key="t1")
    backend._live("text", text="x")  # no exception
    backend.agents = {}
    backend._live_event({"type": "user", "message": {"content": [{"type": "tool_result", "content": object()}]}})


def backend(tmp):
    b = object.__new__(o.JobBackend)
    b.task = types.SimpleNamespace(id="t1", live_key="t1", project_dir=str(tmp), digest=[], session_id="s", context=0,
                                   plan="", budget=10.0, spent_before=0.0, check_cost=0.0)
    b.app = types.SimpleNamespace(ui=types.SimpleNamespace(dim=lambda m: None, status=lambda m: None, agent=lambda *a: None),
                                  save_tasks=lambda: None, narrate=lambda *a, **k: None)
    b.dir, b.workers, b.uses, b.proc_cost, b.marked_at, b.agents = str(tmp), {}, {}, 0.0, 0.0, {}
    return b


def test_stream_events_are_logged(state):
    b = backend(state)
    b._on_event({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "🗣 looking"},
        {"type": "tool_use", "id": "u1", "name": "Agent", "input": {"subagent_type": "scout", "prompt": "p", "description": "d"}},
        {"type": "tool_use", "id": "u3", "name": "TodoWrite", "input": {"todos": [{"content": "a", "status": "pending"}]}}]}})
    b._on_event({"type": "system", "subtype": "task_started", "task_id": "w1", "tool_use_id": "u1", "description": "d"})
    b._on_event({"type": "assistant", "parent_tool_use_id": "u1", "message": {"content": [
        {"type": "tool_use", "id": "u2", "name": "Bash", "input": {"command": "ls"}}]}})
    b._on_event({"type": "user", "parent_tool_use_id": "u1", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u2", "content": [{"type": "text", "text": "a\nb"}], "is_error": True}]}})
    b._on_event({"type": "system", "subtype": "task_notification", "task_id": "w1", "status": "completed"})
    evs = live.read_all("t1")
    assert [e["kind"] for e in evs] == ["text", "tool_use", "tool_use", "subagent_start", "tool_use", "tool_result",
                                        "subagent_end"]
    assert all(live.problems(e) == [] for e in evs)
    assert "input" in evs[2] and "input" not in evs[1] and evs[1]["summary"]
    assert evs[3]["name"] == "scout" and evs[3]["parent"] == "u1"
    assert evs[4]["parent"] == "u1" and evs[4]["agent"] == "scout" and evs[4]["summary"] == "ls"
    assert evs[5]["text"] == "a\nb" and evs[5]["is_error"] is True and evs[5]["agent"] == "scout"
    assert evs[6]["status"] == "completed"


def test_permission_events(state):
    b = backend(state)
    b.task.plan = "plan"

    async def perm(name, inp, req):
        agents.decided_by("user") if name == "Bash" else None
        return {"behavior": "allow"} if name != "Edit" else {"behavior": "deny", "message": "nope"}

    b._permission = perm

    async def run():
        await b._permission_logged("Read", {"file_path": "a"}, {})  # quiet: not logged
        await b._permission_logged("Bash", {"command": "ls"}, {})
        await b._permission_logged("Edit", {"file_path": "a"}, {})

    asyncio.run(run())
    evs = live.read_all("t1")
    assert [(e["tool"], e["decision"], e["by"]) for e in evs] == [("Bash", "approved", "user"), ("Edit", "refused", "auto")]
    assert evs[1]["reason"] == "nope" and all(live.problems(e) == [] for e in evs)


def test_round_events(state):
    b = backend(state)
    sent = []

    async def send(text):
        sent.append(text)

    b.proc = types.SimpleNamespace(send=send)

    async def run():
        await b.send_user("do it")
        await b.send_user("also this", queued=True)

    asyncio.run(run())
    evs = live.read_all("t1")
    assert sent == ["do it", "also this"] and b.task.round_t0
    assert [(e["kind"], e["prompt"], e.get("queued")) for e in evs] == [("round_start", "do it", None),
                                                                        ("round_start", "also this", True)]


class FakeApp:
    def __init__(self):
        self.tasks, self.opened = {}, []
        self.ui = types.SimpleNamespace(status=lambda m: None, error=lambda m: None)
        self.full_approval = False

    show_output = agents.App.show_output


@pytest.mark.parametrize("auto", [True, False])
def test_auto_open_follows_config(monkeypatch, terminal, auto):
    spawned, _ = terminal
    cfg = agents.config.get()
    monkeypatch.setitem(cfg["behaviour"], "auto_show_output", auto)
    app = agents.App.__new__(agents.App)
    app.tasks, app.full_approval = {"t1": None}, False
    app.ui = types.SimpleNamespace(status=lambda m: None, error=lambda m: None)
    task = agents.Task("t1", {"title": "x", "prompt": "p", "project_dir": "/"})
    app.tasks["t1"] = task

    class B:
        def __init__(self, *a, **k):
            pass

        async def start(self, message=None):
            live.emit(task.live_key, "round_start", prompt="p")

        def running_workers(self):
            return []

    app.backend_for = lambda t, **kw: B()
    asyncio.run(app._start(task))
    assert len(spawned) == (1 if auto else 0)
    # asking for it (the show_output tool, the o key) opens it either way, but only once
    assert app.show_output(task) is None
    assert app.show_output() is None
    assert len(spawned) == 1


def test_show_output_without_anything():
    app = agents.App.__new__(agents.App)
    app.tasks = {}
    assert "no output" in app.show_output()


def make_app(tasks):
    app = agents.App.__new__(agents.App)
    app.tasks, app.full_approval = {t.id: t for t in tasks}, False
    app.errors = []
    app.ui = types.SimpleNamespace(status=lambda m: None, error=app.errors.append)
    return app


def test_saved_output_only_opens_file_viewer_once(terminal, state):
    spawned, alive = terminal
    out = state / "out.md"
    out.write_text("# done", encoding="utf-8")
    task = agents.Task("t1", {"title": "x", "prompt": "p", "project_dir": "/"})
    task.output = str(out)
    app = make_app([task])
    assert app.show_output(task) is None and app.show_output() is None
    assert len(spawned) == 1 and spawned[0][-1] == str(out) and "--live" not in spawned[0]
    assert not os.path.exists(live.path_for(task.live_key))  # no empty jsonl
    assert not os.path.exists(live.live_dir()) or not [n for n in os.listdir(live.live_dir()) if n.endswith(".jsonl")]
    # an empty log counts as no log
    open(live.path_for(task.live_key), "ab").close()
    assert app.show_output(task) is None and len(spawned) == 1
    # with events, the live viewer is used
    live.emit(task.live_key, "round_start", prompt="p")
    assert app.show_output(task) is None and len(spawned) == 2 and spawned[1][-2:] == ["--live", task.live_key]


def test_show_output_nothing_does_not_create_files(terminal):
    task = agents.Task("t1", {"title": "x", "prompt": "p", "project_dir": "/"})
    app = make_app([task])
    assert "no output" in app.show_output(task)
    assert not os.path.exists(live.path_for(task.live_key))
    spawned, _ = terminal
    assert spawned == []
    live.open_window("t9")
    assert not os.path.exists(live.path_for("t9"))


def test_reused_task_id_gets_its_own_log_and_window(terminal, monkeypatch):
    spawned, alive = terminal
    old = agents.Task("t1", {"title": "x", "prompt": "p", "project_dir": "/"})
    new = agents.Task("t1", {"title": "y", "prompt": "q", "project_dir": "/"})
    assert old.live_key != new.live_key and old.live_key.startswith("t1-")
    live.emit(old.live_key, "text", text="old history")
    app = make_app([new])
    live.open_window(old.live_key)
    live.claim_window(old.live_key)
    alive[os.getpid()] = True
    # the old task is closed while its window is open: the file stays, closed
    live.close_task(old.live_key)
    assert os.path.exists(live.path_for(old.live_key))
    # a new task with the same id: its own file, the old window and file are untouched
    live.emit(new.live_key, "text", text="new")
    assert app.show_output(new) is None
    assert [e["text"] for e in live.read_all(old.live_key) if "text" in e] == ["old history"]
    assert [e["text"] for e in live.read_all(new.live_key)] == ["new"]
    assert len(spawned) == 2 and spawned[1][-1] == new.live_key
    # a restored task keeps its key (uid is saved), so a restart appends to the same file
    again = agents.Task.from_dict(new.to_dict())
    assert again.live_key == new.live_key
    # prune keeps live tasks and open windows only
    live.prune([new.live_key])
    assert os.path.exists(live.path_for(old.live_key))  # window alive
    alive.clear()
    live.prune([new.live_key])
    assert not os.path.exists(live.path_for(old.live_key)) and os.path.exists(live.path_for(new.live_key))


def test_placeholder_pid_counts_as_alive_for_a_while(terminal, monkeypatch):
    spawned, alive = terminal
    clock = [1_000_000.0]
    monkeypatch.setattr(live, "_now", lambda: clock[0])
    monkeypatch.setattr(live, "_boot_time", lambda: 0)
    assert live.open_window("t1") is True
    alive.clear()  # the launcher exited at once, the viewer hasn't started yet
    clock[0] += live.PLACEHOLDER_S - 1
    assert live.open_window("t1") is False and len(spawned) == 1
    clock[0] += 2  # the viewer never came up
    assert live.open_window("t1") is True and len(spawned) == 2
    # the viewer claims the window: now its own pid decides, not the clock
    live.claim_window("t1")
    alive[os.getpid()] = True
    clock[0] += 1000
    assert live.open_window("t1") is False
    alive[os.getpid()] = False
    assert live.open_window("t1") is True


def test_pid_file_from_before_boot_is_ignored(terminal, monkeypatch):
    spawned, alive = terminal
    clock = [1000.0]
    monkeypatch.setattr(live, "_now", lambda: clock[0])
    monkeypatch.setattr(live, "_boot_time", lambda: 0)
    live.claim_window("t1")
    alive[os.getpid()] = True
    assert live.window_alive("t1")
    monkeypatch.setattr(live, "_boot_time", lambda: 2000)  # rebooted since; the pid may belong to something else
    assert not live.window_alive("t1")


def _round_backend(state):
    b = backend(state)
    b.proc = types.SimpleNamespace(results=asyncio.Queue())
    b.app.restart_requested = False
    return b


def test_stopped_round_gets_a_round_end(state):
    b = _round_backend(state)
    b.task.state = "building"
    agents.log_round(b.task, "go")
    b.task.state = "stopped"
    b.proc.results.put_nowait({"subtype": "success", "result": "x"})
    asyncio.run(b._results())
    evs = live.read_all("t1")
    assert [e["kind"] for e in evs] == ["round_start", "round_end"] and evs[1]["ok"] is False
    assert live.problems(evs[1]) == []
    # a second call does not add another
    b._end_round_early()
    assert len(live.read_all("t1")) == 2


def test_stop_ends_the_open_round(state):
    b = _round_backend(state)
    agents.log_round(b.task, "go")

    async def noop(*a, **k):
        pass

    b.proc.interrupt = b.proc.close = noop
    asyncio.run(b.stop("closed"))
    live.close_task("t1")  # as Backend.close does next
    assert not os.path.exists(live.path_for("t1"))  # no stray file recreated afterwards
    agents.log_round(b.task, "go")
    asyncio.run(b.stop("saved"))
    evs = live.read_all("t1")
    assert [e["kind"] for e in evs] == ["round_start", "round_end"] and evs[1]["ok"] is False


def test_tasks_never_speak(state):
    """Tasks don't talk: their text and steps go to the live log and the digest, never to the speaker."""
    b = backend(state)
    said = []
    b.app.tts = types.SimpleNamespace(say=lambda *a, **k: said.append(a))
    b._on_event({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "🗣 Looking at audio.py first.\nThen the tests."},
        {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "pytest -q"}}]}})
    assert said == []
    assert any("pytest" in d or "command" in d for d in b.task.digest)  # Haiku answers "how's it going" from it


@pytest.mark.parametrize("verbosity", ["quiet", "normal", "chatty"])
def test_round_report_is_haikus_summary(state, verbosity):
    b = _round_backend(state)
    notes = []
    b.hand_on = False
    b.task.__dict__.update(state="building", go_ahead=set(), handover=None, fresh_from="", show_next=False,
                           kind="job", nudged=99, fresh_after=None, handed_back="", budget_warned=False,
                           last_result="", output="", title="Fix the clip")
    b.app.__dict__.update(verbosity=verbosity, front=types.SimpleNamespace(notify=notes.append),
                          show_output=lambda t: "no window here")
    b.app.ui.result = lambda *a: None
    b.proc.results.put_nowait({"subtype": "success", "result": "Fixed the clip in audio.py; tests pass."})
    b.proc.results.put_nowait(None)
    b.task.state = "building"

    async def run():
        b.task.backend = b
        await b._results()

    asyncio.run(run())
    assert agents.SUMMARY[verbosity] in notes[0]  # (notes[1] is the session ending, from the None above)
    assert "already heard" not in notes[0]


def test_claude_code_details_are_logged(state):
    """The extra data the window draws like Claude Code: thinking, result meta, progress, totals, tokens, the brief."""
    b = backend(state)
    msg = lambda mid, out, content, **k: {"type": "assistant", **k, "message": {"id": mid, "content": content,
                                                                                "usage": {"output_tokens": out}}}
    b._live_event(msg("m1", 60, [{"type": "thinking", "thinking": "", "signature": "x"},
                                 {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/p/a.py"}}]))
    b._live_event(msg("m1", 150, [{"type": "thinking", "thinking": ""}]))  # the same message, more tokens
    b._live_event({"type": "user", "tool_use_result": {"file": {"numLines": 48, "startLine": 1, "totalLines": 48}},
                   "message": {"content": [{"type": "tool_result", "tool_use_id": "r1", "content": "x"}]}})
    b._live_event(msg("m2", 10, [{"type": "tool_use", "id": "a1", "name": "Agent",
                                  "input": {"subagent_type": "builder", "description": "d", "prompt": "the brief"}}]))
    b._live_event({"type": "system", "subtype": "task_started", "task_id": "w1", "tool_use_id": "a1",
                   "subagent_type": "builder", "description": "d", "prompt": "the brief"})
    b._live_event({"type": "system", "subtype": "task_progress", "task_id": "w1", "description": "Editing a.py",
                   "usage": {"total_tokens": 5642, "tool_uses": 1, "duration_ms": 3760}})
    b._live_event({"type": "system", "subtype": "task_updated", "task_id": "w1", "patch": {"status": "completed"}})
    b._live_event({"type": "system", "subtype": "task_notification", "task_id": "w1", "status": "completed",
                   "usage": {"total_tokens": 36777, "tool_uses": 10, "duration_ms": 48422}})
    evs = live.read_all("t1")
    assert all(live.problems(e) == [] for e in evs), [live.problems(e) for e in evs]
    kinds = [e["kind"] for e in evs]
    assert kinds == ["thinking", "tool_use", "tokens", "tool_result", "tool_use", "subagent_start", "progress",
                     "subagent_end", "subagent_end"]
    assert evs[2]["out"] == 150  # one message, counted once at its highest
    assert evs[3]["meta"] == {"lines": 48, "start": 1, "total": 48}
    assert evs[4]["subagent_type"] == "builder" and evs[5]["brief"] == "the brief"
    assert (evs[6]["step"], evs[6]["tokens"], evs[6]["parent"]) == ("Editing a.py", 5642, "a1")
    assert "tokens" not in evs[7] and (evs[8]["tokens"], evs[8]["tool_uses"], evs[8]["ms"]) == (36777, 10, 48422)


def test_job_milestones_and_deciders_are_logged(state):
    b = backend(state)
    b.task.budget, b.task.budget_warned = 10.0, False
    b.proc_cost = 9.0
    asyncio.run(b._save_state({"markdown": "state"}))
    b.budget_note()
    asyncio.run(b._hand_back({"reason": "one file"}))
    box = {"by": "auto"}
    agents._decided_by.set(box)
    b._deny("second builder", "wait")
    evs = live.read_all("t1")
    assert [(e["what"], e.get("spent_pct"), e.get("reason")) for e in evs] == [
        ("state_saved", None, None), ("budget", 90, None), ("handed_back", None, "one file")]
    assert box["by"] == "rules"
    b._deny("checker refused", "no", by="step_checker")
    assert box["by"] == "step_checker"


def test_open_window_already_open_brings_it_forward(terminal, monkeypatch):
    spawned, alive = terminal
    focused = []
    monkeypatch.setattr(live.platform_util, "focus_window", lambda title: focused.append(title) or True)
    live.append("t3", live.event("text", text="x"))
    assert live.open_window("t3", title="cv · t3 · job") is True and focused == []
    assert live.open_window("t3", title="cv · t3 · job") is False  # already open: no second window ...
    assert focused == ["cv · t3 · job"] and len(spawned) == 1  # ... but it's brought to the front
