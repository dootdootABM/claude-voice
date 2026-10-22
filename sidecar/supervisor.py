"""Claude Voice's launcher loop, for every platform (scripts/cv, cv.cmd and cv.ps1 all start this).

Runs tui.py with the arguments given here. If the app exits with RESTART_EXIT, a task asked for a restart: run
restart_job.py (the task's command, run while the app is down), then start the app again in the same terminal.
Any other exit code ends the loop and becomes this program's exit code.
`cv ptt ...` is not an app start: it runs ptt_cli.py once, which sends the command to the running app.

Pure standard library, and it imports nothing from the app: agents.py pulls in the whole audio/Claude stack.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESTART_EXIT = 75  # must equal agents.RESTART_EXIT (test_supervisor.py asserts it)
INTERRUPTED = 130  # the shell convention for "ended by Ctrl+C"
GRACE_S = 10  # how long a child gets to finish after Ctrl+C before it is killed


def tool_dirs():
    """Folders where uv and claude usually live but which a launcher (Rofi, a shortcut) may not have on PATH."""
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, ".local", "bin")]
    if os.name == "nt":
        dirs += [os.path.join(os.environ.get("USERPROFILE") or home, ".local", "bin"),
                 os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs", "uv")]
    seen = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in seen:
            seen.append(d)
    return seen


def child_env(base=None):
    """The environment for the app: tool folders first on PATH (and UTF-8 mode on Windows, whose default is cp1252)."""
    env = dict(os.environ if base is None else base)
    env["PATH"] = os.pathsep.join(tool_dirs() + [env.get("PATH", "")])
    if os.name == "nt":
        env.setdefault("PYTHONUTF8", "1")
    return env


def python_command(script, args=(), env=None, sidecar=HERE):
    """argv running sidecar/<script> in the project's environment: `uv run --no-sync` when uv exists (never syncs,
    so the live .venv is left alone), else the interpreter running this supervisor."""
    env = env or os.environ
    uv = shutil.which("uv", path=env.get("PATH"))
    path = os.path.join(sidecar, script)
    if uv:
        return [uv, "run", "--quiet", "--no-sync", "--project", sidecar, "python", path, *args]
    return [sys.executable, path, *args]


def exit_status(code):
    """A process exit code as a shell would report it (killed by signal N → 128+N)."""
    return 128 - code if code < 0 else code


def run_child(argv, env=None):
    """Run argv attached to this terminal → its exit status. Ctrl+C reaches the child through the terminal; we wait
    for it (killing it after GRACE_S) instead of dying with a traceback."""
    proc = subprocess.Popen(argv, env=env)
    try:
        return exit_status(proc.wait())
    except KeyboardInterrupt:
        try:
            return exit_status(proc.wait(timeout=GRACE_S)) or INTERRUPTED
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
            return INTERRUPTED


def supervise(app_cmd, restart_cmd, restart_code=RESTART_EXIT, env=None):
    """The loop. app_cmd/restart_cmd are argv lists. → the exit code for the whole program."""
    while True:
        code = run_child(app_cmd, env)
        if code != restart_code:
            return code
        try:
            status = run_child(restart_cmd, env)
        except OSError:
            status = 1
        if status == INTERRUPTED:
            return INTERRUPTED  # Ctrl+C while the restart job ran: the user wants out
        if status != 0:
            print("  (restart job failed; starting Claude Voice anyway)", flush=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    env = child_env()
    if args and args[0] == "ptt":  # `cv ptt down|up|toggle`: talk to the running app, don't start another one
        return run_child(python_command("ptt_cli.py", args[1:], env), env)
    return supervise(python_command("tui.py", args, env), python_command("restart_job.py", (), env), env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(INTERRUPTED)
