"""Push to talk: mode switch, tap rejection, the toggle fallback, key releases, the control socket and the global hotkey
combo logic. No real keyboard, microphone, terminal or model is used."""

import asyncio
import json
import types

import numpy as np
import pytest

import audio
import platform_util
import ptt
import ptt_keys
import supervisor
from audio import FRAME, Listener

FRAME_S = audio.FRAME_S


# ---- a Listener with fake parts ---------------------------------------------------------------------------------
class FakeTts:
    def __init__(self):
        self.hold = asyncio.Event()
        self.speaking = False
        self.stops = 0
        self.player = types.SimpleNamespace(gain=1.0)

    def stop(self):
        self.stops += 1
        self.speaking = False


class FakeStt:
    def __init__(self):
        self.calls = []

    async def transcribe(self, samples, fast=False):
        self.calls.append((len(samples), fast))
        return "hello there"


class Rig:
    """A Listener fed frames by hand. frames(n, level) pushes n mic frames and lets the loop process them."""

    def __init__(self, mode=Listener.PTT):
        self.mic = types.SimpleNamespace(frames=asyncio.Queue())
        self.tts, self.stt = FakeTts(), FakeStt()
        self.started, self.texts = 0, []

        async def on_start():
            self.started += 1

        async def on_text(text, t_stop, fast):
            self.texts.append((text, t_stop, fast))

        self.vad_calls = 0

        def vad(frame):
            self.vad_calls += 1
            return 0.0

        vad.reset = lambda: None
        self.listener = Listener(self.mic, vad, self.tts, self.stt, on_start, on_text, mode=mode)
        self.task = asyncio.create_task(self.listener.run())

    async def frames(self, n, level=0.1):
        for _ in range(n):
            self.mic.frames.put_nowait(np.full(FRAME, level, dtype=np.float32))
        for _ in range(20):
            await asyncio.sleep(0)
        while not self.mic.frames.empty():
            assert not self.task.done(), self.task.exception()  # the listener loop must not have died
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0.01)

    async def close(self):
        self.task.cancel()


def run(coro):
    return asyncio.run(coro)


def test_mic_audio_is_ignored_while_the_key_is_not_held():
    async def go():
        r = Rig()
        await r.frames(40, level=0.5)  # plenty of loud "speech"
        await r.settle()
        assert r.vad_calls == 0 and not r.listener.in_speech and r.stt.calls == [] and r.started == 0
        await r.close()
    run(go())


def test_hold_records_exactly_the_held_frames_and_transcribes_at_release(monkeypatch):
    async def go():
        r = Rig()
        r.listener.TAP_S = 0.0
        assert r.listener.ptt_down()
        await r.frames(30)
        assert r.listener.in_speech and r.tts.hold.is_set()
        assert r.listener.ptt_up()
        await r.frames(5)  # these come after the release and must not be recorded
        await r.settle()
        assert not r.listener.in_speech and not r.tts.hold.is_set()
        assert len(r.stt.calls) == 1
        samples, fast = r.stt.calls[0]
        held_frames = (samples - len(Listener.LEAD_IN)) / FRAME
        assert 27 <= held_frames <= 31, held_frames  # the frames between press and release, nothing after
        assert r.texts and r.texts[0][0] == "hello there"
        await r.close()
    run(go())


def test_release_goes_straight_to_the_transcript_without_a_silence_wait():
    async def go():
        r = Rig()
        r.listener.TAP_S = 0.0
        r.listener.ptt_down()
        await r.frames(20)
        t_before = asyncio.get_running_loop().time()
        r.listener.ptt_up()
        await r.frames(1)
        await r.settle()
        assert r.texts, "transcribed without waiting for END_S of silence"
        assert asyncio.get_running_loop().time() - t_before < Listener.END_S / 2
        t_stop = r.texts[0][1]  # the latency readout is counted from the release
        assert t_before - 0.001 <= t_stop <= asyncio.get_running_loop().time()
        await r.close()
    run(go())


def test_taps_shorter_than_a_third_of_a_second_are_ignored():
    async def go():
        r = Rig()
        r.listener.ptt_down()
        await r.frames(3)
        r.listener.ptt_up()
        await r.frames(2)
        await r.settle()
        assert r.stt.calls == [] and r.texts == []
        assert r.started == 0  # a tap doesn't interrupt Haiku either
        assert not r.listener.in_speech and not r.tts.hold.is_set()
        await r.close()
    run(go())


