"""Windows audio fallbacks and the resampler, with mocks (no sound hardware). Run: pytest test_audio_win.py"""

import asyncio
from unittest import mock

import numpy as np

import audio


def _feed(rs, in_sr, out_sr, chunk, seconds=2):
    ch = audio.FrameChunker(audio.FRAME)
    frames, total_out = [], 0
    for _ in range(int(in_sr * seconds) // chunk):
        out = rs.process(np.sin(np.arange(chunk, dtype=np.float32)))
        total_out += len(out)
        frames += ch.push(out)
    return frames, total_out


def test_resampler_frames_and_length():
    for in_sr in (48000, 44100, 32000, 16000, 8000):
        chunk = round(512 * in_sr / 16000)
        frames, total = _feed(audio.Resampler(in_sr, 16000), in_sr, 16000, chunk)
        expected = (in_sr * 2 // chunk) * chunk * 16000 / in_sr
        assert frames and all(f.shape == (512,) and f.dtype == np.float32 for f in frames), in_sr
        assert abs(total - expected) <= 4, (in_sr, total)


def test_resampler_upsample_and_odd_chunks():
    rs = audio.Resampler(24000, 44100)
    total = sum(len(rs.process(np.zeros(n))) for n in [480] * 100 + [1, 7, 333])
    assert abs(total - (48000 + 341) * 44100 / 24000) < 4


def test_resampler_preserves_dc_and_tone():
    rs = audio.Resampler(48000, 16000)
    out = np.concatenate([rs.process(np.full(960, 0.5)) for _ in range(5)])
    assert np.allclose(out[5:], 0.5, atol=1e-5)


def _sd_mock(fail_first=True):
    sd = mock.MagicMock()
    calls = []

    def make(**kw):
        calls.append(kw)
        if fail_first and "extra_settings" in kw:
            raise RuntimeError("Invalid sample rate")
        return mock.MagicMock()
    sd.InputStream.side_effect = lambda **kw: make(**kw)
    sd.OutputStream.side_effect = lambda **kw: make(**kw)
    sd.query_devices.return_value = {"default_samplerate": 48000.0}
    return sd, calls


def test_mic_windows_fallback_order():
    sd, calls = _sd_mock()
    with mock.patch.object(audio, "sd", sd), mock.patch.object(audio.platform_util, "is_windows", return_value=True):
        mic = audio.Mic(mock.MagicMock())
    assert "extra_settings" in calls[0] and calls[0]["samplerate"] == 16000
    assert calls[1]["samplerate"] == 48000 and calls[1]["blocksize"] == 1536 and "extra_settings" not in calls[1]
    assert mic.resampler is not None


def test_mic_windows_auto_convert_ok():
    sd, calls = _sd_mock(fail_first=False)
    with mock.patch.object(audio, "sd", sd), mock.patch.object(audio.platform_util, "is_windows", return_value=True):
        mic = audio.Mic(mock.MagicMock())
    assert len(calls) == 1 and mic.resampler is None


def test_mic_linux_unchanged():
    sd, calls = _sd_mock()
    with mock.patch.object(audio, "sd", sd), mock.patch.object(audio.platform_util, "is_windows", return_value=False):
        audio.Mic(mock.MagicMock())
    assert calls == [dict(samplerate=16000, channels=1, dtype="float32", blocksize=512, callback=mock.ANY)]


def test_mic_callback_emits_exact_frames():
    sd, _ = _sd_mock()
    loop = mock.MagicMock()
    with mock.patch.object(audio, "sd", sd), mock.patch.object(audio.platform_util, "is_windows", return_value=True):
        mic = audio.Mic(loop)
    for _ in range(10):
        mic._cb(np.zeros((1536, 1), dtype=np.float32), 1536, None, None)
    sent = [c.args[1] for c in loop.call_soon_threadsafe.call_args_list]
    assert len(sent) >= 9 and all(f.shape == (512,) for f in sent)


def test_player_windows_fallback_and_callback_size():
    sd, calls = _sd_mock()
    with mock.patch.object(audio, "sd", sd), mock.patch.object(audio.platform_util, "is_windows", return_value=True):
        p = audio.Player()
    assert calls[0]["latency"] == audio.WIN_OUT_LATENCY and "extra_settings" in calls[0]
    assert calls[1]["samplerate"] == 48000 and calls[1]["blocksize"] == 960
    p.play(np.ones(2400, dtype=np.float32))
    for n in (960, 960, 441, 1000):
        out = np.zeros((n, 1), dtype=np.float32)
        p._cb(out, n, None, None)
        assert out.shape == (n, 1)
    assert out.any()


def test_player_linux_unchanged():
    sd, calls = _sd_mock()
    with mock.patch.object(audio, "sd", sd), mock.patch.object(audio.platform_util, "is_windows", return_value=False):
        audio.Player()
    assert calls == [dict(samplerate=24000, channels=1, dtype="float32", blocksize=480, latency="low",
                          callback=mock.ANY)]


def test_tts_load_cpu_never_selects_cuda_even_when_available():
    import sys
    ort = mock.MagicMock()
    ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    ort.InferenceSession.return_value.get_providers.return_value = ["CPUExecutionProvider"]
    kokoro_mod = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"onnxruntime": ort, "kokoro_onnx": kokoro_mod}):
        _, device = audio.Tts.load("cpu")
    ort.preload_dlls.assert_not_called()
    (model_path,), kwargs = ort.InferenceSession.call_args
    assert model_path.endswith("kokoro-v1.0.int8.onnx")
    assert kwargs["providers"] == ["CPUExecutionProvider"]
    assert device == "CPUExecutionProvider"
