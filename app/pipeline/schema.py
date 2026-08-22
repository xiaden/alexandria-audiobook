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
import time

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
    id TEXT PRIMARY KEY,
    text TEXT
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

_SPEAKER_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_character_span_speaker_unique
    ON character_span (span_id, relation_type)
    WHERE relation_type = 'speaker';
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
    kind TEXT NOT NULL CHECK (kind IN ('voice_profile', 'voice_assignment', 'instruction', 'alias_merge')),
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


# ---------------------------------------------------------------------------
# Workbench — combined 2b/2c/2d workbench tables (DD-combined-walks-2b-2d)
# ---------------------------------------------------------------------------
# All workbench DDL is registered in CONTRACTS.md.  ``workbench_generation``
# is the SOLE per-book revision allocator and intentionally has NO foreign key
# to ``book`` — ``book.version`` is never read or incremented by the workbench.
# Generated and manual presence projections live in separate tables with
# independent target uniqueness; disagreement between them is a conflict, not
# a duplicate insert.  ``source_run_id`` / ``generation_revision`` on generated
# rows are provenance only and never participate in uniqueness.

_WORKBENCH_DDL = """
CREATE TABLE IF NOT EXISTS workbench_generation (
    generation_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workbench_decision (
    decision_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL
        CHECK (target_kind IN ('presence', 'alias_merge', 'review', 'boundary')),
    target_key TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('active', 'undone', 'superseded', 'conflict')),
    source TEXT NOT NULL CHECK (source IN ('human', 'generated')),
    created_ms INTEGER NOT NULL,
    undone_by TEXT REFERENCES workbench_decision(decision_id),
    supersedes_id TEXT REFERENCES workbench_decision(decision_id)
);

CREATE INDEX IF NOT EXISTS idx_workbench_decision_book_status
    ON workbench_decision (book_id, status);

CREATE TABLE IF NOT EXISTS workbench_provenance (
    provenance_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    run_id TEXT REFERENCES walk_run(run_id),
    generation_revision INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('walk', 'human', 'derived')),
    created_ms INTEGER NOT NULL
);

-- Active tombstone: an absent character is never a NULL/deleted junction.
CREATE TABLE IF NOT EXISTS character_scene_absence (
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    scene_id TEXT NOT NULL REFERENCES scene(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_ms INTEGER NOT NULL,
    PRIMARY KEY (book_id, scene_id, character_id)
);

-- Non-destructive convergent alias model: members remain addressable, the
-- active relation projects them under the canonical character, and prior
-- voice assignments + downstream impact are stored for reversible unmerge.
CREATE TABLE IF NOT EXISTS character_alias_merge (
    merge_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    canonical_id TEXT NOT NULL REFERENCES character(id),
    member_id TEXT NOT NULL REFERENCES character(id),
    merge_revision INTEGER NOT NULL CHECK (merge_revision >= 0),
    decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'undone')),
    prior_member_name TEXT NOT NULL,
    prior_member_aliases_json TEXT NOT NULL,
    prior_member_voice_assignment_id TEXT REFERENCES voice_config(id),
    consequence_json TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    UNIQUE (book_id, merge_id)
);

-- At most one ACTIVE merge per (book, member) — history is keyed by
-- (merge_id, merge_revision), never by a UNIQUE(book_id, member_id, status).
CREATE UNIQUE INDEX IF NOT EXISTS ux_alias_active_member
    ON character_alias_merge (book_id, member_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS boundary_override (
    override_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    chapter_id TEXT REFERENCES chapter(id),
    scene_id TEXT REFERENCES scene(id),
    paragraph_id TEXT REFERENCES paragraph(id),
    decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id),
    payload_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_ms INTEGER NOT NULL,
    CHECK (chapter_id IS NOT NULL OR scene_id IS NOT NULL OR paragraph_id IS NOT NULL)
);

-- Generated 2b/2d rows: unique by stable target key only, never source_run_id.
CREATE TABLE IF NOT EXISTS character_scene_generated (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES character(id),
    scene_id TEXT NOT NULL REFERENCES scene(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('present', 'speaker')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    generation_revision INTEGER NOT NULL,
    source_run_id TEXT REFERENCES walk_run(run_id),
    UNIQUE (book_id, character_id, scene_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_character_scene_generated_book
    ON character_scene_generated (book_id);

-- Manual presence projection: separate rows, independent target uniqueness;
-- ``absent`` coexists with present/speaker rows and is gated by the tombstone.
CREATE TABLE IF NOT EXISTS character_scene_manual (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES character(id),
    scene_id TEXT NOT NULL REFERENCES scene(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('present', 'speaker', 'absent')),
    decision_id TEXT NOT NULL REFERENCES workbench_decision(decision_id),
    UNIQUE (book_id, character_id, scene_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_character_scene_manual_book
    ON character_scene_manual (book_id);
"""

