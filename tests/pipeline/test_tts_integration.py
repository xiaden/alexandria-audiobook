"""Tests for render_audiobook — TTS integration contract.

Covers:
- render_audiobook returns a job_id (UUID string)
- NARRATOR speaker gets default narrator voice config
- Character speakers get voice_assignment_id → voice_config mapping
- FakeTTSEngine.generate_batch called with correct chunk format
- Voice config mapping includes all speakers
- Empty book returns valid job_id (no chunks to render)
- use_batch=False calls generate_voice per-chunk
- export_annotated_script is called internally
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from pydub import AudioSegment

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.tts_integration import (
    NARRATOR_VOICE,
    PAUSED_ARTIFACT_NAME,
    _assemble_paused_artifact,
    _build_chunks,
    _build_voice_config,
    render_audiobook,
)

# ---------------------------------------------------------------------------
# Fake TTSEngine (no GPU/model loading)
# ---------------------------------------------------------------------------


class FakeTTSEngine:
    """Records calls to generate_batch and generate_voice without loading models."""

    def __init__(self):
        self.batch_calls: list[dict] = []
        self.voice_calls: list[dict] = []

    def generate_batch(
        self, chunks, voice_config, output_dir, batch_seed=-1, cancel_check=None
    ):
        """Record the call, write placeholder wavs, and return all indices as completed."""
        self.batch_calls.append(
            {
                "chunks": list(chunks),
                "voice_config": dict(voice_config),
                "output_dir": output_dir,
                "batch_seed": batch_seed,
                "cancel_check": cancel_check,
            }
        )
        # Mirror app/tts.py's batch file naming so the fsync/manifest
        # discipline has real files to work with.
        for chunk in chunks:
            out_path = os.path.join(output_dir, f"temp_batch_{chunk['index']}.wav")
            with open(out_path, "wb") as f:
                f.write(b"fake wav data\n")
        completed = [c["index"] for c in chunks]
        return {"completed": completed, "failed": []}

    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        """Record the call, write a placeholder wav, and return True (success)."""
        self.voice_calls.append(
            {
                "text": text,
                "instruct_text": instruct_text,
                "speaker": speaker,
                "voice_config": dict(voice_config),
                "output_path": output_path,
            }
        )
        with open(output_path, "wb") as f:
            f.write(b"fake wav data\n")
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_storage(storage: InMemorySQLiteAdapter) -> None:
    """Insert a minimal but complete document spine with characters and voices."""
    # -- Voice config -------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES ('vc1', 'Warm Female', 'A warm female voice')"
    )
    storage.execute_insert(
        "INSERT INTO voice_config (id, name, description) VALUES ('vc2', 'Deep Male', 'A deep male voice')"
    )

    # -- Series + Book ------------------------------------------------------
    storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
    storage.execute_insert(
        "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
    )

    # -- Chapters -----------------------------------------------------------
    storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
    storage.execute_insert(
        "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
    )

    # -- Scenes -------------------------------------------------------------
    storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
    storage.execute_insert(
        "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
    )

    # -- Paragraphs ---------------------------------------------------------
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p2')")
    storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p3')")
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p2', 'sc1', 2)"
    )
    storage.execute_insert(
        "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p3', 'sc1', 3)"
    )

    # -- Spans --------------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp1', 'quotation', 'Hello there!', 'cheerfully')"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp2', 'sentence', 'She walked away.', NULL)"
    )
    storage.execute_insert(
        "INSERT INTO span (id, span_type, text, instruct) VALUES ('sp3', 'quotation', 'Goodbye.', 'sadly')"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p2', 1)"
    )
    storage.execute_insert(
        "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp3', 'p3', 1)"
    )

    # -- Characters ---------------------------------------------------------
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c1', 'Alice', '[]', 'vc1')"
    )
    storage.execute_insert(
        "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c2', 'Bob', '[]', 'vc2')"
    )

    # -- character_span (speaker junctions) ---------------------------------
    # sp1 → Alice is speaker
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"""
    )
    # sp2 → no speaker junction (UNKNOWN → NARRATOR)
    # sp3 → Bob is speaker
    storage.execute_insert(
        """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
           VALUES ('c2', 'sp3', 'speaker', 'walk', 0.9)"""
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    """Return a populated InMemorySQLiteAdapter."""
    s = InMemorySQLiteAdapter()
    s.init_db()
    _populate_storage(s)
    return s


@pytest.fixture
def fake_engine():
    """Return a FakeTTSEngine instance."""
    return FakeTTSEngine()


@pytest.fixture(autouse=True)
def _render_root(tmp_path, monkeypatch):
    """Pin RENDER_ROOT to a fresh tmp dir for every test in this module.

    ``render_audiobook`` resolves ``RENDER_ROOT`` from the environment at
    call time; without this pin, ``output_dir=None`` renders would create
    run dirs under the repo's ``data/render_root``.  All Plan C phase 1
    tests rely on this deterministic file-backed root.
    """
    root = tmp_path / "render_root"
    root.mkdir()
    monkeypatch.setenv("RENDER_ROOT", str(root))
    return str(root)


# ---------------------------------------------------------------------------
# Tests: render_audiobook returns job_id
# ---------------------------------------------------------------------------


class TestRenderAudiobookReturnsJobId:
    def test_returns_string(self, storage, fake_engine):
        """render_audiobook returns a string job_id."""
        job_id = render_audiobook("b1", storage, fake_engine)
        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_returns_uuid_format(self, storage, fake_engine):
        """job_id is a valid UUID string."""
        import uuid

        job_id = render_audiobook("b1", storage, fake_engine)
        # Should not raise
        parsed = uuid.UUID(job_id)
        assert str(parsed) == job_id

    def test_each_call_returns_unique_job_id(self, storage, fake_engine):
        """Each call to render_audiobook returns a different job_id."""
        job_id_1 = render_audiobook("b1", storage, fake_engine)
        job_id_2 = render_audiobook("b1", storage, fake_engine)
        assert job_id_1 != job_id_2


# ---------------------------------------------------------------------------
# Tests: NARRATOR gets default voice config
# ---------------------------------------------------------------------------


class TestNarratorVoiceConfig:
    def test_narrator_gets_default_voice(self, storage, fake_engine):
        """NARRATOR speaker uses the NARRATOR_VOICE constant."""
        render_audiobook("b1", storage, fake_engine)

        # The voice_config passed to generate_batch should include NARRATOR
        assert len(fake_engine.batch_calls) == 1
        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert "NARRATOR" in voice_config
        assert voice_config["NARRATOR"] == NARRATOR_VOICE
        assert voice_config["NARRATOR"]["type"] == "custom"
        assert voice_config["NARRATOR"]["voice"] == "Ryan"

    def test_narrator_voice_constant_values(self):
        """NARRATOR_VOICE constant has expected keys."""
        assert "type" in NARRATOR_VOICE
        assert "voice" in NARRATOR_VOICE
        assert NARRATOR_VOICE["type"] == "custom"
        assert NARRATOR_VOICE["voice"] == "Ryan"


# ---------------------------------------------------------------------------
# Tests: Character speakers get voice_assignment_id → voice_config
# ---------------------------------------------------------------------------


class TestCharacterVoiceConfig:
    def test_character_with_voice_assignment_resolved(self, storage, fake_engine):
        """Characters with voice_assignment_id get their voice config from voice_config table."""
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        # Alice has voice_assignment_id = 'vc1' → voice_config name = 'Warm Female'
        assert "Alice" in voice_config
        assert voice_config["Alice"]["voice"] == "Warm Female"
        assert voice_config["Alice"]["type"] == "custom"

    def test_character_voice_description_included(self, storage, fake_engine):
        """Voice description from voice_config table is included in the mapping."""
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert voice_config["Alice"]["description"] == "A warm female voice"
        assert voice_config["Bob"]["description"] == "A deep male voice"

    def test_bob_voice_resolved(self, storage, fake_engine):
        """Bob's voice_assignment_id is correctly resolved."""
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert "Bob" in voice_config
        assert voice_config["Bob"]["voice"] == "Deep Male"


# ---------------------------------------------------------------------------
# Tests: generate_batch called with correct chunk format
# ---------------------------------------------------------------------------


class TestBatchChunkFormat:
    def test_chunks_have_required_keys(self, storage, fake_engine):
        """Each chunk has index, text, instruct, speaker keys."""
        render_audiobook("b1", storage, fake_engine)

        chunks = fake_engine.batch_calls[0]["chunks"]
        assert len(chunks) == 3  # 3 spans in test data
        for chunk in chunks:
            assert "index" in chunk
            assert "text" in chunk
            assert "instruct" in chunk
            assert "speaker" in chunk

    def test_chunks_are_zero_indexed(self, storage, fake_engine):
        """Chunk indices start at 0 and are sequential."""
        render_audiobook("b1", storage, fake_engine)

        chunks = fake_engine.batch_calls[0]["chunks"]
        indices = [c["index"] for c in chunks]
        assert indices == [0, 1, 2]

    def test_chunk_text_matches_script(self, storage, fake_engine):
        """Chunk text matches the annotated script entries."""
        render_audiobook("b1", storage, fake_engine)

        chunks = fake_engine.batch_calls[0]["chunks"]
        assert chunks[0]["text"] == "Hello there!"
        assert chunks[1]["text"] == "She walked away."
        assert chunks[2]["text"] == "Goodbye."

    def test_chunk_instruct_matches_script(self, storage, fake_engine):
        """Chunk instruct matches the annotated script entries (empty string for NULL)."""
        render_audiobook("b1", storage, fake_engine)

        chunks = fake_engine.batch_calls[0]["chunks"]
        assert chunks[0]["instruct"] == "cheerfully"
        assert chunks[1]["instruct"] == ""  # NULL → empty string
        assert chunks[2]["instruct"] == "sadly"

    def test_chunk_speaker_matches_script(self, storage, fake_engine):
        """Chunk speaker matches the annotated script entries (NARRATOR for unowned)."""
        render_audiobook("b1", storage, fake_engine)

        chunks = fake_engine.batch_calls[0]["chunks"]
        assert chunks[0]["speaker"] == "Alice"
        assert chunks[1]["speaker"] == "NARRATOR"
        assert chunks[2]["speaker"] == "Bob"


# ---------------------------------------------------------------------------
# Tests: Voice config includes all speakers
# ---------------------------------------------------------------------------


class TestVoiceConfigCompleteness:
    def test_all_speakers_in_voice_config(self, storage, fake_engine):
        """Voice config mapping includes entries for all speakers in the script."""
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        # Script has Alice, NARRATOR, Bob
        assert "Alice" in voice_config
        assert "NARRATOR" in voice_config
        assert "Bob" in voice_config

    def test_voice_config_has_three_entries(self, storage, fake_engine):
        """Voice config has exactly as many entries as unique speakers."""
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert len(voice_config) == 3


# ---------------------------------------------------------------------------
# Tests: Empty book
# ---------------------------------------------------------------------------


class TestEmptyBook:
    def test_empty_book_returns_job_id(self, fake_engine):
        """A non-existent book returns a valid job_id without calling TTS."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        job_id = render_audiobook("nonexistent", s, fake_engine)
        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_empty_book_no_batch_calls(self, fake_engine):
        """Empty book does not call generate_batch."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        render_audiobook("nonexistent", s, fake_engine)
        assert len(fake_engine.batch_calls) == 0

    def test_empty_book_no_voice_calls(self, fake_engine):
        """Empty book does not call generate_voice."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        render_audiobook("nonexistent", s, fake_engine, use_batch=False)
        assert len(fake_engine.voice_calls) == 0

    def test_empty_book_with_existing_book(self, fake_engine):
        """A book that exists but has no spans returns a valid job_id."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert("INSERT INTO series (id) VALUES ('s-empty')")
        s.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b-empty', 's-empty', 1)"
        )
        job_id = render_audiobook("b-empty", s, fake_engine)
        assert isinstance(job_id, str)
        assert len(fake_engine.batch_calls) == 0


