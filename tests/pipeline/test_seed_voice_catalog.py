"""Tests for voice catalog seed script (Plan B Phase 2).

Tests verify:
1. Default seed inserts NARRATOR + Ryan voices (at least 2 voices)
2. --include-samples inserts additional clone/design/LoRA test voices
3. Seed is idempotent (running twice skips existing voices)
4. Custom narrator voice flag works
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.pipeline.adapter import InMemorySQLiteAdapter  # noqa: E402
import scripts.seed_voice_catalog as seed_module  # noqa: E402
from scripts.seed_voice_catalog import seed_voice_catalog  # noqa: E402


@pytest.fixture
def inmemory_adapter() -> InMemorySQLiteAdapter:
    """Create an in-memory SQLite adapter with schema initialized."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture
def patch_adapter(inmemory_adapter: InMemorySQLiteAdapter):
    """Monkey-patch SQLiteAdapter in seed module to use in-memory adapter."""
    conn = inmemory_adapter.get_connection()
    original_adapter = seed_module.SQLiteAdapter

    class MockSQLiteAdapter:
        def __init__(self, db_path: str):
            self._conn = conn
            self._storage = inmemory_adapter

        def init_db(self):
            self._storage.init_db()

        def get_connection(self):
            return self._conn

        def execute_insert(self, sql: str, params: tuple = ()) -> int:
            return self._storage.execute_insert(sql, params)

        def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
            return self._storage.execute_query(sql, params)

        def close(self):
            pass  # Don't close in-memory connection

    seed_module.SQLiteAdapter = MockSQLiteAdapter
    yield inmemory_adapter
    seed_module.SQLiteAdapter = original_adapter


class TestSeedDefaultVoices:
    """Test default seed inserts NARRATOR + Ryan voices."""

    def test_seed_inserts_at_least_two_voices(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify default seed inserts at least 2 voices (NARRATOR + Ryan)."""
        result = seed_voice_catalog(db_path=":memory:")

        assert result["inserted"] == 2
        assert result["skipped"] == 0

        # Verify voices exist in DB
        conn = patch_adapter.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
        count = cursor.fetchone()[0]
        assert count >= 2

    def test_seed_inserts_narrator_voice(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify NARRATOR voice is inserted with correct fields."""
        seed_voice_catalog(db_path=":memory:")

        conn = patch_adapter.get_connection()
        cursor = conn.execute(
            "SELECT id, name, type, voice, description FROM voice_config WHERE id = 'NARRATOR'"
        )
        row = cursor.fetchone()
        assert row is not None
        id_, name, type_, voice, desc = row
        assert id_ == "NARRATOR"
        assert name == "NARRATOR"
        assert type_ == "custom"
        assert voice == "Ryan"
        assert "Narrator" in desc or "narrator" in desc

    def test_seed_inserts_ryan_voice(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify default Ryan voice is inserted with correct fields."""
        seed_voice_catalog(db_path=":memory:")

        conn = patch_adapter.get_connection()
        cursor = conn.execute(
            "SELECT id, name, type, voice, description FROM voice_config WHERE id = 'ryan'"
        )
        row = cursor.fetchone()
        assert row is not None
        id_, name, type_, voice, desc = row
        assert id_ == "ryan"
        assert name == "Ryan"
        assert type_ == "custom"
        assert voice == "Ryan"


class TestSeedWithSamples:
    """Test --include-samples inserts additional test voices."""

    def test_seed_with_samples_inserts_six_voices(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify --include-samples inserts NARRATOR + Ryan + 4 sample voices."""
        result = seed_voice_catalog(db_path=":memory:", include_samples=True)

        assert result["inserted"] == 6  # 2 default + 4 samples
        assert result["skipped"] == 0

        conn = patch_adapter.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
        count = cursor.fetchone()[0]
        assert count == 6

    def test_seed_samples_include_all_types(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify sample voices cover clone, design, builtin_lora, and lora types."""
        seed_voice_catalog(db_path=":memory:", include_samples=True)

        conn = patch_adapter.get_connection()
        cursor = conn.execute(
            "SELECT DISTINCT type FROM voice_config ORDER BY type"
        )
        types = {row[0] for row in cursor.fetchall()}
        assert "custom" in types
        assert "clone" in types
        assert "design" in types
        assert "builtin_lora" in types
        assert "lora" in types


class TestSeedIdempotency:
    """Test seed is idempotent — running twice skips existing voices."""

    def test_seed_is_idempotent(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify running seed twice produces no duplicates."""
        result1 = seed_voice_catalog(db_path=":memory:")
        assert result1["inserted"] == 2
        assert result1["skipped"] == 0

        result2 = seed_voice_catalog(db_path=":memory:")
        assert result2["inserted"] == 0
        assert result2["skipped"] == 2

        # Verify count unchanged
        conn = patch_adapter.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
        count = cursor.fetchone()[0]
        assert count == 2

    def test_seed_with_samples_is_idempotent(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify running seed with samples twice produces no duplicates."""
        result1 = seed_voice_catalog(db_path=":memory:", include_samples=True)
        assert result1["inserted"] == 6

        result2 = seed_voice_catalog(db_path=":memory:", include_samples=True)
        assert result2["inserted"] == 0
        assert result2["skipped"] == 6

        conn = patch_adapter.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM voice_config")
        count = cursor.fetchone()[0]
        assert count == 6


class TestCustomNarratorVoice:
    """Test --narrator-voice flag overrides default narrator voice."""

    def test_custom_narrator_voice(self, patch_adapter: InMemorySQLiteAdapter) -> None:
        """Verify custom narrator voice name is used."""
        seed_voice_catalog(db_path=":memory:", narrator_voice="David")

        conn = patch_adapter.get_connection()
        cursor = conn.execute(
            "SELECT voice FROM voice_config WHERE id = 'NARRATOR'"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "David"