# ---------------------------------------------------------------------------
# Voice / Persona / Prompt Parity — clone reference, persona revision, and
# prompt-config revision tables (DD-voice-persona-prompt-parity-browser-external-validation)
# ---------------------------------------------------------------------------
# Three append-only parity tables registered in CONTRACTS.md.  ``owner_id`` /
# ``author_id`` are stable string principals (single-user local; default
# sentinel ``"local"`` — no auth/principal infra exists).  ``relative_path`` is
# a contained application-relative path, never an API-resolved filesystem path.
# ``deleted_ms``/``superseded_by``/``base_revision`` implement soft tombstone /
# revision chaining.  No data backfill is required; the DDL is purely additive
# and idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).

_VOICE_PERSONA_PROMPT_DDL = """
CREATE TABLE IF NOT EXISTS clone_reference (
    reference_id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL REFERENCES voice_config(id),
    owner_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    sha256 TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    deleted_ms INTEGER
);

-- Sensible owner-scoped lookup index for the clone reference resource.
CREATE INDEX IF NOT EXISTS idx_clone_reference_voice_owner
    ON clone_reference (voice_id, owner_id);

CREATE TABLE IF NOT EXISTS persona_revision (
    persona_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES character(id),
    book_id TEXT REFERENCES book(id),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    fields_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    scene_scope TEXT NOT NULL CHECK (scene_scope IN ('book', 'scenes')),
    review_state TEXT NOT NULL
        CHECK (review_state IN ('draft', 'needs_review', 'accepted', 'rejected')),
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    voice_consequences_json TEXT NOT NULL,
    author_id TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    superseded_by TEXT REFERENCES persona_revision(persona_id)
);

-- Per-character lookup index for persona revision history.
CREATE INDEX IF NOT EXISTS idx_persona_revision_character
    ON persona_revision (character_id);

CREATE TABLE IF NOT EXISTS prompt_config_revision (
    revision_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id),
    task TEXT NOT NULL,
    base_revision TEXT REFERENCES prompt_config_revision(revision_id),
    source_layers_json TEXT NOT NULL,
    effective_prompt TEXT,
    settings_json TEXT NOT NULL,
    raw_json TEXT,
    validation_json TEXT NOT NULL,
    author_id TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    superseded_by TEXT REFERENCES prompt_config_revision(revision_id)
);

-- Per (book, task) lookup index for prompt/config revision history.
CREATE INDEX IF NOT EXISTS idx_prompt_config_revision_book_task
    ON prompt_config_revision (book_id, task);
"""


