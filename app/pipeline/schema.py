"""SQLite-WAL two-graph schema for the audiobook pipeline.

Graph1 TREE: series -> book -> chapter -> scene -> paragraph -> span
Graph2 CHARACTER: character + junctions (character_series, character_book,
                  character_scene, character_span, character_metadata)

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
    instruct TEXT
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
    description TEXT
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
    )
