"""The live output window: follows STATE_DIR/live/<key>.jsonl (live.py) like `tail -f` and draws it step by step, much
like a Claude Code chat, in cv's look (the OutputApp palette, orb, Markdown / table / diagram renderers).

Run by `show_output.py --live <key>`. The window claims itself (live.claim_window) and lets go at exit.

New lines are appended as widgets; nothing already drawn is touched, unless the file was reset (it got shorter), in
which case everything is drawn again from the top.

Keys (on top of the output window's): e expand / collapse the result or subagent under the cursor (the one nearest the
middle of the window, marked with a bright ⎿ / ▾) · f follow new output on/off · G / End jump to the end and follow ·
scrolling up stops following · q close
"""

import asyncio
import difflib
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
from show_output import MiniOrb, OutputApp, Renderer, Rule, block_widget, gradient, tree

PREVIEW_LINES = 3  # lines of a tool result shown before it is expanded
EXPANDED_LINES = 400
DIFF_LINES = 30
PROMPT_LINES = 12
QUIET_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit", "TodoWrite")  # their result is the diff / checklist itself
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BY = {"user": "by you", "auto": "automatically", "step_checker": "by the step checker"}


# ---- pure renderers: event → renderable ------------------------------------------------------------------------
def fmt_duration(s):
    s = float(s or 0)
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s"


def render_prompt(ev, pal):
    lines = str(ev.get("prompt", "")).strip().split("\n")
    more = len(lines) - PROMPT_LINES
    body = "\n".join(lines[:PROMPT_LINES])
    out = Text()
    out.append("> ", style=f"bold {pal.hex('accent')}")
    out.append(body, style=pal.hex("foreground"))
    if more > 0:
        out.append(f"\n  … +{more} lines", style=pal.hex("dim"))
    if ev.get("queued"):
        out.append("  (queued)", style=pal.hex("dim"))
    return out


def tool_label(ev):
    return f"{ev.get('name', '?')}({ev.get('summary', '')})"


def render_tool_line(ev, pal):
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append("● ", style=pal.hex("accent3"))
    out.append(str(ev.get("name", "?")), style=f"bold {pal.hex('foreground')}")
    out.append(f"({ev.get('summary', '')})", style=pal.hex("dim"))
    return out


def render_permission(ev, pal):
    ok = ev.get("decision") == "approved"
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append("  ✓ " if ok else "  ✗ ", style=pal.hex("accent3" if ok else "error"))
    by = ev.get("by")
    words = "auto-approved" if ok and by == "auto" else ("approved " if ok else "refused ") + BY.get(by, str(by or ""))
    out.append(words.strip(), style=pal.hex("foreground" if ok else "error"))
    out.append(f" · {tool_label({'name': ev.get('tool'), 'summary': ev.get('summary', '')})}", style=pal.hex("dim"))
    if ev.get("reason"):
        out.append(f" · {ev['reason']}", style=pal.hex("dim"))
    return out


def render_result(text, pal, is_error=False, expanded=False, cursor=False):
    """A tool result: ⎿ and a few lines; expanded, up to EXPANDED_LINES."""
    lines = (text or "").rstrip("\n").split("\n") if (text or "").strip() else ["(no output)"]
    limit = EXPANDED_LINES if expanded else PREVIEW_LINES
    colour = pal.hex("error" if is_error else "dim")
    out = Text(no_wrap=not expanded, overflow="fold" if expanded else "ellipsis")
    for i, line in enumerate(lines[:limit]):
        out.append("  ⎿  " if i == 0 else "     ",
                   style=pal.hex("accent") if (cursor and i == 0) else pal.hex("dim"))
        out.append(line + ("\n" if i < min(len(lines), limit) - 1 else ""), style=colour)
    if len(lines) > limit:
        out.append(f"\n     … +{len(lines) - limit} lines" + (" (e to expand)" if not expanded else ""),
                   style=pal.hex("dim"))
    elif expanded and len(lines) > PREVIEW_LINES:
        out.append("\n     (e to collapse)", style=pal.hex("dim"))
    return out


def diff_changes(name, inp):
    """Edit / MultiEdit / Write / NotebookEdit input → [(sign, line)] of what changed, or None."""
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