def _migrate_walk_review_item_kind(connection: sqlite3.Connection) -> None:
    """Extend ``walk_review_item.kind`` CHECK to include ``alias_merge``.

    Existing databases created the table with ``kind`` restricted to
    ``('voice_profile', 'voice_assignment', 'instruction')``.  Alias-merge
    review items need ``kind='alias_merge'`` (``target_id`` holds the merge
    ID).  SQLite cannot alter a CHECK constraint in place, so the table is
    rebuilt transactionally while preserving every row and the
    ``idx_walk_review_item_book_status`` index.  Guarded by inspecting the
    stored CREATE SQL so ``create_schema`` stays idempotent; no human data is
    deleted or rewritten.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='walk_review_item'"
    ).fetchone()
    if row is None or row[0] is None:
        return
    if "alias_merge" in row[0]:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        connection.execute(
            """CREATE TABLE walk_review_item_new (
                id TEXT PRIMARY KEY,
                book_id TEXT,
                run_id TEXT,
                kind TEXT NOT NULL CHECK (kind IN
                    ('voice_profile', 'voice_assignment', 'instruction', 'alias_merge')),
                target_table TEXT,
                target_id TEXT,
                prior_value TEXT,
                status TEXT NOT NULL CHECK (status IN
                    ('pending', 'resolved', 'superseded', 'stale')),
                created_ms INTEGER
            )"""
        )
        connection.execute(
            """INSERT INTO walk_review_item_new
                 (id, book_id, run_id, kind, target_table, target_id,
                  prior_value, status, created_ms)
               SELECT id, book_id, run_id, kind, target_table, target_id,
                      prior_value, status, created_ms
                 FROM walk_review_item"""
        )
        connection.execute("DROP TABLE walk_review_item")
        connection.execute(
            "ALTER TABLE walk_review_item_new RENAME TO walk_review_item"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_walk_review_item_book_status"
            " ON walk_review_item (book_id, status)"
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _backfill_workbench_generation(connection: sqlite3.Connection) -> None:
    """Create one ``workbench_generation`` row per existing book at revision 0.

    Additive, idempotent backfill so pre-existing books gain a workbench
    revision allocator row.  The allocator has no FK to ``book`` (it is the
    sole per-book revision owner), so this reads the ``book`` table directly.
    Existing rows are never deleted or rewritten.
    """
    now = int(time.time() * 1000)
    connection.execute(
        """INSERT INTO workbench_generation (generation_id, book_id, revision, updated_ms)
           SELECT 'wg-' || b.id, b.id, 0, ?
             FROM book b
            WHERE NOT EXISTS (
                SELECT 1 FROM workbench_generation g WHERE g.book_id = b.id
            )""",
        (now,),
    )


def _ensure_book_single_speaker_column(connection: sqlite3.Connection) -> None:
    """Add ``single_speaker`` to the book table if it does not exist.

    Registered contract: ``ALTER TABLE book ADD COLUMN single_speaker
    INTEGER NOT NULL DEFAULT 0`` (render-boundary enforcement only).
    Guarded via ``PRAGMA table_info`` so ``create_schema`` stays idempotent
    on existing databases (same pattern as populate.py ``_ensure_*_column``).
    """
    cols = {row[1] for row in connection.execute("PRAGMA table_info(book)").fetchall()}
    if "single_speaker" not in cols:
        connection.execute(
            "ALTER TABLE book ADD COLUMN single_speaker INTEGER NOT NULL DEFAULT 0"
        )


def _ensure_paragraph_text_column(connection: sqlite3.Connection) -> None:
    """Add the derived paragraph-text projection to existing databases."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(paragraph)").fetchall()
    }
    if "text" not in columns:
        connection.execute("ALTER TABLE paragraph ADD COLUMN text TEXT")


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
    cols = {row[1] for row in connection.execute("PRAGMA table_info(book)").fetchall()}
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
    cols = {row[1] for row in connection.execute("PRAGMA table_info(span)").fetchall()}
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
        + _WORKBENCH_DDL
        + _VOICE_PERSONA_PROMPT_DDL
    )
    # Remove legacy duplicate speaker rows before enforcing rerun idempotency.
    connection.execute(
        """DELETE FROM character_span
           WHERE relation_type = 'speaker'
             AND rowid NOT IN (
                 SELECT MIN(rowid)
                 FROM character_span
                 WHERE relation_type = 'speaker'
                 GROUP BY span_id
             )"""
    )
    connection.executescript(_SPEAKER_UNIQUE_INDEX)
    _ensure_book_single_speaker_column(connection)
    _ensure_paragraph_text_column(connection)
    _ensure_book_pause_columns(connection)
    _ensure_span_pause_column(connection)
    _migrate_walk_review_item_kind(connection)
    _backfill_workbench_generation(connection)
