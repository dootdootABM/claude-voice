"""Runs between two launches of Claude Voice when a task asked for a restart (supervisor.py calls it on exit code 75).

Runs the task's run_while_down command, if any, in the task's folder with its time limit, and records the exit code
and the end of the output in the restart file. The relaunched app resumes the task's session with that result.
"""

import json
import os
import shutil
import subprocess
import sys
import time

import platform_util
from agents import RESTART_FILE

OUTPUT_TAIL = 6000  # characters of output handed back to the task


def shell_command(cmd):
    """→ (args, shell) for subprocess: cmd.exe on Windows, else bash (or sh when bash is missing)."""
    if platform_util.is_windows():
        return cmd, True
    return [("bash" if shutil.which("bash") else "sh"), "-c", cmd], False


def _text(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def run_command(cmd, cwd, timeout):
    """Run the shell command → (output, exit code, timed_out). On timeout the whole process tree is killed."""
    args, use_shell = shell_command(cmd)
    if not use_shell:  # POSIX encodes argv/cwd with the locale; under LC_ALL=C that rejects "é". Hand over UTF-8 bytes.
        args = [a.encode("utf-8") for a in args]
        cwd = cwd.encode("utf-8") if isinstance(cwd, str) else cwd
    proc = subprocess.Popen(args, shell=use_shell, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
        return (out or "") + (err or ""), proc.returncode, False
    except subprocess.TimeoutExpired as e:
        platform_util.kill_tree(proc)
        out, err = _text(e.stdout), _text(e.stderr)
        try:
            more_out, more_err = proc.communicate(timeout=5)
            out, err = out + (more_out or ""), err + (more_err or "")
        except subprocess.TimeoutExpired:
            pass  # something still holds the pipes; keep what we have
        return out + err, -1, True


def main():
    try:
        with open(RESTART_FILE, encoding="utf-8") as f:
            req = json.load(f)
    except (OSError, ValueError):
        return
    print(f"\n  Claude Voice is restarting — {req.get('reason', '')}", flush=True)
    cmd = req.get("command")
    if cmd:
        print(f"  running while it's down: {cmd}\n", flush=True)
        t0 = time.monotonic()
        out, code, timed_out = run_command(cmd, req.get("project_dir") or None, req.get("timeout_s", 600))
        req.update(exit_code=code, timed_out=timed_out, duration_s=time.monotonic() - t0, output=out[-OUTPUT_TAIL:])
        print(f"  done (exit code {code}{', timed out' if timed_out else ''}) — starting Claude Voice again…",
              flush=True)
    tmp = RESTART_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, indent=1)
    os.replace(tmp, RESTART_FILE)


if __name__ == "__main__":
    sys.exit(main())