# ---------------------------------------------------------------------------
# Tests: use_batch=False calls generate_voice per-chunk
# ---------------------------------------------------------------------------


class TestIndividualGeneration:
    def test_use_batch_false_calls_generate_voice(self, storage, fake_engine):
        """When use_batch=False, generate_voice is called per-chunk."""
        render_audiobook("b1", storage, fake_engine, use_batch=False)

        # generate_batch should NOT be called
        assert len(fake_engine.batch_calls) == 0
        # generate_voice should be called once per chunk
        assert len(fake_engine.voice_calls) == 3

    def test_generate_voice_receives_correct_args(self, storage, fake_engine):
        """generate_voice receives text, instruct, speaker, voice_config, output_path."""
        render_audiobook("b1", storage, fake_engine, use_batch=False)

        call = fake_engine.voice_calls[0]
        assert call["text"] == "Hello there!"
        assert call["instruct_text"] == "cheerfully"
        assert call["speaker"] == "Alice"
        assert "voice_config" in call
        assert "output_path" in call

    def test_generate_voice_output_path_format(self, storage, fake_engine):
        """generate_voice writes to a tmp sibling; the final chunk_{index:04d}.wav
        appears after the fsync+rename discipline (P1-S1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            render_audiobook(
                "b1", storage, fake_engine, use_batch=False, output_dir=tmpdir
            )

            # The engine receives the tmp sibling path ...
            paths = [c["output_path"] for c in fake_engine.voice_calls]
            assert paths[0] == os.path.join(tmpdir, "chunk_0000.wav.tmp")
            assert paths[1] == os.path.join(tmpdir, "chunk_0001.wav.tmp")
            assert paths[2] == os.path.join(tmpdir, "chunk_0002.wav.tmp")
            # ... and the final chunk file exists after the rename.
            assert os.path.isfile(os.path.join(tmpdir, "chunk_0000.wav"))

    def test_generate_voice_receives_voice_config(self, storage, fake_engine):
        """Each generate_voice call receives the full voice_config mapping."""
        render_audiobook("b1", storage, fake_engine, use_batch=False)

        for call in fake_engine.voice_calls:
            vc = call["voice_config"]
            assert "NARRATOR" in vc
            assert "Alice" in vc
            assert "Bob" in vc


# ---------------------------------------------------------------------------
# Tests: export_annotated_script is called internally
# ---------------------------------------------------------------------------


class TestExportCalledInternally:
    def test_export_is_called(self, storage, fake_engine, monkeypatch):
        """render_audiobook calls export_annotated_script internally."""
        from app.pipeline import tts_integration

        called_with = []
        original = tts_integration.export_annotated_script

        def spy(book_id, storage_arg):
            called_with.append((book_id, storage_arg))
            return original(book_id, storage_arg)

        monkeypatch.setattr(tts_integration, "export_annotated_script", spy)
        render_audiobook("b1", storage, fake_engine)

        assert len(called_with) == 1
        assert called_with[0][0] == "b1"

    def test_export_result_used_for_chunks(self, storage, fake_engine):
        """The script from export_annotated_script is used to build chunks."""
        render_audiobook("b1", storage, fake_engine)

        chunks = fake_engine.batch_calls[0]["chunks"]
        # We know the test data has 3 spans
        assert len(chunks) == 3


# ---------------------------------------------------------------------------
# Tests: _build_voice_config helper
# ---------------------------------------------------------------------------


class TestBuildVoiceConfig:
    def test_narrator_only_script(self):
        """Script with only NARRATOR entries gets NARRATOR voice config."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        script = [
            {"speaker": "NARRATOR", "text": "Hello", "instruct": ""},
            {"speaker": "NARRATOR", "text": "World", "instruct": ""},
        ]
        vc = _build_voice_config(script, s)
        assert "NARRATOR" in vc
        assert vc["NARRATOR"] == NARRATOR_VOICE
        assert len(vc) == 1

    def test_character_without_voice_assignment_gets_fallback(self):
        """Character without voice_assignment_id falls back to NARRATOR_VOICE."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        s.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
        )
        # Character with no voice_assignment_id
        s.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) VALUES ('c1', 'Charlie', '[]', NULL)"
        )
        script = [
            {"speaker": "Charlie", "text": "Hello", "instruct": ""},
        ]
        vc = _build_voice_config(script, s)
        assert "Charlie" in vc
        assert vc["Charlie"] == NARRATOR_VOICE

    def test_empty_script_returns_empty_config(self):
        """Empty script returns empty voice config."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        vc = _build_voice_config([], s)
        assert vc == {}


# ---------------------------------------------------------------------------
# Tests: _build_chunks helper
# ---------------------------------------------------------------------------


class TestBuildChunks:
    def test_empty_script_returns_empty_chunks(self):
        """Empty script produces empty chunks list."""
        chunks = _build_chunks([])
        assert chunks == []

    def test_chunks_preserve_order(self):
        """Chunks maintain the same order as the script."""
        script = [
            {"speaker": "Alice", "text": "First", "instruct": "a"},
            {"speaker": "NARRATOR", "text": "Second", "instruct": ""},
            {"speaker": "Bob", "text": "Third", "instruct": "b"},
        ]
        chunks = _build_chunks(script)
        assert chunks[0]["speaker"] == "Alice"
        assert chunks[1]["speaker"] == "NARRATOR"
        assert chunks[2]["speaker"] == "Bob"

    def test_chunks_have_sequential_indices(self):
        """Chunk indices are 0-based and sequential."""
        script = [
            {"speaker": "A", "text": "1", "instruct": ""},
            {"speaker": "B", "text": "2", "instruct": ""},
            {"speaker": "C", "text": "3", "instruct": ""},
        ]
        chunks = _build_chunks(script)
        assert [c["index"] for c in chunks] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Tests: output_dir handling
# ---------------------------------------------------------------------------


