"""Audio side of the voice agent: mic + streaming Silero VAD, Whisper STT, Kokoro TTS and an instantly stoppable player."""

import asyncio
import collections
import concurrent.futures
import ctypes
import glob
import os
import pathlib
import re
import threading
import time

import numpy as np

import cvlog
import platform_util

try:  # lazy-safe: importing this module (tests, tooling) must not need PortAudio; opening a stream does
    import sounddevice as sd
except (ImportError, OSError):  # OSError: PortAudio library missing
    sd = None

HERE = pathlib.Path(__file__).resolve().parent
MIC_SR = 16000
FRAME = 512  # 32 ms at 16 kHz, the frame size Silero expects
FRAME_S = FRAME / MIC_SR
TTS_SR = 24000


def pick_device(choice="auto"):
    """'cuda' or 'cpu'. 'auto' is cuda when ctranslate2 sees a CUDA GPU, else cpu."""
    if choice != "auto":
        return choice
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # noqa: BLE001 - no usable GPU runtime means CPU
        return "cpu"


class Resampler:
    """Small streaming mono resampler (numpy only): linear interpolation, preceded by a moving-average low-pass when
    downsampling by 1.5x or more. process() takes any chunk length and returns however many output samples are due."""

    def __init__(self, in_sr, out_sr):
        self.step = in_sr / out_sr  # input samples per output sample
        self.taps = max(1, round(self.step)) if self.step >= 1.5 else 1
        self.tail = np.zeros(self.taps - 1, dtype=np.float32)
        self.buf = np.zeros(0, dtype=np.float32)
        self.pos = 0.0  # where the next output sample falls, in buf coordinates

    def process(self, x):
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self.taps > 1:
            ext = np.concatenate([self.tail, x])
            self.tail = ext[len(ext) - (self.taps - 1):]
            x = np.convolve(ext, np.full(self.taps, 1.0 / self.taps, dtype=np.float32), mode="valid")
        self.buf = np.concatenate([self.buf, x])
        last = len(self.buf) - 1
        count = max(0, int(np.ceil((last - self.pos) / self.step))) if last > 0 else 0
        idx = self.pos + np.arange(count) * self.step
        idx = idx[idx < last]
        i0 = idx.astype(np.int64)
        frac = (idx - i0).astype(np.float32)
        out = (self.buf[i0] * (1 - frac) + self.buf[i0 + 1] * frac).astype(np.float32)
        nxt = self.pos + len(idx) * self.step
        drop = min(int(nxt), len(self.buf))
        self.buf, self.pos = self.buf[drop:], nxt - drop
        return out


class FrameChunker:
    """Regroup a stream of arbitrary-length chunks into exact `size`-sample frames."""

    def __init__(self, size):
        self.size = size
        self.buf = np.zeros(0, dtype=np.float32)

    def push(self, x):
        self.buf = np.concatenate([self.buf, x])
        n = len(self.buf) // self.size
        frames = [self.buf[i * self.size:(i + 1) * self.size].copy() for i in range(n)]
        self.buf = self.buf[n * self.size:]
        return frames


WIN_OUT_LATENCY = 0.05  # seconds; "low" can underrun on Windows shared-mode audio


def _wasapi_auto_convert():
    return sd.WasapiSettings(auto_convert=True)


def _native_rate(kind):
    return int(round(sd.query_devices(kind=kind)["default_samplerate"]))


def preload_cuda_libs():
    # ctranslate2 dlopens cuBLAS/cuDNN by soname (Linux) or DLL name (Windows); make the pip-installed copies findable
    # first so no system CUDA is needed. Never fatal: without the nvidia packages the system's own CUDA may still work.
    try:
        import nvidia
        base = list(nvidia.__path__)[0]
        if os.name == "nt":
            dirs = [d for d in sorted(glob.glob(os.path.join(base, "*", "bin"))) if os.path.isdir(d)]
            for d in dirs:
                os.add_dll_directory(d)
            if dirs:
                os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        else:
            for pattern in ("cublas/lib/libcublasLt.so.*", "cublas/lib/libcublas.so.*", "cudnn/lib/libcudnn*.so.*"):
                for lib in sorted(glob.glob(os.path.join(base, pattern))):
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
    except Exception as e:  # noqa: BLE001
        cvlog.log.event("cuda-libs", f"not preloaded: {e}")


