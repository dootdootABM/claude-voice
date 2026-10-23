"""The Haiku voice front end, back-end Claude Code tasks, and the decisions that gate everything.

Haiku is the only voice. When the back end needs a decision (its plan, or permission for a step), the app tells Haiku,
which explains it out loud and passes on the user's spoken answer with the `decide` tool. The app only accepts an
answer that quotes words from the user's latest message, so Haiku can't approve anything on its own.
"""

import asyncio
import collections
import contextvars
import difflib
import json
import ntpath
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

import config
import cvlog
import live
import platform_util
from audio import SentenceSplitter
from projects import find_projects, named_project
import sessions
from claude_proto import ClaudeProc, SdkMcpServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = cvlog.state_dir()
TASKS_FILE = os.path.join(STATE_DIR, "tasks.json")  # open tasks, so a later launch can resume them
RESTART_FILE = os.path.join(STATE_DIR, "restart.json")  # a restart a task asked for (read by scripts/cv's loop)
RESTART_EXIT = 75  # exit code meaning "restart me" to scripts/cv
OUTPUT_DIR = os.path.join(STATE_DIR, "outputs")  # each round's full result, as Markdown, for the output window
KEEP_OUTPUTS = 50
TOKENS_STEP = 100  # the live log's running token count is written when it has grown by this much
BRIEF_MAX = 4000  # characters of a subagent's prompt kept in the live log
QUIET_TOOLS = {"Read", "Glob", "Grep", "LS", "ToolSearch", "TodoWrite", "NotebookRead"}  # no permission line when allowed
_decided_by = contextvars.ContextVar("decided_by", default=None)  # {"by": ...} of the permission request being answered


def log_round(task, text, queued=False):
    """A message went to the task's session: log it as the start of a round (queued: it arrives mid-round)."""
    if not queued:
        task.round_t0 = time.monotonic()
    live.emit(task.live_key, "round_start", prompt=text, queued=True if queued else None,
              cwd=getattr(task, "project_dir", None))


def decided_by(who):
    """Record who is deciding the permission request being answered: user | auto | step_checker | rules | read_only."""
    box = _decided_by.get()
    if box is not None:
        box["by"] = who
HANDOVER_DIR = os.path.join(STATE_DIR, "handovers")  # notes carried from a long conversation into a fresh one
# A conversation this long gets slower, pricier and less focused (well before Sonnet's 1M window): suggest starting
# fresh at each of these sizes, once.
CONTEXT_NUDGES = (sessions.LONG, 300_000)
SAVED_DAYS = 7  # saved tasks older than this are dropped
IDLE_CLOSE_S = 600  # an idle task's claude process is closed after this long (it stays resumable)
# Tasks never speak; Haiku sums up each round of their work, at the length behaviour.verbosity asks for.
SUMMARY = {"quiet": "Tell the user what happened in one short sentence.",
           "normal": "Tell the user what was done, what's next, and anything that needs them, in one to three natural "
                     "sentences.",
           "chatty": "Tell the user what was done, the notable steps along the way, what's next and anything that "
                     "needs them, in up to five natural sentences."}
QUOTE = {"type": "string", "description": "The user's own words from their latest message that give this answer, "
                                          "copied exactly (e.g. \"yes go ahead\")."}
GO_AHEAD = {"type": "boolean", "description": "True when the request itself is a clear, specific instruction to do "
             "this (\"restart cv\", \"commit the changes\", \"run the tests\", \"push it\"), or a question or "
             "lookup that changes nothing (\"explain how X works\"), so asking again would be redundant: it starts at once without a plan or step checks. False for open-ended or design work, "
             "or if anything is unclear. Needs user_quote."}
SHOW = {"type": "boolean", "description": "True when the user wants a detailed answer to read, not just hear: an "
         "explanation, walkthrough, review, comparison or write-up (\"explain how X works\", \"walk me through\", "
         "\"show me\"). The full answer then opens in a window on their screen when it's ready."}
ROUTE = {"type": "string", "enum": ["quick", "job"], "description": "quick (default): one step, one question, "
          "an explanation, one area of the code; one session does it. job: several parts, a design choice or more "
          "than one area; an orchestrator plans it (the user approves the plan), hands each piece to a worker and "
          "checks every step. Takes longer, more accurate. ALWAYS a job: a request to read a file of instructions or a "
          "brief and carry it out; several separate changes or features; anything the user calls a job, a plan or "
          "a list of changes; anything touching more than one area of the code. Ask the user if it could go either way."}
BUDGET = {"type": "number", "description": "A job's usage cap (default 10; 20 = twice as much), only if the user "
          "asked for a bigger or smaller one. It's subscription usage, not money."}
TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Omit for a new task; reuse an id to revise that draft."},
        "title": {"type": "string"},
        "prompt": {"type": "string", "description": "Complete, self-contained instructions for doing the work."},
        "project_dir": {"type": "string", "description": "Absolute path or ~/path of the project."},
        "backend_model": {"type": "string", "enum": ["sonnet", "opus", "haiku"],
                          "description": "Default sonnet; opus only for hard design work."},
        "spoken_summary": {"type": "string", "description": "One sentence to say out loud."},
        "go_ahead": GO_AHEAD,
        "show_output": SHOW,
        "route": ROUTE,
        "budget_usd": BUDGET,
        "user_quote": QUOTE,
    },
    "required": ["title", "prompt", "project_dir", "spoken_summary"],
}
DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_id": {"type": "string"},
        "approve": {"type": "boolean"},
        "user_quote": QUOTE,
        "note": {"type": "string", "description": "Changes the user asked for, or their reason for saying no."},
        "scope": {"type": "string", "enum": ["once", "rest_of_task"],
                  "description": "rest_of_task only if the user said to stop asking about file edits for this task."},
    },
    "required": ["decision_id", "approve", "user_quote"],
}
CONTINUE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "message": {"type": "string", "description": "The follow-up, extra instruction or question, in the user's "
                                                     "terms. The work keeps its full context, so be brief."},
        "same_topic": {"type": "boolean", "description": "True only if this continues that task's own topic: a "
                       "follow-up, a clarification, a fix or next step for what it did. A different subject is false, "
                       "even in the same project (\"now explain the orb\" after a chat about the restart loop): that "
                       "gets a fresh conversation with propose_task."},
        "size": {"type": "string", "enum": ["small", "large"],
                 "description": "small: a follow-up, fix, question or status check on what that task did. large: "
                                "new work with several parts (several changes or features, a brief or file of "
                                "instructions to carry out); a quick task refuses it, start a job with propose_task."},
        "budget_usd": BUDGET,
        "plan_first": {"type": "boolean",
                       "description": "True if the user wants a plan to approve before any changes (\"let's plan "
                                      "first\", \"make a plan\", \"plan it out\")."},
        "go_ahead": GO_AHEAD,
        "show_output": SHOW,
        "user_quote": QUOTE,
    },
    "required": ["task_id", "message", "same_topic", "size", "user_quote"],
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_id": {"type": "string"},
        "answers": {"type": "array", "items": {"type": "string"},
                    "description": "One answer per question, in order: an option label or the user's own words."},
        "user_quote": QUOTE,
    },
    "required": ["decision_id", "answers", "user_quote"],
}


MODE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["ask", "full"],
                 "description": "full: the user gave blanket approval; ask: back to checking each plan and step."},
        "user_quote": QUOTE,
    },
    "required": ["mode", "user_quote"],
}