class TestOutputDir:
    def test_custom_output_dir_used_for_batch(self, storage, fake_engine):
        """Custom output_dir is passed to generate_batch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            render_audiobook("b1", storage, fake_engine, output_dir=tmpdir)
            assert fake_engine.batch_calls[0]["output_dir"] == tmpdir

    def test_auto_created_run_dir_when_none(self, storage, fake_engine, _render_root):
        """When output_dir is None, the run dir is RENDER_ROOT/book-{id}/{job_id}/."""
        job_id = render_audiobook("b1", storage, fake_engine)
        output_dir = fake_engine.batch_calls[0]["output_dir"]
        assert output_dir == os.path.join(_render_root, "book-b1", job_id)
        assert os.path.isdir(output_dir)


# ---------------------------------------------------------------------------
# Tests: batch_seed passthrough
# ---------------------------------------------------------------------------


class TestBatchSeed:
    def test_default_seed_is_negative_one(self, storage, fake_engine):
        """Default batch_seed is -1."""
        render_audiobook("b1", storage, fake_engine)
        assert fake_engine.batch_calls[0]["batch_seed"] == -1

    def test_custom_seed_passed_through(self, storage, fake_engine):
        """Custom batch_seed is passed to generate_batch."""
        render_audiobook("b1", storage, fake_engine, batch_seed=42)
        assert fake_engine.batch_calls[0]["batch_seed"] == 42


# ---------------------------------------------------------------------------
# Tests: _build_voice_config includes all voice type fields
# ---------------------------------------------------------------------------


class TestBuildVoiceConfigAllFields:
    def test_voice_config_includes_type_field(self):
        """Voice config includes the actual type from DB, not hardcoded 'custom'."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        # Insert a clone voice
        s.execute_insert(
            "INSERT INTO voice_config (id, name, description, type) "
            "VALUES ('clone-vc', 'CloneVoice', 'A cloned voice', 'clone')"
        )
        s.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c1', 'Alice', '[]', 'clone-vc')"
        )
        script = [{"speaker": "Alice", "text": "Hello", "instruct": ""}]
        vc = _build_voice_config(script, s)
        assert "Alice" in vc
        assert vc["Alice"]["type"] == "clone"

    def test_voice_config_includes_all_fields(self):
        """Voice config includes all fields from voice_config table."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        # Insert a fully-populated clone voice
        s.execute_insert(
            "INSERT INTO voice_config "
            "(id, name, description, type, voice, character_style, seed, "
            "ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
            "VALUES ('clone-full', 'CloneVoice', 'A cloned voice', 'clone', "
            "'CloneVoice', 'neutral', '42', 'some/path/audio.wav', "
            "'Reference text', NULL, 'ada/path', 'canonical-speaker')"
        )
        s.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c1', 'Alice', '[]', 'clone-full')"
        )
        script = [{"speaker": "Alice", "text": "Hello", "instruct": ""}]
        vc = _build_voice_config(script, s)
        assert "Alice" in vc
        alice = vc["Alice"]
        assert alice["type"] == "clone"
        assert alice["voice"] == "CloneVoice"
        assert alice["character_style"] == "neutral"
        assert alice["seed"] == "42"
        assert alice["ref_audio"] == "some/path/audio.wav"
        assert alice["ref_text"] == "Reference text"
        assert alice["adapter_id"] is None
        assert alice["adapter_path"] == "ada/path"
        assert alice["description"] == "A cloned voice"
        assert alice["alias_of"] == "canonical-speaker"

    def test_voice_config_null_type_defaults_to_custom(self):
        """Voice config with NULL type in DB defaults to 'custom'."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        # Insert a voice with only id, name, description (type defaults to 'custom')
        s.execute_insert(
            "INSERT INTO voice_config (id, name, description) "
            "VALUES ('vc1', 'Warm Female', 'A warm voice')"
        )
        s.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c1', 'Alice', '[]', 'vc1')"
        )
        script = [{"speaker": "Alice", "text": "Hello", "instruct": ""}]
        vc = _build_voice_config(script, s)
        assert vc["Alice"]["type"] == "custom"

    def test_voice_config_design_type(self):
        """Voice config correctly returns design type and description."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert(
            "INSERT INTO voice_config (id, name, description, type) "
            "VALUES ('design-vc', 'DesignedVoice', 'A warm elderly male voice', 'design')"
        )
        s.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c1', 'Gandalf', '[]', 'design-vc')"
        )
        script = [{"speaker": "Gandalf", "text": "You shall not pass", "instruct": ""}]
        vc = _build_voice_config(script, s)
        assert vc["Gandalf"]["type"] == "design"
        assert vc["Gandalf"]["description"] == "A warm elderly male voice"


# ---------------------------------------------------------------------------
# Tests: NARRATOR voice resolved from database (Plan F Phase 2)
# ---------------------------------------------------------------------------


class TestNarratorFromDatabase:
    """NARRATOR voice resolution: DB row takes priority over hardcoded constant."""

    def test_narrator_voice_from_db_overrides_constant(self):
        """When a NARRATOR row exists in voice_config, its values are used instead of NARRATOR_VOICE constant."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        # Insert a NARRATOR row with type="clone" and voice="CustomNarrator"
        s.execute_insert(
            "INSERT INTO voice_config "
            "(id, name, description, type, voice, character_style, seed, "
            "ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
            "VALUES ('NARRATOR', 'NARRATOR', 'Default narrator', 'clone', "
            "'CustomNarrator', 'neutral', '42', 'refs/narrator.wav', "
            "'Narrator reference text', NULL, NULL, NULL)"
        )
        script = [{"speaker": "NARRATOR", "text": "Once upon a time", "instruct": ""}]
        vc = _build_voice_config(script, s)
        assert "NARRATOR" in vc
        # DB values should be used, NOT the hardcoded constant
        assert vc["NARRATOR"]["type"] == "clone"
        assert vc["NARRATOR"]["voice"] == "CustomNarrator"
        assert vc["NARRATOR"]["description"] == "Default narrator"
        assert vc["NARRATOR"]["character_style"] == "neutral"
        assert vc["NARRATOR"]["seed"] == "42"
        assert vc["NARRATOR"]["ref_audio"] == "refs/narrator.wav"
        assert vc["NARRATOR"]["ref_text"] == "Narrator reference text"

    def test_narrator_fallback_to_constant_when_not_in_db(self):
        """When no NARRATOR row exists in voice_config, falls back to NARRATOR_VOICE constant."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        # Do NOT insert a NARRATOR row
        script = [{"speaker": "NARRATOR", "text": "Once upon a time", "instruct": ""}]
        vc = _build_voice_config(script, s)
        assert "NARRATOR" in vc
        # Should use the hardcoded constant
        assert vc["NARRATOR"] == NARRATOR_VOICE
        assert vc["NARRATOR"]["type"] == "custom"
        assert vc["NARRATOR"]["voice"] == "Ryan"


# ---------------------------------------------------------------------------
# Tests: Integration — clone voice routing through render_audiobook
# ---------------------------------------------------------------------------


class TestCloneVoiceIntegration:
    """Integration test: clone voice type flows through render_audiobook to TTSEngine."""

    def _populate_clone_storage(self, storage: InMemorySQLiteAdapter) -> None:
        """Insert a minimal document spine with a clone voice assigned to a character."""
        # -- Clone voice config -----------------------------------------------
        storage.execute_insert(
            "INSERT INTO voice_config "
            "(id, name, description, type, ref_audio, ref_text) "
            "VALUES ('clone-vc', 'CloneVoice', 'A cloned voice', 'clone', "
            "'refs/alice.wav', 'Alice reference text')"
        )

        # -- Series + Book ----------------------------------------------------
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
        )

        # -- Chapters ---------------------------------------------------------
        storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
        storage.execute_insert(
            "INSERT INTO book_chapter (child_id, parent_id, position) VALUES ('ch1', 'b1', 1)"
        )

        # -- Scenes -----------------------------------------------------------
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
        storage.execute_insert(
            "INSERT INTO chapter_scene (child_id, parent_id, position) VALUES ('sc1', 'ch1', 1)"
        )

        # -- Paragraphs -------------------------------------------------------
        storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p1')")
        storage.execute_insert(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )

        # -- Spans ------------------------------------------------------------
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp1', 'quotation', 'Hello from Alice!', 'cheerfully')"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )

        # -- Character with clone voice ---------------------------------------
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c1', 'Alice', '[]', 'clone-vc')"
        )

        # -- character_span (speaker junction) --------------------------------
        storage.execute_insert(
            "INSERT INTO character_span (character_id, span_id, relation_type, source, confidence) "
            "VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"
        )

    def test_clone_voice_type_flows_to_tts_engine(self, fake_engine):
        """render_audiobook passes clone voice type through to TTSEngine voice_config."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        self._populate_clone_storage(s)

        render_audiobook("b1", s, fake_engine)

        # Verify voice_config passed to generate_batch has clone type for Alice
        assert len(fake_engine.batch_calls) == 1
        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert "Alice" in voice_config
        assert voice_config["Alice"]["type"] == "clone"
        assert voice_config["Alice"]["voice"] == "CloneVoice"
        assert voice_config["Alice"]["ref_audio"] == "refs/alice.wav"
        assert voice_config["Alice"]["ref_text"] == "Alice reference text"

    def test_clone_voice_individual_mode(self, fake_engine):
        """render_audiobook with use_batch=False passes clone voice type to generate_voice."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        self._populate_clone_storage(s)

        render_audiobook("b1", s, fake_engine, use_batch=False)

        # Verify voice_config passed to generate_voice has clone type for Alice
        assert len(fake_engine.voice_calls) == 1
        voice_config = fake_engine.voice_calls[0]["voice_config"]
        assert "Alice" in voice_config
        assert voice_config["Alice"]["type"] == "clone"
        assert voice_config["Alice"]["ref_audio"] == "refs/alice.wav"


# ---------------------------------------------------------------------------
# Tests: Integration — all five voice types route through render_audiobook
# ---------------------------------------------------------------------------


class TestVoiceTypeRouting:
    """Integration test: each supported voice type flows through render_audiobook.

    The real TTSEngine dispatches on the ``type`` field inside ``generate_voice``
    (app/tts.py): clone → generate_clone_voice, lora/builtin_lora →
    generate_lora_voice, design → generate_design_voice, else →
    generate_custom_voice.  This test proves the routing contract by verifying
    that ``render_audiobook`` delivers the correct ``type`` (and type-specific
    fields) to the engine for every speaker — no TTS models are instantiated.
    """

    def _populate_routing_storage(self, storage: InMemorySQLiteAdapter) -> None:
        """Insert a document spine with five characters, one per voice type."""
        # -- Voice configs: one row per supported type ----------------------
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, voice) "
            "VALUES ('vc-custom', 'CustomVoice', 'A plain custom voice', "
            "'custom', 'CustomVoice')"
        )
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, "
            "ref_audio, ref_text) "
            "VALUES ('vc-clone', 'CloneVoice', 'A cloned voice', 'clone', "
            "'refs/clone.wav', 'Clone reference text')"
        )
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, adapter_path) "
            "VALUES ('vc-builtin-lora', 'BuiltinLoraVoice', 'A built-in LoRA voice', "
            "'builtin_lora', 'builtin_lora/voice_alpha')"
        )
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type, "
            "adapter_path, character_style) "
            "VALUES ('vc-lora', 'LoraVoice', 'A trained LoRA voice', 'lora', "
            "'adapters/voice_beta', 'neutral')"
        )
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description, type) "
            "VALUES ('vc-design', 'DesignVoice', 'A warm elderly male voice', "
            "'design')"
        )

        # -- Series + Book --------------------------------------------------
        storage.execute_insert("INSERT INTO series (id) VALUES ('s1')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b1', 's1', 1)"
        )

        # -- Chapters -------------------------------------------------------
        storage.execute_insert("INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')")
        storage.execute_insert(
            "INSERT INTO book_chapter (child_id, parent_id, position) "
            "VALUES ('ch1', 'b1', 1)"
        )

        # -- Scenes ---------------------------------------------------------
        storage.execute_insert("INSERT INTO scene (id) VALUES ('sc1')")
        storage.execute_insert(
            "INSERT INTO chapter_scene (child_id, parent_id, position) "
            "VALUES ('sc1', 'ch1', 1)"
        )

        # -- Paragraphs -----------------------------------------------------
        for i in range(1, 6):
            storage.execute_insert(f"INSERT INTO paragraph (id) VALUES ('p{i}')")
            storage.execute_insert(
                "INSERT INTO scene_paragraph (child_id, parent_id, position) "
                f"VALUES ('p{i}', 'sc1', {i})"
            )

        # -- Spans ----------------------------------------------------------
        spans = [
            ("sp1", "quotation", "Custom voice line.", "calmly"),
            ("sp2", "quotation", "Clone voice line.", "warmly"),
            ("sp3", "quotation", "Built-in LoRA voice line.", "brightly"),
            ("sp4", "quotation", "LoRA voice line.", "softly"),
            ("sp5", "quotation", "Design voice line.", "grandly"),
        ]
        for span_id, span_type, text, instruct in spans:
            storage.execute_insert(
                "INSERT INTO span (id, span_type, text, instruct) "
                f"VALUES ('{span_id}', '{span_type}', '{text}', '{instruct}')"
            )
            storage.execute_insert(
                "INSERT INTO paragraph_span (child_id, parent_id, position) "
                f"VALUES ('{span_id}', 'p{span_id[2]}', 1)"
            )

        # -- Characters, each assigned a different voice type ---------------
        assignments = [
            ("c1", "Cara", "vc-custom"),
            ("c2", "Clyde", "vc-clone"),
            ("c3", "Billie", "vc-builtin-lora"),
            ("c4", "Lorne", "vc-lora"),
            ("c5", "Delia", "vc-design"),
        ]
        for char_id, name, vc_id in assignments:
            storage.execute_insert(
                "INSERT INTO character (id, name, aliases, voice_assignment_id) "
                f"VALUES ('{char_id}', '{name}', '[]', '{vc_id}')"
            )

        # -- character_span (speaker junctions) -----------------------------
        for i, (char_id, _name, _vc_id) in enumerate(assignments, start=1):
            storage.execute_insert(
                "INSERT INTO character_span "
                "(character_id, span_id, relation_type, source, confidence) "
                f"VALUES ('{char_id}', 'sp{i}', 'speaker', 'walk', 0.95)"
            )

    def test_voice_type_routing(self, fake_engine):
        """render_audiobook routes each voice type to the correct engine method."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        self._populate_routing_storage(s)

        render_audiobook("b1", s, fake_engine)

        # The voice_config delivered to the engine carries the per-speaker
        # `type` (and type-specific fields) that TTSEngine.generate_voice
        # dispatches on:
        #   clone        → generate_clone_voice   (uses ref_audio, ref_text)
        #   lora         → generate_lora_voice    (uses adapter_path)
        #   builtin_lora → generate_lora_voice    (uses adapter_path)
        #   design       → generate_design_voice  (uses description)
        #   custom       → generate_custom_voice
        assert len(fake_engine.batch_calls) == 1
        voice_config = fake_engine.batch_calls[0]["voice_config"]

        # All five characters are present as speakers, no extras
        expected_speakers = {"Cara", "Clyde", "Billie", "Lorne", "Delia"}
        assert set(voice_config) == expected_speakers

        expected = {
            "Cara": {
                "type": "custom",
                "voice": "CustomVoice",
                "description": "A plain custom voice",
            },
            "Clyde": {
                "type": "clone",
                "voice": "CloneVoice",
                "ref_audio": "refs/clone.wav",
                "ref_text": "Clone reference text",
            },
            "Billie": {
                "type": "builtin_lora",
                "voice": "BuiltinLoraVoice",
                "adapter_path": "builtin_lora/voice_alpha",
            },
            "Lorne": {
                "type": "lora",
                "voice": "LoraVoice",
                "adapter_path": "adapters/voice_beta",
                "character_style": "neutral",
            },
            "Delia": {
                "type": "design",
                "voice": "DesignVoice",
                "description": "A warm elderly male voice",
            },
        }
        for speaker, fields in expected.items():
            assert speaker in voice_config, f"{speaker} missing from voice_config"
            assert voice_config[speaker]["type"] == fields["type"], (
                f"{speaker} should route as {fields['type']}"
            )
            for key, value in fields.items():
                assert voice_config[speaker][key] == value, (
                    f"{speaker}.{key}: expected {value!r}, got {voice_config[speaker][key]!r}"
                )


