"""The live output window: follows STATE_DIR/live/<key>.jsonl (live.py) like `tail -f` and draws it the way a Claude
Code terminal does (● tool lines, ⎿ results, numbered diffs, subagents with their last steps, the ✻ spinner), in cv's
look: the wallpaper palette, the orb and gradient title, and cv's Markdown / table / diagram renderers.

Run by `show_output.py --live <key>`. The window claims itself (live.claim_window) and lets go at exit.

New lines are appended as widgets; nothing already drawn is redrawn, unless the file was reset (it got shorter), in
which case everything is drawn again from the top.

Jobs: each worker (scout, builder, reviewer) is a live block with its last steps and the plan check's verdicts; the
orchestrator's milestones (plan approved, piece done, fresh orchestrator) are lines of their own, and a panel above the
footer lists the workers still running.

Keys (on top of the output window's): e expand / collapse the result or worker under the cursor (the one nearest the
middle of the window, marked with a bright ⎿ / ●) · enter open that worker on its own (esc goes back) · f follow new
output on/off · G / End jump to the end and follow · scrolling up stops following · q close
"""

import difflib
import os
import time

from rich.console import Group
from rich.style import Style
from rich.text import Text
from markdown_it import MarkdownIt
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Static

import cvlog
import live
import show_output
from show_output import MiniOrb, OutputApp, Renderer, block_widget, gradient, tree

PREVIEW_LINES = 3  # lines of a tool's output shown before it is expanded
EXPANDED_LINES = 400
DIFF_LINES = 30
PROMPT_LINES = 12
RECENT_STEPS = 3  # a running worker's last steps shown under it
QUIET_TOOLS = ("TodoWrite",)  # its result is the checklist itself
FILE_TOOLS = ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "NotebookRead")
AGENT_TOOLS = ("Agent", "Task")
DISPLAY = {"Edit": "Update", "MultiEdit": "Update", "Grep": "Search", "Glob": "Search", "WebFetch": "Fetch",
           "WebSearch": "Web Search", "TodoWrite": "Update Todos", "NotebookEdit": "Edit Notebook"}
SPIN = "·✢✳✶✻✽✻✶✳✢"  # Claude Code's spinner, there and back
WORKER_SPIN = "◐◓◑◒"
VERBS = ("Pondering", "Cogitating", "Brewing", "Noodling", "Percolating", "Ruminating", "Simmering", "Thinking",
         "Working", "Crafting", "Mulling", "Tinkering")
APPROVED = {"user": "approved by you", "auto": "auto-approved", "step_checker": "checked by the plan check",
            "read_only": "read-only"}
REFUSED = {"user": "refused by you", "auto": "refused automatically", "step_checker": "refused by the plan check",
           "rules": "blocked by the job rules"}
ENDED_OK = ("completed", "done")


# ---- formatting --------------------------------------------------------------------------------------------------
def fmt_duration(s):
    s = float(s or 0)
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s"


def fmt_tokens(n):
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def short_path(path, cwd=None):
    """A path as Claude Code shows it: relative to the project, else with ~ for home."""
    path = str(path or "")
    if cwd and (path[:1] in ("/", "\\") or os.path.splitdrive(path)[0]):  # rooted (isabs varies on Windows)
        try:
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".."):
                return rel.replace(os.sep, "/") if "/" in path and "\\" not in path else rel  # keep its own slashes
        except ValueError:  # another drive (Windows)
            pass
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home and home != "~" and path.startswith(home + os.sep) else path


def display(ev, cwd=None):
    """A tool call as Claude Code names it → (name, args); args is None when it shows none."""
    name, summary = str(ev.get("name", "?")), str(ev.get("summary", ""))
    if name in AGENT_TOOLS:
        return str(ev.get("subagent_type") or "agent"), summary
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"{parts[1] if len(parts) > 2 else ''} - {parts[-1]} (MCP)".lstrip(" -"), summary
    if name == "TodoWrite":
        return DISPLAY[name], None
    if name in FILE_TOOLS:
        summary = short_path(summary, cwd)
    if name in ("Grep", "Glob") and ', path: "' in summary:
        head, path = summary.rsplit(', path: "', 1)
        summary = f'{head}, path: "{short_path(path.rstrip(chr(34)), cwd)}"'
    if name == "WebSearch":
        summary = f'"{summary}"'
    return DISPLAY.get(name, name), summary


def tool_label(ev, cwd=None):
    name, args = display(ev, cwd)
    return name if args is None else f"{name}({args})"


def lines_of(text):
    return (text or "").rstrip("\n").split("\n") if (text or "").strip() else []


# ---- pure renderers: event → renderable ------------------------------------------------------------------------
def render_prompt(ev, pal):
    lines = str(ev.get("prompt", "")).strip().split("\n")
    more = len(lines) - PROMPT_LINES
    out = Text()
    out.append("> ", style=f"bold {pal.hex('dim')}")
    out.append("\n  ".join(lines[:PROMPT_LINES]), style=pal.hex("foreground"))
    if more > 0:
        out.append(f"\n  … +{more} lines", style=pal.hex("dim"))
    if ev.get("queued"):
        out.append("  (queued)", style=pal.hex("dim"))
    return out


def bullet_colour(state):
    """The ● of a tool line or worker: running (blinks, dim), ok, error, or ended without a result."""
    return {"ok": "accent3", "error": "error"}.get(state, "dim")


def render_tool_line(ev, pal, state="ok", verdict=None, cwd=None, cursor=False, blink_on=True):
    out = Text(no_wrap=True, overflow="ellipsis")
    dot = "●" if state != "running" or blink_on else " "
    out.append(dot + " ", style=pal.hex("accent" if cursor else bullet_colour(state)))
    name, args = display(ev, cwd)
    out.append(name, style=f"bold {pal.hex('foreground')}")
    if args is not None:
        out.append(f"({args})", style=pal.hex("foreground"))
    if verdict and verdict.get("decision") == "approved":
        out.append(f"  ✓ {APPROVED.get(verdict.get('by'), 'approved')}", style=pal.hex("dim"))
    return out


