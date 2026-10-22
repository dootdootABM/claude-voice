"""Headless test of the orb UI with a stand-in engine (no models, no mic, no Claude).

Run: pytest test_tui.py   — checks keys and states; set CV_SHOTS=<dir> to save an SVG per state.
"""

import asyncio
import collections
import os
import tempfile
import types

os.environ["CLAUDE_VOICE_STATE_DIR"] = tempfile.mkdtemp()  # the real logs and saved tasks stay untouched; before tui is imported

import tui  # noqa: E402


class FakeEngine:
    def __init__(self):
        self.ready, self.state, self.muted = False, "waking", False
        self.mic_level = self.out_level = 0.0
        self.exited = asyncio.Event()
        self.typed_lines, self.keys, self.stopped = [], [], 0
        self.decisions = collections.deque()
        self.tasks = {}
        self.app = types.SimpleNamespace(pending=None, decisions=self.decisions, tasks=self.tasks)
        self.shut = False
        self.input_mode, self.ptt_calls = "always_listening", []

    def set_input_mode(self, mode):
        self.input_mode = mode

    def ptt_toggle(self):
        self.ptt_calls.append("toggle")

    def ptt_down(self):
        self.ptt_calls.append("down")

    def ptt_up(self):
        self.ptt_calls.append("up")

    async def start(self):
        await asyncio.sleep(0.3)
        self.ready, self.state = True, "listening"

    def set_muted(self, m):
        self.muted = m  # the state (and so the orb) stays the same; only the status line changes

    def stop_talking(self):
        self.stopped += 1

    async def typed(self, line):
        self.typed_lines.append(line)

    async def decision_key(self, d, line):
        self.keys.append(line)
        return True

    @property
    def active_tasks(self):
        return [t for t in self.tasks.values() if t.active]

    async def shutdown(self):
        self.shut = True


async def main(shots):
    opts = tui.make_parser().parse_args([])
    app = tui.VoiceApp(opts)
    fake = app.engine = FakeEngine()
    async with app.run_test(size=(64, 24)) as pilot:
        async def shot(name):
            await pilot.pause(0.25)
            if shots:
                app.save_screenshot(f"{shots}/{name}.svg")
            return str(app.query_one("#status").render())

        print("waking:", ascii(await shot("1-waking")))
        await pilot.pause(0.4)
        print("listening:", ascii(await shot("2-listening")))

        fake.state, fake.mic_level = "talking", 0.9
        print("talking:", ascii(await shot("3-talking")))
        fake.state, fake.out_level = "speaking", 0.7
        print("speaking:", ascii(await shot("4-speaking")))
        fake.state = "thinking"
        print("thinking:", ascii(await shot("5-thinking")))

        d = types.SimpleNamespace(kind="tool", title="t1-step2 · run cargo test", body="$ cargo test", task=None)
        fake.app.pending = d
        fake.decisions.append(d)
        fake.state = "waiting"
        print("waiting:", ascii(await shot("6-waiting")))
        await pilot.press("a")
        assert fake.keys == ["a"], fake.keys
        fake.app.pending = None
        fake.decisions.clear()

        task = types.SimpleNamespace(id="t1", title="Add shuffle key", active=True,
                                     digest=collections.deque(["running the tests"]))
        fake.tasks["t1"] = task
        fake.state = "working"
        print("working:", ascii(await shot("7-working")))

        fake.state = "listening"
        fake.tasks.clear()
        await pilot.press("space")
        assert fake.muted
        assert app.query_one("#orb")._state() == "listening"  # orb keeps animating as unmuted
        print("muted:", ascii(await shot("8-muted")))
        await pilot.press("space")
        assert not fake.muted
        fake.tasks["t1"] = task  # work running again, for the quit check below

        await pilot.press("t")  # transcript on
        tui.TuiUi(app).you("what's the plan for the shuffle key?")
        tui.TuiUi(app).agent("haiku", "I'll add a keybinding in the config, then run the tests.")
        tui.TuiUi(app).agent("work", "Reading the keybinding code.")
        assert app.query_one("#log").has_class("shown")
        print("transcript on:", ascii(await shot("9-transcript")))
        await pilot.press("t")
        assert not app.query_one("#log").has_class("shown")

        await pilot.press("i")
        await pilot.press(*"hello there")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert fake.typed_lines == ["hello there"], fake.typed_lines

        await pilot.press("escape")
        assert fake.stopped == 1

        tui.TuiUi(app).error("microphone not found")
        print("error toast:", ascii(await shot("10-error")))

        await pilot.press("q")  # work running: first q only asks
        status = await shot("11-quit-confirm")
        print("q once:", ascii(status))
        assert status == "work is still running", status
        assert not fake.shut
        await pilot.press("q")
        await pilot.pause(0.2)
        assert fake.shut
    print("all checks passed")


def test_orb_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "STATE_FILE", str(tmp_path / "ui.json"))  # don't touch the real saved preferences
    asyncio.run(main(os.environ.get("CV_SHOTS")))