# Even with full approval, these still come back for a yes: hard to undo, outside the project, or outward-facing.
RISKY_COMMANDS = [
    (r"\bsudo\b|\bdoas\b", "it needs root"),
    (r"\b(kill|pkill|killall)\b", "it stops running processes"),
    (r"\b(pacman|yay|paru|apt|dnf|pip3?\s+install\s+--break|npm\s+i(nstall)?\s+-g)\b", "it installs or removes packages"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s|branch\s+-D|rebase)", "it rewrites or publishes git history"),
    (r"\b(npm|cargo|uv|twine)\s+publish\b", "it publishes a package"),
    (r"\bsystemctl\b[^;&|]*\b(enable|disable|start|stop|restart|mask|daemon-reload)\b|\bloginctl\b",
     "it changes system services"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(ba|z)?sh\b", "it runs a downloaded script"),
    (r"\b(dd|mkfs\S*|fdisk|parted|wipefs|shutdown|reboot|poweroff)\b", "it's a disk or power command"),
    (r"\bchmod\s+-R|\bchown\s+-R|>\s*/(etc|usr|boot)/", "it changes system files or permissions"),
]
# Windows (cmd and PowerShell) counterparts, matched case-insensitively and on any host: a model can emit them
# anywhere. They reuse the reasons above so IMPLIED_BY treats them alike. Deletes are handled in deletes().
_AT_COMMAND = (r"(?:^|[;&|(\n{}]|\bcmd(?:\.exe)?(?:\s+/[a-z])*?\s+/[ck]\s+|\s-c(?:ommand)?\s+"
               r"|\b(?:do|then|else|try)\s+|\bif\s+(?:not\s+)?(?:exist|defined)\s+\S+\s+|\berrorlevel\s+\d+\s+"
               r"|\)\s*)[\s\"']*")
_PS = r"(?:powershell|pwsh)(?:\.exe)?"
WINDOWS_RISKY_COMMANDS = [(re.compile(pat, re.I), why) for pat, why in [
    (_AT_COMMAND + r"(taskkill|Stop-Process)\b", "it stops running processes"),
    (_AT_COMMAND + r"wmic\b[^;&|]*\b(terminate|delete)\b", "it stops running processes"),
    (_AT_COMMAND + r"Start-Process\b[^;&|]*-Verb\s+RunAs\b", "it needs root"),
    (r"\b(winget|choco|scoop)(\.exe)?\s+(install|uninstall|remove|upgrade|update)\b", "it installs or removes packages"),
    (r"\bInvoke-Expression\b|" + _AT_COMMAND + r"iex\b|\bDownloadString\b|\b(irm|iwr|Invoke-RestMethod|Invoke-WebRequest)\b[^;&]*\|",
     "it runs a downloaded script"),
    # an encoded command or script file hides what it runs, so it can't be judged: ask
    (_AT_COMMAND + _PS + r"(?:\s+-(?!c\b|command\b)[A-Za-z]+(?::\S+)?(?:\s+[^-\s]\S*)?)*?\s+-(?:e|ec|en[a-z]*|f|fi[a-z]*)\b",
     "it runs a downloaded script"),
    (r"\bschtasks(\.exe)?\b|" + _AT_COMMAND + r"(sc(\.exe)?\s+(delete|stop|config|create)\b"
     r"|(Stop|Restart)-Service\b|net(\.exe)?\s+stop\b)", "it changes system services"),
    (_AT_COMMAND + r"(Set-ExecutionPolicy\b|netsh(\.exe)?\s+advfirewall\b|net(\.exe)?\s+user\b)",
     "it changes system files or permissions"),
    (_AT_COMMAND + r"(format(\.com)?\s+[A-Za-z]:|diskpart\b)|\b(bcdedit|cipher\s+/w|Format-Volume|Clear-Disk)\b"
     r"|\bshutdown(\.exe)?\s+/|\b(Restart|Stop)-Computer\b", "it's a disk or power command"),
    (r"\breg(\.exe)?\s+delete\b|\b(takeown|icacls)(\.exe)?\b", "it changes system files or permissions"),
]]
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")  # PowerShell is the shell tool's name in Claude Code on Windows
SENSITIVE_DIRS = ("~/.ssh", "~/.gnupg", "/etc", "/usr", "/boot", "/var")
BOUNDARY_SENSITIVE_DIRS = ("~/.aws", "~/.azure")  # new entries use the boundary-safe match
# Windows locations, compared with ntpath semantics (case-insensitive, / or \, `..` collapsed) and a path-boundary
# check: the directory itself or something under it, never a sibling that merely shares a prefix.
WINDOWS_SENSITIVE_DIRS = (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData")
WINDOWS_SENSITIVE_ENV = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA")
WINDOWS_SENSITIVE_HOME = (".ssh", ".gnupg", ".aws", ".azure")
WINDOWS_PATH = re.compile(r"^[A-Za-z]:|^\\\\|^//")  # a drive or UNC path, whatever the host


def _win_norm(p):
    return ntpath.normcase(ntpath.normpath(p))


def _win_under(p, d):
    """Boundary check on normalised Windows paths: d itself or anything below it."""
    return p == d or p.startswith(d.rstrip("\\") + "\\")


def _win_sensitive(path, env=None, state_dir=STATE_DIR):
    """True for a Windows system, program, app-data or key location. The app's own state dir (under
    %LOCALAPPDATA%) is carved out, so a write there gets the plain "outside the project" ask, as on Linux."""
    env = os.environ if env is None else env
    p = _win_norm(path)
    if state_dir and _win_under(p, _win_norm(state_dir)):
        return False
    dirs = list(WINDOWS_SENSITIVE_DIRS) + [env[k] for k in WINDOWS_SENSITIVE_ENV if env.get(k)]
    home = env.get("USERPROFILE") or os.path.expanduser("~")
    dirs += [ntpath.join(home, h) for h in WINDOWS_SENSITIVE_HOME]
    return any(_win_under(p, _win_norm(d)) for d in dirs)


def _tmp_only(paths, tempdir=None, win=False):
    """Every path is scratch space: under /tmp/ (POSIX), or on Windows under the system temp dir (case-insensitive,
    boundary-safe); never with `..`. `win`/`tempdir` let the Windows form run on any host. The temp dir is only
    trusted on Windows hosts (or when asked for), so a Linux $TMPDIR changes nothing."""
    winmode = win or os.name == "nt"
    if tempdir is None and winmode:
        tempdir = tempfile.gettempdir()

    def ok(p):
        if ".." in p:
            return False
        if not win and p.startswith("/tmp/"):
            return True
        return winmode and bool(tempdir) and _win_norm(p) != _win_norm(tempdir) \
            and _win_under(_win_norm(p), _win_norm(tempdir))
    return bool(paths) and all(ok(p) for p in paths)


# A delete verb anywhere in a segment (after `if exist x`, `do`, `{`, `(`, `cmd /c`, any powershell options...), not
# as part of a longer word, path or option. `ri` is PowerShell's alias for Remove-Item.
_WIN_DELETE = re.compile(r"(?<![\w\-./\\$%:])(del|erase|rd|rmdir|ri|remove-item)(?![\w\-.\\:])", re.I)
_DOTNET_DELETE = re.compile(r"\[(?:System\.)?IO\.(?:File|Directory)\]::Delete\(", re.I)
_WIN_OPT_WITH_VALUE = ("include", "exclude", "filter", "stream", "credential")


def _win_delete(cmd, rest, project_dir, tempdir):
    """Why a cmd/PowerShell delete (del, erase, rd /s, Remove-Item) needs a yes, or None. Judged like the POSIX
    ones: scratch space is fine, forced or recursive deletes elsewhere ask, and a plain delete asks unless every
    target is inside the project and not in a sensitive location."""
    cmd = cmd.lower()
    if cmd == "ri":
        cmd = "remove-item"
    if cmd in ("rd", "rmdir") and not re.search(r"(^|\s)/s\b", rest, re.I):
        return None  # without /s it only removes an empty directory, as rmdir on Linux
    try:
        tok = shlex.split(rest, posix=False)
    except ValueError:
        tok = rest.split()
    tok = [t.strip("\"'") for t in tok]
    targets, forced, skip = [], cmd in ("rd", "rmdir"), False
    for i, t in enumerate(tok):
        if skip:
            skip = False
        elif t.startswith("-") and len(t) > 1:  # PowerShell parameter
            name = t.lstrip("-").split(":")[0].lower()
            if name and any(w.startswith(name) for w in ("recurse", "force")):
                forced = True
            elif name and any(w.startswith(name) for w in ("path", "literalpath")) and i + 1 < len(tok):
                targets.append(tok[i + 1])
                skip = True
            elif name in _WIN_OPT_WITH_VALUE:
                skip = True
        elif re.fullmatch(r"/[A-Za-z?](:\S*)?", t):  # cmd switch such as /q /s /f /a:h
            forced = forced or t[1].lower() in "sqf"
        else:
            targets.append(t)
    tmp = _tmp_only(targets, tempdir, win=True)
    if forced:
        return None if tmp else "it deletes files recursively or forcibly"

    def inside(t):
        if not project_dir or re.search(r"[%$~(]|\.\.", t):
            return False
        base = _win_norm(project_dir)
        full = _win_norm(ntpath.join(project_dir, t))
        return full != base and _win_under(full, base) and not _win_sensitive(full)
    if tmp or (targets and all(inside(t) for t in targets)):
        return None
    return "it deletes files outside the project"


def deletes(cmd, project_dir=None, tempdir=None):
    """Why a shell command deletes files in a way that needs a yes, or None. Recursive/forced `rm` (flags in any
    position), `git rm`, `find -delete` and deletes from Python count; deleting only under /tmp (scratch files a
    task made itself) doesn't. cmd/PowerShell deletes (del, rd /s, Remove-Item) are judged by _win_delete."""
    if re.search(r"shutil\.rmtree|os\.(remove|unlink|rmdir|removedirs)\(|\.unlink\(|\.rmdir\(", cmd):
        return "it deletes files from a script"
    if _DOTNET_DELETE.search(cmd):
        return "it deletes files from a script"
    for seg in re.split(r"&&|\|\||[;&|\n]", cmd):  # cmd also chains with a single &
        if re.match(r"\s*[\"']?(echo|printf|grep|egrep|fgrep|rg|man|which|Write-Host|Write-Output)\b", seg, re.I) and not re.search(r"[({`$]", seg):
            continue  # the delete words are just text here (`echo rd /s`)
        for m in _WIN_DELETE.finditer(seg):
            rest = seg[m.end():].split("}")[0].strip().rstrip(")}").strip()
            if why := _win_delete(m.group(1), rest, project_dir, tempdir):
                return why
    for seg in re.split(r"&&|\|\||[;|\n]", cmd):
        try:
            tok = shlex.split(seg, comments=True)
        except ValueError:
            tok = seg.split()
        for i, t in enumerate(tok):
            git = t == "rm" and i > 0 and tok[i - 1] == "git"
            if t == "find" and "-delete" in tok[i:]:
                roots = [x for x in tok[i + 1:] if not x.startswith("-")][:1]
                if not _tmp_only(roots):
                    return "it deletes files (find -delete)"
            if t not in ("rm", "rmdir") or (t == "rmdir" and not git):
                continue
            args = tok[i + 1:]
            opts = [a for a in args if a.startswith("-")]
            targets = [a for a in args if not a.startswith("-")]
            if git:
                if "--cached" not in opts and not _tmp_only(targets):
                    return "it deletes files from the project (git rm)"
                continue
            forced = any(o in ("--recursive", "--force") or (not o.startswith("--") and re.search(r"[rRf]", o))
                         for o in opts)
            if forced and not _tmp_only(targets):
                return "it deletes files recursively or forcibly"
    return None


# A risky step still goes ahead without asking when the user's own request named it ("restart cv" → the restart,
# "push it" → git push). Reasons not listed here (downloaded scripts, disks, files outside the project) always ask.
DELETE_WORDS = {"delete", "remove", "rm", "clean", "cleanup", "wipe"}
IMPLIED_BY = {
    "it restarts the voice app": {"restart", "relaunch", "reload"},
    "it needs root": {"sudo", "root", "install", "uninstall", "upgrade", "update"},
    "it stops running processes": {"kill", "stop", "quit"},
    "it installs or removes packages": {"install", "uninstall", "remove", "upgrade", "update"},
    "it rewrites or publishes git history": {"push", "reset", "rebase"},
    "it publishes a package": {"publish", "release"},
    "it changes system services": {"service", "systemctl", "enable", "disable", "start", "stop", "restart"},
    "it changes system files or permissions": {"chmod", "chown", "permissions"},
    "it deletes files from a script": DELETE_WORDS,
    "it deletes files (find -delete)": DELETE_WORDS,
    "it deletes files from the project (git rm)": DELETE_WORDS,
    "it deletes files recursively or forcibly": DELETE_WORDS,
    "it deletes files outside the project": DELETE_WORDS,
}


def risk(name, inp, project_dir):
    """Why this step needs a yes even with full approval, or None."""
    if name.endswith("restart_voice_app"):
        return "it restarts the voice app"
    if name in SHELL_TOOLS:
        cmd = inp.get("command", "")
        return (deletes(cmd, project_dir)
                or next((why for pat, why in RISKY_COMMANDS if re.search(pat, cmd)), None)
                or next((why for pat, why in WINDOWS_RISKY_COMMANDS if pat.search(cmd)), None))
    path = inp.get("file_path") or inp.get("notebook_path")
    if name in EDIT_TOOLS and path:
        real = os.path.realpath(os.path.expanduser(path))
        if any(os.path.normcase(real).startswith(os.path.normcase(os.path.realpath(os.path.expanduser(d))))
               for d in SENSITIVE_DIRS):
            return "it changes a sensitive system or key file"
        home_dirs = [os.path.normcase(os.path.realpath(os.path.expanduser(d))) for d in BOUNDARY_SENSITIVE_DIRS]
        if any(os.path.normcase(real) == d or os.path.normcase(real).startswith(d + os.sep) for d in home_dirs):
            return "it changes a sensitive system or key file"
        if (os.name == "nt" or WINDOWS_PATH.match(path)) and _win_sensitive(real if os.name == "nt" else path):
            return "it changes a sensitive system or key file"
        if not os.path.normcase(real).startswith(os.path.normcase(os.path.realpath(project_dir)) + os.sep):
            return "it changes a file outside the project"
    return None


EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "stop_running_work": {"type": "boolean",
                              "description": "True only after the user confirmed they want to quit even though work "
                                             "is still running."},
        "user_quote": QUOTE,
    },
    "required": ["user_quote"],
}