class Vad:
    """Silero VAD run one 512-sample frame at a time, carrying its recurrent state between calls."""

    def __init__(self):
        import faster_whisper
        import onnxruntime
        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(64, dtype=np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.context, frame])[None, :]
        self.context = frame[-64:]
        prob, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        return float(prob.reshape(-1)[0])


class Mic:
    """Default input device → asyncio queue of float32 frames."""

    def __init__(self, loop):
        self.loop = loop
        self.frames = asyncio.Queue()
        self.resampler = self.chunker = None
        if not platform_util.is_windows():
            self.stream = sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32", blocksize=FRAME,
                                         callback=self._cb)
            return
        # Windows shared mode often refuses 16 kHz: let WASAPI convert, else open at the native rate and resample.
        try:
            self.stream = sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32", blocksize=FRAME,
                                         extra_settings=_wasapi_auto_convert(), callback=self._cb)
            cvlog.log.event("mic", "WASAPI auto-convert at 16 kHz")
        except Exception as e:  # noqa: BLE001
            rate = _native_rate("input")
            cvlog.log.event("mic", f"auto-convert failed ({e}); native {rate} Hz with resampling")
            self.resampler, self.chunker = Resampler(rate, MIC_SR), FrameChunker(FRAME)
            self.stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                         blocksize=max(1, round(FRAME * rate / MIC_SR)), callback=self._cb)

    def _cb(self, data, frames, time_info, status):
        x = data[:, 0]
        if self.resampler is None:
            self.loop.call_soon_threadsafe(self.frames.put_nowait, x.copy())
            return
        for f in self.chunker.push(self.resampler.process(x)):
            self.loop.call_soon_threadsafe(self.frames.put_nowait, f)

    def start(self):
        self.stream.start()


class Stt:
    HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye", "so"}

    # distil-large-v3 was the most accurate on the author's voice; Whisper's 30 s window makes it ~1.75 s per utterance on
    # the GTX 1650 regardless of length. small.en is ~0.5 s and fine for short answers ("yes", "go ahead", "no,
    # skip it"), so it's used for those while an approval or question is waiting (+~400 MB VRAM).
    # On CUDA it runs int8_float16; on CPU int8 (the engine then defaults to small.en and no fast model).
    def __init__(self, model="distil-large-v3", fast_model="small.en", device="cuda"):
        if device == "cuda":
            preload_cuda_libs()
            kwargs = dict(device="cuda", compute_type="int8_float16")
        else:
            kwargs = dict(device="cpu", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 4), num_workers=1)
        from faster_whisper import WhisperModel
        self.device = device
        self.models = {"main": WhisperModel(model, **kwargs)}
        if fast_model and fast_model != model:
            self.models["fast"] = WhisperModel(fast_model, **kwargs)
        self.names = {"main": model, "fast": fast_model}
        self.pool = concurrent.futures.ThreadPoolExecutor(1)
        for key in self.models:
            self._run(np.zeros(MIC_SR, dtype=np.float32), key)  # warm-up

    # small.en follows a vocabulary hint (missed "Claude" 0/8 with it, 3/8 without, nothing hallucinated on
    # silence); distil-large-v3 ignores both the hint and hotwords, so its mishearings are fixed in fix_names.
    VOCAB = "Claude, Claude Voice, Claude Code, Haiku, Sonnet, Opus, Kokoro, Whisper, rmpc, cv."

    def _run(self, audio, key="main"):
        model = self.models.get(key, self.models["main"])
        segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                   condition_on_previous_text=False,
                                   initial_prompt=self.VOCAB if key == "fast" else None)
        return fix_names(" ".join(s.text.strip() for s in segs).strip())

    async def transcribe(self, audio, fast=False):
        key = "fast" if fast and "fast" in self.models else "main"
        text = await asyncio.get_running_loop().run_in_executor(self.pool, self._run, audio, key)
        return "" if re.sub(r"[^a-z ]", "", text.lower()).strip() in self.HALLUCINATIONS else text


