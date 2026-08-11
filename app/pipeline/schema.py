"""SQLite-WAL two-graph schema for the audiobook pipeline.

Graph1 TREE: series -> book -> chapter -> scene -> paragraph -> span
Graph2 CHARACTER: character + junctions (character_series, character_book,
                  character_scene, character_span, character_metadata)
Universal Upgrade (Plan A): render_job, render_chunk, walk_run,
                  walk_review_item, walk_override, project_snapshot + 3
                  (book_id, status) indices; book.single_speaker added via
                  a guarded ALTER TABLE (idempotent on existing databases).

All DDL is issued by ``create_schema(connection)`` which is idempotent
(uses CREATE TABLE IF NOT EXISTS / CREATE VIEW IF NOT EXISTS).
"""

from __future__ import annotations

import sqlite3


# ---------------------------------------------------------------------------
# Graph 1 — TREE (document spine)
# ---------------------------------------------------------------------------

_GRAPH1_TREE_DDL = """
CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS book (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id),
    book_number INTEGER,
    version INTEGER DEFAULT 1,
    position INTEGER
);

CREATE TABLE IF NOT EXISTS chapter (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id)
);

CREATE TABLE IF NOT EXISTS scene (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS paragraph (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS span (
    id TEXT PRIMARY KEY,
    span_type TEXT NOT NULL CHECK (span_type IN ('sentence', 'quotation')),
    instruct TEXT,
    text TEXT
);
"""

# ---------------------------------------------------------------------------
# Graph 1 — EDGE tables (parent-owned ordering)
# ---------------------------------------------------------------------------

_GRAPH1_EDGE_DDL = """
CREATE TABLE IF NOT EXISTS book_chapter (
    child_id TEXT NOT NULL UNIQUE REFERENCES chapter(id),
    parent_id TEXT NOT NULL REFERENCES book(id),
    position INTEGER,
    UNIQUE (parent_id, position)
);

CREATE TABLE IF NOT EXISTS chapter_scene (
    child_id TEXT NOT NULL UNIQUE REFERENCES scene(id),
    parent_id TEXT NOT NULL REFERENCES chapter(id),
    position INTEGER,
    UNIQUE (parent_id, position)
);

CREATE TABLE IF NOT EXISTS scene_paragraph (
    child_id TEXT NOT NULL UNIQUE REFERENCES paragraph(id),
    parent_id TEXT NOT NULL REFERENCES scene(id),
    position INTEGER,
    UNIQUE (parent_id, position)
);

CREATE TABLE IF NOT EXISTS paragraph_span (
    child_id TEXT NOT NULL UNIQUE REFERENCES span(id),
    parent_id TEXT NOT NULL REFERENCES paragraph(id),
    position INTEGER,
    UNIQUE (parent_id, position)
);
"""

# ---------------------------------------------------------------------------
# Graph 2 — CHARACTER core + voice_config
# ---------------------------------------------------------------------------

_GRAPH2_CHARACTER_DDL = """
CREATE TABLE IF NOT EXISTS voice_config (
    id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    type TEXT DEFAULT 'custom',
    voice TEXT,
    character_style TEXT,
    seed TEXT DEFAULT '-1',
    ref_audio TEXT,
    ref_text TEXT,
    adapter_id TEXT,
    adapter_path TEXT,
    alias_of TEXT
);

CREATE TABLE IF NOT EXISTS character (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT DEFAULT '[]',
    voice_assignment_id TEXT REFERENCES voice_config(id),
    description TEXT
);

CREATE TABLE IF NOT EXISTS character_metadata (
    character_id TEXT NOT NULL REFERENCES character(id),
    key TEXT NOT NULL,
    value TEXT,
    UNIQUE (character_id, key)
);
"""

# ---------------------------------------------------------------------------
# Graph 2 — Junction tables
# ---------------------------------------------------------------------------

_GRAPH2_JUNCTION_DDL = """
CREATE TABLE IF NOT EXISTS character_series (
    character_id TEXT NOT NULL REFERENCES character(id),
    series_id TEXT NOT NULL REFERENCES series(id),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    human_override INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS character_book (
    character_id TEXT NOT NULL REFERENCES character(id),
    book_id TEXT NOT NULL REFERENCES book(id),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    human_override INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS character_scene (
    character_id TEXT NOT NULL REFERENCES character(id),
    scene_id TEXT NOT NULL REFERENCES scene(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('present', 'speaker')),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    human_override INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS character_span (
    character_id TEXT NOT NULL REFERENCES character(id),
    span_id TEXT NOT NULL REFERENCES span(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('speaker', 'mentioned', 'present')),
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    human_override INTEGER DEFAULT 0
);
"""

# ---------------------------------------------------------------------------
# Presentation VIEW
# ---------------------------------------------------------------------------

_SPAN_PRESENTATION_VIEW = """
CREATE VIEW IF NOT EXISTS span_presentation AS
SELECT
    span.id,
    span.span_type,
    span.instruct,
    ROW_NUMBER() OVER (
        ORDER BY book.position,
                 chapter_edge.position,
                 scene_edge.position,
                 paragraph_edge.position,
                 span_edge.position
    ) AS global_index
FROM span
JOIN paragraph_span AS span_edge
    ON span.id = span_edge.child_id
JOIN scene_paragraph AS paragraph_edge
    ON span_edge.parent_id = paragraph_edge.child_id
JOIN chapter_scene AS scene_edge
    ON paragraph_edge.parent_id = scene_edge.child_id
JOIN book_chapter AS chapter_edge
    ON scene_edge.parent_id = chapter_edge.child_id
JOIN book
    ON chapter_edge.parent_id = book.id;
"""