# ---------------------------------------------------------------------------
# P3-S1: render_job / render_chunk row persistence (rows = truth)
# ---------------------------------------------------------------------------


class _RowInspectingBatchEngine(FakeTTSEngine):
    """FakeTTSEngine that records render_job status during batch dispatch."""

    def __init__(self, storage, job_id):
        super().__init__()
        self.storage = storage
        self.job_id = job_id
        self.status_during_batch = None

    def generate_batch(
        self, chunks, voice_config, output_dir, batch_seed=-1, cancel_check=None
    ):
        rows = self.storage.execute_query(
            "SELECT status FROM render_job WHERE job_id = ?", (self.job_id,)
        )
        self.status_during_batch = rows[0]["status"] if rows else None
        return super().generate_batch(
            chunks, voice_config, output_dir, batch_seed, cancel_check
        )


class _RowInspectingVoiceEngine(FakeTTSEngine):
    """FakeTTSEngine that records render_chunk status around generate_voice."""

    def __init__(self, storage, job_id):
        super().__init__()
        self.storage = storage
        self.job_id = job_id
        self.status_at_entry = []  # chunk row status when generate_voice starts
        self.status_at_exit = []  # chunk row status when generate_voice returns

    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        idx = len(self.status_at_entry)
        rows = self.storage.execute_query(
            "SELECT status FROM render_chunk WHERE job_id = ? AND idx = ?",
            (self.job_id, idx),
        )
        self.status_at_entry.append(rows[0]["status"] if rows else None)
        result = super().generate_voice(
            text, instruct_text, speaker, voice_config, output_path
        )
        rows = self.storage.execute_query(
            "SELECT status FROM render_chunk WHERE job_id = ? AND idx = ?",
            (self.job_id, len(self.status_at_exit)),
        )
        self.status_at_exit.append(rows[0]["status"] if rows else None)
        return result


class _AllFailedBatchEngine(FakeTTSEngine):
    """FakeTTSEngine whose batch generation fails for every chunk."""

    def generate_batch(
        self, chunks, voice_config, output_dir, batch_seed=-1, cancel_check=None
    ):
        super().generate_batch(
            chunks, voice_config, output_dir, batch_seed, cancel_check
        )
        return {
            "completed": [],
            "failed": [(c["index"], f"boom {c['index']}") for c in chunks],
        }


class _FailingVoiceEngine(FakeTTSEngine):
    """FakeTTSEngine whose individual generation always raises."""

    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        super().generate_voice(text, instruct_text, speaker, voice_config, output_path)
        raise RuntimeError("voice boom")