def fix_names(text):
    """Whisper's usual mishearings of "Claude": "clawed" (rare in this context, always fixed), and "cloud", "clod",
    "clore", "claud" only right before Voice/Code (so a real "cloud" stays; "plot code" is a real phrase)."""
    text = re.sub(r"\bclawed\b", "Claude", text, flags=re.I)
    return re.sub(r"\b(cloud|clod|clore|claud)(\s+)(voice|code)\b",
                  lambda m: f"Claude{m.group(2)}{m.group(3).capitalize()}", text, flags=re.I)


class Player:
    """Plays queued clips back to back; stop() silences within one 20 ms block."""

    def __init__(self):
        self.clips = collections.deque()
        self.lock = threading.Lock()
        self.current = None
        self.pos = 0
        self.gain = 1.0  # ducked while you might be interrupting
        self.applied_gain = 1.0  # what the last block ended at; each block ramps from here to `gain`, so ducking doesn't click
        self.idle_since = time.monotonic()  # when it last ran out of audio (for the listening readout)
        self.level = 0.0  # loudness of the last output block, 0..1 (for the UI)
        # Between clips the stream plays a noise floor (~-86 dBFS, inaudible) instead of exact zeros: USB DACs and
        # headphone amps tend to mute on digital silence and take a moment to wake, which clipped the start of replies.
        self.floor = (np.random.default_rng(0).standard_normal(TTS_SR) * 5e-5).astype(np.float32)
        self.floor_pos = 0
        self.resampler = None
        self.pending = np.zeros(0, dtype=np.float32)  # resampled audio not yet handed to the device
        if not platform_util.is_windows():
            self.stream = sd.OutputStream(samplerate=TTS_SR, channels=1, dtype="float32", blocksize=480,
                                          latency="low", callback=self._cb)
        else:
            try:
                self.stream = sd.OutputStream(samplerate=TTS_SR, channels=1, dtype="float32", blocksize=480,
                                              latency=WIN_OUT_LATENCY, extra_settings=_wasapi_auto_convert(),
                                              callback=self._cb)
            except Exception as e:  # noqa: BLE001
                rate = _native_rate("output")
                cvlog.log.event("speaker", f"auto-convert failed ({e}); native {rate} Hz with resampling")
                self.resampler = Resampler(TTS_SR, rate)
                self.stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                              blocksize=max(1, round(480 * rate / TTS_SR)),
                                              latency=WIN_OUT_LATENCY, callback=self._cb)
        self.stream.start()

    def _cb(self, out, frames, time_info, status):
        if self.resampler is None:
            self._render(out, frames)
            return
        block = np.zeros((480, 1), dtype=np.float32)
        while len(self.pending) < frames:  # render 20 ms at the TTS rate, convert to the device rate
            self._render(block, 480)
            self.pending = np.concatenate([self.pending, self.resampler.process(block[:, 0])])
        out[:, 0] = self.pending[:frames]
        self.pending = self.pending[frames:]

    def _render(self, out, frames):
        out.fill(0)
        try:
            filled = self._fill(out, frames)
        finally:
            self.level = min(1.0, float(np.sqrt(np.mean(out ** 2))) * 4)
        if filled < frames:  # nothing (more) to play in this block
            start = self.floor_pos
            out[filled:, 0] = self.floor[start:start + frames - filled]
            self.floor_pos = (start + frames) % (len(self.floor) - frames)

    def _fill(self, out, frames):
        filled = 0
        with self.lock:
            g0, g1 = self.applied_gain, self.gain
            self.applied_gain = g1
            ramp = None if g0 == g1 else np.linspace(g0, g1, frames, dtype=np.float32)
            while filled < frames:
                if self.current is None:
                    if not self.clips:
                        break
                    self.current, self.pos = self.clips.popleft(), 0
                n = min(frames - filled, len(self.current) - self.pos)
                seg_gain = g1 if ramp is None else ramp[filled:filled + n]
                out[filled:filled + n, 0] = self.current[self.pos:self.pos + n] * seg_gain
                filled += n
                self.pos += n
                if self.pos >= len(self.current):
                    self.current = None
                    if not self.clips:
                        self.idle_since = time.monotonic()
        return filled

    def play(self, clip):
        with self.lock:
            self.clips.append(clip.astype(np.float32))

    def stop(self):
        with self.lock:
            self.clips.clear()
            self.current = None
            self.gain = 1.0

    @property
    def busy(self):
        with self.lock:
            return self.current is not None or bool(self.clips)


