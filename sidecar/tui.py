"""cv — the voice assistant as a minimal full-screen terminal app: a breathing orb, one status line, nothing else.

Keys: space mute · p always listening / push to talk · hold V to talk (push to talk) · t transcript · i type ·
Esc stop talking · a/r/e/m answer a pending decision · q quit.
`cv --plain` runs the old scrolling text UI instead (voice_term.py).
"""

import asyncio
import json
import os
import sys
import time

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

import numpy as np

import config
import cvlog
import orb
import platform_util
import ptt
import ptt_keys
from agents import RESTART_EXIT
from engine import Engine, StartupError, make_parser, parse_args
from theme import Theme

STATE_FILE = os.path.join(platform_util.state_dir(), "ui.json")
_BRAILLE = np.array([" "] + [chr(0x2800 + i) for i in range(1, 256)])  # cell bits → character
LOCK_FILE = platform_util.lock_path()


# Keys cv uses itself; the push-to-talk key can't be one of them.
RESERVED_KEYS = {"space", "t", "i", "p", "a", "r", "e", "m", "o", "q", "escape", "enter"}


def load_prefs():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(prefs):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f)


class Orb(Widget):
    """Draws the orb from the engine's state and sound levels.

    Drawing is the app's main CPU cost (it shares the event loop with the mic, VAD and Haiku's stream), so: the frame
    rate follows the state (smooth only while reacting to sound), unchanged frames aren't redrawn, and cells are
    emitted in runs of one colour with cached styles.
    """

    FPS = {"talking": 24, "speaking": 24}  # everything else (breathing, thinking, …): DEFAULT_FPS
    DEFAULT_FPS = 12

    def __init__(self, voice, **kw):
        super().__init__(**kw)
        self.voice = voice
        self.energy = 0.0
        self.t0 = time.monotonic()
        self.last_draw = 0.0
        self.last_key = None
        self.text = Text()
        self._style_cache = {}  # (not `styles`: that's Textual's own attribute)

    def on_mount(self):
        self.set_interval(1 / 30, self.tick)

    def _state(self):
        v = self.voice
        return "waiting" if v.flash_until > time.monotonic() else v.engine.state  # brief accent flash on errors

    def tick(self):
        state, now = self._state(), time.monotonic()
        if now - self.last_draw < 1 / self.FPS.get(state, self.DEFAULT_FPS) - 0.002:
            return
        self.last_draw = now
        e = self.voice.engine
        target = {"talking": e.mic_level, "speaking": e.out_level}.get(state, 0.0)
        self.energy += (target - self.energy) * (0.45 if target > self.energy else 0.12)  # fast attack, slow decay
        t = now - self.t0
        cols, rows = max(4, self.size.width), max(2, self.size.height)
        bits, rgb = orb.render_arrays(state, self.energy, t, cols, rows, self.voice.theme_.colors, min(1.0, t / 1.5))
        key = hash((bits.tobytes(), rgb.tobytes()))
        if key == self.last_key:
            return  # nothing moved (e.g. muted): skip the repaint entirely
        self.last_key = key
        self.text = self._to_text(bits, rgb)
        self.refresh()

    def _style(self, code):
        st = self._style_cache.get(code)
        if st is None:
            st = self._style_cache[code] = Style(color=Color.from_rgb(code >> 16, (code >> 8) & 255, code & 255))
        return st

    def _to_text(self, bits, rgb):
        # One packed colour per cell (-1 = blank), then split each row where the colour changes: a handful of
        # runs per row instead of one styled segment per cell.
        code = (rgb[..., 0].astype(np.int32) << 16) | (rgb[..., 1].astype(np.int32) << 8) | rgb[..., 2]
        code = np.where(bits > 0, code, -1)
        chars = _BRAILLE[bits]
        text = Text(no_wrap=True, overflow="crop")
        rows, cols = bits.shape
        for y in range(rows):
            line = "".join(chars[y])
            row = code[y]
            cuts = [0, *(np.flatnonzero(np.diff(row)) + 1).tolist(), cols]
            for a, b in zip(cuts, cuts[1:]):
                c = int(row[a])
                text.append(line[a:b], style=None if c < 0 else self._style(c))
            if y < rows - 1:
                text.append("\n")
        return text

    def render(self):
        return self.text


