"""Fetch the Kokoro text-to-speech files into sidecar/models/ (git-ignored). Standard library only.

  python get_models.py              all three files (fp32 for CUDA, int8 for CPU, the voices)
  python get_models.py --cpu        only what a machine without CUDA needs (int8 and the voices)
  python get_models.py --from DIR   take them from another copy's models folder (hard link, else copy), no download

Files already there with the right size are skipped. On Linux the Whisper models need no step: faster-whisper fetches
them on first run.

Windows also fetches the Whisper models (distil-large-v3 and small.en; small.en only with --cpu) into the Hugging Face
cache, so the first launch doesn't sit at "waking up" while ~2 GB downloads with no progress shown (and there the hf-xet
transfer was seen to stall for good). That part needs huggingface_hub from the uv environment, which
scripts\\get-models.cmd runs it in; without it, it's skipped with a note. --no-whisper skips it; --from leaves it alone.
"""

import argparse
import os
import shutil
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {  # name → size in bytes, to tell a complete file from a broken download
    "kokoro-v1.0.onnx": 325532387,
    "kokoro-v1.0.int8.onnx": 92361271,
    "voices-v1.0.bin": 28214398,
}
CPU_FILES = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
WHISPER = {  # name → Hugging Face repo, as faster_whisper.utils._MODELS maps them (engine.py picks the names)
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "small.en": "Systran/faster-whisper-small.en",
}
CPU_WHISPER = ("small.en",)
WHISPER_UP_FRONT = os.name == "nt"  # Windows only; Linux leaves them to faster-whisper on first run
WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]  # as
# faster_whisper.utils.download_model asks for, so WhisperModel finds them in the cache and downloads nothing


def complete(path, size):
    return os.path.isfile(path) and os.path.getsize(path) == size


def download(name, size, out=sys.stdout):
    dest = os.path.join(MODELS, name)
    part = dest + ".part"
    with urllib.request.urlopen(URL + name, timeout=60) as r, open(part, "wb") as f:
        done, step = 0, size // 20 or 1
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if done // step != (done - len(chunk)) // step:
                print(f"\r  {name}: {done * 100 // size}%", end="", file=out, flush=True)
    print(file=out)
    if os.path.getsize(part) != size:
        os.remove(part)
        raise OSError(f"{name}: the download was incomplete; run it again")
    os.replace(part, dest)


def take(name, size, src):
    """Hard-link (or copy) a complete file from another models folder."""
    from_path, dest = os.path.join(src, name), os.path.join(MODELS, name)
    if not complete(from_path, size):
        raise OSError(f"{from_path} is missing or incomplete")
    if os.path.exists(dest):
        os.remove(dest)
    try:
        os.link(from_path, dest)
    except OSError:
        shutil.copyfile(from_path, dest)


def fetch_whisper(names, out=sys.stdout):
    """Download the Whisper models into the Hugging Face cache. → False if huggingface_hub isn't installed here."""
    # hf-xet transfers were seen to stall for good partway through model.bin; plain HTTP doesn't
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return False
    for name in names:
        print(f"  whisper {name}: fetching (or checking) {WHISPER[name]}", file=out, flush=True)
        snapshot_download(WHISPER[name], allow_patterns=WHISPER_FILES)
    return True


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="get-models", description="Fetch the Kokoro model files into sidecar/models/.")
    ap.add_argument("--cpu", action="store_true", help="only the files a machine without CUDA needs")
    ap.add_argument("--from", dest="src", metavar="DIR", help="take them from another models folder instead")
    ap.add_argument("--no-whisper", action="store_true", help="Windows: skip the Whisper speech-to-text models")
    args = ap.parse_args(argv)
    os.makedirs(MODELS, exist_ok=True)
    names = CPU_FILES if args.cpu else tuple(FILES)
    for name in names:
        if complete(os.path.join(MODELS, name), FILES[name]):
            print(f"  {name}: already there", file=out)
            continue
        try:
            if args.src:
                take(name, FILES[name], os.path.expanduser(args.src))
                print(f"  {name}: taken from {args.src}", file=out)
            else:
                download(name, FILES[name], out)
        except OSError as e:
            print(f"get-models: {e}", file=sys.stderr)
            return 1
    print(f"Models are in {MODELS}", file=out)
    if not WHISPER_UP_FRONT or args.src or args.no_whisper:
        return 0
    try:
        if not fetch_whisper(CPU_WHISPER if args.cpu else tuple(WHISPER), out):
            print("  whisper: skipped, huggingface_hub isn't installed for this Python. Run `uv sync` in sidecar/ and "
                  "get-models again, or let cv fetch them on first run.", file=out)
    except Exception as e:  # noqa: BLE001 - network or hub errors: the Kokoro part is done, say what failed
        print(f"get-models: the Whisper models didn't download ({e}); run it again", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
