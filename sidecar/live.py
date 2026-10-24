"""The live output log of a task, and the window that follows it.

Writer side (agents.Backend, orchestrator.JobBackend) appends one JSON object per line to
STATE_DIR/live/<task-id>.jsonl as things happen; the viewer (`show_output.py --live <task-id>`) tails the same file.
That keeps working across a cv restart (a restarted task appends to the same file) and on Windows (no socket).

Event schema (version 1). Every event has:  v (1), ts (unix seconds), kind. Events produced inside a subagent also
carry  parent (the tool_use id of the Agent call that started it) and agent (its type: scout, builder, reviewer).

  round_start    prompt, queued, cwd?     the request sent to the task; queued=true when it arrived mid-round
  text           text                     one assistant text block (the claude stream gives whole blocks, not deltas)
  tool_use       id, name, summary,       a tool call. summary is short ("git diff --stat", "sidecar/audio.py");
                 input?, subagent_type?   input (the full tool input) is only given for Edit, MultiEdit, Write,
                                          NotebookEdit and TodoWrite, which the viewer draws as diffs / a checklist;
                                          subagent_type for an Agent call (scout, builder, reviewer, ...)
  tool_result    tool_use_id, text,       the result of that call. text is the full result up to RESULT_MAX
                 is_error, truncated,     characters. meta (result_meta) is what Claude Code summarises it with:
                 meta?                    Read {lines, start, total}, Grep/Glob {files, lines}, Edit/Write {patch,
                                          created}
  thinking       (none)                   the model thought before this step (its text isn't given)
  permission     tool, summary,           a permission request and its outcome. decision: approved | refused;
                 decision, by, reason?    by: user | auto | step_checker | rules (a job rule) | read_only
  subagent_start id, name, description,   a worker started (id is Claude Code's task id; parent is the Agent call).
                 parent, brief?           brief: the prompt it was given (jobs)
  progress       id, step, tokens?,       a running subagent's latest step and totals so far
                 tool_uses?, ms?
  subagent_end   id, name, status,        it finished (completed, failed, stopped, ...), with its totals when known
                 tokens?, tool_uses?, ms?
  tokens         out                      output tokens of the round so far (for the spinner)
  job            what, ...                an orchestrator milestone: plan_approved {plan}, piece_done {piece, summary,
                                          files}, state_saved, hand_on {tokens}, handed_back {reason},
                                          budget {spent_pct}
  round_end      duration_s, turns, ok,   the round's result
                 out_tokens?
  task_closed    (none)                   the task was closed; the live file goes once its window is gone

Fields marked ? and the kinds thinking, progress, tokens and job came later; a viewer draws a log without them.

The <task-id> in the file names is the task's live_key (agents.Task: id plus a per-instance uid), so a new task that
reuses an id never shares a file or window with an older one.

Windows: a window is one process whose pid is in STATE_DIR/live/<task-id>.pid. The viewer claims it at start
(claim_window) and releases it at exit (release_window); open_window starts a viewer unless that pid is alive.
Writing never raises into the task: errors are logged and dropped.
"""

import json
import os
import re
import sys
import time

import cvlog
import platform_util

VERSION = 1
RESULT_MAX = 20000  # characters of a tool result kept in the log
FULL_INPUT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit", "TodoWrite")
# kind → the fields it must have (besides v, ts, kind)
KINDS = {
    "round_start": ("prompt",),
    "text": ("text",),
    "tool_use": ("id", "name", "summary"),
    "tool_result": ("tool_use_id", "text", "is_error"),
    "permission": ("tool", "decision", "by"),
    "subagent_start": ("id", "name"),
    "subagent_end": ("id", "name", "status"),
    "thinking": (),
    "progress": ("id", "step"),
    "tokens": ("out",),
    "job": ("what",),
    "round_end": ("duration_s", "turns"),
    "task_closed": (),
}
DECISIONS = ("approved", "refused")
DECIDERS = ("user", "auto", "step_checker", "rules", "read_only")


# ---- schema ------------------------------------------------------------------------------------------------------
def event(kind, **fields):
    """A new event dict. Fields that are None are left out."""
    return {"v": VERSION, "ts": round(time.time(), 3), "kind": kind, **{k: v for k, v in fields.items() if v is not None}}


def problems(ev):
    """What's wrong with an event (an empty list when it's valid)."""
    if not isinstance(ev, dict):
        return ["not an object"]
    kind = ev.get("kind")
    if kind not in KINDS:
        return [f"unknown kind {kind!r}"]
    out = [f"{kind}: missing {f}" for f in KINDS[kind] if f not in ev]
    if kind == "permission":
        if ev.get("decision") not in DECISIONS:
            out.append(f"permission: bad decision {ev.get('decision')!r}")
        if ev.get("by") not in DECIDERS:
            out.append(f"permission: bad by {ev.get('by')!r}")
    return out


