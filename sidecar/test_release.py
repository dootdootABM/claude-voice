"""What gets published: nothing personal in the files git tracks, and the model fetcher (get_models.py) without the
network.

The leak check scans every tracked file for personal-looking paths and e-mail addresses, and, on a machine with a
real config, for that config's own user.name and user.system (CI has no such file, so that part is skipped there).

Run: pytest test_release.py
"""

import getpass
import io
import os
import re
import subprocess
from pathlib import Path

import pytest

import config
import get_models
import platform_util

ROOT = Path(__file__).resolve().parent.parent
# Placeholder people in tests and docs (C:\Users\x, /home/u/proj, /home/user) are fine; anyone else's home is not.
HOME_PATH = re.compile(r"/home/(?!(?:u|x|user|you|me|runner|<[^>]*>)\b)[a-z][\w.-]+|"
                       r"[A-Za-z]:\\Users\\(?!(?:x|a|u|you|user|me|runneradmin|<[^>]*>)\b)[\w.-]+", re.I)
EMAIL = re.compile(r"\b[\w.+-]+@(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|proton(?:mail)?)\.\w+", re.I)
ALLOWED_EMAILS = ("noreply@anthropic.com",)


def tracked():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    for name in filter(None, out.decode("utf-8").split("\0")):
        path = ROOT / name
        if path.is_file():
            try:
                yield name, path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary


def test_no_personal_paths_or_emails():
    found = []
    for name, text in tracked():
        found += [f"{name}: {m.group(0)}" for m in HOME_PATH.finditer(text)]
        found += [f"{name}: {m.group(0)}" for m in EMAIL.finditer(text) if m.group(0).lower() not in ALLOWED_EMAILS]
    assert not found, "personal details in tracked files:\n" + "\n".join(found)


def test_nothing_from_the_real_config():
    path = os.path.join(platform_util.config_dir(), "config.yaml")  # the real one (the tests point CLAUDE_VOICE_CONFIG away)
    if not os.path.isfile(path):
        pytest.skip("no real config on this machine")
    user = config.load(path, voices=None)["user"]
    words = [w for w in (user["name"], user["system"]) if w and w not in (config.DEFAULTS["user"]["name"],
                                                                            config.DEFAULTS["user"]["system"])]
    if not os.environ.get("CI"):
        words.append(getpass.getuser())
    words = [w for w in words if len(w) >= 4]
    found = [f"{name}: {w}" for name, text in tracked() for w in words if re.search(rf"\b{re.escape(w)}\b", text, re.I)]
    assert not found, "your own details in tracked files:\n" + "\n".join(found)


def test_fixtures_and_scripts_are_tracked():
    names = {n for n, _ in tracked()}
    for need in ("sidecar/testdata/live_sample.jsonl", "scripts/get-models", "scripts/get-models.cmd",
                 "scripts/try-fresh", "scripts/try-fresh.cmd", "scripts/install-cv.cmd", "config.example.yaml"):
        assert need in names, f"{need} is not tracked by git"


# ---- get_models --------------------------------------------------------------------------------------------------
@pytest.fixture
def models(monkeypatch, tmp_path):
    files = {"a.onnx": 5, "a.int8.onnx": 3, "voices.bin": 4}
    monkeypatch.setattr(get_models, "FILES", files)
    monkeypatch.setattr(get_models, "CPU_FILES", ("a.int8.onnx", "voices.bin"))
    monkeypatch.setattr(get_models, "MODELS", str(tmp_path / "models"))
    monkeypatch.setattr(get_models, "fetch_whisper", lambda names, out=None: True)  # no Hugging Face downloads
    return tmp_path, files


def test_from_another_copy_and_skip_complete(models):
    tmp, files = models
    src = tmp / "other"
    src.mkdir()
    for name, size in files.items():
        (src / name).write_bytes(b"x" * size)
    out = io.StringIO()
    assert get_models.main(["--from", str(src), "--cpu"], out) == 0
    assert sorted(os.listdir(get_models.MODELS)) == ["a.int8.onnx", "voices.bin"]
    assert get_models.main(["--from", str(src), "--cpu"], out) == 0 and "already there" in out.getvalue()
    (src / "a.onnx").write_bytes(b"x")  # incomplete in the source: refused
    assert get_models.main(["--from", str(src)], io.StringIO()) == 1


def test_download_checks_the_size(models, monkeypatch):
    bodies = {"a.onnx": b"12345", "a.int8.onnx": b"12", "voices.bin": b"1234"}  # a.int8.onnx comes back short
    monkeypatch.setattr(get_models.urllib.request, "urlopen",
                        lambda url, timeout=0: io.BytesIO(bodies[url.rsplit("/", 1)[1]]))
    assert get_models.main([], io.StringIO()) == 1
    have = sorted(os.listdir(get_models.MODELS))
    assert have == ["a.onnx"]  # the short one was not kept, not even as .part
    bodies["a.int8.onnx"] = b"123"
    assert get_models.main([], io.StringIO()) == 0
    assert sorted(os.listdir(get_models.MODELS)) == ["a.int8.onnx", "a.onnx", "voices.bin"]


def test_linux_leaves_whisper_to_first_run(models, monkeypatch):
    tmp, files = models
    src = tmp / "other"
    src.mkdir()
    for name, size in files.items():
        (src / name).write_bytes(b"x" * size)
    monkeypatch.setattr(get_models, "WHISPER_UP_FRONT", False)
    asked = []
    monkeypatch.setattr(get_models, "fetch_whisper", lambda names, out=None: asked.append(names) or True)
    assert get_models.main(["--from", str(src)], io.StringIO()) == 0
    assert get_models.main(["--cpu"], io.StringIO()) == 0
    assert get_models.main([], io.StringIO()) == 0
    assert asked == []


def test_whisper_models_follow_the_flags(models, monkeypatch):
    tmp, files = models
    src = tmp / "other"
    src.mkdir()
    for name, size in files.items():
        (src / name).write_bytes(b"x" * size)
    monkeypatch.setattr(get_models, "WHISPER_UP_FRONT", True)  # Windows
    asked = []
    monkeypatch.setattr(get_models, "fetch_whisper", lambda names, out=None: asked.append(names) or True)
    assert get_models.main(["--from", str(src)], io.StringIO()) == 0  # all three Kokoro files there: no downloads
    assert get_models.main(["--cpu"], io.StringIO()) == 0
    assert get_models.main([], io.StringIO()) == 0
    assert get_models.main(["--no-whisper"], io.StringIO()) == 0
    assert asked == [("small.en",), ("distil-large-v3", "small.en")]  # --from and --no-whisper: none

    out = io.StringIO()  # huggingface_hub not installed for this Python: a note, not a failure
    monkeypatch.setattr(get_models, "fetch_whisper", lambda names, out=None: False)
    assert get_models.main([], out) == 0 and "uv sync" in out.getvalue()

    def fail(names, out=None):
        raise OSError("network down")
    monkeypatch.setattr(get_models, "fetch_whisper", fail)
    assert get_models.main([], io.StringIO()) == 1


def test_fetch_whisper_asks_for_what_faster_whisper_loads(monkeypatch):
    import sys
    import types
    got = []
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = lambda repo, allow_patterns: got.append((repo, allow_patterns))
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    assert get_models.fetch_whisper(("small.en",), io.StringIO())
    assert got == [("Systran/faster-whisper-small.en", get_models.WHISPER_FILES)]
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # not installed
    assert get_models.fetch_whisper(("small.en",), io.StringIO()) is False
