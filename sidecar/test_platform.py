"""platform_util: the Linux and Windows branches, checked with mocks (nothing is spawned, the clipboard is never touched).
Run: pytest test_platform.py"""

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

import platform_util as pu

WIN = sys.platform == "win32"


def check(label, ok, detail=""):
    assert ok, f"{label}: {detail}" if detail else label


def on(platform):
    return mock.patch.object(sys, "platform", platform)


def which_only(*names):
    return lambda n: f"/x/{n}" if n in names else None



# ---- state dir
@pytest.mark.skipif(WIN, reason="asserts POSIX default state dir and paths")
def test_state_dir():
    tmp = tempfile.mkdtemp()
    with mock.patch.dict(os.environ, {"CLAUDE_VOICE_STATE_DIR": tmp}):
        check("state dir env override (linux)", pu.state_dir() == tmp)
        with on("win32"):
            check("state dir env override (windows)", pu.state_dir() == tmp)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_VOICE_STATE_DIR"}
    with mock.patch.dict(os.environ, env, clear=True):
        check("state dir linux default", pu.state_dir() == os.path.expanduser("~/.local/state/claude-voice"))
        with on("win32"), mock.patch.dict(sys.modules, {"platformdirs": None}), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\L"}):
            check("state dir windows, no platformdirs", pu.state_dir() == os.path.join(r"C:\L", "claude-voice"), pu.state_dir())
        fake = types.SimpleNamespace(user_state_dir=lambda app, appauthor=None: "/pd/" + app)
        with on("win32"), mock.patch.dict(sys.modules, {"platformdirs": fake}):
            check("state dir windows via platformdirs", pu.state_dir() == "/pd/claude-voice")



# ---- latest link and its fallback
@pytest.mark.skipif(WIN, reason="asserts a real symlink (needs a privilege on Windows); the fallback is covered by test_latest_fallback")
def test_latest_link():
    root = tempfile.mkdtemp()
    target = os.path.join(root, "20260101-000000")
    os.mkdir(target)
    pu.point_latest(root, target)
    check("latest symlink on linux", os.path.islink(os.path.join(root, "latest")) and not os.path.exists(os.path.join(root, "latest.txt")))


def test_latest_fallback():
    root = tempfile.mkdtemp()
    target = os.path.join(root, "20260101-000000")
    os.mkdir(target)
    root2 = tempfile.mkdtemp()
    with mock.patch("os.symlink", side_effect=OSError("no privilege")):
        pu.point_latest(root2, target)
    with open(os.path.join(root2, "latest.txt"), encoding="utf-8") as f:
        check("latest.txt fallback when symlink fails", f.read().strip() == target)



# ---- single-instance lock
def test_single_instance_lock():
    lp = os.path.join(tempfile.mkdtemp(), "t.lock")
    a = pu.acquire_lock(lp)
    b = pu.acquire_lock(lp)
    check("first lock taken, second refused", a is not None and b is None)
    a.close()
    c = pu.acquire_lock(lp)
    check("lock free again once released", c is not None)
    c.close()


@pytest.mark.skipif(WIN, reason="asserts a POSIX path")
def test_lock_path_xdg():
    with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}):
        check("lock path under XDG_RUNTIME_DIR", pu.lock_path() == "/run/user/1/claude-voice.lock")


def test_lock_path_fallback():
    with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}, clear=True):
        check("lock path falls back to the temp dir", pu.lock_path() == os.path.join(tempfile.gettempdir(), "claude-voice.lock"))


def test_tui_has_no_fcntl():
    src = (Path(__file__).resolve().parent / "tui.py").read_text(encoding="utf-8")
    check("tui has no fcntl", "fcntl" not in src)



# ---- terminal windows
def test_terminal_windows():
    with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run, on("linux"):
        pu.spawn_terminal(["py", "f.py"], "claude-voice-output", "cv · t1", cwd="/p")
        args, kw = popen.call_args
        check("linux window argv", args[0] == ["kitty", "--class", "claude-voice-output", "--title", "cv · t1", "py", "f.py"], args[0])
        check("linux window detached in own session", kw["start_new_session"] is True and kw["cwd"] == "/p")
        with mock.patch("shutil.which", which_only("kitty")):
            pu.spawn_terminal(["vim", "x.md"], "claude-voice-edit", wait=True)
            check("linux edit in kitty", run.call_args[0][0] == ["kitty", "--class", "claude-voice-edit", "-e", "vim", "x.md"], run.call_args[0][0])
        with mock.patch("shutil.which", which_only()):
            pu.spawn_terminal(["vim", "x.md"], "claude-voice-edit", wait=True)
            check("linux edit without kitty runs the editor here", run.call_args[0][0] == ["vim", "x.md"])

    with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run, on("win32"):
        with mock.patch("shutil.which", which_only("wt")):
            pu.spawn_terminal(["py", "f.py"], "c", "cv · t1")
            args, kw = popen.call_args
            check("windows wt argv", args[0] == ["wt.exe", "-w", "new", "--title", "cv · t1", "py", "f.py"], args[0])
            check("windows wt detached", kw["creationflags"] == pu.DETACHED_PROCESS | pu.CREATE_NEW_PROCESS_GROUP and "start_new_session" not in kw)
            pu.spawn_terminal(["py", "a;b"], "c", "cv · t1 · Fix parser; add tests")
            check("windows wt escapes ; (else it splits the command line)", popen.call_args[0][0] ==
                  ["wt.exe", "-w", "new", "--title", "cv · t1 · Fix parser\\; add tests", "py", "a\\;b"], popen.call_args[0][0])
        with mock.patch("shutil.which", which_only()):
            pu.spawn_terminal(["py", "f.py"], "c", "cv · t1")
            args, kw = popen.call_args
            check("windows without wt: new console", args[0] == ["py", "f.py"] and kw["creationflags"] & pu.CREATE_NEW_CONSOLE)
        with mock.patch("shutil.which", which_only("wt")):
            pu.spawn_terminal(["notepad", "x.md"], "c", wait=True)
            check("windows edit waits by running the editor directly", run.call_args[0][0] == ["notepad", "x.md"])
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ("VISUAL", "EDITOR")}, clear=True):
            check("windows default editor is notepad", pu.default_editor() == "notepad")
    with mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": ""}), on("linux"):
        check("linux default editor is vim", pu.default_editor() == "vim")