def render_diff(name, inp, pal):
    changes = diff_changes(name, inp)
    if changes is None:
        return None
    added, removed = sum(s == "+" for s, _ in changes), sum(s == "-" for s, _ in changes)
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    head = f"Wrote {added} lines to {path}" if name == "Write" else (
        f"Added {added} line{'s' * (added != 1)}, removed {removed} line{'s' * (removed != 1)}")
    out = Text()
    out.append("  ⎿  " + head, style=pal.hex("dim"))
    for sign, line in changes[:DIFF_LINES]:
        colour = pal.hex("accent3" if sign == "+" else "error")
        out.append("\n     ")
        out.append(f"{sign} {line} ", style=Style(color=colour, bgcolor=pal.tint("accent3" if sign == "+" else "error", 0.16)))
    if len(changes) > DIFF_LINES:
        out.append(f"\n     … +{len(changes) - DIFF_LINES} more lines", style=pal.hex("dim"))
    return out


def render_todos(inp, pal):
    todos = inp.get("todos") if isinstance(inp, dict) else None
    if not isinstance(todos, list):
        return None
    out = Text()
    for i, t in enumerate(x for x in todos if isinstance(x, dict)):
        done, active = t.get("status") == "completed", t.get("status") == "in_progress"
        out.append(("\n" if i else "") + "  ")
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
    turns = ev.get("turns")
    label = fmt_duration(ev.get("duration_s")) + (f" · {turns} turn{'s' * (turns != 1)}" if turns is not None else "")
    ok = ev.get("ok", True)
    return TextRule(label if ok else label + " · failed", pal.hex("dim" if ok else "error"))


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
    """

    def __init__(self, widgets, pal, **kw):
        super().__init__(**kw)
        self.widgets, self.pal = widgets, pal

    def compose(self):
        yield Static(Text("●", style=self.pal.hex("accent")), classes="mark")
        with Vertical():
            yield from self.widgets


class ToolResult(Static):
    """⎿ and a preview of a tool result; `e` expands it."""

    DEFAULT_CSS = "ToolResult { height: auto; }"

    def __init__(self, text, is_error, pal, **kw):
        super().__init__(**kw)
        self.text, self.is_error, self.pal = text, is_error, pal
        self.expanded = self.cursor = False
        self.paint()

    @property
    def expandable(self):
        return len((self.text or "").rstrip("\n").split("\n")) > PREVIEW_LINES

    def paint(self):
        self.update(render_result(self.text, self.pal, self.is_error, self.expanded, self.cursor))

    def toggle(self):
        if self.expandable:
            self.expanded = not self.expanded
            self.paint()

    def set_cursor(self, on):
        if on != self.cursor:
            self.cursor = on
            self.paint()


class SubAgent(Vertical):
    """A worker's block: a header, its steps nested below; collapsed once it has finished."""

    DEFAULT_CSS = """
    SubAgent { height: auto; margin: 0 0 1 0; }
    SubAgent > .head { height: auto; }
    SubAgent > .steps { height: auto; margin: 0 0 0 2; padding: 0 0 0 1; }
    """

    def __init__(self, name, description, pal, **kw):
        super().__init__(**kw)
        self.agent, self.description, self.pal = name, description, pal
        self.status = None
        self.expanded = True
        self.cursor = False
        self.head = Static(classes="head")
        self.steps = Vertical(classes="steps")
        self.steps.styles.border_left = ("outer", pal.tint("accent2", 0.5))
        self.paint()

    def compose(self):
        yield self.head
        yield self.steps

    @property
    def expandable(self):
        return self.status is not None

    def paint(self):
        p = self.pal
        out = Text(no_wrap=True, overflow="ellipsis")
        out.append("▾ " if self.expanded else "▸ ", style=p.hex("accent" if self.cursor else "accent2"))
        out.append(self.agent, style=f"bold {p.hex('accent2')}")
        if self.description:
            out.append(f" · {self.description}", style=p.hex("dim"))
        if self.status:
            ok = self.status in ("completed", "done")
            out.append(f"  {'✓' if ok else '✗'} {self.status}", style=p.hex("accent3" if ok else "error"))
        self.head.update(out)
        self.steps.display = self.expanded

    def finish(self, status):
        self.status, self.expanded = status or "completed", False
        self.paint()

    def toggle(self):
        if self.expandable:
            self.expanded = not self.expanded
            self.paint()

    def set_cursor(self, on):
        if on != self.cursor:
            self.cursor = on
            self.paint()