def test_a_real_hold_counts_and_tells_the_app_you_are_speaking():
    async def go():
        r = Rig()
        r.listener.ptt_down()
        for _ in range(12):  # ~0.4 s of real time
            await r.frames(1)
            await asyncio.sleep(0.03)
        assert r.started == 1
        r.listener.ptt_up()
        await r.frames(1)
        await r.settle()
        assert len(r.stt.calls) == 1
        await r.close()
    run(go())


def test_pressing_the_key_while_it_speaks_stops_the_speech_at_once():
    async def go():
        r = Rig()
        r.tts.speaking = True
        r.listener.ptt_down()
        await r.frames(1)
        assert r.started == 1  # the app's on_start stops the speech (barge-in)
        r.listener.ptt_up()
        await r.frames(1)
        await r.close()
    run(go())


def test_space_mute_still_wins_in_push_to_talk():
    async def go():
        r = Rig()
        r.listener.muted = True
        assert not r.listener.ptt_down()
        r.listener.muted = False
        r.listener.TAP_S = 0.0
        r.listener.ptt_down()
        await r.frames(10)
        r.listener.muted = True  # muted mid-recording: dropped
        await r.frames(2)
        r.listener.muted = False
        await r.frames(2)
        await r.settle()
        assert r.stt.calls == [] and not r.listener.in_speech and not r.listener.ptt_held
        await r.close()
    run(go())


def test_toggle_is_press_to_start_press_again_to_stop():
    async def go():
        r = Rig()
        r.listener.TAP_S = 0.0
        assert r.listener.ptt_toggle() and r.listener.ptt_held
        await r.frames(10)
        assert r.listener.in_speech
        assert r.listener.ptt_toggle() and not r.listener.ptt_held
        await r.frames(1)
        await r.settle()
        assert len(r.stt.calls) == 1
        await r.close()
    run(go())


def test_down_up_ignored_in_the_wrong_state():
    async def go():
        r = Rig()
        assert not r.listener.ptt_up()  # not down
        assert r.listener.ptt_down() and not r.listener.ptt_down()  # already down
        r2 = Rig(mode=Listener.ALWAYS)
        assert not r2.listener.ptt_down() and not r2.listener.ptt_toggle()
        await r.close()
        await r2.close()
    run(go())


def test_switching_modes_drops_a_half_made_recording_and_changes_behaviour():
    async def go():
        r = Rig(mode=Listener.ALWAYS)
        await r.frames(5)
        assert r.vad_calls == 5  # always-listening: every frame goes through the voice detector
        r.listener.set_mode(Listener.PTT)
        r.listener.ptt_down()
        await r.frames(10)
        assert r.listener.in_speech
        r.vad_calls = 0
        r.listener.set_mode(Listener.ALWAYS)  # back, mid-hold
        await r.frames(3)
        assert not r.listener.in_speech and not r.listener.ptt_held and not r.tts.hold.is_set()
        assert r.vad_calls == 3
        assert r.stt.calls == []
        await r.close()
    run(go())


# ---- engine glue: the control-socket commands -------------------------------------------------------------------
class FakeListener:
    def __init__(self):
        self.mode, self.ptt_held, self.muted, self.calls = Listener.PTT, False, False, []

    def ptt_down(self):
        self.calls.append("down")
        changed, self.ptt_held = not self.ptt_held, True
        return changed

    def ptt_up(self):
        self.calls.append("up")
        changed, self.ptt_held = self.ptt_held, False
        return changed

    def ptt_toggle(self):
        return self.ptt_up() if self.ptt_held else self.ptt_down()

    def set_mode(self, mode):
        self.mode = mode


def make_engine():
    import engine
    e = engine.Engine.__new__(engine.Engine)
    e.listener, e.input_mode, e.ptt_server, e.global_hotkey = FakeListener(), ptt.PTT, None, None
    return e