FIND_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "Project name as the user said it, e.g. \"rmpc\"."}},
    "required": ["query"],
}
SESSIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "query": {"type": "string", "description": "What the user wants to pick up, in their words."},
    },
    "required": ["project_dir"],
}
RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "From find_sessions (or a propose_task reply)."},
        "project_dir": {"type": "string"},
        "title": {"type": "string", "description": "Short title for the task, e.g. \"thesis pretraining\"."},
        "message": {"type": "string", "description": "What the user wants now, to send into that conversation."},
        "plan_first": CONTINUE_SCHEMA["properties"]["plan_first"],
        "go_ahead": GO_AHEAD,
        "show_output": SHOW,
        "user_quote": QUOTE,
    },
    "required": ["session_id", "project_dir", "title", "message", "user_quote"],
}
CLOSE_SCHEMA = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}, "user_quote": QUOTE},
    "required": ["task_id", "user_quote"],
}
SHOW_SCHEMA = {
    "type": "object",
    "properties": {"task_id": {"type": "string", "description": "The task whose output to show; omit for the latest."}},
}
FRESH_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "keep": {"type": "string", "description": "Anything the user said to be sure to carry over (optional)."},
        "user_quote": QUOTE,
    },
    "required": ["task_id", "user_quote"],
}
RESTART_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Why, in a few plain words (shown to the user)."},
        "run_while_down": {"type": "string", "description": "Optional shell command to run while the app is "
                                                            "stopped, e.g. a benchmark. Its output comes back to you."},
        "timeout_s": {"type": "integer", "description": "Limit for run_while_down, in seconds (default 600)."},
    },
    "required": ["reason"],
}


def prompt_note(key):
    """A prompt from the config, set off from the text it is appended to."""
    return "\n\n" + config.prompt(key)


def voice():
    """The assistant's voice (tasks never speak: their work shows in the live window, and Haiku reports on it)."""
    return config.get().voice_spec()