class Tts:
    """Sentence queue → Kokoro (CPU thread) → Player. Synthesizes the next sentence while the current one plays."""

    @staticmethod
    def load(device="auto", voice="af_bella", lang="en-us"):
        """Load and warm up Kokoro (a few seconds; safe to run in a thread). Returns (kokoro, device).
        device is the resolved --device: "cpu" keeps Kokoro off the GPU even when CUDA is available.
        voice and lang (from the config) are only used for the warm-up."""
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        # fp32 on CUDA: ~0.4 s per sentence on the GTX 1650 (int8 on CPU: ~2.6 s, and it starves the VAD of CPU).
        # int8 is CPU-only in practice (CUDA falls back per-op), so it's the fallback when CUDA isn't usable.
        has_cuda = device != "cpu" and "CUDAExecutionProvider" in ort.get_available_providers()
        if has_cuda and hasattr(ort, "preload_dlls"):
            try:
                ort.preload_dlls()
            except Exception as e:  # noqa: BLE001 - the CPU provider still works
                cvlog.log.event("cuda-libs", f"ort.preload_dlls failed: {e}")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        if has_cuda:
            # The GPU does the work, so one CPU thread is enough, and the arena grows by what's asked, not in big steps
            # (same speed, ~300 MB less VRAM, ~11 fewer threads).
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            session = ort.InferenceSession(str(HERE / "models/kokoro-v1.0.onnx"), sess_options=opts,
                                           providers=[("CUDAExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"}),
                                                      "CPUExecutionProvider"])
        else:
            opts.intra_op_num_threads = min(4, os.cpu_count() or 2)  # leave CPU for the VAD and Whisper
            session = ort.InferenceSession(str(HERE / "models/kokoro-v1.0.int8.onnx"), sess_options=opts,
                                           providers=["CPUExecutionProvider"])
        kokoro = Kokoro.from_session(session, str(HERE / "models/voices-v1.0.bin"))
        kokoro.create("Warm up.", voice=voice, lang=lang)  # first CUDA run builds kernels
        return kokoro, session.get_providers()[0]

    def __init__(self, player, loaded=None, speed=1.05, lang="en-us", pronounce=None):
        self.kokoro, self.device = loaded or self.load()
        self.speed, self.lang = speed, lang
        self.pronounce = pronounce or {}  # voice.pronounce: the user's own fix-ups, on top of PRONOUNCE
        self._blends = {}
        self.player = player
        self.queue = asyncio.Queue()
        self.pool = concurrent.futures.ThreadPoolExecutor(1)
        self.generation = 0  # bumped by stop(); in-flight results from an older generation are dropped
        self.synthesizing = False
        self.hold = asyncio.Event()  # set while the user is talking: don't start new sentences
        self.on_play = None  # one-shot callback(speaker) when the next clip is handed to the player (latency readout)
        self._worker = asyncio.create_task(self._run())

    def style(self, voice):
        """A voice name, or a blend ((name, weight), ...) → what Kokoro takes: the name, or the weighted mean of the
        voices' style vectors."""
        if isinstance(voice, str):
            return voice
        if voice not in self._blends:
            total = sum(w for _, w in voice)
            self._blends[voice] = sum(self.kokoro.get_voice_style(n) * (w / total) for n, w in voice).astype("float32")
        return self._blends[voice]

    def say(self, text, voice, speaker=""):
        text = clean_for_speech(text, self.pronounce)
        if text:
            self.queue.put_nowait((self.generation, text, voice, speaker))

    def stop(self):
        self.generation += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        self.player.stop()

    @property
    def speaking(self):
        return self.player.busy or self.synthesizing or not self.queue.empty()

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            gen, text, voice, speaker = await self.queue.get()
            while self.hold.is_set():
                await asyncio.sleep(0.05)
            if gen != self.generation:
                continue
            self.synthesizing = True
            try:
                audio, _ = await loop.run_in_executor(
                    self.pool, lambda: self.kokoro.create(text, voice=self.style(voice), speed=self.speed, lang=self.lang))
            except Exception as e:  # noqa: BLE001 - one bad sentence shouldn't kill speech
                print(f"[tts error] {e}")
                continue
            finally:
                self.synthesizing = False
            if gen == self.generation:
                # Clips are spoken one by one, so add the pause Kokoro itself puts between sentences (or clauses);
                # back to back they ran together.
                pause = SENTENCE_PAUSE_S if text.rstrip().endswith((".", "!", "?")) else CLAUSE_PAUSE_S
                self.player.play(np.concatenate([audio, np.zeros(int(pause * TTS_SR), dtype=np.float32)]))
                if self.on_play:
                    cb, self.on_play = self.on_play, None
                    cb(speaker)