def test_engine_commands():
    e = make_engine()
    assert e.ptt_command("down").startswith("ok") and e.listener.ptt_held
    assert e.ptt_command("down").startswith("ok (nothing to do)")
    assert e.ptt_command("status") == "mode=push_to_talk held=yes muted=no"
    assert e.ptt_command("up").endswith("idle") and not e.listener.ptt_held
    assert e.ptt_command("toggle").endswith("listening") and e.listener.ptt_held
    e.set_input_mode(ptt.ALWAYS)
    assert e.listener.mode == ptt.ALWAYS
    assert e.ptt_command("down").startswith("ignored: not in push-to-talk mode")
    e.listener = None
    assert e.ptt_command("down").startswith("error")


# ---- the socket ---------------------------------------------------------------------------------------------------
def test_socket_roundtrip_without_a_loop():
    seen = []
    server = ptt.PttServer(lambda cmd: seen.append(cmd) or f"got {cmd}")
    server.start()
    try:
        assert ptt.send_command("down") == "got down"
        assert ptt.send_command("TOGGLE") == "got toggle"
        assert "unknown command" in ptt.send_command("fly")
        assert seen == ["down", "toggle"]
    finally:
        server.stop()
    with pytest.raises(ConnectionError):
        ptt.send_command("down")  # nothing listens any more


def test_socket_handler_runs_on_the_event_loop():
    import threading

    async def go():
        loop = asyncio.get_running_loop()
        where = []
        server = ptt.PttServer(lambda cmd: where.append(threading.current_thread()) or "ok", loop)
        server.start()
        try:
            reply = await asyncio.to_thread(ptt.send_command, "up")
            assert reply == "ok" and where == [threading.current_thread()]
        finally:
            await asyncio.to_thread(server.stop)
    run(go())


def test_socket_handler_error_becomes_a_reply():
    def boom(cmd):
        raise RuntimeError("nope")

    server = ptt.PttServer(boom)
    server.start()
    try:
        assert ptt.send_command("down") == "error: nope"
    finally:
        server.stop()


def test_no_app_running_is_a_clear_error_and_exit_status(capsys):
    import ptt_cli
    assert ptt_cli.main(["down"]) == 1
    assert "isn't running" in capsys.readouterr().err
    assert ptt_cli.main(["sideways"]) == 2


def test_cli_prints_the_reply():
    import ptt_cli
    server = ptt.PttServer(lambda cmd: "ok: listening")
    server.start()
    try:
        assert ptt_cli.main(["down"]) == 0
    finally:
        server.stop()


def test_socket_address_is_under_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    addr = platform_util.ptt_address()
    if platform_util.is_windows():
        assert addr.startswith("\\\\.\\pipe\\") and platform_util.ptt_family() == "AF_PIPE"
    else:
        assert addr == str(tmp_path / "ptt.sock") and platform_util.ptt_family() == "AF_UNIX"
    long = tmp_path / ("d" * 120)
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(long))
    assert len(platform_util.ptt_address()) < 100  # too long for a Unix socket path: moved to the temp dir
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path / "other"))
    assert platform_util.ptt_address() != addr


def test_supervisor_routes_cv_ptt_to_the_client_not_the_app(monkeypatch):
    ran = []
    monkeypatch.setattr(supervisor, "run_child", lambda argv, env=None: ran.append(argv) or 0)
    assert supervisor.main(["ptt", "toggle"]) == 0
    assert len(ran) == 1 and ran[0][-2:] == [supervisor.os.path.join(supervisor.HERE, "ptt_cli.py"), "toggle"]
    assert not any(a.endswith("tui.py") for a in ran[0])


# ---- the global hotkey --------------------------------------------------------------------------------------------
def test_parse_hotkey():
    assert ptt.parse_hotkey("Ctrl+Alt+V") == ({"ctrl", "alt"}, "v")
    assert ptt.parse_hotkey("win+f9") == ({"super"}, "f9")
    for bad in ("", "ctrl+", "ctrl+alt", "hyper+v"):
        with pytest.raises(ValueError):
            ptt.parse_hotkey(bad)


def test_combo_tracker_down_and_up():
    t = ptt.ComboTracker("ctrl+alt+v")
    assert t.feed("v", True) is None  # plain v: nothing
    assert t.feed("v", False) is None
    assert t.feed("ctrl", True) is None and t.feed("alt", True) is None
    assert t.feed("v", True) == "down"
    assert t.feed("v", True) is None  # auto-repeat
    assert t.feed("v", False) == "up"
    assert t.feed("v", True) == "down"  # still holding the modifiers
    assert t.feed("ctrl", False) == "up"  # letting go of a modifier ends it too
    assert t.feed("v", False) is None


