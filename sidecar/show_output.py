"""The output window: one task's full answer (a Markdown file from STATE_DIR/outputs), rendered as a small Textual app.

Run by App.show_output in its own kitty window (class claude-voice-output). It follows cv's look: the wallpaper
palette (reloaded live, like the orb), a small breathing orb in the header, and the same braille renderer.

Everything is laid out at the window's current width and reflows on every resize. Blocks that can't wrap without
losing their shape (```diagram terminal art, code, tables) keep their shape and scroll sideways when the window is
narrower than they are, instead of being cut or broken.

Keys: ↑↓/j k scroll · ←→/h l wide blocks sideways · space/b page · g/G top/end · ] [ next/previous section ·
t contents · y copy · q close
Run: python show_output.py <file.md>   or   python show_output.py --live <task-id>  (live_view.py)
"""

import re
import subprocess
import sys
import time

import asyncio
import platform_util
from markdown_it import MarkdownIt
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, HorizontalScroll, VerticalScroll
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

import art_fix
import orb
from theme import Theme, mix

DIAGRAM_LANGS = ("diagram", "art", "ascii", "text-art")
# In terminal art: box lines stay quiet, arrows and markers stand out, bars get a gradient. By Unicode block.
LINES = re.compile(r"[─-╿]+")
ARROWS = re.compile(r"[←-⇿■-◿➔➤]+")
BARS = re.compile(r"[▀-▟]+")
CALLOUTS = {"NOTE": ("ℹ", "accent2"), "TIP": ("✦", "accent3"), "IMPORTANT": ("◆", "accent"),
            "WARNING": ("▲", "error"), "CAUTION": ("■", "error")}
BULLETS = ("●", "○", "▪", "·")
_BRAILLE = [" "] + [chr(0x2800 + i) for i in range(1, 256)]
# A left bar only: the quote and callout frame.
LEFT_BAR = box.Box("    \n▎   \n▎   \n▎   \n▎   \n▎   \n▎   \n    \n")


def gradient(text, a, b, style=""):
    """Text whose colour runs from a to b across its characters."""
    out = Text()
    n = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        r, g, bl = mix(a, b, i / n)
        out.append(ch, Style.parse(f"{style} #{r:02x}{g:02x}{bl:02x}".strip()))
    return out


class Rule:
    """A full-width rule, drawn at whatever width it's given (so it follows resizes)."""

    def __init__(self, a, b, char="─"):
        self.a, self.b, self.char = a, b, char

    def __rich_console__(self, console, options):
        yield gradient(self.char * max(options.max_width, 1), self.a, self.b)


class Palette:
    def __init__(self, theme):
        self.t = theme

    def hex(self, role):
        return self.t.hex(role)

    def rgb(self, role):
        return self.t.colors[role]

    def tint(self, role, amount):
        r, g, b = mix(self.rgb("background"), self.rgb(role), amount)
        return f"#{r:02x}{g:02x}{b:02x}"


# ---- Markdown → blocks ------------------------------------------------------------------------------------------
def tree(tokens):
    """markdown-it's flat token stream → nested nodes {type, tok, children}."""
    root = {"type": "root", "tok": None, "children": []}
    stack = [root]
    for t in tokens:
        if t.nesting == 1:
            node = {"type": t.type[:-5], "tok": t, "children": []}
            stack[-1]["children"].append(node)
            stack.append(node)
        elif t.nesting == -1:
            stack.pop()
        else:
            stack[-1]["children"].append({"type": t.type, "tok": t, "children": []})
    return root["children"]


def plain(node):
    """The text of a node, without formatting (for headings in the contents list)."""
    if node["type"] == "inline":
        return node["tok"].content
    return "".join(plain(c) for c in node["children"])