# ---- the app ---------------------------------------------------------------------------------------------------
class LiveApp(OutputApp):
    CSS = OutputApp.CSS + """
    #body { height: 1fr; padding: 0 2 0 2; }
    #spin { height: 1; padding: 0 2; }
    """
    BINDINGS = [  # the output window's keys, except that f follows here instead of paging
        *(b if b.action != "page(1)" else show_output.Binding("space,pagedown", "page(1)", show=False)
          for b in OutputApp.BINDINGS),
        show_output.Binding("e", "expand", show=False),
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
        self.reset_state()
        self._busy = False
        self._last_y = self._last_max = 0

    def reset_state(self):
        self.offset = 0
        self.tools = {}  # tool_use id → (name, container)
        self.by_parent = {}  # an Agent call's tool_use id → its SubAgent
        self.by_id = {}  # a subagent's own id → SubAgent
        self.running = False
        self.closed = False
        self.round_t0 = 0.0
        self.step = ""
        self.title_text = ""
        self.n_events = 0
        self.expandables = []

    def compose(self):
        with Horizontal(id="top"):
            yield MiniOrb(self.pal)
            yield Static(id="titles")
        yield VerticalScroll(id="body")
        yield Static(id="spin")
        yield Static(id="foot")

    async def on_mount(self):
        live.claim_window(self.key)
        self.paint_scrollbars(self.query_one("#body"))
        await self.build()
        self.set_interval(self.POLL_S, self.poll)
        self.set_interval(0.1, self.tick)
        self.set_interval(2.0, self.reload_theme)
        self.query_one("#body").focus()

    def on_unmount(self):
        live.release_window(self.key)

    # ---- following the file -------------------------------------------------------------------------------------
    async def build(self):
        """Draw everything from the top (first start, a reset file, a palette change)."""
        await self.query_one("#body").remove_children()
        self.reset_state()
        self.pinned = None
        await self.poll()

    async def poll(self):
        if self._busy:
            return
        self._busy = True
        try:
            events, new = live.read_from(self.key, self.offset)
            if new < self.offset:  # the file was reset: start over
                await self.query_one("#body").remove_children()
                self.reset_state()
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

    def scroll_to_end(self):
        try:
            body = self.query_one("#body")
        except NoMatches:
            return
        body.scroll_end(animate=False)
        self._last_y, self._last_max = body.scroll_y, body.max_scroll_y

    def where(self, ev):
        """The container an event's widgets go in: the body, or the steps of the subagent the event belongs to."""
        sub = self.by_parent.get(ev.get("parent")) if ev.get("parent") else None
        return sub.steps if sub else self.query_one("#body")

    async def put(self, container, *widgets):
        await container.mount(*widgets)
        for w in widgets:
            if hasattr(w, "toggle"):
                self.expandables.append(w)

    async def apply(self, ev):
        kind, p = ev.get("kind"), self.pal
        self.n_events += 1
        c = self.where(ev)
        if kind == "round_start":
            if not (ev.get("queued") and self.running):
                self.running, self.round_t0 = True, float(ev.get("ts") or time.time())
            self.step = ""
            self.title_text = self.title_text or str(ev.get("prompt", "")).strip().split("\n")[0]
            w = Line(render_prompt(ev, p))
            w.styles.background = p.tint("foreground", 0.07)
            w.styles.margin = (1, 0, 1, 0)
            await self.put(c, w)
        elif kind == "text":
            await self.apply_text(c, str(ev.get("text", "")))
        elif kind == "tool_use":
            self.tools[ev.get("id")] = (ev.get("name"), c)
            self.step = tool_label(ev)
            await self.put(c, Line(render_tool_line(ev, p)))
            extra = render_diff(ev.get("name"), ev.get("input"), p) if ev.get("name") != "TodoWrite" else render_todos(
                ev.get("input"), p)
            if extra is not None:
                await self.put(c, Line(extra))
        elif kind == "tool_result":
            name, home = self.tools.get(ev.get("tool_use_id"), (None, c))
            if name in QUIET_TOOLS and not ev.get("is_error"):
                return
            await self.put(home, ToolResult(str(ev.get("text", "")), bool(ev.get("is_error")), p))
        elif kind == "permission":
            await self.put(c, Line(render_permission(ev, p)))
        elif kind == "subagent_start":
            home = self.tools.get(ev.get("parent"), (None, c))[1]
            sub = SubAgent(str(ev.get("name", "agent")), str(ev.get("description") or ""), p)
            self.by_id[ev.get("id")] = sub
            if ev.get("parent"):
                self.by_parent[ev["parent"]] = sub
            await self.put(home, sub)
        elif kind == "subagent_end":
            sub = self.by_id.get(ev.get("id"))
            if sub:
                sub.finish(ev.get("status"))
        elif kind == "round_end":
            self.running, self.step = False, ""
            await self.put(self.query_one("#body"), Line(render_round_end(ev, p)))
        elif kind == "task_closed":
            self.running, self.closed, self.step = False, True, ""
            w = Line(TextRule("task closed", p.hex("accent2"), "━"))
            w.styles.margin = (1, 0, 0, 0)
            await self.put(self.query_one("#body"), w)

    async def apply_text(self, container, text):
        p = self.pal
        md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
        widgets = [w for n in tree(md.parse(text)) if (w := block_widget(n, Renderer(p), p)) is not None]
        if widgets:
            await self.put(container, Entry(widgets, p))

    # ---- header, spinner, footer --------------------------------------------------------------------------------
    def tick(self):
        try:
            self._tick()
        except NoMatches:
            pass

    def _tick(self):
        p = self.pal
        body = self.query_one("#body", VerticalScroll)
        if self.follow and body.scroll_y < self._last_y and body.max_scroll_y >= self._last_max:
            self.follow = False  # the user scrolled up
        self._last_y, self._last_max = body.scroll_y, body.max_scroll_y
        spin = self.query_one("#spin", Static)
        if self.running:
            frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append(f"{frame} ", style=p.hex("accent"))
            line.append(f"Working… {fmt_duration(max(time.time() - self.round_t0, 0))}", style=p.hex("foreground"))
            if self.step:
                line.append(f" · {self.step}", style=p.hex("dim"))
            spin.update(line)
        else:
            spin.update("")
        state = "task closed" if self.closed else ("working" if self.running else "idle")
        title = gradient(self.title_text[:80] or self.key, p.rgb("accent"), p.rgb("accent2"), "bold")
        sub = Text(f"{self.key} · {state}", style=p.hex("dim"))
        self.query_one("#titles", Static).update(Group(Text(), title, sub))
        cur = self.cursor_widget()
        for w in self.expandables:
            w.set_cursor(w is cur)
        self.update_foot()

    def hint(self):
        return Text(("following" if self.follow else "paused · G follows") + " · e expand · f follow · ↑↓ scroll · q close",
                    style=self.pal.hex("dim"))

    # ---- the cursor ---------------------------------------------------------------------------------------------
    def visible_expandables(self):
        body = self.query_one("#body")
        return [w for w in self.expandables if w.is_attached and w.region.height and w.region.overlaps(body.region)
                and w.expandable]

    def cursor_widget(self):
        """The expandable under the cursor: the pinned one, else the one nearest the middle of the window."""
        if self.pinned is not None and self.pinned in self.expandables:
            return self.pinned
        body = self.query_one("#body")
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
        super().action_scroll(n)

    def action_page(self, n):
        self.pinned = None
        if n < 0:
            self.follow = False
        super().action_page(n)

    def action_edge(self, end):
        self.pinned = None
        if end:
            self.follow = True
        else:
            self.follow = False
        super().action_edge(end)

    def action_section(self, step):
        pass

    def action_toc(self):
        pass

    def action_copy(self):
        w = self.cursor_widget()
        text = getattr(w, "text", None)
        if text and show_output.platform_util.copy_text(text):
            self.toast("copied the result")
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