class TestRenderJobPersistence:
    """P3-S1: render_audiobook persists render_job / render_chunk rows."""

    def test_job_row_created_running_at_start(self, storage, fake_engine):
        """A render_job row exists with status 'running' during generation."""
        engine = _RowInspectingBatchEngine(storage, "job-running")
        render_audiobook("b1", storage, engine, job_id="job-running")
        assert engine.status_during_batch == "running"

    def test_batch_mode_creates_no_chunk_rows(self, storage, fake_engine):
        """Batch mode persists job-level rows only — no render_chunk rows."""
        render_audiobook("b1", storage, fake_engine, job_id="job-batch")
        rows = storage.execute_query(
            "SELECT COUNT(*) AS cnt FROM render_chunk WHERE job_id = ?",
            ("job-batch",),
        )
        assert rows[0]["cnt"] == 0

    def test_individual_mode_chunk_rows_done_after_write(self, storage, fake_engine):
        """Individual mode: chunk rows pending during generate_voice, done after it returns."""
        engine = _RowInspectingVoiceEngine(storage, "job-ind")
        with tempfile.TemporaryDirectory() as tmpdir:
            render_audiobook(
                "b1",
                storage,
                engine,
                use_batch=False,
                job_id="job-ind",
                output_dir=tmpdir,
            )
            assert engine.status_at_entry == ["pending", "pending", "pending"]
            assert engine.status_at_exit == ["pending", "pending", "pending"]
            rows = storage.execute_query(
                "SELECT idx, status, wav_path FROM render_chunk "
                "WHERE job_id = ? ORDER BY idx",
                ("job-ind",),
            )
            assert [(r["idx"], r["status"]) for r in rows] == [
                (0, "done"),
                (1, "done"),
                (2, "done"),
            ]
            assert rows[0]["wav_path"] == os.path.join(tmpdir, "chunk_0000.wav")

    def test_final_transaction_sets_completed_and_artifact(self, storage, fake_engine):
        """Final transaction marks the job completed and records output_artifact_path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            job_id = render_audiobook(
                "b1", storage, fake_engine, job_id="job-final", output_dir=tmpdir
            )
            rows = storage.execute_query(
                "SELECT status, output_dir, output_artifact_path, finished_ms "
                "FROM render_job WHERE job_id = ?",
                (job_id,),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "completed"
            assert rows[0]["output_dir"] == tmpdir
            # No audiobook.m4b exists → artifact path falls back to the output dir
            assert rows[0]["output_artifact_path"] == tmpdir
            assert rows[0]["finished_ms"] is not None

    def test_individual_failure_records_failed_chunk_and_job(
        self, storage, fake_engine
    ):
        """generate_voice exception → failed chunk row + failed job row + re-raise."""
        engine = _FailingVoiceEngine()
        with pytest.raises(RuntimeError, match="voice boom"):
            render_audiobook("b1", storage, engine, use_batch=False, job_id="job-fail")
        chunk_rows = storage.execute_query(
            "SELECT idx, status, error FROM render_chunk WHERE job_id = ? ORDER BY idx",
            ("job-fail",),
        )
        assert chunk_rows[0]["status"] == "failed"
        assert chunk_rows[0]["error"] == "voice boom"
        job_rows = storage.execute_query(
            "SELECT status, error FROM render_job WHERE job_id = ?", ("job-fail",)
        )
        assert job_rows[0]["status"] == "failed"
        assert job_rows[0]["error"] == "voice boom"

    def test_batch_all_failed_marks_job_failed(self, storage, fake_engine):
        """generate_batch all-failed → job row failed with recorded error."""
        engine = _AllFailedBatchEngine()
        with pytest.raises(RuntimeError, match="Batch render failed"):
            render_audiobook("b1", storage, engine, job_id="job-bf")
        rows = storage.execute_query(
            "SELECT status, error FROM render_job WHERE job_id = ?", ("job-bf",)
        )
        assert rows[0]["status"] == "failed"
        assert "boom 0" in rows[0]["error"]

    def test_empty_script_job_completed(self, fake_engine):
        """A book with no spans still gets a completed job row."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        s.execute_insert("INSERT INTO series (id) VALUES ('s-empty')")
        s.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b-empty', 's-empty', 1)"
        )
        job_id = render_audiobook("b-empty", s, fake_engine, job_id="job-empty")
        rows = s.execute_query(
            "SELECT status, output_artifact_path FROM render_job WHERE job_id = ?",
            (job_id,),
        )
        assert rows[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# P1-S1: RENDER_ROOT run directories + fsync discipline (Plan C phase 1)
# ---------------------------------------------------------------------------


class TestRenderRootResolution:
    """P1-S1: get_render_root() reads RENDER_ROOT from the env at call time."""

    def test_env_override(self, monkeypatch, tmp_path):
        """RENDER_ROOT env var wins when set."""
        from app.pipeline.tts_integration import get_render_root

        custom = str(tmp_path / "custom-root")
        monkeypatch.setenv("RENDER_ROOT", custom)
        assert get_render_root() == custom

    def test_default_under_data(self, monkeypatch):
        """Without RENDER_ROOT, the default lives under data/ (gitignored)."""
        from app.pipeline.tts_integration import get_render_root

        monkeypatch.delenv("RENDER_ROOT", raising=False)
        assert get_render_root() == os.path.join(os.getcwd(), "data", "render_root")

    def test_resolution_is_per_call(self, monkeypatch, tmp_path):
        """get_render_root re-reads the env on every call."""
        from app.pipeline.tts_integration import get_render_root

        monkeypatch.setenv("RENDER_ROOT", str(tmp_path / "a"))
        assert get_render_root() == str(tmp_path / "a")
        monkeypatch.setenv("RENDER_ROOT", str(tmp_path / "b"))
        assert get_render_root() == str(tmp_path / "b")


class TestRenderRootRunDirs:
    """P1-S1: output_dir=None renders land in RENDER_ROOT/book-{id}/{job_id}/."""

    def test_batch_run_dir_under_render_root(self, storage, fake_engine, _render_root):
        """Batch render with output_dir=None writes into the RENDER_ROOT run dir."""
        render_audiobook("b1", storage, fake_engine, job_id="job-broot")
        run_dir = os.path.join(_render_root, "book-b1", "job-broot")
        assert fake_engine.batch_calls[0]["output_dir"] == run_dir
        assert os.path.isdir(run_dir)
        # the fake batch engine produces temp_batch_*.wav (app/tts.py naming)
        assert os.path.isfile(os.path.join(run_dir, "temp_batch_0.wav"))

    def test_individual_chunks_under_run_dir(self, storage, fake_engine, _render_root):
        """Individual mode writes chunk_0000.wav.. under the RENDER_ROOT run dir."""
        render_audiobook(
            "b1", storage, fake_engine, use_batch=False, job_id="job-iroot"
        )
        run_dir = os.path.join(_render_root, "book-b1", "job-iroot")
        for i in range(3):
            assert os.path.isfile(os.path.join(run_dir, f"chunk_{i:04d}.wav"))
            # the tmp write path is renamed away — no .tmp leftovers
            assert not os.path.exists(os.path.join(run_dir, f"chunk_{i:04d}.wav.tmp"))


class TestChunkFsyncDiscipline:
    """P1-S1: per-chunk 2-fsync discipline — tmp write → fsync → rename → fsync parent."""

    def test_tmp_then_rename_then_parent_fsync_order(
        self, storage, fake_engine, monkeypatch
    ):
        """fsync(tmp file) precedes rename; rename precedes fsync(parent dir)."""
        from app.pipeline import tts_integration as ti

        events: list[str] = []
        monkeypatch.setattr(
            ti,
            "_fsync_file",
            lambda p: events.append(f"fsync_file:{os.path.basename(p)}"),
        )
        monkeypatch.setattr(
            ti,
            "_fsync_dir",
            lambda p: events.append(f"fsync_dir:{os.path.basename(p)}"),
        )
        real_replace = os.replace

        def spy_replace(src, dst):
            events.append(f"replace:{os.path.basename(src)}->{os.path.basename(dst)}")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy_replace)

        render_audiobook(
            "b1", storage, fake_engine, use_batch=False, job_id="job-fsseq"
        )

        # Per chunk: fsync(tmp) < rename < fsync(parent dir).  The fsync_dir
        # events share one basename (the run dir), so compare by occurrence:
        # the i-th dir fsync must follow the i-th chunk's rename.
        dir_indices = [k for k, e in enumerate(events) if e.startswith("fsync_dir:")]
        for i in range(3):
            tmp = f"chunk_{i:04d}.wav.tmp"
            final = f"chunk_{i:04d}.wav"
            fi = events.index(f"fsync_file:{tmp}")
            ri = events.index(f"replace:{tmp}->{final}")
            assert fi < ri
            assert dir_indices[i] > ri

    def test_os_fsync_reached_for_tmp_and_parent(
        self, storage, fake_engine, monkeypatch
    ):
        """The discipline reaches the real os.fsync: file + parent dir per chunk."""
        calls: list = []
        real_fsync = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))

        render_audiobook(
            "b1", storage, fake_engine, use_batch=False, job_id="job-rawfs"
        )

        # 3 chunks × (fsync tmp file + fsync parent dir) + manifest file
        # fsync + manifest rename's parent-dir fsync = 8
        assert len(calls) == 8

    def test_row_done_only_after_file_durable(
        self, storage, fake_engine, _render_root, monkeypatch
    ):
        """Crash between rename and row-update: file exists, chunk row still pending.

        Locks the invariant that done-marking happens strictly after the WAV
        is durable — exactly the state Phase 2 reconciliation keys off.
        """
        from app.pipeline import tts_integration as ti

        def crashing_mark_done(storage_, job_id_, idx_, wav_path_):
            raise RuntimeError("simulated crash between rename and row update")

        monkeypatch.setattr(ti, "_mark_chunk_done", crashing_mark_done)

        with pytest.raises(RuntimeError, match="simulated crash"):
            render_audiobook(
                "b1", storage, fake_engine, use_batch=False, job_id="job-crash"
            )

        # File is durable (renamed into its final place) ...
        assert os.path.isfile(
            os.path.join(_render_root, "book-b1", "job-crash", "chunk_0000.wav")
        )
        # ... but the row was never marked done — detectable by reconciliation.
        rows = storage.execute_query(
            "SELECT status FROM render_chunk WHERE job_id = ? AND idx = 0",
            ("job-crash",),
        )
        assert rows[0]["status"] == "pending"


class TestBatchFsyncDiscipline:
    """P1-S1: batch mode applies fsync at file level (no per-chunk rows)."""

    def test_batch_fsyncs_produced_files_then_parent(
        self, storage, fake_engine, monkeypatch
    ):
        """Every produced temp_batch_*.wav is fsynced, then the parent dir once."""
        from app.pipeline import tts_integration as ti

        events: list[str] = []
        monkeypatch.setattr(
            ti,
            "_fsync_file",
            lambda p: events.append(f"fsync_file:{os.path.basename(p)}"),
        )
        monkeypatch.setattr(
            ti,
            "_fsync_dir",
            lambda p: events.append(f"fsync_dir:{os.path.basename(p)}"),
        )

        render_audiobook("b1", storage, fake_engine, job_id="job-bfs")

        for i in range(3):
            assert f"fsync_file:temp_batch_{i}.wav" in events
        first_dir_fsync = events.index("fsync_dir:job-bfs")
        # every produced file is fsynced before the parent dir entry
        assert first_dir_fsync > events.index("fsync_file:temp_batch_2.wav")


