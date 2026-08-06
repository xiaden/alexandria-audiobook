#!/usr/bin/env python3
"""Migration script for voice_config schema (Plan O: Voice Workflow Parity).

Adds 9 new columns to the voice_config table to match the VoiceConfigItem model:
- type TEXT DEFAULT 'custom'
- voice TEXT
- character_style TEXT
- seed TEXT DEFAULT '-1'
- ref_audio TEXT
- ref_text TEXT
- adapter_id TEXT
- adapter_path TEXT
- alias_of TEXT

This script is idempotent — safe to run multiple times. It checks for each
column via PRAGMA table_info before issuing ALTER TABLE ADD COLUMN.

Usage:
    python scripts/migrate_voice_config_schema.py [db_path]

If db_path is not provided, defaults to ./data/pipeline.db
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Add project root to path so we can import app modules
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.pipeline.adapter import SQLiteAdapter  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Columns to add: (column_name, column_definition)
# column_definition includes type and optional DEFAULT clause
VOICE_CONFIG_COLUMNS: list[tuple[str, str]] = [
    ("type", "TEXT DEFAULT 'custom'"),
    ("voice", "TEXT"),
    ("character_style", "TEXT"),
    ("seed", "TEXT DEFAULT '-1'"),
    ("ref_audio", "TEXT"),
    ("ref_text", "TEXT"),
    ("adapter_id", "TEXT"),
    ("adapter_path", "TEXT"),
    ("alias_of", "TEXT"),
]


def get_existing_columns(conn) -> set[str]:
    """Get set of existing column names in voice_config table."""
    cursor = conn.execute("PRAGMA table_info(voice_config)")
    rows = cursor.fetchall()
    return {row[1] for row in rows}


def migrate_voice_config_schema(db_path: str = "./data/pipeline.db") -> None:
    """Add missing columns to voice_config table.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. Defaults to ./data/pipeline.db.
    """
    logger.info(f"Connecting to database: {db_path}")
    storage = SQLiteAdapter(db_path)
    conn = storage.get_connection()

    try:
        # Check if voice_config table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='voice_config'"
        )
        if not cursor.fetchone():
            logger.warning("voice_config table does not exist. Run init_db() first.")
            return

        existing_columns = get_existing_columns(conn)
        logger.info(f"Existing columns: {sorted(existing_columns)}")

        added_count = 0
        skipped_count = 0

        for col_name, col_def in VOICE_CONFIG_COLUMNS:
            if col_name in existing_columns:
                logger.info(f"Column '{col_name}' already exists — skipping")
                skipped_count += 1
            else:
                sql = f"ALTER TABLE voice_config ADD COLUMN {col_name} {col_def}"
                logger.info(f"Adding column: {sql}")
                conn.execute(sql)
                added_count += 1

        conn.commit()
        logger.info(
            f"Migration complete: {added_count} columns added, {skipped_count} skipped"
        )

    finally:
        storage.close()


def main() -> None:
    """Entry point for migration script."""
    db_path = sys.argv[1] if len(sys.argv) > 1 else "./data/pipeline.db"
    migrate_voice_config_schema(db_path)


if __name__ == "__main__":
    main()