def render_permission(ev, pal, cwd=None):
    """A permission outcome on a line of its own (when no tool line shows it)."""
    ok = ev.get("decision") == "approved"
    by = ev.get("by")
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append("● " if not ok else "  ✓ ", style=pal.hex("accent3" if ok else "error"))
    words = APPROVED.get(by, "approved") if ok else REFUSED.get(by, "refused")
    out.append(words, style=pal.hex("foreground" if ok else "error"))
    out.append(f" · {tool_label({'name': ev.get('tool'), 'summary': ev.get('summary', '')}, cwd)}", style=pal.hex("dim"))
    if ev.get("reason"):
        out.append(f" · {ev['reason']}", style=pal.hex("dim"))
    return out


def refusal_line(verdict, pal):
    by = verdict.get("by")
    out = Text(REFUSED.get(by, "refused"), style=pal.hex("error"))
    if verdict.get("reason"):
        out.append(f" · {verdict['reason']}", style=pal.hex("dim"))
    return out


class DiffRow:
    """One numbered diff line, its background drawn across the whole width (Claude Code's look)."""

    def __init__(self, num, sign, text, width, pal):
        self.num, self.sign, self.text, self.width, self.pal = num, sign, text, width, pal

    def __rich_console__(self, console, options):
        p = self.pal
        body = f"{'' if self.num is None else self.num:>{self.width}} {self.sign} {self.text}"
        pad = max(options.max_width - 5 - len(body), 0)
        out = Text("     ", no_wrap=True, overflow="ellipsis")
        if self.sign == " ":
            out.append(body, style=p.hex("dim"))
        else:
            role = "accent3" if self.sign == "+" else "error"
            out.append(body + " " * pad, style=Style(color=p.hex("foreground"), bgcolor=p.tint(role, 0.2)))
        yield out


def patch_rows(hunks):
    """structuredPatch hunks → [(line number, sign, text)], with … between hunks."""
    rows = []
    for i, h in enumerate(hunks or []):
        if i:
            rows.append((None, " ", "…"))
        old, new = int(h.get("old") or 0), int(h.get("new") or 0)
        for line in h.get("lines") or []:
            sign, text = (line[:1] or " "), line[1:]
            if sign == "-":
                rows.append((old, "-", text))
                old += 1
            elif sign == "+":
                rows.append((new, "+", text))
                new += 1
            elif sign == "\\":  # "\ No newline at end of file"
                continue
            else:
                rows.append((new, " ", text))
                old, new = old + 1, new + 1
    return rows


def diff_changes(name, inp):
    """Edit / MultiEdit / Write / NotebookEdit input → [(sign, line)] of what changed, or None (old logs: no patch)."""
    if not isinstance(inp, dict):
        return None
    pairs = []
    if name == "Edit":
        pairs = [(inp.get("old_string", ""), inp.get("new_string", ""))]
    elif name == "MultiEdit":
        pairs = [(e.get("old_string", ""), e.get("new_string", "")) for e in inp.get("edits") or [] if isinstance(e, dict)]
    elif name == "Write":
        pairs = [("", inp.get("content", ""))]
    elif name == "NotebookEdit":
        pairs = [("", inp.get("new_source", ""))]
    else:
        return None
    out = []
    for old, new in pairs:
        a, b = str(old).splitlines(), str(new).splitlines()
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            out += [("-", x) for x in a[i1:i2]] + [("+", x) for x in b[j1:j2]]
    return out


def diff_summary(name, rows, path, created=False):
    added, removed = sum(r[1] == "+" for r in rows), sum(r[1] == "-" for r in rows)
    if name == "Write" and (created or not removed):
        return f"Wrote {added} line{'s' * (added != 1)} to {path}"
    return (f"Updated {path} with {added} addition{'s' * (added != 1)} and {removed} "
            f"removal{'s' * (removed != 1)}")


def result_summary(name, text, meta):
    """Claude Code's one-line summary of a result (Read 48 lines, Found 5 files), or None to show the output."""
    meta = meta or {}
    if name == "Read":
        n = meta.get("lines") or len(lines_of(text))
        return f"Read {n} line{'s' * (n != 1)}"
    if name in ("Grep", "Glob"):
        if meta.get("files") is not None and not meta.get("lines"):
            n = meta["files"]
            return f"Found {n} file{'s' * (n != 1)}"
        if meta.get("lines") is not None:
            n = meta["lines"]
            return f"Found {n} line{'s' * (n != 1)}"
    return None


def render_todos(inp, pal):
    todos = inp.get("todos") if isinstance(inp, dict) else None
    if not isinstance(todos, list):
        return None
    out = Text()
    for i, t in enumerate(x for x in todos if isinstance(x, dict)):
        done, active = t.get("status") == "completed", t.get("status") == "in_progress"
        out.append(("\n     " if i else "  ⎿  "), style=pal.hex("dim"))
        out.append("☒ " if done else "☐ ", style=pal.hex("accent3" if done else "accent" if active else "dim"))
        style = Style(color=pal.hex("dim"), strike=True) if done else Style(
            color=pal.hex("foreground"), bold=active or None)
        out.append(str(t.get("content") or t.get("activeForm") or ""), style=style)
    return out if todos else None


class TextRule:
    """A thin rule with a label, drawn at whatever width it is given."""

    def __init__(self, label, colour, char="─"):
        self.label, self.colour, self.char = label, colour, char

    def __rich_console__(self, console, options):
        head = f"{self.char * 2} {self.label} " if self.label else ""
        yield Text(head + self.char * max(options.max_width - len(head), 0), style=self.colour, no_wrap=True,
                   overflow="crop")


