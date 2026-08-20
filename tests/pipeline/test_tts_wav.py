"""Tests for WAV serialization in the TTS engine."""

import numpy as np
import pytest
import soundfile as sf

from app.tts import TTSEngine


def test_save_wav_interleaves_channel_first_audio(tmp_path):
    """Channel-first model output is written frame-interleaved."""
    channel_first = np.array(
        [
            [0.1, 0.2, 0.3],
            [-0.1, -0.2, -0.3],
        ],
        dtype=np.float32,
    )
    output_path = tmp_path / "stereo.wav"

    TTSEngine._save_wav(channel_first, 16000, str(output_path))

    samples, sample_rate = sf.read(output_path, dtype="float32")
    np.testing.assert_allclose(samples, channel_first.T, atol=1 / 32768)
    assert sample_rate == 16000


def test_save_wav_preserves_mono_audio(tmp_path):
    """The normal 1-D model output remains unchanged."""
    mono = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    output_path = tmp_path / "mono.wav"

    TTSEngine._save_wav(mono, 22050, str(output_path))

    samples, sample_rate = sf.read(output_path, dtype="float32")
    np.testing.assert_allclose(samples, mono, atol=1 / 32768)
    assert sample_rate == 22050


def test_save_wav_rejects_more_than_two_dimensions(tmp_path):
    """Unsupported tensor shapes fail explicitly instead of reaching SoundFile."""
    with pytest.raises(ValueError):
        TTSEngine._save_wav(np.zeros((1, 2, 3), dtype=np.float32), 16000, str(tmp_path / "bad.wav"))
