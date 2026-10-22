"""Fetch the Kokoro text-to-speech files into sidecar/models/ (git-ignored). Standard library only.

  python get_models.py              all three files (fp32 for CUDA, int8 for CPU, the voices)
  python get_models.py --cpu        only what a machine without CUDA needs (int8 and the voices)
  python get_models.py --from DIR   take them from another copy's models folder (hard link, else copy), no download

Files already there with the right size are skipped. The Whisper models need no step: faster-whisper fetches them on
first run.
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


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="get-models", description="Fetch the Kokoro model files into sidecar/models/.")
    ap.add_argument("--cpu", action="store_true", help="only the files a machine without CUDA needs")
    ap.add_argument("--from", dest="src", metavar="DIR", help="take them from another models folder instead")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
