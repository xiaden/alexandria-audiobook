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

import os
import tempfile

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.tts_integration import (
    NARRATOR_VOICE,
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

    def generate_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Record the call and return all indices as completed."""
        self.batch_calls.append(
            {
                "chunks": list(chunks),
                "voice_config": dict(voice_config),
                "output_dir": output_dir,
                "batch_seed": batch_seed,
            }
        )
        completed = [c["index"] for c in chunks]
        return {"completed": completed, "failed": []}

    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        """Record the call and return True (success)."""
        self.voice_calls.append(
            {
                "text": text,
                "instruct_text": instruct_text,
                "speaker": speaker,
                "voice_config": dict(voice_config),
                "output_path": output_path,
            }
        )
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
    storage.execute_insert(
        "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
    )
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
        """Output paths follow chunk_{index:04d}.wav pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            render_audiobook(
                "b1", storage, fake_engine, use_batch=False, output_dir=tmpdir
            )

            paths = [c["output_path"] for c in fake_engine.voice_calls]
            assert paths[0] == os.path.join(tmpdir, "chunk_0000.wav")
            assert paths[1] == os.path.join(tmpdir, "chunk_0001.wav")
            assert paths[2] == os.path.join(tmpdir, "chunk_0002.wav")

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
            render_audiobook(
                "b1", storage, fake_engine, output_dir=tmpdir
            )
            assert fake_engine.batch_calls[0]["output_dir"] == tmpdir

    def test_auto_created_output_dir_when_none(self, storage, fake_engine):
        """When output_dir is None, a temp directory is created."""
        render_audiobook("b1", storage, fake_engine)
        output_dir = fake_engine.batch_calls[0]["output_dir"]
        assert os.path.isdir(output_dir)
        assert "audiobook_b1_" in output_dir


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
        storage.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
        )
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
        storage.execute_insert(
            "INSERT INTO chapter (id, book_id) VALUES ('ch1', 'b1')"
        )
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
            assert (
                voice_config[speaker]["type"] == fields["type"]
            ), f"{speaker} should route as {fields['type']}"
            for key, value in fields.items():
                assert (
                    voice_config[speaker][key] == value
                ), f"{speaker}.{key}: expected {value!r}, got {voice_config[speaker][key]!r}"


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

    def generate_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
        rows = self.storage.execute_query(
            "SELECT status FROM render_job WHERE job_id = ?", (self.job_id,)
        )
        self.status_during_batch = rows[0]["status"] if rows else None
        return super().generate_batch(chunks, voice_config, output_dir, batch_seed)


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

    def generate_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
        super().generate_batch(chunks, voice_config, output_dir, batch_seed)
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

    def test_individual_failure_records_failed_chunk_and_job(self, storage, fake_engine):
        """generate_voice exception → failed chunk row + failed job row + re-raise."""
        engine = _FailingVoiceEngine()
        with pytest.raises(RuntimeError, match="voice boom"):
            render_audiobook(
                "b1", storage, engine, use_batch=False, job_id="job-fail"
            )
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