class TestManifest:
    """P1-S3: manifest.json is a derived cache written after completion."""

    @staticmethod
    def _load_manifest(root, book_id, job_id):
        with open(os.path.join(root, f"book-{book_id}", job_id, "manifest.json")) as f:
            return json.load(f)

    def test_individual_manifest_content(self, storage, fake_engine, _render_root):
        """Manifest carries job/book/mode/count/relative chunk paths/status."""
        render_audiobook("b1", storage, fake_engine, use_batch=False, job_id="job-mf")
        manifest = self._load_manifest(_render_root, "b1", "job-mf")
        assert manifest["job_id"] == "job-mf"
        assert manifest["book_id"] == "b1"
        assert manifest["mode"] == "individual"
        assert manifest["chunk_count"] == 3
        assert manifest["status"] == "completed"
        assert [c["idx"] for c in manifest["chunks"]] == [0, 1, 2]
        # wav paths are relative to the run dir (documented choice)
        assert manifest["chunks"][0]["wav_path"] == "chunk_0000.wav"
        assert isinstance(manifest["created_ms"], int)

    def test_batch_manifest_content(self, storage, fake_engine, _render_root):
        """Batch manifest lists the produced temp_batch_*.wav files."""
        render_audiobook("b1", storage, fake_engine, job_id="job-mfb")
        manifest = self._load_manifest(_render_root, "b1", "job-mfb")
        assert manifest["mode"] == "batch"
        assert manifest["chunk_count"] == 3
        assert [c["wav_path"] for c in manifest["chunks"]] == [
            "temp_batch_0.wav",
            "temp_batch_1.wav",
            "temp_batch_2.wav",
        ]

    def test_manifest_write_is_atomic(self, storage, fake_engine, _render_root):
        """No .tmp leftovers next to manifest.json after a completed render."""
        render_audiobook("b1", storage, fake_engine, job_id="job-mfa")
        assert not os.path.exists(
            os.path.join(_render_root, "book-b1", "job-mfa", "manifest.json.tmp")
        )

    def test_no_manifest_on_failed_render(self, storage, _render_root):
        """Failed renders leave no manifest (rows stay the authority)."""
        engine = _FailingVoiceEngine()
        with pytest.raises(RuntimeError, match="voice boom"):
            render_audiobook("b1", storage, engine, use_batch=False, job_id="job-mff")
        assert not os.path.exists(
            os.path.join(_render_root, "book-b1", "job-mff", "manifest.json")
        )


# ---------------------------------------------------------------------------
# P1-S1: book.single_speaker render-boundary enforcement (Plan J)
# ---------------------------------------------------------------------------


class TestSingleSpeakerRenderBoundary:
    """P1-S1: book.single_speaker=1 forces the NARRATOR voice config at the
    render boundary only — export_annotated_script stays faithful."""

    @staticmethod
    def _set_single_speaker(
        storage: InMemorySQLiteAdapter, book_id: str = "b1", value: int = 1
    ) -> None:
        storage.execute_update(
            "UPDATE book SET single_speaker = ? WHERE id = ?", (value, book_id)
        )

    @staticmethod
    def _populate_character_only_storage(
        storage: InMemorySQLiteAdapter,
    ) -> None:
        """Insert a book whose spans all have character speakers (no NARRATOR)."""
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description) VALUES ('vc1', 'Warm Female', 'A warm female voice')"
        )
        storage.execute_insert(
            "INSERT INTO voice_config (id, name, description) VALUES ('vc2', 'Deep Male', 'A deep male voice')"
        )
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
        storage.execute_insert("INSERT INTO paragraph (id) VALUES ('p2')")
        storage.execute_insert(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p1', 'sc1', 1)"
        )
        storage.execute_insert(
            "INSERT INTO scene_paragraph (child_id, parent_id, position) VALUES ('p2', 'sc1', 2)"
        )
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp1', 'quotation', 'Hello there!', 'cheerfully')"
        )
        storage.execute_insert(
            "INSERT INTO span (id, span_type, text, instruct) "
            "VALUES ('sp2', 'quotation', 'Goodbye.', 'sadly')"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp1', 'p1', 1)"
        )
        storage.execute_insert(
            "INSERT INTO paragraph_span (child_id, parent_id, position) VALUES ('sp2', 'p2', 1)"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c1', 'Alice', '[]', 'vc1')"
        )
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases, voice_assignment_id) "
            "VALUES ('c2', 'Bob', '[]', 'vc2')"
        )
        storage.execute_insert(
            "INSERT INTO character_span (character_id, span_id, relation_type, source, confidence) "
            "VALUES ('c1', 'sp1', 'speaker', 'walk', 0.95)"
        )
        storage.execute_insert(
            "INSERT INTO character_span (character_id, span_id, relation_type, source, confidence) "
            "VALUES ('c2', 'sp2', 'speaker', 'walk', 0.9)"
        )

    def test_single_speaker_forces_narrator_config_batch(self, storage, fake_engine):
        """single_speaker=1: every entry in the batch voice_config is NARRATOR_VOICE."""
        self._set_single_speaker(storage, "b1", 1)
        render_audiobook("b1", storage, fake_engine)

        assert len(fake_engine.batch_calls) == 1
        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert set(voice_config) == {"NARRATOR", "Alice", "Bob"}
        for speaker, config in voice_config.items():
            assert config == NARRATOR_VOICE, (
                f"{speaker} not forced to NARRATOR_VOICE: {config!r}"
            )

    def test_single_speaker_forces_narrator_config_individual(
        self, storage, fake_engine
    ):
        """single_speaker=1: each generate_voice call's voice_config is all-NARRATOR."""
        self._set_single_speaker(storage, "b1", 1)
        render_audiobook("b1", storage, fake_engine, use_batch=False)

        assert len(fake_engine.voice_calls) == 3
        for call in fake_engine.voice_calls:
            voice_config = call["voice_config"]
            assert set(voice_config) == {"NARRATOR", "Alice", "Bob"}
            for speaker, config in voice_config.items():
                assert config == NARRATOR_VOICE, (
                    f"{speaker} not forced to NARRATOR_VOICE: {config!r}"
                )

    def test_single_speaker_uses_db_narrator_row_when_present(
        self, storage, fake_engine
    ):
        """single_speaker=1: a NARRATOR voice_config row wins over the constant."""
        storage.execute_insert(
            "INSERT INTO voice_config "
            "(id, name, description, type, voice, character_style, seed, "
            "ref_audio, ref_text, adapter_id, adapter_path, alias_of) "
            "VALUES ('NARRATOR', 'NARRATOR', 'Default narrator', 'clone', "
            "'CustomNarrator', 'neutral', '42', 'refs/narrator.wav', "
            "'Narrator reference text', NULL, NULL, NULL)"
        )
        self._set_single_speaker(storage, "b1", 1)
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        expected = voice_config["NARRATOR"]
        assert expected["type"] == "clone"
        assert expected["voice"] == "CustomNarrator"
        assert expected["description"] == "Default narrator"
        for speaker, config in voice_config.items():
            assert config == expected, (
                f"{speaker} not forced to DB NARRATOR config: {config!r}"
            )

    def test_single_speaker_without_narrator_span_still_forces_narrator(
        self, fake_engine
    ):
        """single_speaker=1 with no NARRATOR span: a NARRATOR config is still forced."""
        s = InMemorySQLiteAdapter()
        s.init_db()
        self._populate_character_only_storage(s)
        self._set_single_speaker(s, "b1", 1)

        render_audiobook("b1", s, fake_engine)

        assert len(fake_engine.batch_calls) == 1
        voice_config = fake_engine.batch_calls[0]["voice_config"]
        # No NARRATOR span exists in the script — the boundary still provides one
        assert "NARRATOR" in voice_config
        for speaker, config in voice_config.items():
            assert config == NARRATOR_VOICE, (
                f"{speaker} not forced to NARRATOR_VOICE: {config!r}"
            )

    def test_single_speaker_export_stays_faithful(self, storage, fake_engine):
        """export_annotated_script is untouched by single_speaker=1 (editor contract)."""
        from app.pipeline.assembly import export_annotated_script

        self._set_single_speaker(storage, "b1", 1)

        script = export_annotated_script("b1", storage)
        assert [e["speaker"] for e in script] == ["Alice", "NARRATOR", "Bob"]

        render_audiobook("b1", storage, fake_engine)
        # Chunks still carry the faithful speaker names — forcing happens via the
        # voice_config mapping, never by rewriting the script/chunks.
        chunks = fake_engine.batch_calls[0]["chunks"]
        assert [c["speaker"] for c in chunks] == ["Alice", "NARRATOR", "Bob"]

    def test_single_speaker_zero_keeps_per_speaker_configs(self, storage, fake_engine):
        """single_speaker=0: each speaker keeps its own voice config (unchanged)."""
        self._set_single_speaker(storage, "b1", 0)
        render_audiobook("b1", storage, fake_engine)

        voice_config = fake_engine.batch_calls[0]["voice_config"]
        assert voice_config["Alice"]["voice"] == "Warm Female"
        assert voice_config["Bob"]["voice"] == "Deep Male"
        assert voice_config["NARRATOR"] == NARRATOR_VOICE
        assert len(voice_config) == 3


# ---------------------------------------------------------------------------
# Tests: global pause config passthrough (Plan J Phase 4)
# ---------------------------------------------------------------------------


class TestPauseConfigBoundary:
    """Global pause config must flow into the render boundary (batch chunks).

    ``pause_between_speakers_ms`` / ``pause_same_speaker_ms`` are carried
    through ``render_audiobook`` into the engine dispatch unchanged, with the
    ``TTSConfig`` defaults (500 / 250 ms) applied when a value is omitted.
    The per-span ``pause_after_ms`` variant is separately persisted (P1) and
    applied per-boundary by ``_assemble_paused_artifact`` (P3); these tests pin
    the global variant only.
    """

    def test_batch_chunks_carry_default_pause_values(self, storage, fake_engine):
        """Without a TTSConfig the 500/250 ms defaults reach every chunk."""
        render_audiobook("b1", storage, fake_engine)
        chunks = fake_engine.batch_calls[0]["chunks"]
        assert len(chunks) == 3
        for chunk in chunks:
            assert chunk["pause_between_speakers_ms"] == 500
            assert chunk["pause_same_speaker_ms"] == 250

    def test_configured_pause_values_flow_unchanged(self, storage, fake_engine):
        """Custom TTSConfig pause values reach the engine unchanged."""
        render_audiobook(
            "b1",
            storage,
            fake_engine,
            tts_config={
                "pause_between_speakers_ms": 750,
                "pause_same_speaker_ms": 350,
            },
        )
        chunks = fake_engine.batch_calls[0]["chunks"]
        for chunk in chunks:
            assert chunk["pause_between_speakers_ms"] == 750
            assert chunk["pause_same_speaker_ms"] == 350

    def test_partial_tts_config_falls_back_per_field(self, storage, fake_engine):
        """A TTSConfig missing one pause field keeps that field's default."""
        render_audiobook(
            "b1",
            storage,
            fake_engine,
            tts_config={"pause_between_speakers_ms": 1000},
        )
        chunks = fake_engine.batch_calls[0]["chunks"]
        for chunk in chunks:
            assert chunk["pause_between_speakers_ms"] == 1000
            assert chunk["pause_same_speaker_ms"] == 250  # default retained

    def test_config_json_pause_values_reach_render_boundary(
        self, storage, fake_engine, tmp_path, monkeypatch
    ):
        """End-to-end: config.json pause values → load_tts_config → render."""
        from app.utils import load_tts_config

        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "tts": {
                        "mode": "external",
                        "pause_between_speakers_ms": 900,
                        "pause_same_speaker_ms": 400,
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(config_path))

        tts_config = load_tts_config()
        assert tts_config["pause_between_speakers_ms"] == 900

        render_audiobook("b1", storage, fake_engine, tts_config=tts_config)
        chunks = fake_engine.batch_calls[0]["chunks"]
        for chunk in chunks:
            assert chunk["pause_between_speakers_ms"] == 900
            assert chunk["pause_same_speaker_ms"] == 400

    def test_individual_mode_accepts_tts_config(self, storage, fake_engine):
        """Individual mode renders with a TTSConfig present (no regression)."""
        render_audiobook(
            "b1",
            storage,
            fake_engine,
            use_batch=False,
            job_id="job-iv-pause",
            tts_config={"pause_between_speakers_ms": 600},
        )
        assert len(fake_engine.voice_calls) == 3


