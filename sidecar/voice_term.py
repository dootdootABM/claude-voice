"""Plain text UI for the voice agent (`cv --plain`): a scrolling transcript of everything, typed commands.

The default UI is the orb (tui.py); this is the plain text UI that `cv --plain` uses.
"""

import asyncio
import shutil
import sys
import threading

import platform_util
from engine import Engine, StartupError, make_parser, parse_args

C = {"haiku": "\033[38;5;213m", "work": "\033[38;5;80m", "you": "\033[38;5;228m", "dim": "\033[2m",
     "err": "\033[31m", "card": "\033[38;5;215m", "ok": "\033[32m", "bold": "\033[1m", "off": "\033[0m"}

HELP = """\
Talk normally — it listens hands-free. Or type a message and press Enter.
  Enter on an empty line   stop it talking            /mute, /unmute   mic on/off
  /quiet /normal /chatty   how much progress it narrates while working
  /mode                    switch always-listening / push to talk       /ptt   (push to talk) start / stop talking
  /tasks                   list tasks                 /kill <task>     stop or close a task
  /quit
Decisions (task drafts, plans, steps): just answer Haiku out loud. The keyboard works too when a card is shown:
  a = approve   r [note] = reject / send back   e = edit the task prompt   m <sonnet|opus|haiku> = back-end model"""

CARD_KEYS = {"task": "answer out loud, or: [a]pprove & plan   [e]dit   [m]odel <name>   [r]eject [note]",
             "plan": "answer out loud, or: [a]pprove plan   [r] <what to change>",
             "tool": "answer out loud, or: [a]pprove   [r]eject [note]",
             "question": "answer out loud (or type your answer)"}


class Ui:
    def say(self, tag, text, color):
        print(f"{C[color]}{tag}{C['off']} {text}", flush=True)

    def agent(self, who, text):
        self.say("Haiku   ▸" if who == "haiku" else "Haiku   ·", text, who)

    def you(self, text):
        self.say("You     ▸", text, "you")

    def dim(self, text):
        print(f"{C['dim']}{text}{C['off']}", flush=True)

    def status(self, text):
        print(f"{C['dim']}· {text}{C['off']}", flush=True)

    def error(self, text):
        print(f"{C['err']}! {text}{C['off']}", flush=True)

    def card(self, d):
        bar = "─" * min(shutil.get_terminal_size().columns, 100)
        print(f"\n{C['card']}{bar}\n{C['bold']}▌ {d.title}{C['off']}{C['card']}\n{bar}{C['off']}")
        print(d.body.strip())
        print(f"{C['card']}{bar}\n{CARD_KEYS[d.kind]}{C['off']}\n", flush=True)

    def result(self, task, text, res):
        u = res.get("usage", {})
        ok = res.get("subtype") == "success"
        mark = "✔ round finished" if ok else "✘ stopped with a problem"
        print(f"\n{C['ok' if ok else 'err']}{mark}: {task.id}{C['off']} {C['dim']}(open for follow-ups · "
              f"{res.get('num_turns')} turns, "
              f"out {u.get('output_tokens')} tokens){C['off']}\n{text}\n", flush=True)


async def main(opts):
    ui = Ui()
    engine = Engine(ui, opts)
    ui.status("loading Kokoro and Whisper…")
    try:
        await engine.start()
    except StartupError as e:
        ui.error(str(e))
        await engine.shutdown()
        return False
    app = engine.app
    print(f"\n{C['bold']}Claude voice — terminal{C['off']}\n{HELP}\n", flush=True)

    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def read_stdin():
        for l in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, l)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    asyncio.create_task(engine.exited.wait()).add_done_callback(lambda _: lines.put_nowait(None))  # voice "bye"

    while (line := await lines.get()) is not None:
        line = line.rstrip("\n")
        if not line.strip():
            engine.stop_talking()
            continue
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            if cmd == "quit":
                break
            elif cmd in ("mute", "unmute") and engine.listener:
                engine.set_muted(cmd == "mute")
                ui.status(f"mic {'muted' if engine.muted else 'on'}")
            elif cmd == "mode" and engine.listener:
                engine.set_input_mode("always_listening" if engine.input_mode == "push_to_talk" else "push_to_talk")
                ui.status(f"input: {engine.input_mode.replace('_', ' ')}")
            elif cmd == "ptt" and engine.listener:
                ui.status(engine.ptt_command("toggle"))
            elif cmd in ("quiet", "normal", "chatty"):
                app.verbosity = cmd
                ui.status(f"narration: {cmd}")
            elif cmd == "tasks":
                for t in app.tasks.values():
                    ui.status(f"{t.id} [{t.state}] {t.title} ({t.model}, {t.project_dir})")
                if not app.tasks:
                    ui.status("no tasks yet")
            elif cmd == "kill" and arg.strip() in app.tasks and app.tasks[arg.strip()].backend:
                await app.tasks[arg.strip()].backend.stop()
                ui.status(f"{arg.strip()} stopped")
            elif cmd == "help":
                print(HELP)
            else:
                ui.error(f"unknown command /{cmd} (try /help)")
            continue
        await engine.typed(line)

    await engine.shutdown()
    return engine.restart_requested


if __name__ == "__main__":
    platform_util.utf8_console()
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
