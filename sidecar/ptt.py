"""Push to talk, the parts that aren't audio: the control socket behind `cv ptt down|up|toggle`, and the optional
global hotkey (input.global_ptt_key).

Standard library only at import time. pynput and evdev are optional extras and are imported only when a global hotkey is
actually started, so everything else (and the tests) runs without them.
"""

import concurrent.futures
import os
import socket
import sys
import threading
from multiprocessing import connection

import platform_util

ALWAYS, PTT = "always_listening", "push_to_talk"
MODES = (ALWAYS, PTT)
COMMANDS = ("down", "up", "toggle", "status")


def other_mode(mode):
    return ALWAYS if mode == PTT else PTT


# ---- the control socket ----------------------------------------------------------------------------------------
class PttServer:
    """Listens on a local socket (a Unix socket, or a named pipe on Windows) for one-word commands and answers each
    with one line of text. `handler(command)` → the reply; it runs on `loop` when one is given (so it can touch
    asyncio state), else on the server's own thread. Messages are plain bytes, never pickles."""

    def __init__(self, handler, loop=None, address=None):
        self.handler, self.loop = handler, loop
        self.address = address or platform_util.ptt_address()
        self._listener = None
        self._thread = None
        self._stopping = False

    def start(self):
        family = platform_util.ptt_family()
        if family == "AF_UNIX":
            os.makedirs(os.path.dirname(self.address) or ".", exist_ok=True)
            try:
                os.unlink(self.address)  # a stale socket from a crashed run (only one app runs at a time)
            except FileNotFoundError:
                pass
        self._listener = connection.Listener(self.address, family)
        if family == "AF_UNIX":
            try:
                os.chmod(self.address, 0o600)  # only you may press the key
            except OSError:
                pass
        self._thread = threading.Thread(target=self._serve, name="ptt-socket", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stopping:
            try:
                conn = self._listener.accept()
            except (OSError, EOFError):
                if self._stopping:
                    return
                continue
            try:
                with conn:
                    if conn.poll(2):
                        conn.send_bytes(self._dispatch(conn.recv_bytes(256).decode("utf-8", "replace")).encode("utf-8"))
            except (OSError, EOFError):
                pass  # the client went away

    def _dispatch(self, text):
        cmd = text.strip().lower()
        if cmd not in COMMANDS:
            return f"error: unknown command {cmd!r} (use: {', '.join(COMMANDS)})"
        try:
            if self.loop is None:
                return str(self.handler(cmd))
            done = concurrent.futures.Future()

            def run():
                try:
                    done.set_result(str(self.handler(cmd)))
                except Exception as e:  # noqa: BLE001 - the reply is the error, the app carries on
                    done.set_exception(e)

            self.loop.call_soon_threadsafe(run)
            return done.result(timeout=3)
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    def stop(self):
        self._stopping = True
        if self._listener is None:
            return
        try:  # wake the thread out of accept()
            with connection.Client(self.address, platform_util.ptt_family()):
                pass
        except OSError:
            pass
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        self._listener = None


def send_command(cmd, address=None, timeout=3.0):
    """Send one command to the running app → its reply. Raises ConnectionError when the app isn't running."""
    address = address or platform_util.ptt_address()
    try:
        # (a plain socket would do on Unix, but a named pipe on Windows needs multiprocessing's client)
        with connection.Client(address, platform_util.ptt_family()) as conn:
            conn.send_bytes(cmd.encode("utf-8"))
            if not conn.poll(timeout):
                raise ConnectionError("Claude Voice did not answer")
            return conn.recv_bytes().decode("utf-8", "replace")
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as e:
        raise ConnectionError("Claude Voice isn't running (or is still starting)") from e
    except OSError as e:  # Windows: a missing pipe is FileNotFoundError or a WinError
        raise ConnectionError(f"Claude Voice isn't running ({e})") from e


# ---- the global hotkey ------------------------------------------------------------------------------------------
class HotkeyUnavailable(Exception):
    """The global hotkey can't be set up; the message says why and what to do."""


MOD_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt", "opt": "alt", "shift": "shift",
               "super": "super", "win": "super", "windows": "super", "cmd": "super", "command": "super",
               "meta": "super"}


def parse_hotkey(spec):
    """"ctrl+alt+v" → ({"ctrl", "alt"}, "v"). Raises ValueError for anything else."""
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    bad = [m for m in mods if m not in MOD_ALIASES]
    if bad or key in MOD_ALIASES:
        raise ValueError(f"{spec!r} is not a hotkey like 'ctrl+alt+v' (modifiers: ctrl, alt, shift, super)")
    return {MOD_ALIASES[m] for m in mods}, key


class ComboTracker:
    """Turns a stream of key press/release events (key names as the backends normalise them: 'ctrl', 'alt', 'shift',
    'super', 'v', 'space', 'f9' ...) into "down" (the combo was just completed) and "up" (it was broken). A plain
    single key such as 'v' is refused by the caller: it would fire on every 'v' typed anywhere."""

    def __init__(self, spec):
        self.mods, self.key = parse_hotkey(spec)
        self.pressed = set()
        self.active = False

    def feed(self, name, pressed):
        """→ "down", "up" or None."""
        if pressed:
            self.pressed.add(name)
        else:
            self.pressed.discard(name)
        if not self.active:
            if pressed and name == self.key and self.mods <= self.pressed:
                self.active = True
                return "down"
        elif not pressed and (name == self.key or name in self.mods):
            self.active = False
            return "up"
        return None


