"""The voice agent without a UI: speech in and out, Haiku, tasks and decisions. A UI passes itself in as `ui`
(methods agent, you, dim, status, error, card, result) and reads `state`, `mic_level` and `out_level` to draw."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import cvlog
import config
import ptt
from agents import RESTART_FILE, App, Backend, Task, voice
import platform_util
from audio import Listener, Mic, Player, Stt, Tts, Vad, pick_device


def make_parser():
    """The command line. The defaults come from the config file (which sits on top of the built-in defaults), so a flag
    given here always wins. Raises config.ConfigError if the config file is bad."""
    cfg = config.get()
    ap = argparse.ArgumentParser(prog="cv", description="Hands-free voice assistant for Claude Code. Settings live in "
                                 f"the config file ({platform_util.config_path()}); flags override it.")
    ap.add_argument("--plain", action="store_true", help="plain scrolling text UI instead of the orb")
    ap.add_argument("--half-duplex", action="store_true", default=cfg["behaviour"]["half_duplex"],
                    help="ignore the mic while it's speaking (without headphones, if it interrupts itself)")
    ap.add_argument("--no-mic", action="store_true", help="type only")
    ap.add_argument("--list-voices", action="store_true", help="print the available voices and exit")
    ap.add_argument("--verbosity", default=cfg["behaviour"]["verbosity"], choices=["quiet", "normal", "chatty"])
    ap.add_argument("--device", default=cfg["stt"]["device"], choices=["auto", "cuda", "cpu"],
                    help="where to run Whisper: cuda if a GPU is found (auto), or force one")
    ap.add_argument("--stt-model", default=cfg["stt"]["model"],
                    help="Whisper model (default distil-large-v3 on cuda, small.en on cpu; small.en is faster, less accurate)")
    ap.add_argument("--front-model", default=cfg["models"]["front"], help="model for the voice agent (haiku, sonnet, ...)")
    ap.add_argument("--fast-stt", action=argparse.BooleanOptionalAction, default=cfg["stt"]["fast_model"],
                    help="use small.en for short answers while an approval or question is waiting (--no-fast-stt: don't)")
    return ap


def parse_args(argv=None):
    """Parse the command line (and the config file under it). A bad config file prints a readable message and exits
    with status 2; `--list-voices` prints the voices and exits 0."""
    try:
        opts = make_parser().parse_args(argv)
    except config.ConfigError as e:
        print(f"claude-voice: config error in {e}", file=sys.stderr)
        sys.exit(2)
    if opts.list_voices:
        voices = config.available_voices()
        if voices is None:
            print(f"claude-voice: voices file not found: {config.VOICES_FILE} (see the README for the model files)",
                  file=sys.stderr)
            sys.exit(1)
        cfg = config.get()
        spec = cfg.voice_spec()
        current = {spec} if isinstance(spec, str) else {n for n, _ in spec}
        print(config.format_voices(voices, current))
        print("\n* = the voice in use; set voice.name (or voice.blend) in " + platform_util.config_path())
        sys.exit(0)
    return opts


def edit_in_editor(text):
    """Open the prompt in a new kitty window (so the app keeps running) or fall back to the editor (vim, notepad on Windows) in this terminal."""
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    platform_util.spawn_terminal([platform_util.default_editor(), path], "claude-voice-edit", wait=True)
    with open(path, encoding="utf-8") as f:
        new = f.read()
    os.unlink(path)
    return new


class StartupError(RuntimeError):
    """The app can't start, with a message meant for the user (no traceback)."""


def explain_startup_error(e, device="cuda"):
    msg = str(e)
    if "out of memory" in msg.lower():
        if device == "cuda":
            return ("Not enough GPU memory to load the speech models. Is another copy of cv, or a game, using the GPU? "
                    "`nvidia-smi` shows what's running.")
        return "Not enough memory to load the speech models. Close other programs, or try `--stt-model tiny.en`."
    if "claude" in msg.lower() and isinstance(e, OSError):
        return "Couldn't start Claude Code (`claude`). Is it installed and on your PATH?"
    if "PortAudio" in msg or "device" in msg.lower():
        return f"Couldn't open the microphone or speakers: {msg}"
    return f"Couldn't start: {msg}"