def summarize(name, inp, limit=200):
    """A short Claude-Code-style description of a tool call's input: the command, the file, the pattern."""
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Grep", "Glob") and inp.get("pattern"):  # Search(pattern: "x", path: "y"), as Claude Code shows it
        text = f'pattern: "{inp["pattern"]}"' + (f', path: "{inp["path"]}"' if inp.get("path") else "")
        return " ".join(text.split())[:limit]
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(key):
            text = str(inp[key])
            break
    else:
        text = json.dumps(inp, ensure_ascii=False) if inp else ""
    return " ".join(text.split())[:limit]


PATCH_LINES = 200  # lines of a structuredPatch kept in a tool_result's meta


def result_meta(name, r):
    """The parts of Claude Code's tool_use_result its own transcript summarises a result with, or None."""
    if not isinstance(r, dict):
        return None
    if name == "Read" and isinstance(r.get("file"), dict):
        f = r["file"]
        meta = {"lines": f.get("numLines"), "start": f.get("startLine"), "total": f.get("totalLines")}
    elif name in ("Grep", "Glob"):
        meta = {"files": r.get("numFiles"), "lines": r.get("numLines")}
    elif name in ("Edit", "MultiEdit", "Write") and isinstance(r.get("structuredPatch"), list):
        hunks, left = [], PATCH_LINES
        for h in r["structuredPatch"]:
            if left <= 0 or not isinstance(h, dict):
                break
            lines = [str(x) for x in h.get("lines") or []][:left]
            left -= len(lines)
            hunks.append({"old": h.get("oldStart"), "new": h.get("newStart"), "lines": lines})
        meta = {"patch": hunks, "created": True if r.get("type") == "create" else None}
        if r.get("type") == "create" and not hunks and isinstance(r.get("content"), str):  # a new file has no hunks
            lines = r["content"].splitlines()
            meta["patch"] = [{"old": 0, "new": 1, "lines": ["+" + x for x in lines[:PATCH_LINES]]}]
            meta["added"] = len(lines)
    else:
        return None
    meta = {k: v for k, v in meta.items() if v is not None}
    return meta or None


# ---- files -------------------------------------------------------------------------------------------------------
def live_dir():
    return os.path.join(platform_util.state_dir(), "live")


def _safe(task_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))


def path_for(task_id):
    return os.path.join(live_dir(), _safe(task_id) + ".jsonl")


def pid_path(task_id):
    return os.path.join(live_dir(), _safe(task_id) + ".pid")


def _log_error(what, e):
    try:
        cvlog.log.event("live:error", f"{what}: {e!r}")
    except Exception:  # noqa: BLE001 - even the log failed
        pass


def append(task_id, ev):
    """Append one event as a line of the task's file. → True if written. Never raises."""
    try:
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
        os.makedirs(live_dir(), exist_ok=True)
        with open(path_for(task_id), "ab") as f:  # append mode: a restarted cv carries on in the same file
            f.write(line.encode("utf-8"))
            f.flush()
        return True
    except Exception as e:  # noqa: BLE001 - the task must not notice
        _log_error(f"append {task_id}", e)
        return False


def emit(task_id, kind, **fields):
    """append(task_id, event(kind, **fields)), safe even if building the event fails. → True if written."""
    try:
        return append(task_id, event(kind, **fields))
    except Exception as e:  # noqa: BLE001
        _log_error(f"emit {kind}", e)
        return False


