"""The echo gate: without headphones the mic hears the reply, and that must not duck, interrupt or be sent back as
you. No sound card or model is used."""

import asyncio
import types

import numpy as np

from audio import FRAME, EchoGate, Listener
from test_ptt import Rig, run


class FakePlayer:
    """Plays at `level` (rms) until stopped."""

    def __init__(self, level=0.1):
        self.level, self.gain = level, 1.0

    def recent_level(self, window):
        return self.level, self.level


def learned(gate, coupling, frames=60):
    for _ in range(frames):
        gate.is_echo(coupling * gate.player.level)
    return gate


def test_speaker_echo_is_gated_and_a_louder_voice_is_not():
    gate = learned(EchoGate(FakePlayer(0.1)), coupling=0.5)
    assert abs(gate.coupling - 0.5) < 1e-6
    assert gate.is_echo(0.05) and gate.is_echo(0.08)  # the echo, and a bit over it
    assert not gate.is_echo(0.2)  # you, talking over it


def test_headphones_change_nothing():
    gate = learned(EchoGate(FakePlayer(0.1)), coupling=0.001)
    assert not gate.is_echo(0.01)  # even a quiet voice gets through


def test_nothing_playing_gates_nothing_and_teaches_nothing():
    gate = EchoGate(FakePlayer(0.0))
    for _ in range(60):
        assert not gate.is_echo(0.3)
    assert not gate.ratios and gate.coupling == EchoGate.UNKNOWN


def test_talking_over_it_now_and_then_does_not_raise_the_bar():
    gate = EchoGate(FakePlayer(0.1))
    for i in range(300):
        gate.is_echo(0.5 if i % 20 == 0 else 0.03)  # 5% of frames are you
    assert gate.coupling < 0.35


def test_until_learned_only_a_clearly_loud_voice_counts():
    gate = EchoGate(FakePlayer(0.1))
    assert gate.is_echo(0.1) and not gate.is_echo(0.3)


def speaker_rig():
    """An always-listening Listener whose VAD calls everything speech, playing through speakers at 0.1."""
    r = Rig(mode=Listener.ALWAYS)
    player = FakePlayer(0.1)
    r.tts.player, r.tts.speaking = player, True
    r.listener.echo = EchoGate(player)
    r.listener.vad = lambda frame: 0.9
    r.listener.vad.reset = lambda: None
    return r, player


def test_its_own_voice_through_speakers_does_not_interrupt_it():
    async def go():
        r, player = speaker_rig()
        await r.frames(80, level=0.04)  # echo at 0.4 of the output, the whole time it talks
        assert r.started == 0 and not r.listener.in_speech and player.gain == 1.0
        await r.close()
    run(go())


def test_you_can_still_interrupt_it_through_speakers():
    async def go():
        r, player = speaker_rig()
        await r.frames(40, level=0.04)  # it learns the echo
        await r.frames(5, level=0.3)  # you, clearly louder
        assert r.started == 1 and r.listener.in_speech
        await r.close()
    run(go())