class Engine:
    def __init__(self, ui, opts):
        cvlog.start()
        cvlog.log.event("launch", f"pid={os.getpid()} opts={vars(opts)}")
        self.ui, self.opts = cvlog.LoggedUi(ui), opts
        self.ready = False
        self.exited = asyncio.Event()  # set when the user says goodbye
        self.app = self.tts = self.stt = self.listener = self.player = None
        self.input_mode = config.get()["input"]["mode"]  # always_listening | push_to_talk (a UI may override it before start)
        self.ptt_server = self.global_hotkey = None

    async def start(self):
        loop = asyncio.get_running_loop()
        t_start = time.monotonic()

        def on_exception(loop, ctx):
            exc = ctx.get("exception")
            cvlog.log.event("EXCEPTION", f"{ctx.get('message')} :: "
                            + ("".join(traceback.format_exception(exc)) if exc else repr(ctx)))
            loop.default_exception_handler(ctx)

        loop.set_exception_handler(on_exception)
        try:
            with open(os.path.expanduser("~/.voice-code/config.json"), encoding="utf-8") as f:
                if json.load(f).get("enabled"):
                    self.ui.error("VoiceCode is on, so replies may also be read out by its hook: run `voice-code off`.")
        except (OSError, ValueError):
            pass
        # Model loading and the Haiku process start are independent, so they run together and startup takes as long as
        # the slowest one. Everything happens in threads (or the loop) so a UI can keep animating meanwhile.
        self.app = app = App(self.ui, None, self.opts.front_model)  # app.tts is set once Kokoro is loaded below
        app.verbosity = self.opts.verbosity
        app.on_exit = self.exited.set
        saved = app.load_saved_tasks()
        device = pick_device(self.opts.device)
        # On CPU the main model defaults to small.en (distil-large-v3 is too slow there) and the fast model is off;
        # an explicit --stt-model always wins.
        stt_model = self.opts.stt_model or ("distil-large-v3" if device == "cuda" else "small.en")
        fast_model = None if not self.opts.fast_stt or device != "cuda" else "small.en"
        cvlog.log.event("device", f"stt on {device} (--device {self.opts.device}) model={stt_model} fast={fast_model}")
        cfg = config.get()
        cvlog.log.event("config", f"{cfg.path or 'built-in defaults'} warnings={cfg.warnings}")
        jobs = [asyncio.to_thread(Tts.load, device, cfg.warm_voice(), cfg["voice"]["lang"]), app.front.start()]
        if not self.opts.no_mic:
            jobs.append(asyncio.to_thread(Stt, stt_model, fast_model, device))
        try:
            loaded, _, *stt = await asyncio.gather(*jobs)
        except Exception as e:
            raise StartupError(explain_startup_error(e, device)) from e
        if stt:
            self.stt = stt[0]
        self.player = Player()
        self.tts = tts = Tts(self.player, loaded, cfg["voice"]["speed"], cfg["voice"]["lang"])
        app.tts = tts

        if not self.opts.no_mic:
            async def on_start():
                talking = tts.speaking
                tts.stop()  # barge-in: you started talking, so everything queued (Haiku and narration) stops at once
                tts.on_play = None  # the interrupted reply's timing readout would measure the wrong thing
                gap = ("while I was talking" if talking
                       else f"{time.monotonic() - self.player.idle_since:.1f}s after I stopped")
                self.ui.dim(f"listening… (you started {gap})")
                # Don't wait for Haiku to confirm the interrupt: that would hold up voice detection for your words.
                asyncio.create_task(app.front.interrupt())

            async def on_text(text, t_stop, fast):
                heard = loop.time() - t_stop
                model = self.stt.names["fast" if fast and "fast" in self.stt.models else "main"]

                def first_audio(speaker):
                    self.ui.dim(f"⏱ heard you {heard:.2f}s after you stopped ({model}) · first audio after "
                                f"{loop.time() - t_stop:.2f}s")

                tts.on_play = first_audio
                await self.user_said(text)

            mic = Mic(loop)
            self.listener = Listener(mic, Vad(), tts, self.stt, on_start, on_text,
                                     half_duplex=self.opts.half_duplex, expecting_answer=lambda: bool(app.decisions),
                                     mode=self.input_mode)
            app.user_speaking = lambda: self.listener.in_speech  # Haiku waits before announcing while you talk

            def on_clipping(share):
                cvlog.log.event("mic-clipping", f"{share:.0%} of the utterance at full scale")
                self.ui.error(f"Your mic is clipping ({share:.0%} of that at full volume): turn its input volume down a "
                              "little, e.g. 35–40% in Quickshell. Too loud makes short answers easy to miss.")

            self.listener.on_clipping = on_clipping
            mic.start()
            asyncio.create_task(self.listener.run())
            self._start_ptt_inputs(loop, cfg)
        self.ready = True
        cvlog.log.event("ready", f"{time.monotonic() - t_start:.1f}s tts={tts.device} "
                        f"stt={list(self.stt.models) if self.stt else None}")
        asyncio.create_task(self._log_state())
        if not await self._resume_after_restart():
            if saved:
                lines = "; ".join(f"{t.id} in {t.project_dir} ({t.title}"
                                  + (", interrupted mid-work" if t.was in ("planning", "building") else "")
                                  + (f"; last: {t.last_result[:160]}" if t.last_result else "") + ")" for t in saved)
                app.front.notify(f"[Open from your last session: {lines}.] Greet the user in a few words and offer "
                                 "to pick that up (in one sentence). Resuming keeps everything you knew.")
            else:
                tts.say("I'm listening.", voice())

    async def _resume_after_restart(self):
        """If a task restarted the app, resume its session with what happened while the app was down."""
        try:
            with open(RESTART_FILE, encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError):
            return False
        os.unlink(RESTART_FILE)
        app = self.app
        task = app.tasks.get(req["task_id"])
        if task is None:
            if not req.get("session_id"):
                return False
            task = app.tasks[req["task_id"]] = Task.from_dict(
                {"id": req["task_id"], "title": req.get("reason", "restart"), "project_dir": req["project_dir"],
                 "model": "sonnet", "session_id": req["session_id"]})
        msg = f"The Claude Voice app has restarted, as you asked ({req.get('reason', '')})."
        if req.get("command"):
            if "exit_code" in req:
                tail = req.get("output", "")
                msg += (f" While it was down, `{req['command']}` ran in {req.get('duration_s', 0):.0f}s"
                        + (" and was stopped at the time limit" if req.get("timed_out") else "")
                        + f", exit code {req['exit_code']}. Output (last part):\n```\n{tail}\n```")
            else:
                msg += f" `{req['command']}` did NOT run (the app wasn't started through scripts/cv)."
        msg += " Continue the task from where you left off."
        task.backend = Backend(app, task, resume=True)
        task.state = "building"
        await task.backend.start(msg)
        app.ui.status(f"{task.id}: resumed after restart")
        app.front.notify(f"[You're back after restarting yourself for task {task.id} ({req.get('reason', '')}), and "
                         "you're carrying on with it.] Tell the user in a few words that you're back and continuing.")
        return True

    async def _log_state(self):
        """Log every change of what the UI shows, with the details behind it."""
        last = None
        while True:
            app = self.app
            snap = (self.state, app.front.generating, len(app.decisions), self.muted,
                    tuple(f"{t.id}:{t.state}" for t in app.tasks.values()))
            if snap != last:
                last = snap
                cvlog.log.event("state", f"{snap[0]} haiku_busy={snap[1]} decisions={snap[2]} muted={snap[3]} "
                                f"tasks={','.join(snap[4]) or '-'}")
            await asyncio.sleep(0.25)

    # ---- input -----------------------------------------------------------------------------------------
    async def user_said(self, text):
        self.ui.you(text)
        await self.app.front.say(text)

    async def typed(self, line):
        """A typed line: a decision key if one applies, otherwise a message to Haiku."""
        d = self.app.pending
        if d and await self.decision_key(d, line):
            return
        await self.user_said(line)

    async def decision_key(self, d, line):
        # Only the single-letter keys count; anything else typed (like "yes, go ahead") goes to Haiku as a message.
        if d.kind == "question":
            return False  # typed answers go to Haiku like spoken ones
        cmd, _, rest = line.strip().partition(" ")
        cmd = cmd.lower()
        if cmd == "a" and not rest:
            self.app.resolve_by_keyboard(d, True)
        elif cmd == "r":
            self.app.resolve_by_keyboard(d, False, rest.strip())
        elif cmd == "e" and d.kind == "task":
            d.task.prompt = (await asyncio.to_thread(edit_in_editor, d.task.prompt)).strip()
            self.app.front_notes.append(f"[the user edited the prompt of {d.task.id} on the keyboard]")
            self.app.resolve_by_keyboard(d, False, "__edited__")
        elif cmd == "m" and d.kind == "task" and rest.strip() in ("sonnet", "opus", "haiku"):
            d.task.model = rest.strip()
            self.app.front_notes.append(f"[the user set {d.task.id}'s back-end model to {d.task.model}]")
            self.app.resolve_by_keyboard(d, False, "__edited__")
        else:
            return False
        return True

    # ---- push to talk ----------------------------------------------------------------------------------
    def _start_ptt_inputs(self, loop, cfg):
        """The control socket (`cv ptt ...`) and, if configured, the global hotkey. Neither is fatal when it fails."""
        try:
            self.ptt_server = ptt.PttServer(self.ptt_command, loop)
            self.ptt_server.start()
        except OSError as e:
            cvlog.log.event("ptt-socket", f"not started: {e}")
            self.ptt_server = None
        spec = cfg["input"]["global_ptt_key"]
        if spec:
            try:
                self.global_hotkey = ptt.GlobalHotkey.start(spec, loop, self.ptt_down, self.ptt_up)
                cvlog.log.event("ptt-hotkey", f"{spec} via {self.global_hotkey.backend.NAME}")
            except ptt.HotkeyUnavailable as e:
                cvlog.log.event("ptt-hotkey", f"unavailable: {e}")
                self.ui.error(f"Global push-to-talk key {spec!r} is off: {e}")

    def set_input_mode(self, mode):
        self.input_mode = mode
        if self.listener:
            self.listener.set_mode(mode)
        cvlog.log.event("input-mode", mode)

    def ptt_down(self):
        return bool(self.listener and self.listener.ptt_down())

    def ptt_up(self):
        return bool(self.listener and self.listener.ptt_up())

    def ptt_toggle(self):
        return bool(self.listener and self.listener.ptt_toggle())

    @property
    def ptt_held(self):
        return bool(self.listener and self.listener.ptt_held)

    def ptt_command(self, cmd):
        """`cv ptt down|up|toggle|status` → a one-line reply for the person who ran it."""
        if not self.listener:
            return "error: Claude Voice has no microphone open (still starting, or --no-mic)"
        if cmd == "status":
            return f"mode={self.input_mode} held={'yes' if self.ptt_held else 'no'} muted={'yes' if self.muted else 'no'}"
        if self.input_mode != ptt.PTT:
            return "ignored: not in push-to-talk mode (press p in the cv window, or set input.mode: push_to_talk)"
        if self.muted:
            return "ignored: the microphone is muted"
        changed = {"down": self.ptt_down, "up": self.ptt_up, "toggle": self.ptt_toggle}[cmd]()
        return ("ok" if changed else "ok (nothing to do)") + f": {'listening' if self.ptt_held else 'idle'}"

    def set_muted(self, muted):
        if self.listener:
            self.listener.muted = muted

    @property
    def muted(self):
        return bool(self.listener and self.listener.muted)

    def stop_talking(self):
        if self.tts:
            self.tts.stop()

    # ---- what a UI draws ------------------------------------------------------------------------------
    @property
    def state(self):
        """waking | talking | speaking | thinking | waiting | working | listening. Muting doesn't change it: the orb
        keeps animating as usual and only the status line says muted (see `muted`)."""
        if not self.ready:
            return "waking"
        if self.listener and self.listener.in_speech:
            return "talking"
        if self.tts.speaking:
            return "speaking"
        if self.app.decisions:  # above thinking, so a pending decision (and its keys) always shows
            return "waiting"
        if self.app.front.generating:
            return "thinking"
        if self.active_tasks:
            return "working"
        return "listening"

    @property
    def active_tasks(self):
        return [t for t in self.app.tasks.values() if t.active] if self.app else []

    @property
    def mic_level(self):
        return self.listener.level if self.listener else 0.0

    @property
    def out_level(self):
        return self.player.level if self.tts else 0.0

    @property
    def restart_requested(self):
        return bool(self.app and self.app.restart_requested)

    async def shutdown(self):
        cvlog.log.event("shutdown", f"restart={self.restart_requested}")
        if self.ptt_server:
            self.ptt_server.stop()
        if self.global_hotkey:
            self.global_hotkey.stop()
        if self.tts:
            self.tts.stop()
        if self.app:
            self.app.save_tasks()  # before stopping anything, so interrupted work can be resumed next time
            for t in self.app.tasks.values():
                if t.backend and t.open:
                    await t.backend.stop()
            self.app.front.closing = True
            await self.app.front.proc.close()