class Renderer:
    """Turns nodes into rich renderables in the current palette."""

    def __init__(self, pal):
        self.p = pal

    def inline(self, node, base=""):
        p = self.p
        out = Text(style=base)
        bold = italic = strike = False
        link = None
        for c in node["tok"].children or []:
            style = Style.parse(base or "none") + Style(bold=bold or None, italic=italic or None,
                                                        strike=strike or None)
            if link:
                style += Style(color=p.hex("accent2"), underline=True, link=link)
            if c.type == "text":
                out.append(c.content, style + (Style(color=p.hex("foreground")) if bold and not link else Style()))
            elif c.type == "code_inline":
                out.append(f" {c.content} ", Style(color=p.hex("accent3"), bgcolor=p.tint("accent3", 0.14)))
            elif c.type == "strong_open":
                bold = True
            elif c.type == "strong_close":
                bold = False
            elif c.type == "em_open":
                italic = True
            elif c.type == "em_close":
                italic = False
            elif c.type == "s_open":
                strike = True
            elif c.type == "s_close":
                strike = False
            elif c.type == "link_open":
                link = c.attrs.get("href")
            elif c.type == "link_close":
                link = None
            elif c.type == "softbreak":
                out.append(" ")
            elif c.type == "hardbreak":
                out.append("\n")
            elif c.type == "image":
                out.append(f"[image: {c.content or c.attrs.get('src', '')}]", Style(color=p.hex("dim"), italic=True))
            else:
                out.append(c.content, style)
        return out

    def blocks(self, nodes, depth=0):
        return [r for n in nodes if (r := self.block(n, depth)) is not None]

    def block(self, n, depth=0):
        p, t = self.p, n["type"]
        if t == "paragraph":
            return self.inline(n["children"][0], f"{p.hex('foreground')}") if n["children"] else None
        if t in ("bullet_list", "ordered_list"):
            return self.list(n, depth)
        if t == "blockquote":
            return self.quote(n, depth)
        if t == "heading":  # nested (inside a list or quote): no big styling
            return self.inline(n["children"][0], f"bold {p.hex('accent2')}")
        if t in ("fence", "code_block"):
            return self.code(n["tok"])
        if t == "table":
            return self.table(n)
        if t == "hr":
            return Rule(p.rgb("dim"), p.rgb("background"))
        if t in ("html_block", "html_inline"):
            return Text(n["tok"].content.rstrip(), style=p.hex("dim"))
        return None

    def list(self, n, depth):
        p = self.p
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True, justify="right")
        grid.add_column(ratio=1)
        start = int(n["tok"].attrs.get("start", 1) or 1)
        for i, item in enumerate(n["children"]):
            kids = item["children"]
            marker = Text(f"{start + i}." if n["type"] == "ordered_list" else BULLETS[min(depth, 3)],
                          style=p.hex("accent") if depth == 0 else p.hex("accent2"))
            first = kids[0] if kids else None
            if first and first["type"] == "paragraph" and first["children"]:
                m = re.match(r"\[( |x|X)\]\s+", first["children"][0]["tok"].content)
                if m:  # a task list item
                    done = m.group(1) != " "
                    marker = Text("☑" if done else "☐", style=p.hex("accent3" if done else "dim"))
                    inl = first["children"][0]["tok"]
                    if inl.children and inl.children[0].type == "text":
                        inl.children[0].content = inl.children[0].content[m.end():]
            grid.add_row(marker, Group(*self.blocks(kids, depth + 1)))
        return grid

    def quote(self, n, depth, framed=True):
        """→ a Panel with a left bar (nested), or with framed=False → (title, body, colour) for a Callout widget."""
        p = self.p
        kids = n["children"]
        title, colour = None, "accent2"
        if kids and kids[0]["type"] == "paragraph" and kids[0]["children"]:
            inl = kids[0]["children"][0]["tok"]
            m = re.match(r"\[!(\w+)\]\s*", inl.content)
            if m and m.group(1).upper() in CALLOUTS:  # GitHub-style callout: > [!NOTE]
                icon, colour = CALLOUTS[m.group(1).upper()]
                title = Text(f" {icon} {m.group(1).capitalize()} ", style=f"bold {p.hex(colour)}")
                inl.children = [c for c in inl.children if not (c.type == "text" and c.content.startswith("[!"))]
                while inl.children and inl.children[0].type in ("softbreak", "hardbreak"):
                    inl.children.pop(0)
        body = Group(*self.blocks(kids, depth))
        if not framed:
            return title, body, colour
        return Panel(body, box=LEFT_BAR, border_style=p.hex(colour), title=title, title_align="left",
                     padding=(0, 1), style=Style(italic=title is None), expand=True)

    def code(self, tok):
        p = self.p
        lang = (tok.info or "").split()[0].lower() if tok.info else ""
        src = tok.content.rstrip("\n")
        if lang in DIAGRAM_LANGS:
            return Panel(self.art(src), box=box.ROUNDED, border_style=p.hex("dim"), padding=(0, 2), expand=False)
        syntax = Syntax(src, lang or "text", theme="ansi_dark", line_numbers=src.count("\n") >= 3,
                        word_wrap=False, background_color="default", indent_guides=True)
        return Panel(syntax, box=box.ROUNDED, border_style=p.tint("accent", 0.55), padding=(0, 1), expand=False,
                     title=Text(f" {lang} ", style=f"bold {p.hex('accent')}") if lang else None, title_align="left")

    def art(self, src):
        p = self.p
        src = art_fix.fix(src)  # square up boxes whose rows the model counted off by a cell or two
        art = Text(src, style=p.hex("foreground"), no_wrap=True, overflow="ignore")
        art.highlight_regex(LINES, p.hex("accent2"))
        art.highlight_regex(ARROWS, f"bold {p.hex('accent')}")
        for m in BARS.finditer(src):  # bars: a gradient along their length, so they read as charts
            n = max(m.end() - m.start() - 1, 1)
            for i in range(m.start(), m.end()):
                r, g, b = mix(p.rgb("accent2"), p.rgb("accent"), (i - m.start()) / n)
                art.stylize(f"#{r:02x}{g:02x}{b:02x}", i, i + 1)
        return art

    def table(self, n):
        p = self.p
        head = next((c for c in n["children"] if c["type"] == "thead"), None)
        body = next((c for c in n["children"] if c["type"] == "tbody"), None)
        tbl = Table(box=box.ROUNDED, border_style=p.hex("dim"), header_style=f"bold {p.hex('accent')}",
                    expand=False, pad_edge=True)
        if head:
            for cell in head["children"][0]["children"]:
                align = {"text-align:right": "right", "text-align:center": "center"}.get(
                    (cell["tok"].attrs.get("style") or "").replace(" ", ""), "left")
                tbl.add_column(self.inline(cell["children"][0]) if cell["children"] else "", justify=align,
                               overflow="fold")
        for row in (body or {"children": []})["children"]:
            tbl.add_row(*[self.inline(c["children"][0], p.hex("foreground")) if c["children"] else ""
                          for c in row["children"]])
        return tbl