# ---------------------------------------------------------------------------
# P3: Deterministic render-time paused assembly (Plan L)
# ---------------------------------------------------------------------------


class _RealWavBatchEngine:
    """Writes genuine pydub WAVs (with a distinct sample_rate) per chunk."""

    def __init__(self, duration_ms=200, sample_rate=22050, channels=1, sample_width=2):
        self.duration_ms = duration_ms
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.batch_calls = []
        self.voice_calls = []

    def _write(self, output_path):
        seg = _tone_segment(self.duration_ms, self.sample_rate)
        seg = seg.set_channels(self.channels).set_sample_width(self.sample_width)
        seg.export(output_path, format="wav")

    def generate_batch(
        self, chunks, voice_config, output_dir, batch_seed=-1, cancel_check=None
    ):
        self.batch_calls.append(list(chunks))
        for chunk in chunks:
            self._write(os.path.join(output_dir, f"temp_batch_{chunk['index']}.wav"))
        return {"completed": [c["index"] for c in chunks], "failed": []}

    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        self.voice_calls.append(output_path)
        self._write(output_path)
        return True


def _tone_segment(duration_ms: int, sample_rate: int = 22050) -> AudioSegment:
    """Return a genuine audible tone (so inserted gaps are measurable silence)."""
    import array
    import math

    n = int(sample_rate * duration_ms / 1000.0)
    frames = array.array(
        "h",
        (int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate)) for i in range(n)),
    )
    return AudioSegment(
        frames.tobytes(),
        frame_rate=sample_rate,
        sample_width=2,
        channels=1,
    )


def _silence_duration_ms(segment: AudioSegment) -> int:
    """Return the total length (ms) of silence in *segment*.

    Uses pydub's ``dBFS`` per-millisecond sampling: a frame at or below
    ``-50 dBFS`` counts as silence.  With tone chunks, every inserted gap is
    genuine low-amplitude silence and the tones are loud, so this measures the
    exact inserted gap length.
    """
    total = 0
    for ms in range(len(segment)):
        if segment[ms : ms + 1].dBFS <= -50:
            total += 1
    return total