# ---- clipboard
def test_clipboard():
    with mock.patch("subprocess.run") as run, on("linux"):
        with mock.patch("shutil.which", which_only("wl-copy", "xclip")):
            check("copy: wl-copy preferred", pu.copy_text("hi") and run.call_args[0][0] == ["wl-copy"])
        with mock.patch("shutil.which", which_only("xclip")):
            check("copy: xclip fallback", pu.copy_text("hi") and run.call_args[0][0][0] == "xclip")
        with mock.patch("shutil.which", which_only("xsel")):
            check("copy: xsel fallback", pu.copy_text("hi") and run.call_args[0][0][0] == "xsel")
        with mock.patch("shutil.which", which_only()):
            check("copy: no tool -> False", pu.copy_text("hi") is False)
        with mock.patch("shutil.which", which_only("wl-copy")):
            run.side_effect = subprocess.CalledProcessError(1, "wl-copy")
            check("copy: tool failing -> False", pu.copy_text("hi") is False)
    with mock.patch("subprocess.run") as run, on("win32"):
        pyperclip = types.SimpleNamespace(copy=mock.Mock())
        with mock.patch.dict(sys.modules, {"pyperclip": pyperclip}):
            check("copy windows: pyperclip", pu.copy_text("hi") and pyperclip.copy.call_args[0][0] == "hi" and not run.called)
        with mock.patch.dict(sys.modules, {"pyperclip": None}), mock.patch("shutil.which", which_only("powershell")):
            ok = pu.copy_text("hi")
            check("copy windows: PowerShell fallback", ok and "Set-Clipboard" in run.call_args[0][0][-1] and run.call_args[1]["input"] == b"hi")
        with mock.patch.dict(sys.modules, {"pyperclip": None}), mock.patch("shutil.which", which_only()):
            check("copy windows: nothing available -> False", pu.copy_text("hi") is False)



# ---- claude
def test_claude():
    with mock.patch("shutil.which", lambda n: r"C:\bin\claude.CMD" if n == "claude" else None):
        check("claude_exe uses which (PATHEXT)", pu.claude_exe() == r"C:\bin\claude.CMD")
    with mock.patch("shutil.which", which_only()):
        check("claude_exe falls back to the bare name", pu.claude_exe() == "claude")
    e = pu.not_found_error(FileNotFoundError())
    check("not-found error names claude", isinstance(e, FileNotFoundError) and "claude" in str(e))
    import claude_proto
    with mock.patch.dict(os.environ, {"Anthropic_Api_Key": "k", "ANTHROPIC_BASE_URL": "u", "KEEP_ME": "1"}):
        env = claude_proto.subscription_env({"X": "y"})
        check("subscription env filter is case-insensitive", "Anthropic_Api_Key" not in env and "ANTHROPIC_BASE_URL" not in env
              and env["KEEP_ME"] == "1" and env["X"] == "y")



# ---- process-tree kill
def test_kill_tree():
    proc = mock.Mock(pid=42)
    with mock.patch("subprocess.run") as run, on("win32"):
        pu.kill_tree(proc)
        check("windows kill: taskkill /T /F then kill", run.call_args[0][0] == ["taskkill", "/T", "/F", "/PID", "42"] and proc.kill.called)
        check("windows gets its own process group", pu.new_group_flags() == pu.CREATE_NEW_PROCESS_GROUP)
    proc = mock.Mock(pid=42)
    with mock.patch("subprocess.run") as run, on("linux"):
        pu.kill_tree(proc)
        check("linux kill: just kill", not run.called and proc.kill.called and pu.new_group_flags() == 0)



# ---- projects and theme
def test_projects_and_theme():
    import projects
    import theme
    with on("linux"):
        import importlib
        importlib.reload(projects)
        check("linux project roots unchanged", len(projects.ROOTS) == 3 and "AppData" not in projects.SKIP)
    with on("win32"):
        importlib.reload(projects)
        check("windows project roots", len(projects.ROOTS) == 2 and {"AppData", "OneDrive"} <= projects.SKIP)
    importlib.reload(projects)
    th = theme.Theme(path=os.path.join(tempfile.mkdtemp(), "none.conf"))
    check("theme with a missing palette keeps defaults", th.hex("background") == theme.DEFAULTS["background"])
    bad = os.path.join(tempfile.mkdtemp(), "bad.conf")
    with open(bad, "wb") as f:
        f.write(b"\xff\xfe\x00bad")
    check("theme with an unreadable palette doesn't raise", theme.Theme(path=bad).hex("accent") == theme.DEFAULTS["accent"])


def test_focus_window_is_windows_only():
    with on("linux"), mock.patch("ctypes.WinDLL", create=True) as dll:
        check("linux: focus_window does nothing", pu.focus_window("cv · t1") is False and not dll.called)
