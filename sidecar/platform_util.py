"""Everything that differs between Linux and Windows, in one place: where state lives, the single-instance lock, opening
a terminal window, the clipboard, finding `claude`, and killing a process tree. Linux behaviour is the original one
(kitty, wl-copy, ~/.local/state, flock); the Windows branches are untested on real Windows.

platformdirs and portalocker are imported lazily with a fallback, so an environment that predates them still runs.
"""

import errno
import hashlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile

APP = "claude-voice"
# Windows process-creation flags (numeric, so this module imports and can be tested on Linux).
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)


def is_windows():
    return sys.platform.startswith("win")  # (not os.name: tests switch platforms by patching sys.platform)


# ---- state ---------------------------------------------------------------------------------------------------
def state_dir():
    """CLAUDE_VOICE_STATE_DIR, else ~/.local/state/claude-voice (Linux) or %LOCALAPPDATA%\\claude-voice (Windows)."""
    env = os.environ.get("CLAUDE_VOICE_STATE_DIR")
    if env:
        return env
    if is_windows():
        try:
            import platformdirs
            return platformdirs.user_state_dir(APP, appauthor=False)
        except Exception:  # noqa: BLE001 - not installed (or None): use the folder it would have picked
            base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
            return os.path.join(base, APP)
    return os.path.expanduser("~/.local/state/claude-voice")


def config_path():
    """The config file. CLAUDE_VOICE_CONFIG (a file, or a folder that holds config.yaml), else
    ~/.config/claude-voice/config.yaml (Linux; XDG_CONFIG_HOME honoured), %APPDATA%\\claude-voice\\config.yaml (Windows),
    ~/Library/Application Support/claude-voice/config.yaml (macOS)."""
    env = os.environ.get("CLAUDE_VOICE_CONFIG")
    if env:
        env = os.path.expanduser(env)
        return os.path.join(env, "config.yaml") if os.path.isdir(env) or env.endswith(("/", os.sep)) else env
    return os.path.join(config_dir(), "config.yaml")


