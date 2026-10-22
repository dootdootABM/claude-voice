"""One config file for everything a user would want to change: who they are, how it behaves, which models, which voice,
what the prompts say. Merge order: built-in defaults < the user's config.yaml < command-line flags (engine.make_parser
takes its defaults from here, so a flag given on the command line always wins).

The file is YAML (PyYAML). If PyYAML isn't installed the built-in defaults are used and a warning says so.
A bad value raises ConfigError with a message naming the key and the line; unknown keys only produce a warning.
`python config.py --write-example` regenerates ../config.example.yaml from the defaults below (a test keeps them in sync).
"""

import copy
import difflib
import importlib
import json
import os
import pathlib
import re
import shutil
import sys

import platform_util
from default_prompts import PROMPTS

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
EXAMPLE = ROOT / "config.example.yaml"
VOICES_FILE = HERE / "models" / "voices-v1.0.bin"

DEFAULTS = {
    "user": {
        "name": "Alex",
        "system": "Ubuntu 24.04, GNOME, 16 GB RAM, RTX 3060",
        "project_dirs": ["~/src", "~"],
        "notes_dir": "",
        "preferences": "They like fast, minimal setups and short answers.",
        "load_global_claude_md": True,
    },
    "behaviour": {"verbosity": "normal", "auto_show_output": True, "half_duplex": False},
    "models": {"front": "haiku", "backend": "sonnet", "job": "sonnet", "scout": "haiku", "checker": "sonnet"},
    "voice": {"name": "af_bella", "blend": {}, "speed": 1.05, "lang": "en-us"},
    "stt": {"device": "auto", "model": None, "fast_model": True},
    "input": {"mode": "always_listening", "ptt_key": "v", "global_ptt_key": None},
    "prompts": dict(PROMPTS),
}

# One line per key, written above it in config.example.yaml.
COMMENTS = {
    "user": "Who you are. Filled into the prompts wherever they use {name}, {system}, {project_dirs} ...",
    "user.name": "What the assistant calls you.",
    "user.system": "Your machine, in a line: OS, desktop, hardware. Helps it give advice that fits.",
    "user.project_dirs": "Folders where your projects live. The first one is where new projects go.",
    "user.notes_dir": 'Optional: a notes folder it may mention ("" = none).',
    "user.preferences": "Anything else it should know about how you like things.",
    "user.load_global_claude_md": "Also load ~/.claude/CLAUDE.md into the assistant's context.",
    "behaviour": "How it behaves.",
    "behaviour.verbosity": "How much it says when a task finishes a round: quiet | normal | chatty.",
    "behaviour.auto_show_output": "Open the live output window when a task starts.",
    "behaviour.half_duplex": "Ignore the microphone while it speaks (use this without headphones).",
    "models": "Which Claude models do what (an alias like haiku/sonnet/opus, or a full model id).",
    "models.front": "The voice assistant you talk to.",
    "models.backend": "Default model for quick tasks.",
    "models.job": "Default model for jobs (orchestrator, builders, reviewers).",
    "models.scout": "A job's scouts, which only read code to plan it: a cheaper model is enough (\"\" = models.job).",
    "models.checker": "The step checker, which reviews each edit and command a job's workers make before it runs.",
    "voice": "Text to speech (Kokoro). `cv --list-voices` shows every voice.",
    "voice.name": "Any voice in models/voices-v1.0.bin.",
    "voice.blend": "Optional mix of voices, e.g. {af_bella: 0.6, af_heart: 0.4}. When set it replaces voice.name.",
    "voice.speed": "Speaking speed, 0.5 to 2.0.",
    "voice.lang": "Language of the text it speaks (en-us, en-gb, fr-fr, it, ja, cmn, ...).",
    "stt": "Speech recognition (Whisper).",
    "stt.device": "auto | cuda | cpu. auto uses an NVIDIA GPU when there is one.",
    "stt.model": "Whisper model; null = distil-large-v3 on cuda, small.en on cpu.",
    "stt.fast_model": "Use small.en for short answers while an approval or question is waiting (cuda only).",
    "input": "How you talk to it.",
    "input.mode": "always_listening | push_to_talk.",
    "input.ptt_key": "Key to hold in the cv window to talk, in push_to_talk mode.",
    "input.global_ptt_key": 'Optional system-wide push-to-talk key such as "ctrl+alt+v"; null = off.',
    "prompts": "Each key you set REPLACES the built-in text completely (it is not merged). Leave a key out, or set it "
               "to null, to keep the built-in text. Placeholders in {braces} are filled in: {name} {system} "
               "{project_dirs} {new_project_dir} {notes_dir} {notes_line} {preferences} {app_dir}; "
               "handover_start also needs {project} and {note}, handover_job needs {note}.",
    "prompts.front_agent": "The voice assistant's rules: how it talks, when it asks, which tools it uses.",
    "prompts.front_context": "Who you are and where things are (uses the user section).",
    "prompts.narration": "What every task is told about speaking progress lines.",
    "prompts.go_note": "Added to a task you gave a clear instruction for: start without asking.",
    "prompts.show_note": "Added to a task whose answer you want to read on screen.",
    "prompts.short_ack": "Added to the reply the assistant gets after handing over work.",
    "prompts.handover_ask": "Asks a long conversation for a handover note.",
    "prompts.handover_start": "Starts the fresh conversation from the note. Needs {project} and {note}.",
    "prompts.handover_job": "Starts a fresh orchestrator from the note. Needs {note}.",
    "prompts.orchestrator": "The orchestrator's brief for jobs.",
    "prompts.step_check": "The step checker's instructions for jobs.",
}