def _scan(task_id, offset):
    """-> ([(event, offset after it)], offset after the last complete line). See read_from."""
    try:
        with open(path_for(task_id), "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < offset:
                offset = 0
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    out, pos = [], 0
    while (nl := data.find(b"\n", pos)) >= 0:  # only complete lines; a half-written last one waits
        raw, pos = data[pos:nl], nl + 1
        try:
            ev = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append((ev, offset + pos))
    return out, offset + pos


def read_from(task_id, offset=0):
    """The complete events after byte `offset` -> (events, new_offset). A half-written last line is left for the next
    call; unreadable lines are skipped. If the file is shorter than offset (it was reset), reading restarts at 0, which
    the caller sees as new_offset < offset."""
    pairs, end = _scan(task_id, offset)
    return [ev for ev, _ in pairs], end


def iter_from(task_id, offset=0):
    """Yield (event, byte offset after it) for the complete events from `offset` on."""
    yield from _scan(task_id, offset)[0]


def read_all(task_id):
    return read_from(task_id, 0)[0]


def remove(task_id):
    """Delete the task's log file (and a stale pid file)."""
    for p in (path_for(task_id), pid_path(task_id)):
        try:
            os.remove(p)
        except OSError:
            pass


def is_closed(task_id):
    """Does the log end with task_closed?"""
    events = read_all(task_id)
    return bool(events) and events[-1].get("kind") == "task_closed"


def close_task(task_id):
    """The task is closed: record it (once), and delete the file if no window is following it."""
    if not os.path.exists(path_for(task_id)):
        return
    if not is_closed(task_id):
        emit(task_id, "task_closed")
    if not window_alive(task_id):
        remove(task_id)


def prune(keep_ids):
    """Delete log files of tasks that no longer exist (not in keep_ids) and have no window. Ids can be reused by the
    next launch, so a stale file must not live on."""
    keep = {_safe(i) for i in keep_ids}
    try:
        names = os.listdir(live_dir())
    except OSError:
        return
    for n in names:
        if n.endswith(".jsonl") and n[:-6] not in keep and not window_alive(n[:-6]):
            remove(n[:-6])


# ---- windows -----------------------------------------------------------------------------------------------------
PLACEHOLDER_S = 15  # how long the terminal launcher's pid stands in for a viewer that hasn't claimed the window yet


def _now():
    return time.time()


def _boot_time():
    """When the machine booted (unix seconds), or 0 if that isn't cheap to find (only Linux's /proc/stat is tried)."""
    try:
        with open("/proc/stat", encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _read_pid(task_id):
    """-> (pid, written at, is_placeholder); pid 0 if there is no usable pid file."""
    try:
        with open(pid_path(task_id), encoding="utf-8") as f:
            lines = f.read().split()
        pid, ts = int(lines[0]), (float(lines[1]) if len(lines) > 1 else 0.0)
        return pid, ts, "placeholder" in lines[2:]
    except (OSError, ValueError, IndexError):
        return 0, 0.0, False


def _write_pid(task_id, pid, placeholder=False):
    os.makedirs(live_dir(), exist_ok=True)
    with open(pid_path(task_id), "w", encoding="utf-8") as f:
        f.write(f"{pid}\n{_now():.3f}\n{'placeholder' if placeholder else 'viewer'}\n")


def window_alive(task_id):
    """Is a viewer for this task running? A placeholder pid (the terminal launcher's, which may exit at once, as
    gnome-terminal and wt.exe do) counts as alive for PLACEHOLDER_S seconds. Limitation: a pid can be reused by an
    unrelated process after the viewer dies; a pid file older than the last boot is ignored (Linux), nothing more."""
    pid, ts, placeholder = _read_pid(task_id)
    if not pid:
        return False
    if placeholder and _now() - ts < PLACEHOLDER_S:
        return True
    if ts < _boot_time():
        return False
    return platform_util.pid_alive(pid)


def claim_window(task_id):
    """Called by the viewer at start: this process is the task's window."""
    _write_pid(task_id, os.getpid())


def release_window(task_id):
    """Called by the viewer at exit: drop the pid file (only if it is still ours), and the log too when the task has
    been closed."""
    if _read_pid(task_id)[0] in (os.getpid(), 0) or not window_alive(task_id):
        try:
            os.remove(pid_path(task_id))
        except OSError:
            pass
    if is_closed(task_id):
        remove(task_id)


def viewer_command(task_id):
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "show_output.py"), "--live", task_id]


def file_command(task_id, file):
    """The viewer for a saved output file; it claims the window under file_key(task_id)."""
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "show_output.py"),
            "--claim", file_key(task_id), file]


def file_key(task_id):
    return f"{task_id}.out"


def has_events(task_id):
    try:
        return os.path.getsize(path_for(task_id)) > 0
    except OSError:
        return False


def open_window(task_id, force=False, title=None, cwd=None, file=None):
    """Open the task's live output window unless it is already open (then leave it, on Windows brought to the front
    by its title). If it was closed, a new one
    replays the whole file. With file= (a saved output, for a task that has no live log) that Markdown file is shown
    instead, still one window per task. force=True opens another one regardless. → True if a window was started,
    False if one was already open. Raises OSError if no window could be started."""
    key = file_key(task_id) if file else task_id
    if not force and window_alive(key):
        platform_util.focus_window(title)  # Windows: bring it to the front (elsewhere a no-op)
        return False
    argv = file_command(task_id, file) if file else viewer_command(task_id)
    started = _now()
    proc = platform_util.spawn_terminal(argv, "claude-voice-output", title or f"cv · {task_id}", cwd=cwd)
    pid = getattr(proc, "pid", 0)
    if pid:  # until the viewer claims the window itself, the terminal's pid stands in, so a second ask doesn't duplicate
        try:
            held, ts, placeholder = _read_pid(key)
            if held and not placeholder and ts >= started:
                return True  # the viewer was quicker and has claimed the window already: keep its real pid
            _write_pid(key, pid, placeholder=True)
        except OSError as e:
            _log_error("pid file", e)
    return True


def follow_plain(task_id, out=None, stop=lambda: False, poll_s=0.3):
    """A bare-bones viewer: print each event as a line and keep following until stop() (or Ctrl-C). The real rendering
    lives in show_output.py."""
    out = out or sys.stdout
    offset = 0
    claim_window(task_id)
    try:
        while True:
            events, offset = read_from(task_id, offset)
            for ev in events:
                body = ev.get("text") or ev.get("summary") or ev.get("prompt") or ev.get("decision") or ""
                print(f"{ev.get('kind', '?'):<14} {str(body).splitlines()[0] if body else ''}", file=out, flush=True)
                if ev.get("kind") == "task_closed":
                    print("task closed", file=out, flush=True)
            if stop():
                return
            time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
    finally:
        release_window(task_id)
