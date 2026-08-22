"""Spec-first tests for GET /api/pipeline/download/{job_id} (app/pipeline/api_export.py).

Phase 6 of TASK-universal-upgrade-B-render-walk-persistence: the download
endpoint is rewritten to be row-backed (the render_job row is the source of
truth, NOT the in-process ``_render_jobs`` dict) and to return a real 404
with a JSON detail body when a completed job's artifact file is missing —
instead of the previous broken 200 (an empty ``audiobook.zip``).

Covers:
- present-file 200: completed job row whose output_artifact_path is an
  existing audiobook.m4b → 200, content-type audio/mp4, filename
  audiobook.m4b, exact file bytes
- zip fallback: completed job row with wav/mp3/m4a/flac chunks in the
  output dir but no m4b → 200, application/zip, audiobook.zip containing
  exactly those files (ZIP_STORED)
- unknown job 404: no render_job row → 404 'Unknown job_id: {id}'
- FileResponse-404 subclass: completed job whose recorded artifact file is
  missing → 404 with a JSON detail body (NOT a broken 200)
- not-completed job 404: 'Job not completed (status: X)' preserved

Phase 4 of TASK-universal-upgrade-C-artifacts-gc-export-backend adds
GET /api/pipeline/export/chunk/{job_id}/{idx} (bounded-range WAV serving):
- full GET (no Range) -> 200 audio/wav full body (never capped)
- valid byte Range -> 206 + Content-Range (capped per request to
  PIPELINE_MAX_RANGE_BYTES, default 4 MiB)
- unsatisfiable Range -> 416 + Content-Range: bytes */total
- malformed Range -> 400 (starlette >= 0.49.1 native behavior)
- unknown job / unknown idx / non-integer idx -> 404
- pending / failed chunk rows -> 409 (row exists, not servable)
- evicted chunk rows (GC tombstone) -> 410 Gone
- wav_path resolved from the row against the run dir, containment-checked
  (path traversal -> 404); missing file -> 404 JSON

Phase 5 of TASK-universal-upgrade-C-artifacts-gc-export-backend adds
GET /api/pipeline/export/audio/{job_id} (whole-book playback):
- completed job with a file artifact (output_artifact_path names a file, e.g.
  audiobook.m4b) -> 200 serving the artifact with Range support and the media
  type for its extension (audio/mp4 for .m4b, audio/mpeg for .mp3, ...)
- completed individual-mode job with no file artifact -> 200 audio/wav: a
  synthesized whole-book WAV (chunk 0's RIFF header with sizes patched for the
  summed payload + each chunk's PCM data) streamed chunk-by-chunk from disk
  with Range support computed across chunk boundaries
- batch-mode job (no chunk rows) -> synthesized whole-book WAV from the sorted
  *.wav files in the run dir
- unknown job -> 404; non-completed job -> 404 'Job not completed (status: X)';
  expired job (GC tombstone) -> 410 Gone; evicted/non-done chunks -> 410/409
- missing artifact/chunk file -> 404 JSON; path traversal -> 404;
  chunk formats differing (sample rate/channels/bits) -> 409

Phase 6 of TASK-universal-upgrade-C-artifacts-gc-export-backend adds
POST /api/pipeline/export/m4b (3-phase FFMETADATA1 polished export):
- request: multipart form (job_id + title/author/narrator/year/description +
  optional cover UploadFile); rows = truth, dispatch mirrors export_audio
  (unknown job -> 404, expired -> 410, non-completed -> 404, pending/evicted
  chunks -> 409/410, path traversal -> 404, format mismatch -> 409)
- phase 1 CONCAT: ffmpeg concat demuxer joins the chunk WAVs (rows for
  individual mode, sorted *.wav for batch) into an intermediate WAV
- phase 2 METADATA: audiobook.ffmetadata written atomically (tmp -> os.replace)
  with global tags (title/artist/album_artist/date/comment) and one [CHAPTER]
  per chunk, TIMEBASE=1/1000, integer-ms START/END, last END clamped to the
  concatenated duration from a single ffprobe pass
- phase 3 MUX: m4b (aac + ipod container + optional mjpeg attached_pic cover),
  mp3 (libmp3lame, feature-detected; degrade to m4b-only with a message when
  absent), audiobook-audacity.zip (ZIP_STORED, chunk wavs + mp3)
- output_artifact_path updated to the m4b; m4b servable through the phase-5
  GET /export/audio/{job_id} artifact path as audio/mp4
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api import get_storage, router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage():
    """In-memory SQLite adapter (render_job table created by init_db)."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture()