# ---- widgets ----------------------------------------------------------------------------------------------------
class MiniOrb(Widget):
    """cv's orb, small, breathing: the same renderer as the main window."""

    DEFAULT_CSS = "MiniOrb { width: 8; height: 3; }"

    def __init__(self, pal, **kw):
        super().__init__(**kw)
        self.pal, self.t0 = pal, time.monotonic()

    def on_mount(self):
        self.set_interval(1 / 12, self.refresh)

    def render(self):
        bits, rgb = orb.render_arrays("listening", 0.0, time.monotonic() - self.t0, self.size.width or 8,
                                      self.size.height or 3, self.pal.t.colors)
        out = Text()
        for y in range(bits.shape[0]):
            for x in range(bits.shape[1]):
                r, g, b = rgb[y, x]
                out.append(_BRAILLE[int(bits[y, x])], f"#{r:02x}{g:02x}{b:02x}")
            if y < bits.shape[0] - 1:
                out.append("\n")
        return out


class Block(Static):
    """One top-level block of the answer."""

    DEFAULT_CSS = "Block { height: auto; margin: 0 0 1 0; }"


class Wide(HorizontalScroll):
    """A block that keeps its shape (art, code, a table): it scrolls sideways when the window is narrower.

    Its content is sized to its own natural width, measured by rich (Textual would cap `width: auto` at the window's
    width, which crops it instead), and to at least the window's width so short ones still line up."""

    DEFAULT_CSS = """
    Wide { height: auto; margin: 0 0 1 0; scrollbar-size-horizontal: 1; scrollbar-size-vertical: 0; }
    Wide > Static { height: auto; }
    """

    def __init__(self, renderable, **kw):
        super().__init__(**kw)
        self.renderable = renderable

    def compose(self):
        yield Static(self.renderable)

    def on_mount(self):
        self.app.paint_scrollbars(self)
        console = self.app.console
        self.natural = console.measure(self.renderable, options=console.options.update_width(10_000)).maximum
        self.fit()

    def on_resize(self):
        self.fit()

    def fit(self):
        if hasattr(self, "natural"):
            self.children[0].styles.width = max(self.natural, self.size.width)