# ---------------------------------------------------------------------------
# Universal Upgrade — job/run/review/override/snapshot tables (Plan A)
# ---------------------------------------------------------------------------

_UNIVERSAL_UPGRADE_DDL = """
CREATE TABLE IF NOT EXISTS render_job (
    job_id TEXT PRIMARY KEY,
    book_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('batch', 'individual')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'expired')),
    error TEXT,
    output_dir TEXT,
    output_artifact_path TEXT,
    created_ms INTEGER,
    started_ms INTEGER,
    finished_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_render_job_book_status
    ON render_job (book_id, status);

CREATE TABLE IF NOT EXISTS render_chunk (
    job_id TEXT NOT NULL REFERENCES render_job(job_id),
    idx INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed', 'evicted')),
    wav_path TEXT,
    error TEXT,
    PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS walk_run (
    run_id TEXT PRIMARY KEY,
    book_id TEXT,
    walk_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'interrupted', 'cancelled')),
    cancel_requested INTEGER DEFAULT 0,
    heartbeat_ms INTEGER,
    result_json TEXT,
    error TEXT,
    created_ms INTEGER,
    finished_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_walk_run_book_status
    ON walk_run (book_id, status);

CREATE TABLE IF NOT EXISTS walk_review_item (
    id TEXT PRIMARY KEY,
    book_id TEXT,
    run_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('voice_profile', 'voice_assignment', 'instruction')),
    target_table TEXT,
    target_id TEXT,
    prior_value TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'resolved', 'superseded', 'stale')),
    created_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_walk_review_item_book_status
    ON walk_review_item (book_id, status);

CREATE TABLE IF NOT EXISTS walk_override (
    book_id TEXT NOT NULL,
    walk_name TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT,
    PRIMARY KEY (book_id, walk_name, key)
);

CREATE TABLE IF NOT EXISTS project_snapshot (
    name TEXT PRIMARY KEY,
    book_id TEXT,
    snapshot_json TEXT,
    created_ms INTEGER
);
"""


def _ensure_book_single_speaker_column(connection: sqlite3.Connection) -> None:
    """Add ``single_speaker`` to the book table if it does not exist.

    Registered contract: ``ALTER TABLE book ADD COLUMN single_speaker
    INTEGER NOT NULL DEFAULT 0`` (render-boundary enforcement only).
    Guarded via ``PRAGMA table_info`` so ``create_schema`` stays idempotent
    on existing databases (same pattern as populate.py ``_ensure_*_column``).
    """
    cols = {
        row[1]
        for row in connection.execute("PRAGMA table_info(book)").fetchall()
    }
    if "single_speaker" not in cols:
        connection.execute(
            "ALTER TABLE book ADD COLUMN single_speaker INTEGER NOT NULL DEFAULT 0"
        )


def _ensure_book_pause_columns(connection: sqlite3.Connection) -> None:
    """Add nullable book-level pause override columns if they do not exist.

    Plan L pause contract — nullable project/book override columns
    ``pause_between_speakers_ms INTEGER NULL`` and
    ``pause_same_speaker_ms INTEGER NULL`` on the ``book`` table (the
    project/book carrier, mirroring the ``book.single_speaker`` guarded
    ALTER pattern).  ``NULL`` means "resolve the applicable default";
    ``0`` is an intentional no-gap override.  Existing rows migrate with
    ``NULL`` (no override) — nothing is silently coerced to 0.  Guarded via
    ``PRAGMA table_info`` so ``create_schema`` stays idempotent.
    """
    cols = {
        row[1]
        for row in connection.execute("PRAGMA table_info(book)").fetchall()
    }
    for col, ddl in (
        ("pause_between_speakers_ms", "pause_between_speakers_ms INTEGER NULL"),
        ("pause_same_speaker_ms", "pause_same_speaker_ms INTEGER NULL"),
    ):
        if col not in cols:
            connection.execute(f"ALTER TABLE book ADD COLUMN {ddl}")


def _ensure_span_pause_column(connection: sqlite3.Connection) -> None:
    """Add nullable ``span.pause_after_ms`` with a CHECK if it does not exist.

    Plan L pause contract — nullable per-span override column
    ``pause_after_ms INTEGER NULL`` with ``CHECK (pause_after_ms IS NULL OR
    pause_after_ms >= 0)``.  ``NULL`` means "resolve the applicable default";
    ``0`` is an intentional no-gap override.  SQLite supports ``ALTER TABLE
    ADD COLUMN`` with a CHECK constraint (verified against 3.46.1); existing
    rows migrate with ``NULL``.  Guarded via ``PRAGMA table_info`` so
    ``create_schema`` stays idempotent.
    """
    cols = {
        row[1]
        for row in connection.execute("PRAGMA table_info(span)").fetchall()
    }
    if "pause_after_ms" not in cols:
        connection.execute(
            "ALTER TABLE span ADD COLUMN pause_after_ms INTEGER NULL"
            " CHECK (pause_after_ms IS NULL OR pause_after_ms >= 0)"
        )


def create_schema(connection: sqlite3.Connection) -> None:
    """Create all pipeline tables and views on *connection*.

    Idempotent — safe to call multiple times.  Foreign keys are only
    enforced when the caller has issued ``PRAGMA foreign_keys = ON``.
    """
    connection.executescript(
        _GRAPH1_TREE_DDL
        + _GRAPH1_EDGE_DDL
        + _GRAPH2_CHARACTER_DDL
        + _GRAPH2_JUNCTION_DDL
        + _SPAN_PRESENTATION_VIEW
        + _UNIVERSAL_UPGRADE_DDL
    )
    _ensure_book_single_speaker_column(connection)
    _ensure_book_pause_columns(connection)
    _ensure_span_pause_column(connection)