def client(storage):
    """FastAPI TestClient with the combined pipeline router and storage override."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/pipeline/download/{job_id}
# ---------------------------------------------------------------------------


class TestDownloadRender:
    """Row-backed GET /api/pipeline/download/{job_id}."""

    def test_present_m4b_file_returns_200_audio_mp4(self, client, storage, tmp_path):
        """A completed job whose output_artifact_path is an existing audiobook.m4b
        is served with audio/mp4 and filename audiobook.m4b."""
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        m4b_path = output_dir / "audiobook.m4b"
        m4b_path.write_bytes(b"FAKE-M4B-CONTENT")

        job_id = "job-m4b-present"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (job_id, str(output_dir), str(m4b_path)),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mp4"
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="audiobook.m4b"'
        )
        assert resp.content == b"FAKE-M4B-CONTENT"

    def test_present_wav_artifact_returns_200_audio_wav(
        self, client, storage, tmp_path
    ):
        """A recorded paused WAV is downloaded with its native type and name."""
        output_dir = tmp_path / "out-wav"
        output_dir.mkdir()
        wav_path = output_dir / "audiobook-paused.wav"
        wav_path.write_bytes(b"FAKE-WAV-CONTENT")

        job_id = "job-wav-present"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (job_id, str(output_dir), str(wav_path)),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.headers["content-disposition"] == (
            'attachment; filename="audiobook-paused.wav"'
        )
        assert resp.content == b"FAKE-WAV-CONTENT"

    def test_zip_fallback_when_no_m4b(self, client, storage, tmp_path):
        """A completed job row with audio chunks but no m4b yields audiobook.zip
        (application/zip) containing exactly the wav/mp3/m4a/flac files."""
        output_dir = tmp_path / "out-zip"
        output_dir.mkdir()
        chunk_names = (
            "chunk_0000.wav",
            "chunk_0001.mp3",
            "chunk_0002.m4a",
            "chunk_0003.flac",
        )
        for name in chunk_names:
            (output_dir / name).write_bytes(b"audio-data-" + name.encode())
        # Non-audio file must be excluded from the archive.
        (output_dir / "ignore.txt").write_bytes(b"nope")

        job_id = "job-zip-fallback"
        # Completed without an m4b: _finalize_job records the output dir as
        # output_artifact_path.
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (job_id, str(output_dir), str(output_dir)),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="audiobook.zip"'
        )

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert sorted(zf.namelist()) == sorted(chunk_names)
        assert zf.testzip() is None
        assert zf.getinfo("chunk_0000.wav").file_size == len(
            b"audio-data-chunk_0000.wav"
        )

    def test_zip_fallback_natural_chunk_order(self, client, storage, tmp_path):
        """The on-demand ZIP preserves numeric batch chunk order."""
        output_dir = tmp_path / "out-zip-natural"
        output_dir.mkdir()
        for index in (10, 2, 0):
            (output_dir / f"temp_batch_{index}.wav").write_bytes(str(index).encode())
        job_id = "job-zip-natural"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (job_id, str(output_dir), str(output_dir)),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert zf.namelist() == [
                "temp_batch_0.wav",
                "temp_batch_2.wav",
                "temp_batch_10.wav",
            ]

    def test_unknown_job_returns_404(self, client):
        """No render_job row for the job_id → 404 with the job_id in the detail."""
        resp = client.get("/api/pipeline/download/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown job_id: does-not-exist"

    def test_missing_artifact_returns_404_with_detail(self, client, storage, tmp_path):
        """A completed job whose recorded output_artifact_path file is MISSING
        returns 404 with a JSON detail body — NOT a broken 200.

        The legacy implementation 404s when the output DIR is missing but
        serves an empty ``audiobook.zip`` (200) when the artifact FILE
        itself is gone; this test locks in the FileResponse-404 behavior.
        """
        from app.pipeline import api_export

        output_dir = tmp_path / "out-missing"
        output_dir.mkdir()  # directory exists; the m4b does not

        job_id = "job-artifact-missing"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (job_id, str(output_dir), str(output_dir / "audiobook.m4b")),
        )
        # Mirror the legacy in-memory entry the dict-backed implementation
        # would carry (status completed + output_dir) so the RED run hits the
        # actual defect: 200 empty zip instead of a 404.
        api_export._render_jobs[job_id] = {
            "status": "completed",
            "output_dir": str(output_dir),
        }

        try:
            resp = client.get(f"/api/pipeline/download/{job_id}")
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Audio file not found"
        finally:
            api_export._render_jobs.pop(job_id, None)

    def test_job_not_completed_returns_404(self, client, storage):
        """A non-completed job row → 404 'Job not completed (status: X)'."""
        job_id = "job-still-running"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir) "
            "VALUES (?, 'b1', 'batch', 'running', '/tmp/out')",
            (job_id,),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not completed (status: running)"

    def test_missing_output_dir_returns_404(self, client, storage):
        """A completed row whose output_dir does not exist → 404 (preserved check,
        now adapted to read the path from the row instead of the dict)."""
        job_id = "job-dir-gone"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (
                job_id,
                "/nonexistent/output/dir",
                "/nonexistent/output/dir/audiobook.m4b",
            ),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Output directory not found"

    def test_serves_m4b_produced_by_merge_after_finalize(
        self, client, storage, tmp_path
    ):
        """A row completed without an m4b (artifact = output dir) still serves an
        m4b that POST /merge produced later — the m4b-first parity path."""
        output_dir = tmp_path / "out-late-merge"
        output_dir.mkdir()
        m4b_path = output_dir / "audiobook.m4b"
        m4b_path.write_bytes(b"LATE-MERGE-M4B")

        job_id = "job-late-merge"
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, 'b1', 'batch', 'completed', ?, ?)",
            (job_id, str(output_dir), str(output_dir)),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mp4"
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="audiobook.m4b"'
        )
        assert resp.content == b"LATE-MERGE-M4B"


# ---------------------------------------------------------------------------
# GET /api/pipeline/export/chunk/{job_id}/{idx}
# ---------------------------------------------------------------------------


class TestExportChunkBoundedRange:
    """Bounded-range WAV serving for GET /api/pipeline/export/chunk/{job_id}/{idx}.

    Rows = truth: the render_chunk row supplies status and wav_path; the
    wav_path is resolved against the run dir (row's output_dir, or the
    derived RENDER_ROOT/book-{id}/{job_id}/ when NULL) and containment is
    enforced (path traversal -> 404).  A full GET without a Range header is
    never capped; a Range request serves at most PIPELINE_MAX_RANGE_BYTES
    bytes per request (default 4 MiB) by clamping the slice end.
    """

    _WAV = b"RIFF....WAVE...."  # 18 bytes

    def _seed_job_and_chunk(
        self,
        storage,
        job_id,
        idx,
        wav_path,
        *,
        status="done",
        output_dir=None,
        book_id="b1",
    ):
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir) "
            "VALUES (?, ?, 'individual', 'completed', ?)",
            (job_id, book_id, output_dir),
        )
        storage.execute_insert(
            "INSERT INTO render_chunk (job_id, idx, status, wav_path) "
            "VALUES (?, ?, ?, ?)",
            (job_id, idx, status, wav_path),
        )

    def _write_chunk(self, tmp_path, data=_WAV):
        chunk_file = tmp_path / "chunk_0000.wav"
        chunk_file.write_bytes(data)
        return str(chunk_file)

    def test_full_get_without_range_returns_200_audio_wav(
        self, client, storage, tmp_path
    ):
        """A done chunk served without a Range header returns the full body as
        audio/wav (200) — the whole-body path is never capped."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-full", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get("/api/pipeline/export/chunk/job-full/0")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.content == self._WAV

    def test_partial_range_returns_206_with_content_range(
        self, client, storage, tmp_path
    ):
        """A valid byte range returns 206 with Content-Range and the slice only."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-range", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get(
            "/api/pipeline/export/chunk/job-range/0", headers={"Range": "bytes=2-5"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 2-5/{len(self._WAV)}"
        assert resp.headers["content-length"] == "4"
        assert resp.content == self._WAV[2:6]

    def test_open_ended_range_returns_206_rest_of_file(self, client, storage, tmp_path):
        """bytes=<start>- (open-ended) serves from start to EOF as 206."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-open", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get(
            "/api/pipeline/export/chunk/job-open/0", headers={"Range": "bytes=4-"}
        )
        assert resp.status_code == 206
        assert (
            resp.headers["content-range"]
            == f"bytes 4-{len(self._WAV) - 1}/{len(self._WAV)}"
        )
        assert resp.content == self._WAV[4:]

    def test_unsatisfiable_range_returns_416_with_star(self, client, storage, tmp_path):
        """A range starting beyond EOF returns 416 with Content-Range: bytes */N."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-416", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get(
            "/api/pipeline/export/chunk/job-416/0", headers={"Range": "bytes=999999-"}
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"] == f"bytes */{len(self._WAV)}"

    def test_range_end_beyond_eof_clamped_to_eof(self, client, storage, tmp_path):
        """A range whose end exceeds the file size is clamped to EOF (206)."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-clamp-eof", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get(
            "/api/pipeline/export/chunk/job-clamp-eof/0",
            headers={"Range": "bytes=2-99999"},
        )
        assert resp.status_code == 206
        assert (
            resp.headers["content-range"]
            == f"bytes 2-{len(self._WAV) - 1}/{len(self._WAV)}"
        )
        assert resp.content == self._WAV[2:]

    def test_malformed_range_returns_400(self, client, storage, tmp_path):
        """A malformed Range header returns 400 — starlette >= 0.49.1 native
        behavior (the plan wording says 416 for malformed; the installed
        starlette 1.3.1 sends 400 for malformed and 416 only for unsatisfiable,
        which is the behavior locked by the >= 0.49.1 pin)."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-mal", 0, wav_path, output_dir=str(tmp_path)
        )

        for bad in ("bytes=abc", "bytes="):
            resp = client.get(
                "/api/pipeline/export/chunk/job-mal/0", headers={"Range": bad}
            )
            assert resp.status_code == 400, f"Range {bad!r} should be 400"

    def test_unknown_job_returns_404(self, client):
        """No render_job row -> 404 with the job_id in the detail."""
        resp = client.get("/api/pipeline/export/chunk/does-not-exist/0")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown job_id: does-not-exist"

    def test_unknown_idx_returns_404(self, client, storage, tmp_path):
        """A job with no chunk row for the idx -> 404."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-nochunk", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get("/api/pipeline/export/chunk/job-nochunk/7")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown chunk idx: 7"

    def test_non_integer_idx_returns_404(self, client, storage, tmp_path):
        """A non-integer idx can name no chunk -> 404 (validation choice, plan
        allows 404 or 400 for non-int / out-of-range idx)."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-alpha", 0, wav_path, output_dir=str(tmp_path)
        )

        for bad in ("abc", "1.5"):
            resp = client.get(f"/api/pipeline/export/chunk/job-alpha/{bad}")
            assert resp.status_code == 404

    def test_negative_idx_returns_404(self, client, storage, tmp_path):
        """A negative idx has no row -> 404."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-neg", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get("/api/pipeline/export/chunk/job-neg/-1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown chunk idx: -1"

    def test_pending_chunk_returns_409(self, client, storage, tmp_path):
        """A pending chunk row is not servable yet -> 409 Conflict."""
        self._seed_job_and_chunk(
            storage,
            "job-pending",
            0,
            "ignored.wav",
            status="pending",
            output_dir=str(tmp_path),
        )
        resp = client.get("/api/pipeline/export/chunk/job-pending/0")
        assert resp.status_code == 409
        assert "pending" in resp.json()["detail"]

    def test_failed_chunk_returns_409(self, client, storage, tmp_path):
        """A failed chunk row is permanently unservable -> 409 Conflict."""
        self._seed_job_and_chunk(
            storage,
            "job-failed",
            0,
            "ignored.wav",
            status="failed",
            output_dir=str(tmp_path),
        )
        resp = client.get("/api/pipeline/export/chunk/job-failed/0")
        assert resp.status_code == 409
        assert "failed" in resp.json()["detail"]

    def test_evicted_chunk_returns_410(self, client, storage, tmp_path):
        """An evicted chunk row is a GC tombstone -> 410 Gone (the row exists but
        the artifact was intentionally removed and will not return)."""
        self._seed_job_and_chunk(
            storage,
            "job-evicted",
            0,
            "evicted.wav",
            status="evicted",
            output_dir=str(tmp_path),
        )
        resp = client.get("/api/pipeline/export/chunk/job-evicted/0")
        assert resp.status_code == 410
        assert "evicted" in resp.json()["detail"].lower()

    def test_range_slice_capped_to_env_limit(
        self, client, storage, tmp_path, monkeypatch
    ):
        """A Range request never streams more than PIPELINE_MAX_RANGE_BYTES per
        request: oversized / open-ended ranges are clamped to a cap-sized prefix
        (206 with the clamped Content-Range)."""
        monkeypatch.setenv("PIPELINE_MAX_RANGE_BYTES", "4")
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-cap", 0, wav_path, output_dir=str(tmp_path)
        )

        # Open-ended bytes=0- would stream the whole 18-byte file: clamped to 4.
        resp = client.get(
            "/api/pipeline/export/chunk/job-cap/0", headers={"Range": "bytes=0-"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-3/{len(self._WAV)}"
        assert resp.content == self._WAV[:4]

        # Explicit end beyond the cap: clamped to start + cap - 1.
        resp = client.get(
            "/api/pipeline/export/chunk/job-cap/0", headers={"Range": "bytes=2-17"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 2-5/{len(self._WAV)}"
        assert resp.content == self._WAV[2:6]

    def test_full_get_not_capped_even_when_larger_than_limit(
        self, client, storage, tmp_path, monkeypatch
    ):
        """The cap targets Range requests only: a full GET without Range still
        returns the whole body even when the file exceeds the cap."""
        monkeypatch.setenv("PIPELINE_MAX_RANGE_BYTES", "4")
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-big", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.get("/api/pipeline/export/chunk/job-big/0")
        assert resp.status_code == 200
        assert resp.content == self._WAV

    def test_relative_wav_path_resolved_against_run_dir(
        self, client, storage, tmp_path
    ):
        """A relative wav_path (the manifest form) is resolved against the row's
        output_dir before serving."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "chunk_0000.wav").write_bytes(self._WAV)
        self._seed_job_and_chunk(
            storage, "job-rel", 0, "chunk_0000.wav", output_dir=str(run_dir)
        )

        resp = client.get("/api/pipeline/export/chunk/job-rel/0")
        assert resp.status_code == 200
        assert resp.content == self._WAV

    def test_derived_run_dir_when_output_dir_null(
        self, client, storage, tmp_path, monkeypatch
    ):
        """With output_dir NULL the run dir is derived as
        RENDER_ROOT/book-{id}/{job_id}/ (phase 1 layout) and the stored
        wav_path is resolved against it."""
        render_root = tmp_path / "render_root"
        run_dir = render_root / "book-b1" / "job-derived"
        run_dir.mkdir(parents=True)
        (run_dir / "chunk_0000.wav").write_bytes(self._WAV)
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        self._seed_job_and_chunk(
            storage, "job-derived", 0, str(run_dir / "chunk_0000.wav"), output_dir=None
        )

        resp = client.get("/api/pipeline/export/chunk/job-derived/0")
        assert resp.status_code == 200
        assert resp.content == self._WAV

    def test_wav_path_escaping_run_dir_returns_404(self, client, storage, tmp_path):
        """Path traversal: a wav_path that resolves outside the run dir (relative
        '..' escape or absolute path elsewhere) is rejected with 404."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        outside = tmp_path / "outside.wav"
        outside.write_bytes(self._WAV)
        self._seed_job_and_chunk(
            storage, "job-trav", 0, "../../outside.wav", output_dir=str(run_dir)
        )
        self._seed_job_and_chunk(
            storage, "job-trav-abs", 1, str(outside), output_dir=str(run_dir)
        )

        for job_id, idx in (("job-trav", "0"), ("job-trav-abs", "1")):
            resp = client.get(f"/api/pipeline/export/chunk/{job_id}/{idx}")
            assert resp.status_code == 404, (job_id, idx)
            assert resp.headers.get("content-type", "").startswith("application/json")

    def test_missing_wav_file_returns_404(self, client, storage, tmp_path):
        """A done row whose WAV file has vanished -> 404 JSON (FileResponse404
        pattern), not a broken 200 or a 500."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        wav_path = str(run_dir / "chunk_0000.wav")  # never created
        self._seed_job_and_chunk(
            storage, "job-gone", 0, wav_path, output_dir=str(run_dir)
        )

        resp = client.get("/api/pipeline/export/chunk/job-gone/0")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Chunk WAV not found"

    def test_head_returns_headers_without_body(self, client, storage, tmp_path):
        """HEAD is served trivially (Starlette auto-adds HEAD to GET routes and
        FileResponse sends headers only)."""
        wav_path = self._write_chunk(tmp_path)
        self._seed_job_and_chunk(
            storage, "job-head", 0, wav_path, output_dir=str(tmp_path)
        )

        resp = client.head("/api/pipeline/export/chunk/job-head/0")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == str(len(self._WAV))
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == b""


# ---------------------------------------------------------------------------
# GET /api/pipeline/export/audio/{job_id}
# ---------------------------------------------------------------------------


def _make_wav(
    data: bytes, sample_rate: int = 16000, channels: int = 1, bits: int = 16
) -> bytes:
    """Build a minimal 44-byte-header PCM WAV carrying *data* as its payload.

    The synthesized whole-book body for concatenated chunks is byte-identical
    to ``_make_wav(chunk0_data + chunk1_data + ...)`` — the endpoint patches
    exactly the RIFF size + data-size fields a fresh header would carry.
    """
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        len(data),
    )
    assert len(header) == 44
    return header + data


class TestExportAudioWholeBook:
    """GET /api/pipeline/export/audio/{job_id} — whole-book playback.

    Rows = truth: the render_job row decides what is served.  A completed job
    whose output_artifact_path names a file serves that artifact (media type
    from the extension) with Range support.  A completed job whose artifact is
    the run dir (individual mode without an m4b) serves a synthesized
    whole-book WAV streamed chunk-by-chunk from disk with Range computed
    across chunk boundaries.  Batch-mode jobs (no chunk rows) synthesize from
    the sorted *.wav files in the run dir.  Unknown jobs -> 404, non-completed
    jobs -> 404 'Job not completed (status: X)', expired jobs (GC tombstone)
    -> 410 Gone.  Playback is inline (no Content-Disposition).
    """

    def _seed_job(
        self,
        storage,
        job_id,
        *,
        mode="individual",
        status="completed",
        output_dir=None,
        artifact=None,
        book_id="b1",
    ):
        storage.execute_insert(
            "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
            "output_artifact_path) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, book_id, mode, status, output_dir, artifact),
        )

    def _seed_chunk(self, storage, job_id, idx, wav_path, status="done"):
        storage.execute_insert(
            "INSERT INTO render_chunk (job_id, idx, status, wav_path) "
            "VALUES (?, ?, ?, ?)",
            (job_id, idx, status, wav_path),
        )

    def _seed_individual_render(self, storage, job_id, run_dir, datas):
        """Insert a completed individual-mode job + one done chunk per payload."""
        paths = []
        for i, data in enumerate(datas):
            path = run_dir / f"chunk_{i:04d}.wav"
            path.write_bytes(_make_wav(data))
            paths.append(str(path))
        self._seed_job(
            storage,
            job_id,
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )
        for i, path in enumerate(paths):
            self._seed_chunk(storage, job_id, i, path)

    # -- artifact serving ---------------------------------------------------

    def test_artifact_m4b_full_get_200(self, client, storage, tmp_path):
        """A completed job whose output_artifact_path is an m4b file serves it as
        audio/mp4 with the exact bytes, accept-ranges, and inline playback (no
        Content-Disposition)."""
        out = tmp_path / "out"
        out.mkdir()
        m4b = out / "audiobook.m4b"
        m4b.write_bytes(b"M4B-DATA-0123456789")
        self._seed_job(
            storage,
            "job-art",
            mode="batch",
            status="completed",
            output_dir=str(out),
            artifact=str(m4b),
        )

        resp = client.get("/api/pipeline/export/audio/job-art")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mp4"
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.content == b"M4B-DATA-0123456789"
        assert "content-disposition" not in resp.headers

    def test_artifact_m4b_range_206(self, client, storage, tmp_path):
        """A byte range on the artifact returns 206 + Content-Range + the slice."""
        out = tmp_path / "out"
        out.mkdir()
        m4b = out / "audiobook.m4b"
        m4b.write_bytes(b"M4B-DATA-0123456789")
        self._seed_job(
            storage,
            "job-art-range",
            mode="batch",
            status="completed",
            output_dir=str(out),
            artifact=str(m4b),
        )

        resp = client.get(
            "/api/pipeline/export/audio/job-art-range", headers={"Range": "bytes=2-9"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 2-9/19"
        assert resp.content == b"B-DATA-0"

    def test_artifact_media_type_from_extension(self, client, storage, tmp_path):
        """The media type is picked from the artifact file's extension (.mp3)."""
        out = tmp_path / "out"
        out.mkdir()
        mp3 = out / "audiobook.mp3"
        mp3.write_bytes(b"MP3-FAKE")
        self._seed_job(
            storage,
            "job-mp3",
            mode="batch",
            status="completed",
            output_dir=str(out),
            artifact=str(mp3),
        )

        resp = client.get("/api/pipeline/export/audio/job-mp3")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.content == b"MP3-FAKE"

    def test_artifact_head_headers_only(self, client, storage, tmp_path):
        """HEAD on the artifact returns headers (Content-Length) without a body."""
        out = tmp_path / "out"
        out.mkdir()
        m4b = out / "audiobook.m4b"
        m4b.write_bytes(b"M4B-DATA-0123456789")
        self._seed_job(
            storage,
            "job-art-head",
            mode="batch",
            status="completed",
            output_dir=str(out),
            artifact=str(m4b),
        )

        resp = client.head("/api/pipeline/export/audio/job-art-head")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "19"
        assert resp.headers["content-type"] == "audio/mp4"
        assert resp.content == b""

    def test_artifact_missing_file_404_json(self, client, storage, tmp_path):
        """A completed row whose recorded artifact FILE is missing -> 404 JSON
        (FileResponse404 pattern), not a broken 200 or a 500."""
        out = tmp_path / "out"
        out.mkdir()
        missing = out / "audiobook.m4b"  # never created
        self._seed_job(
            storage,
            "job-art-gone",
            mode="batch",
            status="completed",
            output_dir=str(out),
            artifact=str(missing),
        )

        resp = client.get("/api/pipeline/export/audio/job-art-gone")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Audio file not found"

    def test_late_merge_m4b_parity(self, client, storage, tmp_path):
        """A row completed with artifact = run dir still serves an m4b that POST
        /merge produced later (parity with download_render)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "audiobook.m4b").write_bytes(b"LATE-M4B")
        self._seed_individual_render(storage, "job-parity", run_dir, [b"0123456789"])

        resp = client.get("/api/pipeline/export/audio/job-parity")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mp4"
        assert resp.content == b"LATE-M4B"

    # -- job state -----------------------------------------------------------

    def test_unknown_job_404(self, client):
        """No render_job row -> 404 with the job_id in the detail."""
        resp = client.get("/api/pipeline/export/audio/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown job_id: does-not-exist"

    def test_expired_job_410(self, client, storage):
        """An expired job (GC tombstone — artifacts intentionally gone) -> 410."""
        self._seed_job(storage, "job-expired", status="expired", output_dir="/gone")
        resp = client.get("/api/pipeline/export/audio/job-expired")
        assert resp.status_code == 410
        assert "expired" in resp.json()["detail"].lower()

    def test_running_job_404_not_completed(self, client, storage):
        """A non-completed job -> 404 'Job not completed (status: X)' (the
        download_render pattern)."""
        self._seed_job(storage, "job-running", status="running", output_dir="/tmp/out")
        resp = client.get("/api/pipeline/export/audio/job-running")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not completed (status: running)"

    def test_failed_job_404_not_completed(self, client, storage):
        """A failed job is not servable -> 404 like download_render."""
        self._seed_job(storage, "job-failed", status="failed", output_dir="/tmp/out")
        resp = client.get("/api/pipeline/export/audio/job-failed")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not completed (status: failed)"

    # -- synthesized whole-book WAV ------------------------------------------

    def test_synthesized_individual_full_get_200(self, client, storage, tmp_path):
        """A completed individual-mode job with no m4b serves a whole-book WAV:
        chunk 0's header with patched sizes + every chunk's PCM payload."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"
        self._seed_individual_render(storage, "job-synth", run_dir, [data0, data1])

        resp = client.get("/api/pipeline/export/audio/job-synth")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.headers["accept-ranges"] == "bytes"
        expected = _make_wav(data0 + data1)
        assert resp.headers["content-length"] == str(len(expected))
        assert resp.content == expected

    def test_synthesized_range_across_chunk_boundary(self, client, storage, tmp_path):
        """A Range spanning the chunk boundary returns a 206 slice drawn from both
        chunks (offsets computed across the virtual stream)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"  # 44-byte header + 20 data = 64
        self._seed_individual_render(storage, "job-cross", run_dir, [data0, data1])

        resp = client.get(
            "/api/pipeline/export/audio/job-cross", headers={"Range": "bytes=50-59"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 50-59/64"
        assert resp.content == b"6789abcdef"

    def test_synthesized_range_inside_second_chunk(self, client, storage, tmp_path):
        """A Range wholly inside the second chunk's data section."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"
        self._seed_individual_render(storage, "job-chunk1", run_dir, [data0, data1])

        resp = client.get(
            "/api/pipeline/export/audio/job-chunk1", headers={"Range": "bytes=54-59"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 54-59/64"
        assert resp.content == b"abcdef"

    def test_synthesized_open_ended_range(self, client, storage, tmp_path):
        """bytes=<start>- streams from start to EOF as 206."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"
        self._seed_individual_render(storage, "job-open", run_dir, [data0, data1])

        resp = client.get(
            "/api/pipeline/export/audio/job-open", headers={"Range": "bytes=44-"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 44-63/64"
        assert resp.content == data0 + data1

    def test_synthesized_unsatisfiable_416(self, client, storage, tmp_path):
        """A range starting beyond the virtual stream end -> 416 with
        Content-Range: bytes */total (starlette >= 0.49.1 semantics)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"
        self._seed_individual_render(storage, "job-416", run_dir, [data0, data1])

        resp = client.get(
            "/api/pipeline/export/audio/job-416", headers={"Range": "bytes=999999-"}
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"] == "bytes */64"

    def test_synthesized_malformed_range_400(self, client, storage, tmp_path):
        """A malformed Range header -> 400 (starlette native semantics)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._seed_individual_render(storage, "job-mal", run_dir, [b"0123456789"])

        for bad in ("bytes=abc", "bytes="):
            resp = client.get(
                "/api/pipeline/export/audio/job-mal", headers={"Range": bad}
            )
            assert resp.status_code == 400, f"Range {bad!r} should be 400"

    def test_synthesized_range_capped(self, client, storage, tmp_path, monkeypatch):
        """Synthesized Range requests are capped per request to
        PIPELINE_MAX_RANGE_BYTES (default 4 MiB), like the phase-4 chunk cap."""
        monkeypatch.setenv("PIPELINE_MAX_RANGE_BYTES", "4")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"
        self._seed_individual_render(storage, "job-cap", run_dir, [data0, data1])
        expected = _make_wav(data0 + data1)

        resp = client.get(
            "/api/pipeline/export/audio/job-cap", headers={"Range": "bytes=0-"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 0-3/64"
        assert resp.content == expected[:4]

        resp = client.get(
            "/api/pipeline/export/audio/job-cap", headers={"Range": "bytes=2-17"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 2-5/64"
        assert resp.content == expected[2:6]

    def test_synthesized_head_headers_only(self, client, storage, tmp_path):
        """HEAD on the synthesized form returns Content-Length (full virtual
        size) without a body."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data0, data1 = b"0123456789", b"abcdefghij"
        self._seed_individual_render(storage, "job-head", run_dir, [data0, data1])

        resp = client.head("/api/pipeline/export/audio/job-head")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "64"
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == b""

    def test_batch_mode_whole_book_from_dir_enumeration(
        self, client, storage, tmp_path
    ):
        """Batch mode has no chunk rows (by contract): the whole-book WAV is
        synthesized from the sorted *.wav files in the run dir."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "temp_batch_0.wav").write_bytes(_make_wav(b"AAAA"))
        (run_dir / "temp_batch_1.wav").write_bytes(_make_wav(b"BBBB"))
        self._seed_job(
            storage,
            "job-batch",
            mode="batch",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )

        resp = client.get("/api/pipeline/export/audio/job-batch")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == _make_wav(b"AAAA" + b"BBBB")

    def test_batch_mode_whole_book_natural_chunk_order(self, client, storage, tmp_path):
        """Batch chunks remain in numeric order once the batch has 10+ files."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        for index in (10, 2, 0):
            (run_dir / f"temp_batch_{index}.wav").write_bytes(
                _make_wav(str(index).encode())
            )
        self._seed_job(
            storage,
            "job-batch-natural",
            mode="batch",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )

        resp = client.get("/api/pipeline/export/audio/job-batch-natural")
        assert resp.status_code == 200
        assert resp.content == _make_wav(b"02" + b"10")

    def test_batch_no_wavs_404(self, client, storage, tmp_path):
        """A completed batch job whose run dir holds no WAVs -> 404."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._seed_job(
            storage,
            "job-empty",
            mode="batch",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )

        resp = client.get("/api/pipeline/export/audio/job-empty")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No audio files found in output directory"

    def test_individual_evicted_chunk_410(self, client, storage, tmp_path):
        """A completed job with an evicted chunk row (GC tombstone) -> 410."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        chunk0 = run_dir / "chunk_0000.wav"
        chunk0.write_bytes(_make_wav(b"0123456789"))
        self._seed_job(
            storage,
            "job-evict",
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )
        self._seed_chunk(storage, "job-evict", 0, str(chunk0))
        self._seed_chunk(storage, "job-evict", 1, "gone.wav", status="evicted")

        resp = client.get("/api/pipeline/export/audio/job-evict")
        assert resp.status_code == 410
        assert "evicted" in resp.json()["detail"].lower()

    def test_individual_pending_chunk_409(self, client, storage, tmp_path):
        """A completed job with a pending chunk row is inconsistent -> 409."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        chunk0 = run_dir / "chunk_0000.wav"
        chunk0.write_bytes(_make_wav(b"0123456789"))
        self._seed_job(
            storage,
            "job-pend",
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )
        self._seed_chunk(storage, "job-pend", 0, str(chunk0))
        self._seed_chunk(storage, "job-pend", 1, "ignored.wav", status="pending")

        resp = client.get("/api/pipeline/export/audio/job-pend")
        assert resp.status_code == 409
        assert "pending" in resp.json()["detail"]

    def test_individual_missing_chunk_file_404(self, client, storage, tmp_path):
        """A done chunk row whose WAV file has vanished -> 404 JSON (incomplete
        audio is never served)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._seed_job(
            storage,
            "job-gone",
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )
        # Row says done; the file was never written.
        self._seed_chunk(storage, "job-gone", 0, str(run_dir / "chunk_0000.wav"))

        resp = client.get("/api/pipeline/export/audio/job-gone")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Audio file not found"

    def test_individual_no_chunk_rows_404(self, client, storage, tmp_path):
        """A completed individual job with no chunk rows -> 404."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._seed_job(
            storage,
            "job-norows",
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )

        resp = client.get("/api/pipeline/export/audio/job-norows")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No audio chunks found for job"

    def test_derived_run_dir_when_output_dir_null(
        self, client, storage, tmp_path, monkeypatch
    ):
        """With output_dir NULL the run dir is derived as
        RENDER_ROOT/book-{id}/{job_id}/ and chunk wav_paths resolve against it."""
        render_root = tmp_path / "render_root"
        run_dir = render_root / "book-b1" / "job-derived"
        run_dir.mkdir(parents=True)
        chunk = run_dir / "chunk_0000.wav"
        chunk.write_bytes(_make_wav(b"0123456789"))
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        self._seed_job(storage, "job-derived", output_dir=None, artifact=None)
        self._seed_chunk(storage, "job-derived", 0, str(chunk))

        resp = client.get("/api/pipeline/export/audio/job-derived")
        assert resp.status_code == 200
        assert resp.content == _make_wav(b"0123456789")

    def test_path_traversal_wav_path_404(self, client, storage, tmp_path):
        """Path traversal: a chunk wav_path resolving outside the run dir
        (relative '..' escape) is rejected with a JSON 404."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        outside = tmp_path / "outside.wav"
        outside.write_bytes(_make_wav(b"0123456789"))
        self._seed_job(
            storage,
            "job-trav",
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )
        self._seed_chunk(storage, "job-trav", 0, "../../outside.wav")

        resp = client.get("/api/pipeline/export/audio/job-trav")
        assert resp.status_code == 404
        assert resp.headers.get("content-type", "").startswith("application/json")

    def test_format_mismatch_409(self, client, storage, tmp_path):
        """Chunks whose formats disagree (different sample rates) cannot be
        concatenated without a resampler (no ffmpeg in this phase) -> 409,
        documented limitation."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        chunk0 = run_dir / "chunk_0000.wav"
        chunk0.write_bytes(_make_wav(b"0123456789", sample_rate=16000))
        chunk1 = run_dir / "chunk_0001.wav"
        chunk1.write_bytes(_make_wav(b"abcdefghij", sample_rate=8000))
        self._seed_job(
            storage,
            "job-fmt",
            mode="individual",
            status="completed",
            output_dir=str(run_dir),
            artifact=str(run_dir),
        )
        self._seed_chunk(storage, "job-fmt", 0, str(chunk0))
        self._seed_chunk(storage, "job-fmt", 1, str(chunk1))

        resp = client.get("/api/pipeline/export/audio/job-fmt")
        assert resp.status_code == 409
        assert "differ" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/pipeline/export/m4b — 3-phase FFMETADATA1 polished export
# ---------------------------------------------------------------------------

# 0.5 s of silence at 16 kHz mono 16-bit: 8000 samples * 2 bytes.
_CHUNK_PAYLOAD = b"\x00\x00" * 8000
_CHUNK_DURATION_MS = 500


def _seed_job_row(
    storage,
    job_id,
    *,
    book_id="b1",
    mode="individual",
    status="completed",
    output_dir=None,
    artifact=None,
):
    """Insert a render_job row with explicit fields (rows = truth)."""
    storage.execute_insert(
        "INSERT INTO render_job (job_id, book_id, mode, status, output_dir, "
        "output_artifact_path) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, book_id, mode, status, output_dir, artifact),
    )


def _seed_chunk_row(storage, job_id, idx, wav_path, status="done"):
    """Insert a render_chunk row."""
    storage.execute_insert(
        "INSERT INTO render_chunk (job_id, idx, status, wav_path) VALUES (?, ?, ?, ?)",
        (job_id, idx, status, wav_path),
    )


def _seed_two_chunk_job(
    storage,
    job_id,
    run_dir,
    *,
    mode="individual",
    status="completed",
    output_dir=None,
    artifact=None,
    book_id="b1",
    seed_rows=True,
):
    """Completed job with two real 0.5 s chunk WAVs on disk.

    Individual mode seeds one done render_chunk row per WAV (rows = truth);
    batch mode leaves the rows out and relies on run-dir *.wav enumeration.
    Pass ``seed_rows=False`` to write the WAVs and job row only (caller seeds
    chunk rows with custom statuses).  Returns the absolute wav paths.
    """
    paths = []
    for i in range(2):
        path = run_dir / f"chunk_{i:04d}.wav"
        path.write_bytes(_make_wav(_CHUNK_PAYLOAD))
        paths.append(str(path))
    _seed_job_row(
        storage,
        job_id,
        book_id=book_id,
        mode=mode,
        status=status,
        output_dir=output_dir or str(run_dir),
        artifact=artifact or str(run_dir),
    )
    if mode == "individual" and seed_rows:
        for i, path in enumerate(paths):
            _seed_chunk_row(storage, job_id, i, path)
    return paths


def _wav_duration_ms(path):
    """Duration of a _make_wav PCM file in integer ms (header-derived, exactly
    the math ffprobe reports for WAV: data bytes / byte_rate)."""
    with open(path, "rb") as f:
        header = f.read(44)
    assert len(header) == 44
    byte_rate = struct.unpack("<I", header[28:32])[0]
    data_size = struct.unpack("<I", header[40:44])[0]
    assert byte_rate > 0
    return round(data_size / byte_rate * 1000)


def _ffprobe_json(*args):
    """Run ffprobe against the real binary (available in CI) and return JSON."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _parse_ffmetadata(path):
    """Return (header_line, chapters, tags) from an FFMETADATA1 file.

    chapters is a list of dicts with keys TIMEBASE/START/END/title;
    tags is a dict of the global (non-chapter) key=value entries.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0] if lines else ""
    chapters = []
    tags = {}
    current = None
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        if line == "[CHAPTER]":
            chapters.append({})
            current = chapters[-1]
        elif current is not None:
            key, _, value = line.partition("=")
            current[key] = value
        else:
            key, _, value = line.partition("=")
            tags[key] = value
    return header, chapters, tags


class TestExportM4B:
    """POST /api/pipeline/export/m4b — 3-phase FFMETADATA1 polished export.

    Concat (ffmpeg concat demuxer over the chunk WAVs in order) -> metadata
    (audiobook.ffmetadata with global tags + one chapter per chunk, TIMEBASE
    1/1000, integer-ms START/END, last END clamped to the single-ffprobe
    duration) -> mux (m4b + optional attached_pic cover + optional libmp3lame
    mp3 + always-producible ZIP_STORED audacity bundle).  The ffmetadata file
    is CI-validated: TIMEBASE=1/1000, integer milliseconds, END clamp.
    """

    def _post(self, client, job_id, **fields):
        return client.post(
            "/api/pipeline/export/m4b", data={"job_id": job_id, **fields}
        )

    def _post_with_cover(
        self, client, job_id, cover_bytes, content_type="image/jpeg", **fields
    ):
        return client.post(
            "/api/pipeline/export/m4b",
            data={"job_id": job_id, **fields},
            files={"cover": ("cover.jpg", cover_bytes, content_type)},
        )

    def _export(self, client, storage, job_id, run_dir, **fields):
        """Seed a 2-chunk individual job and run a full export."""
        _seed_two_chunk_job(storage, job_id, run_dir)
        return self._post(client, job_id, **fields)

    # -- happy path ---------------------------------------------------------

    def test_m4b_export_full_success(self, client, storage, tmp_path):
        """2-chunk job with full metadata -> 200 with m4b, mp3 and audacity
        artifacts; response carries the artifact paths and flags."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        resp = self._export(
            client,
            storage,
            "job-ok",
            run_dir,
            title="The Title",
            author="The Author",
            narrator="The Narrator",
            year="2024",
            description="The description.",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["output_path"].endswith("audiobook.m4b")
        assert os.path.isfile(body["output_path"])
        assert body["mp3"] is True
        assert body["mp3_path"].endswith("audiobook.mp3")
        assert os.path.isfile(body["mp3_path"])
        assert body["audacity"] is True
        assert body["audacity_path"].endswith("audiobook-audacity.zip")
        assert os.path.isfile(body["audacity_path"])
        # The m4b is a real mp4/aac container with exactly two chapters.
        probe = _ffprobe_json("-show_chapters", body["output_path"])
        assert len(probe["chapters"]) == 2

    def test_pauses_truthful_fallback_when_no_paused_artifact(
        self, client, storage, tmp_path
    ):
        """Without a canonical paused artifact (a render that completed without
        assembly, e.g. a fake engine) the export falls back to the per-chunk
        concat so legacy rows still export, and the tri-state truthfully reports
        pauses_applied=false / pauses_state='failed' with a bounded pauses_error
        (no filesystem paths) and NO pauses_message (Plan L, superseding the
        Plan K disclosure)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        resp = self._export(client, storage, "job-pauses", run_dir, title="T")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pauses_applied"] is False
        assert body["pauses_state"] == "failed"
        assert body["pauses_error"]  # bounded detail
        # No filesystem-path leakage in the failure detail.
        assert "/" not in body["pauses_error"] and "\\" not in body["pauses_error"]
        assert "pauses_message" not in body
        # Resolved pause metadata is always present.
        assert "resolved_pause_between_speakers_ms" in body
        assert "resolved_pause_same_speaker_ms" in body
        assert "pause_override_count" in body

    def test_paused_artifact_used_truthful_and_duration_matches(
        self, client, storage, tmp_path
    ):
        """When the canonical paused artifact is present it is the export source:
        pauses_applied=true / pauses_state='applied' with a concise message, and
        the decoded m4b duration equals the paused artifact duration (not the
        shorter per-chunk concat) — proving the gaps ride in the output and the
        unpaused chunks are never exported as the whole-book artifact (P4-S3)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-paused", run_dir)
        # 2 x 0.5 s chunks concat to 1.0 s; the paused artifact is 1.5 s — a
        # clearly distinct duration so the export source is provable.
        paused_path = run_dir / "audiobook-paused.wav"
        paused_path.write_bytes(_make_wav(b"\x00\x00" * 24000))  # 1.5 s
        resp = self._post(client, "job-paused", title="T")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pauses_applied"] is True
        assert body["pauses_state"] == "applied"
        assert body["pauses_error"] is None
        assert "pauses_message" in body
        paused_ms = _wav_duration_ms(str(paused_path))
        # m4b duration == paused artifact duration (within encoder tolerance).
        m4b_ms = round(
            float(
                _ffprobe_json("-show_entries", "format=duration", body["output_path"])[
                    "format"
                ]["duration"]
            )
            * 1000
        )
        assert m4b_ms == pytest.approx(paused_ms, abs=250), (m4b_ms, paused_ms)
        # The mp3 derives from the same paused source.
        mp3_ms = round(
            float(
                _ffprobe_json("-show_entries", "format=duration", body["mp3_path"])[
                    "format"
                ]["duration"]
            )
            * 1000
        )
        assert mp3_ms == pytest.approx(paused_ms, abs=250), (mp3_ms, paused_ms)
        # The Audacity bundle carries the paused source, never the unpaused chunks.
        with zipfile.ZipFile(body["audacity_path"]) as zf:
            names = zf.namelist()
        assert "audiobook-paused.wav" in names
        assert not any(n.startswith("chunk_") for n in names)
        # Single source -> a single whole-book chapter.
        probe = _ffprobe_json("-show_chapters", body["output_path"])
        assert len(probe["chapters"]) == 1

    def test_paused_artifact_used_batch_mode(self, client, storage, tmp_path):
        """Batch-mode export with the paused artifact present uses it as the
        single source — the batch *.wav enumeration never double-counts it
        (P4-S3, batch side of the stale-manifest exclusion)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-bp", run_dir, mode="batch")
        paused_path = run_dir / "audiobook-paused.wav"
        paused_path.write_bytes(_make_wav(b"\x00\x00" * 24000))  # 1.5 s
        resp = self._post(client, "job-bp")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pauses_applied"] is True
        paused_ms = _wav_duration_ms(str(paused_path))
        m4b_ms = round(
            float(
                _ffprobe_json("-show_entries", "format=duration", body["output_path"])[
                    "format"
                ]["duration"]
            )
            * 1000
        )
        assert m4b_ms == pytest.approx(paused_ms, abs=250), (m4b_ms, paused_ms)
        with zipfile.ZipFile(body["audacity_path"]) as zf:
            names = zf.namelist()
        assert "audiobook-paused.wav" in names
        assert not any(n.startswith("chunk_") for n in names)

    def test_malformed_paused_artifact_falls_back(self, client, storage, tmp_path):
        """A malformed (non-WAV) paused artifact is treated as absent: the export
        falls back to the per-chunk concat without crashing and the tri-state
        reports failed (failed-pydub-assembly negative space)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-mal", run_dir)
        (run_dir / "audiobook-paused.wav").write_bytes(b"not a wav")
        resp = self._post(client, "job-mal")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pauses_applied"] is False
        assert body["pauses_state"] == "failed"
        # The unpaused per-chunk concat (2 x 0.5 s = 1.0 s) was still exported.
        m4b_ms = round(
            float(
                _ffprobe_json("-show_entries", "format=duration", body["output_path"])[
                    "format"
                ]["duration"]
            )
            * 1000
        )
        assert m4b_ms == pytest.approx(1000, abs=250), m4b_ms

    def test_repeated_export_idempotent(self, client, storage, tmp_path):
        """Exporting the same completed job twice succeeds both times; the row
        stays completed and output_artifact_path is refreshed (P4-S4 repeated
        rerender)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-again", run_dir)
        r1 = self._post(client, "job-again")
        r2 = self._post(client, "job-again")
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r2.json()["output_path"] == r1.json()["output_path"]

    def test_cancelled_job_404(self, client, storage, tmp_path):
        """A cancelled render is not completed -> the export returns 404
        (P4-S4 cancelled-render negative space)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-cancel", run_dir, status="cancelled")
        resp = self._post(client, "job-cancel")
        assert resp.status_code == 404

    def test_export_audio_serves_paused_artifact(self, client, storage, tmp_path):
        """GET /export/audio/{job_id} serves the canonical paused artifact
        (output_artifact_path) as a range-served WAV — the /export/audio whole-
        book surface consumes the paused source (P4-S1)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        paused_path = run_dir / "audiobook-paused.wav"
        paused_path.write_bytes(_make_wav(b"\x00\x00" * 8000))  # 0.5 s
        _seed_two_chunk_job(storage, "job-srv", run_dir, artifact=str(paused_path))
        resp = client.get("/api/pipeline/export/audio/job-srv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        rng = client.get(
            "/api/pipeline/export/audio/job-srv", headers={"Range": "bytes=0-9"}
        )
        assert rng.status_code == 206

    def test_output_artifact_path_updated_and_servable(self, client, storage, tmp_path):
        """The render_job row's output_artifact_path is updated to the m4b and
        the m4b becomes servable through the phase-5 whole-book artifact path
        as audio/mp4 (rows = truth)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-serv", run_dir)
        resp = self._post(client, "job-serv", title="T")
        assert resp.status_code == 200, resp.text
        rows = storage.execute_query(
            "SELECT output_artifact_path FROM render_job WHERE job_id = ?",
            ("job-serv",),
        )
        assert rows[0]["output_artifact_path"].endswith("audiobook.m4b")
        assert os.path.isfile(rows[0]["output_artifact_path"])

        audio = client.get("/api/pipeline/export/audio/job-serv")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/mp4"
        assert len(audio.content) > 1000

    def test_ffmetadata_ci_validation(self, client, storage, tmp_path):
        """CI-validated ffmetadata on the 2-chunk fixture: header, TIMEBASE
        1/1000, integer-ms START/END, per-chunk chapter boundaries and the
        last chapter's END clamped to the concatenated duration (never
        exceeding the real stream duration)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        paths = _seed_two_chunk_job(storage, "job-ci", run_dir)
        resp = self._post(client, "job-ci")
        assert resp.status_code == 200, resp.text

        header, chapters, _ = _parse_ffmetadata(run_dir / "audiobook.ffmetadata")
        assert header == ";FFMETADATA1"
        assert len(chapters) == 2  # one chapter per chunk
        for chapter in chapters:
            assert chapter["TIMEBASE"] == "1/1000"
            # integer milliseconds: digits round-trip through int()
            assert chapter["START"].isdigit()
            assert chapter["END"].isdigit()
            int(chapter["START"])
            int(chapter["END"])
        # Boundaries follow the chunk durations (2 x 0.5 s).
        assert chapters[0]["START"] == "0"
        assert chapters[0]["END"] == "500"
        assert chapters[1]["START"] == "500"
        # The last chapter's END is clamped to the concatenated duration
        # (the same math the single ffprobe pass reports for WAV input).
        expected_total = sum(_wav_duration_ms(p) for p in paths)
        assert expected_total == 1000
        assert chapters[1]["END"] == str(expected_total)
        # Never exceeds the real muxed stream duration either.
        m4b_probe = _ffprobe_json(
            "-show_entries", "format=duration", str(run_dir / "audiobook.m4b")
        )
        m4b_duration_ms = round(float(m4b_probe["format"]["duration"]) * 1000)
        assert int(chapters[1]["END"]) <= m4b_duration_ms

    def test_ffmetadata_global_tags(self, client, storage, tmp_path):
        """title/author/narrator/year/description land in the ffmetadata global
        section (title/artist/album_artist/date/comment) and round-trip into
        the muxed m4b tags."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        resp = self._export(
            client,
            storage,
            "job-tags",
            run_dir,
            title="The Title",
            author="The Author",
            narrator="The Narrator",
            year="2024",
            description="The description.",
        )
        assert resp.status_code == 200, resp.text
        _, _, tags = _parse_ffmetadata(run_dir / "audiobook.ffmetadata")
        assert tags["title"] == "The Title"
        assert tags["artist"] == "The Author"
        assert tags["album_artist"] == "The Narrator"
        assert tags["date"] == "2024"
        assert tags["comment"] == "The description."

        probe = _ffprobe_json(
            "-show_entries",
            "format_tags=title,artist,album_artist,date,comment",
            str(run_dir / "audiobook.m4b"),
        )
        fmt_tags = probe["format"]["tags"]
        assert fmt_tags["title"] == "The Title"
        assert fmt_tags["artist"] == "The Author"
        assert fmt_tags["album_artist"] == "The Narrator"
        assert fmt_tags["date"] == "2024"
        assert fmt_tags["comment"] == "The description."

    def test_metadata_escaping_roundtrip(self, client, storage, tmp_path):
        """Values containing ffmetadata-special characters (= ; # \\) are
        escaped in the file and round-trip byte-exact through the mux; control
        characters (newline injection) are stripped."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        nasty = "Semi=Colon;Hash#Back\\Slash"
        newline = "Line1\nLine2"
        resp = self._export(
            client,
            storage,
            "job-esc",
            run_dir,
            title=nasty,
            description=newline,
        )
        assert resp.status_code == 200, resp.text
        _, _, tags = _parse_ffmetadata(run_dir / "audiobook.ffmetadata")
        # Same escape order as the generator: backslash first, then the
        # line-structure specials (= ; #).
        expected = (
            nasty.replace("\\", "\\\\")
            .replace("=", "\\=")
            .replace(";", "\\;")
            .replace("#", "\\#")
        )
        assert tags["title"] == expected
        assert "\n" not in tags["comment"]  # newline stripped, not written

        probe = _ffprobe_json(
            "-show_entries",
            "format_tags=title,comment",
            str(run_dir / "audiobook.m4b"),
        )
        fmt_tags = probe["format"]["tags"]
        assert fmt_tags["title"] == nasty
        assert "\n" not in fmt_tags.get("comment", "")

    def test_mp3_produced_with_libmp3lame(self, client, storage, tmp_path):
        """libmp3lame is present in this ffmpeg build (7.1.5) -> a real mp3
        (codec mp3) is produced alongside the m4b."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        resp = self._export(client, storage, "job-mp3", run_dir, title="T")
        assert resp.status_code == 200, resp.text
        assert resp.json()["mp3"] is True
        mp3_path = run_dir / "audiobook.mp3"
        assert mp3_path.is_file()
        probe = _ffprobe_json("-show_entries", "stream=codec_name", str(mp3_path))
        assert probe["streams"][0]["codec_name"] == "mp3"

    def test_mp3_degraded_when_encoder_absent(
        self, client, storage, tmp_path, monkeypatch
    ):
        """When libmp3lame is not available the endpoint degrades to M4B-only:
        mp3 flag False, no mp3 file, no mp3_path, and a clear message — while
        the m4b export still succeeds (DD open item #8)."""
        import app.pipeline.api_export as api_export_module

        monkeypatch.setattr(api_export_module, "_libmp3lame_available", lambda: False)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        resp = self._export(client, storage, "job-deg", run_dir, title="T")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mp3"] is False
        assert body["mp3_path"] is None
        assert body["output_path"].endswith("audiobook.m4b")
        assert os.path.isfile(body["output_path"])
        assert not (run_dir / "audiobook.mp3").exists()
        assert "message" in body and "mp3" in body["message"].lower()
        # The audacity bundle is still produced.
        assert body["audacity"] is True
        assert (run_dir / "audiobook-audacity.zip").is_file()

    def test_audacity_zip_stored_bundle(self, client, storage, tmp_path):
        """audiobook-audacity.zip is ZIP_STORED and contains the per-chunk WAVs
        (byte-exact) plus the mp3 when produced."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        paths = _seed_two_chunk_job(storage, "job-zip", run_dir)
        resp = self._post(client, "job-zip", title="T")
        assert resp.status_code == 200, resp.text
        zip_path = run_dir / "audiobook-audacity.zip"
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            assert infos, "empty bundle"
            assert all(i.compress_type == zipfile.ZIP_STORED for i in infos)
            names = set(zf.namelist())
            for path in paths:
                arcname = os.path.basename(path)
                assert arcname in names
                assert zf.read(arcname) == _make_wav(_CHUNK_PAYLOAD)
            assert "audiobook.mp3" in names

    def test_batch_mode_dir_enumeration(self, client, storage, tmp_path):
        """Batch-mode job (no chunk rows by contract) exports from the sorted
        *.wav files in the run dir; ffmetadata still has one chapter per file."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-batch", run_dir, mode="batch")
        resp = self._post(client, "job-batch", title="T")
        assert resp.status_code == 200, resp.text
        _, chapters, _ = _parse_ffmetadata(run_dir / "audiobook.ffmetadata")
        assert len(chapters) == 2
        assert (run_dir / "audiobook.m4b").is_file()

    def test_artifacts_in_derived_run_dir(self, client, storage, tmp_path, monkeypatch):
        """A row with NULL output_dir resolves its run dir as
        RENDER_ROOT/book-{id}/{job_id} and artifacts land there."""
        root = tmp_path / "render_root"
        monkeypatch.setenv("RENDER_ROOT", str(root))
        run_dir = root / "book-b1" / "job-der"
        run_dir.mkdir(parents=True)
        _seed_two_chunk_job(storage, "job-der", run_dir, output_dir=None, artifact=None)
        resp = self._post(client, "job-der", title="T")
        assert resp.status_code == 200, resp.text
        assert resp.json()["output_path"] == str(run_dir / "audiobook.m4b")
        assert (run_dir / "audiobook.m4b").is_file()

    # -- cover ---------------------------------------------------------------

    @pytest.fixture()
    def cover_jpg(self, tmp_path):
        """A tiny real JPEG generated once per test via ffmpeg."""
        path = tmp_path / "cover.jpg"
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=32x32",
                "-frames:v",
                "1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return path.read_bytes()

    def test_cover_embedded_attached_pic(self, client, storage, tmp_path, cover_jpg):
        """An uploaded cover is embedded as an mjpeg attached_pic stream in the
        m4b (disposition attached_pic = 1)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-cover", run_dir)
        resp = self._post_with_cover(client, "job-cover", cover_jpg, title="T")
        assert resp.status_code == 200, resp.text
        probe = _ffprobe_json(
            "-show_streams",
            "-select_streams",
            "v",
            str(run_dir / "audiobook.m4b"),
        )
        assert len(probe["streams"]) == 1
        stream = probe["streams"][0]
        assert stream["codec_name"] == "mjpeg"
        assert stream["disposition"]["attached_pic"] == 1

    def test_cover_oversize_400(self, client, storage, tmp_path, monkeypatch):
        """Cover uploads over the size cap are rejected (env-tunable cap)."""
        monkeypatch.setenv("PIPELINE_MAX_COVER_BYTES", "8")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-bigcover", run_dir)
        resp = self._post_with_cover(client, "job-bigcover", b"x" * 16, title="T")
        assert resp.status_code == 400

    def test_cover_bad_content_type_400(self, client, storage, tmp_path):
        """A non-image cover upload is rejected with 400."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-textcover", run_dir)
        resp = self._post_with_cover(
            client,
            "job-textcover",
            b"not an image",
            content_type="text/plain",
            title="T",
        )
        assert resp.status_code == 400

    # -- dispatch / input validation ----------------------------------------

    def test_unknown_job_404(self, client):
        resp = self._post(client, "job-missing")
        assert resp.status_code == 404
        assert "Unknown job_id" in resp.json()["detail"]

    def test_expired_job_410(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-exp", run_dir, status="expired")
        resp = self._post(client, "job-exp")
        assert resp.status_code == 410
        assert "expired" in resp.json()["detail"].lower()

    def test_non_completed_job_404(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-run", run_dir, status="running")
        resp = self._post(client, "job-run")
        assert resp.status_code == 404
        assert "not completed" in resp.json()["detail"]

    def test_missing_run_dir_404(self, client, storage, tmp_path):
        """Completed row whose output dir does not exist -> 404 (download_render
        precedent)."""
        _seed_job_row(
            storage,
            "job-nodir",
            output_dir=str(tmp_path / "nowhere"),
            artifact=str(tmp_path / "nowhere"),
        )
        resp = self._post(client, "job-nodir")
        assert resp.status_code == 404

    def test_pending_chunk_409(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        paths = _seed_two_chunk_job(storage, "job-pend", run_dir, seed_rows=False)
        _seed_chunk_row(storage, "job-pend", 0, paths[0], status="done")
        _seed_chunk_row(storage, "job-pend", 1, paths[1], status="pending")
        resp = self._post(client, "job-pend")
        assert resp.status_code == 409
        assert "not servable" in resp.json()["detail"]

    def test_evicted_chunk_410(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        paths = _seed_two_chunk_job(storage, "job-evict", run_dir, seed_rows=False)
        _seed_chunk_row(storage, "job-evict", 0, paths[0], status="done")
        _seed_chunk_row(storage, "job-evict", 1, paths[1], status="evicted")
        resp = self._post(client, "job-evict")
        assert resp.status_code == 410
        assert "evicted" in resp.json()["detail"].lower()

    def test_no_chunks_400(self, client, storage, tmp_path):
        """Completed individual job with zero chunk rows -> 400 (merge_audiobook
        'No audio chunks' precedent)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_job_row(
            storage, "job-0chunk", output_dir=str(run_dir), artifact=str(run_dir)
        )
        resp = self._post(client, "job-0chunk")
        assert resp.status_code == 400
        assert "no audio chunks" in resp.json()["detail"].lower()

    def test_format_mismatch_409(self, client, storage, tmp_path):
        """Chunks with differing sample rates cannot share one concat pipeline
        -> 409, mirroring the phase-5 synthesized-path limitation."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        chunk0 = run_dir / "chunk_0000.wav"
        chunk0.write_bytes(_make_wav(b"0123456789", sample_rate=16000))
        chunk1 = run_dir / "chunk_0001.wav"
        chunk1.write_bytes(_make_wav(b"abcdefghij", sample_rate=8000))
        _seed_job_row(
            storage, "job-fmt", output_dir=str(run_dir), artifact=str(run_dir)
        )
        _seed_chunk_row(storage, "job-fmt", 0, str(chunk0))
        _seed_chunk_row(storage, "job-fmt", 1, str(chunk1))
        resp = self._post(client, "job-fmt")
        assert resp.status_code == 409
        assert "differ" in resp.json()["detail"]

    def test_path_traversal_wav_path_404(self, client, storage, tmp_path):
        """A poisoned chunk wav_path escaping the run dir is never fed to
        ffmpeg -> 404 (containment discipline from phases 4-5)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_job_row(
            storage, "job-escape", output_dir=str(run_dir), artifact=str(run_dir)
        )
        _seed_chunk_row(storage, "job-escape", 0, str(tmp_path / "evil.wav"))
        resp = self._post(client, "job-escape")
        assert resp.status_code == 404

    def test_missing_chunk_file_404(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_job_row(
            storage, "job-gone", output_dir=str(run_dir), artifact=str(run_dir)
        )
        _seed_chunk_row(storage, "job-gone", 0, str(run_dir / "chunk_0000.wav"))
        _seed_chunk_row(storage, "job-gone", 1, str(run_dir / "chunk_0001.wav"))
        resp = self._post(client, "job-gone")
        assert resp.status_code == 404

    def test_ffmpeg_missing_500(self, client, storage, tmp_path, monkeypatch):
        """No ffmpeg binary -> 500 (merge_audiobook precedent), not a crash."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_two_chunk_job(storage, "job-noffmpeg", run_dir)
        monkeypatch.setattr(
            "app.pipeline.api_export.subprocess.run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
        )
        resp = self._post(client, "job-noffmpeg")
        assert resp.status_code == 500
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/pipeline/export/mp3/{job_id} + /export/audacity/{job_id}
# (Plan K parity routes: row-backed MP3 / Audacity bundle artifact serving)
# ---------------------------------------------------------------------------


class TestExportMp3Artifact:
    """GET /api/pipeline/export/mp3/{job_id} — row-backed MP3 artifact serving.

    Rows = truth: the render_job row decides what is served (dispatch mirrors
    download_render / export_m4b — never the in-process _render_jobs dict).
    A completed job whose run dir holds ``audiobook.mp3`` is served as
    audio/mpeg with an attachment Content-Disposition of audiobook.mp3.
    Unknown job -> 404 JSON, expired (GC tombstone) -> 410, non-completed ->
    404, run dir missing -> 404, artifact file missing -> JSON 404 via
    FileResponse404.
    """

    @staticmethod
    def _write_mp3(run_dir):
        (run_dir / "audiobook.mp3").write_bytes(b"MP3-ARTIFACT")

    def test_present_mp3_served_200(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._write_mp3(run_dir)
        _seed_job_row(storage, "job-mp3-ok", output_dir=str(run_dir))
        resp = client.get("/api/pipeline/export/mp3/job-mp3-ok")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="audiobook.mp3"'
        )
        assert resp.content == b"MP3-ARTIFACT"

    def test_unknown_job_404(self, client, storage):
        resp = client.get("/api/pipeline/export/mp3/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown job_id: does-not-exist"

    def test_expired_job_410(self, client, storage):
        _seed_job_row(storage, "job-mp3-expired", status="expired")
        resp = client.get("/api/pipeline/export/mp3/job-mp3-expired")
        assert resp.status_code == 410
        assert "expired" in resp.json()["detail"].lower()

    def test_non_completed_job_404(self, client, storage):
        _seed_job_row(storage, "job-mp3-running", status="running")
        resp = client.get("/api/pipeline/export/mp3/job-mp3-running")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not completed (status: running)"

    def test_missing_run_dir_404(self, client, storage, tmp_path):
        _seed_job_row(storage, "job-mp3-nodir", output_dir=str(tmp_path / "nowhere"))
        resp = client.get("/api/pipeline/export/mp3/job-mp3-nodir")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Output directory not found"

    def test_missing_artifact_404_json(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_job_row(storage, "job-mp3-noartifact", output_dir=str(run_dir))
        resp = client.get("/api/pipeline/export/mp3/job-mp3-noartifact")
        assert resp.status_code == 404
        assert resp.headers.get("content-type", "").startswith("application/json")
        assert resp.json()["detail"] == "File not found"

    def test_derived_run_dir_when_output_dir_null(
        self, client, storage, tmp_path, monkeypatch
    ):
        render_root = tmp_path / "render_root"
        run_dir = render_root / "book-b1" / "job-mp3-derived"
        run_dir.mkdir(parents=True)
        self._write_mp3(run_dir)
        monkeypatch.setenv("RENDER_ROOT", str(render_root))
        _seed_job_row(storage, "job-mp3-derived", output_dir=None)
        resp = client.get("/api/pipeline/export/mp3/job-mp3-derived")
        assert resp.status_code == 200
        assert resp.content == b"MP3-ARTIFACT"


class TestExportAudacityArtifact:
    """GET /api/pipeline/export/audacity/{job_id} — row-backed Audacity bundle.

    Same row-backed dispatch as the mp3 route; a completed job whose run dir
    holds ``audiobook-audacity.zip`` is served as application/zip with an
    attachment Content-Disposition of audiobook-audacity.zip.  Unknown job ->
    404 JSON, expired -> 410, non-completed -> 404, run dir missing -> 404,
    artifact file missing -> JSON 404 via FileResponse404.
    """

    def test_present_zip_served_200(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "audiobook-audacity.zip").write_bytes(b"ZIP-ARTIFACT")
        _seed_job_row(storage, "job-aud-ok", output_dir=str(run_dir))
        resp = client.get("/api/pipeline/export/audacity/job-aud-ok")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="audiobook-audacity.zip"'
        )
        assert resp.content == b"ZIP-ARTIFACT"

    def test_unknown_job_404(self, client, storage):
        resp = client.get("/api/pipeline/export/audacity/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Unknown job_id: does-not-exist"

    def test_expired_job_410(self, client, storage):
        _seed_job_row(storage, "job-aud-expired", status="expired")
        resp = client.get("/api/pipeline/export/audacity/job-aud-expired")
        assert resp.status_code == 410
        assert "expired" in resp.json()["detail"].lower()

    def test_non_completed_job_404(self, client, storage):
        _seed_job_row(storage, "job-aud-running", status="running")
        resp = client.get("/api/pipeline/export/audacity/job-aud-running")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not completed (status: running)"

    def test_missing_run_dir_404(self, client, storage, tmp_path):
        _seed_job_row(storage, "job-aud-nodir", output_dir=str(tmp_path / "nowhere"))
        resp = client.get("/api/pipeline/export/audacity/job-aud-nodir")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Output directory not found"

    def test_missing_artifact_404_json(self, client, storage, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _seed_job_row(storage, "job-aud-noartifact", output_dir=str(run_dir))
        resp = client.get("/api/pipeline/export/audacity/job-aud-noartifact")
        assert resp.status_code == 404
        assert resp.headers.get("content-type", "").startswith("application/json")
        assert resp.json()["detail"] == "File not found"