def test_a_bare_key_can_never_be_a_global_hotkey():
    class Boom:
        def __init__(self, *a):
            raise AssertionError("must not even be tried")

    with pytest.raises(ptt.HotkeyUnavailable) as e:
        ptt.GlobalHotkey.start("v", None, lambda: None, lambda: None, backends=[Boom])
    assert "ctrl+alt+v" in str(e.value)


def test_global_hotkey_drives_down_and_up_on_the_loop():
    class FakeBackend:
        NAME = "fake"
        instance = None

        def __init__(self, tracker, emit):
            self.tracker, self.emit = tracker, emit
            FakeBackend.instance = self

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    async def go():
        calls = []
        hk = ptt.GlobalHotkey.start("ctrl+alt+v", asyncio.get_running_loop(), lambda: calls.append("down"),
                                    lambda: calls.append("up"), backends=[FakeBackend])
        b = FakeBackend.instance
        for name, pressed in (("ctrl", True), ("alt", True), ("v", True), ("v", False)):
            b.emit(b.tracker.feed(name, pressed))
        await asyncio.sleep(0.01)
        assert calls == ["down", "up"]
        hk.stop()
        assert b.stopped
    run(go())


def test_unavailable_hotkey_explains_itself():
    class NoPerm:
        NAME = "evdev"

        def __init__(self, *a):
            raise ptt.HotkeyUnavailable("add yourself to the 'input' group")

    with pytest.raises(ptt.HotkeyUnavailable) as e:
        ptt.GlobalHotkey.start("ctrl+alt+v", None, lambda: None, lambda: None, backends=[NoPerm])
    assert "input" in str(e.value)


# ---- key releases inside the terminal -----------------------------------------------------------------------------
def test_kitty_sequences_never_crash_textual_and_never_become_bogus_keys():
    from textual._xterm_parser import XTermParser
    events = []
    tracker = ptt_keys.KeyTracker("v", events.append, release_supported=True)
    assert ptt_keys.install(tracker)
    try:
        parser = XTermParser()
        keys = lambda data: [(e.key) for e in parser.feed(data)]
        assert keys("\x1b[118;1;118u") == []  # press of the talk key: consumed
        assert events == ["down"]
        assert keys("\x1b[118;1:2u") == [] and events == ["down"]  # auto-repeat: ignored
        assert keys("\x1b[118;1:3u") == [] and events == ["down", "up"]  # release
        # other keys: releases vanish, repeats become presses, plain presses are untouched
        assert keys("\x1b[116;1:3u") == []
        assert keys("\x1b[116;1:2;116u") == ["t"]
        assert keys("\x1b[116;1;116u") == ["t"]
        assert keys("\x1b[1;1:3A") == [] and keys("\x1b[1;1:2A") == ["up"]
        assert events == ["down", "up"]
    finally:
        ptt_keys.uninstall()


def test_without_release_support_the_talk_key_is_left_to_the_app_toggle():
    from textual._xterm_parser import XTermParser
    events = []
    tracker = ptt_keys.KeyTracker("v", events.append, release_supported=False)
    assert ptt_keys.install(tracker)
    try:
        parser = XTermParser()
        assert [e.key for e in parser.feed("\x1b[118;1;118u")] == ["v"]  # a plain press goes through to the app
        assert events == [] and not tracker.release_supported
        # the first event type proves the terminal reports releases
        assert [e.key for e in parser.feed("\x1b[116;1:3u")] == [] and tracker.release_supported
        assert [e.key for e in parser.feed("v")] == ["v"]  # a legacy-encoded v is not tracked (no event type)
    finally:
        ptt_keys.uninstall()