# Placeholders a prompt must contain (the code fills them in when it uses the prompt).
REQUIRED = {"handover_start": ("project", "note"), "handover_job": ("note",)}
BASE_PLACEHOLDERS = ("name", "system", "project_dirs", "new_project_dir", "notes_dir", "notes_line", "preferences",
                     "app_dir")
KNOWN_PLACEHOLDERS = BASE_PLACEHOLDERS + ("project", "note")
CHOICES = {"behaviour.verbosity": ("quiet", "normal", "chatty"), "stt.device": ("auto", "cuda", "cpu"),
           "input.mode": ("always_listening", "push_to_talk")}
LANG_OF = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "j": "ja", "p": "pt-br",
           "z": "cmn"}
LANG_NAME = {"a": "American English", "b": "British English", "e": "Spanish", "f": "French", "h": "Hindi",
             "i": "Italian", "j": "Japanese", "p": "Brazilian Portuguese", "z": "Mandarin Chinese"}
GENDER_NAME = {"f": "female", "m": "male"}


class ConfigError(Exception):
    """The config file can't be used. str(e) is a readable, multi-line message (no traceback needed)."""


# ---- voices --------------------------------------------------------------------------------------------------
def available_voices(path=None):
    """The voice names in voices-v1.0.bin, or None when the file isn't there (e.g. in CI)."""
    path = pathlib.Path(path or VOICES_FILE)
    if not path.exists():
        return None
    import numpy as np
    with np.load(path) as z:
        return sorted(z.files)


def voice_problem(name, voices):
    """None if `name` is a voice (or voices is unknown); else a message with a did-you-mean."""
    if voices is None or name in voices:
        return None
    close = difflib.get_close_matches(str(name), voices, n=1, cutoff=0.6)
    return f"{name!r} is not a Kokoro voice" + (f"; did you mean {close[0]!r}?" if close else " (see `cv --list-voices`)")


