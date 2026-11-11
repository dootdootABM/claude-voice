"""Approval modes (ask, accept edits, auto) and what each lets through, without models: no claude, no checker.

Run: pytest test_approval_modes.py
"""

import asyncio
import json
import os
import types

import agents
import orchestrator

PROJECT = os.path.join(os.path.abspath(os.sep), "cv-test-project")


def backend(mode, go_ahead=()):
    b = object.__new__(agents.Backend)
    b.task = types.SimpleNamespace(id="t1", project_dir=PROJECT, digest=[], go_ahead=set(go_ahead), auto_edits=False,
                                   review_next_plan=False, prompt="the task", last_asked="", steps=0, check_cost=0.0)
    b.app = types.SimpleNamespace(mode=mode, ui=types.SimpleNamespace(dim=lambda m: None, status=lambda m: None))
    b.asked, b.checked = [], []

    async def ask_step(inp, verb, target, body, why, checker=""):
        b.asked.append(why)
        return {"behavior": "ask"}

    async def check_risky(name, inp, verb, target, why):
        b.checked.append(why)
        return b.checker_says, "reason"

    b._ask_step, b._check_risky, b.checker_says = ask_step, check_risky, True
    return b


def decide(b, name, inp):
    return asyncio.run(b._permission(name, inp, {}))["behavior"]


EDIT = ("Edit", {"file_path": os.path.join(PROJECT, "a.py"), "old_string": "a", "new_string": "b"})
BUILD = ("Bash", {"command": "make"})
LOOK = ("Bash", {"command": "pacman -Qs -q 'wacom|huion'; systemctl --user list-units | grep -iE 'a|b'; lsmod"})
INSTALL = ("Bash", {"command": "yay -S opentabletdriver"})
PUSH = ("Bash", {"command": "git push"})
RM_INSIDE = ("Bash", {"command": "rm -rf build"})
RM_OUTSIDE = ("Bash", {"command": "rm -rf ~/Downloads/old"})


def test_ask_mode_asks_for_changes_but_not_for_looking():
    b = backend("ask")
    assert decide(b, *LOOK) == "allow"
    assert decide(b, "Read", {"file_path": "/etc/hostname"}) == "allow"
    assert decide(b, *EDIT) == "ask" and decide(b, *BUILD) == "ask"
    assert decide(b, *INSTALL) == "ask" and not b.checked


def test_edits_mode_lets_edits_through_only():
    b = backend("edits")
    assert decide(b, *EDIT) == "allow"
    assert decide(b, *BUILD) == "ask"
    assert decide(b, *INSTALL) == "ask" and not b.checked


def test_auto_mode_runs_everything_and_checks_risky_steps():
    b = backend("auto")
    assert decide(b, *EDIT) == "allow" and decide(b, *BUILD) == "allow"
    assert decide(b, *INSTALL) == "allow" and b.checked == ["it installs or removes packages"]
    assert decide(b, *RM_INSIDE) == "allow"
    assert not b.asked


def test_auto_mode_asks_when_the_checker_is_unsure():
    b = backend("auto")
    b.checker_says = False
    assert decide(b, *INSTALL) == "ask" and b.asked == ["it installs or removes packages"]
    b.checker_says = None
    assert decide(b, *INSTALL) == "ask"


def test_auto_mode_always_asks_for_the_hard_list():
    b = backend("auto")
    for step in (PUSH, RM_OUTSIDE, ("Bash", {"command": "sudo pacman -Syu"}),
                 ("Bash", {"command": "reboot"}), ("Read", {"file_path": os.path.expanduser("~/.ssh/id_ed25519")})):
        assert decide(b, *step) == "ask", step
    assert not b.checked


def test_request_that_names_the_risk_still_goes_ahead():
    b = backend("ask", go_ahead={"push", "it"})
    assert decide(b, *PUSH) == "allow"


def test_mode_is_saved_and_cycles(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "MODE_FILE", str(tmp_path / "approval.json"))
    assert agents.load_mode() == "ask"  # nothing saved yet
    app = object.__new__(agents.App)
    app.mode, app.front_notes = agents.load_mode(), []
    app.ui = types.SimpleNamespace(status=lambda m: None)
    assert app.cycle_mode() == "edits" and app.cycle_mode() == "auto" and app.cycle_mode() == "ask"
    assert app.front_notes  # Haiku hears about keyboard switches
    assert app.set_mode("full") == "auto" and app.full_approval  # the old name still works
    assert json.loads((tmp_path / "approval.json").read_text(encoding="utf-8"))["mode"] == "auto"
    assert agents.load_mode() == "auto"  # what the next launch starts in
    assert app.set_mode("nonsense") is None and app.mode == "auto"
    (tmp_path / "approval.json").write_text("{broken", encoding="utf-8")
    assert agents.load_mode() == "ask"


def test_read_only_knows_system_queries():
    for cmd in ("pacman -Qs x", "pacman -Si x", "paru -Qm", "systemctl status sshd", "journalctl -b -p err",
                "hyprctl -j clients", "lsusb; lsmod | grep -iE 'wacom|hid'", "pgrep -a 'otd|opentablet'"):
        assert orchestrator.read_only(cmd), cmd
    for cmd in ("pacman -Syu", "yay", "systemctl restart x", "journalctl --vacuum-size=1G", "hyprctl dispatch exit",
                "grep 'a|b' f > out", "echo \"$(rm -rf x)\""):
        assert not orchestrator.read_only(cmd), cmd


def test_package_queries_are_not_risky():
    for cmd in ("pacman -Qs x", "pacman -Ss x", "paru -Qm", "apt list --installed", "flatpak list"):
        assert agents.risk("Bash", {"command": cmd}, PROJECT) is None, cmd
    for cmd in ("pacman -S x", "pacman -Rns x", "yay", "yay -Syu", "apt install x", "flatpak install x"):
        assert agents.risk("Bash", {"command": cmd}, PROJECT) == "it installs or removes packages", cmd


def test_deletes_inside_the_project_only():
    assert agents.deletes_inside("rm -rf build dist", PROJECT)
    assert agents.deletes_inside("find build -delete", PROJECT)
    for cmd in ("rm -rf .", "rm -rf ../x", "rm -rf ~/x", "rm -rf $HOME/x", "rm -rf /", f"rm -rf {PROJECT}"):
        assert not agents.deletes_inside(cmd, PROJECT), cmd


def test_deletes_inside_judges_windows_paths():
    project = r"D:\cv-test-project"
    assert agents.deletes_inside(r"rm -rf D:\cv-test-project\build", project, nt=True)
    assert agents.deletes_inside(r"rm -rf build", project, nt=True)
    assert agents.deletes_inside(r'rm -rf "d:\CV-TEST-PROJECT\dist"', project, nt=True)  # case-insensitive
    for cmd in (r"rm -rf D:\cv-test-project", r"rm -rf D:\cv-test-project\..", r"rm -rf D:\\", r"rm -rf C:\x",
                r"rm -rf D:\cv-test-project-old", r"rm -rf %USERPROFILE%\x", r"rm -rf ..\other"):
        assert not agents.deletes_inside(cmd, project, nt=True), cmd
