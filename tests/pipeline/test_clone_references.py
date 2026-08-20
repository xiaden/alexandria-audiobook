"""Route tests for pipeline-native clone-reference resources.

Covers upload/list/preview/download/delete, ownership (owner sentinel
``local``), path/content/size/duration rejection, partial-file cleanup,
cross-owner/cross-voice 404, idempotent delete, live 503 + ``Retry-After: 5``
on transaction contention, and no filesystem-path exposure.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pipeline.adapter import ConcurrentTransactionError, InMemorySQLiteAdapter
from app.pipeline.api_export import get_tts_engine
from app.pipeline.api_onboard import get_storage
from app.pipeline.api_voices import router as voices_router

# app/app.py imports app-local bare modules (``utils``, ``hf_utils``) at module
# level.  Load them into sys.modules before importing app.app so the real
# application — including the ConcurrentTransactionError → 503 exception
# handler — can be exercised through TestClient.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"


def _load_app_local_module(name: str) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

_LOCAL = "local"


def make_wav(duration_seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Minimal valid 16-bit mono PCM WAV blob (accepted by ffprobe)."""
    num_samples = int(duration_seconds * sample_rate)
    byte_rate = sample_rate * 2
    data = struct.pack("<h", 0) * num_samples
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + fmt
    header += b"data" + struct.pack("<I", len(data))
    return header + data


def _seed_voices(storage: InMemorySQLiteAdapter) -> None:
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, type, voice, ref_audio, ref_text) "
        "VALUES ('vclone', 'Clone', 'clone', 'c', NULL, NULL)"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, type, voice) "
        "VALUES ('vcustom', 'Custom', 'custom', 'c2')"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, type) "
        "VALUES ('vbogus', 'Bad', 'notatype')"
    )


def _insert_reference(
    storage: InMemorySQLiteAdapter,
    reference_id: str,
    voice_id: str = "vclone",
    owner_id: str = _LOCAL,
    relative_path: str = "clip.wav",
    created_ms: int = 1,
) -> None:
    storage.insert_clone_reference(
        {
            "reference_id": reference_id,
            "voice_id": voice_id,
            "owner_id": owner_id,
            "relative_path": relative_path,
            "original_filename": "clip.wav",
            "media_type": "audio/wav",
            "byte_size": 100,
            "duration_ms": 1000,
            "sha256": "a" * 64,
            "created_ms": created_ms,
            "deleted_ms": None,
        }
    )


@pytest.fixture()
def storage() -> InMemorySQLiteAdapter:
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    _seed_voices(adapter)
    return adapter


@pytest.fixture()
def ref_root(tmp_path, monkeypatch) -> str:
    root = tmp_path / "references"
    root.mkdir()
    monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(root))
    return str(root)


@pytest.fixture()
def client(storage, ref_root) -> TestClient:
    app = FastAPI()
    app.include_router(voices_router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_tts_engine] = lambda: None
    return TestClient(app)


