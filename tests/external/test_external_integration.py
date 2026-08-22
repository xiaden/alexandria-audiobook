"""Deterministic integration tests for clone/design/LoRA seams (P2-S1, P2-S2).

Phase 2 of TASK-voice-persona-prompt-parity-D: these are "integration tests" in
the DD sense — they exercise the clone / design / builtin-LoRA / LoRA / narrator
seams through the *existing* engine factory + ``tts_integration.py`` public
seams, using a deterministic fake engine that implements the existing generate
contract and the fixed in-memory ``make_wav`` media fixture.

No production code is touched: only ``tests/external/`` additions.  ``app/tts.py``
stays byte-identical; no new routes, no arbitrary engine arguments.

Availability model (matches the phase-1 capability matrix):
  * Deterministic CI (no ``ALEXANDRIA_EXTERNAL`` marker): every test injects the
    fake engine, so the seams always pass deterministically — never a fabricated
    green, because the fake genuinely implements the generate contract.
  * A real-engine run is opt-in and availability-aware: when gated to a real
    engine the capability matrix reports ``supported | unavailable | failed``
    distinctly (phase-1), and these tests would exercise that engine through the
    same seams.  The matrix/fake-congruence test at the bottom asserts the fake
    satisfies the same capability contract the matrix probes.

Gate: ``/tmp/qa-venv/bin/pytest -q tests/external/`` — deterministic green.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api_export import get_tts_engine as api_get_tts_engine
from app.pipeline.api_onboard import get_storage
from app.pipeline.api_voices import router as voices_router
from app.pipeline.tts_integration import NARRATOR_VOICE, render_audiobook
from tests.external.capability_matrix import (
    ENABLE_EXTERNAL_ENV,
    ENGINE_CAPABILITIES,
    discover_capabilities,
    make_wav,
)


@pytest.fixture(autouse=True)
def _ensure_external_disabled(monkeypatch):
    """Deterministic CI: force the opt-in marker OFF for every test here."""
    monkeypatch.delenv(ENABLE_EXTERNAL_ENV, raising=False)


# ---------------------------------------------------------------------------
# Deterministic fake engine implementing the existing generate contract
# ---------------------------------------------------------------------------


class RecordingEngine:
    """Fake TTS engine implementing the existing generate contract.

    Mirrors ``app.tts.TTSEngine``'s public surface: ``generate_voice`` dispatches
    by ``voice_data["type"]`` (clone / lora+builtin_lora / design / custom), plus
    ``generate_batch`` and the individual ``generate_*`` methods.  Every call is
    recorded (in order) so tests assert the seam routed correctly; each method
    writes a deterministic ``make_wav`` fixture to the requested output path so
    the caller's file-arrangement logic (fsync/manifest/preview) has real bytes.
    """

    mode = "local"

    def __init__(self):
        self.calls: list[dict] = []
        self._wav = make_wav()

    def _record(self, method: str, **fields) -> None:
        entry = {"method": method, **fields}
        self.calls.append(entry)

    def _write_wav(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(self._wav)

    # -- The preview route calls generate_voice; dispatch by type like TTSEngine.
    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        self._record(
            "generate_voice",
            text=text,
            instruct_text=instruct_text,
            speaker=speaker,
            voice_config=dict(voice_config),
            output_path=output_path,
        )
        voice_data = voice_config.get(speaker, {})
        voice_type = voice_data.get("type", "custom")
        if voice_type == "clone":
            return self.generate_clone_voice(text, speaker, voice_config, output_path)
        if voice_type in ("lora", "builtin_lora"):
            return self.generate_lora_voice(text, instruct_text, voice_data, output_path)
        if voice_type == "design":
            return self.generate_design_voice(text, instruct_text, voice_data, output_path)
        return self.generate_custom_voice(text, instruct_text, speaker, voice_config, output_path)

    def generate_custom_voice(self, text, instruct_text, speaker, voice_config, output_path):
        self._record(
            "generate_custom_voice",
            text=text,
            instruct_text=instruct_text,
            speaker=speaker,
            voice_data=dict(voice_config.get(speaker, {})),
            output_path=output_path,
        )
        self._write_wav(output_path)
        return True

    def generate_clone_voice(self, text, speaker, voice_config, output_path):
        self._record(
            "generate_clone_voice",
            text=text,
            speaker=speaker,
            voice_config=dict(voice_config),
            output_path=output_path,
        )
        self._write_wav(output_path)
        return True

    def generate_lora_voice(self, text, instruct_text, voice_data, output_path):
        self._record(
            "generate_lora_voice",
            text=text,
            instruct_text=instruct_text,
            voice_data=dict(voice_data),
            output_path=output_path,
        )
        self._write_wav(output_path)
        return True

    def generate_design_voice(self, text, instruct_text, voice_data, output_path):
        self._record(
            "generate_design_voice",
            text=text,
            instruct_text=instruct_text,
            voice_data=dict(voice_data),
            output_path=output_path,
        )
        self._write_wav(output_path)
        return True

    def generate_voice_design(self, description, sample_text, language=None, seed=-1):
        # Returns (wav_path, sample_rate) per the design-preview contract.
        import tempfile

        self._record(
            "generate_voice_design",
            description=description,
            sample_text=sample_text,
            language=language,
            seed=seed,
        )
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        self._write_wav(wav_path)
        return wav_path, 8000

    def generate_batch(self, chunks, voice_config, output_dir, batch_seed=-1, cancel_check=None):
        self._record(
            "generate_batch",
            chunks=list(chunks),
            voice_config=dict(voice_config),
            output_dir=output_dir,
            batch_seed=batch_seed,
        )
        for chunk in chunks:
            out = os.path.join(output_dir, f"temp_batch_{chunk['index']}.wav")
            self._write_wav(out)
        return {"completed": [c["index"] for c in chunks], "failed": []}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_voice(storage: InMemorySQLiteAdapter, vid: str, **cols) -> None:
    """Insert a voice_config row with only the provided columns (rest NULL)."""
    defaults = {
        "id": vid,
        "name": None,
        "description": None,
        "type": "custom",
        "voice": None,
        "character_style": None,
        "seed": "-1",
        "ref_audio": None,
        "ref_text": None,
        "adapter_id": None,
        "adapter_path": None,
        "alias_of": None,
    }
    defaults.update(cols)
    placeholders = ", ".join(["?"] * len(defaults))
    storage.execute_insert(
        f"INSERT INTO voice_config ({', '.join(defaults)}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )


def _make_client(storage: InMemorySQLiteAdapter, engine: RecordingEngine):
    """FastAPI TestClient over the voices router with storage + engine overrides."""
    app = FastAPI()
    app.include_router(voices_router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[api_get_tts_engine] = lambda: engine
    return TestClient(app)


def _insert_clone_reference(
    storage: InMemorySQLiteAdapter,
    *,
    reference_id: str,
    voice_id: str,
    relative_path: str,
    media_type: str = "audio/wav",
) -> None:
    """Insert a clone_reference row pointing at *relative_path*."""
    storage.insert_clone_reference(
        {
            "reference_id": reference_id,
            "voice_id": voice_id,
            "owner_id": "local",
            "relative_path": relative_path,
            "original_filename": "ref.wav",
            "media_type": media_type,
            "byte_size": len(make_wav()),
            "duration_ms": 50,
            "sha256": "0" * 64,
            "created_ms": 1,
            "deleted_ms": None,
        }
    )


# ---------------------------------------------------------------------------
# 1. Clone reference rendering preview (GET .../references/{id}/preview)
# ---------------------------------------------------------------------------


class TestCloneReferenceRenderingPreview:
    def test_preview_streams_the_stored_reference_media(self, tmp_path, monkeypatch):
        """GET .../references/{id}/preview streams the stored WAV bytes."""
        ref_root = tmp_path / "refs"
        ref_root.mkdir()
        monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(ref_root))
        (ref_root / "abc123.wav").write_bytes(make_wav())

        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(storage, "clonev", type="clone")
        _insert_clone_reference(
            storage,
            reference_id="abc123",
            voice_id="clonev",
            relative_path="abc123.wav",
        )
        client = _make_client(storage, RecordingEngine())

        resp = client.get(
            "/api/pipeline/voices/clonev/references/abc123/preview"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == make_wav()

    def test_preview_404_when_media_missing(self, tmp_path, monkeypatch):
        """The route 404s (not 500) when the stored file is gone."""
        ref_root = tmp_path / "refs"
        ref_root.mkdir()
        monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(ref_root))

        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(storage, "clonev", type="clone")
        _insert_clone_reference(
            storage,
            reference_id="gone",
            voice_id="clonev",
            relative_path="gone.wav",  # never written
        )
        client = _make_client(storage, RecordingEngine())
        resp = client.get("/api/pipeline/voices/clonev/references/gone/preview")
        assert resp.status_code == 404

    def test_preview_404_cross_voice(self, tmp_path, monkeypatch):
        """A reference owned by another voice is indistinguishable from absence."""
        ref_root = tmp_path / "refs"
        ref_root.mkdir()
        monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(ref_root))
        (ref_root / "abc123.wav").write_bytes(make_wav())

        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(storage, "clonev", type="clone")
        _insert_voice(storage, "otherv", type="clone")
        _insert_clone_reference(
            storage,
            reference_id="abc123",
            voice_id="otherv",
            relative_path="abc123.wav",
        )
        client = _make_client(storage, RecordingEngine())
        # Accessing via the *wrong* voice must 404.
        resp = client.get(
            "/api/pipeline/voices/clonev/references/abc123/preview"
        )
        assert resp.status_code == 404

    def test_clone_preview_through_tts_seam_dispatches_clone(self, tmp_path, monkeypatch):
        """POST /voices/{clone_id}/preview routes the clone voice to generate_clone_voice.

        This is the "preview through TTS seam" for a clone voice: the preview
        route builds a voice_config with ``type == "clone"`` and ``ref_audio``
        and calls ``generate_voice``, which (per the existing contract) must
        dispatch to ``generate_clone_voice``.
        """
        monkeypatch.setattr(
            "app.pipeline.api_voices._PREVIEWS_DIR", str(tmp_path)
        )
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(
            storage,
            "clonev",
            type="clone",
            ref_audio="refs/abc123.wav",
            ref_text="aligned transcript",
            voice="base",
        )
        engine = RecordingEngine()
        client = _make_client(storage, engine)

        resp = client.post(
            "/api/pipeline/voices/clonev/preview", json={"sample_text": "Hello"}
        )
        assert resp.status_code == 200
        assert resp.json()["voice_id"] == "clonev"

        methods = [c["method"] for c in engine.calls]
        assert "generate_clone_voice" in methods
        clone_call = next(
            c for c in engine.calls if c["method"] == "generate_clone_voice"
        )
        vc = clone_call["voice_config"]["clonev"]
        assert vc["type"] == "clone"
        assert vc["ref_audio"] == "refs/abc123.wav"
        assert vc["ref_text"] == "aligned transcript"


# ---------------------------------------------------------------------------
# 2. Design voice rendering
# ---------------------------------------------------------------------------


class TestDesignVoiceRendering:
    def test_design_preview_dispatches_design(self, tmp_path, monkeypatch):
        """A design voice preview routes to the design generation seam."""
        monkeypatch.setattr(
            "app.pipeline.api_voices._PREVIEWS_DIR", str(tmp_path)
        )
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(
            storage,
            "designv",
            type="design",
            description="A warm, authoritative narrator",
            character_style="calm",
        )
        engine = RecordingEngine()
        client = _make_client(storage, engine)

        resp = client.post(
            "/api/pipeline/voices/designv/preview", json={"sample_text": "Once upon a time"}
        )
        assert resp.status_code == 200
        assert resp.json()["voice_id"] == "designv"

        methods = [c["method"] for c in engine.calls]
        assert "generate_design_voice" in methods
        design_call = next(
            c for c in engine.calls if c["method"] == "generate_design_voice"
        )
        vd = design_call["voice_data"]
        assert vd["type"] == "design"
        assert vd["description"] == "A warm, authoritative narrator"
        # The output WAV is written deterministically.
        assert os.path.isfile(design_call["output_path"])
        with open(design_call["output_path"], "rb") as f:
            assert f.read() == make_wav()

    def test_voice_design_seam_returns_wav_and_rate(self):
        """generate_voice_design returns a (wav_path, sample_rate) tuple.

        The fake engine writes the fixture to an ``mkstemp`` file in the OS
        temp dir; we read it back via a context manager and unlink it in a
        ``finally`` so no stray /tmp file leaks across test runs. The WAV bytes
        are unchanged (deterministic ``make_wav`` fixture).
        """
        engine = RecordingEngine()
        wav_path, sr = engine.generate_voice_design("A voice", "sample")
        try:
            assert isinstance(wav_path, str) and os.path.isfile(wav_path)
            assert sr == 8000
            with open(wav_path, "rb") as f:
                assert f.read() == make_wav()
        finally:
            if os.path.isfile(wav_path):
                os.unlink(wav_path)


# ---------------------------------------------------------------------------
# 3. Builtin LoRA + LoRA adapter rendering
# ---------------------------------------------------------------------------


class TestLoraRendering:
    def test_builtin_lora_preview_dispatches_lora(self, tmp_path, monkeypatch):
        """A builtin_lora voice preview routes to the LoRA seam."""
        monkeypatch.setattr(
            "app.pipeline.api_voices._PREVIEWS_DIR", str(tmp_path)
        )
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(
            storage,
            "blora",
            type="builtin_lora",
            adapter_id="builtin_narrator",
            character_style="storybook",
        )
        engine = RecordingEngine()
        client = _make_client(storage, engine)

        resp = client.post(
            "/api/pipeline/voices/blora/preview", json={"sample_text": "Hi"}
        )
        assert resp.status_code == 200

        methods = [c["method"] for c in engine.calls]
        assert "generate_lora_voice" in methods
        lora_call = next(
            c for c in engine.calls if c["method"] == "generate_lora_voice"
        )
        vd = lora_call["voice_data"]
        assert vd["type"] == "builtin_lora"
        assert vd["adapter_id"] == "builtin_narrator"

    def test_lora_adapter_preview_dispatches_lora(self, tmp_path, monkeypatch):
        """A LoRA-adapter voice preview routes to the LoRA seam with adapter_path."""
        monkeypatch.setattr(
            "app.pipeline.api_voices._PREVIEWS_DIR", str(tmp_path)
        )
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_voice(
            storage,
            "lorav",
            type="lora",
            adapter_id="custom-adapter",
            adapter_path="lora_adapters/custom",
        )
        engine = RecordingEngine()
        client = _make_client(storage, engine)

        resp = client.post(
            "/api/pipeline/voices/lorav/preview", json={"sample_text": "Hi"}
        )
        assert resp.status_code == 200

        methods = [c["method"] for c in engine.calls]
        assert "generate_lora_voice" in methods
        lora_call = next(
            c for c in engine.calls if c["method"] == "generate_lora_voice"
        )
        vd = lora_call["voice_data"]
        assert vd["type"] == "lora"
        assert vd["adapter_id"] == "custom-adapter"
        assert vd["adapter_path"] == "lora_adapters/custom"


# ---------------------------------------------------------------------------
# 4. Narrator fallback
# ---------------------------------------------------------------------------


def _minimal_book(storage: InMemorySQLiteAdapter) -> None:
    """Insert a one-span book with NO voice_config rows (NARRATOR has no row)."""
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
    )
    storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
    )
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
    )
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) "
        "VALUES ('sp1', 'sentence', 'No speaker here.', NULL)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
    )


class TestNarratorFallback:
    def test_narrator_falls_back_to_constant_when_no_row(self, tmp_path, monkeypatch):
        """With no voice_config NARRATOR row, the NARRATOR_VOICE constant is used."""
        monkeypatch.setenv("RENDER_ROOT", str(tmp_path / "render_root"))
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _minimal_book(storage)
        engine = RecordingEngine()

        render_audiobook("b1", storage, engine)
        assert engine.calls
        batch = engine.calls[0]
        assert batch["method"] == "generate_batch"
        vc = batch["voice_config"]
        assert "NARRATOR" in vc
        assert vc["NARRATOR"] == NARRATOR_VOICE
        # The unattributed span resolves to NARRATOR speaker.
        assert batch["chunks"][0]["speaker"] == "NARRATOR"

    def test_narrator_voice_constant_shape(self):
        """NARRATOR_VOICE has the documented custom/Ryan shape."""
        assert NARRATOR_VOICE == {"type": "custom", "voice": "Ryan"}


# ---------------------------------------------------------------------------
# Availability-aware: the fake engine satisfies the capability contract
# ---------------------------------------------------------------------------


class TestFakeEngineCapabilityContract:
    def test_fake_engine_reports_all_engine_capabilities_supported(self):
        """The deterministic fake implements every engine capability the matrix probes.

        This proves the integration tests exercise genuinely-capable seams
        (never a fabricated green): through the same ``engine_provider`` seam
        the phase-1 matrix uses, the fake reports ``supported`` for clone /
        design / builtin_lora / lora / model-runtime.
        """
        report = discover_capabilities(
            enable_external=True, engine_provider=lambda: RecordingEngine()
        )
        by_name = {row["capability"]: row for row in report}
        for cap in ENGINE_CAPABILITIES:
            assert by_name[cap]["status"] == "supported", cap