def format_voices(voices, current=()):
    """The text of `cv --list-voices`: grouped by language, then gender; * marks the configured voice(s)."""
    groups = {}
    for v in voices:
        lang, gender = v[:1], v[1:2]
        groups.setdefault((lang, gender), []).append(v)
    lines = []
    for (lang, gender), names in sorted(groups.items(), key=lambda kv: (list(LANG_NAME).index(kv[0][0])
                                                                       if kv[0][0] in LANG_NAME else 99, kv[0][1])):
        title = LANG_NAME.get(lang, f"language '{lang}'")
        code = LANG_OF.get(lang)
        lines.append(f"{title}{f' ({code})' if code else ''}, {GENDER_NAME.get(gender, gender)}:")
        lines.append("  " + "  ".join(n + ("*" if n in current else "") for n in names))
    return "\n".join(lines)


# ---- placeholders --------------------------------------------------------------------------------------------
def fill(text, values):
    """Replace {key} for each key in values; other braces (JSON in a prompt, say) are left alone."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def _join(items):
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


# ---- loading -------------------------------------------------------------------------------------------------
def _yaml():
    try:
        return importlib.import_module("yaml")
    except ImportError:
        return None


def _lines(node, prefix=(), out=None):
    """Map each key path in a composed YAML tree to its 1-based line."""
    out = {} if out is None else out
    if node is not None and node.__class__.__name__ == "MappingNode":
        for k, v in node.value:
            path = prefix + (str(k.value),)
            out[path] = k.start_mark.line + 1
            _lines(v, path, out)
    return out


class Config:
    def __init__(self, data, path=None, warnings=(), first_run=False):
        self.data, self.path, self.warnings, self.first_run = data, path, list(warnings), first_run

    def __getitem__(self, section):
        return self.data[section]

    # ---- prompts ----
    def values(self, **extra):
        u = self.data["user"]
        dirs = u["project_dirs"]
        notes = u["notes_dir"]
        return {"name": u["name"], "system": u["system"], "project_dirs": _join(dirs), "new_project_dir":
                (dirs[0] if dirs else "~").rstrip("/") or "~", "notes_dir": notes,
                "notes_line": f" Notes are in {notes}." if notes else "", "preferences": u["preferences"],
                "app_dir": str(ROOT).replace(os.path.expanduser("~"), "~", 1), **extra}

    def prompt(self, key, **extra):
        """The prompt `key` with placeholders filled in (extra: the ones the caller knows, like project and note)."""
        return fill(self.data["prompts"][key], self.values(**extra))

    # ---- voice ----
    def voice_spec(self):
        """What to pass as Kokoro's voice: a name, or a tuple of (name, weight) for a blend."""
        v = self.data["voice"]
        if v["blend"]:
            return tuple((str(k), float(w)) for k, w in v["blend"].items())
        return v["name"]

    def warm_voice(self):
        spec = self.voice_spec()
        return spec if isinstance(spec, str) else spec[0][0]