def render_round_end(ev, pal):
    ok = ev.get("ok", True)
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append("✻ ", style=pal.hex("accent" if ok else "error"))
    out.append(("Worked for " if ok else "Failed after ") + fmt_duration(ev.get("duration_s")),
               style=pal.hex("dim" if ok else "error"))
    if ev.get("out_tokens"):
        out.append(f" · {fmt_tokens(ev['out_tokens'])} tokens", style=pal.hex("dim"))
    return out


def render_thinking(pal):
    return Text("✻ Thinking…", style=Style(color=pal.hex("dim"), italic=True))


def spinner_line(verb, elapsed, tokens, step, pal, now=None):
    now = time.monotonic() if now is None else now
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append(SPIN[int(now * 8) % len(SPIN)] + " ", style=pal.hex("accent"))
    out.append(f"{verb}… ", style=f"bold {pal.hex('accent')}")
    extra = fmt_duration(max(elapsed, 0)) + (f" · ↓ {fmt_tokens(tokens)} tokens" if tokens else "")
    out.append(f"({extra})", style=pal.hex("dim"))
    if step:
        out.append(f" · {step}", style=pal.hex("dim"))
    return out


def verb_for(ts):
    return VERBS[int(float(ts or 0)) % len(VERBS)]


def render_job(ev, pal):
    """An orchestrator milestone as a ● line (piece_done also gets its summary under ⎿)."""
    what = ev.get("what")
    out = Text()
    if what == "piece_done":
        out.append("● ", style=pal.hex("accent3"))
        out.append("Piece done", style=f"bold {pal.hex('foreground')}")
        out.append(f" · {ev.get('piece', '')}", style=pal.hex("foreground"))
        files = ev.get("files") or []
        if files:
            out.append(f" ({', '.join(map(str, files[:6]))}{', …' if len(files) > 6 else ''})", style=pal.hex("dim"))
        if ev.get("summary"):
            out.append("\n  ⎿  ", style=pal.hex("dim"))
            out.append(str(ev["summary"]).split("\n")[0], style=pal.hex("dim"))
    elif what == "state_saved":
        out.append("  ✓ state saved", style=pal.hex("dim"))
    elif what == "handed_back":
        out.append("● ", style=pal.hex("accent2"))
        out.append("Handed back as a quick task", style=f"bold {pal.hex('foreground')}")
        if ev.get("reason"):
            out.append(f" · {ev['reason']}", style=pal.hex("dim"))
    elif what == "budget":
        out.append("● ", style=pal.hex("error"))
        out.append(f"{ev.get('spent_pct', '?')}% of the usage cap used", style=pal.hex("foreground"))
    else:
        out.append(f"● {what}", style=pal.hex("dim"))
    return out


# ---- widgets ---------------------------------------------------------------------------------------------------
class Line(Static):
    DEFAULT_CSS = "Line { height: auto; }"


class Entry(Horizontal):
    """A ● marker with the Markdown blocks of one piece of assistant text beside it."""

    DEFAULT_CSS = """
    Entry { height: auto; }
    Entry > .mark { width: 2; height: 1; }
    Entry > Vertical { width: 1fr; height: auto; }
    Entry Block, Entry Wide, Entry Callout { margin: 0 0 1 0; }
    Entry Vertical > *:last-child { margin: 0; }
    """

    def __init__(self, widgets, pal, mark="●", **kw):
        super().__init__(**kw)
        self.widgets, self.pal, self.mark = widgets, pal, mark

    def compose(self):
        yield Static(Text(self.mark, style=self.pal.hex("foreground")), classes="mark")
        with Vertical():
            yield from self.widgets


class Fold(Static):
    """⎿ and a few rows that `e` unfolds: a head line (a summary) and/or body rows (output, diff lines)."""

    DEFAULT_CSS = "Fold { height: auto; }"

    def __init__(self, pal, head=None, rows=(), preview=PREVIEW_LINES, colour="dim", text="", **kw):
        super().__init__(**kw)
        self.pal, self.head, self.rows, self.preview, self.colour = pal, head, list(rows), preview, colour
        self.text = text  # for y (copy)
        self.expanded = self.cursor = False
        self.paint()

    @property
    def expandable(self):
        return len(self.rows) > self.preview

    def paint(self):
        p = self.pal
        gutter = p.hex("accent") if self.cursor else p.hex("dim")
        limit = EXPANDED_LINES if self.expanded else self.preview
        parts, first = [], True
        folded = self.preview == 0 and self.rows  # a summary that unfolds into the output (Read 48 lines)
        if self.head is not None:
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append("  ⎿  ", style=gutter)
            t.append_text(self.head if isinstance(self.head, Text) else Text(str(self.head), style=p.hex(self.colour)))
            if folded:
                t.append(" (e to collapse)" if self.expanded else " (e to expand)", style=p.hex("dim"))
            parts.append(t)
            first = False
        for row in self.rows[:limit]:
            if isinstance(row, DiffRow):
                parts.append(row)
                continue
            t = Text(no_wrap=not self.expanded, overflow="fold" if self.expanded else "ellipsis")
            t.append("  ⎿  " if first else "     ", style=gutter)
            t.append_text(row if isinstance(row, Text) else Text(str(row), style=p.hex(self.colour)))
            parts.append(t)
            first = False
        hidden = len(self.rows) - limit
        if folded and self.head is not None:
            pass
        elif hidden > 0:
            parts.append(Text(f"     … +{hidden} lines (e to expand)", style=p.hex("dim")))
        elif self.expanded and len(self.rows) > self.preview:
            parts.append(Text("     (e to collapse)", style=p.hex("dim")))
        self.update(Group(*parts))

    def toggle(self):
        if self.expandable:
            self.expanded = not self.expanded
            self.paint()

    def set_cursor(self, on):
        if on != self.cursor:
            self.cursor = on
            self.paint()