def config_dir():
    """The platform's per-user config folder for this app (no CLAUDE_VOICE_CONFIG override)."""
    home = os.path.expanduser("~")
    if is_windows():
        return os.path.join(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"), APP)
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", APP)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), APP)


def point_latest(root, target):
    """Make `root/latest` lead to `target`: a symlink, or on Windows without the privilege, a latest.txt holding the path."""
    latest = os.path.join(root, "latest")
    try:
        if os.path.islink(latest):
            os.unlink(latest)
        os.symlink(target, latest)
        return
    except OSError:
        pass
    try:
        with open(os.path.join(root, "latest.txt"), "w", encoding="utf-8") as f:
            f.write(target + "\n")
    except OSError:
        pass


def utf8_console():
    """Make stdin/stdout/stderr UTF-8 (Windows consoles default to cp1252, which garbles or crashes on emoji and box
    characters) and, on Windows, turn on ANSI escape handling for the raw-ANSI --plain UI."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):  # any of them may be None (pythonw)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # None, not a reconfigurable text stream (replaced by a test, or closed)
    if os.name == "nt":
        try:
            importlib.import_module("colorama").just_fix_windows_console()
        except (ImportError, AttributeError):
            os.system("")  # side effect: enables VT processing on the console


# ---- single instance -----------------------------------------------------------------------------------------
def lock_path():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "claude-voice.lock")


def acquire_lock(path=None):
    """Take an exclusive, non-blocking lock on `path` for the life of the process. → the open file (keep a reference
    to it), or None when another process holds the lock."""
    f = open(path or lock_path(), "w", encoding="utf-8")
    try:
        try:
            portalocker = importlib.import_module("portalocker")
        except ImportError:
            portalocker = None
        if portalocker:
            try:
                portalocker.lock(f, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:  # includes AlreadyLocked
                f.close()
                return None
        else:  # an environment without portalocker (Linux only)
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # BlockingIOError from flock
        f.close()
        return None
    return f


# ---- push-to-talk control socket ---------------------------------------------------------------------------------
def ptt_family():
    """The multiprocessing.connection family of the control socket: a named pipe on Windows, a Unix socket elsewhere."""
    return "AF_PIPE" if is_windows() else "AF_UNIX"


def ptt_address():
    """Where `cv ptt` reaches the running app: <state dir>/ptt.sock (Linux, macOS), or the named pipe
    \\\\.\\pipe\\claude-voice-ptt-<hash of the state dir> (Windows; pipes don't live in folders, so the hash keeps
    two state dirs apart). A Unix socket path is limited to ~104 characters, so a long state dir uses the temp dir."""
    sd = state_dir()
    tag = hashlib.sha1(os.path.abspath(sd).encode("utf-8")).hexdigest()[:10]
    if is_windows():
        return rf"\\.\pipe\{APP}-ptt-{tag}"
    path = os.path.join(sd, "ptt.sock")
    if len(path.encode("utf-8")) > 100:
        path = os.path.join(tempfile.gettempdir(), f"{APP}-ptt-{tag}.sock")
    return path


# ---- terminal windows ----------------------------------------------------------------------------------------
def default_editor():
    return os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if is_windows() else "vim")


def spawn_terminal(argv, cls, title=None, wait=False, cwd=None):
    """Run `argv` in a window of its own. wait=False: start it detached, → the Popen (raises OSError if it can't).
    wait=True: block until it ends (→ CompletedProcess), falling back to this terminal when no window tool exists.
    Linux: kitty --class <cls> [--title <title>]. Windows: Windows Terminal (wt) or a new console."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if is_windows():
        if wait:
            return subprocess.run(argv, cwd=cwd)  # wt returns at once, so run it right here and wait
        if shutil.which("wt"):
            # wt splits its command line into subcommands at every `;`, ours included ("Fix parser; add tests"):
            # `\;` is a literal one.
            cmd = ["wt.exe", "-w", "new", *(["--title", title] if title else []), *argv]
            cmd = cmd[:3] + [a.replace(";", "\\;") for a in cmd[3:]]
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            cmd, flags = list(argv), CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP  # (can't combine with DETACHED)
        return subprocess.Popen(cmd, cwd=cwd, creationflags=flags, **quiet)
    kitty = ["kitty", "--class", cls, *(["--title", title] if title else [])]
    if wait:
        if shutil.which("kitty"):
            return subprocess.run([*kitty, "-e", *argv], cwd=cwd)
        return subprocess.run(list(argv), cwd=cwd)
    return subprocess.Popen([*kitty, *argv], cwd=cwd, start_new_session=True, **quiet)  # outlives a cv restart


def focus_window(title):
    """Bring the window with exactly this title to the front (restoring it if minimised). → True if found.
    Windows only, where an open output window otherwise stays behind cv; Windows Terminal keeps the --title it was
    opened with. Elsewhere it does nothing (→ False)."""
    if not is_windows() or not title:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            n = user32.GetWindowTextLengthW(hwnd)
            if n == len(title) and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == title:
                    found.append(hwnd)
                    return False  # stop
            return True

        user32.EnumWindows(each, 0)
        if not found:
            return False
        if user32.IsIconic(found[0]):
            user32.ShowWindow(found[0], 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(found[0]))
    except (OSError, AttributeError):
        return False


def pid_alive(pid):
    """Is a process with this pid running? (os.kill(pid, 0) would terminate it on Windows, so ask the OS instead.)
    A zombie (exited, not yet reaped by its parent) counts as gone."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if is_windows():
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE  # a 64-bit handle must not be truncated to a C int
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - can't ask: assume it is alive rather than open a duplicate window
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    try:  # Linux: a zombie still answers kill(0)
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


# ---- clipboard -----------------------------------------------------------------------------------------------
def copy_text(text):
    """Put text on the clipboard. → True if a tool took it."""
    try:
        if is_windows():
            try:
                importlib.import_module("pyperclip").copy(text)
                return True
            except Exception:  # noqa: BLE001 - no pyperclip, or it failed: ask PowerShell
                pass
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if not ps:
                return False
            script = "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
            subprocess.run([ps, "-NoProfile", "-Command", script], input=text.encode(), check=True, timeout=10)
            return True
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


# ---- claude --------------------------------------------------------------------------------------------------
def claude_exe():
    """The claude executable: which() honours PATHEXT on Windows (claude.cmd / claude.exe). If it isn't found, the
    bare name, so the spawn fails with FileNotFoundError (see not_found_error)."""
    return shutil.which("claude") or "claude"


def not_found_error(e):
    """A FileNotFoundError that says what's missing (Windows' own message often doesn't name the program)."""
    return FileNotFoundError(errno.ENOENT, "claude not found on PATH (is Claude Code installed?)", "claude")


def kill_tree(proc):
    """Kill a child and what it started. On Windows proc.kill() would leave grandchildren running, so taskkill /T first."""
    if is_windows():
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except (OSError, ProcessLookupError):
        pass  # already gone


def new_group_flags():
    """creationflags for a child that should get its own process group (Windows); 0 elsewhere."""
    return CREATE_NEW_PROCESS_GROUP if is_windows() else 0
