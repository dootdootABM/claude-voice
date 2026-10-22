"""Text files are always UTF-8, whatever the platform default (cp1252 on Windows): a static scan for text-mode file
calls with no encoding, and a round trip of emoji and box characters under a non-UTF-8 default.
Run: pytest test_encoding.py"""

import ast
import glob
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def check(label, ok, detail=""):
    assert ok, f"{label}: {detail}" if detail else label


ALLOW = set()  # no file is allowed to skip the scan


def mode_of(call, pos):
    """The literal mode string of an open()-like call ('r' when absent), or None when it isn't a literal."""
    node = call.args[pos] if len(call.args) > pos else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if node is None:
        return "r"
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def missing_encoding(path):
    bad = []
    for call in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if not isinstance(call, ast.Call):
            continue
        f = call.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ("read_text", "write_text"):
            # Path methods: encoding is the first positional (read) / second (write)
            has = any(k.arg == "encoding" for k in call.keywords) or len(call.args) > (0 if name == "read_text" else 1)
        elif name in ("open", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"):
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "wave":
                continue
            if name == "open" and isinstance(f, ast.Attribute) and not (
                    isinstance(f.value, ast.Name) and f.value.id in ("os", "io", "codecs", "Path")):
                continue  # a method named open (e.g. a backend's), not the file builtin
            if name == "open" and isinstance(f, ast.Attribute) and f.value.id == "os":
                continue  # os.open is a file descriptor call
            mode = mode_of(call, 1 if name == "open" else 0)
            if mode is not None and "b" in mode:
                continue
            has = any(k.arg == "encoding" for k in call.keywords)
        else:
            if any(k.arg in ("text", "universal_newlines") and getattr(k.value, "value", None) is True
                   for k in call.keywords) and not any(k.arg == "encoding" for k in call.keywords):
                bad.append(call.lineno)
            continue
        if not has:
            bad.append(call.lineno)
    return bad


SIDECAR_FILES = sorted(glob.glob(os.path.join(HERE, "*.py")))


@pytest.mark.parametrize("path", [p for p in SIDECAR_FILES if os.path.basename(p) not in ALLOW],
                         ids=os.path.basename)
def test_every_text_mode_call_names_an_encoding(path):
    bad = missing_encoding(path)
    check(f"{os.path.basename(path)}: every text-mode call names an encoding", not bad, f"lines {bad}" if bad else "")


def test_scanned_the_sidecar():
    scanned = sum(1 for p in SIDECAR_FILES if os.path.basename(p) not in ALLOW)
    check("scanned the sidecar", scanned >= 10, str(scanned))



def test_scan_catches_what_it_exists_for(tmp_path):
    # the scan itself catches the mistakes it exists for
    probe = os.path.join(str(tmp_path), "probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write('open("a")\nopen("a", "w")\nopen("a", "rb")\nopen("a", encoding="utf-8")\nP.read_text()\nP.write_text("x")\n'
                'subprocess.run(["x"], text=True)\n')
    check("scan flags the bare calls and only those", missing_encoding(probe) == [1, 2, 5, 6, 7], str(missing_encoding(probe)))


# runtime: a non-UTF-8 default, round trip through the writers
TEXT = "emoji 🗣 ⏎ ─▌▸✔ ⣿⠁⡇ — é"
CODE = r'''
import json, os, sys, asyncio, types
TEXT = %r
import cvlog
log = cvlog.start()
log.event("test", TEXT)
log.raw("claude", "in", TEXT)
log.events.flush()
for f in log.raws.values():
    f.flush()
ev = open(os.path.join(log.dir, "events.log"), "rb").read().decode("utf-8")
assert TEXT in ev, ev
raw = open(os.path.join(log.dir, "raw", "claude.jsonl"), "rb").read().decode("utf-8")
assert json.loads(raw)["data"] == TEXT

import tui
tui.save_prefs({"t": TEXT})
assert tui.load_prefs() == {"t": TEXT}

import orchestrator as o
b = object.__new__(o.JobBackend)
b.dir = os.path.join(os.environ["CLAUDE_VOICE_STATE_DIR"], "job")
b.task = types.SimpleNamespace(digest=[])
asyncio.run(b._save_state({"markdown": TEXT}))
assert b.state().strip() == TEXT, b.state()
print("roundtrip ok", __import__("locale").getpreferredencoding(False))
''' % ascii(TEXT)


def test_round_trip_under_non_utf8_default():
    env = {**os.environ, "PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
           "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp()}
    r = subprocess.run([sys.executable, "-c", CODE], cwd=HERE, env=env, capture_output=True, encoding="utf-8", errors="replace")
    check("round trip through cvlog, tui prefs and job state under PYTHONUTF8=0 LC_ALL=C", r.returncode == 0,
          (r.stdout + r.stderr).strip()[-600:])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