SENTENCE_PAUSE_S, CLAUSE_PAUSE_S = 0.25, 0.1  # Kokoro's own pauses after a sentence and after a clause
# Words the phonemizer gets wrong in cv's usual talk (checked against Kokoro's espeak phonemes). voice.pronounce in the
# config adds to these or overrides them. Keys match whole words, case-sensitively.
PRONOUNCE = {"README": "read me", "pytest": "pie test", "CI": "C I", "TUI": "T U I", "vs": "versus",
             "vs.": "versus", "e.g.": "for example", "i.e.": "that is", "~/": "home folder ", "WSL": "W S L"}
FILE_EXT = re.compile(r"\b([\w-]+)\.(py|md|yaml|yml|json|toml|txt|sh|js|ts|rs|go|cmd|ps1|log)\b")


def clean_for_speech(text, pronounce=None):
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)  # code blocks are never read out
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"[`*_#>|]+", "", text)
    text = text.replace("—", ", ")
    text = FILE_EXT.sub(r"\1 dot \2", text)  # audio.py: "audio dot py", not one run-together word
    for word, say in {**PRONOUNCE, **(pronounce or {})}.items():  # whole words: a boundary where the word has one
        before = r"(?<![\w~])" if re.match(r"\w", word) else ""
        after = r"(?!\w)" if re.search(r"\w$", word) else ""
        text = re.sub(before + re.escape(word) + after, say.replace("\\", "\\\\"), text)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Feed streaming text, get back complete sentences (skips text inside open code fences).

    Kokoro speaks each chunk on its own, with the falling tone of a finished sentence at its end, so chunks end only
    where a sentence does (not at a colon). The one exception is the first chunk of a reply: it may end at a clause
    break (a comma, semicolon or dash after at least FIRST_MIN characters), so speech starts before the first sentence
    has finished streaming. It is never cut mid-phrase.
    """

    END = re.compile(r"(?<=[.!?])\s+|\n{2,}")
    CLAUSE = re.compile(r"[,;—]\s")
    FIRST_MIN = 30

    def __init__(self):
        self.buf = ""
        self.first = True

    def feed(self, delta):
        self.buf += delta
        if self.buf.count("```") % 2:
            return []  # inside a code block: wait for it to close
        self.buf = re.sub(r"```.*?```", " ", self.buf, flags=re.S)
        parts = self.END.split(self.buf)
        self.buf = parts.pop()
        out = [p for p in parts if p.strip()]
        if self.first and not out:
            cut = next((m.end() for m in self.CLAUSE.finditer(self.buf) if m.start() >= self.FIRST_MIN), None)
            if cut:
                out, self.buf = [self.buf[:cut]], self.buf[cut:]
        if out:
            self.first = False
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        if rest.count("```") % 2:
            rest = rest[:rest.index("```")]
        return [p for p in self.END.split(rest) if p.strip()]


ANSWER_WORDS = {"yes", "yeah", "yep", "yup", "no", "nope", "nah", "okay", "ok", "sure", "fine", "approve",
                "approved", "reject", "deny", "skip", "cancel", "stop", "wait", "hold", "go", "ahead", "apply",
                "leave", "keep", "drop", "both", "neither", "first", "second", "third", "last", "recommended",
                "correct", "right", "exactly", "absolutely", "definitely", "never", "good", "great", "perfect",
                "sounds", "commit", "proceed", "continue", "later", "minutes"}
DANGLING = {"the", "a", "an", "to", "for", "and", "or", "of", "is", "if", "with", "my", "your", "that", "this", "on",
            "in", "at", "about", "but", "so", "because", "what", "which", "how", "why", "when", "where", "can",
            "could", "would", "should", "will", "just", "like", "then", "also"}


def looks_like_answer(text):
    """A short reply to a pending question ("yes", "go ahead", "skip it", "the recommended one"), not the start of
    a sentence ("what is the", "just check")."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    return bool(words) and len(words) <= 8 and words[-1] not in DANGLING and any(w in ANSWER_WORDS for w in words)


