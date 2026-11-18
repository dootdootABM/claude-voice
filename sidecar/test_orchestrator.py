"""The job rules in orchestrator.JobBackend, without models: who may do what, one builder at a time, mark_done only
after a review, the budget stop, read-only commands skipping the step checker, and handing a long job on to a fresh
orchestrator. The step checker is replaced by a stub; its own behaviour needs a model.

Run: pytest test_orchestrator.py
"""

import asyncio
import os
import tempfile

import pytest
import types

os.environ.setdefault("CLAUDE_VOICE_STATE_DIR", tempfile.mkdtemp())  # never the real saved tasks (conftest sets it)

import orchestrator as o  # noqa: E402


def check(label, got, want):
    assert got == want, f"{label}: {got!r} != {want!r}"


def backend():
    b = object.__new__(o.JobBackend)
    # Not in the temp folder: on Windows that's under AppData, which the risk rules treat as sensitive. The folder never
    # has to exist (nothing is written into it).
    project = os.path.join(os.path.abspath(os.sep), "cv-test-project")
    b.task = types.SimpleNamespace(id="t1", project_dir=project, digest=[], plan="1. a piece",
                                   budget=10.0, spent_before=0.0, check_cost=0.0, context=0, prompt="the job")
    b.app = types.SimpleNamespace(mode="ask", ui=types.SimpleNamespace(dim=lambda m: None, status=lambda m: None))
    b.dir, b.workers, b.uses, b.proc_cost, b.marked_at = o.state_dir(b.task.project_dir), {}, {}, 0.0, 0
    b.checked = []
    b.hand_on, b.carry_on = False, False

    async def check_stub(w, name, inp, verb, target, why=None):
        b.checked.append((w.kind, name))
        return True, "fine"

    b._check = check_stub
    return b


def started(b, agent, kind):
    b.uses[f"use-{agent}"] = (kind, f"brief for {agent}", f"{kind} {agent}")
    b._on_event({"type": "system", "subtype": "task_started", "task_id": agent, "tool_use_id": f"use-{agent}"})


def finished(b, agent):
    b._on_event({"type": "system", "subtype": "task_updated", "task_id": agent, "patch": {"status": "completed"}})


def behaviour(resp):
    return resp["behavior"]


