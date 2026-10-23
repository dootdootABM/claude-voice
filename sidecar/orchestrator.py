"""Project jobs: an orchestrator session that plans, hands each piece to a worker, and checks every step.

A job is a task with route "job". Its Claude Code session is the orchestrator: it never edits or runs anything itself
and works through Claude Code subagents (AGENTS, passed with --agents):

  scout     read-only investigation; several may run at once
  builder   makes one piece's change; only ever one at a time
  reviewer  checks a finished piece against its brief; read-only apart from running checks

cv enforces the rules rather than trusting the plan. Every permission request from a subagent carries its agent_id,
which Claude Code's task_started event ties to the worker type and its brief, so JobBackend._permission knows who is
asking: the orchestrator's own edits are refused, a second builder waits, read-only workers wait while a builder
edits, and every builder or reviewer edit and command (after the usual risk check, which still goes to the user)
goes to the step checker: a one-shot model call that sees the plan, the brief, the done list and the exact step.
Commands that only read (look at files, git status/diff/log, run the tests) skip it: it costs a fresh call each time
and has nothing to judge there.

Usage grows with how long a conversation runs, since every call re-reads it. So workers report briefly, scouts run on
a cheaper model, and once a piece is marked done in a conversation that has grown long, the orchestrator saves its
state and cv carries the job on in a fresh conversation (the approved plan, the state and the done list; no new plan
review).

The done list is cv's: mark_done records the files that builder really touched, and is refused unless a reviewer
checked the work after the last builder. The plan and the done list live in STATE_DIR/projects/<folder>/, never in
the repo, so a fresh orchestrator can carry on from them.
"""

import asyncio
import itertools
import json
import os
import re
import time

import config
import cvlog
import platform_util
import sessions
from claude_proto import subscription_env
from agents import SHELL_TOOLS, prompt_note, Backend, STATE_DIR, Task, decided_by, describe_tool, risk

# A job's usage cap, in Claude Code's list-price estimate. On a subscription nothing is charged: the cap only stops
# one job from eating the plan's 5-hour and weekly limits. Never spoken as money.
BUDGET_WARN = 0.8
CHECK_TIMEOUT_S = 120
# Order of events (a worker finishing, a piece marked done): a counter, not the clock, whose ticks on Windows are
# ~15 ms apart, so a builder and its reviewer could seem to finish at the same moment.
ORDER = itertools.count(1)
FRESH_AFTER = 40_000  # tokens: past this, a finished piece hands the job on to a fresh orchestrator
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "TodoWrite", "ToolSearch", "NotebookRead"}
WRITE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}

REPORT = ("End with this report and nothing after it, at most 15 lines: the orchestrator keeps it for the rest of "
          "the job, so give paths and line numbers, not pasted code, diffs or logs.\n"
          "Status: done | blocked | failed\nChanged: files, one line each (or none)\nFound: what matters for the next "
          "step\nChecks: what you ran and the result\nNeeds: a decision or question, if blocked")