def _is_wayland():
    return sys.platform.startswith("linux") and (
        bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland")


class _PynputBackend:
    NAME = "pynput"

    def __init__(self, tracker, emit):
        try:
            from pynput import keyboard
        except Exception as e:  # noqa: BLE001 - ImportError, or on Linux a missing display
            raise HotkeyUnavailable(f"pynput isn't usable here ({e}). Install it with: pip install pynput "
                                    "(uv sync --extra hotkey)") from e
        self.keyboard, self.tracker, self.emit = keyboard, tracker, emit
        self.listener = keyboard.Listener(on_press=lambda k: self._on(k, True), on_release=lambda k: self._on(k, False))

    def name_of(self, key):
        kb = self.keyboard
        if isinstance(key, kb.Key):
            n = key.name
            for prefix, mod in (("ctrl", "ctrl"), ("alt", "alt"), ("shift", "shift"), ("cmd", "super")):
                if n.startswith(prefix):
                    return mod
            return n
        char = getattr(key, "char", None)
        if char:
            return chr(ord(char) + 96) if ord(char) < 32 else char.lower()  # ctrl+v arrives as '\x16'
        vk = getattr(key, "vk", None)
        return chr(vk).lower() if vk and 48 <= vk <= 90 else None  # letters/digits by virtual key code

    def _on(self, key, pressed):
        name = self.name_of(key)
        if name:
            self.emit(self.tracker.feed(name, pressed))

    def start(self):
        self.listener.start()
        self.listener.wait()

    def stop(self):
        self.listener.stop()


class _EvdevBackend:
    NAME = "evdev"
    MODS = {"leftctrl": "ctrl", "rightctrl": "ctrl", "leftalt": "alt", "rightalt": "alt", "leftshift": "shift",
            "rightshift": "shift", "leftmeta": "super", "rightmeta": "super"}

    def __init__(self, tracker, emit):
        try:
            import evdev
        except ImportError as e:
            raise HotkeyUnavailable("Reading the keyboard on Wayland needs the evdev package: pip install evdev "
                                    "(uv sync --extra hotkey-wayland).") from e
        self.evdev, self.tracker, self.emit = evdev, tracker, emit
        denied, self.devices = [], []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except PermissionError:
                denied.append(path)
                continue
            except OSError:
                continue
            if evdev.ecodes.EV_KEY in dev.capabilities() and evdev.ecodes.KEY_A in dev.capabilities()[evdev.ecodes.EV_KEY]:
                self.devices.append(dev)
            else:
                dev.close()
        if not self.devices:
            if denied or not evdev.list_devices():
                raise HotkeyUnavailable(
                    "Cannot read the keyboard (/dev/input/event*), which a global push-to-talk key needs on Wayland. "
                    "Add yourself to the 'input' group: sudo usermod -aG input $USER, then log out and back in. "
                    "Note that any program you run can then read your keystrokes.")
            raise HotkeyUnavailable("No keyboard found under /dev/input for the global push-to-talk key.")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ptt-evdev", daemon=True)

    def _name(self, code):
        names = self.evdev.ecodes.KEY.get(code)
        name = (names[0] if isinstance(names, list) else names or "").removeprefix("KEY_").lower()
        return self.MODS.get(name, name)

    def _run(self):
        import select
        by_fd = {d.fd: d for d in self.devices}
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(list(by_fd), [], [], 0.3)
            except (OSError, ValueError):
                return
            for fd in ready:
                try:
                    for ev in by_fd[fd].read():
                        if ev.type == self.evdev.ecodes.EV_KEY and ev.value in (0, 1):  # 2 = auto-repeat
                            self.emit(self.tracker.feed(self._name(ev.code), bool(ev.value)))
                except OSError:  # unplugged
                    by_fd.pop(fd, None)
            if not by_fd:
                return

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)
        for d in self.devices:
            d.close()


class GlobalHotkey:
    """A system-wide push-to-talk key. `on_down()` / `on_up()` are called on `loop`. Use start(); raises
    HotkeyUnavailable (with advice) when it can't work."""

    def __init__(self, backend):
        self.backend = backend

    @classmethod
    def start(cls, spec, loop, on_down, on_up, backends=None):
        tracker = ComboTracker(spec)
        if not tracker.mods:
            raise HotkeyUnavailable(f"input.global_ptt_key {spec!r} has no modifier: a bare key would trigger on every "
                                    "key you type in other apps. Use something like 'ctrl+alt+v'.")

        def emit(event):
            if event == "down":
                loop.call_soon_threadsafe(on_down)
            elif event == "up":
                loop.call_soon_threadsafe(on_up)

        if backends is None:
            if sys.platform.startswith("linux"):
                backends = [_EvdevBackend] if _is_wayland() else [_PynputBackend, _EvdevBackend]
            else:
                backends = [_PynputBackend]
        errors = []
        for make in backends:
            try:
                backend = make(tracker, emit)
                backend.start()
                return cls(backend)
            except HotkeyUnavailable as e:
                errors.append(str(e))
            except Exception as e:  # noqa: BLE001 - e.g. macOS without the Accessibility permission
                errors.append(f"{make.NAME if hasattr(make, 'NAME') else make}: {e}")
        raise HotkeyUnavailable(" / ".join(errors))

    def stop(self):
        try:
            self.backend.stop()
        except Exception:  # noqa: BLE001
            pass