class Listener:
    """VAD state machine over mic frames.

    Calls on_start() when speech begins and on_text(text, t_stop) with the transcript when the turn ends (t_stop is
    when you went quiet). Whisper costs ~1.75 s per call regardless of length, so it is started speculatively after
    SPECULATE_S of silence; if you keep talking that result is discarded, otherwise the end-of-turn wait (END_S)
    overlaps with transcription instead of coming before it.
    """

    # People pause mid-sentence for longer than 0.2 s, which wasted transcriptions (and queued the real one behind them).
    # The end-of-turn wait is free up to ~2.25 s: the speculative transcript started at 0.5 s isn't ready before
    # then anyway. 1.2 s still cut him off mid-thought in real use ("For idle task, …"), so 1.6 s.
    SPECULATE_S, END_S = 0.5, 1.6
    # Short answers while something waits for you: the fast model, and the turn ends early (FAST_END_S) — but only
    # once the quick transcript reads like an answer. Ending every short utterance early cut questions off in real
    # use ("what is the…", "Just check…"); those now wait END_S and get the accurate model.
    FAST_MAX_S, FAST_END_S = 2.0, 0.7
    PREROLL = 19  # frames (~600 ms) kept from before speech was detected: 320 ms clipped first words
    # Silero's confidence drops on very loud, clipped input (real speech at rms 0.7–0.9 scored 0.2–0.6, so short
    # answers like "I approve" never got 3 frames over 0.5). The VAD sees frames capped at a moderate loudness;
    # quieter frames are untouched, and Whisper still gets the original audio.
    VAD_LEVEL = 0.2
    CLIP_WARN = 0.3  # share of an utterance's frames at full scale before warning once that the mic is too hot
    LEAD_IN = np.zeros(int(MIC_SR * 0.2), dtype=np.float32)  # Whisper drops a first word that starts at the edge
    DUCK = 0.25
    DUCK_AFTER = 2  # speech-like frames (~64 ms) before the voice ducks: one frame was enough for its own echo to dip it

    # Push to talk (input.mode = push_to_talk): the key, not the VAD, decides what is recorded. Holds shorter than TAP_S
    # are taps and are ignored; a hold longer than PTT_MAX_S is ended for you.
    ALWAYS, PTT = "always_listening", "push_to_talk"
    TAP_S, PTT_MAX_S = 0.3, 120

    def __init__(self, mic, vad, tts, stt, on_start, on_text, half_duplex=False, expecting_answer=lambda: False,
                 mode="always_listening"):
        self.mic, self.vad, self.tts, self.stt = mic, vad, tts, stt
        self.on_start, self.on_text = on_start, on_text
        self.half_duplex = half_duplex
        self.expecting_answer = expecting_answer  # True while an approval or question is waiting
        self.muted = False
        self.in_speech = False
        self.level = 0.0  # loudness of the last mic frame, 0..1 (for the UI)
        self.on_clipping = None  # callback(share) the first time an utterance is mostly clipped
        self.clip_warned = False
        self.mode = mode
        self.ptt_held = False  # the key is down right now (set the moment the key event arrives)
        self._ptt_events = collections.deque()  # ("down"|"up", time) in order; the frame loop acts on them
        self._ptt_buf, self._ptt_t_down, self._ptt_started = [], 0.0, False
        self._reset = False

    # ---- push to talk: called from the UI, the control socket or a global hotkey (on the event loop) ----
    def set_mode(self, mode):
        if mode != self.mode:
            self.mode = mode
            self.ptt_held = False
            self._ptt_events.clear()
            self._reset = True  # the frame loop drops whatever was being recorded in the old mode

    def ptt_down(self):
        """The talk key went down. → False if ignored (wrong mode, muted, or already down)."""
        if self.mode != self.PTT or self.muted or self.ptt_held:
            return False
        self.ptt_held = True
        self._ptt_events.append(("down", time.monotonic()))
        return True

    def ptt_up(self):
        """The talk key came up. → False if it wasn't down."""
        if not self.ptt_held:
            return False
        self.ptt_held = False
        self._ptt_events.append(("up", time.monotonic()))
        return True

    def ptt_toggle(self):
        """For terminals that report no key releases: press to start, press again to stop."""
        return self.ptt_up() if self.ptt_held else self.ptt_down()

    async def _ptt_frame(self, frame):
        """One mic frame in push-to-talk mode: recorded while the key is held, otherwise thrown away."""
        while self._ptt_events:
            kind, t = self._ptt_events.popleft()
            if kind == "down" and not self.in_speech:
                self.in_speech, self._ptt_buf, self._ptt_t_down, self._ptt_started = True, [], t, False
                self.tts.hold.set()
                if self.tts.speaking:  # pressing the key while it talks interrupts it at once
                    self._ptt_started = True
                    await self.on_start()
            elif kind == "up" and self.in_speech:
                self._ptt_end(t)
        if self.in_speech and not self.muted:
            self._ptt_buf.append(frame)
            if not self._ptt_started and time.monotonic() - self._ptt_t_down >= self.TAP_S:
                self._ptt_started = True  # held long enough to not be a tap: now it counts as speaking to Haiku
                await self.on_start()
            if len(self._ptt_buf) * FRAME_S > self.PTT_MAX_S:
                self.ptt_held = False
                self._ptt_end(time.monotonic())

    def _ptt_end(self, t_up):
        buf, self._ptt_buf = self._ptt_buf, []
        self.in_speech = False
        self.tts.hold.clear()
        held = t_up - self._ptt_t_down
        if held < self.TAP_S or not buf:
            cvlog.log.event("ptt", f"tap of {held:.2f}s ignored")
            return
        cvlog.log.event("ptt", f"held {held:.2f}s")
        audio = np.concatenate([self.LEAD_IN, *buf])
        fast = held <= self.FAST_MAX_S and self.expecting_answer()
        asyncio.create_task(self._finish(None, audio, t_up, fast))  # t_up is the release time: latency is counted from it

    def _reset_state(self):
        self._reset = False
        self.in_speech = False
        self._ptt_buf = []
        self.tts.hold.clear()
        if self.vad:
            self.vad.reset()

    async def _finish(self, spec, audio, t_stop, fast):
        text = await spec if spec else await self.stt.transcribe(audio, fast=fast)
        if fast and not looks_like_answer(text):  # not a quick answer after all: redo it with the accurate model
            fast = False
            text = await self.stt.transcribe(audio)
        cvlog.log.audio(audio, text)
        if text:
            await self.on_text(text, t_stop, fast)

    async def run(self):
        pre = collections.deque(maxlen=self.PREROLL)
        buf, voiced, silent = [], 0, 0
        loud, loud_vad = 0, 0.0  # a run of loud frames that hasn't started speech (diagnostics)
        spec = None  # speculative transcription of buf up to the current pause
        while True:
            frame = await self.mic.frames.get()
            if self._reset:  # the mode was switched: forget what the other mode had half-recorded
                pre.clear()
                buf, voiced, silent, spec, loud, loud_vad = [], 0, 0, None, 0, 0.0
                self._reset_state()
            self.level = 0.0 if self.muted else min(1.0, float(np.sqrt(np.mean(frame ** 2))) * 3)
            if self.mode == self.PTT:
                if self.muted and self.in_speech:  # muted mid-recording: throw it away
                    self.ptt_held = False
                    self._ptt_buf = []
                    self._ptt_events.clear()
                    self.in_speech = False
                    self.tts.hold.clear()
                if not self.muted:
                    await self._ptt_frame(frame)
                continue
            if self.muted or (self.half_duplex and self.tts.speaking):
                pre.clear()
                voiced = 0
                continue
            rms = float(np.sqrt(np.mean(frame ** 2)))
            p = self.vad(frame * (self.VAD_LEVEL / rms) if rms > self.VAD_LEVEL else frame)
            if not self.in_speech:
                # Loud sound that isn't (yet) speech: log it, so a missed "I approve" shows up in the session log.
                if rms > 0.15:
                    loud += 1
                    loud_vad = max(loud_vad, p)
                elif loud:
                    if loud >= 6:
                        cvlog.log.event("mic-not-speech", f"{loud * FRAME_S:.1f}s of loud sound, vad max {loud_vad:.2f}")
                    loud, loud_vad = 0, 0.0
                pre.append(frame)
                # Barge-in: duck the voice on the first speech-like frame (instant feedback), stop it once 3 frames
                # (~96 ms) confirm, and restore the volume if it was just a blip. headphone tests
                # showed background noise at ~0.0 and real speech at 0.9+, so 0.5 is a safe threshold.
                if p <= 0.5 and voiced >= self.DUCK_AFTER and self.tts.speaking:
                    # It ducked, then the sound stopped short of a barge-in: most likely the mic heard the voice itself
                    # (speakers, no headphones). Logged, since each dip is heard as a word sounding off or cut.
                    cvlog.log.event("voice-dip", f"{voiced * FRAME_S * 1000:.0f} ms, vad {p:.2f}")
                voiced = voiced + 1 if p > 0.5 else 0
                player = self.tts.player
                player.gain = self.DUCK if voiced >= self.DUCK_AFTER and self.tts.speaking else 1.0
                if voiced >= 3:
                    loud, loud_vad = 0, 0.0
                    self.in_speech = True
                    self.tts.hold.set()
                    buf, silent, spec = list(pre), 0, None
                    await self.on_start()
            else:
                buf.append(frame)
                if p < 0.35:
                    silent += 1
                elif silent:
                    silent, spec = 0, None  # you kept talking: the speculative transcript is out of date
                speech = buf[:len(buf) - silent]
                fast = len(speech) * FRAME_S <= self.FAST_MAX_S and self.expecting_answer()
                if spec is None and silent * FRAME_S >= self.SPECULATE_S and len(speech) > 6:
                    spec = asyncio.create_task(self.stt.transcribe(np.concatenate([self.LEAD_IN, *speech]),
                                                                   fast=fast))
                quiet = silent * FRAME_S
                answered = (fast and quiet > self.FAST_END_S and spec is not None and spec.done()
                            and not spec.cancelled() and spec.exception() is None
                            and looks_like_answer(spec.result()))
                if answered or quiet > self.END_S or len(buf) * FRAME_S > 30:
                    self.in_speech = False
                    self.tts.hold.clear()
                    voiced = 0
                    pre.clear()
                    self.vad.reset()
                    if len(speech) > 6:  # ignore blips shorter than ~200 ms of speech
                        clipped = sum(float(np.abs(f).max()) >= 0.99 for f in speech) / len(speech)
                        if clipped > self.CLIP_WARN and not self.clip_warned and self.on_clipping:
                            self.clip_warned = True
                            self.on_clipping(clipped)
                        t_stop = asyncio.get_running_loop().time() - silent * FRAME_S
                        asyncio.create_task(self._finish(spec, np.concatenate([self.LEAD_IN, *speech]), t_stop,
                                                         fast))
                    spec = None
