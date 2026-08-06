#!/usr/bin/env python3
"""Seed the voice_config table with default voices (Plan F: Narrator Configurability).

Inserts:
- A NARRATOR voice config (type=custom, voice=Ryan by default)
- A default "Ryan" voice for general use (type=custom, voice=Ryan)
- Optionally, sample clone/design/LoRA voices for testing (--include-samples)

This script is idempotent — uses INSERT OR IGNORE so re-running produces no
duplicates and no errors.

Usage:
    python scripts/seed_voice_catalog.py [--narrator-voice VOICE] [--include-samples]

Environment:
    PIPELINE_DB_PATH  Override database path (default: ./data/pipeline.db)
"""

from __future__ import annotations

import argparse
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

# Default database path (overridable via env var or CLI)
DEFAULT_DB_PATH = "./data/pipeline.db"

# SQL for inserting a voice config row (idempotent via INSERT OR IGNORE)
_INSERT_VOICE_SQL = """
INSERT OR IGNORE INTO voice_config
    (id, name, type, voice, description)
VALUES (?, ?, ?, ?, ?)
"""

# SQL for inserting a voice config row with all fields (for sample voices)
_INSERT_VOICE_FULL_SQL = """
INSERT OR IGNORE INTO voice_config
    (id, name, type, voice, character_style, seed, ref_audio, ref_text,
     adapter_id, adapter_path, alias_of, description)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _ensure_schema(storage: SQLiteAdapter) -> None:
    """Ensure the voice_config table exists by running init_db()."""
    storage.init_db()


def _insert_voice(
    storage: SQLiteAdapter,
    voice_id: str,
    name: str,
    voice_type: str,
    voice_name: str,
    description: str = "",
) -> bool:
    """Insert a single voice config row. Returns True if inserted, False if skipped."""
    # Check if voice already exists (idempotency guard).
    # We cannot rely on lastrowid from INSERT OR IGNORE because Python's
    # sqlite3 returns the existing row's rowid even when the insert was
    # skipped — so we probe first instead.
    existing = storage.execute_query(
        "SELECT id FROM voice_config WHERE id = ?", (voice_id,)
    )
    if existing:
        return False

    storage.execute_insert(
        _INSERT_VOICE_SQL,
        (voice_id, name, voice_type, voice_name, description),
    )
    return True



def _insert_sample_voices(storage: SQLiteAdapter) -> tuple[int, int]:
    """Insert sample clone/design/LoRA voices for testing.

    Returns (inserted_count, skipped_count).
    """
    samples = [
        # (id, name, type, voice, description)
        (
            "sample-clone-1",
            "Sample Clone Voice",
            "clone",
            "sample-clone",
            "Sample clone voice for testing (ref_audio required at render time)",
        ),
        (
            "sample-design-1",
            "Sample Design Voice",
            "design",
            "sample-design",
            "Sample design voice for testing",
        ),
        (
            "sample-lora-1",
            "Sample LoRA Voice",
            "builtin_lora",
            "sample-lora",
            "Sample builtin LoRA voice for testing",
        ),
        (
            "sample-lora-custom-1",
            "Sample Custom LoRA Voice",
            "lora",
            "sample-custom-lora",
            "Sample custom LoRA voice for testing (adapter_id required at render time)",
        ),
    ]

    inserted = 0
    skipped = 0
    for voice_id, name, voice_type, voice_name, description in samples:
        if _insert_voice(storage, voice_id, name, voice_type, voice_name, description):
            logger.info(f"  Inserted sample voice: {voice_id} ({voice_type})")
            inserted += 1
        else:
            logger.info(f"  Skipped sample voice (already exists): {voice_id}")
            skipped += 1

    return inserted, skipped


def seed_voice_catalog(
    db_path: str | None = None,
    narrator_voice: str = "Ryan",
    include_samples: bool = False,
) -> dict[str, int]:
    """Seed the voice_config table with default voices.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. Defaults to PIPELINE_DB_PATH env var
        or ./data/pipeline.db.
    narrator_voice:
        Voice name to use for the NARRATOR row (default: "Ryan").
    include_samples:
        If True, also insert sample clone/design/LoRA voices for testing.

    Returns
    -------
    dict with counts: inserted, skipped
    """
    resolved_path = db_path or os.environ.get("PIPELINE_DB_PATH", DEFAULT_DB_PATH)
    logger.info(f"Connecting to database: {resolved_path}")

    storage = SQLiteAdapter(resolved_path)
    try:
        _ensure_schema(storage)

        inserted = 0
        skipped = 0

        # 1. Insert NARRATOR voice
        logger.info(f"Inserting NARRATOR voice (type=custom, voice={narrator_voice})...")
        if _insert_voice(
            storage,
            voice_id="NARRATOR",
            name="NARRATOR",
            voice_type="custom",
            voice_name=narrator_voice,
            description="Narrator voice for spans with no speaker attribution",
        ):
            logger.info("  Inserted NARRATOR voice")
            inserted += 1
        else:
            logger.info("  Skipped NARRATOR voice (already exists)")
            skipped += 1

        # 2. Insert default Ryan voice
        logger.info("Inserting default 'Ryan' voice (type=custom, voice=Ryan)...")
        if _insert_voice(
            storage,
            voice_id="ryan",
            name="Ryan",
            voice_type="custom",
            voice_name="Ryan",
            description="Default custom voice",
        ):
            logger.info("  Inserted 'ryan' voice")
            inserted += 1
        else:
            logger.info("  Skipped 'ryan' voice (already exists)")
            skipped += 1

        # 3. Optionally insert sample voices
        if include_samples:
            logger.info("Inserting sample clone/design/LoRA voices...")
            sample_inserted, sample_skipped = _insert_sample_voices(storage)
            inserted += sample_inserted
            skipped += sample_skipped

        logger.info(
            f"Seed complete: {inserted} voice(s) inserted, {skipped} skipped"
        )
        return {"inserted": inserted, "skipped": skipped}

    finally:
        storage.close()


def main() -> None:
    """Entry point for seed script."""
    parser = argparse.ArgumentParser(
        description="Seed voice_config table with default voices."
    )
    parser.add_argument(
        "--narrator-voice",
        default="Ryan",
        help='Voice name for the NARRATOR row (default: "Ryan")',
    )
    parser.add_argument(
        "--include-samples",
        action="store_true",
        help="Also insert sample clone/design/LoRA voices for testing",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the SQLite database (default: $PIPELINE_DB_PATH or ./data/pipeline.db)",
    )

    args = parser.parse_args()

    seed_voice_catalog(
        db_path=args.db_path,
        narrator_voice=args.narrator_voice,
        include_samples=args.include_samples,
    )


if __name__ == "__main__":
    main()