def _check(data, user, lines, voices, errors, warnings, label):
    """Validate the merged `data` (user's raw values in `user`), appending to errors / warnings."""
    def where(path):
        n = lines.get(tuple(path.split(".")))
        return f"{label}: line {n}: {path}" if n else f"{label}: {path}"

    def err(path, msg):
        errors.append(f"{where(path)}: {msg}")

    # unknown keys
    for sec, vals in user.items():
        if sec not in DEFAULTS:
            warnings.append(f"{where(sec)}: unknown section, ignored")
        elif isinstance(vals, dict):
            for k in vals:
                if k not in DEFAULTS[sec]:
                    warnings.append(f"{where(sec + '.' + k)}: unknown key, ignored")

    def typed(path, ok, what, value):
        if not ok(value):
            err(path, f"{value!r} is not {what}")
            return False
        return True

    is_str = lambda v: isinstance(v, str)  # noqa: E731
    is_bool = lambda v: isinstance(v, bool)  # noqa: E731
    is_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    g = lambda path: data[path.split(".")[0]][path.split(".")[1]]  # noqa: E731
    for p in ("user.name", "user.system", "user.notes_dir", "user.preferences", "voice.lang"):
        typed(p, is_str, "text", g(p))
    for p in ("user.load_global_claude_md", "behaviour.auto_show_output", "behaviour.half_duplex",
              "stt.fast_model"):
        typed(p, is_bool, "true or false", g(p))
    dirs = g("user.project_dirs")
    if isinstance(dirs, str):
        data["user"]["project_dirs"] = dirs = [dirs]
    typed("user.project_dirs", lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v), "a list of folders",
          dirs)
    for p, choices in CHOICES.items():
        if g(p) not in choices:
            err(p, f"{g(p)!r} is not one of {' | '.join(choices)}")
    for p in ("models.front", "models.backend", "models.job", "models.checker", "input.ptt_key"):
        typed(p, lambda v: isinstance(v, str) and v.strip(), "a non-empty text", g(p))
    typed("models.scout", lambda v: isinstance(v, str), 'a model name, or "" for models.job', g("models.scout"))
    typed("stt.model", lambda v: v is None or (isinstance(v, str) and v.strip()), "a model name or null", g("stt.model"))
    typed("input.global_ptt_key", lambda v: v is None or (isinstance(v, str) and v.strip()), 'a key like "ctrl+alt+v" or null',
          g("input.global_ptt_key"))
    if typed("voice.speed", is_num, "a number", g("voice.speed")) and not 0.5 <= g("voice.speed") <= 2.0:
        err("voice.speed", f"{g('voice.speed')} is outside 0.5 to 2.0")
    if is_str(g("voice.name")) and g("voice.name") and (msg := voice_problem(g("voice.name"), voices)):
        err("voice.name", msg)
    blend = g("voice.blend")
    if blend is None:
        data["voice"]["blend"] = blend = {}
    if not isinstance(blend, dict):
        err("voice.blend", f"{blend!r} is not a mapping like {{af_bella: 0.6, af_heart: 0.4}}")
    else:
        for name, w in blend.items():
            if not is_num(w) or w <= 0:
                err("voice.blend", f"the weight of {name!r} must be a number above 0, not {w!r}")
            elif msg := voice_problem(str(name), voices):
                err("voice.blend", msg)
    # prompts
    prompts = data["prompts"]
    for key, text in list(prompts.items()):
        path = f"prompts.{key}"
        if key not in PROMPTS:
            continue
        if text is None:
            prompts[key] = text = PROMPTS[key]
        if not isinstance(text, str) or not text.strip():
            err(path, "must be text (the whole prompt), not " + repr(text)[:40])
            continue
        for ph in REQUIRED.get(key, ()):
            if "{" + ph + "}" not in text:
                err(path, f"must contain the placeholder {{{ph}}} (the app fills it in when it uses this prompt)")
        for ph in sorted(set(re.findall(r"\{(\w+)\}", text)) - set(KNOWN_PLACEHOLDERS)):
            warnings.append(f"{where(path)}: unknown placeholder {{{ph}}}, left as written")


def _merge(user):
    data = copy.deepcopy(DEFAULTS)
    for sec, vals in user.items():
        if sec not in data:
            continue
        if vals is None:
            continue
        if not isinstance(data[sec], dict):
            continue
        if not isinstance(vals, dict):
            raise ConfigError(f"{sec}: expected a section with keys under it, got {vals!r}")
        for k, v in vals.items():
            if k in data[sec]:
                data[sec][k] = v  # a prompt replaces the whole text; a list or mapping replaces the whole value
    return data