class ToolResult(Fold):
    """A tool's output: ⎿ and its first lines (errors in the error colour)."""

    def __init__(self, text, is_error, pal, head=None, preview=PREVIEW_LINES, **kw):
        rows = lines_of(text)
        if is_error and rows and head is None and not rows[0].lower().startswith("error"):
            rows[0] = "Error: " + rows[0]
        if not rows and head is None:
            rows = ["(No content)"]
        self.is_error = is_error
        super().__init__(pal, head=head, rows=rows, preview=preview, colour="error" if is_error else "dim", text=text,
                         **kw)


class ToolBlock(Vertical):
    """● Name(args), then what came of it: a verdict, the result, a diff, a checklist."""

    DEFAULT_CSS = """
    ToolBlock { height: auto; }
    ToolBlock > .head { height: auto; }
    """

    def __init__(self, ev, pal, cwd=None, **kw):
        super().__init__(**kw)
        self.ev, self.pal, self.cwd = ev, pal, cwd
        self.state = "running"
        self.verdict = None
        self.head = Static(classes="head")
        self.paint()

    def compose(self):
        yield self.head

    def paint(self, blink_on=True):
        self.head.update(render_tool_line(self.ev, self.pal, self.state, self.verdict, self.cwd, blink_on=blink_on))

    def finish(self, state):
        if self.state == "running":
            self.state = state
            self.paint()


class SubAgent(Vertical):
    """A worker, as Claude Code shows one: ● kind(description), its last steps while it runs, then Done (…).
    e unfolds its whole step tree in place; enter opens it on its own."""

    DEFAULT_CSS = """
    SubAgent { height: auto; }
    SubAgent > .head { height: auto; }
    SubAgent > .steps { height: auto; margin: 0 0 0 2; padding: 0 0 0 1; }
    SubAgent > .preview { height: auto; }
    """

    def __init__(self, ev, pal, cwd=None, **kw):
        super().__init__(**kw)
        self.pal, self.cwd = pal, cwd
        self.use = ev.get("id")  # the Agent call's tool_use id: its nested events carry it as parent
        self.agent = str(ev.get("subagent_type") or "agent")
        self.description = str(ev.get("summary") or "")
        self.brief, self.report, self.verdict = "", None, None
        self.status = None
        self.t0 = float(ev.get("ts") or time.time())
        self.tokens = self.tool_uses = self.ms = None
        self.recent = []  # [label, verdict or None] of its tool calls
        self.progress = []  # step descriptions from progress events (background workers)
        self.events = []  # every event of its own, for the focus view
        self.expanded = self.cursor = False
        self.head = Static(classes="head")
        self.steps = Vertical(classes="steps")
        self.steps.styles.border_left = ("outer", pal.tint("accent2", 0.5))
        self.preview = Static(classes="preview")
        self.paint()

    def compose(self):
        yield self.head
        yield self.steps
        yield self.preview

    @property
    def expandable(self):
        return bool(self.steps.children) or bool(self.recent)

    @property
    def running(self):
        return self.status is None

    def started(self, ev):
        self.agent = str(ev.get("name") or self.agent)
        self.description = str(ev.get("description") or self.description)
        self.brief = str(ev.get("brief") or self.brief)
        self.paint()

    def step(self, label):
        self.recent.append([label, None])
        self.paint()

    def judged(self, label, verdict):
        for item in reversed(self.recent):
            if item[0] == label and item[1] is None:
                item[1] = verdict
                break
        else:
            self.recent.append([label, verdict])
        self.paint()

    def progressed(self, ev):
        for k in ("tokens", "tool_uses", "ms"):
            if ev.get(k) is not None:
                setattr(self, k, ev[k])
        s = str(ev.get("step") or "")
        if s and (not self.progress or self.progress[-1] != s):
            self.progress.append(s)
        self.paint()

    def refused(self, verdict):
        self.verdict = verdict
        self.finish({"status": "blocked"})

    def finish(self, ev):
        self.status = str(ev.get("status") or "completed")
        for k in ("tokens", "tool_uses", "ms"):
            if ev.get(k) is not None:
                setattr(self, k, ev[k])
        self.paint()

    def totals(self, live_time=True):
        bits = []
        uses = self.tool_uses if self.tool_uses is not None else len(self.recent) or None
        if uses is not None:
            bits.append(f"{uses} tool use{'s' * (uses != 1)}")
        if self.tokens:
            bits.append(f"{fmt_tokens(self.tokens)} tokens")
        secs = self.ms / 1000 if self.ms else (time.time() - self.t0 if live_time and self.running else None)
        if secs:
            bits.append(fmt_duration(secs))
        return " · ".join(bits)

    def paint(self, blink_on=True):
        p = self.pal
        state = "running" if self.running else ("ok" if self.status in ENDED_OK else "error")
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append("●" if state != "running" or blink_on else " ",
                    style=p.hex("accent" if self.cursor else bullet_colour(state)))
        head.append(" ")
        head.append(self.agent, style=f"bold {p.hex('foreground')}")
        if self.description:
            head.append(f"({self.description})", style=p.hex("foreground"))
        self.head.update(head)
        self.steps.display = self.expanded
        rows = []
        gutter = p.hex("accent") if self.cursor else p.hex("dim")
        if self.running and not self.expanded:
            if self.recent:
                for label, verdict in self.recent[-RECENT_STEPS:]:
                    t = Text(label, style=p.hex("foreground"), no_wrap=True, overflow="ellipsis")
                    if verdict and verdict.get("decision") == "approved":
                        t.append(f"  ✓ {APPROVED.get(verdict.get('by'), 'approved')}", style=p.hex("dim"))
                    rows.append(t)
                    if verdict and verdict.get("decision") == "refused":
                        rows.append(Text("  ").append_text(refusal_line(verdict, p)))
            else:
                rows += [Text(s, style=p.hex("foreground")) for s in self.progress[-RECENT_STEPS:]]
            shown = min(len(self.recent), RECENT_STEPS)
            uses = self.tool_uses if self.tool_uses is not None else len(self.recent)
            more = []
            if uses - shown > 0:
                more.append(f"+{uses - shown} more tool use{'s' * (uses - shown != 1)}")
            if self.tokens:
                more.append(f"{fmt_tokens(self.tokens)} tokens")
            more.append(fmt_duration(time.time() - self.t0))
            rows.append(Text(" · ".join(more), style=p.hex("dim")))
            if not self.recent and not self.progress:
                rows.insert(0, Text("Running in the background…" if not self.expanded else "", style=p.hex("dim")))
        elif self.running:
            rows.append(Text(self.totals() or "running…", style=p.hex("dim")))
        else:
            ok = self.status in ENDED_OK
            word = "Done" if ok else self.status.capitalize()
            tot = self.totals(live_time=False)
            if self.verdict is not None:
                rows.append(refusal_line(self.verdict, p))
            else:
                rows.append(Text(word + (f" ({tot})" if tot else ""), style=p.hex("dim" if ok else "error")))
        out = []
        for i, r in enumerate(rows):
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append("  ⎿  " if i == 0 else "     ", style=gutter)
            t.append_text(r)
            out.append(t)
        self.preview.update(Group(*out))

    def toggle(self):
        if self.expandable:
            self.expanded = not self.expanded
            self.paint()

    def set_cursor(self, on):
        if on != self.cursor:
            self.cursor = on
            self.paint()


