#!/usr/bin/env python3
"""Migration script for voice_config.json to pipeline DB (Plan O: Voice Workflow Parity).

Reads the legacy voice_config.json file from the project root and inserts each
voice entry into the voice_config table in the pipeline database.

This script is idempotent — safe to run multiple times. It uses INSERT OR IGNORE
to skip voices that already exist in the database.

Usage:
    python scripts/migrate_voice_config_json_to_db.py [db_path]

If db_path is not provided, defaults to ./data/pipeline.db
Can also be overridden via PIPELINE_DB_PATH environment variable.
"""

from __future__ import annotations

import json
import logging
import os
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

# Default voice_config.json path (project root)
VOICE_CONFIG_JSON_PATH = project_root / "voice_config.json"

# Fields to migrate from VoiceConfigItem to DB columns
# Mapping: JSON field name → DB column name
VOICE_CONFIG_FIELDS = [
    "type",
    "voice",
    "character_style",
    "seed",
    "ref_audio",
    "ref_text",
    "adapter_id",
    "adapter_path",
    "description",
    "alias_of",
]


def read_voice_config_json(json_path: Path) -> dict:
    """Read voice_config.json and return as dict.

    Returns empty dict if file doesn't exist or is invalid JSON.
    """
    if not json_path.exists():
        logger.info(f"voice_config.json not found at {json_path} — nothing to migrate")
        return {}

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                logger.warning(f"voice_config.json is not a dict — got {type(data).__name__}")
                return {}
            return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse voice_config.json: {e}")
        return {}


def migrate_voice_config_json_to_db(
    db_path: str = "./data/pipeline.db",
    json_path: Path | None = None,
) -> dict[str, int]:
    """Migrate voice_config.json entries to voice_config table.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. Defaults to ./data/pipeline.db.
    json_path:
        Path to voice_config.json. Defaults to project root / voice_config.json.

    Returns
    -------
    dict with counts: {"migrated": int, "skipped": int, "errors": int}
    """
    if json_path is None:
        json_path = VOICE_CONFIG_JSON_PATH

    logger.info(f"Connecting to database: {db_path}")
    storage = SQLiteAdapter(db_path)
    conn = storage.get_connection()

    try:
        # Initialize schema (ensure voice_config table exists)
        storage.init_db()

        # Read voice_config.json
        voice_config = read_voice_config_json(json_path)
        if not voice_config:
            logger.info("No voices to migrate")
            return {"migrated": 0, "skipped": 0, "errors": 0}

        logger.info(f"Found {len(voice_config)} voices in voice_config.json")

        migrated_count = 0
        skipped_count = 0
        error_count = 0

        for voice_name, config in voice_config.items():
            if not isinstance(config, dict):
                logger.warning(f"Voice '{voice_name}' config is not a dict — skipping")
                error_count += 1
                continue

            # Build INSERT OR IGNORE statement
            # Columns: id, name, + all VoiceConfigItem fields
            columns = ["id", "name"] + VOICE_CONFIG_FIELDS
            placeholders = ", ".join(["?"] * len(columns))
            column_names = ", ".join(columns)

            sql = f"INSERT OR IGNORE INTO voice_config ({column_names}) VALUES ({placeholders})"

            # Build values tuple
            values = [voice_name, voice_name]  # id and name are both the voice name
            for field in VOICE_CONFIG_FIELDS:
                value = config.get(field)
                # Handle None and empty string consistently
                if value is None:
                    values.append(None)
                else:
                    values.append(value)

            try:
                cursor = conn.execute(sql, tuple(values))
                # Check if row was inserted (rowcount > 0) or ignored (rowcount == 0)
                if cursor.rowcount > 0:
                    logger.info(f"Migrated voice: {voice_name}")
                    migrated_count += 1
                else:
                    logger.info(f"Voice '{voice_name}' already exists — skipping")
                    skipped_count += 1
            except Exception as e:
                logger.error(f"Failed to migrate voice '{voice_name}': {e}")
                error_count += 1

        conn.commit()
        logger.info(
            f"Migration complete: {migrated_count} migrated, {skipped_count} skipped, {error_count} errors"
        )

        return {
            "migrated": migrated_count,
            "skipped": skipped_count,
            "errors": error_count,
        }

    finally:
        storage.close()


def main() -> None:
    """Entry point for migration script."""
    # Check environment variable first, then CLI arg, then default
    db_path = os.environ.get("PIPELINE_DB_PATH")
    if db_path is None:
        db_path = sys.argv[1] if len(sys.argv) > 1 else "./data/pipeline.db"

    result = migrate_voice_config_json_to_db(db_path)
    # Exit with error code if there were errors
    if result["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
