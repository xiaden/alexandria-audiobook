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

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.clone_reference_media import (
    CloneReferenceMediaError,
    canonical_contain,
    cleanup_expired_references,
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


# ---------------------------------------------------------------------------
# cleanup_expired_references (bounded post-retention sweep)
# ---------------------------------------------------------------------------


class TestCleanupExpiredReferences:
    """Bounded cleanup sweep for tombstoned, unreferenced reference files."""

    def _seed_voice(self, storage: InMemorySQLiteAdapter) -> None:
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, type, voice) "
            "VALUES ('vc', 'Clone', 'clone', 'c')"
        )

    def _add_reference(
        self,
        storage: InMemorySQLiteAdapter,
        reference_id: str,
        relative_path: str,
        *,
        deleted_ms: int | None = None,
    ) -> None:
        """Insert a clone_reference row, optionally tombstoned at *deleted_ms*."""
        storage.insert_clone_reference(
            {
                "reference_id": reference_id,
                "voice_id": "vc",
                "owner_id": "local",
                "relative_path": relative_path,
                "original_filename": relative_path,
                "media_type": "audio/wav",
                "byte_size": 100,
                "duration_ms": 1000,
                "sha256": "a" * 64,
                "created_ms": 0,
            }
        )
        if deleted_ms is not None:
            storage.tombstone_clone_reference(
                reference_id, "local", deleted_ms
            )

    def test_only_old_tombstoned_files_removed_and_rows_hard_deleted(
        self, tmp_path
    ):
        # now=2_000_000, retention=100_000 -> threshold 1_900_000.  Only rows
        # tombstoned strictly before the threshold are cleanup candidates.
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        self._seed_voice(storage)
        root = str(tmp_path)

        (tmp_path / "old.wav").write_bytes(b"data")
        self._add_reference(storage, "old", "old.wav", deleted_ms=1_000_000)

        # Fresh tombstone still inside the retention window: kept.
        (tmp_path / "fresh.wav").write_bytes(b"data")
        self._add_reference(storage, "fresh", "fresh.wav", deleted_ms=1_950_000)

        removed = cleanup_expired_references(
            storage, root, older_than_ms=100_000, now_ms=2_000_000
        )

        assert removed == ["old"]
        assert not (tmp_path / "old.wav").exists()
        # Row hard-deleted after the file is removed.
        assert storage.get_clone_reference("old", "local") is None
        # Fresh tombstone untouched, file + row preserved.
        assert (tmp_path / "fresh.wav").exists()
        assert storage.get_clone_reference("fresh", "local") is not None

    def test_active_reference_never_swept(self, tmp_path):
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        self._seed_voice(storage)
        root = str(tmp_path)

        # Active (non-tombstoned) reference with an old created_ms: the sweep
        # must not touch it even though the row is old.
        (tmp_path / "active.wav").write_bytes(b"data")
        self._add_reference(storage, "active", "active.wav", deleted_ms=None)

        removed = cleanup_expired_references(
            storage, root, older_than_ms=0, now_ms=2_000_000
        )

        assert removed == []
        assert (tmp_path / "active.wav").exists()
        assert storage.get_clone_reference("active", "local") is not None

    def test_symlink_never_followed_or_removed(self, tmp_path):
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        self._seed_voice(storage)
        root = str(tmp_path)

        # A real victim file just outside the reference root.
        victim = tmp_path.parent / "victim.wav"
        victim.write_bytes(b"victim-data")
        # A tombstoned row whose relative_path is a symlink pointing outside
        # the root — the canonical escape path.
        link = tmp_path / "link.wav"
        link.symlink_to(victim)
        self._add_reference(storage, "link", "link.wav", deleted_ms=0)

        removed = cleanup_expired_references(
            storage, root, older_than_ms=0, now_ms=2_000_000
        )

        assert removed == []
        assert victim.exists()          # link target never followed/removed
        assert link.is_symlink()        # the link itself never removed
        assert storage.get_clone_reference("link", "local") is not None

    def test_row_kept_when_backing_file_missing(self, tmp_path):
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        self._seed_voice(storage)
        root = str(tmp_path)

        # Tombstoned well past retention, but no backing file exists (already
        # gone).  Sweep skips it: no file to remove means the row stays.
        self._add_reference(storage, "gone", "gone.wav", deleted_ms=0)

        removed = cleanup_expired_references(
            storage, root, older_than_ms=0, now_ms=2_000_000
        )

        assert removed == []
        assert storage.get_clone_reference("gone", "local") is not None
