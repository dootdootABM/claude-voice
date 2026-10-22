"""What the voice is given to say: sentence chunking for natural reading, pronunciation fix-ups, and the pauses
between clips. No model or audio device needed.

Run: pytest test_speech.py
"""

import asyncio
import types

import numpy as np
import pytest

import audio
from audio import SentenceSplitter, clean_for_speech


def stream(text, step=3):
    """Feed text the way a reply streams in, a few characters at a time → the chunks it is spoken in."""
    s, out = SentenceSplitter(), []
    for i in range(0, len(text), step):
        out += s.feed(text[i:i + step])
    return out + s.flush()


def test_chunks_end_only_where_sentences_end():
    text = ("No roadblock on the usage side. Piece seven is only partly done: the device setting reaches Whisper "
            "and Kokoro, but the Windows fallbacks aren't in yet. I'll redo that part.")
    chunks = [c.strip() for c in stream(text)]
    assert chunks == ["No roadblock on the usage side.",
                      "Piece seven is only partly done: the device setting reaches Whisper and Kokoro, but the Windows "
                      "fallbacks aren't in yet.",
                      "I'll redo that part."]


def test_first_chunk_early_only_at_a_clause_break():
    long_first = "The builder finished the live window and the reviewer checked it, so the next piece can start now."
    chunks = [c.strip() for c in stream(long_first)]
    assert chunks[0] == "The builder finished the live window and the reviewer checked it,"  # a real clause break
    no_break = "The builder finished the live window and the reviewer checked every single part of it carefully."
    assert [c.strip() for c in stream(no_break)] == [no_break]  # never cut mid-phrase, however long
    short = "Yes, done."
    assert [c.strip() for c in stream(short)] == [short]  # too short to split


def test_code_blocks_are_skipped():
    assert "print" not in " ".join(stream("Here it is.\n\n```py\nprint(1)\n```\n\nThat's all."))


@pytest.mark.parametrize("text, said", [
    ("Update the README and run pytest.", "Update the read me and run pie test."),
    ("CI passed on the TUI.", "C I passed on the T U I."),
    ("It's in config.yaml, not audio.py.", "It's in config dot yaml, not audio dot py."),
    ("Haiku vs Sonnet, e.g. for jobs.", "Haiku versus Sonnet, for example for jobs."),
    ("Look in ~/src for it.", "Look in home folder src for it."),
    ("CIA and pytests stay as they are.", "CIA and pytests stay as they are."),  # whole words only
])
def test_pronunciation_fixups(text, said):
    assert clean_for_speech(text) == said


def test_users_own_pronunciation_wins():
    assert clean_for_speech("Open rmpc, then CI.", {"rmpc": "R M P C", "CI": "continuous integration"}) == \
        "Open R M P C, then continuous integration."


def test_clips_get_a_natural_pause(monkeypatch):
    played = []
    player = types.SimpleNamespace(play=played.append, stop=lambda: None, busy=False)
    kokoro = types.SimpleNamespace(create=lambda text, **k: (np.ones(100, dtype=np.float32), audio.TTS_SR))

    async def run():
        tts = audio.Tts(player, (kokoro, "cpu"))
        tts.say("The tests pass.", "af_bella")
        tts.say("Then the README,", "af_bella")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(played) == 2:
                break
        tts._worker.cancel()

    asyncio.run(run())
    assert len(played[0]) == 100 + int(audio.SENTENCE_PAUSE_S * audio.TTS_SR)
    assert len(played[1]) == 100 + int(audio.CLAUSE_PAUSE_S * audio.TTS_SR)