async def main():
    b = backend()
    path = os.path.join(b.task.project_dir, "calc.py")
    edit = {"file_path": path, "old_string": "a", "new_string": "b"}

    check("orchestrator edits a file", behaviour(await b._permission("Edit", edit, {})), "deny")
    check("orchestrator runs a command", behaviour(await b._permission("Bash", {"command": "ls"}, {})), "deny")
    check("orchestrator reads", behaviour(await b._permission("Read", {"file_path": path}, {})), "allow")
    check("orchestrator saves state", behaviour(await b._permission("mcp__voiceapp__save_state", {}, {})), "allow")

    started(b, "s1", "scout")
    started(b, "s2", "scout")
    check("two scouts read at once", [behaviour(await b._permission("Read", {"file_path": path}, {"agent_id": a}))
                                      for a in ("s1", "s2")], ["allow", "allow"])
    check("scout edits", behaviour(await b._permission("Edit", edit, {"agent_id": "s1"})), "deny")
    finished(b, "s1")
    finished(b, "s2")

    started(b, "b1", "builder")
    check("builder edit goes to the checker", behaviour(await b._permission("Edit", edit, {"agent_id": "b1"})),
          "allow")
    check("checker saw it", b.checked[-1], ("builder", "Edit"))
    check("builder's file recorded", sorted(b.workers["b1"].files), ["calc.py"])
    check("builder's PowerShell step allowed", behaviour(await b._permission("PowerShell", {"command": "Get-Date"},
                                                                             {"agent_id": "b1"})), "allow")
    check("builder's PowerShell command recorded", b.workers["b1"].commands[-1:], ["Get-Date"])
    started(b, "b2", "builder")
    check("second builder while one runs", behaviour(await b._permission("Edit", edit, {"agent_id": "b2"})), "deny")
    finished(b, "b2")
    started(b, "r0", "reviewer")
    check("reviewer reads during a build", behaviour(await b._permission("Read", {"file_path": path},
                                                                         {"agent_id": "r0"})), "deny")
    finished(b, "r0")
    check("mark_done before any builder finished", (await b._mark_done({"piece": "1", "summary": "x"}))[:7],
          "Refused")
    finished(b, "b1")
    check("mark_done without a review after it", (await b._mark_done({"piece": "1", "summary": "x"}))[:7],
          "Refused")
    started(b, "r1", "reviewer")
    check("reviewer edits", behaviour(await b._permission("Write", {"file_path": path, "content": ""},
                                                          {"agent_id": "r1"})), "deny")
    check("reviewer runs tests", behaviour(await b._permission("Bash", {"command": "python test_calc.py"},
                                                               {"agent_id": "r1"})), "allow")
    seen = len(b.checked)
    check("reviewer's git diff allowed", behaviour(await b._permission("Bash", {"command": "git diff --stat"},
                                                                       {"agent_id": "r1"})), "allow")
    check("read-only command skips the checker", len(b.checked), seen)
    finished(b, "r1")
    check("mark_done after the review", (await b._mark_done({"piece": "1. a piece", "summary": "x"}))[:8],
          "Recorded")
    check("done list has the builder's files", b.done_list()[0]["files"], ["calc.py"])
    check("same piece marked twice", (await b._mark_done({"piece": "1. again", "summary": "x"}))[:7], "Refused")

    # A long conversation hands the job on after a piece is done (cv starts the fresh one when the round ends).
    started(b, "b4", "builder")
    await b._permission("Edit", edit, {"agent_id": "b4"})
    finished(b, "b4")
    started(b, "r4", "reviewer")
    finished(b, "r4")
    b.task.context = o.FRESH_AFTER
    check("mark_done in a long conversation", (await b._mark_done({"piece": "2", "summary": "x"}))[:40],
          "Recorded. This conversation has grown lo")
    check("hand-on set", b.hand_on, True)
    handed = []

    async def hand_on_stub():
        handed.append(True)

    b._hand_on = hand_on_stub
    check("round end hands on", (await b._after_round("saved", True), handed), (True, [True]))

    # ... but not after the plan's last piece: that conversation writes the final report itself.
    b.hand_on = False
    started(b, "b5", "builder")
    await b._permission("Edit", edit, {"agent_id": "b5"})
    finished(b, "b5")
    started(b, "r5", "reviewer")
    finished(b, "r5")
    check("last piece in a long conversation", (await b._mark_done({"piece": "3", "summary": "x", "last": True}))[:8],
          "Recorded")
    check("no hand-on after the last piece", b.hand_on, False)

    check("save_state with the tool's name for the field", await b._save_state({"state": "# where we are"}), "Saved.")
    check("state written", b.state(), "# where we are\n")
    check("empty save_state refused", (await b._save_state({"markdown": " "}))[:7], "Refused")
    check("state kept", b.state(), "# where we are\n")
    check("opening carries the approved plan", "The plan the user approved:\n1. a piece" in b.opening("go"), True)
    check("a new job starts in plan mode", b._mode(False, False), "plan")
    b.carry_on = True
    check("a carried-on job skips plan mode", b._mode(False, False), "default")
    b.carry_on = False

    b.task.check_cost = 10.0
    started(b, "b3", "builder")
    check("budget used up", behaviour(await b._permission("Edit", edit, {"agent_id": "b3"})), "deny")

    # A fresh start would close the session and kill the running builder: it waits for the builder instead.
    check("running workers", b.running_workers(), ["builder"])
    task = types.SimpleNamespace(session_id="s", open=True, state="idle", active=False, handover=None, backend=b,
                                 fresh_after=None)
    front = object.__new__(__import__("agents").Front)
    front.app = types.SimpleNamespace(tasks={"t1": task})
    front._check_quote = lambda quote: None
    check("fresh start while a builder runs", (await front._fresh_start({"task_id": "t1", "keep": "k"}))[:20],
          "Its builder is still")
    check("fresh start deferred", task.fresh_after, "k")
    finished(b, "b3")
    check("no running workers after it finished", b.running_workers(), [])



READ_ONLY = ["git diff --stat -- audio.py | tail -3", "cd /x && grep -n foo bar.py | head -40", "sed -n 120,180p a.py",
             "sed -n '1,40p' a.py", "UV_PROJECT_ENVIRONMENT=/tmp/x uv run --no-sync pytest -q 2>&1 | tail -5",
             "PYTHONUTF8=0 LC_ALL=C python3 -m pytest -q", "find . -name '*.py' | wc -l", "ls -la; git status --short",
             "cat a.py 2>/dev/null"]
WRITES = ["sed -i s/a/b/ x", "sed -n 'w out' x", "cat a > b", "echo hi >> f", "python3 - <<'E'\nprint(1)\nE",
          "git commit -m x", "rm -rf x", "ls & rm x", "find . -delete", "echo $(rm x)", "uv run pytest", "sort -o f g",
          "git diff --output=f", "cd x && uv lock", "for f in *; do cat $f; done", "python test_calc.py", ""]


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only(command):
    assert o.read_only(command)


@pytest.mark.parametrize("command", WRITES)
def test_not_read_only(command):
    assert not o.read_only(command)