def test_talk_key_is_an_ordinary_key_when_inactive_and_modified_keys_are_not_the_talk_key():
    events = []
    active = [False]
    t = ptt_keys.KeyTracker("v", events.append, is_active=lambda: active[0], release_supported=True)
    assert not t.handle(118, 1, 1)  # typing "v" into the input box
    active[0] = True
    assert not t.handle(118, 5, 1)  # ctrl+v
    assert t.handle(118, 2, 1) and events == ["down"]  # shift is fine
    active[0] = False
    assert t.handle(118, 1, 3) and events == ["down", "up"]  # a release is still honoured
    assert t.reset() is False
    active[0] = True
    t.handle(118, 1, 1)
    assert t.reset() and events[-1] == "up"  # focus lost mid-hold


def test_terminal_detection():
    assert ptt_keys.terminal_probably_reports_releases({"KITTY_WINDOW_ID": "3"})
    assert ptt_keys.terminal_probably_reports_releases({"TERM": "foot"})
    assert ptt_keys.terminal_probably_reports_releases({"TERM_PROGRAM": "WezTerm"})
    assert not ptt_keys.terminal_probably_reports_releases({"TERM": "xterm-256color"})
    assert ptt_keys.terminal_probably_reports_releases({"TERM": "xterm-256color", "CV_PTT_RELEASE": "1"})
    assert not ptt_keys.terminal_probably_reports_releases({"TERM": "foot", "CV_PTT_RELEASE": "0"})


# ---- the window ---------------------------------------------------------------------------------------------------
def test_window_mode_key_status_and_toggle_fallback(tmp_path, monkeypatch):
    import tui
    from test_tui import FakeEngine
    monkeypatch.setattr(tui, "STATE_FILE", str(tmp_path / "ui.json"))
    monkeypatch.setenv("CV_PTT_RELEASE", "0")  # whatever terminal runs the tests: one without key releases

    async def go():
        app = tui.VoiceApp(tui.make_parser().parse_args([]))
        fake = app.engine = FakeEngine()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            status = lambda: (str(app.query_one("#status").render()), str(app.query_one("#hints").render()))
            await pilot.pause(0.3)
            words, hints = status()
            assert words == "listening" and "p push to talk" in hints

            await pilot.press("v")  # always listening: v does nothing
            assert fake.ptt_calls == []

            await pilot.press("p")
            await pilot.pause(0.3)
            assert fake.input_mode == "push_to_talk"
            words, hints = status()
            assert words == "press V to talk" and "V again stops" in hints and "p always listening" in hints
            assert json.load(open(tui.STATE_FILE, encoding="utf-8"))["input_mode"] == "push_to_talk"  # saved with the other prefs

            await pilot.press("v")  # no release events here: press toggles
            assert fake.ptt_calls == ["toggle"]
            fake.state = "talking"
            await pilot.pause(0.3)
            assert status()[0] == "listening" and status()[1] == "press V to send"
            fake.state = "listening"

            app.keys = types.SimpleNamespace(release_supported=True, reset=lambda: False)  # a terminal with releases
            await pilot.pause(0.3)
            assert status()[0] == "hold V to talk"
            await pilot.press("v")  # the parser hook handles it in that case: the app does nothing
            assert fake.ptt_calls == ["toggle"]
            app.keys = None

            await pilot.press("space")
            assert fake.muted
            await pilot.press("space")

            await pilot.press("i")  # typing a v in the input box is just a v
            await pilot.press("v")
            assert fake.ptt_calls == ["toggle"] and app.query_one("#input").value == "v"
            await pilot.press("escape")

            await pilot.press("p")
            assert fake.input_mode == "always_listening"
        # restarts remember the choice, unless the config changed since
        app2 = tui.VoiceApp(tui.make_parser().parse_args([]))
        assert app2.engine.input_mode == "always_listening"
        app2.prefs = {"input_mode": "push_to_talk", "input_mode_config": "always_listening"}
        assert app2.chosen_mode() == "push_to_talk"
        app2.prefs = {"input_mode": "push_to_talk", "input_mode_config": "something else"}
        assert app2.chosen_mode() == "always_listening"
    run(go())


def test_ptt_key_that_clashes_with_a_cv_key_falls_back_to_v(monkeypatch):
    import tui
    app = tui.VoiceApp.__new__(tui.VoiceApp)
    for key, expected in (("t", "v"), ("Space", "v"), ("F9", "f9"), (" X ", "x")):
        monkeypatch.setattr(tui.config, "get", lambda key=key: {"input": {"ptt_key": key}})
        assert app.chosen_ptt_key() == expected
