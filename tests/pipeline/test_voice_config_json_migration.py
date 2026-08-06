"""Tests for voice_config.json to DB migration script (Plan O).

Tests verify:
1. Migration inserts voices from voice_config.json with correct field mapping
2. Migration is idempotent (running twice skips existing voices)
3. Migration handles empty/missing voice_config.json gracefully
4. Migration handles invalid JSON gracefully
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.pipeline.adapter import InMemorySQLiteAdapter  # noqa: E402
from scripts.migrate_voice_config_json_to_db import (  # noqa: E402
    migrate_voice_config_json_to_db,
    read_voice_config_json,
)


@pytest.fixture
def sample_voice_config() -> dict:
    """Sample voice_config.json content."""
    return {
        "Ryan": {
            "type": "custom",
            "voice": "Ryan",
            "character_style": "narrator",
            "seed": "-1",
            "ref_audio": None,
            "ref_text": None,
            "adapter_id": None,
            "adapter_path": None,
            "description": "Default narrator voice",
            "alias_of": None,
        },
        "Warm Female": {
            "type": "clone",
            "voice": "Warm Female",
            "character_style": "",
            "seed": "42",
            "ref_audio": "/path/to/ref.wav",
            "ref_text": "Reference text",
            "adapter_id": None,
            "adapter_path": None,
            "description": "Warm female clone voice",
            "alias_of": None,
        },
        "Design Voice": {
            "type": "design",
            "voice": "Design Voice",
            "character_style": "character",
            "seed": "-1",
            "ref_audio": None,
            "ref_text": None,
            "adapter_id": "adapter-123",
            "adapter_path": "/path/to/adapter",
            "description": "A designed voice",
            "alias_of": None,
        },
    }


@pytest.fixture
def voice_config_json_file(tmp_path: Path, sample_voice_config: dict) -> Path:
    """Create a temporary voice_config.json file."""
    json_path = tmp_path / "voice_config.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sample_voice_config, f)
    return json_path


@pytest.fixture
def empty_voice_config_json_file(tmp_path: Path) -> Path:
    """Create an empty voice_config.json file."""
    json_path = tmp_path / "voice_config.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({}, f)
    return json_path


@pytest.fixture
def invalid_voice_config_json_file(tmp_path: Path) -> Path:
    """Create an invalid voice_config.json file."""
    json_path = tmp_path / "voice_config.json"
    with open(json_path, "w", encoding="utf-8") as f:
        f.write("not valid json {{{")
    return json_path


@pytest.fixture
def inmemory_adapter() -> InMemorySQLiteAdapter:
    """Create an in-memory SQLite adapter."""
    adapter = InMemorySQLiteAdapter()
    return adapter


class TestReadVoiceConfigJson:
    """Test read_voice_config_json helper function."""

    def test_read_valid_json(self, voice_config_json_file: Path) -> None:
        """Verify valid JSON is read correctly."""
        data = read_voice_config_json(voice_config_json_file)
        assert isinstance(data, dict)
        assert len(data) == 3
        assert "Ryan" in data
        assert "Warm Female" in data
        assert "Design Voice" in data

    def test_read_empty_json(self, empty_voice_config_json_file: Path) -> None:
        """Verify empty JSON returns empty dict."""
        data = read_voice_config_json(empty_voice_config_json_file)
        assert data == {}

    def test_read_invalid_json(self, invalid_voice_config_json_file: Path) -> None:
        """Verify invalid JSON returns empty dict without error."""
        data = read_voice_config_json(invalid_voice_config_json_file)
        assert data == {}

    def test_read_missing_file(self, tmp_path: Path) -> None:
        """Verify missing file returns empty dict without error."""
        missing_path = tmp_path / "nonexistent.json"
        data = read_voice_config_json(missing_path)
        assert data == {}

    def test_read_non_dict_json(self, tmp_path: Path) -> None:
        """Verify non-dict JSON returns empty dict."""
        json_path = tmp_path / "array.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        data = read_voice_config_json(json_path)
        assert data == {}


class TestMigrationWithSampleData:
    """Test migration with sample voice_config.json."""

    def test_migration_inserts_voices(
        self, inmemory_adapter: InMemorySQLiteAdapter, voice_config_json_file: Path
    ) -> None:
        """Verify migration inserts all voices with correct fields."""
        # Get the connection from the adapter
        conn = inmemory_adapter.get_connection()

        # Monkey-patch SQLiteAdapter to use our in-memory connection
        import scripts.migrate_voice_config_json_to_db as migration_module

        original_adapter = migration_module.SQLiteAdapter

        class MockSQLiteAdapter:
            def __init__(self, db_path: str):
                self._conn = conn
                self._storage = inmemory_adapter

            def init_db(self):
                self._storage.init_db()

            def get_connection(self):
                return self._conn

            def close(self):
                pass  # Don't close in-memory connection

        migration_module.SQLiteAdapter = MockSQLiteAdapter

        try:
            # Run migration
            result = migrate_voice_config_json_to_db(
                db_path=":memory:", json_path=voice_config_json_file
            )

            # Verify counts
            assert result["migrated"] == 3
            assert result["skipped"] == 0
            assert result["errors"] == 0

            # Verify voices inserted
            cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
            count = cursor.fetchone()[0]
            assert count == 3

            # Verify Ryan voice fields
            cursor = conn.execute(
                "SELECT id, name, type, voice, character_style, seed, description FROM voice_config WHERE id = 'Ryan'"
            )
            row = cursor.fetchone()
            assert row is not None
            id_, name, type_, voice, char_style, seed, desc = row
            assert id_ == "Ryan"
            assert name == "Ryan"
            assert type_ == "custom"
            assert voice == "Ryan"
            assert char_style == "narrator"
            assert seed == "-1"
            assert desc == "Default narrator voice"

            # Verify Warm Female voice fields
            cursor = conn.execute(
                "SELECT id, name, type, voice, seed, ref_audio, ref_text, description FROM voice_config WHERE id = 'Warm Female'"
            )
            row = cursor.fetchone()
            assert row is not None
            id_, name, type_, voice, seed, ref_audio, ref_text, desc = row
            assert id_ == "Warm Female"
            assert name == "Warm Female"
            assert type_ == "clone"
            assert voice == "Warm Female"
            assert seed == "42"
            assert ref_audio == "/path/to/ref.wav"
            assert ref_text == "Reference text"
            assert desc == "Warm female clone voice"

            # Verify Design Voice fields
            cursor = conn.execute(
                "SELECT id, name, type, adapter_id, adapter_path, description FROM voice_config WHERE id = 'Design Voice'"
            )
            row = cursor.fetchone()
            assert row is not None
            id_, name, type_, adapter_id, adapter_path, desc = row
            assert id_ == "Design Voice"
            assert name == "Design Voice"
            assert type_ == "design"
            assert adapter_id == "adapter-123"
            assert adapter_path == "/path/to/adapter"
            assert desc == "A designed voice"

        finally:
            # Restore original adapter
            migration_module.SQLiteAdapter = original_adapter

    def test_migration_is_idempotent(
        self, inmemory_adapter: InMemorySQLiteAdapter, voice_config_json_file: Path
    ) -> None:
        """Verify migration can run multiple times without errors."""
        conn = inmemory_adapter.get_connection()

        import scripts.migrate_voice_config_json_to_db as migration_module

        original_adapter = migration_module.SQLiteAdapter

        class MockSQLiteAdapter:
            def __init__(self, db_path: str):
                self._conn = conn
                self._storage = inmemory_adapter

            def init_db(self):
                self._storage.init_db()

            def get_connection(self):
                return self._conn

            def close(self):
                pass

        migration_module.SQLiteAdapter = MockSQLiteAdapter

        try:
            # First run
            result1 = migrate_voice_config_json_to_db(
                db_path=":memory:", json_path=voice_config_json_file
            )
            assert result1["migrated"] == 3
            assert result1["skipped"] == 0

            # Second run — should skip all
            result2 = migrate_voice_config_json_to_db(
                db_path=":memory:", json_path=voice_config_json_file
            )
            assert result2["migrated"] == 0
            assert result2["skipped"] == 3
            assert result2["errors"] == 0

            # Verify count unchanged
            cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
            count = cursor.fetchone()[0]
            assert count == 3

        finally:
            migration_module.SQLiteAdapter = original_adapter


class TestMigrationEdgeCases:
    """Test migration edge cases."""

    def test_migration_with_empty_json(
        self, inmemory_adapter: InMemorySQLiteAdapter, empty_voice_config_json_file: Path
    ) -> None:
        """Verify migration handles empty JSON gracefully."""
        conn = inmemory_adapter.get_connection()

        import scripts.migrate_voice_config_json_to_db as migration_module

        original_adapter = migration_module.SQLiteAdapter

        class MockSQLiteAdapter:
            def __init__(self, db_path: str):
                self._conn = conn
                self._storage = inmemory_adapter

            def init_db(self):
                self._storage.init_db()

            def get_connection(self):
                return self._conn

            def close(self):
                pass

        migration_module.SQLiteAdapter = MockSQLiteAdapter

        try:
            result = migrate_voice_config_json_to_db(
                db_path=":memory:", json_path=empty_voice_config_json_file
            )
            assert result["migrated"] == 0
            assert result["skipped"] == 0
            assert result["errors"] == 0

            # Verify no voices inserted
            cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
            count = cursor.fetchone()[0]
            assert count == 0

        finally:
            migration_module.SQLiteAdapter = original_adapter

    def test_migration_with_missing_json(
        self, inmemory_adapter: InMemorySQLiteAdapter, tmp_path: Path
    ) -> None:
        """Verify migration handles missing JSON file gracefully."""
        conn = inmemory_adapter.get_connection()

        import scripts.migrate_voice_config_json_to_db as migration_module

        original_adapter = migration_module.SQLiteAdapter

        class MockSQLiteAdapter:
            def __init__(self, db_path: str):
                self._conn = conn
                self._storage = inmemory_adapter

            def init_db(self):
                self._storage.init_db()

            def get_connection(self):
                return self._conn

            def close(self):
                pass

        migration_module.SQLiteAdapter = MockSQLiteAdapter

        try:
            missing_path = tmp_path / "nonexistent.json"
            result = migrate_voice_config_json_to_db(
                db_path=":memory:", json_path=missing_path
            )
            assert result["migrated"] == 0
            assert result["skipped"] == 0
            assert result["errors"] == 0

        finally:
            migration_module.SQLiteAdapter = original_adapter

    def test_migration_with_invalid_voice_config(
        self, inmemory_adapter: InMemorySQLiteAdapter, tmp_path: Path
    ) -> None:
        """Verify migration handles invalid voice config entries gracefully."""
        conn = inmemory_adapter.get_connection()

        # Create JSON with invalid entries
        json_path = tmp_path / "invalid.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "Valid Voice": {"type": "custom", "voice": "Valid"},
                    "Invalid Voice": "not a dict",  # Invalid: not a dict
                },
                f,
            )

        import scripts.migrate_voice_config_json_to_db as migration_module

        original_adapter = migration_module.SQLiteAdapter

        class MockSQLiteAdapter:
            def __init__(self, db_path: str):
                self._conn = conn
                self._storage = inmemory_adapter

            def init_db(self):
                self._storage.init_db()

            def get_connection(self):
                return self._conn

            def close(self):
                pass

        migration_module.SQLiteAdapter = MockSQLiteAdapter

        try:
            result = migrate_voice_config_json_to_db(db_path=":memory:", json_path=json_path)
            # Valid voice migrated, invalid voice counted as error
            assert result["migrated"] == 1
            assert result["errors"] == 1

            # Verify only valid voice inserted
            cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
            count = cursor.fetchone()[0]
            assert count == 1

        finally:
            migration_module.SQLiteAdapter = original_adapter