class Callout(Static):
    """A quote or a GitHub-style callout (> [!NOTE]): a bar down its whole left side."""

    DEFAULT_CSS = "Callout { height: auto; margin: 0 0 1 0; padding: 0 0 0 2; }"

    def __init__(self, title, body, colour, pal):
        super().__init__(Group(title, body) if title else body)
        self.styles.border_left = ("outer", pal.hex(colour))
        if not title:
            self.styles.text_style = "italic"


class Heading(Static):
    DEFAULT_CSS = "Heading { height: auto; margin: 1 0 1 0; }"

    def __init__(self, level, title, renderable, **kw):
        super().__init__(renderable, **kw)
        self.title = title


def block_widget(n, r, pal):
    """The widget for one top-level node: a Callout, a Wide (art, code, table) or a Block; None if it draws nothing."""
    if n["type"] == "blockquote":
        return Callout(*r.quote(n, 0, framed=False), pal)
    if n["type"] in ("fence", "code_block", "table"):
        return Wide(r.block(n))
    rend = r.block(n)
    return Block(rend) if rend is not None else None


class OutputApp(App):
    CSS = """
    Screen { background: ansi_default; layout: vertical; }
    #top { height: 3; padding: 0 2; }
    #titles { width: 1fr; height: 3; padding: 0 0 0 2; }
    #main { height: 1fr; }
    #toc { width: 32; display: none; border-right: vkey $panel; padding: 1 1; background: ansi_default; }
    #toc.shown { display: block; }
    #body { padding: 1 4 1 4; scrollbar-size-vertical: 1; }
    #foot { height: 1; padding: 0 2; }
    """
    BINDINGS = [
        Binding("q,escape", "quit", "close"),
        Binding("j,down", "scroll(3)", show=False), Binding("k,up", "scroll(-3)", show=False),
        Binding("space,f,pagedown", "page(1)", show=False), Binding("b,pageup", "page(-1)", show=False),
        Binding("g,home", "edge(0)", show=False), Binding("G,end", "edge(1)", show=False),
        Binding("right_square_bracket", "section(1)", show=False),
        Binding("left_square_bracket", "section(-1)", show=False),
        Binding("h,left", "sideways(-8)", show=False), Binding("l,right", "sideways(8)", show=False),
        Binding("t", "toc", show=False), Binding("y", "copy", show=False),
    ]

    def __init__(self, path):
        super().__init__(ansi_color=True)  # kitty's own background (and its opacity) shows through, like cv
        self.path = path
        self.theme_ = Theme()
        self.pal = Palette(self.theme_)
        self.source = open(path, encoding="utf-8").read()
        self.toast_until, self.toast_text = 0.0, ""

    # ---- layout -------------------------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield MiniOrb(self.pal)
            yield Static(id="titles")
        with Horizontal(id="main"):
            yield OptionList(id="toc")
            yield VerticalScroll(id="body")
        yield Static(id="foot")

    async def on_mount(self):
        await self.build()
        self.set_interval(0.1, self.update_foot)
        self.set_interval(2.0, self.reload_theme)
        self.query_one("#body").focus()

    def split(self):
        """→ (title, meta, nodes): cv's own header lines (# t1 · title, *project · time*) come off the top."""
        md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
        nodes = tree(md.parse(self.source))
        title, meta = "", ""
        if nodes and nodes[0]["type"] == "heading" and nodes[0]["tok"].tag == "h1":
            title = plain(nodes.pop(0))
            kids = (nodes[0]["children"][0]["tok"].children or []) if nodes and nodes[0]["type"] == "paragraph" else []
            if kids and kids[0].type == "em_open" and kids[-1].type == "em_close":  # *project · time*
                meta = plain(nodes.pop(0)).strip("*_ ")
        return title, meta, nodes

    async def build(self):
        """(Re)build every block from the source in the current palette."""
        p, r = self.pal, Renderer(self.pal)
        title, meta, nodes = self.split()
        words = len(re.findall(r"\w+", self.source))
        head = gradient(title or "Output", p.rgb("accent"), p.rgb("accent2"), "bold")
        sub = Text(" · ".join(x for x in (meta, f"{words:,} words", f"{max(1, round(words / 230))} min read") if x),
                   style=p.hex("dim"))
        self.query_one("#titles", Static).update(Group(Text(), head, sub))
        body = self.query_one("#body", VerticalScroll)
        self.paint_scrollbars(body)
        await body.remove_children()
        toc = self.query_one("#toc", OptionList)
        toc.clear_options()
        self.headings = []
        widgets = []
        for n in nodes:
            if n["type"] == "heading":
                level = int(n["tok"].tag[1])
                text = plain(n)
                if level <= 2:
                    rend = Group(gradient(text, p.rgb("accent"), p.rgb("accent2"), "bold"),
                                 Rule(p.rgb("accent"), p.rgb("background"), "━" if level == 1 else "─"))
                else:
                    rend = Text.assemble(("▍ ", p.hex("accent")), (text, f"bold {p.hex('accent2')}"))
                w = Heading(level, text, rend)
                self.headings.append(w)
                toc.add_option(Option(Text("  " * (level - 1) + text, style=p.hex("foreground" if level <= 2
                                                                                       else "dim"))))
                widgets.append(w)
            elif (w := block_widget(n, r, p)) is not None:
                widgets.append(w)
        if getattr(self, "_built", False):
            await body.mount_all(widgets)
        else:  # first open: the answer streams in, a few blocks at a time
            self._built = True
            await body.mount_all(widgets[:4])
            for i in range(4, len(widgets), 3):
                await asyncio.sleep(0.03)
                await body.mount_all(widgets[i:i + 3])
        if len(self.headings) >= 3 and self.size.width >= 130 and not getattr(self, "_toc_set", False):
            toc.add_class("shown")  # wide window, several sections: show the contents
        self._toc_set = True

    def paint_scrollbars(self, widget):
        """Scrollbars in the wallpaper palette, quiet until hovered (Textual's default is a bright blue)."""
        p, st = self.pal, widget.styles
        st.scrollbar_background = st.scrollbar_background_hover = st.scrollbar_background_active = p.tint(
            "foreground", 0.025)
        st.scrollbar_color = p.tint("accent", 0.22)
        st.scrollbar_color_hover = st.scrollbar_color_active = p.tint("accent", 0.8)
        st.scrollbar_corner_color = p.tint("foreground", 0.025)

    # ---- footer -------------------------------------------------------------------------------------------------
    def update_foot(self):
        try:
            self._update_foot()
        except NoMatches:
            pass  # the window is closing: its widgets are already gone

    def _update_foot(self):
        p = self.pal
        body = self.query_one("#body", VerticalScroll)
        frac = 1.0 if body.max_scroll_y <= 0 else body.scroll_y / body.max_scroll_y
        width = max(10, min(40, self.size.width // 4))
        done = round(frac * width)
        bar = gradient("━" * done, p.rgb("accent2"), p.rgb("accent")) if done else Text()
        bar.append("─" * (width - done), p.hex("dim"))
        bar.append(f"  {frac:4.0%}", p.hex("dim"))
        if time.monotonic() < self.toast_until:
            hint = Text(self.toast_text, style=f"bold {p.hex('accent3')}")
        else:
            hint = self.hint()
        gap = max(1, self.size.width - 4 - bar.cell_len - hint.cell_len)
        line = Text.assemble(bar, " " * gap, hint) if gap > 1 else bar
        line.no_wrap, line.overflow = True, "ellipsis"
        self.query_one("#foot", Static).update(line)

    def hint(self):
        wide = any(w.max_scroll_x for w in self.query(Wide))
        return Text("↑↓ scroll · " + ("←→ wide blocks · " if wide else "") + "[ ] sections · t contents · "
                    "y copy · q close", style=self.pal.hex("dim"))

    async def reload_theme(self):
        if self.theme_.reload() and self.is_running:
            y = self.query_one("#body").scroll_y
            await self.build()
            self.query_one("#body").scroll_to(y=y, animate=False)

    # ---- keys ---------------------------------------------------------------------------------------------------
    def action_scroll(self, n):
        self.query_one("#body").scroll_relative(y=n, animate=False)

    def action_page(self, n):
        body = self.query_one("#body")
        body.scroll_relative(y=n * max(1, body.size.height - 2), animate=True, duration=0.15)

    def action_edge(self, end):
        body = self.query_one("#body")
        (body.scroll_end if end else body.scroll_home)(animate=True, duration=0.2)

    def action_section(self, step):
        if not self.headings:
            return
        body = self.query_one("#body")
        tops = [h.region.y - body.region.y + body.scroll_y for h in self.headings]
        here = body.scroll_y + 1
        target = (next((h for h, y in zip(self.headings, tops) if y > here), None) if step > 0 else
                  next((h for h, y in reversed(list(zip(self.headings, tops))) if y < here - 1), None))
        if target:
            body.scroll_to_widget(target, top=True, animate=True, duration=0.2)

    def action_sideways(self, n):
        """Scroll the wide blocks (art, code, tables) that are on screen sideways."""
        body = self.query_one("#body")
        for w in self.query(Wide):
            if w.max_scroll_x and w.region.overlaps(body.region):
                w.scroll_relative(x=n, animate=False)

    def action_toc(self):
        self.query_one("#toc").toggle_class("shown")

    def on_option_list_option_selected(self, event):
        if 0 <= event.option_index < len(self.headings):
            self.query_one("#body").scroll_to_widget(self.headings[event.option_index], top=True, animate=True,
                                                    duration=0.2)
            self.query_one("#body").focus()

    def action_copy(self):
        if platform_util.copy_text(self.source):
            self.toast_text = "copied the whole answer"
        else:
            self.toast_text = "couldn't copy (no clipboard tool found)"
        self.toast_until = time.monotonic() + 2


def main():
    platform_util.utf8_console()
    if sys.argv[1:2] == ["--live"] and len(sys.argv) > 2:  # a task's live log (live.py), followed like tail -f
        import live_view
        live_view.run(sys.argv[2])
        return
    if sys.argv[1:2] == ["--claim"] and len(sys.argv) > 3:  # a saved output file, as one window per task (live.file_key)
        import live
        live.claim_window(sys.argv[2])
        try:
            OutputApp(sys.argv[3]).run()
        finally:
            live.release_window(sys.argv[2])
        return
    OutputApp(sys.argv[1]).run()


if __name__ == "__main__":
    main()
