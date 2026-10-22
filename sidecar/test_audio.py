"""Player gain ducking must ramp, not step (a step is an audible click). Run: pytest test_audio.py"""

import collections
import sys
import threading

import numpy as np
import pytest

import audio

def test_gain_ducking_ramps():
    p = audio.Player.__new__(audio.Player)  # no sound card needed
    p.clips, p.lock, p.current, p.pos = collections.deque(), threading.Lock(), None, 0
    p.gain = p.applied_gain = 1.0
    p.idle_since = 0.0
    p.play(np.ones(4800, dtype=np.float32))

    blocks = []
    for i in range(6):
        if i == 2:
            p.gain = 0.25  # the user starts talking
        out = np.zeros((480, 1), dtype=np.float32)
        p._fill(out, 480)
        blocks.append(out[:, 0])
    y = np.concatenate(blocks)
    assert y[:960].min() == 1.0 and abs(y[-1] - 0.25) < 1e-6, (y[:3], y[-1])
    assert np.abs(np.diff(y)).max() < 0.01, f"gain step too abrupt: {np.abs(np.diff(y)).max()}"
    print("ok: max sample-to-sample jump", np.abs(np.diff(y)).max())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
