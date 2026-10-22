"""Session logs, for debugging what cv did: one folder per launch under ~/.local/state/claude-voice/logs/.

  events.log     one line per event: what was said and heard, tool calls, permissions, task steps, results, state
                 changes, errors (`latest` links to the newest session)
  raw/<name>.jsonl   every stream-json message to and from each Claude Code process (front, task-t1, …)

Everything goes through `log`, which does nothing until start() is called, so tests and tools stay quiet.
"""

import datetime
import json
import os
import shutil
import threading
import time

import platform_util

KEEP = 15  # sessions kept


def state_dir():
    return platform_util.state_dir()


def _root():
    return os.path.join(state_dir(), "logs")


class _Null:
    dir = None

    def event(self, kind, text=""):
        pass

    def audio(self, samples, text):
        pass

    def raw(self, name, direction, data):
        pass


class SessionLog:
    def __init__(self):
        root = _root()
        self.dir = os.path.join(root, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(os.path.join(self.dir, "raw"), exist_ok=True)
        self.lock = threading.Lock()
        self.events = open(os.path.join(self.dir, "events.log"), "a", buffering=1, encoding="utf-8")
        self.raws = {}
        self.t0 = time.monotonic()
        platform_util.point_latest(root, self.dir)  # a symlink, or latest.txt where symlinks aren't allowed
        old = sorted(d for d in os.listdir(root) if d != "latest" and os.path.isdir(os.path.join(root, d)))
        for d in old[:-KEEP]:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    def event(self, kind, text=""):
        text = str(text).replace("\n", " ⏎ ")
        line = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]} +{time.monotonic() - self.t0:7.1f}s " \
               f"{kind:<14} {text}\n"
        with self.lock:
            self.events.write(line)

    def audio(self, samples, text):
        """With CV_LOG_AUDIO=1, keep each utterance as a WAV (16 kHz) next to the log, to hear what was clipped."""
        if os.environ.get("CV_LOG_AUDIO") != "1":
            return
        import wave
        import numpy as np
        d = os.path.join(self.dir, "audio")
        os.makedirs(d, exist_ok=True)
        n = len(os.listdir(d)) + 1
        path = os.path.join(d, f"{n:03d}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        self.event("audio", f"{os.path.basename(path)} {len(samples) / 16000:.1f}s :: {text}")

    def raw(self, name, direction, data):
        with self.lock:
            f = self.raws.get(name)
            if f is None:
                f = self.raws[name] = open(os.path.join(self.dir, "raw", f"{name}.jsonl"), "a", buffering=1, encoding="utf-8")
            f.write(json.dumps({"t": round(time.monotonic() - self.t0, 3), "dir": direction, "data": data}) + "\n")


log = _Null()


def start():
    global log
    if isinstance(log, _Null):
        log = SessionLog()
    return log


class LoggedUi:
    """Wraps a UI so every call the engine makes to it is also logged."""

    def __init__(self, ui):
        self._ui = ui

    def __getattr__(self, name):
        target = getattr(self._ui, name)
        if not callable(target):
            return target

        def call(*args, **kw):
            log.event(f"ui.{name}", _describe(name, args))
            return target(*args, **kw)
        return call


def _describe(name, args):
    if name == "card" and args:
        d = args[0]
        return f"{d.id} [{d.kind}] {d.title} :: {d.body[:400]}"
    if name == "result" and len(args) >= 3:
        task, text, res = args[:3]
        return f"{task.id} {res.get('subtype')} turns={res.get('num_turns')} :: {text[:400]}"
    return " | ".join(str(a)[:400] for a in args)
