"""Focused tests for app/pipeline/clone_reference_media.py.

Covers the path-containment boundary, media allow-list / sniffing, bounded
duration probing, SHA-256, atomic persistence, and partial-write cleanup.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct

import pytest

from app.pipeline.clone_reference_media import (
    CloneReferenceMediaError,
    canonical_contain,
    compute_sha256,
    media_type_from_path,
    probe_duration_ms,
    reference_root,
    sniff_allows,
    validate_and_copy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_wav(duration_seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Minimal valid 16-bit mono PCM WAV blob."""
    num_samples = int(duration_seconds * sample_rate)
    byte_rate = sample_rate * 2
    data = struct.pack("<h", 0) * num_samples
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + fmt
    header += b"data" + struct.pack("<I", len(data))
    return header + data


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


class TestCanonicalContain:
    def test_contained_child_resolves(self, tmp_path):
        child = os.path.join(str(tmp_path), "a.wav")
        assert canonical_contain(str(tmp_path), child) == os.path.realpath(child)

    def test_parent_traversal_rejected(self, tmp_path):
        outside = os.path.join(str(tmp_path), "..", "escape.wav")
        assert canonical_contain(str(tmp_path), outside) is None

    def test_symlink_escape_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside_victim"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        candidate = str(link / "x.wav")
        assert canonical_contain(str(tmp_path), candidate) is None




# ---------------------------------------------------------------------------
# Media allow-list / sniffing
# ---------------------------------------------------------------------------


class TestMediaTypeFromPath:
    def test_known_extensions(self):
        assert media_type_from_path("sample.wav") == "audio/wav"
        assert media_type_from_path("sample.MP3") == "audio/mpeg"
        assert media_type_from_path("sample.ogg") == "audio/ogg"

    def test_unsupported_extension_rejected(self):
        with pytest.raises(CloneReferenceMediaError):
            media_type_from_path("sample.txt")

    def test_missing_extension_rejected(self):
        with pytest.raises(CloneReferenceMediaError):
            media_type_from_path("sample")


class TestSniffAllows:
    def test_matching_magic_ok(self):
        assert sniff_allows("audio/wav", b"RIFF....WAVE")
        assert sniff_allows("audio/ogg", b"OggS....")

    def test_mismatched_content_rejected(self):
        assert not sniff_allows("audio/wav", b"GIF89a....")
        assert not sniff_allows("audio/flac", b"ID3....")


class TestComputeSha256:
    def test_known_digest(self):
        data = b"hello world"
        assert compute_sha256(io.BytesIO(data)) == hashlib.sha256(data).hexdigest()

    def test_empty_stream(self):
        assert compute_sha256(io.BytesIO(b"")) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Duration probing
# ---------------------------------------------------------------------------


class TestProbeDuration:
    def test_wav_duration_probed(self, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(make_wav(duration_seconds=1.0))
        assert probe_duration_ms(str(path)) == pytest.approx(1000, abs=5)

    def test_non_media_raises(self, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(b"not really audio at all")
        with pytest.raises(CloneReferenceMediaError):
            probe_duration_ms(str(path))

    def test_engine_seam_preferred_over_ffprobe(self, tmp_path):
        # A broken media file would fail ffprobe; the engine probe seam must
        # be preferred when the injected TTS engine exposes it.
        path = tmp_path / "a.wav"
        path.write_bytes(b"junk")
        calls = []

        class FakeEngine:
            def probe_duration_ms(self, p):
                calls.append(p)
                return 12345

        assert probe_duration_ms(str(path), tts_engine=FakeEngine()) == 12345
        assert calls == [str(path)]


# ---------------------------------------------------------------------------
# validate_and_copy
# ---------------------------------------------------------------------------


class TestValidateAndCopy:
    def test_persists_and_returns_metadata(self, tmp_path):
        root = str(tmp_path)
        payload = make_wav(duration_seconds=1.0)
        metadata = validate_and_copy(
            io.BytesIO(payload), root, "ref123", "clip.wav"
        )
        assert metadata["relative_path"] == "ref123.wav"
        assert metadata["original_filename"] == "clip.wav"
        assert metadata["media_type"] == "audio/wav"
        assert metadata["byte_size"] == len(payload)
        assert metadata["duration_ms"] == pytest.approx(1000, abs=5)
        assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
        assert (tmp_path / "ref123.wav").read_bytes() == payload

    def test_unsupported_extension_rejected(self, tmp_path):
        with pytest.raises(CloneReferenceMediaError):
            validate_and_copy(
                io.BytesIO(b"data"), str(tmp_path), "ref123", "clip.txt"
            )
        assert list(tmp_path.iterdir()) == []

    def test_content_mismatch_rejected_and_cleaned(self, tmp_path):
        with pytest.raises(CloneReferenceMediaError):
            validate_and_copy(
                io.BytesIO(b"not a wav file at all"), str(tmp_path), "r1", "a.wav"
            )
        assert list(tmp_path.iterdir()) == []

    def test_oversize_rejected_and_partial_removed(self, tmp_path):
        payload = make_wav(duration_seconds=1.0)
        with pytest.raises(CloneReferenceMediaError):
            validate_and_copy(
                io.BytesIO(payload),
                str(tmp_path),
                "r1",
                "a.wav",
                max_bytes=16,
            )
        # No .tmp, no .wav, no partial file left behind.
        assert list(tmp_path.iterdir()) == []

    def test_duration_limit_rejected_and_cleaned(self, tmp_path):
        payload = make_wav(duration_seconds=1.0)
        with pytest.raises(CloneReferenceMediaError):
            validate_and_copy(
                io.BytesIO(payload),
                str(tmp_path),
                "r1",
                "a.wav",
                max_duration_ms=1,
            )
        assert list(tmp_path.iterdir()) == []

    def test_traversal_filename_uses_basename_extension_only(self, tmp_path):
        # Even a hostile original filename is stored under the reference id;
        # only the extension is derived from it.
        payload = make_wav(duration_seconds=1.0)
        metadata = validate_and_copy(
            io.BytesIO(payload),
            str(tmp_path),
            "r1",
            "../../../evil.wav",
        )
        assert metadata["relative_path"] == "r1.wav"
        assert (tmp_path / "r1.wav").read_bytes() == payload
        assert not (tmp_path.parent / "evil.wav").exists()

    def test_reference_root_defaults_under_repo(self):
        root = reference_root()
        assert root.endswith(os.path.join("designed_voices", "references"))