AGENTS = {
    "scout": {
        "description": "Read-only investigation of the project: find where things are, how they work, what a change "
                       "would touch. Several can run at once.",
        "prompt": "You investigate and report; you never change anything. Be thorough and specific: paths, names, "
                  "line references. " + REPORT,
        "tools": ["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
    },
    "builder": {
        "description": "Makes the change for exactly one piece of an approved plan. Only one builder runs at a time.",
        "prompt": "You make exactly the change your brief describes, nothing more: no unrelated fixes, no refactors "
                  "it didn't ask for, nothing from the done list again. Read before you edit. Run the checks the "
                  "brief names. Every edit and command is checked against your brief before it runs; if one is "
                  "refused, follow the reason given. " + REPORT,
        "tools": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
    },
    "reviewer": {
        "description": "Checks one finished piece against its brief: reads the diff, runs the tests, reports problems. "
                       "Changes nothing.",
        "prompt": "You verify one piece of work against its brief. Read the diff (git diff) and the code around it, "
                  "run the tests or checks that apply, and look for anything missing, wrong, or done twice. Don't "
                  "fix anything. Status: done only if the piece is complete and correct; otherwise failed, with "
                  "exactly what's wrong. " + REPORT,
        "tools": ["Read", "Grep", "Glob", "Bash"],
    },
}

STATE_SCHEMA = {"type": "object", "properties": {"markdown": {"type": "string", "description":
                "The whole current state: goal, plan with each piece's status, decisions and why, open questions."}},
                "required": ["markdown"]}
DONE_SCHEMA = {"type": "object", "properties": {
    "piece": {"type": "string", "description": "The piece's number and name from the plan, e.g. \"2. Parser tests\"."},
    "summary": {"type": "string", "description": "What was done and what the reviewer confirmed, in a few lines."}},
    "required": ["piece", "summary"]}
HAND_BACK_SCHEMA = {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}


# Commands that only look (or run the tests), so the step checker has nothing to judge. Each part of a command line
# must be one of these, with no output redirection, substitution or here-doc: anything else goes to the checker.
LOOK = {"ls", "cat", "head", "tail", "wc", "grep", "egrep", "rg", "pwd", "echo", "printf", "file", "stat", "du",
        "diff", "cmp", "cut", "tr", "which", "type", "nl", "true", "realpath", "basename", "dirname", "date", "pytest"}
GIT_LOOK = {"status", "diff", "log", "show", "blame", "ls-files", "rev-parse", "grep", "shortlog", "describe"}
SEGMENT = re.compile(r"&&|\|\||;|\||\n")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
SED_PRINT = re.compile(r"""^['"]?(\d+|\$)(,(\d+|\$))?p['"]?$""")  # sed -n 10,40p


def read_only(command):
    """True when every part of a Bash command line only reads files or runs the tests."""
    if not command.strip() or re.search(r"`|\$\(|<<|<\(|>\(", command):
        return False
    plain = re.sub(r"\d?>&\d|\d?>\s*/dev/null", "", command)
    if ">" in plain or re.search(r"(?<!&)&(?!&)", plain):  # writes a file, or runs something in the background
        return False
    for seg in SEGMENT.split(command):
        words = seg.split()
        while words and ASSIGN.match(words[0]):  # VAR=value prefixes
            words.pop(0)
        if not words:
            continue
        cmd, rest = words[0], words[1:]
        if cmd in ("cd", "export") or cmd in LOOK:
            continue
        if cmd == "sort" and not any(a.startswith(("-o", "--output")) for a in rest):
            continue
        if cmd == "sed" and rest[:1] == ["-n"] and len(rest) > 1 and SED_PRINT.match(rest[1]):
            continue
        if cmd == "find" and not {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf",
                                  "-fls"} & set(rest):
            continue
        if cmd == "git" and rest and rest[0] in GIT_LOOK and "--output" not in " ".join(rest):
            continue
        if cmd in ("python", "python3") and rest[:2] == ["-m", "pytest"]:
            continue
        if cmd == "uv" and rest[:2] == ["run", "--no-sync"] and ("pytest" in rest[2:3] or rest[2:5] == [
                "python", "-m", "pytest"]):
            continue
        return False
    return True


def state_dir(project_dir):
    return os.path.join(STATE_DIR, "projects", sessions.slug(project_dir))


class Worker:
    def __init__(self, kind, brief, description):
        self.kind, self.brief, self.description = kind, brief, description
        self.running, self.finished_at = True, 0  # finished_at: an ORDER number
        self.files = set()  # files it edited
        self.commands = []


class JobBackend(Backend):
    """The orchestrator's Claude Code session, and the rules every worker step goes through."""

    @property
    def system_prompt(self):
        return config.prompt("orchestrator")

    def __init__(self, app, task, resume=False, plan=False, carry_on=False):
        self.carry_on = carry_on  # a fresh orchestrator for an approved plan: no plan mode (see _hand_on)
        super().__init__(app, task, resume, plan)
        self.uses = {}  # Agent tool_use id → (kind, brief, description), until its task_started arrives
        self.workers = {}  # agent_id → Worker
        self.dir = state_dir(task.project_dir)
        self.proc_cost = 0.0  # this process's running total (Claude Code reports it cumulatively)
        self.marked_at = 0  # ORDER number of the last mark_done
        self.hand_on = False  # set by mark_done in a long conversation: the next round end starts a fresh one

    # ---- hooks ------------------------------------------------------------------------------------------
    def _mode(self, plan, resume):
        return "default" if self.carry_on or (resume and not plan) else "plan"

    def _extra_args(self):
        scout = config.get()["models"]["scout"] or self.task.model  # read-only work: a cheaper model does
        agents = {k: {**v, "model": scout if k == "scout" else self.task.model} for k, v in AGENTS.items()}
        return ["--agents", json.dumps(agents)]

    def _tools(self):
        return {**super()._tools(),
                "save_state": ("Save the job's whole current state (rewrite it, don't append). Call it after the plan "
                               "is approved and after every piece.", STATE_SCHEMA, self._save_state),
                "mark_done": ("Record a piece as done, after its reviewer confirmed it. cv adds the files the builder "
                              "changed to the done list.", DONE_SCHEMA, self._mark_done),
                "hand_back": ("The job is really one small piece: hand it back to be done as a quick task, without "
                              "a plan.", HAND_BACK_SCHEMA, self._hand_back)}

    # ---- state ------------------------------------------------------------------------------------------
    def _path(self, name):
        os.makedirs(self.dir, exist_ok=True)
        return os.path.join(self.dir, name)

    def done_list(self):
        try:
            with open(self._path("done.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def state(self):
        try:
            with open(self._path("state.md"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def opening(self, prompt):
        """The first message of a fresh orchestrator: the job, plus where earlier work in this project stands."""
        parts = [f"The job (from the user, by voice): {prompt}"]
        if self.task.plan:
            parts.append(f"The plan the user approved:\n{self.task.plan}")
        if state := self.state():
            parts.append(f"State saved by an earlier orchestrator in this project:\n{state}")
        if done := self.done_list():
            parts.append("Done list (finished and reviewed; never redo these):\n" + self._done_text(done))
        return "\n\n".join(parts)

    @staticmethod
    def _done_text(done):
        return "\n".join(f"- {d['piece']}: {d['summary']} (files: {', '.join(d['files']) or 'none'})" for d in done)

    async def _save_state(self, args):
        # The schema says markdown, but a model sometimes names it after the tool ("state"): take any one text.
        text = args.get("markdown") or next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
        if not text.strip():
            return "Refused: nothing to save. Pass the whole current state as `markdown`."
        with open(self._path("state.md"), "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        self.task.digest.append("state saved")
        self._live("job", what="state_saved")
        return "Saved."

    async def _mark_done(self, args):
        if any(w.kind == "builder" and w.running for w in self.workers.values()):
            return "Refused: a builder is still working. Wait for it, have a reviewer check it, then call mark_done."
        # This piece's builders: those that did something since the last mark_done (a retry after a review included;
        # a builder that was refused before it changed anything doesn't count).
        piece = [w for w in self.workers.values() if w.kind == "builder" and w.finished_at > self.marked_at
                 and (w.files or w.commands)]
        if not piece:
            return "Refused: no builder has finished any work since the last piece, so there's nothing to mark done."
        last = max(w.finished_at for w in piece)
        if not any(w.kind == "reviewer" and not w.running and w.finished_at > last for w in self.workers.values()):
            return "Refused: a reviewer has to check the last builder's work first. Run one, then call mark_done."
        files = sorted({f for w in piece for f in w.files})
        self.marked_at = next(ORDER)
        done = self.done_list() + [{"piece": args.get("piece", ""), "summary": args.get("summary", ""),
                                    "files": files, "at": time.strftime("%Y-%m-%d %H:%M")}]
        with open(self._path("done.json"), "w", encoding="utf-8") as f:
            json.dump(done, f, indent=1)
        self.task.digest.append(f"done: {args.get('piece', '')}")
        self._live("job", what="piece_done", piece=args.get("piece", ""), summary=args.get("summary", ""), files=files)
        self.app.ui.status(f"{self.task.id}: done — {args.get('piece', '')} ({len(files)} files)")
        if self.task.context >= FRESH_AFTER:
            self.hand_on = True
            return ("Recorded. This conversation has grown long, so the job carries on in a fresh one. Now call "
                    "save_state with the whole current state (what's done, what's next, decisions, open questions), "
                    "then end your turn with one short line. Don't start the next piece.")
        return "Recorded. Done list now:\n" + self._done_text(done)

    async def _hand_back(self, args):
        self.task.handed_back = args.get("reason", "")
        self._live("job", what="handed_back", reason=self.task.handed_back)
        self.app.ui.status(f"{self.task.id}: handed back as a quick task ({self.task.handed_back})")
        return "Handed back. End your turn now with one short line; don't do anything else."

    # ---- events -----------------------------------------------------------------------------------------
    def _on_event(self, ev):
        t = ev.get("type")
        if t == "assistant" and not ev.get("parent_tool_use_id"):
            for b in ev["message"]["content"]:
                if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                    i = b.get("input", {})
                    self.uses[b["id"]] = (i.get("subagent_type", ""), i.get("prompt", ""), i.get("description", ""))
        elif t == "system" and ev.get("subtype") == "task_started":
            kind, brief, desc = self.uses.pop(ev.get("tool_use_id"), (ev.get("subagent_type", ""), "",
                                                                       ev.get("description", "")))
            self.workers[ev.get("task_id")] = Worker(kind or ev.get("subagent_type", ""), brief, desc)
            self.app.ui.dim(f"  {self.task.id} │ ▶ {kind}: {desc}")
            self.task.digest.append(f"{kind} started: {desc[:60]}")
        elif t == "system" and ev.get("subtype") in ("task_updated", "task_notification"):
            w = self.workers.get(ev.get("task_id"))
            status = (ev.get("patch") or {}).get("status") or ev.get("status")
            if w and w.running and status in ("completed", "failed", "stopped", "killed", "error"):
                w.running, w.finished_at = False, next(ORDER)
                self.app.ui.dim(f"  {self.task.id} │ ■ {w.kind} {status}: {w.description}")
                self.task.digest.append(f"{w.kind} {status}: {w.description[:60]}")
        elif t == "result":
            self.proc_cost = ev.get("total_cost_usd") or self.proc_cost
        super()._on_event(ev)

    @property
    def spent(self):
        return self.task.spent_before + self.proc_cost + self.task.check_cost

    # ---- the rules --------------------------------------------------------------------------------------
    async def _permission(self, name, inp, req):
        t = self.task
        agent = req.get("agent_id")
        if not agent:  # the orchestrator itself
            if name in READ_TOOLS or name.split("__")[-1] in ("save_state", "mark_done", "hand_back"):
                return self._allow(inp)
            if name == "ExitPlanMode":
                return await self._plan(name, inp, req)
            if name == "AskUserQuestion" or name.endswith("restart_voice_app"):
                return await super()._permission(name, inp, req)  # the user's call, as for any task
            return self._deny(f"orchestrator tried {name}",
                              "You don't edit or run anything yourself. Hand it to a worker (builder, or a scout or "
                              "reviewer for checks).")
        w = self.workers.get(agent)
        if not w:  # a subagent whose start we missed: treat it as the most careful kind
            w = self.workers[agent] = Worker("builder", "", "unknown worker")
        building = [x for k, x in self.workers.items() if k != agent and x.kind == "builder" and x.running]
        if building:
            if w.kind == "builder":
                return self._deny(f"second builder ({w.description})",
                                  "Another builder is still working; builders run one at a time. Stop here and "
                                  "report Status: blocked, so the orchestrator can run this piece after it.")
            return self._deny(f"{w.kind} during a build ({w.description})",
                              "A builder is editing right now, so the code is half-changed. Stop and report Status: "
                              "blocked; the orchestrator will run you after the builder finishes.")
        if name in READ_TOOLS:
            return self._allow(inp)
        if name in WRITE_TOOLS and w.kind != "builder":
            return self._deny(f"{w.kind} tried to edit", "Only builders change files. Report what should change.")
        if self.spent >= t.budget:
            return self._deny("usage cap reached", "The job's usage cap is reached. Stop and report Status: blocked; "
                                                   "the user decides whether to continue.")
        verb, target, body = describe_tool(name, inp)
        if why := risk(name, inp, t.project_dir):  # hard to undo: always the user's call, as before
            return await self._ask_step(inp, verb, target, body, why)
        if name == "Bash" and read_only(inp.get("command", "")):
            decided_by("read_only")
            self.app.ui.dim(f"  {t.id} │ ✓ read-only: {w.kind} {verb} {target}")
            return self._allow(inp)
        ok, reason = await self._check(w, name, inp, verb, target)
        if ok is None:  # the checker didn't answer: ask the user instead of guessing
            return await self._ask_step(inp, verb, target, body, None)
        decided_by("step_checker")
        if not ok:
            return self._deny(f"checker refused {w.kind}: {verb} {target} — {reason}",
                              f"Step refused by the plan check: {reason}", by="step_checker")
        if name in WRITE_TOOLS:
            w.files.add(os.path.relpath(inp.get("file_path") or inp.get("notebook_path") or "", t.project_dir))
        elif name in SHELL_TOOLS:
            w.commands.append(inp.get("command", ""))
        self.app.ui.dim(f"  {t.id} │ ✓ checked: {w.kind} {verb} {target}")
        return self._allow(inp)

    async def _plan(self, name, inp, req):
        """A job's plan (or a change to it) always goes to the user, even with full approval: it's what authorises
        the workers' steps. The approved plan is what the step checker checks against."""
        t = self.task
        t.review_next_plan = True
        resp = await super()._permission(name, inp, req)
        if resp.get("behavior") == "allow":
            t.plan = inp.get("plan", "")
            self._live("job", what="plan_approved", plan=t.plan)
            with open(self._path("plan.md"), "w", encoding="utf-8") as f:
                f.write(t.plan + "\n")
        return resp

    def running_workers(self):
        return [w.kind for w in self.workers.values() if w.running]

    async def stop(self, state="stopped"):
        self.task.spent_before += self.proc_cost  # Claude Code's total is per process
        self.proc_cost = 0.0
        await super().stop(state)

    async def start(self, message=None):
        await super().start(message or self.opening(self.task.prompt))

    def _deny(self, what, message, by="rules"):
        decided_by(by)
        cvlog.log.event(f"job-{self.task.id}:deny", what)
        self.app.ui.dim(f"  {self.task.id} │ ✗ {what}")
        return {"behavior": "deny", "message": message}

    async def _check(self, w, name, inp, verb, target):
        """The step checker. → (True | False | None for no answer, reason)"""
        t = self.task
        step = json.dumps({k: (v[:3000] if isinstance(v, str) else v) for k, v in inp.items()}, indent=1)
        ask = (f"APPROVED PLAN:\n{t.plan or '(not recorded)'}\n\nDONE LIST:\n{self._done_text(self.done_list()) or 'none yet'}"
               f"\n\nWORKER: {w.kind}\nITS BRIEF:\n{w.brief or w.description}\n\nFILES IT EDITED SO FAR: "
               f"{', '.join(sorted(w.files)) or 'none'}\n\nTHE STEP: {name} ({verb} {target})\n{step}")
        cmd = [platform_util.claude_exe(), "-p", "--model", config.get()["models"]["checker"], "--output-format", "json", "--tools", "",
               "--system-prompt", config.prompt("step_check"), "--setting-sources", "", "--strict-mcp-config",
               "--no-session-persistence"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=t.project_dir, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=subscription_env({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}))
            out, _ = await asyncio.wait_for(proc.communicate(ask.encode()), CHECK_TIMEOUT_S)
            res = json.loads(out)
            t.check_cost += res.get("total_cost_usd") or 0
            m = re.search(r"\{.*\}", res.get("result", ""), re.S)
            verdict = json.loads(m.group(0)) if m else {}
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            cvlog.log.event(f"job-{t.id}:check-error", repr(e))
            return None, ""
        ok, reason = verdict.get("allow"), verdict.get("reason", "")
        cvlog.log.event(f"job-{t.id}:check", f"{w.kind} {verb} {target} -> {ok} ({reason})")
        t.digest.append(f"check {'ok' if ok else 'refused'}: {verb} {target}")
        return (bool(ok) if isinstance(ok, bool) else None), reason

    # ---- results ----------------------------------------------------------------------------------------
    async def _after_round(self, text, ok):
        t = self.task
        if self.hand_on and not self.running_workers():
            await self._hand_on()
            return True
        if t.handed_back:  # one small piece after all: do it as a quick task, under the usual rules
            reason, t.handed_back = t.handed_back, ""
            await self.close()
            tid = self.app.next_task_id()
            quick = self.app.tasks[tid] = Task(tid, {"title": t.title, "prompt": t.prompt, "route": "quick",
                                                     "project_dir": t.project_dir, "backend_model": t.model})
            quick.go_ahead = set(t.go_ahead_words)
            if quick.go_ahead:
                quick.prompt += prompt_note("go_note")
            asyncio.create_task(self.app.review_task(quick))
            self.app.front.notify(
                f"[Job {t.id} turned out to be one small piece ({reason}), so it's now quick task {tid}"
                + (", already started" if quick.go_ahead else f", waiting for the user's yes (decision {tid}-review)")
                + ".] Tell the user in a few words" + ("." if quick.go_ahead else ", and check they want you to start."))
            return True
        if note := self.budget_note():
            self.app.front.notify(note)
        return False

    async def _hand_on(self):
        """Carry the job on in a fresh orchestrator: the approved plan, the saved state and the done list, instead
        of every worker report so far. It starts outside plan mode, since the plan is already approved."""
        t = self.task
        self.hand_on = False
        was = t.context
        await self.close()
        t.session_id, t.context, t.nudged = None, 0, 0
        self._live("job", what="hand_on", tokens=was)
        t.backend = self.app.backend_for(t, carry_on=True)
        t.state = "building"
        await t.backend.start(t.backend.opening(
            f"{t.prompt}\n\n(Carrying on: the plan is approved and earlier pieces are on the done list. Start the "
            "next unfinished piece now; present a revised plan only if it has to change.)"))
        self.app.ui.status(f"{t.id}: fresh orchestrator for the next piece (the last one had {was // 1000}k tokens)")

    def budget_note(self):
        """A line for Haiku when the job passes 80% of its budget (once), or None."""
        t = self.task
        if not t.budget_warned and self.spent >= BUDGET_WARN * t.budget:
            t.budget_warned = True
            self._live("job", what="budget", spent_pct=round(100 * self.spent / t.budget))
            return (f"[Job {t.id} has used {self.spent / t.budget:.0%} of its usage cap.] Tell the user in one "
                    "sentence and ask whether to keep going; if they say yes, raise the cap with continue_task "
                    "(budget_usd, e.g. double it). It's subscription usage, not money: never mention dollars.")
        return None