def test_job_rules(monkeypatch):
    # A frozen clock: worker order must not depend on it (Windows' clock ticks ~15 ms apart).
    monkeypatch.setattr(o.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(o.Backend, "_on_event", lambda self, ev: None)  # the base class's transcript output isn't under test
    asyncio.run(main())


def test_checker_is_told_when_a_step_is_shortened():
    short = o.step_text({"file_path": "a.py", "content": "x = 1\n"})
    assert "more characters" not in short and "x = 1" in short
    body = "y" * (o.CHECK_TEXT_MAX + 1345)
    text = o.step_text({"file_path": "a.py", "content": body})
    assert "1345 more characters, not shown here" in text  # not a file that looks written cut off
    assert "y" * (o.CHECK_TEXT_MAX + 1) not in text


def test_checker_context_is_a_prompt_file_named_by_content():
    a = o.checker_prompt_file("APPROVED PLAN:\n1. a piece")
    assert a == o.checker_prompt_file("APPROVED PLAN:\n1. a piece")  # same context: same file, so the cache is hit
    assert a != o.checker_prompt_file("APPROVED PLAN:\n2. another")  # a new context never rewrites one in use
    with open(a, encoding="utf-8") as f:
        text = f.read()
    assert text.startswith(o.config.prompt("step_check")) and text.endswith("APPROVED PLAN:\n1. a piece")
    stale = os.path.join(o.CHECKER_DIR, "stale.md")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("old")
    os.utime(stale, (0, 0))
    o.checker_prompt_file("APPROVED PLAN:\n3. a third")  # writing a new one drops those long out of use
    assert not os.path.exists(stale) and os.path.exists(a)


def test_check_sends_only_the_step_and_keeps_the_rest_in_the_context(monkeypatch):
    b = backend()
    seen = {}

    async def fake_checker(task, ask, log_key, what, context=""):
        seen.update(ask=ask, context=context)
        return True, "fine"

    monkeypatch.setattr(o, "run_checker", fake_checker)
    monkeypatch.setattr(b, "done_list", lambda: [{"piece": "0. Snapshot", "summary": "made", "files": ["a.sh"]}])
    w = o.Worker("builder", "brief: edit calc.py only", "builder one")
    w.files.add("calc.py")
    inp = {"file_path": "calc.py", "old_string": "a", "new_string": "b"}
    ok, _ = asyncio.run(o.JobBackend._check(b, w, "Edit", inp, "edit", "calc.py"))
    assert ok
    for part in ("APPROVED PLAN:\n1. a piece", "0. Snapshot: made", "WORKER: builder", "brief: edit calc.py only"):
        assert part in seen["context"] and part not in seen["ask"], part
    assert "THE STEP: Edit (edit calc.py)" in seen["ask"] and "EDITED SO FAR: calc.py" in seen["ask"]
    assert "THE STEP" not in seen["context"]  # the step is the only part that is new on each check


def test_checker_gets_the_projects_last_plan_when_the_job_has_none(tmp_path):
    b = backend()
    b.task.plan, b.dir = "", str(tmp_path)
    assert b.plan_for_checker() == "APPROVED PLAN:\n(not recorded)"
    (tmp_path / "plan.md").write_text("1. the earlier plan\n", encoding="utf-8")
    text = b.plan_for_checker()
    assert text.endswith("1. the earlier plan") and "no plan of its own" in text
    b.task.plan = "1. its own plan"
    assert b.plan_for_checker() == "APPROVED PLAN:\n1. its own plan"


def test_run_checker_passes_the_context_as_the_system_prompt_file(monkeypatch):
    seen = {}

    class Proc:
        async def communicate(self, data):
            seen["stdin"] = data.decode()
            return b'{"total_cost_usd": 0.5, "result": "{\\"allow\\": false, \\"reason\\": \\"not in the brief\\"}"}', b""

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(o.asyncio, "create_subprocess_exec", fake_exec)
    task = types.SimpleNamespace(project_dir=os.getcwd(), check_cost=0.0)
    ok, reason = asyncio.run(o.run_checker(task, "THE STEP: Bash (run ls)", "job-t1", "builder run ls", "APPROVED PLAN:\nthe plan"))
    assert (ok, reason, task.check_cost) == (False, "not in the brief", 0.5)
    cmd = seen["cmd"]
    assert "--system-prompt" not in cmd
    with open(cmd[cmd.index("--system-prompt-file") + 1], encoding="utf-8") as f:
        assert f.read().endswith("APPROVED PLAN:\nthe plan")
    assert seen["stdin"] == "THE STEP: Bash (run ls)"  # the plan is not sent again with every step