@pytest.fixture()
def real_client(storage, ref_root):
    """Live app (with the global ConcurrentTransactionError → 503 handler)."""
    import app.app as real_app

    app = real_app.app
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_tts_engine] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class TestCreateCloneReference:
    def test_upload_returns_201_with_reference_and_voice(self, client, ref_root):
        payload = make_wav()
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", payload, "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 201
        body = resp.json()
        ref = body["reference"]
        assert ref["voice_id"] == "vclone"
        assert ref["owner_id"] == _LOCAL
        assert ref["original_filename"] == "clip.wav"
        assert ref["media_type"] == "audio/wav"
        assert ref["byte_size"] == len(payload)
        assert ref["deleted_ms"] is None
        assert ref["relative_path"] == f"{ref['reference_id']}.wav"
        # No filesystem absolute path is exposed.
        assert os.path.abspath(ref_root) not in body["reference"]["relative_path"]
        assert "/" not in ref["relative_path"]
        assert body["voice"]["id"] == "vclone"
        assert body["voice"]["type"] == "clone"
        assert body["voice"]["ref_audio"] == ref["relative_path"]
        assert body["voice"]["ref_text"] == "hello world"

    def test_upload_writes_file_under_reference_root(self, client, ref_root):
        payload = make_wav()
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", payload, "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 201
        relative = resp.json()["reference"]["relative_path"]
        written = os.path.join(ref_root, relative)
        assert os.path.isfile(written)
        assert open(written, "rb").read() == payload

    def test_upload_missing_ref_text_returns_422(self, client):
        payload = make_wav()
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", payload, "audio/wav")},
        )
        assert resp.status_code == 422

    def test_upload_blank_ref_text_returns_400(self, client):
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "   "},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Reference text is required"

    def test_upload_strips_ref_text(self, client):
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "  hello world  "},
        )
        assert resp.status_code == 201
        assert resp.json()["voice"]["ref_text"] == "hello world"

    def test_upload_unknown_voice_404(self, client):
        resp = client.post(
            "/api/pipeline/voices/nope/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 404

    def test_upload_invalid_voice_type_400(self, client):
        resp = client.post(
            "/api/pipeline/voices/vbogus/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 400

    def test_upload_unsupported_extension_400(self, client):
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("evil.txt", b"text", "text/plain")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 400

    def test_upload_content_mismatch_400(self, client, ref_root):
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", b"not really wav", "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 400
        assert list(os.listdir(ref_root)) == []

    def test_upload_oversize_400_and_cleaned(self, client, ref_root, monkeypatch):
        monkeypatch.setenv("CLONE_REFERENCE_MAX_BYTES", "16")
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 400
        assert list(os.listdir(ref_root)) == []

    def test_upload_duration_limit_400_and_cleaned(self, client, ref_root, monkeypatch):
        monkeypatch.setenv("CLONE_REFERENCE_MAX_DURATION_MS", "1")
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 400
        assert list(os.listdir(ref_root)) == []

    def test_upload_hostile_filename_uses_basename(self, client, ref_root):
        resp = client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("../../evil.wav", make_wav(), "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 201
        ref = resp.json()["reference"]
        assert ref["original_filename"] == "evil.wav"
        assert ref["relative_path"] == f"{ref['reference_id']}.wav"
        assert not os.path.exists(os.path.join(ref_root, "..", "evil.wav"))

    def test_upload_contention_503_retry_after(self, real_client, storage, ref_root, monkeypatch):
        def boom(record):
            raise ConcurrentTransactionError("contention")

        monkeypatch.setattr(storage, "insert_clone_reference", boom)
        resp = real_client.post(
            "/api/pipeline/voices/vclone/references",
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"ref_text": "hello world"},
        )
        assert resp.status_code == 503
        assert resp.headers.get("retry-after") == "5"
        # Failed insert must not orphan the written media file.
        assert list(os.listdir(ref_root)) == []


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListCloneReferences:
    def test_list_returns_owner_rows_ascending(self, client, storage):
        _insert_reference(storage, "r1", created_ms=1)
        _insert_reference(storage, "r2", created_ms=2)
        resp = client.get("/api/pipeline/voices/vclone/references")
        assert resp.status_code == 200
        refs = resp.json()["references"]
        assert [r["reference_id"] for r in refs] == ["r1", "r2"]
        for r in refs:
            assert r["owner_id"] == _LOCAL

    def test_list_empty(self, client):
        resp = client.get("/api/pipeline/voices/vclone/references")
        assert resp.status_code == 200
        assert resp.json()["references"] == []

    def test_list_excludes_cross_owner(self, client, storage):
        _insert_reference(storage, "r1", owner_id="someone_else")
        resp = client.get("/api/pipeline/voices/vclone/references")
        assert resp.status_code == 200
        assert resp.json()["references"] == []

    def test_list_excludes_deleted_references(self, client, storage):
        _insert_reference(storage, "active", created_ms=1)
        _insert_reference(storage, "deleted", created_ms=2)
        storage.execute_update(
            "UPDATE clone_reference SET deleted_ms = ? WHERE reference_id = ?",
            (123, "deleted"),
        )

        resp = client.get("/api/pipeline/voices/vclone/references")

        assert resp.status_code == 200
        assert [r["reference_id"] for r in resp.json()["references"]] == ["active"]

    def test_list_unknown_voice_404(self, client):
        resp = client.get("/api/pipeline/voices/nope/references")
        assert resp.status_code == 404

    def test_list_no_absolute_path_exposed(self, client, storage, ref_root):
        _insert_reference(storage, "r1", relative_path="r1.wav")
        resp = client.get("/api/pipeline/voices/vclone/references")
        body = resp.json()
        assert os.path.abspath(ref_root) not in str(body)
        assert body["references"][0]["relative_path"] == "r1.wav"


# ---------------------------------------------------------------------------
# Preview / download
# ---------------------------------------------------------------------------


class TestPreviewCloneReference:
    def test_preview_inline_audio(self, client, storage, ref_root):
        payload = make_wav()
        _insert_reference(storage, "r1", relative_path="r1.wav")
        os.makedirs(ref_root, exist_ok=True)
        with open(os.path.join(ref_root, "r1.wav"), "wb") as fh:
            fh.write(payload)
        resp = client.get(
            "/api/pipeline/voices/vclone/references/r1/preview"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == payload

    def test_preview_missing_media_404(self, client, storage):
        _insert_reference(storage, "r1", relative_path="r1.wav")
        resp = client.get(
            "/api/pipeline/voices/vclone/references/r1/preview"
        )
        assert resp.status_code == 404

    def test_preview_unknown_reference_404(self, client):
        resp = client.get(
            "/api/pipeline/voices/vclone/references/nope/preview"
        )
        assert resp.status_code == 404

    def test_preview_cross_owner_404(self, client, storage):
        _insert_reference(storage, "r1", owner_id="someone_else")
        resp = client.get(
            "/api/pipeline/voices/vclone/references/r1/preview"
        )
        assert resp.status_code == 404

    def test_preview_cross_voice_404(self, client, storage):
        _insert_reference(storage, "r1", voice_id="vclone")
        resp = client.get(
            "/api/pipeline/voices/vcustom/references/r1/preview"
        )
        assert resp.status_code == 404


class TestDownloadCloneReference:
    def test_download_is_attachment(self, client, storage, ref_root):
        payload = make_wav()
        _insert_reference(storage, "r1", relative_path="r1.wav")
        with open(os.path.join(ref_root, "r1.wav"), "wb") as fh:
            fh.write(payload)
        resp = client.get(
            "/api/pipeline/voices/vclone/references/r1/download"
        )
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        assert disposition.lower().startswith("attachment")
        assert "clip.wav" in disposition
        assert resp.content == payload

    def test_download_unknown_reference_404(self, client):
        resp = client.get(
            "/api/pipeline/voices/vclone/references/nope/download"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteCloneReference:
    def test_delete_clears_matching_voice_reference_config(self, client, storage):
        _insert_reference(storage, "r1", relative_path="r1.wav")
        storage.execute_update(
            "UPDATE voice_config SET ref_audio = ?, ref_text = ? WHERE id = ?",
            ("r1.wav", "matching transcript", "vclone"),
        )

        resp = client.delete("/api/pipeline/voices/vclone/references/r1")

        assert resp.status_code == 204
        voice = storage.execute_query(
            "SELECT ref_audio, ref_text FROM voice_config WHERE id = ?",
            ("vclone",),
        )[0]
        assert voice["ref_audio"] is None
        assert voice["ref_text"] is None

    def test_delete_does_not_clear_newer_voice_reference_config(
        self, client, storage
    ):
        _insert_reference(storage, "r1", relative_path="r1.wav")
        storage.execute_update(
            "UPDATE voice_config SET ref_audio = ?, ref_text = ? WHERE id = ?",
            ("newer.wav", "newer transcript", "vclone"),
        )

        resp = client.delete("/api/pipeline/voices/vclone/references/r1")

        assert resp.status_code == 204
        voice = storage.execute_query(
            "SELECT ref_audio, ref_text FROM voice_config WHERE id = ?",
            ("vclone",),
        )[0]
        assert voice["ref_audio"] == "newer.wav"
        assert voice["ref_text"] == "newer transcript"

    def test_delete_204_tombstones_and_removes_file(self, client, storage, ref_root):
        _insert_reference(storage, "r1", relative_path="r1.wav")
        with open(os.path.join(ref_root, "r1.wav"), "wb") as fh:
            fh.write(b"data")
        resp = client.delete(
            "/api/pipeline/voices/vclone/references/r1"
        )
        assert resp.status_code == 204
        row = storage.get_clone_reference("r1", _LOCAL)
        assert row["deleted_ms"] is not None
        assert not os.path.exists(os.path.join(ref_root, "r1.wav"))

    def test_delete_is_idempotent(self, client, storage):
        _insert_reference(storage, "r1", relative_path="r1.wav")
        assert client.delete(
            "/api/pipeline/voices/vclone/references/r1"
        ).status_code == 204
        assert client.delete(
            "/api/pipeline/voices/vclone/references/r1"
        ).status_code == 204

    def test_delete_cross_owner_404(self, client, storage, ref_root):
        _insert_reference(storage, "r1", owner_id="someone_else")
        resp = client.delete("/api/pipeline/voices/vclone/references/r1")
        assert resp.status_code == 404
        # Cross-owner row must remain untouched.
        assert storage.get_clone_reference("r1", "someone_else")["deleted_ms"] is None

    def test_delete_cross_voice_404_does_not_tombstone(self, client, storage, ref_root):
        _insert_reference(storage, "r1", voice_id="vclone")
        resp = client.delete("/api/pipeline/voices/vcustom/references/r1")
        assert resp.status_code == 404
        assert storage.get_clone_reference("r1", _LOCAL)["deleted_ms"] is None

    def test_delete_unknown_reference_404(self, client):
        resp = client.delete("/api/pipeline/voices/vclone/references/nope")
        assert resp.status_code == 404

    def test_delete_contention_503(self, real_client, storage, monkeypatch):
        _insert_reference(storage, "r1")

        def boom(reference_id, owner_id, now_ms):
            raise ConcurrentTransactionError("contention")

        monkeypatch.setattr(storage, "tombstone_clone_reference", boom)
        resp = real_client.delete("/api/pipeline/voices/vclone/references/r1")
        assert resp.status_code == 503
        assert resp.headers.get("retry-after") == "5"


# ---------------------------------------------------------------------------
# Regression: existing voice CRUD remains intact
# ---------------------------------------------------------------------------


class TestExistingVoiceCrudPreserved:
    def test_list_voices_still_works(self, client):
        resp = client.get("/api/pipeline/voices")
        assert resp.status_code == 200
        ids = {v["id"] for v in resp.json()}
        assert {"vclone", "vcustom", "vbogus"} <= ids