# ---- a stream of events drawn into one container ---------------------------------------------------------------
class Flow:
    """Draws events into `root`: the main body, or the focus view of one worker. Keeps the bookkeeping of what is
    where (tool blocks by id, workers by their Agent call), so two flows never mix."""

    def __init__(self, app, root):
        self.app, self.root = app, root
        self.tools = {}  # tool_use id → ToolBlock / SubAgent
        self.subs = {}  # an Agent call's tool_use id → its SubAgent
        self.by_id = {}  # a subagent's own task id → SubAgent
        self.expandables = []
        self.pending_perms = []  # permissions that came before their tool call

    @property
    def pal(self):
        return self.app.pal

    def where(self, ev):
        sub = self.subs.get(ev.get("parent")) if ev.get("parent") else None
        return sub.steps if sub else self.root

    async def put(self, container, *widgets):
        for w in widgets:
            if container is self.root:
                w.add_class("item")
        await container.mount(*widgets)
        for w in widgets:
            if hasattr(w, "toggle"):
                self.expandables.append(w)

    def workers(self):
        return [s for s in self.subs.values() if s.running]

    def blocks(self):
        return [w for w in self.tools.values() if isinstance(w, ToolBlock) and w.state == "running"]

    def end_round(self):
        """The round is over: its own unanswered calls ended (a background worker's go on)."""
        for b in self.blocks():
            if not b.ev.get("parent"):
                b.finish("ended")

    async def apply(self, ev):
        kind, p, cwd = ev.get("kind"), self.pal, self.app.cwd
        own = kind in ("subagent_start", "progress", "subagent_end")  # a worker's lifecycle: drawn on its block
        sub = self.subs.get(ev.get("parent")) if ev.get("parent") and not own else None
        if sub is not None:
            sub.events.append(ev)
            if self.app.focus_flow is not None and self.app.focus_sub is sub and self is self.app.flow:
                await self.app.focus_flow.apply({k: v for k, v in ev.items() if k != "parent"})
        c = self.where(ev)
        if kind == "round_start":
            w = Line(render_prompt(ev, p))
            w.styles.background = p.tint("foreground", 0.07)
            await self.put(c, w)
        elif kind == "text":
            await self.apply_text(c, str(ev.get("text", "")))
        elif kind == "thinking":
            await self.put(c, Line(render_thinking(p)))
        elif kind == "tool_use":
            await self.tool_use(c, ev, sub)
        elif kind == "tool_result":
            await self.tool_result(ev)
        elif kind == "permission":
            await self.permission(c, ev, sub)
        elif kind == "subagent_start":
            s = self.subs.get(ev.get("parent"))
            if s is None:  # its Agent call wasn't logged (an old log): a block of its own
                s = SubAgent({"id": ev.get("parent"), "subagent_type": ev.get("name"),
                              "summary": ev.get("description"), "ts": ev.get("ts")}, p, cwd)
                self.subs[ev.get("parent")] = s
                await self.put(c, s)
            self.by_id[ev.get("id")] = s
            s.started(ev)
        elif kind == "progress":
            s = self.by_id.get(ev.get("id")) or self.subs.get(ev.get("parent"))
            if s:
                s.progressed(ev)
        elif kind == "subagent_end":
            s = self.by_id.get(ev.get("id")) or self.subs.get(ev.get("parent"))
            if s:
                s.finish(ev)
        elif kind == "job":
            await self.job(c, ev)
        elif kind == "round_end":
            await self.put(self.root, Line(render_round_end(ev, p)))
        elif kind == "task_closed":
            for s in self.workers():
                s.finish({"status": "stopped"})
            for b in self.blocks():
                b.finish("ended")
            w = Line(TextRule("task closed", p.hex("accent2"), "━"))
            await self.put(self.root, w)

    async def tool_use(self, c, ev, sub):
        p, cwd = self.pal, self.app.cwd
        if ev.get("name") in AGENT_TOOLS:
            s = SubAgent(ev, p, cwd)
            self.tools[ev.get("id")] = self.subs[ev.get("id")] = s
            await self.put(c, s)
            return
        block = ToolBlock(ev, p, cwd)
        self.tools[ev.get("id")] = block
        await self.put(c, block)
        if sub is not None:
            sub.step(tool_label(ev, cwd))
        for perm in self.pending_perms:  # its permission came first
            if self.matches(perm, ev):
                self.pending_perms.remove(perm)
                await self.judge(block, perm, sub)
                break
        if ev.get("name") == "TodoWrite" and (todos := render_todos(ev.get("input"), p)) is not None:
            await block.mount(Line(todos))

    @staticmethod
    def matches(perm, ev):
        return (perm.get("tool") == ev.get("name") and str(perm.get("summary", "")) == str(ev.get("summary", ""))
                and perm.get("parent") == ev.get("parent"))

    async def permission(self, c, ev, sub):
        if ev.get("tool") in AGENT_TOOLS and ev.get("decision") == "refused":  # a worker that wasn't let start
            for s in reversed(list(self.subs.values())):
                if s.running and not s.brief and s.description == str(ev.get("summary") or ""):
                    s.refused(ev)
                    return
        for block in reversed(list(self.tools.values())):
            if isinstance(block, ToolBlock) and block.verdict is None and block.state == "running" and \
                    self.matches(ev, block.ev):
                await self.judge(block, ev, sub)
                return
        self.pending_perms.append(ev)
        self.pending_perms = self.pending_perms[-20:]
        if ev.get("decision") == "refused":  # a refusal may never get a tool line (nothing ran): show it now
            self.pending_perms.remove(ev)
            await self.put(c, Line(render_permission(ev, self.pal, self.app.cwd)))
            if sub is not None:
                sub.judged(tool_label({"name": ev.get("tool"), "summary": ev.get("summary")}, self.app.cwd), ev)

    async def judge(self, block, verdict, sub):
        block.verdict = verdict
        block.paint()
        if verdict.get("decision") == "refused":
            block.finish("error")
            await block.mount(Fold(self.pal, head=refusal_line(verdict, self.pal), rows=[], preview=0))
        if sub is not None:
            sub.judged(tool_label(block.ev, self.app.cwd), verdict)

    async def tool_result(self, ev):
        p, cwd = self.pal, self.app.cwd
        target = self.tools.get(ev.get("tool_use_id"))
        text, err, meta = str(ev.get("text", "")), bool(ev.get("is_error")), ev.get("meta") or {}
        if isinstance(target, SubAgent):  # the worker's report
            target.report = text
            if target.status is None and err:
                target.finish({"status": "failed"})
            return
        if target is None:  # its call was never logged
            await self.put(self.where(ev), ToolResult(text, err, p))
            return
        target.finish("error" if err else "ok")
        name = target.ev.get("name")
        if target.verdict and target.verdict.get("decision") == "refused":
            return  # the refusal line already says it
        if name in QUIET_TOOLS and not err:
            return
        if not err and name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
            path = short_path(target.ev.get("summary"), cwd)
            if meta.get("patch") is not None:
                rows = patch_rows(meta["patch"])
            else:
                changes = diff_changes(name, target.ev.get("input"))
                rows = [(None, s, x) for s, x in changes] if changes is not None else None
            if rows is not None:
                width = max([len(str(n)) for n, _, _ in rows if n is not None] or [1])
                head = diff_summary(name, rows, path, meta.get("created"))
                drows = [DiffRow(n, s, x, width, p) for n, s, x in rows]
                await self.put(target, Fold(p, head=head, rows=drows, preview=DIFF_LINES, text=text))
                return
        summary = None if err else result_summary(name, text, meta)
        if summary is not None:
            await self.put(target, ToolResult(text, err, p, head=summary, preview=0))
        else:
            await self.put(target, ToolResult(text, err, p))

    async def job(self, c, ev):
        p = self.pal
        what = ev.get("what")
        if what == "plan_approved":
            head = Text()
            head.append("● ", style=p.hex("accent3"))
            head.append("Plan approved", style=f"bold {p.hex('foreground')}")
            plan = ToolResult(str(ev.get("plan", "")), False, p, preview=PREVIEW_LINES)
            w = Vertical(Line(head), plan)
            w.styles.height = "auto"
            await self.put(c, w)
            self.expandables.append(plan)
        elif what == "hand_on":
            label = "fresh orchestrator" + (f" · the last one had {fmt_tokens(ev['tokens'])} tokens"
                                            if ev.get("tokens") else "")
            await self.put(self.root, Line(TextRule(label, p.hex("accent2"))))
        else:
            await self.put(c, Line(render_job(ev, p)))

    async def apply_text(self, container, text, mark="●"):
        p = self.pal
        md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
        widgets = [w for n in tree(md.parse(text)) if (w := block_widget(n, Renderer(p), p)) is not None]
        if widgets:
            await self.put(container, Entry(widgets, p, mark))