class TuiUi:
    """The interface the engine talks to. Everything goes into the transcript; errors also show as a toast."""

    def __init__(self, app):
        self.app = app

    def _log(self, text, role="foreground", bold=False):
        self.app.log_line(text, role, bold)

    def agent(self, who, text):
        self._log(("" if who == "haiku" else "· ") + text, "accent2" if who == "haiku" else "dim")

    def you(self, text):
        self._log(f"you  {text}", "accent3")

    def dim(self, text):
        self._log(text, "dim")

    def status(self, text):
        self._log(f"· {text}", "dim")

    def error(self, text):
        self._log(f"! {text}", "error")
        self.app.toast(text)

    def card(self, d):
        self._log(f"▌ {d.title}", "accent", bold=True)
        self._log(d.body.strip(), "foreground")

    def result(self, task, text, res):
        ok = res.get("subtype") == "success"
        self._log(f"{'✔' if ok else '✘'} {task.id} {'round finished' if ok else 'stopped with a problem'}",
                  "accent3" if ok else "error", bold=True)
        self._log(text, "foreground")


class VoiceApp(App):
    CSS = """
    Screen { layout: vertical; background: ansi_default; }
    #orb { height: 1fr; min-height: 5; }
    #status { height: 1; content-align: center middle; text-align: center; }
    #hints { height: 1; content-align: center middle; text-align: center; }
    #log { height: 2fr; display: none; border-top: solid $panel; padding: 0 2; scrollbar-size: 0 0; }
    #log.shown { display: block; }
    #input { display: none; border: none; height: 1; padding: 0 2; }
    #input.shown { display: block; }
    """
    BINDINGS = [
        Binding("space", "mute", "mute", show=False),
        Binding("t", "transcript", "transcript", show=False),
        Binding("i", "type", "type", show=False),
        Binding("escape", "escape", "stop", show=False),
        Binding("a", "decide('a')", show=False),
        Binding("r", "decide('r')", show=False),
        Binding("e", "decide('e')", show=False),
        Binding("m", "decide('m')", show=False),
        Binding("p", "toggle_mode", "mode", show=False),
        Binding("o", "show_output", "output", show=False),
        Binding("q", "quit_app", "quit", show=False),
        Binding("ctrl+c", "quit_app", "quit", show=False, priority=True),
    ]

    def __init__(self, opts):
        super().__init__(ansi_color=True)  # let kitty's own background (and its opacity) show through
        self.theme_ = Theme()
        self.prefs = load_prefs()
        self.engine = Engine(TuiUi(self), opts)
        self.engine.input_mode = self.chosen_mode()
        self.ptt_key = self.chosen_ptt_key()
        self.keys = None  # ptt_keys.KeyTracker once the terminal's key releases are being read
        self.flash_until = 0.0
        self.toast_text, self.toast_until = "", 0.0
        self.quit_armed_until = 0.0

    def compose(self) -> ComposeResult:
        yield Orb(self, id="orb")
        yield Static(id="status")
        yield Static(id="hints")
        yield RichLog(id="log", wrap=True, markup=False, max_lines=2000)
        yield Input(id="input", placeholder="type a message · Enter sends · Esc closes")

    # ---- input mode ------------------------------------------------------------------------------------
    def chosen_mode(self):
        """The config's input.mode, unless you switched with `p` since the config last said something different."""
        configured = self.engine.input_mode
        saved = self.prefs.get("input_mode")
        if saved in ptt.MODES and self.prefs.get("input_mode_config") == configured:
            return saved
        return configured

    def chosen_ptt_key(self):
        key = str(config.get()["input"]["ptt_key"]).strip().lower() or "v"
        if key in RESERVED_KEYS:
            cvlog.log.event("ptt", f"input.ptt_key {key!r} is already a cv key; using 'v'")
            return "v"
        return key

    @property
    def ptt_mode(self):
        return self.engine.input_mode == ptt.PTT

    @property
    def release_events(self):
        """True when this terminal reports key releases, so the key can be held."""
        return bool(self.keys and self.keys.release_supported)

    def action_toggle_mode(self):
        if not self.engine.ready:
            return
        new = ptt.other_mode(self.engine.input_mode)
        if self.keys:
            self.keys.reset()
        self.engine.set_input_mode(new)
        cvlog.log.event("key", f"p → {new}")
        self.prefs["input_mode"] = new
        self.prefs["input_mode_config"] = config.get()["input"]["mode"]
        save_prefs(self.prefs)

    def _start_key_releases(self):
        """Ask the terminal for key-release events (kitty keyboard protocol) so the talk key can be held."""
        loop = asyncio.get_running_loop()
        tracker = ptt_keys.KeyTracker(
            self.ptt_key, lambda kind: loop.call_soon_threadsafe(self._key_event, kind),
            is_active=self._key_active, release_supported=ptt_keys.terminal_probably_reports_releases())
        if tracker.codepoint is None or not ptt_keys.install(tracker):
            return
        try:
            self._driver.write(ptt_keys.ENABLE_EVENT_TYPES)
            self._driver.flush()
        except Exception as e:  # noqa: BLE001 - no escape-sequence driver (headless, Windows console): toggle fallback
            cvlog.log.event("ptt", f"key releases not requested: {e}")
            return
        self.keys = tracker

    def _key_active(self):
        return self.ptt_mode and self.engine.ready and not self.query_one("#input").has_class("shown")

    def _key_event(self, kind):
        (self.engine.ptt_down if kind == "down" else self.engine.ptt_up)()

    def on_key(self, event):
        """The talk key, when the terminal gives no releases: press to start, press again to stop."""
        if event.key != self.ptt_key or not self._key_active() or self.release_events:
            return
        event.stop()
        self.engine.ptt_toggle()

    def on_unmount(self):
        ptt_keys.uninstall()

    def on_app_blur(self):
        if self.keys and self.keys.reset():  # the release would have gone to another window
            cvlog.log.event("ptt", "window lost focus while the key was held")

    async def on_mount(self):
        self._start_key_releases()
        if self.prefs.get("transcript"):
            self.query_one("#log").add_class("shown")
        self.set_interval(0.2, self.update_status)
        self.set_interval(2.0, self.reload_theme)
        self.run_worker(self.start_engine(), exclusive=True)

    async def start_engine(self):
        try:
            await self.engine.start()
        except Exception as e:  # noqa: BLE001 - show it plainly instead of a traceback; q still quits
            cvlog.log.event("startup-error", repr(e))
            self.toast(str(e) if isinstance(e, StartupError) else f"Couldn't start: {e}", seconds=3600)
            return
        await self.engine.exited.wait()  # "bye" by voice
        await self.shutdown()

    # ---- status line -----------------------------------------------------------------------------------
    def update_status(self):
        e = self.engine
        now = time.monotonic()
        state = e.state
        words, detail, hints = {
            "waking": ("waking up…", "", ""),
            "talking": ("listening", "", ""),
            "speaking": ("", "", ""),
            "thinking": ("thinking…", "", ""),
        }.get(state, ("listening", "", "space mute · p push to talk · t text · i type · o output · q quit"))
        if e.ready and e.input_mode == ptt.PTT:
            hints = hints.replace("p push to talk", "p always listening")
            k = self.ptt_key.upper() if len(self.ptt_key) == 1 else self.ptt_key
            hold = self.release_events  # else: press to start, press again to stop
            if state == "listening":
                words = f"hold {k} to talk" if hold else f"press {k} to talk"
                if not hold:
                    hints = hints.replace("space mute · ", f"space mute · {k} again stops · ")
            elif state == "talking":
                hints = f"release {k} to send" if hold else f"press {k} to send"
        if state == "waiting":
            d = e.app.pending
            words, detail = "your call", d.title.split("·", 1)[-1].strip() if d else ""
            hints = {"task": "a start · e edit · m model · r drop", "plan": "a approve · r change",
                     "tool": "a approve · r reject"}.get(d.kind if d else "", "answer out loud") + " · or just say it"
        elif state == "working":
            t = e.active_tasks[0]
            words, detail = "working", (t.digest[-1] if t.digest else t.title)
        if e.ready and e.muted:  # the orb animates as usual; only the status line shows it
            if state == "listening":
                words, detail = "muted", ""
            else:
                detail = "muted" + (f" · {detail}" if detail else "")
            hints = "space unmute · " + (hints.split(" · ", 1)[1] if hints.startswith("space mute") else hints or "q quit")
        quitting = now < self.quit_armed_until
        if quitting:
            words, detail, hints = "work is still running", "", "q again to stop it and quit"
        status = Text(justify="center", no_wrap=True, overflow="ellipsis")
        if now < self.toast_until and not quitting:
            status.append(self.toast_text, style=f"bold {self._hex('error')}")
        else:
            status.append(words, style=self._hex("foreground" if state in ("talking", "waiting") else "dim"))
            if detail:
                status.append(f" · {detail[:60]}", style=self._hex("dim"))
        key = (status.plain, str(status.spans), hints, self.theme_.mtime)
        if key != getattr(self, "_status_key", None):  # repaint only when it changed
            self._status_key = key
            self.query_one("#status", Static).update(status)
            self.query_one("#hints", Static).update(Text(hints, style=f"{self._hex('dim')} dim", justify="center"))

    def _hex(self, role):
        return self.theme_.hex(role)

    def reload_theme(self):
        if self.theme_.reload():
            self.update_status()

    def toast(self, text, seconds=5):
        self.toast_text, self.toast_until = text, time.monotonic() + seconds
        self.flash_until = time.monotonic() + 0.4

    def log_line(self, text, role, bold=False):
        style = self._hex(role) + (" bold" if bold else "")
        self.query_one("#log", RichLog).write(Text(text, style=style))

    # ---- keys ------------------------------------------------------------------------------------------
    def action_mute(self):
        self.engine.set_muted(not self.engine.muted)
        cvlog.log.event("key", f"space → muted={self.engine.muted}")

    def action_transcript(self):
        log = self.query_one("#log")
        log.toggle_class("shown")
        cvlog.log.event("key", f"t → transcript={log.has_class('shown')}")
        self.prefs["transcript"] = log.has_class("shown")
        save_prefs(self.prefs)

    def action_type(self, prefill=""):
        box = self.query_one("#input", Input)
        box.add_class("shown")
        box.value = prefill
        box.cursor_position = len(prefill)
        box.focus()

    def _close_input(self):
        box = self.query_one("#input", Input)
        box.remove_class("shown")
        box.value = ""
        self.set_focus(None)

    def action_escape(self):
        cvlog.log.event("key", "esc")
        if self.query_one("#input").has_class("shown"):
            self._close_input()
        else:
            self.engine.stop_talking()

    async def on_input_submitted(self, event: Input.Submitted):
        line = event.value.strip()
        cvlog.log.event("typed", line)
        self._close_input()
        if line and self.engine.ready:
            await self.engine.typed(line)

    async def action_decide(self, key):
        cvlog.log.event("key", key)
        e = self.engine
        d = e.app.pending if e.ready else None
        if not d or d.kind == "question":
            return
        if key == "a":
            await e.decision_key(d, "a")
        elif key == "r":
            self.action_type("r ")  # optional note, Enter to send
        elif key == "e" and d.kind == "task":
            self.run_worker(e.decision_key(d, "e"))
        elif key == "m" and d.kind == "task":
            self.action_type("m ")

    def action_show_output(self):
        cvlog.log.event("key", "o")
        if self.engine.ready and (err := self.engine.app.show_output()):
            self.toast(err)

    async def action_quit_app(self):
        cvlog.log.event("key", "q")
        if self.engine.ready and self.engine.active_tasks and time.monotonic() > self.quit_armed_until:
            self.quit_armed_until = time.monotonic() + 5  # first press: ask
            return
        await self.shutdown()

    async def shutdown(self):
        await self.engine.shutdown()
        self.exit()


def main():
    platform_util.utf8_console()
    opts = parse_args()
    lock = platform_util.acquire_lock(LOCK_FILE)  # one copy at a time, so two never listen to the mic
    if lock is None:
        print("Claude Voice is already running.", file=sys.stderr)
        sys.exit(1)
    if opts.plain:
        import voice_term
        try:
            if asyncio.run(voice_term.main(opts)):
                sys.exit(RESTART_EXIT)
        except KeyboardInterrupt:
            pass
        return
    app = VoiceApp(opts)
    try:
        app.run()
    except Exception as e:
        import traceback
        cvlog.log.event("CRASH", "".join(traceback.format_exception(e)))
        raise
    cvlog.log.event("exit", f"restart={app.engine.restart_requested} return_code={app.return_code}")
    if app.engine.restart_requested:
        sys.exit(RESTART_EXIT)  # scripts/cv runs the pending command, then starts the app again


if __name__ == "__main__":
    main()