def front_context():
    """The context prompt from the config (who the user is, where things are) plus, unless user.load_global_claude_md
    is false, the user's global ~/.claude/CLAUDE.md, read fresh at every start.

    The global file is added by hand rather than via --setting-sources user, which would also load the user's hooks
    (e.g. VoiceCode's MessageDisplay hook, which would read Haiku's replies aloud a second time).
    """
    parts = [config.prompt("front_context")]
    global_md = pathlib.Path.home() / ".claude/CLAUDE.md"
    if config.get()["user"]["load_global_claude_md"] and global_md.exists():
        parts.append("# The user's global instructions (~/.claude/CLAUDE.md)\n\n" + global_md.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def front_args(model):
    """The `claude` arguments for the voice front end. Its system prompt is the config's front_agent text, written to
    a file (a long prompt on the command line can hit Windows' length limit)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    prompt_file = os.path.join(STATE_DIR, "front-agent.md")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(config.prompt("front_agent") + "\n")
    return ["--model", model, "--system-prompt-file", prompt_file,
            "--append-system-prompt", front_context(), "--tools", "",
            "--allowedTools", "mcp__voice__propose_task,mcp__voice__decide,mcp__voice__continue_task,"
                              "mcp__voice__answer_question,mcp__voice__set_approval_mode,mcp__voice__close_task,"
                              "mcp__voice__find_project,mcp__voice__show_output,mcp__voice__find_sessions,"
                              "mcp__voice__resume_session,mcp__voice__fresh_start,"
                              "mcp__voice__end_session",
            "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
            "--include-partial-messages", "--no-session-persistence"]


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


BIG_WORK_REFUSAL = ("Not sent: this is new multi-part work, so it gets its own job. Call propose_task with route job "
                    "(same project_dir), mentioning task {id}'s last result if it builds on it.")
_DO_VERB = re.compile(r"\b(?:carry\s+out|execute|implement|follow(?!\s+up\b)|apply|do|work\s+through)\s+"
                      r"(?:(?:the|those|these|all|of|its|their|every|each|my)\s+){0,3}"
                      r"(?:instructions|prompt|plan|brief|changes|tasks|steps|list|everything|all)\b", re.I)
_FILE_REF = re.compile(r"\.(?:md|markdown|txt)\b|\b(?:markdown|text)\s+file\b|\bchanges?\s+file\b", re.I)


def asks_to_carry_out_file(message):
    """True when the message tells the work to carry out the instructions/plan/changes in a md/txt file (narrow on
    purpose: reading a file to answer a question, or a small task that merely mentions a file, doesn't match)."""
    return bool(_DO_VERB.search(message or "") and _FILE_REF.search(message or ""))


class Decision:
    """Something only the user can answer. kind: task | plan | tool | question."""

    def __init__(self, did, kind, title, body, task, asked_at):
        self.id, self.kind, self.title, self.body, self.task = did, kind, title, body, task
        self.asked_at = asked_at  # count of user messages when it was raised; the answer must come after
        self.answers = []  # for kind == "question"
        self.questions = []  # for kind == "question": the question texts, set by Backend._ask_user
        self.future = asyncio.get_running_loop().create_future()

    def resolve(self, approved, note="", scope="once"):
        if not self.future.done():
            self.future.set_result((approved, note, scope))


class Task:
    def __init__(self, tid, args):
        self.id = tid
        self.uid = uuid.uuid4().hex[:8]  # this instance of the id (ids are reused by later launches); kept when saved
        self.update(args)
        # draft → planning → building ⇄ idle (finished a round; session kept open for follow-ups) → closed,
        # or rejected / failed / stopped
        self.state = "draft"
        self.backend = None
        self.steps = 0
        self.auto_edits = False  # the user said to stop asking about edits; lasts for the whole task
        self.review_next_plan = False  # "plan first" asked for: show the plan even with full approval
        self.go_ahead = set()  # words of a request that was its own go-ahead ("commit it"); lasts one round
        self.digest = collections.deque(maxlen=12)
        self.session_id = None  # the Claude Code session, so the work can be resumed later
        self.fork = False  # resume into a copy of the session (it may be open elsewhere)
        self.context = 0  # tokens the conversation carries (from its latest model call)
        self.nudged = 0  # how many of CONTEXT_NUDGES were passed on to Haiku
        self.idle_since = None  # monotonic time it went idle (set when a round ends)
        self.handover = None  # a Future while this conversation writes its handover note (see Front._fresh)
        self.fresh_from = ""  # this task carries on from that one via a handover
        self.fresh_after = None  # fresh_start asked while workers ran: its "keep", until they finish
        # jobs (orchestrator.py): spending so far (closed processes, step checks), the approved plan
        self.spent_before = self.check_cost = 0.0
        self.budget_warned = False
        self.plan = ""
        self.handed_back = ""
        self.go_ahead_words = set()  # a job's go-ahead, kept for a hand-back to a quick task
        self.last_asked = ""  # the user's latest request to it, so Haiku can tell a follow-up from a new topic
        self.last_result = ""
        self.output = ""  # file with the latest round's full result (see Backend._save_output)
        self.show_next = False  # open the output window when this round finishes
        self.was = ""  # for a task saved by an earlier launch: its state back then
        self.saved_at = time.time()

    @property
    def live_key(self):
        """What names this task's live log and window (live.py): the id plus the instance, so a later task that
        reuses the id never shares them."""
        return f"{self.id}-{self.uid}"

    SAVED_STATES = ("planning", "building", "idle", "saved")

    def to_dict(self):
        return {"id": self.id, "uid": self.uid, "title": self.title, "project_dir": self.project_dir, "model": self.model,
                "session_id": self.session_id, "state": self.was if self.state == "saved" else self.state,
                "auto_edits": self.auto_edits, "go_ahead": sorted(self.go_ahead), "last_result": self.last_result[:400],
                "output": self.output, "context": self.context, "nudged": self.nudged,
                "kind": self.kind, "budget": self.budget, "plan": self.plan, "last_asked": self.last_asked,
                "spent_before": self.spent_before + self.check_cost + getattr(self.backend, "proc_cost", 0),
                "digest": list(self.digest)[-4:], "saved_at": time.time() if self.state != "saved" else self.saved_at}

    @classmethod
    def from_dict(cls, d):
        t = cls(d["id"], {"title": d["title"], "project_dir": d["project_dir"], "backend_model": d["model"]})
        t.uid = d.get("uid") or t.uid
        t.state, t.was = "saved", d.get("state", "idle")
        t.session_id, t.auto_edits = d.get("session_id"), d.get("auto_edits", False)
        t.go_ahead = set(d.get("go_ahead", []))  # survives a restart the request itself asked for
        t.last_result, t.saved_at = d.get("last_result", ""), d.get("saved_at", time.time())
        t.output = d.get("output", "")
        t.context, t.nudged = d.get("context", 0), d.get("nudged", 0)
        t.kind, t.budget, t.plan = d.get("kind", "quick"), d.get("budget", 10.0), d.get("plan", "")
        t.spent_before = d.get("spent_before", 0.0)
        t.last_asked = d.get("last_asked", "")
        t.digest.extend(d.get("digest", []))
        return t

    def update(self, args):
        self.title = args.get("title", getattr(self, "title", ""))
        self.prompt = args.get("prompt", getattr(self, "prompt", ""))
        self.project_dir = os.path.expanduser(args.get("project_dir", getattr(self, "project_dir", "~")))
        self.kind = args.get("route") or getattr(self, "kind", "quick")
        models = config.get()["models"]
        self.model = args.get("backend_model") or getattr(self, "model", models["job"] if self.kind == "job" else models["backend"])
        self.budget = float(args.get("budget_usd") or getattr(self, "budget", 10.0))

    @property
    def asked_note(self):
        return f"; last asked: {self.last_asked[:80]!r}" if self.last_asked else ""

    @property
    def size_note(self):
        return f"; long conversation, {self.context // 1000}k tokens" if self.context >= CONTEXT_NUDGES[0] else ""

    @property
    def active(self):
        return self.state in ("planning", "building")

    @property
    def open(self):
        return self.active or self.state == "idle"


class App:
    """Shared state: the UI, speech, tasks, pending decisions, and notes queued for Haiku's next turn."""

    def __init__(self, ui, tts, front_model="haiku"):
        self.ui, self.tts = ui, tts
        self.tasks = {}
        self.decisions = collections.deque()
        self.front_notes = []  # outcomes Haiku should hear on its next turn (costs nothing until you speak)
        self.verbosity = "normal"  # quiet | normal | chatty
        self.full_approval = False  # the user gave blanket approval: only risky steps and questions come back
        self.user_speaking = lambda: False  # replaced by the voice UI
        self.on_exit = lambda: None  # set by the UI: close the app
        self.exit_requested = False
        self.restart_requested = False  # exit with RESTART_EXIT so scripts/cv relaunches the app
        self.front = Front(self, front_model)

    # ---- saved tasks -----------------------------------------------------------------------------------
    def next_task_id(self):
        nums = [int(t[1:]) for t in self.tasks if t[1:].isdigit()]
        return f"t{max(nums, default=0) + 1}"

    def save_tasks(self):
        keep = [t.to_dict() for t in self.tasks.values() if t.state in Task.SAVED_STATES and t.session_id]
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=1)
        os.replace(tmp, TASKS_FILE)
        cvlog.log.event("tasks-saved", ", ".join(f"{d['id']}:{d['state']}" for d in keep) or "none")

    def load_saved_tasks(self):
        """Tasks left open by an earlier launch come back as state 'saved' (no session running until resumed)."""
        try:
            with open(TASKS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return []
        cutoff = time.time() - SAVED_DAYS * 86400
        tasks = [Task.from_dict(d) for d in saved if d.get("saved_at", 0) > cutoff and d.get("session_id")]
        for t in tasks:
            self.tasks[t.id] = t
        live.prune(t.live_key for t in self.tasks.values())  # no log of a task that is gone
        cvlog.log.event("tasks-loaded", ", ".join(f"{t.id}:{t.was} {t.title}" for t in tasks) or "none")
        return tasks

    def request_restart(self, task, args):
        """A task asked to restart the app (approved by the user): save everything, then exit for scripts/cv."""
        req = {"task_id": task.id, "session_id": task.session_id, "project_dir": task.project_dir,
               "reason": args.get("reason", ""), "command": (args.get("run_while_down") or "").strip(),
               "timeout_s": int(args.get("timeout_s") or 600), "requested_at": time.time()}
        os.makedirs(STATE_DIR, exist_ok=True)
        cvlog.log.event("restart-request", json.dumps(req))
        with open(RESTART_FILE, "w", encoding="utf-8") as f:
            json.dump(req, f, indent=1)
        self.save_tasks()
        self.ui.status(f"restarting for {task.id}: {req['reason']}")
        self.tts.say("Restarting now, back in a moment.", voice())
        self.exit_requested = self.restart_requested = True
        self.front.events.clear()

        async def go():
            for _ in range(60):  # let the line above play
                if not self.tts.speaking:
                    break
                await asyncio.sleep(0.1)
            self.on_exit()

        asyncio.create_task(go())

    def show_output(self, task=None):
        """Open a task's live output window (default: the task with the newest activity). One window per task: if it
        is already open nothing happens; if it was closed, the new one replays the whole log. → error or None"""
        if task is None:
            def newest(t):
                paths = [p for p in (live.path_for(t.live_key), t.output) if p and os.path.exists(p)]
                return max((os.path.getmtime(p) for p in paths), default=None)

            task = max((t for t in self.tasks.values() if newest(t) is not None), key=newest, default=None)
        saved = task.output if task and task.output and os.path.exists(task.output) else None
        if not task or not (live.has_events(task.live_key) or saved):
            return "There's no output to show yet."
        file = None if live.has_events(task.live_key) else saved  # no live log (a restored task): its saved Markdown
        # A new window rather than cv's own: it tiles in the current workspace and closes with q.
        try:
            opened = live.open_window(task.live_key, file=file, title=f"cv · {task.id} · {task.title}",
                                      cwd=task.project_dir if os.path.isdir(task.project_dir) else None)
        except OSError as e:
            self.ui.error(f"couldn't open the output window: {e}")
            return f"Couldn't open the window ({e})."
        cvlog.log.event("show-output", f"{task.id} {'opened' if opened else 'already open'}")
        if opened:
            self.ui.status(f"{task.id}: output opened in a new window")
        return None

    # ---- decisions -------------------------------------------------------------------------------------
    async def ask(self, d, announce=None):
        cvlog.log.event("decision-open", f"{d.id} [{d.kind}] {d.title}")
        self.decisions.append(d)
        if len(self.decisions) == 1:
            self.ui.card(d)
        if announce:
            self.front.notify(announce)
        try:
            result = await d.future
            cvlog.log.event("decision-closed", f"{d.id} -> {result}")
            return result
        finally:
            self.decisions.remove(d)
            if self.decisions:
                self.ui.card(self.decisions[0])

    def new_decision(self, did, kind, title, body, task):
        return Decision(did, kind, title, body, task, self.front.user_turns)

    def find(self, did):
        return next((d for d in self.decisions if d.id == did), None)

    def resolve_by_keyboard(self, d, approved, note=""):
        cvlog.log.event("decision-key", f"{d.id} approved={approved} note={note!r}")
        d.resolve(approved, note)
        if note != "__edited__":
            self.front_notes.append(f"[{d.id} {'approved' if approved else 'declined'} by the user on the keyboard"
                                    f"{': ' + note if note else ''}]")

    @property
    def pending(self):
        return self.decisions[0] if self.decisions else None

    # ---- tasks -----------------------------------------------------------------------------------------
    def backend_for(self, task, **kw):
        if task.kind == "job":
            from orchestrator import JobBackend  # imports this module, so only once it's loaded
            return JobBackend(self, task, **kw)
        return Backend(self, task, **kw)

    async def review_task(self, task):
        if task.kind == "job":  # no review card: the orchestrator's plan is what the user approves
            self.ui.status(f"{task.id}: job started — the plan comes to you for approval — {task.title}")
            return await self._start(task)
        if task.go_ahead:
            self.ui.status(f"{task.id}: starting — your request was the go-ahead — {task.title}")
            return await self._start(task)
        if self.full_approval:
            self.ui.status(f"{task.id}: starting with full approval — {task.title}")
            return await self._start(task)
        while True:
            body = f"project: {task.project_dir}   back end: {task.model}\n\n{task.prompt}"
            d = self.new_decision(f"{task.id}-review", "task", f"Task {task.id}: {task.title}", body, task)
            approved, note, _ = await self.ask(d)
            if note == "__edited__":
                continue  # the prompt was edited in place; show the updated card again
            if approved:
                if note:  # e.g. "yes, but use SQLite": part of what to do, so it goes into the prompt
                    task.prompt += f"\n\nThe user's note when approving: {note}"
                await self._start(task)
            else:
                task.state = "rejected"
            return

    async def _start(self, task):
        for other in self.tasks.values():
            if (other is not task and other.state == "idle" and other.project_dir == task.project_dir
                    and not other.backend.running_workers()):
                await other.backend.close()
        task.state = "planning" if task.kind == "job" or not (self.full_approval or task.go_ahead) else "building"
        task.backend = self.backend_for(task)
        await task.backend.start()
        if config.get()["behaviour"]["auto_show_output"]:
            self.show_output(task)

    # ---- what Haiku sees -------------------------------------------------------------------------------
    def context_prefix(self):
        lines, self.front_notes = self.front_notes, []
        for t in self.tasks.values():
            if t.active and t.digest:
                lines.append(f"[task {t.id} ({t.state}) recent activity: " + " | ".join(t.digest) + "]")
        idle = [f"{t.id} in {t.project_dir} ({t.title}{t.size_note}{t.asked_note})" for t in self.tasks.values()
                if t.state == "idle"]
        if idle:
            lines.append("[open for follow-ups: " + "; ".join(idle) + "]")
        saved = [f"{t.id} in {t.project_dir} ({t.title}{t.size_note}{t.asked_note})" for t in self.tasks.values()
                 if t.state == "saved"]
        if saved:
            lines.append("[saved from an earlier session, resumable with continue_task: " + "; ".join(saved) + "]")
        if self.decisions:
            lines.append("[waiting on the user: " + ", ".join(f"{d.id} ({d.title})" for d in self.decisions) + "]")
        if self.full_approval:
            lines.append("[full approval is on: work starts and runs without asking; only report results]")
        return "\n".join(lines)


class Front:
    """Haiku with a tiny system prompt and three tools. It presents the work as its own, but Claude Code does it."""

    def __init__(self, app, model="haiku"):
        self.app = app
        self.splitter = SentenceSplitter()
        # Busy/interrupt tracking uses times, not counts: Claude Code can fold messages sent while it's busy into
        # one turn (one result for two sends), which made a counter drift so Haiku looked busy forever and every
        # later reply was dropped as "interrupted".
        self._sent = self._ended = self._activity = 0.0  # last message sent / turn ended / stream event
        self._discard_at = None  # set by an interrupt: drop that turn's leftover text (see _discarding)
        self.closing = False
        self.user_turns = 0
        self.last_user_text = ""
        self._home_nudged = -1  # user turn in which a task drafted in ~ was redirected to a named project
        self._sessions_nudged = -1  # user turn in which a new task was pointed at earlier conversations
        self.events = collections.deque()  # app → Haiku announcements, sent when nobody is talking
        self.wake = asyncio.Event()
        tools = SdkMcpServer("voice", {
            "propose_task": ("Draft a piece of work you will do once the user approves it. Nothing runs until then.",
                             TASK_SCHEMA, self._propose),
            "decide": ("Pass on the user's spoken answer to a pending decision (task review, plan, or step).",
                       DECIDE_SCHEMA, self._decide),
            "continue_task": ("Send a follow-up, extra instruction or question to an open task (running, or "
                              "finished but open). It continues with everything it already knows.",
                              CONTINUE_SCHEMA, self._continue),
            "answer_question": ("Pass on the user's answer to a question you asked them during your work.",
                                ANSWER_SCHEMA, self._answer),
            "find_sessions": ("List earlier conversations in a project folder (yours and ones the user had in a "
                              "terminal), best match first. Use it when they pick up a project or earlier work.",
                              SESSIONS_SCHEMA, self._find_sessions),
            "resume_session": ("Carry on an earlier conversation from find_sessions with what the user wants now. "
                               "It continues with everything that conversation knew.", RESUME_SCHEMA, self._resume),
            "find_project": ("Find a project folder on the user's machine by name. Use it whenever they mention a "
                             "project without a path; never ask them where it is.", FIND_SCHEMA, self._find),
            "fresh_start": ("Carry a task on in a fresh conversation: it writes a handover note of what's worth "
                            "keeping, closes, and a new task starts from the note. For long conversations, when the "
                            "user agrees.", FRESH_SCHEMA, self._fresh_start),
            "close_task": ("Close an open or saved task the user is done with (\"forget it\", \"close that\").",
                           CLOSE_SCHEMA, self._close),
            "set_approval_mode": ("Switch between asking for each plan and step (ask) and blanket approval (full).",
                                  MODE_SCHEMA, self._set_mode),
            "show_output": ("Open the full output of a finished round of work in a window on the user's screen, "
                            "when they want to read or see it (\"show me\", \"put that on screen\", \"let me read "
                            "it\").", SHOW_SCHEMA, self._show),
            "end_session": ("Close the voice assistant when the user says goodbye or asks to exit or quit.",
                            EXIT_SCHEMA, self._end_session),
        })
        self.proc = ClaudeProc(
            front_args(model),
            cwd=str(pathlib.Path.home()), sdk_servers=[tools], on_event=self._on_event, name="front",
            # No auto-memory and no automatic CLAUDE.md discovery: Haiku's context is exactly front_context().
            # Project knowledge loads in the Claude Code session for that project, when work happens there.
            env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"})

    async def start(self):
        await self.proc.start()
        asyncio.create_task(self._results())
        asyncio.create_task(self._pump())

    async def say(self, text, from_user=True):
        if from_user:
            self.user_turns += 1
            self.last_user_text = text
        prefix = self.app.context_prefix()
        self._sent = time.monotonic()
        await self.proc.send(f"{prefix}\n{text}" if prefix else text)

    STALL_S = 30  # busy with no output for this long: assume the turn is over, so nothing waits on it forever

    @property
    def generating(self):
        now = time.monotonic()
        if self._sent <= self._ended:
            return False
        if now - max(self._sent, self._activity) > self.STALL_S:
            self._ended = now
            self._discard_at = None
            self.app.ui.dim("(Haiku went quiet; carrying on)")
            return False
        return True

    def notify(self, text):
        """Queue an announcement (a decision to explain, a task result) for Haiku to speak about."""
        if self.app.exit_requested:
            cvlog.log.event("notify-dropped", text[:300])
            return  # saying goodbye: drop late task results and decisions
        cvlog.log.event("notify", text[:500])
        self.events.append(text)
        self.wake.set()

    async def _pump(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.events:
                while self.generating or self.app.user_speaking():
                    await asyncio.sleep(0.1)
                if self.app.exit_requested or not self.events:
                    self.events.clear()
                    break
                await self.say(self.events.popleft(), from_user=False)

    async def interrupt(self):
        if self.generating:
            # Text from the interrupted turn keeps arriving for a moment (and its leftover is flushed when it ends);
            # drop all of it so it isn't spoken after you finish talking.
            self._discard_at = time.monotonic()
            self.splitter = SentenceSplitter()
            try:
                await self.proc.interrupt()
            except Exception:  # noqa: BLE001 - already finished
                pass

    @property
    def _discarding(self):
        """True while the interrupted turn's leftovers arrive. Ends at its result, when a reply to a newer message
        starts (see _on_event), or after 10 s at most, so it can never swallow later replies."""
        if self._discard_at is not None and time.monotonic() - self._discard_at > 10:
            self._discard_at = None
        return self._discard_at is not None

    def _speak(self, sentences):
        if self._discarding:
            return
        for s in sentences:
            self.app.ui.agent("haiku", s)
            self.app.tts.say(s, voice(), "haiku")

    def _on_event(self, ev):
        if ev.get("type") == "stream_event":
            e = ev["event"]
            now = self._activity = time.monotonic()
            if e.get("type") == "message_start":
                if self._sent <= self._ended:
                    self._sent = now  # a reply is streaming, so it's busy even if we didn't just send
                if self._discard_at is not None and self._sent > self._discard_at:
                    self._discard_at = None  # this is the reply to what you said after interrupting
            if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                self._speak(self.splitter.feed(e["delta"]["text"]))
            elif e.get("type") == "message_stop":
                self._speak(self.splitter.flush())

    async def _results(self):
        while (res := await self.proc.results.get()) is not None:
            if self._discarding:
                self.app.ui.dim("(interrupted)")
            self._speak(self.splitter.flush())
            self.splitter = SentenceSplitter()  # next reply gets the early first chunk again
            self._discard_at = None
            self._ended = time.monotonic()
            if self.app.exit_requested and not self.generating:
                asyncio.create_task(self._exit_after_goodbye())
            u = res.get("usage", {})
            self.app.ui.dim(f"haiku turn: in {u.get('input_tokens', 0)} + cache {u.get('cache_read_input_tokens', 0)}"
                            f"/{u.get('cache_creation_input_tokens', 0)}, out {u.get('output_tokens', 0)}")
        if not self.closing:  # a normal quit closes it on purpose
            self.app.ui.error("the Haiku process exited")

    # ---- tools -----------------------------------------------------------------------------------------
    def _check_quote(self, quote, asked_at=-1):
        """The user's latest message must contain the quoted words, and it must be newer than the question."""
        q = words(quote or "")
        said = set(words(self.last_user_text))
        if not q or not set(q) <= said:
            return "Rejected: user_quote isn't in the user's latest message. Ask them and wait for a clear answer."
        if self.user_turns <= asked_at:
            return "Rejected: the user hasn't answered since this was raised. Ask them first."
        return None

    async def _propose(self, args):
        tid = args.get("task_id")
        # Safety net for "work on my luffy project" drafted in plain ~: point Haiku at the project before anything
        # starts (with full approval it would start straight away). A second call in ~ goes through.
        home = os.path.realpath(os.path.expanduser("~"))
        if (os.path.realpath(os.path.expanduser(args.get("project_dir", "~"))) == home
                and self._home_nudged != self.user_turns):
            if found := await asyncio.to_thread(named_project, self.last_user_text):
                self._home_nudged = self.user_turns
                self.app.ui.status(f"project folder: suggesting {found} instead of ~")
                return (f"Not created yet: the user named a project, and {found} is that project's folder. Call "
                        f"propose_task again with project_dir {found}. (If ~ really is right, call it again unchanged.)")
        if args.get("route") == "job" and not tid:
            tid = self.app.next_task_id()
            task = self.app.tasks[tid] = self._new_task(tid, args)
            if args.get("go_ahead") and not self._check_quote(args.get("user_quote")):
                task.go_ahead_words = set(words(self.last_user_text))  # for a hand-back to a quick task
            asyncio.create_task(self.app.review_task(task))
            return (f"Started job {tid}: you're working out the plan, which comes to the user for approval before "
                    "anything changes. Say in a few words that you're on it; then stay quiet until the plan is ready.")
        # Earlier conversations in this folder (cv's or the user's own): offer them once before starting from zero.
        if not tid and self._sessions_nudged != self.user_turns:
            self._sessions_nudged = self.user_turns
            found = await self._earlier(args.get("project_dir", "~"), self.last_user_text, limit=4)
            if os.path.realpath(os.path.expanduser(args.get("project_dir", "~"))) == home:
                found = [x for x in found if x["score"] >= 2]  # ~ holds every general chat: only clear topic matches
            if found:
                self.app.ui.status(f"earlier conversations in {args.get('project_dir')}: offering {len(found)}")
                return ("Not created yet. There are earlier conversations in this folder, best match first:\n"
                        + "\n".join(map(sessions.describe, found)) + "\nIf one is clearly the work they're picking up "
                        "or building on, call resume_session with it (same go_ahead/show_output/plan_first), so it "
                        "carries on with what it already knows. If two could fit, ask in a few words which. If it's "
                        "new, unrelated work, call propose_task again unchanged.")
        go = bool(args.get("go_ahead"))
        if go and (err := self._check_quote(args.get("user_quote"))):
            return err + " (Needed for go_ahead; or propose it without go_ahead.)"
        if (self.app.full_approval or go) and not tid:
            tid = self.app.next_task_id()
            task = self.app.tasks[tid] = self._new_task(tid, args)
            if go:
                task.go_ahead = set(words(self.last_user_text))
                task.prompt += prompt_note("go_note")
            asyncio.create_task(self.app.review_task(task))
            return (f"Started task {tid} right away ({'their request was the go-ahead' if go else 'full approval'}). "
                    "Say in a few words that you're on it, without repeating what they asked; don't ask for approval.")
        if tid in self.app.tasks and self.app.tasks[tid].state == "draft":
            self.app.tasks[tid].update(args)
            self._apply_show(self.app.tasks[tid], args)
            for d in self.app.decisions:  # refresh the open review card
                if d.task is self.app.tasks[tid]:
                    d.resolve(False, "__edited__")
            return f"Draft {tid} updated; still waiting for decision {tid}-review. Nothing has run."
        tid = self.app.next_task_id()
        task = self.app.tasks[tid] = self._new_task(tid, args)
        asyncio.create_task(self.app.review_task(task))
        return (f"Draft {tid} is waiting for the user's decision {tid}-review. Nothing has run. Check they want you to "
                "start, briefly and without repeating their request (for complex work, one sentence on your approach).")

    def _new_task(self, tid, args):
        task = Task(tid, args)
        task.last_asked = self.last_user_text
        return self._apply_show(task, args)

    @staticmethod
    def _apply_show(task, args):
        """show_output asked for: open the result in a window, and have the work write it as a full answer."""
        task.show_next = bool(args.get("show_output"))
        if task.show_next:
            task.prompt += prompt_note("show_note")
        return task

    async def _decide(self, args):
        d = self.app.find(args.get("decision_id", ""))
        if not d:
            return "No such pending decision. " + (self.app.context_prefix() or "Nothing is waiting.")
        if err := self._check_quote(args.get("user_quote"), d.asked_at):
            return err
        approve, note = bool(args.get("approve")), args.get("note", "")
        d.resolve(approve, note, args.get("scope", "once"))
        self.app.ui.status(f"{d.id}: {'approved' if approve else 'declined'} by voice "
                           f"(\"{args.get('user_quote')}\"){' — ' + note if note else ''}")
        if d.kind == "task":
            return (f"Task {d.task.id} approved; you're planning it now and will be told when your plan is ready."
                    if approve else f"Task {d.task.id} dropped.")
        if d.kind == "plan":
            return "Plan approved; you're building it now." if approve else "You'll revise the plan with their changes."
        return "Done." if approve else "Okay, that step is skipped."

    async def _continue(self, args, from_tool=True):
        task = self.app.tasks.get(args.get("task_id", ""))
        if not task or not (task.open or task.state == "saved"):
            return "That task isn't open any more. For more work there, draft a new task with propose_task."
        if err := self._check_quote(args.get("user_quote")):
            return err
        if not args.get("same_topic"):
            # A new subject gets its own conversation; the old one stays open (or resumable) for its own topic.
            return ("Not sent: that's a different topic from what this task is about, so it gets a fresh "
                    "conversation. Call propose_task for it instead (same project_dir is fine). If it really is the "
                    "same topic, call continue_task again with same_topic true.")
        if from_tool and task.kind != "job" and (args.get("size") == "large" or asks_to_carry_out_file(args.get("message", ""))):
            return BIG_WORK_REFUSAL.format(id=task.id)  # a quick task never grows into a job
        said = args["message"]  # their words, for the status lines (the notes below are for the work only)
        task.last_asked = said
        if args.get("budget_usd"):
            task.budget, task.budget_warned = float(args["budget_usd"]), False
        if (task.kind == "job" and not task.active and task.context >= CONTEXT_NUDGES[0]
                and not (task.backend and task.backend.running_workers())):
            # A long orchestrator starts over from its state file and done list instead of carrying its history.
            if task.backend:
                await task.backend.close()
            task.session_id, task.context, task.nudged = None, 0, 0
            task.backend = self.app.backend_for(task)
            task.state = "planning"
            await task.backend.start(task.backend.opening(f"(follow-up) {said}"))
            self.app.ui.status(f"{task.id}: fresh orchestrator from the saved state: \"{said}\"")
            return "Carrying on with that job now." + " " + config.prompt("short_ack")
        go = bool(args.get("go_ahead")) and not args.get("plan_first")
        if go:
            task.go_ahead = set(words(self.last_user_text))
            args = {**args, "message": args["message"] + prompt_note("go_note")}
        if args.get("show_output"):
            task.show_next = True
            args = {**args, "message": args["message"] + prompt_note("show_note")}
        waiting = [d for d in self.app.decisions if d.task is task and d.kind != "task"]
        if waiting:
            # The task is paused inside that step/plan/question and won't read a new message until it's answered
            # (that froze a session for 2 minutes), so decline it and let the instruction ride along as the note.
            for d in waiting:
                d.resolve(False, f"(said by voice) {args['message']}")
                self.app.ui.status(f"{d.id}: declined so your new instruction gets through")
            if args.get("plan_first"):
                task.review_next_plan = True
            return (f"The waiting {', '.join(d.id for d in waiting)} was declined, and the instruction went to the "
                    "work with it, so it's acting on it now. Tell the user that in a few words.")
        if task.state == "saved":  # from an earlier launch: resume its Claude Code session with full context
            message = f"The user says (by voice): {args['message']}"
            # Saved while still planning (its plan never got an answer): go back to planning, so the plan is
            # presented again rather than the work starting unapproved.
            plan = bool(args.get("plan_first")) or (task.was == "planning" and not go)
            if plan:
                task.review_next_plan = True
                message += "\n\nPlan this first, then present a plan for approval before changing anything."
            task.backend = self.app.backend_for(task, resume=True, plan=plan)
            task.state = "planning" if plan else "building"
            await task.backend.start(message)
            self.app.ui.status(f"{task.id}: resumed: \"{said}\"")
            return "Resumed that work where it left off, with everything it knew. Say so in a few words."
        task.digest.append(f"user said: {said[:80]}")
        was_idle = task.state == "idle"
        message = f"The user says (by voice): {args['message']}"
        if args.get("plan_first"):
            task.review_next_plan = True
            # Back into plan mode in the same session: it plans with everything it already knows, and the new plan
            # comes back as a decision like the first one did.
            await task.backend.proc.control({"subtype": "set_permission_mode", "mode": "plan"})
            task.state = "planning"
            message += "\n\nPlan this first: investigate as needed, then present a plan for approval before changing anything."
        elif was_idle:
            task.state = "building"
        # Arrives between steps if it's mid-work; starts a new round if it was idle. Edits/commands still ask.
        log_round(task, message, queued=not was_idle)
        await task.backend.proc.send(message)
        how = "planning" if args.get("plan_first") else "continuing" if was_idle else "added to the running work"
        self.app.ui.status(f"{task.id}: {how}: \"{said}\"")
        if args.get("plan_first"):
            return "Planning that now; you'll present the plan for approval before anything changes."
        return ("Continuing that work now." if was_idle else "Added to the work in progress.") + " " + config.prompt("short_ack")

    async def _end_session(self, args):
        if err := self._check_quote(args.get("user_quote")):
            return err
        running = [t for t in self.app.tasks.values() if t.active]
        if running and not args.get("stop_running_work"):
            names = ", ".join(f"{t.id} ({t.title})" for t in running)
            return (f"Not closing yet: still working on {names}. Ask the user whether to stop that and quit, or wait "
                    "for it to finish. If they confirm, call end_session again with stop_running_work=true.")
        self.app.exit_requested = True  # from here on, nothing new reaches Haiku (see notify and _pump)
        self.events.clear()
        self.app.ui.status(f"closing (\"{args.get('user_quote')}\")")
        for t in running:  # stop the work now, so its last messages don't keep the conversation going
            await t.backend.stop()
        return "Closing after this reply. Say a short, natural goodbye (a few words), nothing else."

    async def _exit_after_goodbye(self):
        # Let the goodbye finish playing (capped, in case speech is stuck), then close the app.
        for _ in range(100):
            if not self.app.tts.speaking:
                break
            await asyncio.sleep(0.1)
        self.app.on_exit()

    async def _set_mode(self, args):
        if err := self._check_quote(args.get("user_quote")):
            return err
        on = args.get("mode") == "full"
        self.app.full_approval = on  # read per step in Backend._permission, so it applies to open sessions at once
        self.app.ui.status(f"approval mode: {'FULL — only risky steps and questions will ask' if on else 'ask'} "
                           f"(\"{args.get('user_quote')}\")")
        if on:
            return ("Full approval is on. Work now starts and runs without checking in, and there's no progress "
                    "narration; just report results. Risky steps (root, installs, deleting, git push, files outside "
                    "the project) and your own questions will still come back. Confirm in a sentence and mention that.")
        return "Back to asking for each plan and step."

    async def _find(self, args):
        hits = await asyncio.to_thread(find_projects, args.get("query", ""))
        self.app.ui.status(f"find_project {args.get('query', '')!r}: " + (", ".join(h["path"] for h in hits[:3]) or "nothing"))
        if not hits:
            return ("No folder by that name under ~ (checked ~/src, ~ and ~/.config). If it's new, create it in ~/src. "
                    "Otherwise ask for another name, or make it a task in ~ to search more widely.")
        return "Best matches first (folder, project files, last modified):\n" + "\n".join(
            f"- {h['path']}  [{', '.join(h['markers']) or 'no project files'}]  {h['modified']}" for h in hits)

    async def _earlier(self, project_dir, query, limit=6):
        """Earlier conversations in a folder, minus the ones open as tasks right now (those take continue_task)."""
        live = {t.session_id for t in self.app.tasks.values() if t.open and t.session_id}
        return await asyncio.to_thread(sessions.sessions, project_dir, query, limit, live)

    async def _find_sessions(self, args):
        found = await self._earlier(args.get("project_dir", "~"), args.get("query") or self.last_user_text)
        self.app.ui.status(f"find_sessions {args.get('project_dir')}: {len(found)} found")
        if not found:
            return "No earlier conversations in that folder. Start new work with propose_task."
        saved = {t.session_id: t.id for t in self.app.tasks.values() if t.state == "saved"}
        return "Best match first:\n" + "\n".join(
            sessions.describe(s) + (f" [saved task {saved[s['id']]}]" if s["id"] in saved else "") for s in found)

    async def _resume(self, args):
        sid = args.get("session_id", "")
        if err := self._check_quote(args.get("user_quote")):
            return err
        if task := next((t for t in self.app.tasks.values() if t.session_id == sid), None):
            return await self._continue({**args, "task_id": task.id, "same_topic": True}, from_tool=False)  # one cv already knows
        found = {s["id"]: s for s in await asyncio.to_thread(sessions.sessions, args.get("project_dir", "~"), "", 200)}
        if sid not in found:
            return "No conversation with that id in that folder. Check find_sessions."
        tid = self.app.next_task_id()
        task = self.app.tasks[tid] = Task(tid, args)
        task.state, task.was, task.session_id = "saved", "idle", sid
        task.fork = found[sid]["busy"]  # maybe open in a terminal right now: carry on in a copy, not the same file
        self.app.ui.status(f"{tid}: picking up {found[sid]['title'] or sid[:8]!r}" + (" (as a copy)" if task.fork else ""))
        return await self._continue({**args, "task_id": tid, "same_topic": True}, from_tool=False)

    async def _show(self, args):
        task = None
        if tid := args.get("task_id"):
            if not (task := self.app.tasks.get(tid)):
                return "No such task."
        if err := self.app.show_output(task):
            return err
        return "It's open in a window on their screen now. Say so in a few words."

    async def _fresh_start(self, args):
        task = self.app.tasks.get(args.get("task_id", ""))
        if not task or not task.session_id or not (task.open or task.state == "saved"):
            return "No open or saved task by that id."
        if err := self._check_quote(args.get("user_quote")):
            return err
        if task.active or task.handover:
            return "It's still working. Start fresh once this round finishes; tell the user that."
        if busy := task.backend and task.backend.running_workers():
            # Closing the session now would kill them; Backend._results starts fresh once they've finished.
            task.fresh_after = args.get("keep", "")
            return (f"Its {' and '.join(busy)} is still working, so the fresh start waits until that "
                    "finishes, then happens by itself. Say so in a few words.")
        asyncio.create_task(self._fresh(task, args.get("keep", "")))
        return "Writing a handover note, then starting a fresh conversation from it. Say so in a few words."

    async def _fresh(self, old, keep):
        ask = config.prompt("handover_ask") + (f"\n\nThe user especially wants to keep: {keep}" if keep else "")
        old.handover = asyncio.get_running_loop().create_future()
        self.app.ui.status(f"{old.id}: writing a handover ({old.context // 1000}k tokens so far)")
        try:
            if old.state == "saved":
                old.backend = self.app.backend_for(old, resume=True)
                old.state = "building"
                await old.backend.start(ask)
            else:
                old.state = "building"
                log_round(old, ask)
                await old.backend.proc.send(ask)
            note = (await asyncio.wait_for(old.handover, 600)).strip()
        except asyncio.TimeoutError:
            note = ""
        finally:
            old.handover = None
        if not note:
            old.state = "idle"
            self.notify(f"[The handover for {old.id} didn't come through, so it's still the same conversation.] "
                        "Tell the user in one sentence.")
            return
        os.makedirs(HANDOVER_DIR, exist_ok=True)
        path = os.path.join(HANDOVER_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{old.id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Handover from {old.id} · {old.title}\n\n*{old.project_dir}, session {old.session_id}*\n\n{note}\n")
        if old.backend:
            await old.backend.close()
        old.state = "closed"
        tid = self.app.next_task_id()
        start = "handover_job" if old.kind == "job" else "handover_start"
        new = self.app.tasks[tid] = Task(tid, {"title": old.title, "project_dir": old.project_dir,
                                               "backend_model": old.model, "route": old.kind,
                                               "prompt": config.prompt(start, project=old.project_dir, note=note)})
        if old.kind == "job":  # the same job: its cap and spending carry over
            new.budget, new.plan = old.budget, old.plan
            new.spent_before = old.spent_before + old.check_cost
        new.fresh_from = old.id
        new.go_ahead = {"handover"}  # straight in, no plan review: it only reads the note
        self.app.ui.status(f"{tid}: fresh conversation for {old.title!r} from {old.id}'s handover ({path})")
        await self.app._start(new)

    async def _close(self, args):
        task = self.app.tasks.get(args.get("task_id", ""))
        if not task:
            return "No such task."
        if err := self._check_quote(args.get("user_quote")):
            return err
        if task.backend and task.open:
            await task.backend.close()
        task.state = "closed"
        live.close_task(task.live_key)
        self.app.save_tasks()
        return f"Closed {task.id}."

    async def _answer(self, args):
        d = self.app.find(args.get("decision_id", ""))
        if not d or d.kind != "question":
            return "No such pending question. " + (self.app.context_prefix() or "Nothing is waiting.")
        if err := self._check_quote(args.get("user_quote"), d.asked_at):
            return err
        answers = [str(a) for a in args.get("answers") or [] if str(a).strip()]
        qs = d.questions
        if len(answers) < len(qs):  # before, missing answers went to the task as empty strings
            missing = "; ".join(f"Q{i + 1}: {q}" for i, q in enumerate(qs) if i >= len(answers))
            return (f"Not sent: {len(qs)} questions need {len(qs)} answers, in order, and you gave {len(answers)}. "
                    f"Still unanswered: {missing}. Ask the user about those, then call answer_question again "
                    "with every answer.")
        d.answers = answers
        d.resolve(True)
        self.app.ui.status(f"{d.id}: answered by voice: {'; '.join(d.answers)}")
        return "Got it, carrying on."


class Backend:
    """One Claude Code session per task: plan mode first, then every edit or command needs the user's yes."""

    @property
    def system_prompt(self):  # appended to Claude Code's own (orchestrator.JobBackend has its own)
        return config.prompt("narration")

    def __init__(self, app, task, resume=False, plan=False):
        self.app, self.task = app, task
        self.agents = {}  # subagent task_id → its type, while it runs (a background one outlives the round)
        # With full approval (or a request that was its own go-ahead) it skips the separate planning round (it still
        # thinks before acting); otherwise plan mode first, so the plan can be reviewed. Never acceptEdits: in that mode Claude
        # Code also runs rm/mv/mkdir in the project by itself, so they'd skip the app's risk check. Auto-approval
        # happens here, in _permission.
        # A resumed session (earlier launch, or after a restart) carries on in default mode unless a plan is wanted.
        mode = self._mode(plan, resume)
        args = ["--model", task.model, "--permission-mode", mode, "--append-system-prompt", self.system_prompt]
        args += self._extra_args()
        if resume and task.session_id:
            args += ["--resume", task.session_id] + (["--fork-session"] if task.fork else [])
            task.fork = False  # the copy gets its own id (from init), which is the one resumed from now on
        elif not resume:
            args += ["--name", f"cv · {task.title}"]  # a title for find_sessions, and for claude --resume
        tools = SdkMcpServer("voiceapp", self._tools())
        self.proc = ClaudeProc(args, cwd=task.project_dir, can_use_tool=self._permission_logged, on_event=self._on_event,
                               sdk_servers=[tools], name=f"task-{task.id}")
        self.questions = 0
        self.plans = 0

    # ---- hooks (orchestrator.JobBackend overrides them) --------------------------------------------------
    def _mode(self, plan, resume):
        return "plan" if plan or not (self.app.full_approval or self.task.go_ahead or resume) else "default"

    def _extra_args(self):
        return []

    def _tools(self):
        return {"restart_voice_app": (
            "Restart the Claude Voice app you are running inside, instead of killing it: to free the GPU (e.g. for "
            "a benchmark) or to load changes to its code. The user approves it first. The app saves its state, "
            "stops, runs run_while_down (if given) in this project folder, starts again, and resumes this session "
            "with that command's output. After calling it, end your turn and wait.", RESTART_SCHEMA, self._restart)}

    # ---- the task's live log (live.py): never raises into the task ------------------------------------------------
    def _live(self, kind, **fields):
        key = getattr(self.task, "live_key", None)
        if key:
            live.emit(key, kind, **fields)

    async def send_user(self, text, queued=False):
        """Send a message to the session, and log it as the start of a round (queued: it arrives mid-round)."""
        log_round(self.task, text, queued)
        await self.proc.send(text)

    def _nested(self, ev):
        """parent / agent fields for an event that happened inside a subagent."""
        parent = ev.get("parent_tool_use_id")
        if not parent:
            return {}
        return {"parent": parent, "agent": self.__dict__.setdefault("agent_names", {}).get(parent)}

    def _live_event(self, ev):
        """Log what a stream event shows: text, thinking, tool calls and results, subagents starting, progressing and
        ending, and the round's output tokens."""
        t = ev.get("type")
        d = self.__dict__
        names = d.setdefault("agent_names", {})  # Agent tool_use id → subagent type
        tool_names = d.setdefault("tool_names", {})  # tool_use id → tool name (for the result's meta)
        if t == "assistant":
            nest = self._nested(ev)
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            thought = d.setdefault("thought", set())  # message ids whose thinking is logged (blocks come one by one)
            if any(b.get("type") == "thinking" for b in content) and (msg.get("id") or id(msg)) not in thought:
                thought.add(msg.get("id") or id(msg))
                self._live("thinking", **nest)
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    self._live("text", text=b["text"], **nest)
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    tool_names[b.get("id")] = b.get("name")
                    if b.get("name") in ("Agent", "Task"):
                        names[b["id"]] = inp.get("subagent_type") or "agent"
                    self._live("tool_use", id=b.get("id"), name=b.get("name"), summary=live.summarize(b.get("name"), inp),
                               input=inp if b.get("name") in live.FULL_INPUT_TOOLS else None,
                               subagent_type=names.get(b.get("id")), **nest)
            if not nest:  # the round's output tokens (a message repeats with growing usage: keep its highest)
                per = d.setdefault("out_tokens", {})
                out = (msg.get("usage") or {}).get("output_tokens")
                if isinstance(out, int) and out > per.get(msg.get("id"), 0):
                    per[msg.get("id")] = out
                    total = sum(per.values())
                    if total - d.get("out_logged", 0) >= TOKENS_STEP:
                        d["out_logged"] = total
                        self._live("tokens", out=total)
        elif t == "user":
            content = ev.get("message", {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    body = b.get("content")
                    if isinstance(body, list):
                        body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
                    body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
                    meta = live.result_meta(tool_names.get(b.get("tool_use_id")), ev.get("tool_use_result"))
                    self._live("tool_result", tool_use_id=b.get("tool_use_id"), text=body[:live.RESULT_MAX],
                               is_error=bool(b.get("is_error")), truncated=True if len(body) > live.RESULT_MAX else None,
                               meta=meta, **self._nested(ev))
        elif t == "system" and ev.get("subtype") == "task_started":
            use = ev.get("tool_use_id")
            name = ev.get("subagent_type") or names.get(use) or "agent"
            names[use] = name
            d.setdefault("agent_uses", {})[ev.get("task_id")] = (use, name)
            brief = ev.get("prompt")
            self._live("subagent_start", id=ev.get("task_id"), name=name, description=ev.get("description"), parent=use,
                       brief=brief[:BRIEF_MAX] if isinstance(brief, str) else None)
        elif t == "system" and ev.get("subtype") == "task_progress":
            use, _ = d.get("agent_uses", {}).get(ev.get("task_id"), (None, None))
            if use is not None:
                u = ev.get("usage") or {}
                self._live("progress", id=ev.get("task_id"), parent=use,
                           step=str(ev.get("description") or ev.get("last_tool_name") or ""),
                           tokens=u.get("total_tokens"), tool_uses=u.get("tool_uses"), ms=u.get("duration_ms"))
        elif t == "system" and ev.get("subtype") in ("task_updated", "task_notification"):
            status = (ev.get("patch") or {}).get("status") or ev.get("status")
            if status in ("completed", "failed", "stopped", "killed", "error"):
                u = ev.get("usage") or {}
                ends = d.setdefault("agent_ends", {})  # task_id → its end was logged without totals
                if ev.get("task_id") in ends:  # the totals came after the end: log the end again with them
                    if u:
                        use, name = ends.pop(ev["task_id"])
                        self._live("subagent_end", id=ev.get("task_id"), name=name, status=status, parent=use,
                                   tokens=u.get("total_tokens"), tool_uses=u.get("tool_uses"), ms=u.get("duration_ms"))
                    return
                use, name = d.setdefault("agent_uses", {}).pop(ev.get("task_id"), (None, "agent"))
                if use is not None:
                    self._live("subagent_end", id=ev.get("task_id"), name=name, status=status, parent=use,
                               tokens=u.get("total_tokens"), tool_uses=u.get("tool_uses"), ms=u.get("duration_ms"))
                    if not u:
                        ends[ev.get("task_id")] = (use, name)
        elif t == "result":
            d["out_tokens"], d["out_logged"], d["thought"] = {}, 0, set()

    async def _permission_logged(self, name, inp, req):
        """The permission handler claude is given: _permission, plus a line in the live log with who decided."""
        box = {"by": "auto"}
        _decided_by.set(box)  # this request's own task context, so concurrent requests don't mix
        resp = await self._permission(name, inp, req)
        try:
            ok = resp.get("behavior") == "allow"
            if not (ok and box["by"] == "auto" and name in QUIET_TOOLS):
                use, agent = self.__dict__.get("agent_uses", {}).get(req.get("agent_id"), (None, None))
                self._live("permission", tool=name, summary=live.summarize(name, inp), decision="approved" if ok else "refused",
                           by=box["by"], reason=None if ok else resp.get("message"), parent=use, agent=agent)
        except Exception as e:  # noqa: BLE001
            cvlog.log.event("live:error", repr(e))
        return resp

    async def _restart(self, args):
        self.app.request_restart(self.task, args)
        return ("The app is restarting now. This session will be resumed afterwards"
                + (" with the command's output." if args.get("run_while_down") else ".")
                + " End your turn now and wait; don't do anything else.")

    async def start(self, message=None):
        """New task: send its prompt. Resumed task: send `message` into the restored session."""
        t = self.task
        if not os.path.isdir(t.project_dir):
            t.state = "failed"
            self.app.ui.error(f"{t.id}: project folder {t.project_dir} doesn't exist")
            self.app.front.notify(f"[Task {t.id} couldn't start: the folder {t.project_dir} doesn't exist.] "
                                  f"Tell the user in one sentence.")
            return
        await self.proc.start()
        await self.send_user(message or t.prompt)
        asyncio.create_task(self._results())
        if not message:
            self.app.ui.status(f"{t.id}: planning with Claude Code ({t.model}) in {t.project_dir}")

    def _end_round_early(self):
        """The round in progress won't get a result (stopped, closed, saved): say so, so no viewer spins forever."""
        t0 = getattr(self.task, "round_t0", None)
        if t0:
            self.task.round_t0 = None
            self._live("round_end", duration_s=round(time.monotonic() - t0, 1), turns=0, ok=False)

    async def stop(self, state="stopped"):
        self.task.state = state
        self._end_round_early()
        try:
            await self.proc.interrupt()
        except Exception:  # noqa: BLE001
            pass
        await self.proc.close()

    def running_workers(self):
        """The types of subagents still running, which closing this session would kill."""
        return list(self.agents.values())

    async def close(self):
        """End an idle session (its context is saved by Claude Code as usual)."""
        await self.stop("closed")
        live.close_task(self.task.live_key)
        self.app.ui.status(f"{self.task.id}: closed")

    @staticmethod
    def _allow(inp, mode=None):
        # A mode change rides on the approval itself. (Sending set_permission_mode separately raced with the CLI's
        # own switch out of plan mode, so "stop asking about edits" was overwritten.)
        resp = {"behavior": "allow", "updatedInput": inp}
        if mode:
            resp["updatedPermissions"] = [{"type": "setMode", "mode": mode, "destination": "session"}]
        return resp

    async def _permission(self, name, inp, req):
        t = self.task
        if name == "AskUserQuestion":
            return await self._ask_user(inp)
        if name == "ExitPlanMode" and (self.app.full_approval or t.go_ahead) and not t.review_next_plan:
            t.state = "building"
            t.digest.append("plan made and auto-approved")
            self.app.ui.dim(f"  {t.id} │ plan auto-approved (full approval)")
            return self._allow(inp, "default")
        if name == "ExitPlanMode":
            t.review_next_plan = False
            plan = inp.get("plan", "")
            self.plans += 1
            pid = f"{t.id}-plan" + (str(self.plans) if self.plans > 1 else "")
            d = self.app.new_decision(pid, "plan", f"{pid} · plan for {t.title}", plan, t)
            decided_by("user")
            ok, note, scope = await self.app.ask(d, announce=(
                f"[Decision {d.id} needed: your plan for task {t.id} ({t.title}) is ready.\nPLAN:\n{plan[:4000]}\n]"
                " Tell the user your plan in two or three natural sentences and check they're happy for you to go"
                " ahead, in your own words. Answer follow-up questions from the plan. Never read code aloud."))
            if ok:
                t.state = "building"
                t.digest.append("plan approved")
                t.auto_edits |= scope == "rest_of_task"
                # Leaving plan mode into default, so every edit and command comes back here; edits are then
                # auto-approved below if the user said to stop asking about them (for the rest of the task).
                self._forward_note(note)
                return self._allow(inp, "default")
            t.digest.append("plan sent back for changes")
            return {"behavior": "deny", "message": "The user wants changes to the plan: " + (note or "revise it") +
                                                   ". Revise the plan and call ExitPlanMode again."}
        verb, target, body = describe_tool(name, inp)
        why = risk(name, inp, t.project_dir)
        if not why and (self.app.full_approval or t.go_ahead or (t.auto_edits and name in EDIT_TOOLS)):
            t.digest.append(f"auto-approved: {verb} {target}")
            self.app.ui.dim(f"  {t.id} │ auto-approved: {verb} {target}")
            return self._allow(inp)
        if why and t.go_ahead and bool(IMPLIED_BY.get(why, set()) & t.go_ahead):
            t.digest.append(f"done as asked: {verb} {target}")
            self.app.ui.dim(f"  {t.id} │ approved by your request ({why}): {verb} {target}")
            return self._allow(inp)
        return await self._ask_step(inp, verb, target, body, why)

    async def _ask_step(self, inp, verb, target, body, why):
        """Ask the user about one step, through Haiku."""
        t = self.task
        t.steps += 1
        d = self.app.new_decision(f"{t.id}-step{t.steps}", "tool", f"{t.id}-step{t.steps} · {verb} {target}", body, t)
        caution = (f" This is a step you always check first, even with full approval ({why}); say that in a few "
                   "plain words, as your own habit." if why and self.app.full_approval else "")
        decided_by("user")
        ok, note, scope = await self.app.ask(d, announce=(
            f"[Decision {d.id} needed: for task {t.id} you need to {verb} {target}.\nDETAILS (may be shortened):\n"
            f"{body[:800]}\n]"
            + caution +
            " In one natural sentence, say what you want to do and check it's fine, in your own words."))
        t.digest.append(f"{'approved' if ok else 'denied'}: {verb} {target}")
        if ok:
            if scope == "rest_of_task":
                t.auto_edits = True
            self._forward_note(note)
            return self._allow(inp)
        return {"behavior": "deny", "message": "The user said no" + (f": {note}" if note else ".")}

    def _forward_note(self, note):
        """An approval can't carry text, so a note that came with one follows as a message; Claude Code picks it up
        between steps. (Before, approval notes were silently dropped: "approve, but only close idle tasks" lost
        the "but".)"""
        if note and note != "__edited__":
            cvlog.log.event(f"task-{self.task.id}:note", note)
            self.task.digest.append(f"user's note: {note[:80]}")

            async def send():
                await asyncio.sleep(0.05)  # after the approval itself has been written
                await self.proc.send(f"The user approved that, with this note (by voice): {note}")

            asyncio.create_task(send())

    async def _ask_user(self, inp):
        t, qs = self.task, inp.get("questions") or []
        self.questions += 1
        lines = []
        for i, q in enumerate(qs, 1):
            opts = ", ".join(o.get("label", "") for o in q.get("options") or [])
            lines.append(f"Q{i}: {q.get('question', '')}" + (f"  (options: {opts}; or their own answer)" if opts else ""))
        body = "\n".join(lines)
        d = self.app.new_decision(f"{t.id}-q{self.questions}", "question", f"{t.id}-q{self.questions} · question",
                                  body, t)
        d.questions = [q.get("question", "") for q in qs]
        decided_by("user")
        answered, note, _ = await self.app.ask(d, announce=(
            f"[Question {d.id}: while working on task {t.id} you need to ask the user:\n{body}\n]"
            " Ask it the way you'd ask a colleague, in one or two sentences, mentioning the options briefly."))
        if not answered:  # the user said something else instead (see Front._continue)
            t.digest.append("user redirected instead of answering")
            return {"behavior": "deny", "message": "The user didn't answer that; instead: " + (note or "stop and wait")}
        answers = {q.get("question", ""): (d.answers[i] if i < len(d.answers) else "")
                   for i, q in enumerate(qs)}
        t.digest.append("user answered: " + "; ".join(d.answers)[:80])
        return {"behavior": "allow", "updatedInput": {**inp, "answers": answers}}

    def _on_event(self, ev):
        t = ev.get("type")
        try:
            self._live_event(ev)
        except Exception as e:  # noqa: BLE001 - the log must never break the task
            cvlog.log.event("live:error", repr(e))
        if t == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            if self.task.session_id != ev["session_id"]:
                self.task.session_id = ev["session_id"]
                self.app.save_tasks()  # resumable from now on
        elif t == "system" and ev.get("subtype") == "task_started":
            self.agents[ev.get("task_id")] = ev.get("subagent_type") or "agent"
        elif t == "system" and ev.get("subtype") in ("task_updated", "task_notification"):
            if ((ev.get("patch") or {}).get("status") or ev.get("status")) in ("completed", "failed", "stopped",
                                                                                 "killed", "error"):
                self.agents.pop(ev.get("task_id"), None)
        elif t == "assistant":
            u = ev["message"].get("usage") or {}
            if not ev.get("parent_tool_use_id") and u:  # the main conversation, not a subagent's
                self.task.context = sessions.context_tokens(u)
            for block in ev["message"]["content"]:
                if block.get("type") == "text":
                    for line in block["text"].splitlines():
                        if line := line.strip():
                            self.app.ui.dim(f"  {self.task.id} │ {line[:160]}")
                elif block.get("type") == "tool_use":
                    verb, target, _ = describe_tool(block["name"], block.get("input", {}))
                    self.app.ui.dim(f"  {self.task.id} │ ⚙ {verb} {target}")
                    if block["name"] not in ("Read", "Glob", "Grep", "ToolSearch", "TodoWrite"):
                        self.task.digest.append(f"{verb} {target}"[:100])  # Haiku answers "how's it going" from it
        elif t == "_stderr" and ev["text"].strip():
            self.app.ui.dim(f"  {self.task.id} │ stderr: {ev['text'][:160]}")

    async def _results(self):
        """One result per round of work. The session stays open afterwards so follow-ups keep the context."""
        t = self.task
        while (res := await self.proc.results.get()) is not None:
            if t.state in ("stopped", "closed", "saved"):
                self._end_round_early()
                return
            text = (res.get("result") or "").strip()
            ok = res.get("subtype") == "success"
            t0 = getattr(t, "round_t0", None)
            self._live("round_end", duration_s=round(res["duration_ms"] / 1000, 1) if res.get("duration_ms")
                       else round(time.monotonic() - t0, 1) if t0 else 0, turns=res.get("num_turns") or 0, ok=ok,
                       out_tokens=(res.get("usage") or {}).get("output_tokens"))
            t.round_t0 = None
            if self.app.restart_requested:
                return  # the app is going down; this session gets resumed after the restart
            t.state = "idle"
            t.go_ahead = set()  # the request's go-ahead covered this round only
            t.idle_since = time.monotonic()
            asyncio.create_task(self._close_if_still_idle(t.idle_since))
            if t.handover and not t.handover.done():
                t.handover.set_result(text if ok else "")  # Front._fresh takes it from here
                continue
            t.last_result = text
            self._save_output(text)
            self.app.save_tasks()
            self.app.ui.result(t, text, res)
            if await self._after_round(text, ok):
                continue
            if t.fresh_from:
                self.app.front.notify(f"[Started fresh: task {t.id} carries on from {t.fresh_from} with just its "
                                      f"handover note ({t.context // 1000}k tokens now). It says: {text[:300]}] Tell "
                                      "the user in one sentence that you've started fresh and what you can carry on with.")
                t.fresh_from = ""
                continue
            shown = (t.show_next or t.kind == "job") and not self.app.show_output(t)
            t.show_next = False
            long = ""
            if t.nudged < len(CONTEXT_NUDGES) and t.context >= CONTEXT_NUDGES[t.nudged]:
                t.nudged = sum(t.context >= n for n in CONTEXT_NUDGES)
                self.app.ui.status(f"{t.id}: conversation is {t.context // 1000}k tokens; suggesting a fresh start")
                long = (f" Also: this conversation now carries about {t.context // 1000}k tokens, so each reply is "
                        "slower, costs more and is less focused. After your report, say in a sentence that it's "
                        "getting long and offer to start fresh with just the useful context (fresh_start) if they like.")
            self.app.front.notify(
                f"[Task {t.id} {'finished this round' if ok else 'stopped with a problem'}; it stays open for "
                f"follow-ups. Your work log:\n{text[:1200]}\n] Lead with what you found or did, not a recap of "
                "what they asked. "
                + ("The full answer is open in a window on their screen now: give the gist and say it's up there "
                   "to read. " if shown else "")
                + SUMMARY[self.app.verbosity] + " Don't read out file names, code or numbers they'd need to see; "
                "everything is in the task's window." + long)
            if t.fresh_after is not None and not self.running_workers():
                keep, t.fresh_after = t.fresh_after, None
                asyncio.create_task(self.app.front._fresh(t, keep))
        # t.backend is another one when the task carried on in a fresh session (JobBackend._hand_on)
        if t.backend is self and t.state not in ("stopped", "closed", "saved") and not self.app.restart_requested:
            t.state = "failed"
            self.app.front.notify(f"[Task {t.id}'s session ended unexpectedly.] Tell the user in one sentence.")

    async def _after_round(self, text, ok):
        """orchestrator.JobBackend: a hand-back or a budget warning. True = handled, skip the usual report."""
        return False

    def _save_output(self, text):
        """Write this round's full result for the output window."""
        t = self.task
        body = text.strip()
        if not body:
            return
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = os.path.join(OUTPUT_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{t.id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {t.id} · {t.title}\n\n*{t.project_dir} · {time.strftime('%H:%M')}*\n\n{body}\n")
        t.output = path
        for old in sorted(os.listdir(OUTPUT_DIR))[:-KEEP_OUTPUTS]:
            os.remove(os.path.join(OUTPUT_DIR, old))

    async def _close_if_still_idle(self, since):
        """Free the claude process (~300 MB) of a task that has sat idle; it stays resumable as a saved task."""
        await asyncio.sleep(IDLE_CLOSE_S)
        t = self.task
        if t.state != "idle" or t.idle_since != since or not t.session_id or self.app.exit_requested:
            return  # it got new work in the meantime, or can't be resumed
        if self.running_workers():  # closing would kill them; their finish starts a new round and a new timer
            return
        t.was, t.saved_at = "idle", time.time()
        await self.stop("saved")
        t.backend = None
        self.app.save_tasks()
        self.app.ui.status(f"{t.id}: idle for {IDLE_CLOSE_S // 60} min, saved (say the word to pick it up again)")


def describe_tool(name, inp):
    """→ (verb, target, card body) for an approval card or a narration line."""
    fp = pathlib.Path(inp.get("file_path") or inp.get("notebook_path") or "").name
    if name == "Edit":
        diff = difflib.unified_diff(inp.get("old_string", "").splitlines(), inp.get("new_string", "").splitlines(),
                                    lineterm="", n=1)
        return "edit", fp, "\n".join(list(diff)[2:42]) or "(no textual change)"
    if name == "MultiEdit":
        return "edit", fp, f"{len(inp.get('edits', []))} edits"
    if name == "Write":
        lines = inp.get("content", "").splitlines()
        return "write", fp, "\n".join(lines[:30]) + (f"\n… ({len(lines)} lines)" if len(lines) > 30 else "")
    if name in SHELL_TOOLS:
        return "run", inp.get("description") or "a command", "$ " + inp.get("command", "")
    if name.endswith("restart_voice_app"):
        cmd = inp.get("run_while_down")
        return "restart", "the voice app", (f"why: {inp.get('reason', '')}" + (f"\nwhile it's down: $ {cmd}" if cmd else ""))
    if name == "WebFetch":
        return "fetch", inp.get("url", "a page"), inp.get("prompt", "")
    if name == "WebSearch":
        return "search the web for", repr(inp.get("query", "")), ""
    if name in ("Read", "Glob", "Grep"):
        return name.lower(), fp or inp.get("pattern", ""), json.dumps(inp)[:300]
    return f"use {name}", "", json.dumps(inp, indent=1)[:600]
