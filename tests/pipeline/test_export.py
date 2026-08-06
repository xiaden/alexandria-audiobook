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
"""

from __future__ import annotations

import io
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
        assert resp.headers["content-disposition"] == 'attachment; filename="audiobook.m4b"'
        assert resp.content == b"FAKE-M4B-CONTENT"

    def test_zip_fallback_when_no_m4b(self, client, storage, tmp_path):
        """A completed job row with audio chunks but no m4b yields audiobook.zip
        (application/zip) containing exactly the wav/mp3/m4a/flac files."""
        output_dir = tmp_path / "out-zip"
        output_dir.mkdir()
        chunk_names = ("chunk_0000.wav", "chunk_0001.mp3", "chunk_0002.m4a", "chunk_0003.flac")
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
        assert resp.headers["content-disposition"] == 'attachment; filename="audiobook.zip"'

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert sorted(zf.namelist()) == sorted(chunk_names)
        assert zf.testzip() is None
        assert zf.getinfo("chunk_0000.wav").file_size == len(b"audio-data-chunk_0000.wav")

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
        api_export._render_jobs[job_id] = {"status": "completed", "output_dir": str(output_dir)}

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
            (job_id, "/nonexistent/output/dir", "/nonexistent/output/dir/audiobook.m4b"),
        )

        resp = client.get(f"/api/pipeline/download/{job_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Output directory not found"

    def test_serves_m4b_produced_by_merge_after_finalize(self, client, storage, tmp_path):
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
        assert resp.headers["content-disposition"] == 'attachment; filename="audiobook.m4b"'
        assert resp.content == b"LATE-MERGE-M4B"