def parse(text, label="config.yaml", voices=None):
    """Parse YAML text → Config (defaults + the text's values, validated). Raises ConfigError."""
    yaml = _yaml()
    if yaml is None:
        raise ConfigError(f"{label}: PyYAML is not installed (pip install pyyaml, or uv sync)")
    try:
        user = yaml.safe_load(text)
        node = yaml.compose(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        at = f" line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(e, "problem", None) or str(e)
        raise ConfigError(f"{label}:{at}: this is not valid YAML: {problem}") from None
    if user is None:
        user = {}
    if not isinstance(user, dict):
        raise ConfigError(f"{label}: the file must be a mapping of sections (user:, voice:, ...), not {type(user).__name__}")
    lines = _lines(node)
    for sec, vals in user.items():  # a section that is not a mapping
        if sec in DEFAULTS and vals is not None and not isinstance(vals, dict):
            n = lines.get((str(sec),))
            raise ConfigError(f"{label}: line {n}: {sec}: expected keys under it, got {vals!r}")
    data = _merge(user)
    errors, warnings = [], []
    _check(data, user, lines, voices, errors, warnings, label)
    if errors:
        raise ConfigError("\n".join(errors))
    return Config(data, warnings=warnings)


def defaults():
    return Config(copy.deepcopy(DEFAULTS))


def load(path=None, voices="auto"):
    """The config for this run: first-run copy of the example, then the file merged over the defaults. Raises
    ConfigError (message ready to print)."""
    path = str(path or platform_util.config_path())
    first_run = False
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copyfile(EXAMPLE, path)
            first_run = True
        except OSError:
            pass  # read-only home, or no example (a bare copy of the code): the built-in defaults are the same
    if not os.path.exists(path):
        return defaults()
    if _yaml() is None:
        cfg = defaults()
        cfg.warnings.append(f"PyYAML is not installed, so {path} was not read; using the built-in defaults. "
                            "Install it with `pip install pyyaml` (or `uv sync`).")
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: can't read it: {e}") from None
    cfg = parse(text, label=path, voices=available_voices() if voices == "auto" else voices)
    cfg.path, cfg.first_run = path, first_run
    return cfg


_current = None


def get():
    """The loaded config (loaded once per process, on first use)."""
    global _current
    if _current is None:
        _current = load()
        report(_current)
    return _current


def set_current(cfg):
    global _current
    _current = cfg


def report(cfg, out=None):
    out = out or sys.stderr
    if cfg.first_run:
        print(f"claude-voice: first run: created your config file {cfg.path}\n"
              "  Edit it to set your name, voice, models and prompts (it is read at every start).", file=out)
    for w in cfg.warnings:
        print(f"claude-voice: warning: {w}", file=out)


def prompt(key, **extra):
    return get().prompt(key, **extra)


# ---- the example file ----------------------------------------------------------------------------------------
def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if v == "":
        return '""'
    return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid YAML string


def _wrap(text, indent):
    out, line = [], indent + "#"
    for w in text.split():
        if len(line) + 1 + len(w) > 118:
            out.append(line)
            line = indent + "#"
        line += " " + w
    return out + [line]


def example_text():
    """config.example.yaml, generated from DEFAULTS and COMMENTS."""
    out = ["# Claude Voice configuration.",
           "# Copy of config.example.yaml, created on the first run. Edit it; it is read at every start.",
           "# Order of precedence: built-in defaults < this file < command-line flags (cv --verbosity ..., --device ...).",
           "# Every key is shown with its default. Delete any key you don't want to change.", ""]
    for sec, vals in DEFAULTS.items():
        out += _wrap(COMMENTS[sec], "") + [f"{sec}:"]
        for k, v in vals.items():
            out += _wrap(COMMENTS[f"{sec}.{k}"], "  ")
            if sec == "prompts":
                body = v.strip("\n").split("\n")
                out += [f"  {k}: |-"] + [("    " + ln) if ln else "" for ln in body]
            elif k == "blend":
                out.append("  blend: {}")
            elif isinstance(v, list):
                out.append(f"  {k}: [" + ", ".join(_scalar(i) for i in v) + "]")
            else:
                out.append(f"  {k}: {_scalar(v)}")
            if sec == "prompts":
                out.append("")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


if __name__ == "__main__":
    if "--write-example" in sys.argv:
        EXAMPLE.write_text(example_text(), encoding="utf-8")
        print("wrote", EXAMPLE)
    else:
        print(f"config file: {platform_util.config_path()}")