class TestPausedAssembly:
    """P3-S1/S2: the private ``_assemble_paused_artifact`` postprocessor."""

    def _write_chunks(self, tmp_path, count, duration_ms=200, sample_rate=22050):
        paths = []
        for i in range(count):
            p = os.path.join(str(tmp_path), f"chunk_{i:04d}.wav")
            _tone_segment(duration_ms, sample_rate).export(p, format="wav")
            paths.append(p)
        return paths

    @staticmethod
    def _script(speakers):
        return [{"id": f"sp{i}", "speaker": s} for i, s in enumerate(speakers)]

    def test_different_speaker_gap_present(self, tmp_path):
        """Different speakers insert pause_between_speakers_ms between them."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=400,
            pause_same_speaker_ms=100,
            span_pause_after_ms={},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        # 100ms + 400ms gap + 100ms = 600ms
        assert len(combined) == 600
        assert _silence_duration_ms(combined) == 400

    def test_same_speaker_gap_uses_same_speaker_pause(self, tmp_path):
        """Same speaker inserts pause_same_speaker_ms between them."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Alice"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=400,
            pause_same_speaker_ms=80,
            span_pause_after_ms={},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        assert len(combined) == 280  # 100 + 80 + 100
        assert _silence_duration_ms(combined) == 80

    def test_positive_override_replaces_default(self, tmp_path):
        """A positive span.pause_after_ms override inserts exactly that gap."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=400,
            pause_same_speaker_ms=100,
            span_pause_after_ms={"sp0": 900},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        assert len(combined) == 1100  # 100 + 900 override + 100
        # boundary sampling can shave 1ms off a gap; allow ±2ms
        assert abs(_silence_duration_ms(combined) - 900) <= 2

    def test_zero_override_is_no_gap(self, tmp_path):
        """0 override = intentional no-gap (never coerced to default)."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=400,
            pause_same_speaker_ms=100,
            span_pause_after_ms={"sp0": 0},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        assert len(combined) == 200  # 100 + 0 + 100
        assert _silence_duration_ms(combined) == 0

    def test_last_span_override_ignored(self, tmp_path):
        """The final entry's pause is ignored (no pause after the last span)."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=400,
            pause_same_speaker_ms=100,
            # sp1 is the LAST span — its override must be ignored.
            span_pause_after_ms={"sp1": 9999},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        assert len(combined) == 600  # 100 + 400 + 100 (last override ignored)
        assert _silence_duration_ms(combined) == 400

    def test_single_span_no_pause(self, tmp_path):
        """A single span produces the segment unchanged (no trailing pause)."""
        paths = self._write_chunks(tmp_path, 1, duration_ms=250, sample_rate=22050)
        script = self._script(["Alice"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=400,
            pause_same_speaker_ms=100,
            span_pause_after_ms={},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        assert len(combined) == 250
        assert _silence_duration_ms(combined) == 0

    def test_format_preserved(self, tmp_path):
        """frame_rate / channels / sample_width preserved from source."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=44100)
        # widen to stereo 8-bit to prove all three are preserved
        seg0 = AudioSegment.silent(duration=100, frame_rate=44100).set_channels(2)
        seg0.export(paths[0], format="wav")
        seg1 = AudioSegment.silent(duration=100, frame_rate=44100).set_channels(2)
        seg1.export(paths[1], format="wav")
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        _assemble_paused_artifact(
            paths,
            script,
            pause_between_speakers_ms=100,
            pause_same_speaker_ms=100,
            span_pause_after_ms={},
            run_dir=str(tmp_path),
            output_path=out,
        )
        combined = AudioSegment.from_wav(out)
        assert (combined.frame_rate, combined.channels, combined.sample_width) == (
            44100,
            2,
            2,
        )

    def test_mismatched_formats_rejected(self, tmp_path):
        """Mixed frame rates are rejected before assembly."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        other = os.path.join(str(tmp_path), "chunk_0001.wav")
        AudioSegment.silent(duration=100, frame_rate=44100).export(other, format="wav")
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        with pytest.raises(ValueError, match="differ in format"):
            _assemble_paused_artifact(
                paths,
                script,
                pause_between_speakers_ms=400,
                pause_same_speaker_ms=100,
                span_pause_after_ms={},
                run_dir=str(tmp_path),
                output_path=out,
            )

    def test_missing_wav_rejected_before_processing(self, tmp_path):
        """Missing / out-of-run-dir paths rejected up front."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        paths[1] = os.path.join(str(tmp_path), "does-not-exist.wav")
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        with pytest.raises(ValueError, match="missing WAV"):
            _assemble_paused_artifact(
                paths,
                script,
                pause_between_speakers_ms=400,
                pause_same_speaker_ms=100,
                span_pause_after_ms={},
                run_dir=str(tmp_path),
                output_path=out,
            )

    def test_path_traversal_rejected(self, tmp_path):
        """A path escaping the run dir is rejected."""
        outside = tempfile.mkdtemp()
        seg = AudioSegment.silent(duration=100, frame_rate=22050)
        outside_path = os.path.join(outside, "chunk.wav")
        seg.export(outside_path, format="wav")
        paths = self._write_chunks(tmp_path, 1, duration_ms=100, sample_rate=22050)
        paths.append(outside_path)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        with pytest.raises(ValueError, match="outside run dir"):
            _assemble_paused_artifact(
                paths,
                script,
                pause_between_speakers_ms=400,
                pause_same_speaker_ms=100,
                span_pause_after_ms={},
                run_dir=str(tmp_path),
                output_path=out,
            )

    def test_path_count_mismatch_rejected(self, tmp_path):
        """WAV count != script count is rejected."""
        paths = self._write_chunks(tmp_path, 1, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")
        with pytest.raises(ValueError, match="does not match script"):
            _assemble_paused_artifact(
                paths,
                script,
                pause_between_speakers_ms=400,
                pause_same_speaker_ms=100,
                span_pause_after_ms={},
                run_dir=str(tmp_path),
                output_path=out,
            )

    def test_temp_cleaned_on_export_failure(self, tmp_path, monkeypatch):
        """A failed pydub export leaves no temp files behind."""
        paths = self._write_chunks(tmp_path, 2, duration_ms=100, sample_rate=22050)
        script = self._script(["Alice", "Bob"])
        out = os.path.join(str(tmp_path), "audiobook-paused.wav")

        def _boom(*args, **kwargs):
            raise RuntimeError("ffmpeg exploded")

        monkeypatch.setattr(AudioSegment, "export", _boom)
        with pytest.raises(RuntimeError, match="ffmpeg exploded"):
            _assemble_paused_artifact(
                paths,
                script,
                pause_between_speakers_ms=400,
                pause_same_speaker_ms=100,
                span_pause_after_ms={},
                run_dir=str(tmp_path),
                output_path=out,
            )
        assert not os.path.exists(f"{out}.tmp")
        assert not os.path.exists(out)


class TestPausedAssemblyWiring:
    """P3-S3/S4: postprocessor wired into render_audiobook."""

    def test_batch_render_produces_canonical_paused_artifact(
        self, storage, _render_root
    ):
        """Batch render writes audiobook-paused.wav + points output_artifact_path at it."""
        engine = _RealWavBatchEngine(duration_ms=200, sample_rate=22050)
        job_id = render_audiobook("b1", storage, engine)
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        assert os.path.isfile(paused)
        rows = storage.execute_query(
            "SELECT status, output_artifact_path FROM render_job WHERE job_id = ?",
            (job_id,),
        )
        assert rows[0]["status"] == "completed"
        assert rows[0]["output_artifact_path"] == paused
        # manifest records the paused artifact (relative)
        with open(os.path.join(run_dir, "manifest.json")) as f:
            manifest = json.loads(f.read())
        assert manifest["paused_artifact"] == PAUSED_ARTIFACT_NAME
        # 3 spans × 200ms + 2 different-speaker gaps × 500ms defaults
        combined = AudioSegment.from_wav(paused)
        assert len(combined) == 600 + 2 * 500

    def test_individual_render_produces_canonical_paused_artifact(
        self, storage, _render_root
    ):
        """Individual render wires the postprocessor too."""
        engine = _RealWavBatchEngine(duration_ms=200, sample_rate=22050)
        job_id = render_audiobook("b1", storage, engine, use_batch=False)
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        assert os.path.isfile(paused)
        rows = storage.execute_query(
            "SELECT status, output_artifact_path FROM render_job WHERE job_id = ?",
            (job_id,),
        )
        assert rows[0]["output_artifact_path"] == paused
        combined = AudioSegment.from_wav(paused)
        assert len(combined) == 600 + 2 * 500

    def test_batch_individual_parity(self, storage, _render_root):
        """Batch and individual render produce byte-identical paused artifacts."""
        batch_job = render_audiobook(
            "b1", storage, _RealWavBatchEngine(duration_ms=200), job_id="p3-par-b"
        )
        ind_job = render_audiobook(
            "b1",
            storage,
            _RealWavBatchEngine(duration_ms=200),
            use_batch=False,
            job_id="p3-par-i",
        )
        batch_paused = os.path.join(
            _render_root, "book-b1", batch_job, PAUSED_ARTIFACT_NAME
        )
        ind_paused = os.path.join(
            _render_root, "book-b1", ind_job, PAUSED_ARTIFACT_NAME
        )
        with open(batch_paused, "rb") as f:
            batch_bytes = f.read()
        with open(ind_paused, "rb") as f:
            ind_bytes = f.read()
        assert batch_bytes == ind_bytes

    def test_deterministic_across_runs(self, storage, _render_root):
        """Two identical batch renders produce identical paused artifacts."""
        j1 = render_audiobook(
            "b1", storage, _RealWavBatchEngine(duration_ms=200), job_id="p3-det-1"
        )
        j2 = render_audiobook(
            "b1", storage, _RealWavBatchEngine(duration_ms=200), job_id="p3-det-2"
        )
        p1 = os.path.join(_render_root, "book-b1", j1, PAUSED_ARTIFACT_NAME)
        p2 = os.path.join(_render_root, "book-b1", j2, PAUSED_ARTIFACT_NAME)
        with open(p1, "rb") as f:
            b1 = f.read()
        with open(p2, "rb") as f:
            b2 = f.read()
        assert b1 == b2

    def test_fake_engine_skips_assembly_nonfatally(
        self, storage, fake_engine, _render_root
    ):
        """Non-WAV outputs fail assembly non-fatally; job still completes."""
        job_id = render_audiobook("b1", storage, fake_engine)
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        assert not os.path.exists(paused)
        rows = storage.execute_query(
            "SELECT status FROM render_job WHERE job_id = ?", (job_id,)
        )
        assert rows[0]["status"] == "completed"


class TestBookTierPauseOverrideWiring:
    """P7-S1/S2: book-tier pause overrides genuinely reach the paused artifact.

    QA Round 1 flagged that book.pause_between_speakers_ms / book.pause_same_speaker_ms
    were persisted (P1), exposed (P2), and reported (P5) but never applied at
    render-time assembly — the audio always used the config default.  These
    tests pin that a persisted book override now reaches ``audiobook-paused.wav``,
    that a NULL book column falls back to the config tier, and that the per-span
    ``pause_after_ms`` override still wins at its own boundary (no regression).
    """

    def test_book_override_reaches_paused_artifact(self, storage, _render_root):
        """A persisted 700 ms between-speakers override lands in the audio."""
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = 700 WHERE id = 'b1'"
        )
        engine = _RealWavBatchEngine(duration_ms=200, sample_rate=22050)
        job_id = render_audiobook("b1", storage, engine)
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        assert os.path.isfile(paused)
        rows = storage.execute_query(
            "SELECT status, output_artifact_path FROM render_job WHERE job_id = ?",
            (job_id,),
        )
        assert rows[0]["status"] == "completed"
        assert rows[0]["output_artifact_path"] == paused  # canonical artifact
        # sp1(Alice)→sp2(NARRATOR)→sp3(Bob): two different-speaker gaps @ 700 ms.
        combined = AudioSegment.from_wav(paused)
        assert len(combined) == 600 + 2 * 700
        assert abs(_silence_duration_ms(combined) - 2 * 700) <= 2

    def test_same_speaker_book_override_reaches_paused_artifact(
        self, storage, _render_root
    ):
        """A persisted pause_same_speaker_ms override lands on same-speaker gaps."""
        storage.execute_update(
            "UPDATE book SET pause_same_speaker_ms = 300 WHERE id = 'b1'"
        )
        # Force sp2 to be the same speaker as sp1 by giving it Alice's junction.
        storage.execute_insert(
            """INSERT INTO character_span (character_id, span_id, relation_type, source, confidence)
               VALUES ('c1', 'sp2', 'speaker', 'walk', 0.9)"""
        )
        engine = _RealWavBatchEngine(duration_ms=200, sample_rate=22050)
        job_id = render_audiobook("b1", storage, engine)
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        combined = AudioSegment.from_wav(paused)
        # sp1(Alice)→sp2(Alice) same-speaker @ 300 ms; sp2→sp3(Bob) @ default 500.
        assert len(combined) == 600 + 300 + 500
        assert abs(_silence_duration_ms(combined) - (300 + 500)) <= 2

    def test_book_null_falls_back_to_config_default(self, storage, _render_root):
        """NULL book override -> config tier applies (existing default flow)."""
        engine = _RealWavBatchEngine(duration_ms=200, sample_rate=22050)
        job_id = render_audiobook(
            "b1",
            storage,
            engine,
            tts_config={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400},
        )
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        combined = AudioSegment.from_wav(paused)
        # Two different-speaker gaps, config value 900 ms (book columns NULL).
        assert len(combined) == 600 + 2 * 900
        assert abs(_silence_duration_ms(combined) - 2 * 900) <= 2

    def test_per_span_override_still_wins_over_book(self, storage, _render_root):
        """Per-span pause_after_ms overrides book/config at its own boundary."""
        storage.execute_update(
            "UPDATE book SET pause_between_speakers_ms = 700 WHERE id = 'b1'"
        )
        storage.execute_update("UPDATE span SET pause_after_ms = 900 WHERE id = 'sp1'")
        engine = _RealWavBatchEngine(duration_ms=200, sample_rate=22050)
        job_id = render_audiobook("b1", storage, engine)
        run_dir = os.path.join(_render_root, "book-b1", job_id)
        paused = os.path.join(run_dir, PAUSED_ARTIFACT_NAME)
        combined = AudioSegment.from_wav(paused)
        # sp1→sp2 gap uses the span override 900; sp2→sp3 gap uses book 700.
        assert len(combined) == 600 + 900 + 700
        assert abs(_silence_duration_ms(combined) - (900 + 700)) <= 2


class TestPausedAssemblyEmptyAndGuard:
    """P3-S5: empty-script behavior + no TTSEngine signature change."""

    def test_empty_script_no_paused_artifact(self, storage, _render_root):
        """An empty book renders no paused artifact (early return, job completes)."""
        # b-empty has no spans in the document spine
        storage.execute_insert("INSERT INTO series (id) VALUES ('s-empty')")
        storage.execute_insert(
            "INSERT INTO book (id, series_id, position) VALUES ('b-empty', 's-empty', 1)"
        )
        job_id = render_audiobook(
            "b-empty",
            storage,
            _RealWavBatchEngine(duration_ms=100),
            job_id="p3-empty",
        )
        run_dir = os.path.join(_render_root, "book-b-empty", job_id)
        assert not os.path.exists(os.path.join(run_dir, PAUSED_ARTIFACT_NAME))
        rows = storage.execute_query(
            "SELECT status FROM render_job WHERE job_id = ?", (job_id,)
        )
        assert rows[0]["status"] == "completed"

    def test_tts_helper_signature_unchanged(self):
        """Guard: app.tts's combine_audio_with_pauses signature is stable."""
        from app.tts import combine_audio_with_pauses

        assert callable(combine_audio_with_pauses)
        # The postprocessor calls it with exactly these kwargs.
        import inspect

        sig = inspect.signature(combine_audio_with_pauses)
        params = list(sig.parameters)
        for required in (
            "audio_segments",
            "speakers",
            "pause_ms",
            "same_speaker_pause_ms",
            "pause_overrides",
        ):
            assert required in params, f"missing param {required}"
