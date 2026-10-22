"""supervisor (the launcher loop) with fake child programs, and restart_job's shell choice and a real POSIX run.
Nothing here starts the app. Run: pytest test_supervisor.py"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from unittest import mock

import pytest

import agents
import platform_util as pu
import restart_job
import supervisor

STATE = os.environ["CLAUDE_VOICE_STATE_DIR"]  # set by conftest before agents was imported: RESTART_FILE derives from it


def check(label, ok, detail=""):
    assert ok, f"{label}: {detail}" if detail else label


@pytest.fixture
def tmp(tmp_path):
    return str(tmp_path)


def script(tmp, name, body):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def py(path, *args):
    return [sys.executable, path, *args]


def loop(app, restart, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = supervisor.supervise(app, restart, **kw)
    return code, buf.getvalue()


def test_constants_and_command_building():
    check("restart code matches agents.RESTART_EXIT", supervisor.RESTART_EXIT == agents.RESTART_EXIT == 75)
    check("exit_status maps signals", supervisor.exit_status(-2) == 130 and supervisor.exit_status(3) == 3)
    cmd = supervisor.python_command("tui.py", ["--plain", "x y"], env={"PATH": os.path.dirname(sys.executable)}, sidecar="/s")
    check("no uv: falls back to this interpreter", cmd == [sys.executable, os.path.join("/s", "tui.py"), "--plain", "x y"], str(cmd))
    fake_bin = tempfile.mkdtemp()
    uv_name = "uv.exe" if os.name == "nt" else "uv"
    with open(os.path.join(fake_bin, uv_name), "w", encoding="utf-8") as f:
        f.write("")
    os.chmod(os.path.join(fake_bin, uv_name), 0o755)
    cmd = supervisor.python_command("tui.py", ["-a"], env={"PATH": fake_bin}, sidecar="/s")
    check("uv found: uv run --no-sync --project", cmd[1:7] == ["run", "--quiet", "--no-sync", "--project", "/s", "python"]
          and cmd[7:] == [os.path.join("/s", "tui.py"), "-a"] and os.path.basename(cmd[0]).startswith("uv"), str(cmd))
    with mock.patch.object(supervisor, "tool_dirs", lambda: ["/x/bin"]):
        check("child PATH has tool dirs first", supervisor.child_env({"PATH": "/usr/bin"})["PATH"].split(os.pathsep)[:2]
              == ["/x/bin", "/usr/bin"])


# ---- the loop
class Loop:
    def __init__(self, tmp):
        self.tmp = tmp
        self.marker = os.path.join(tmp, "restarted")
        self.count = os.path.join(tmp, "count")
        self.args_file = os.path.join(tmp, "args.json")
        self.fake_restart = script(tmp, "fake_restart.py", f"open({self.marker!r}, 'a').write('r')\n")
        self.failing_restart = script(tmp, "failing_restart.py", "import sys; sys.exit(1)\n")
        self.once = script(tmp, "once75.py", f"""import json, os, sys
c = {self.count!r}
n = int(open(c).read()) if os.path.exists(c) else 0
open(c, 'w').write(str(n + 1))
open({self.args_file!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')
sys.exit(75 if n == 0 else 0)
""")

    def reset(self):
        for p in (self.marker, self.count, self.args_file):
            if os.path.exists(p):
                os.remove(p)


@pytest.fixture
def lp(tmp):
    return Loop(tmp)


def test_loop_pass_through(lp, tmp):
    lp.reset()
    code, out = loop(py(script(tmp, "ok.py", "")), py(lp.fake_restart))
    check("exit 0 passes through, no restart", code == 0 and not os.path.exists(lp.marker))
    code, out = loop(py(script(tmp, "three.py", "import sys; sys.exit(3)")), py(lp.fake_restart))
    check("exit 3 passes through, no restart", code == 3 and not os.path.exists(lp.marker))


@pytest.mark.linux_only
def test_loop_sigterm(lp, tmp):
    code, out = loop(py(script(tmp, "sig.py", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")), py(lp.fake_restart))
    check("killed by SIGTERM reports 143", code == 143, str(code))


def test_loop_restart_once(lp):
    lp.reset()
    code, out = loop(py(lp.once, "--plain", "a b"), py(lp.fake_restart))
    runs = [json.loads(l) for l in open(lp.args_file, encoding="utf-8")]
    check("75 once: restart job ran, app relaunched, ends 0", code == 0 and open(lp.marker, encoding="utf-8").read() == "r" and len(runs) == 2)
    check("args pass through on every launch", runs == [["--plain", "a b"]] * 2, str(runs))
    check("no failure message when the job succeeds", "restart job failed" not in out)


def test_loop_failing_restart(lp):
    lp.reset()
    code, out = loop(py(lp.once), py(lp.failing_restart))
    check("failing restart job still relaunches", code == 0 and len(open(lp.args_file, encoding="utf-8").readlines()) == 2)
    check("failure message printed", "  (restart job failed; starting Claude Voice anyway)" in out, out)


def test_loop_restart_cannot_start(lp):
    lp.reset()
    code, out = loop(py(lp.once), ["/nonexistent/program"] if os.name != "nt" else ["nonexistent-program.exe"])
    check("restart job that cannot start still relaunches", code == 0 and "restart job failed" in out)


# ---- restart_job: shell choice
def test_restart_job_shell_choice():
    with mock.patch.object(pu, "is_windows", lambda: True):
        check("windows: shell=True with the string", restart_job.shell_command("echo hi") == ("echo hi", True))
    with mock.patch.object(pu, "is_windows", lambda: False):
        with mock.patch("shutil.which", lambda n: "/bin/bash" if n == "bash" else None):
            check("posix with bash", restart_job.shell_command("echo hi") == (["bash", "-c", "echo hi"], False))
        with mock.patch("shutil.which", lambda n: None):
            check("posix without bash uses sh", restart_job.shell_command("echo hi") == (["sh", "-c", "echo hi"], False))


# ---- restart_job: a real run (POSIX), and a timeout
def run_job(req):
    with open(agents.RESTART_FILE, "w", encoding="utf-8") as f:
        json.dump(req, f)
    with redirect_stdout(io.StringIO()):
        restart_job.main()
    with open(agents.RESTART_FILE, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.linux_only
def test_restart_job_real_run(tmp):
    check("RESTART_FILE is under the temp state dir", agents.RESTART_FILE.startswith(STATE), agents.RESTART_FILE)
    res = run_job({"reason": "test ✔", "command": "echo héllo ✔; echo err >&2; exit 4", "project_dir": tmp})
    check("real run keeps non-ASCII output", "héllo ✔" in res["output"], repr(res.get("output")))
    check("real run records exit code", res["exit_code"] == 4 and res["timed_out"] is False, str(res.get("exit_code")))
    res = run_job({"reason": "t", "command": "sleep 30", "timeout_s": 1})
    check("timeout recorded", res["timed_out"] is True and res["exit_code"] == -1 and res["duration_s"] < 15,
          str(res.get("duration_s")))
    res = run_job({"reason": "no command"})
    check("no command: file rewritten untouched", "exit_code" not in res)