# ---- the app ---------------------------------------------------------------------------------------------------
class LiveApp(OutputApp):
    CSS = OutputApp.CSS + """
    #body, #focus { height: 1fr; padding: 1 2 0 2; }
    #focus { display: none; }
    #body > .item, #focus > .item { margin: 0 0 1 0; }
    #spin { height: 1; padding: 0 2; }
    #workers { height: auto; max-height: 6; padding: 0 2; }
    """
    BINDINGS = [  # the output window's keys, except that f follows here instead of paging, and esc goes back
        *(b for b in (show_output.Binding("space,pagedown", "page(1)", show=False) if b.action == "page(1)" else
                      show_output.Binding("q", "quit", "close") if b.action == "quit" else b
                      for b in OutputApp.BINDINGS)),
        show_output.Binding("escape", "back", show=False),
        show_output.Binding("e", "expand", show=False),
        show_output.Binding("enter", "open_worker", show=False),
        show_output.Binding("f", "follow", show=False),
    ]
    POLL_S = 0.25

    def __init__(self, key):
        show_output.App.__init__(self, ansi_color=True)
        self.key = key
        self.theme_ = show_output.Theme()
        self.pal = show_output.Palette(self.theme_)
        self.toast_until, self.toast_text = 0.0, ""
        self.headings = []
        self.follow = True
        self.pinned = None
        self.flow = self.focus_flow = self.focus_sub = None
        self.reset_state()
        self._busy = False
        self._last_y = self._last_max = 0

    def reset_state(self):
        self.offset = 0
        self.running = False
        self.closed = False
        self.round_t0 = 0.0
        self.verb = VERBS[0]
        self.tokens = 0
        self.step = ""
        self.title_text = ""
        self.cwd = None
        self.n_events = 0
        self.job = False
        self.pieces = 0

    def compose(self):
        with Horizontal(id="top"):
            yield MiniOrb(self.pal)
            yield Static(id="titles")
        yield VerticalScroll(id="body")
        yield VerticalScroll(id="focus")
        yield Static(id="spin")
        yield Static(id="workers")
        yield Static(id="foot")

    async def on_mount(self):
        live.claim_window(self.key)
        self.paint_scrollbars(self.query_one("#body"))
        self.paint_scrollbars(self.query_one("#focus"))
        await self.build()
        self.set_interval(self.POLL_S, self.poll)
        self.set_interval(0.1, self.tick)
        self.set_interval(2.0, self.reload_theme)
        self.query_one("#body").focus()

    def on_unmount(self):
        live.release_window(self.key)

    # ---- following the file -------------------------------------------------------------------------------------
    @property
    def expandables(self):
        return (self.focus_flow or self.flow).expandables if self.flow else []

    def scroller(self):
        return self.query_one("#focus" if self.focus_flow else "#body", VerticalScroll)

    async def build(self):
        """Draw everything from the top (first start, a reset file, a palette change)."""
        await self.close_focus()
        await self.query_one("#body").remove_children()
        self.reset_state()
        self.flow = Flow(self, self.query_one("#body"))
        self.pinned = None
        await self.poll()

    async def poll(self):
        if self._busy:
            return
        self._busy = True
        try:
            events, new = live.read_from(self.key, self.offset)
            if new < self.offset:  # the file was reset: start over
                await self.close_focus()
                await self.query_one("#body").remove_children()
                self.reset_state()
                self.flow = Flow(self, self.query_one("#body"))
                self.pinned = None
            self.offset = new
            for ev in events:
                try:
                    await self.apply(ev)
                except Exception as e:  # noqa: BLE001 - one odd event must not stop the window
                    cvlog.log.event("live:render", f"{ev.get('kind')}: {e!r}")
            if events and self.follow:
                self.call_after_refresh(self.scroll_to_end)
        except NoMatches:
            pass  # closing
        finally:
            self._busy = False

    async def apply(self, ev):
        """The window's own state (spinner, title), then the drawing."""
        kind = ev.get("kind")
        self.n_events += 1
        nested = bool(ev.get("parent"))
        if kind == "round_start":
            if not (ev.get("queued") and self.running):
                self.running, self.round_t0 = True, float(ev.get("ts") or time.time())
                self.verb, self.tokens = verb_for(ev.get("ts")), 0
            self.step = ""
            self.cwd = ev.get("cwd") or self.cwd
            self.title_text = self.title_text or str(ev.get("prompt", "")).strip().split("\n")[0]
        elif kind == "tool_use" and not nested:
            self.step = tool_label(ev, self.cwd)
        elif kind == "tokens":
            self.tokens = int(ev.get("out") or 0)
        elif kind == "job":
            self.job = True
            self.pieces += ev.get("what") == "piece_done"
        elif kind == "round_end":
            self.running, self.step = False, ""
            self.flow.end_round()
        elif kind == "task_closed":
            self.running, self.closed, self.step = False, True, ""
        await self.flow.apply(ev)

    def scroll_to_end(self):
        try:
            body = self.scroller()
        except NoMatches:
            return
        body.scroll_end(animate=False)
        self._last_y, self._last_max = body.scroll_y, body.max_scroll_y

    # ---- the focus view: one worker on its own ------------------------------------------------------------------
    async def open_focus(self, sub):
        await self.close_focus()
        focus = self.query_one("#focus", VerticalScroll)
        self.focus_sub, self.focus_flow = sub, Flow(self, focus)
        if sub.brief:
            w = Line(render_prompt({"prompt": sub.brief}, self.pal))
            w.styles.background = self.pal.tint("foreground", 0.07)
            await self.focus_flow.put(focus, w)
        for ev in sub.events:
            await self.focus_flow.apply({k: v for k, v in ev.items() if k != "parent"})
        if sub.report:
            await self.focus_flow.apply_text(focus, sub.report, mark="⎿")
        self.query_one("#body").display = False
        focus.display = True
        focus.focus()
        self.pinned = None
        self.call_after_refresh(focus.scroll_home, animate=False)

    async def close_focus(self):
        if self.focus_flow is None:
            return
        focus = self.query_one("#focus", VerticalScroll)
        await focus.remove_children()
        focus.display = False
        self.focus_flow = self.focus_sub = None
        body = self.query_one("#body")
        body.display = True
        body.focus()
        self.pinned = None

    # ---- header, spinner, workers, footer -----------------------------------------------------------------------
    def tick(self):
        try:
            self._tick()
        except NoMatches:
            pass

    def _tick(self):
        p = self.pal
        body = self.scroller()
        if self.follow and body.scroll_y < self._last_y and body.max_scroll_y >= self._last_max:
            self.follow = False  # the user scrolled up
        self._last_y, self._last_max = body.scroll_y, body.max_scroll_y
        blink_on = int(time.monotonic() * 2) % 2 == 0
        for flow in filter(None, (self.flow, self.focus_flow)):
            for b in flow.blocks():
                b.paint(blink_on)
            for s in flow.workers():
                s.paint(blink_on)
        spin = self.query_one("#spin", Static)
        spin.update(spinner_line(self.verb, time.time() - self.round_t0, self.tokens, self.step, p)
                    if self.running else "")
        workers = self.flow.workers() if self.flow else []
        panel = self.query_one("#workers", Static)
        panel.display = bool(workers)
        if workers:
            panel.update(Group(*(self.worker_line(s) for s in workers)))
        self.query_one("#titles", Static).update(Group(Text(), *self.titles(workers)))
        cur = self.cursor_widget()
        for w in self.expandables:
            w.set_cursor(w is cur)
        self.update_foot()

    def worker_line(self, s):
        p = self.pal
        out = Text(no_wrap=True, overflow="ellipsis")
        out.append(WORKER_SPIN[int(time.monotonic() * 4) % len(WORKER_SPIN)] + " ", style=p.hex("accent2"))
        out.append(s.agent, style=f"bold {p.hex('foreground')}")
        if s.description:
            out.append(f" · {s.description}", style=p.hex("foreground"))
        now = s.recent[-1][0] if s.recent else (s.progress[-1] if s.progress else "")
        if now:
            out.append(f" · {now}", style=p.hex("dim"))
        out.append(f" · {s.totals()}", style=p.hex("dim"))
        return out

    def titles(self, workers):
        p = self.pal
        title_text = self.focus_sub.agent + (f" · {self.focus_sub.description}" if self.focus_sub.description else "") \
            if self.focus_sub else (self.title_text[:80] or self.key)
        title = gradient(title_text, p.rgb("accent"), p.rgb("accent2"), "bold")
        state = "task closed" if self.closed else ("working" if self.running else "idle")
        bits = [self.key] + (["job"] if self.job else []) + [state]
        if self.pieces:
            bits.append(f"{self.pieces} piece{'s' * (self.pieces != 1)} done")
        if workers:
            bits.append(f"{len(workers)} worker{'s' * (len(workers) != 1)} running")
        if self.focus_sub:
            bits = [self.key, "worker", "running" if self.focus_sub.running else self.focus_sub.status, "esc back"]
        return title, Text(" · ".join(bits), style=p.hex("dim"))

    def hint(self):
        if self.focus_flow:
            return Text("esc back · e expand · ↑↓ scroll · q close", style=self.pal.hex("dim"))
        worker = isinstance(self.cursor_widget(), SubAgent)
        return Text(("following" if self.follow else "paused · G follows") + " · e expand"
                    + (" · ⏎ open worker" if worker else "") + " · f follow · ↑↓ scroll · q close",
                    style=self.pal.hex("dim"))

    def update_foot(self):
        try:
            self._update_foot_live()
        except NoMatches:
            pass

    def _update_foot_live(self):
        p = self.pal
        body = self.scroller()
        frac = 1.0 if body.max_scroll_y <= 0 else body.scroll_y / body.max_scroll_y
        width = max(10, min(40, self.size.width // 4))
        done = round(frac * width)
        bar = gradient("━" * done, p.rgb("accent2"), p.rgb("accent")) if done else Text()
        bar.append("─" * (width - done), p.hex("dim"))
        bar.append(f"  {frac:4.0%}", p.hex("dim"))
        hint = Text(self.toast_text, style=f"bold {p.hex('accent3')}") if time.monotonic() < self.toast_until \
            else self.hint()
        gap = max(1, self.size.width - 4 - bar.cell_len - hint.cell_len)
        line = Text.assemble(bar, " " * gap, hint) if gap > 1 else bar
        line.no_wrap, line.overflow = True, "ellipsis"
        self.query_one("#foot", Static).update(line)

    # ---- the cursor ---------------------------------------------------------------------------------------------
    def visible_expandables(self):
        body = self.scroller()
        return [w for w in self.expandables if w.is_attached and w.region.height and w.region.overlaps(body.region)
                and w.expandable]

    def cursor_widget(self):
        """The expandable under the cursor: the pinned one, else the one nearest the middle of the window."""
        if self.pinned is not None and self.pinned in self.expandables:
            return self.pinned
        body = self.scroller()
        mid = body.region.y + body.region.height / 2
        vis = self.visible_expandables()
        return min(vis, key=lambda w: abs(w.region.y + w.region.height / 2 - mid), default=None)

    def select(self, widget):
        self.pinned = widget

    # ---- keys ---------------------------------------------------------------------------------------------------
    def action_expand(self):
        w = self.cursor_widget()
        if w is not None:
            w.toggle()
            if self.follow:
                self.call_after_refresh(self.scroll_to_end)

    async def action_open_worker(self):
        w = self.cursor_widget()
        if isinstance(w, SubAgent) and self.focus_flow is None:
            await self.open_focus(w)
        else:
            self.toast("no worker here" if self.focus_flow is None else "esc goes back")

    async def action_back(self):
        if self.focus_flow is not None:
            await self.close_focus()
            if self.follow:
                self.call_after_refresh(self.scroll_to_end)
        else:
            self.exit()

    def action_follow(self):
        self.follow = not self.follow
        if self.follow:
            self.scroll_to_end()
        self.toast("following new output" if self.follow else "stopped following")

    def toast(self, text):
        self.toast_text, self.toast_until = text, time.monotonic() + 2

    def action_scroll(self, n):
        self.pinned = None
        if n < 0:
            self.follow = False
        self.scroller().scroll_relative(y=n, animate=False)

    def action_page(self, n):
        self.pinned = None
        if n < 0:
            self.follow = False
        body = self.scroller()
        body.scroll_relative(y=n * max(body.size.height - 2, 1), animate=False)

    def action_edge(self, end):
        self.pinned = None
        self.follow = bool(end)
        body = self.scroller()
        body.scroll_end(animate=False) if end else body.scroll_home(animate=False)

    def action_section(self, step):
        pass

    def action_toc(self):
        pass

    def action_copy(self):
        w = self.cursor_widget()
        text = getattr(w, "text", None) or getattr(w, "report", None)
        if text and show_output.platform_util.copy_text(text):
            self.toast("copied")
        else:
            self.toast("nothing to copy here")

    async def reload_theme(self):
        if self.theme_.reload() and self.is_running:  # a new wallpaper palette: draw it all again
            y = self.query_one("#body").scroll_y
            await self.build()
            self.query_one("#body").scroll_to(y=y, animate=False)


def run(key):
    live.claim_window(key)
    try:
        LiveApp(key).run()
    finally:
        live.release_window(key)
