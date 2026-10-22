"""Key press AND release inside the terminal window, for push to talk.

Terminals that speak the kitty keyboard protocol (kitty, WezTerm, foot, Ghostty, ...) can report key releases, but
Textual neither asks for them nor copes with them. This module asks (`enable_sequence`) and patches Textual's parser
so that:

- release events and key-repeat events never reach Textual as bogus keys (a repeat is turned back into a plain press, a
  release is dropped), and
- presses/releases of the push-to-talk key go to a KeyTracker, which tells the app "down" and "up".

Terminals without the protocol (gnome-terminal, Terminal.app, Windows Terminal ...) report no releases. The tracker then
says so (`release_supported` stays False) and the app falls back to "press to start, press again to stop".
The patch touches a private Textual method and is guarded: install() returns False if Textual has changed.
"""

import os
import re

# CSI > ... u pushed Textual's flags (disambiguate, all keys, text); this ORs in "report event types" (bit 2).
ENABLE_EVENT_TYPES = "\x1b[=2;2u"

# ESC [ code [; modifiers[:event] [; text]] final   (codes may carry :alternates)
_KITTY = re.compile(r"\x1b\[([\d:]*)(?:;([\d]*)(?::(\d+))?(?:;([\d:]*))?)?([u~ABCDEFHPQRS])")
_IGNORED_MODS = 1 | 64 | 128  # shift, caps lock, num lock may be down while talking
_NAMED = {"space": 32, "enter": 13, "tab": 9}

# Terminals known to implement the keyboard protocol with event types.
_KNOWN = ("kitty", "wezterm", "ghostty", "foot")


def codepoint_of(key):
    """The kitty codepoint of a config key name ('v', 'space'), or None if it can't be tracked in the window."""
    key = (key or "").lower()
    if key in _NAMED:
        return _NAMED[key]
    return ord(key) if len(key) == 1 else None


def terminal_probably_reports_releases(environ=None):
    env = os.environ if environ is None else environ
    forced = env.get("CV_PTT_RELEASE")
    if forced in ("0", "1"):
        return forced == "1"
    ident = " ".join([env.get("TERM", ""), env.get("TERM_PROGRAM", "")]).lower()
    return bool(env.get("KITTY_WINDOW_ID")) or any(k in ident for k in _KNOWN)


class KeyTracker:
    """Fed from Textual's input thread. `emit(kind)` ("down" | "up") is called from that thread, so give it something
    thread-safe (loop.call_soon_threadsafe)."""

    def __init__(self, key, emit, is_active=lambda: True, release_supported=False):
        self.codepoint = codepoint_of(key)
        self.emit = emit
        self.is_active = is_active  # False → the key is an ordinary key right now (typing in the input box, ...)
        self.release_supported = release_supported  # becomes True when the terminal proves it by sending an event type
        self.down = False

    def seen_event_type(self):
        self.release_supported = True

    def handle(self, code, mods, etype):
        """One decoded key event → True if it was the talk key and has been consumed (Textual must not see it)."""
        if self.codepoint is None or code != self.codepoint or (mods - 1) & ~_IGNORED_MODS:
            return False
        if etype == 3:  # release: always honoured, even if the key stopped being active mid-hold
            if self.down:
                self.down = False
                self.emit("up")
            return True
        if not (self.release_supported and self.is_active()):
            return False  # no releases to wait for: the app toggles on the plain press instead
        if etype == 2 or self.down:
            return True  # auto-repeat while held
        self.down = True
        self.emit("down")
        return True

    def reset(self):
        """Window lost focus: the release will go to someone else. → True if a hold was cut short."""
        was, self.down = self.down, False
        if was:
            self.emit("up")
        return was


_tracker = None
_patched = False


def rewrite(sequence, tracker=None):
    """What Textual should parse instead of `sequence`: the same sequence without the event type; "" for a release
    (nothing to parse); the sequence itself if it isn't a kitty key report. Also feeds the tracker."""
    m = _KITTY.fullmatch(sequence)
    if not m:
        return sequence
    code, mods, etype, text, final = m.groups()
    tracker = tracker or _tracker
    first = code.split(":")[0]
    if tracker and etype is not None:
        tracker.seen_event_type()
    if tracker and final == "u" and first.isdigit() and tracker.handle(int(first), int(mods or 1), int(etype or 1)):
        return ""
    if etype is None:
        return sequence
    if int(etype) == 3:
        return ""
    return f"\x1b[{code};{mods or 1}" + (f";{text}" if text else "") + final


def install(tracker):
    """Patch Textual's parser (once). → True if events are now handled; False if Textual's internals aren't as expected,
    in which case nothing was changed and event types must not be enabled."""
    global _tracker, _patched
    _tracker = tracker
    if _patched:
        return True
    try:
        from textual import events
        from textual._xterm_parser import XTermParser
        from textual.keys import Keys
        original = XTermParser._parse_extended_key
        ignore = Keys.Ignore
    except (ImportError, AttributeError):
        return False

    def parse_extended_key(self, sequence):
        new = rewrite(sequence)
        if new == "":  # consumed: a release, or the talk key. The parser needs a key event to know it understood the
            return [events.Key(ignore, "")]  # sequence; this one is filtered out before the app sees it
        return original(self, new)

    XTermParser._parse_extended_key = parse_extended_key
    _patched = True
    return True


def uninstall():
    global _tracker
    _tracker = None
