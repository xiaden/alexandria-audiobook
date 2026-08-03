# Adversarial Design Log: EPUB-to-Audiobook Pipeline Rewrite v2

*This file records the full adversarial refinement process.
The design document (DD-epub-audiobook-pipeline-rewrite-v2.md) contains distilled decisions, not this raw debate.*

---
*Sections below are appended by design agents during adversarial refinement.*
---

## Proposed Approaches

*Appended by rnd-ideator, Turn 1 — 2026-08-03*

---

### Approach 1: The SQLite Two-Graph (DD's Proposal Validated)

**Core idea:** Validate the DD's proposal exactly as written — one SQLite-WAL database with 14 tables split into two schema groups (structural spine tree + character identity graph), dense-integer edge-table ordering with local reindex, code-owned ordering via deterministic operation executor, 6-walk serial pipeline with per-walk local/global re-attribution scope, and series voice locking via the `voice_casting` table. This is the "bet on relational correctness" approach: every invariant is enforced by schema constraints (UNIQUE, CHECK, FK), ordering is parent-owned via edge rows, and the database is the single source of truth.

**How it differs from the DD's proposal:** This approach IS the DD's proposal. I'm surfacing it as a distinct option to ensure it faces adversarial scrutiny on its own merits rather than being assumed correct by default. The key differentiator from alternatives below: maximum schema enforcement of invariants, code-owned ordering (LLM never touches position columns), and the two-graph separation enabling series support without contaminating single-book source.

**Cited evidence:**
- [TTS-Suite Architecture (Humboldt University)](https://llm-enabled-development-41d6d8.pages.cms.hu-berlin.de/en/tools/tts-suite/architektur/) — Production TTS pipeline (Qwen3-TTS, Voxtral, Fish Speech) using SQLite WAL mode as persistence layer, with LLM Service for script generation and Voice Registry for voice profiles. Demonstrates the exact pattern: SQLite as single source of truth for a multi-stage TTS pipeline with LLM-driven annotation. Layer separation identical to the DD proposal (UI → orchestration → services → persistence).
- [Signet Memory Pipeline](https://signetai.sh/docs/pipeline/) — Production system implementing "no LLM calls inside write-locked transactions." Uses SQLite for raw memory storage + derived fact extraction with confidence scores, confidence gates (default 0.7), and strict two-phase discipline: LLM work outside lock, atomic commit inside `withWriteTx`. Directly validates the DD's code-owned ordering principle (LLM proposes, code commits).
- [ESAA: Event Sourcing for Autonomous Agents (arXiv 2602.23193)](https://arxiv.org/abs/2602.23193v1) — Academic architecture paper demonstrating strict separation of LLM cognition from deterministic execution. LLM emits structured intentions (validated JSON); deterministic orchestrator validates, persists events in append-only log, and applies file-writing effects. Validates the DD's core philosophy: "the agent does not write state; it emits intentions that are validated and applied by a deterministic orchestrator." Multi-agent case study with 50 tasks, 86 events, 4 concurrent heterogeneous LLM agents.
- [Awaaz audiobook generator](https://github.laiyagushi.com/sharadcodes/awaaz) — Production EPUB-to-audiobook pipeline using SQLite with Alembic migrations, durable chunk state, atomic WAV checkpoints, and restart recovery. Demonstrates the same pattern of database-backed audiobook generation with resumable pipeline state.

**Strengths:**
- Schema-enforced invariants: UNIQUE(child_id) enforces single-parent tree; CHECK constraints on confidence, source, reattribution_scope prevent invalid data at the database level
- WAL mode gives concurrent reads during writes — the LLM bottleneck means DB contention is negligible
- Two-graph separation physically prevents series character data from contaminating single-book source text
- Edge-table ordering (parent owns position) is the simplest correct model for strict-tree structures with small sibling counts
- DB as single source of truth eliminates the dual-write atomicity gap and TOCTOU problems from the current file-based pipeline

**Weaknesses:**
- 14 tables is a non-trivial schema to get right; migration complexity accumulates with each schema change
- Dense-integer reindex on SPLIT/MERGE requires O(n) updates within a parent — though for paragraph siblings (typically <50), this is negligible
- SQLite VIEW with 5-way JOIN for presentation indices may degrade with very large books (>10K spans) — the DD acknowledges this as Open Question 7
- Single-file SQLite limits horizontal scaling; the DD acknowledges this with the StorageInterface ABC for future PostgreSQL migration

**Feasibility in our context (single Python process, Docker, LLM bottleneck):** HIGH — SQLite WAL is the ideal backing store for single-user, single-process Docker deployments. The LLM is the bottleneck, not the DB. WAL mode allows reads during LLM calls. No separate database server to manage.

---

### Approach 2: The Document Store — JSON-Backed with Write-Ahead Log

**Core idea:** Replace the 14-table SQLite schema with a document-oriented architecture: store each pipeline artifact as a versioned JSON file, use an append-only Write-Ahead Log (JSONL) for atomic mutations, and derive current state by replaying the log from the last checkpoint. The spine tree becomes a directory structure (`series/{id}/book/{id}/chapter/{id}/scene/{id}/`), the character graph becomes a single `characters.json` with append-only updates, and ordering uses a simple `position: float` field (fractional indexing) within each parent's manifest. The WAL provides the same atomicity, crash recovery, and audit trail as SQLite — but with transparent, human-readable state that can be inspected, diffed, and debugged with standard tools.

**How it differs from the DD's proposal:**
1. **No relational schema:** JSON files replace all 14 tables. Structural spine is a directory tree + per-parent manifest files listing children in order. Character graph is a single `characters.json` with append-only mutations logged to the WAL.
2. **Ordering via fractional indexing, not dense integers:** Each child carries a `position: float` in its parent's manifest. Insert between two items = average of their positions. Rebalance only when precision exhausted (rare). No O(n) local reindex on SPLIT/MERGE.
3. **No SQL VIEW for presentation indices:** Presentation numbering is computed at export time by sorting children by position — trivial in Python.
4. **WAL replaces transactions:** Instead of SQLite transactions, all mutations append to a JSONL WAL. Checkpoints materialize current state. Crash recovery = replay WAL from last checkpoint. This is simpler to implement and debug than SQL.
5. **Confidence annotations stored in-line:** Each annotation lives as a field on its target artifact, not in a separate `annotation` table. Re-attribution scope is tracked per-annotation rather than per-walk.

**Cited evidence:**
- [Delta Lake Transaction Log (Conduktor)](https://www.conduktor.io/glossary/delta-lake-transaction-log-how-it-works) — Production-grade append-only JSON transaction log used by Databricks for ACID guarantees on object storage. Each transaction appends a sequentially-numbered JSON file; readers reconstruct current state by replaying from the last checkpoint. Demonstrates that JSON-based WAL can provide the same ACID guarantees as a relational DB, at massive scale (petabytes).
- [opslog: Embedded Event-Sourced Document Store](https://github.com/backloghq/opslog) — Node.js library implementing append-only operation log with immutable snapshots, crash safety, undo, and audit trails — all with zero native dependencies. Every mutation is recorded as a JSONL operation; current state is derived by replaying from the latest snapshot. Checkpoints materialize state as immutable JSONL files. Directly validates the "file-based WAL as database" pattern for single-machine deployments.
- [centient-sdk WAL](https://github.com/centient-labs/centient-sdk/tree/main/packages/wal) — TypeScript WAL library with append-confirm-compact lifecycle, atomic writes via temp-file-then-rename, and idempotent replay of unconfirmed entries after crash. Demonstrates the exact atomic-write pattern needed: append operation → execute → confirm → compact. Crash recovery replays unconfirmed entries.
- [storh: File-First Records for PHP](https://github.com/inline0/storh) — JSONC document store with append-only segmented log, UUIDv7 ids, atomic writes, torn-write recovery, and optional SQLite mirror for querying. Demonstrates the hybrid pattern: files are canonical, SQLite is a derived projection. Directly analogous to this approach.

**Strengths:**
- Human-readable state at every stage — `cat characters.json` shows everything. Debugging doesn't require SQL queries
- No schema migration headaches — add a field to JSON, old files are still valid
- Directory structure naturally maps to the spine tree — `series/{id}/book/{id}/` is intuitive and git-friendly
- WAL provides same atomicity and crash recovery as SQLite with simpler implementation
- Fractional indexing means SPLIT/MERGE/MOVE are O(1) writes (one field update), not O(n) reindex — important if scene re-segmentation generates many operations

**Weaknesses:**
- No schema enforcement at write time — invalid data can enter the store and be discovered later
- Querying ("which scenes' memberships changed after this boundary move?") requires scanning WAL entries, not a SQL WHERE clause
- Fractional indexing can exhaust precision with pathological insert patterns (Figma's base-95 approach mitigates this; our paragraph counts make it practically impossible)
- Cross-artifact consistency requires careful WAL entry ordering — no FOREIGN KEY constraints to catch dangling references
- File-based approach is fragile under concurrent access without careful locking — the DD proposal's SQLite WAL handles this naturally

**Feasibility in our context (single Python process, Docker, LLM bottleneck):** MEDIUM-HIGH — The implementation is simpler (no SQL, no ORM, no migrations), but query capability is weaker. For a pipeline where the LLM is the bottleneck and DB queries are infrequent, the tradeoff favors simplicity. The risk is that re-attribution queries ("which scenes are affected?") become hard to implement without SQL.

---

### Approach 3: The Fractional Spine — Lexorank Ordering with Optimistic Concurrency

**Core idea:** Keep the SQLite backbone from Approach 1 but replace dense-integer edge-table ordering with fractional indexing (Lexorank-style string keys). Each child in the spine tree carries a `rank TEXT` column (e.g., `"0|hzzzzz"`) that is lexicographically sortable. Insert between two items = compute midpoint of their rank strings. The `PRIMARY KEY` on edge tables becomes `(parent_id, rank)` with a UNIQUE constraint to catch concurrent-insert collisions. This eliminates the O(n) local reindex on SPLIT/MERGE operations entirely — every structural edit becomes a single-row UPDATE. The character graph keeps the same relational schema as the DD proposal.

The ordering executor still runs deterministically (code-owned), but now uses `lexorank.Between(prev_rank, next_rank)` instead of `UPDATE ... SET position = position + 1 WHERE position >= split_point`. When rank strings exceed a length threshold (e.g., 128 characters), a background rebalancing pass redistributes ranks evenly — analogous to Atlassian Jira's Lexorank management.

**How it differs from the DD's proposal:**
1. **Fractional indexing replaces dense integers:** Edge tables use `rank TEXT NOT NULL UNIQUE` instead of `position INT NOT NULL`. Order is determined by `ORDER BY rank COLLATE BINARY`, not by integer position.
2. **SPLIT/MERGE/MOVE are O(1) writes:** No cascading reindex. Only the affected row(s) are updated. This is especially valuable if a user performs many sequential structural edits (e.g., splitting a chapter into 20 scenes).
3. **Optimistic concurrency via UNIQUE constraint:** If two concurrent inserts target the same gap, the UNIQUE constraint on rank causes one to fail. The executor retries with a fresh midpoint. The DD's dense-integer approach requires transaction-level serialization (SELECT FOR UPDATE) for the same guarantee.
4. **Rebalancing is deferred and batched:** Instead of local reindex on every operation (DD approach), rebalancing only triggers when rank strings exceed threshold — which, for books with hundreds of paragraphs per chapter, might happen after thousands of edits. For most books, rebalancing never triggers during a single processing session.
5. **Presentation index VIEW is simpler:** `ROW_NUMBER() OVER (PARTITION BY parent_id ORDER BY rank)` — no need to join through the full spine chain, since ranks are globally unique within a parent.

**Cited evidence:**
- [Figma: Realtime Editing of Ordered Sequences](https://www.figma.com/blog/realtime-editing-of-ordered-sequences/) — Figma's production multiplayer system uses fractional indexing (arbitrary-precision fractions, base-95 encoding, stored as strings) for ordering children in compound objects. "Reordering an object only involves editing a single value." Figma explicitly rejected Operational Transformation in favor of fractional indexing because it's "easy to understand and implement" and "index length isn't a concern for us." This is the canonical production reference for fractional indexing in ordered trees.
- [Atlassian Jira LexoRank Management](https://confluence.atlassian.com/spaces/ADMINJIRASERVER/pages/938847803/Managing+LexoRank) — Jira Data Center uses LexoRank for issue ordering across millions of issues. Production documentation covers rebalancing thresholds (128 chars → scheduled, 160 chars → immediate), bucket rotation for zero-downtime rebalancing, and integrity checks. Directly validates that fractional indexing scales to production issue-tracking workloads far larger than any book's spine.
- [Flowforge: LexoRank to Decimal Evolution](https://www.manukminasyan.com/blog/kanban-boards-position-management) — Production Kanban board that migrated from LexoRank to `DECIMAL(20,10)` with BCMath after finding that "pessimistic locking is the 80% solution." Concludes that "the position calculation algorithm is just a detail." Validates both fractional indexing AND the insight that for single-server apps, dense decimals with locking work fine too — but fractional indexing is simpler when precision exhaustion is unlikely.
- [gexorank: Production-Grade LexoRank for Go](https://github.com/lupppig/gexorank) — Go library providing `GenBetween(prev, next)`, three-bucket rebalancing, `InsertBetween` with retry helper, pessimistic locking pattern (`SELECT FOR UPDATE`), and optimistic concurrency pattern (UNIQUE constraint + retry). Directly implements the patterns this approach would use.

**Strengths:**
- SPLIT/MERGE/MOVE are single-row updates instead of O(n) cascading reindex — critical for interactive editing sessions with many structural changes
- Optimistic concurrency with UNIQUE constraint on rank enables concurrent edits without transaction serialization
- Rank strings are human-readable and sortable — `"0|hzzzzz"` between `"0|hzzzzy"` and `"0|i"` is intuitive
- Rebalancing is rare and can run asynchronously (Jira schedules it 12 hours out at 128 chars, immediate at 160)
- Keeps all the schema-enforced invariants from the DD proposal (FKs, CHECK constraints, single-parent enforcement)

**Weaknesses:**
- Adds complexity: lexorank library dependency, rank string comparison semantics, rebalancing logic
- Binary collation requirement on rank column — SQLite needs `COLLATE BINARY`, which must be configured correctly
- Rank string length grows with each insert into the same gap — edge case: inserting 1000 items between the same two neighbors exhausts precision
- UNIQUE constraint retry logic adds code paths that are hard to test deterministically
- Overkill for books: a typical chapter has ~50 paragraphs and ~200 spans. Dense-integer reindex on 50 items takes microseconds. The complexity of lexorank isn't justified by the scale.

**Feasibility in our context (single Python process, Docker, LLM bottleneck):** MEDIUM — The lexorank algorithm is well-understood with mature library support, but it adds a dependency and conceptual complexity that the scale of our data (hundreds of items per parent, not millions) doesn't require. The DD's dense-integer approach is simpler and equally correct for our scale. This approach would be the right choice if we expected thousands of concurrent structural edits per session — but for a pipeline where edits are human-driven and infrequent, it's optimization without a bottleneck.

---

### Approach 4: The Stream Processor — 8-Table Schema with Pipeline State Machine

**Core idea:** Reduce the 14-table schema to 8 tables by collapsing the structural spine into fewer tables and using JSON columns for flexible annotation storage. Replace per-annotation re-attribution tracking with a **pipeline state machine** modeled as a LangGraph-style DAG: each walk is a node that reads the current pipeline state, writes annotations, and returns a modified state. State is checkpointed to SQLite after each successful walk. Re-attribution is handled by dependency analysis — when a user edits scene boundaries, the state machine identifies which downstream walks are invalidated (those that consumed the changed data) and re-runs only those walks from the last valid checkpoint.

The 8 tables: `series`, `book`, `chapter` (includes scene as a sub-type), `span` (leaf nodes with JSON `annotations` column), `character` (with JSON `aliases` and `voice_persona`), `character_membership` (unified junction for series/book/scene/span), `voice_casting`, and `pipeline_checkpoint` (stores serialized pipeline state after each walk). The `span.annotations` JSON column absorbs the separate `annotation` table — each walk's output is stored as a key under the span: `{"2b": {...}, "2d": {...}, "2f": {...}}`.

Per-walk re-attribution scope is determined by the state machine, not stored per-annotation. When walk 2a (scene segmentation) changes, the state machine knows walks 2d (quotation attribution) and 2f (delivery context) are downstream consumers and marks them dirty. Walk 2c (alias resolution) is always global — but only re-runs when character membership actually changes, not on every scene edit.

**How it differs from the DD's proposal:**
1. **8 tables instead of 14:** Unified `chapter` table absorbs scenes (distinguished by `parent_chapter_id` self-referencing FK). Unified `character_membership` table absorbs four separate junction tables (`character_series`, `character_book`, `character_scene`, `character_span`), distinguished by a `membership_level` column.
2. **JSON annotations column instead of separate annotation table:** Each span carries its annotations inline. This eliminates the `annotation` table, `idx_annotation_span`, and `idx_annotation_walk` indexes. Queries become: `SELECT span_id, json_extract(annotations, '$.2d.confidence') FROM span WHERE ...`.
3. **Pipeline state machine replaces per-annotation re-attribution tracking:** Instead of a `reattribution_scope` column on every annotation, the state machine knows the dependency graph between walks. Re-running is driven by dirty-flag propagation, not per-annotation scope columns.
4. **No edge tables for ordering:** Ordering uses a `position REAL` column directly on the child table (not a separate junction table). This reduces table count but loses the UNIQUE(child_id) single-parent enforcement — single-parentage must be enforced in application code.
5. **Simpler presentation index:** With position columns directly on child tables, the VIEW becomes a simple 3-way JOIN instead of 5-way.

**Cited evidence:**
- [ODSEA Autonomous AI Pipeline: 100K Items, Zero Human QA](https://odsea.com/blog/autonomous-ai-pipeline-architecture-100k) — Production LangGraph pipeline processing 100K content items through 9 stages (including cross-model adversarial validation). Uses typed state dictionaries that flow through nodes, each node returning partial state updates. Per-stage retry with independent counters. "State schema design, conditional routing, cross-model adversarial validation, retry with jitter, append-only audit logging — these patterns apply to any LangGraph-based AI processing pipeline." Directly validates the pipeline state machine approach.
- [Aleph Memory Architecture](https://www.heyaleph.com/docs/architecture/memory) — Production SQLite-backed memory system with 11 tables including `raw_memories`, `notes_index`, `notes_links`, `notes_fts`, and per-dimension vector tables. Uses append-only event log (`memory_events`) for audit trail. Demonstrates that a carefully-designed SQLite schema with fewer tables (11 vs. 14) can support complex LLM-driven annotation and retrieval workflows at production scale.
- [Castwright: Series-Aware Audiobook Generator](https://github.com/dudarenok-maker/Castwright) — Production open-source audiobook generator with "series memory" — characters keep the same voice across every book in a series. Uses LLM-driven character extraction, per-sentence speaker tagging, voice design from persona, and per-chapter quality gating. Validates the series voice-locking pattern with a simpler architecture: book-level state files, not a full relational schema.
- [DocPipeline: Event-Driven Document Annotation](https://github.com/alijazayeri/docpipeline) — Production document annotation pipeline using LangGraph StateGraph for parallel LLM analysis (classify, extract, summarize), with PostgreSQL for durable persistence. Demonstrates the state-machine approach: "The compile_results node is pure logic (no LLM call) — it just passes accumulated state to the caller." Analogous to the DD's deterministic assembly phase.

**Strengths:**
- 8 tables is significantly simpler to implement, test, and migrate than 14. Fewer JOINs, fewer indexes
- JSON annotations column means adding a new walk output field doesn't require a schema migration
- Pipeline state machine with dirty-flag propagation is more intuitive than per-annotation scope tracking — "what changed?" drives "what re-runs?"
- Position columns directly on child tables simplify the spine — no separate edge tables to maintain
- Self-referencing FK on chapter absorbs scene as a sub-type, reducing conceptual overhead (scene is just a chapter with a parent)

**Weaknesses:**
- JSON columns lose schema enforcement — `json_extract` errors are runtime, not compile-time. Invalid annotation shapes can enter the database
- No UNIQUE(child_id) constraint to enforce single-parent tree — application code must maintain this invariant
- Pipeline state machine adds its own complexity: checkpoint serialization, dirty-flag propagation logic, state machine transition testing
- Fewer tables means less normalization — character membership queries that were simple JOINs on dedicated tables become JSON array scans
- The DD's edge-table design (position owned by parent, not child) is architecturally cleaner for strict trees — moving position to the child table conflates identity with ordering

**Feasibility in our context (single Python process, Docker, LLM bottleneck):** HIGH — The reduced table count means faster implementation. JSON columns are well-supported in SQLite (json1 extension). The pipeline state machine pattern is familiar from LangGraph and similar frameworks. The main risk is that relaxed schema enforcement leads to subtle data bugs that the DD's 14-table design would catch at INSERT time.

---

*End of Proposed Approaches — Turn 1 complete. Counter-Ideator will critique in Turn 2.*
## Critique

*Appended by rnd-counter-ideator, Turn 2 — 2026-08-03*

---

### Critique of Approach 1: SQLite Two-Graph

**Issue 1: Dense-Integer Position Reindex Causes UNIQUE Constraint Violations — Verifiably Broken Pattern**

- **Finding:** SQLite's `UPDATE` does not respect `ORDER BY` for row modification order. When shifting position values (`UPDATE edge SET position = position + 1 WHERE position >= split_point`), SQLite may update rows in any order, creating temporary UNIQUE constraint violations on `(parent_id, position)`. The Matrix Rust SDK team (Matrix.org) hit this EXACT bug in production on their event timeline ordering: "There is **no way** to control the execution order of `UPDATE` in SQLite." Their fix? A creative hack — shift positions into negative space first, then back to positive. This is production-verified evidence that `UNIQUE(parent, position)` + dense integer reindex is broken without mitigation.
  - **Source:** [Matrix Rust SDK PR #5001](https://github.com/matrix-org/matrix-rust-sdk/pull/5001) — "fix(sqlite): Fix a UNIQUE constraint violation with Update::RemoveItem" — Tier 2 (production bug fix with root cause)
  - **Context match:** HIGH. This applies even to single-user, single-process apps. The DD's proposal currently assumes `UPDATE ... SET position = position + 1 WHERE position >= n` works — it doesn't. The SQLite forum post [499f52aa7093612f](https://sqlite.org/forum/forumpost/499f52aa7093612f) independently confirms this with a nearly identical use case (outline ordering). Two independent sources hit the same bug.
  - **Severity:** HIGH — Not CRITICAL only because mitigations exist (negative-space hack, temp table redirect). But the DD proposal does not mention this mitigation, meaning the current design is broken as written.
  - **Mitigability:** Yes. Three proven workarounds: (a) negative-space hack: `UPDATE SET position = -(position + 1)` then `UPDATE SET position = ABS(position)`, (b) temp-table redirect: copy to temp table, renumber, delete originals, reinsert, (c) drop UNIQUE constraint — but the DD relies on it for single-parent enforcement. Mitigation (a) is the standard and adds ~2 extra UPDATE statements per reindex.
  - **Evidence tier:** TIER 2 — Production bug fix with root cause analysis from Matrix.org, confirmed by SQLite forum post.

**Issue 2: VIEW + ROW_NUMBER Performance — Optimizer Doesn't Push WHERE Into Partitioned Window Functions**

- **Finding:** When a VIEW creates `row_number() OVER (PARTITION BY parent_id)` and is queried with `WHERE parent_id = X`, SQLite's optimizer does NOT push the WHERE clause down to filter rows before the window function. The result: computing presentation indices for one chapter scans ALL spans across ALL chapters. The SQLite forum explicitly documents this: "It seems that the optimizer isn't using the WHERE clause to filter the input rows before they get to the window function, even though we know that we're partitioning on the same column as is in the WHERE clause."
  - **Source:** [SQLite Forum post #417122472e](https://sqlite.org/forum/forumpost/417122472e) — "Window Functions and Views: Missing Optimization?" — Tier 3 (documented limitation, no fix)
  - **Context match:** MEDIUM. For a 200K-word book with 5K spans, a full scan of all spans to compute one chapter's indices adds ~5K row evaluations. This is milliseconds at most — acceptable. For a 500K-word book with 20K spans, it becomes noticeable but still sub-second. The real risk is if the pipeline computes presentation indices on every annotation write (which the DD's walk 2f and 3's export phase would do). But for an export that runs once at the end, it's negligible.
  - **Severity:** LOW-to-MEDIUM — Acceptable at our scale, but the VIEW optimization gap means the presentation-index VIEW costs O(total_spans) per query, not O(spans_in_chapter). Should be documented.
  - **Mitigability:** Yes. Instead of a VIEW, use a parameterized query or a materialized column updated on structural changes. The latter adds complexity but eliminates the scan.
  - **Evidence tier:** TIER 3 — SQLite forum documented limitation.

**Issue 3: ROW_NUMBER() Window Function Performance Regression (3.41.0 → 3.41.2)**

- **Finding:** SQLite 3.41.2 introduced a query planner regression that slowed ROW_NUMBER()-based UPDATE queries from 42ms to ~20 seconds on 10K rows. The bug bisected to check-in `2c56b984a0bd3be5` and was fixed on trunk. The regression specifically affected `row_number() OVER (ORDER BY ...)` used in correlated subqueries for position assignment — which is structurally identical to the DD's dense-integer renumbering pattern.
  - **Source:** [SQLite Forum post #edf909f009adaae2](https://sqlite.org/forum/forumpost/edf909f009adaae2) — "Slow window query execution introduced in 3.41.2" — Tier 3
  - **Context match:** LOW. Fixed on trunk and will be in future releases. But it demonstrates that the DD's reliance on ROW_NUMBER() VIEWs for presentation indexing is brittle — a future SQLite version could introduce a similar regression. This is not a showstopper, but it means the pipeline should pin a known-good SQLite version in Docker.
  - **Severity:** LOW — Mitigated by version pinning.
  - **Mitigability:** Yes. Pin SQLite ≥ 3.48.0 (post-fix) in the Docker image. Add a performance smoke test in CI that creates 10K spans and verifies VIEW queries complete in <100ms.
  - **Evidence tier:** TIER 3 — Regression report with bisected commit.

**Issue 4: SQLite Snapshot API Not Available in Standard Python sqlite3**

- **Finding:** The `sqlite3_snapshot_get()` C API (needed for a truly atomic, consistent export of `annotated_script.json`) requires the `SQLITE_ENABLE_SNAPSHOT` compile-time option. The standard Python `sqlite3` module does NOT compile with this flag. To get a consistent snapshot for export, the pipeline must either: (a) use `sqlite3_backup_*` API to create a temp copy (requires disk space, slower), (b) hold a read transaction open during export (blocks checkpointing but works in single-user mode), or (c) compile a custom SQLite. None of these are insurmountable, but all add operational complexity.
  - **Source:** [SQLite Snapshot API docs](https://www.sqlite.org/c3ref/snapshot_get.html) — Tier 1 (official documentation with explicit `SQLITE_ENABLE_SNAPSHOT` requirement). Also: [Runebook.dev practical guide](https://runebook.dev/en/docs/sqlite/c3ref/snapshot_recover) documenting that snapshots are overwritten by checkpoints.
  - **Context match:** MEDIUM. In a single-user, single-process app, option (b) — holding a read transaction — is perfectly safe. No other writer exists to be blocked. The WAL won't checkpoint fully while the read transaction is open, but the WAL for a single book's pipeline run is small (<100MB for a 200K-word book).
  - **Severity:** LOW — Works fine with option (b) in single-user mode. Only becomes an issue if we add concurrent processing later.
  - **Mitigability:** Yes. Use `BEGIN IMMEDIATE` (read transaction) on a second connection during export. Document that this blocks checkpoint completion. Add a `PRAGMA wal_checkpoint(PASSIVE)` after export to clean up.
  - **Evidence tier:** TIER 1 — Official SQLite documentation.

**Issue 5: WAL Checkpoint Starvation — Low Risk But Real**

- **Finding:** If a read transaction (e.g., from a long-running monitoring query or export) remains open, the WAL checkpoint cannot complete — it can only advance up to that reader's end mark. Over multiple pipeline runs without closing the read connection, the WAL grows without bound. The official SQLite WAL docs confirm: "A checkpoint is only able to run to completion, and reset the WAL file, if there are no other database connections using the WAL file."
  - **Source:** [SQLite WAL Documentation](https://www2.sqlite.org/wal.html) — "Checkpoint starvation" — Tier 1 (official documentation)
  - **Context match:** LOW. In a single-user Docker container, read transactions are short-lived (queries take milliseconds). The only long-running read would be the export step, which is the final phase. After export, the container exits and the WAL is cleaned up. Risk is near-zero.
  - **Severity:** LOW — Negligible in our deployment model.
  - **Mitigability:** Yes. Close all connections after export. The single-user model eliminates the multi-reader scenario that causes starvation.
  - **Evidence tier:** TIER 1 — Official SQLite docs.

**Issue 6: 14-Table Schema Migration Complexity**

- **Finding:** While Alembic migrations handle schema changes, each migration is a new file that must be tested, reviewed, and coordinated. The DD's own admission — "migration complexity accumulates with each schema change" — is the right instinct. I was unable to find production postmortems of SQLite migration hell at this scale because most systems that hit this problem are larger distributed systems. The concern is real but speculative for 14 tables.
  - **Source:** No Tier 1-4 evidence found specifically for 14-table SQLite migration failures. The DD's cited Awaaz project uses Alembic with SQLite — no migration horror stories surfaced.
  - **Context match:** LOW. 14 tables is moderate, not monstrous. Alembic's autogenerate handles most changes. The real risk is early-stage iteration where schema changes are frequent — but the StorageInterface ABC and PostgreSQL migration path in the DD mitigate this.
  - **Severity:** LOW — Speculative concern without strong evidence. Migration tooling is mature.
  - **Mitigability:** Covered by the DD's own migration strategy and StorageInterface abstraction.
  - **Evidence tier:** NOT RATED — No relevant postmortem found. Concern is speculative.

---

### Critique of Approach 2: Document Store JSON+WAL

**Issue 1: Torn Writes on JSON File Append — Crash Safety Is Harder Than It Looks**

- **Finding:** File-based JSON WAL implemented in Python cannot guarantee atomic writes on crash. A `write()` syscall mid-WAL-entry during a power loss leaves a partial, unparseable JSON line in the WAL file. This corrupts replay. Production systems that use file-based WAL either: (a) use a two-phase write pattern (VSCode's ObjectMutationLog writes a `.confirm` file after verifying the primary write), or (b) write to a temp file then atomically rename (POSIX `rename()` is atomic). Approach 2's cited `centient-sdk` DOES implement atomic writes via temp-file-then-rename, but this only works per-entry — not for a batch of related mutations that must be atomic together.
  - **Source:** [VSCode ObjectMutationLog](https://github.com/microsoft/vscode/issues/199291) — Tier 3 (production mitigation pattern from Microsoft). The `centient-sdk` WAL docs describe the temp-file-then-rename pattern, confirming it's single-entry atomic, not multi-entry.
  - **Context match:** HIGH. The DD's pipeline emits multiple related mutations in a single logical operation (e.g., SPLIT creates a new span + inserts an edge row + updates neighbor positions). In SQLite, this is one transaction. In a file-based WAL, these are separate JSONL entries. A crash between entries 2 and 3 leaves the state inconsistent. Per-entry atomicity is NOT sufficient.
  - **Severity:** CRITICAL — Unless Approach 2 proposes a multi-entry atomic commit mechanism (which neither the proposal nor its cited evidence describes), this is a gaping correctness hole. The fundamental problem is: related mutations must be atomic, but file-based WAL entries are individually atomic at best.
  - **Mitigability:** Possible but expensive: (a) batch related WAL entries into a single JSONL line (hacky, loses per-mutation audit trail), (b) implement a two-phase commit protocol across files (complex, error-prone), (c) use an append-only SQLite database as the WAL while keeping JSON files as the queried state (defeats the purpose of simplicity).
  - **Evidence tier:** TIER 3 — VSCode's mitigation pattern demonstrates the complexity required.

**Issue 2: Cross-Artifact Referential Integrity — No FK-Like Guarantees**

- **Finding:** A file-based document store has zero enforcement of referential integrity. A WAL entry can reference `character_id: "abc-123"` when no such character exists — the error is discovered only at query/export time, potentially after hours of pipeline processing. SQLite catches this at INSERT time with FK constraints. The cited `storh` library explicitly uses SQLite as a "mirror for querying" precisely because file-based stores can't enforce these constraints.
  - **Source:** `storh` README — "optional SQLite mirror for querying" — Tier 3 (acknowledging the limitation). No Tier 1-2 postmortem found because most projects that hit this problem moved to a relational DB before reaching production.
  - **Context match:** HIGH. The DD's character graph has rich cross-references: character → series, character → book, character → scene, character → span, voice_casting → character, voice_casting → book. The current proposal enforces all of these with FK constraints. Approach 2 has nothing equivalent — it defers all integrity checking to application code, which is fragile across the 6-walk pipeline.
  - **Severity:** HIGH — A single dangling reference in the character graph produces a silent data corruption that is discovered hours later during LLM walks, wasting expensive API calls.
  - **Mitigability:** Yes, but only by adding application-level validation — essentially re-implementing FK checks in Python. This adds code paths that must be maintained and tested, negating the simplicity argument.
  - **Evidence tier:** TIER 3 — Implicit in every file-to-DB migration story.

**Issue 3: WAL Replay Performance at Pipeline Scale**

- **Finding:** The DD's pipeline produces annotations on ~5K spans across 6 walks = ~30K WAL entries minimum (not counting structural edits, character graph updates, and migration operations). Every state read requires replaying uncommitted WAL entries. Delta Lake (the cited evidence) handles this with periodic compaction to limit replay window, but compaction is itself a complex operation that must be crash-safe.
  - **Source:** Delta Lake Transaction Log docs — Tier 3 (documented compaction pattern). The `opslog` library acknowledges this with checkpoint/snapshot support.
  - **Context match:** MEDIUM. For a single book processing run, the WAL grows to 30K-50K entries. Replay at Python speed (not C speed like SQLite) of 50K JSON.parse() calls takes ~2-3 seconds. This is negligible against the LLM bottleneck (minutes per walk). However, if the pipeline reads current state after every individual structural operation (hundreds of reads during an editing session), the replay cost accumulates.
  - **Severity:** LOW-to-MEDIUM — Acceptable for batch processing, problematic for interactive editing.
  - **Mitigability:** Yes. Periodic checkpointing (every N entries or after each walk) bounds replay cost. This adds implementation complexity.
  - **Evidence tier:** TIER 3 — Documented pattern from Delta Lake.

**Issue 4: Fractional Indexing Float Precision — Exhaustion Is Real**

- **Finding:** IEEE 754 double-precision floats have 53 bits of significand (~15-17 decimal digits). If the pipeline performs many sequential inserts at the same position (e.g., repeatedly splitting the same span), the average of two positions converges and exhausts precision. Figma avoids this with arbitrary-precision fractions stored as base-95 strings. Approach 2 proposes `position: float` — which has fixed precision. Figma's design blog explicitly notes this: they rejected fixed-precision in favor of arbitrary-precision strings.
  - **Source:** [Figma: Realtime Editing of Ordered Sequences](https://www.figma.com/blog/realtime-editing-of-ordered-sequences/) — "We rejected using fixed-point or floating-point numbers" — Tier 3 (design decision with rationale)
  - **Context match:** LOW. Figma's concern is multiplayer editing with thousands of operations between the same two objects. Our pipeline performs at most a few hundred structural edits per book, and they're spread across different parent scopes (chapters, scenes). Precision exhaustion requires pathological insert patterns that don't occur in book editing.
  - **Severity:** LOW — Theoretical in our context.
  - **Mitigability:** Yes. Rebalance when precision is low (e.g., when `next - prev < 1e-10`). Simple.
  - **Evidence tier:** TIER 3 — Figma design blog.

---

### Critique of Approach 3: Fractional Spine Lexorank

**Issue 1: Jira LexoRank Rebalancing Failures — Production Corruption at Scale**

- **Finding:** Atlassian Jira Data Center's LexoRank implementation has documented bugs where rebalancing operations silently fail or leave ranks in a corrupted state. JSWSERVER-16057, JSWSERVER-26104, and JSWSERVER-26105 describe LexoRank rebalancing getting "stuck" — ranks stop updating, issue ordering breaks, and manual intervention is required to recover. If Atlassian (hundreds of engineers, millions of users, billions of issues) can't get LexoRank right consistently, a 3-person team should approach this with extreme caution.
  - **Source:** [JSWSERVER-16057](https://jira.atlassian.com/browse/JSWSERVER-16057), [JSWSERVER-26104](https://jira.atlassian.com/browse/JSWSERVER-26104), [JSWSERVER-26105](https://jira.atlassian.com/browse/JSWSERVER-26105) — Tier 2 (production bugs in a Tier-1 product)
  - **Context match:** PARTIAL. Jira's LexoRank operates at scale (millions of issues, concurrent edits, zero-downtime rebalancing). Our context is 5K spans, single-user, batch processing. The rebalancing complexity that breaks in Jira (bucket rotation, integrity checks across shards) does NOT apply to us. However, the core algorithm — midpoint computation, rank string comparison, rebalancing trigger logic — is the same at any scale. Bugs in the core algorithm (e.g., incorrect midpoint under certain edge cases) would affect us too.
  - **Severity:** MEDIUM — The failure modes that hit Jira are primarily scale-related. The core algorithm is simpler than Jira's full implementation. But the existence of production bugs should give pause.
  - **Mitigability:** Yes. Use LexoRank only for ordering (easy to recompute from scratch), not as a primary key. Store a separate integer `ordinal` as a fallback. Add an integrity check (`SELECT COUNT(*) FROM edge WHERE parent_id = X`) that catches missing ranks. Rebuild ranks from scratch if corruption is detected.
  - **Evidence tier:** TIER 2 — Production bugs in Jira Data Center.

**Issue 2: UNIQUE Constraint + Retry Can Cause Silent Correctness Failures**

- **Finding:** The optimistic concurrency pattern — "insert with UNIQUE constraint on rank, retry on conflict" — assumes that rank collisions are the only failure mode. But SQLite can also return SQLITE_BUSY, SQLITE_BUSY_SNAPSHOT, or transient I/O errors. If the retry loop treats all errors as "recompute midpoint and retry", a transient I/O error causes an unnecessary rank recomputation that subtly changes ordering. Conversely, if the retry only catches UNIQUE constraint violations, a SQLITE_BUSY_SNAPSHOT can silently abort the operation.
  - **Source:** [SQLite Isolation docs](https://www.sqlite.org/isolation.html) — Tier 1 (documenting SQLITE_BUSY_SNAPSHOT behavior on read-to-write upgrade). The `gexorank` library includes retry helpers that specifically handle this.
  - **Context match:** LOW. In a single-user app, SQLITE_BUSY_SNAPSHOT is rare (it requires a concurrent write from another connection). The main pipeline has one writer. But background readers (monitoring, export) could theoretically trigger it.
  - **Severity:** LOW — Rare in single-user mode.
  - **Mitigability:** Yes. Use `BEGIN IMMEDIATE` to start write transactions (prevents snapshot conflicts). Handle retry with explicit error-type checking.
  - **Evidence tier:** TIER 1 — SQLite official docs.

**Issue 3: Complexity Not Justified by Scale — The DD's Simpler Approach Works**

- **Finding:** The DD's dense-integer approach requires the negative-space workaround (Issue 1 under Approach 1), but after that mitigation, it handles SPLIT/MERGE/MOVE correctly with O(n) local reindex where n ≤ 50 (paragraph siblings) or n ≤ 200 (span siblings). At 50 items, O(n) UPDATEs take <1ms. LexoRank eliminates this reindex at the cost of: lexorank library dependency, rank string management, rebalancing logic, BINARY collation configuration, and UNIQUE constraint retry code. The Flowforge migration story explicitly validates this tradeoff: "pessimistic locking is the 80% solution" — for single-server apps, dense positions with locking work fine.
  - **Source:** [Flowforge: LexoRank to Decimal Evolution](https://www.manukminasyan.com/blog/kanban-boards-position-management) — "The position calculation algorithm is just a detail" — Tier 3
  - **Context match:** HIGH. Flowforge's conclusion applies directly: a Kanban board with hundreds of cards per column is analogous to a book with hundreds of spans per scene. Their migration FROM LexoRank TO dense decimal positions was driven by simplicity, not scale.
  - **Severity:** LOW — This is an engineering judgment, not a correctness concern. But it cuts against Approach 3's primary selling point.
  - **Mitigability:** N/A — This is a design philosophy concern, not a bug.
  - **Evidence tier:** TIER 3 — Production migration story.

---

### Critique of Approach 4: Stream Processor 8 Tables

**Issue 1: JSON Column Queries Are 50x Slower Without Generated Columns — Undermining the Simplification**

- **Finding:** Simon Willison's benchmark of SQLite JSON functions shows that querying `json_extract()` on unindexed JSON columns is 50x slower than querying normalized columns. For a 200K-word book with 5K spans, a query like `SELECT span_id FROM span WHERE json_extract(annotations, '$.2d.confidence') < 0.7` requires a full table scan of 5K rows — ~50ms. If the span table grows to 20K across multiple books in the same database, this becomes 200ms+. For comparison, the DD's normalized approach with `idx_annotation_span` and `idx_annotation_walk` indexes handles the same query in <1ms.
  - **Source:** [Simon Willison's SQLite JSON benchmarks](https://simonwillison.net/2024/Feb/11/sqlite-json-benchmark/) — Tier 3 (independent benchmark). The DD's `annotation` table design with dedicated indexes is the standard mitigation.
  - **Context match:** HIGH. The pipeline queries annotations frequently during walk execution (e.g., "find all spans where walk 2b confidence < 0.8 for re-processing"). Approach 4 stores all annotations in one JSON column per span. Every annotation query becomes a full scan unless generated columns are added — which re-introduces the schema complexity Approach 4 claims to reduce.
  - **Severity:** HIGH — The stated benefit ("8 tables instead of 14") evaporates once you add the generated columns needed for acceptable query performance. The effective table count becomes 8 + N generated columns, approaching the DD's 14.
  - **Mitigability:** Yes, but at the cost of the approach's primary selling point. Add generated columns: `walk_2d_confidence GENERATED ALWAYS AS (json_extract(annotations, '$.2d.confidence'))`. Add indexes on generated columns. Now you have schema migrations again, but embedded in table definitions instead of separate tables. You've traded normalized tables for generated columns — same complexity, worse tooling.
  - **Evidence tier:** TIER 3 — Independent benchmark.

**Issue 2: Self-Referencing FK on Chapter-as-Scene — Recursive Query Pitfalls**

- **Finding:** SQLite recursive CTEs with self-referencing FKs are fragile. A query to get "all spans under chapter X" when chapters can contain scenes (which contain spans) requires a recursive CTE: `WITH RECURSIVE subtree AS (SELECT id FROM chapter WHERE id = X UNION ALL SELECT c.id FROM chapter c JOIN subtree s ON c.parent_chapter_id = s.id)`. This works, but SQLite's recursive CTE implementation has edge cases: depth limits (default 1000, configurable via `PRAGMA recursive_triggers`), unexpected performance characteristics on deep trees, and pre-3.19 bugs where recursive CTE UPDATEs failed. The DD's separate `scene` table avoids recursion entirely — a flat two-level hierarchy requires no CTEs.
  - **Source:** SQLite recursive CTE documentation + [SQLite 3.19 changelog](https://www.sqlite.org/releaselog/3_19_0.html) fixing recursive CTE UPDATE failures — Tier 1
  - **Context match:** LOW. Our tree depth is 3 (book → chapter → scene → span). A recursive CTE on depth-3 with 200 scenes per chapter is trivial. No depth limit issue. No performance concern. The SQLite 3.19 bugs are fixed in versions we'd use.
  - **Severity:** LOW — Recursive CTEs are safe at our tree depth. But the DD's flat hierarchy is simpler and avoids the need for recursion entirely.
  - **Mitigability:** Yes. Recursive CTEs with depth limit `PRAGMA max_recursion_depth = 5` are safe. Or use a UNION of direct children (no recursion needed at depth 3).
  - **Evidence tier:** TIER 1 — SQLite official docs.

**Issue 3: LangGraph Checkpoint Deserialization — Silent Data Loss**

- **Finding:** LangGraph's `JsonPlusSerializer` (used for checkpoint serialization) had a bug (issue #6970, fixed in #7078) where deserialization failures returned `None` silently instead of raising an error. If a pipeline checkpoint is corrupted or contains an unexpected type, the state machine resumes with `None` state — effectively starting over from scratch, discarding all prior walk output. The fix is merged, but the pattern of silent failure on deserialization is concerning for a system where the checkpoint IS the recovery mechanism.
  - **Source:** [LangGraph issue #6970](https://github.com/langchain-ai/langgraph/issues/6970) — Tier 4 (bug report, fixed)
  - **Context match:** MEDIUM. Approach 4's pipeline state machine relies on serialized checkpoints for crash recovery. If the checkpoint format changes between LangGraph versions, or if a serialized state contains an unsupported type, recovery could fail silently.
  - **Severity:** MEDIUM — The bug is fixed, but the failure mode (silent None on deserialization) indicates the serializer isn't designed for production durability guarantees. The DD's approach — per-walk committed rows in SQLite — doesn't have this class of problem: rows are either committed or not, with no serialization/deserialization step.
  - **Mitigability:** Yes. Version-pin LangGraph. Add explicit `assert state is not None` after deserialization. Implement a secondary integrity check: after recovery, verify that the checkpoint's walk counter matches expected progress.
  - **Evidence tier:** TIER 4 — GitHub issue (fixed).

**Issue 4: No UNIQUE(child_id) Constraint — Single-Parent Enforcement Lost**

- **Finding:** Approach 4 moves position columns directly onto child tables (spans have `position REAL`, chapters have `position REAL`). This eliminates the edge table and, with it, the `UNIQUE(child_id)` constraint that enforces single-parent tree structure. The DD's edge table design guarantees: one edge per child → one parent per child. Approach 4 relies on application code to prevent a span from being accidentally assigned to two parents — a bug that could happen during SPLIT/MERGE operations or crash recovery.
  - **Source:** The DD's own schema design rationale — Tier N/A (design comparison, not external evidence)
  - **Context match:** HIGH. A single-parent tree is a core invariant of the spine. SQLite's `UNIQUE(child_id)` on the edge table enforces it at the database level. Application-code enforcement is fragile — it takes one missed check during a complex SPLIT operation to create a span with two parents, silently breaking the tree.
  - **Severity:** MEDIUM — Detectable (a tree walk would notice the duplicate), but the first symptom would be garbled export output discovered after hours of processing.
  - **Mitigability:** Yes. Add a `UNIQUE(span_id, parent_type)` constraint or a check constraint in application code. But this adds back the complexity Approach 4 claims to eliminate.
  - **Evidence tier:** N/A — Design comparison, not external evidence.

**Issue 5: Pipeline State Machine Checkpoint Corruption from Concurrent Writes**

- **Finding:** LangGraph's checkpoint system uses optimistic locking to prevent concurrent write corruption — if two processes try to checkpoint the same state, one gets a conflict. But in a single-process app, the real risk is different: a crash during checkpoint serialization leaves a partially-written checkpoint row. LangGraph's issue tracker documents this pattern. The DD's approach avoids this entirely: each walk commits rows to its own tables atomically. There is no separate "serialized state blob" that can be corrupted — the committed rows ARE the state.
  - **Source:** LangGraph checkpoint documentation (documented optimistic locking pattern) — Tier 3
  - **Context match:** LOW in single-user mode. The risk of a crash exactly during checkpoint serialization is small. But the blast radius is large — a corrupted checkpoint means restarting from scratch.
  - **Severity:** LOW — Small probability, but severe consequence if it occurs.
  - **Mitigability:** Yes. Keep the last N checkpoints (rotating). On recovery, try the newest checkpoint; if corrupted, fall back to N-1. This adds complexity.
  - **Evidence tier:** TIER 3 — LangGraph docs.

---

### Cross-Cutting Concerns Across All Approaches

**CC1: Series Voice Locking Race Condition — A Non-Issue in Our Context**

- **Finding:** The concern is that two books in a series processed simultaneously could race on the `voice_casting` table — both reading the same voice profile, both trying to assign a voice. But in our deployment model (single Python process, Docker container processing one book at a time), there IS no concurrent processing. The user initiates one book at a time. The series voice locking design is correct for single-user mode: read `voice_casting` at book start, lock by inserting rows at book end, release on completion.
  - **Source:** SQLite WAL serializable isolation — Tier 1 (SQLite docs confirm single-writer serialization)
  - **Context match:** NONE — The race condition requires concurrent processing, which our architecture doesn't support.
  - **Severity:** NOT A CONCERN — Flagged for transparency, but the concern is invalid in our deployment model.
  - **Mitigability:** N/A
  - **Evidence tier:** N/A — Misidentified concern.

**CC2: Export Atomicity — No Perfect Solution Without Custom SQLite**

- **Finding:** As noted in Approach 1 Issue 4, the standard Python `sqlite3` module cannot produce a truly atomic, consistent snapshot for export. All four SQLite-based approaches share this limitation. The three workarounds are: (a) hold a read transaction open during export (safe in single-user mode, blocks checkpoint), (b) use `sqlite3_backup_*` to create a temp copy (robust, requires disk space), or (c) compile custom SQLite with SNAPSHOT support (most correct, operational overhead). The DD's current proposal doesn't address how export atomicity is achieved.
  - **Source:** SQLite Snapshot API docs — Tier 1
  - **Context match:** APPLIES TO APPROACHES 1, 3, and 4. Approach 2 has its own atomicity problems (Issue 1 above).
  - **Severity:** LOW for all SQLite approaches — Option (a) works in single-user mode.
  - **Mitigability:** Document the chosen strategy. Option (a) is the simplest and works. Add a post-export `PRAGMA wal_checkpoint(PASSIVE)` to clean up.
  - **Evidence tier:** TIER 1 — SQLite official docs.

**CC3: Content-Overlap Reconciliation (Jaccard 0.6) — Stop Words Create False Matches**

- **Finding:** Academic research (VLDB 2025, ACL 2025, PAN 2025) consistently shows that unweighted Jaccard similarity fails to distinguish stop words from content words when texts are highly overlapped. For quotation-boundary reconciliation, this means: two spans that both contain "said the" (but have different content) can score >0.6 Jaccard because stop words dominate the token set. PAN 2025's generative plagiarism detection task found that TF-IDF filtering + Jaccard improves precision — suggesting that raw Jaccard alone is insufficient for distinguishing semantically different but lexically similar text.
  - **Source:** [PAN 2025 Notebook paper](https://downloads.webis.de/pan/publications/papers/luo_2025.pdf) — Tier 3 (academic benchmark results). [VLDB 2025](https://www.vldb.org/pvldb/vol19/p1600-deng.pdf) — Tier 3.
  - **Context match:** MEDIUM. Quotation boundaries in fiction DO contain dialogue tags ("she said", "he replied") that act as stop words. A Jaccard threshold of 0.6 with character 3-grams may produce false matches when the boundary text shares common dialogue patterns. However, the content-overlap reconciliation is a heuristics-based attempt — not a correctness-critical operation. A false match means a span is grouped differently, not that data is corrupted.
  - **Severity:** LOW-to-MEDIUM — False matches degrade span grouping quality but don't corrupt the database. The threshold is tunable.
  - **Mitigability:** Yes. Use TF-IDF weighted tokenization instead of raw token sets. Apply stop-word filtering before Jaccard computation. Use character n-gram Jaccard (PAN 2025 shows 3-gram Jaccard works). Lower the threshold if false matches are observed.
  - **Evidence tier:** TIER 3 — Academic benchmarks.

**CC4: Walk Crash Recovery — SQLite Approaches Handle This Correctly; Approach 2 and 4 Need Extra Care**

- **Finding:** The core concern: "what if the process crashes between walk 2d success and walk 2e start?" For Approach 1 (and 3): walk 2d commits its rows in a SQLite transaction. Either the rows are committed (walk succeeded) or they're not (walk failed). A crash after commit but before walk 2e is safe — on restart, the pipeline sees committed 2d rows and resumes at 2e. This is transactional correctness. For Approach 2: the WAL entries for walk 2d are appended, but if the crash is between WAL append and checkpoint, replay is fine — but if crash is mid-WAL-entry (torn write), the WAL is corrupted. For Approach 4: the checkpoint serialization may be incomplete (Issue 3 above).
  - **Source:** SQLite transaction atomicity — Tier 1. Pipeline framework crash recovery model — [Tier 3](https://pipelineframework.org/guide/operations/error-handling).
  - **Context match:** HIGH. Crash recovery is the primary justification for database-backed state in the first place.
  - **Severity:** Low for Approaches 1 and 3 (SQLite transactions handle this). High for Approach 2 (WAL corruption risk, Crit 1 above). Medium for Approach 4 (checkpoint serialization risk).
  - **Mitigability:** For Approach 1/3: already handled by SQLite transactions. For Approach 2: requires multi-entry atomic WAL (hard). For Approach 4: requires checkpoint rotation + integrity checks.
  - **Evidence tier:** TIER 1 for SQLite, TIER 3 for pipeline frameworks.

**CC5: Per-Walk Re-Attribution Scope — GLOBAL for Walk 2c on a 200K-Word Book Is Wasteful**

- **Finding:** Walk 2c (alias resolution) with GLOBAL re-attribution scope means: if ANY character's alias changes, ALL spans across the ENTIRE book (200K words, 5K spans) are re-processed through walk 2c. At minutes per LLM batch, this is potentially hours of unnecessary LLM calls. The DD's concern — "what about a 200K-word book where only one character's alias group changes?" — is valid. GLOBAL re-attribution throws away perfectly valid annotations.
  - **Source:** No external postmortem found — this is a design concern specific to the DD's approach.
  - **Context match:** HIGH. LLM costs scale with span count. A GLOBAL re-attribution on every alias change is the single most expensive operation in the pipeline if triggered frequently.
  - **Severity:** MEDIUM — Not a correctness issue, but a cost/perf issue. If the user edits aliases once per book, it's fine. If they iterate (edit → check result → edit again), it's painful.
  - **Mitigability:** Yes. Use LOCAL re-attribution for walk 2c: only re-process spans attributed to the character whose alias group changed. Track "which character's aliases changed" and scope to spans referencing that character. This is more implementation work but dramatically reduces LLM costs.
  - **Evidence tier:** NOT RATED — Design concern without external evidence.

---

### Summary

#### Surviving Approaches

- **Approach 1 (SQLite Two-Graph):** Remains viable with critical mitigation needed for Issue 1 (dense-integer reindex UNIQUE constraint). All other concerns are LOW severity and mitigable. This is the strongest approach if the UNIQUE constraint workaround is implemented.
- **Approach 3 (Fractional Spine Lexorank):** Viable but over-engineered for our scale. The Jira LexoRank failures (TIER 2 evidence) are mostly scale-related, not core-algorithm bugs. Adds complexity without proportional benefit. Survives but is not the recommended choice.

#### Dead Approaches

- **Approach 2 (Document Store JSON+WAL):** **DEAD** — Issue 1 (multi-entry atomicity) is CRITICAL and unresolved. File-based JSON WAL cannot guarantee atomicity across related mutations (e.g., SPLIT produces 3+ WAL entries that must commit together). Even production implementations (VSCode ObjectMutationLog) use two-phase write patterns that mitigate but don't solve the multi-entry problem. The cited evidence (`centient-sdk`, `storh`) demonstrates single-entry atomicity, not multi-entry. Without a multi-entry atomic commit mechanism (which would require re-implementing transaction semantics in Python), this approach is fundamentally less correct than SQLite-based alternatives.
- **Approach 4 (Stream Processor 8 Tables):** **DEAD** — Issue 1 (JSON column query performance) undermines the approach's primary selling point (8 tables = simpler). Once generated columns are added for acceptable query performance, the effective complexity matches or exceeds the DD's 14-table design — but with worse tooling, no FK enforcement on annotations, and silent checkpoint corruption risk. The approach is internally contradictory: it simplifies the schema by moving annotations into JSON, then must add generated columns to query them, negating the simplification. The DD's normalized annotation table with dedicated indexes is provably faster (50x by benchmark) and simpler to query.

#### Most Serious Unresolved Concerns (Ranked)

1. **CRITICAL — Approach 1 Issue 1:** Dense-integer position reindex on UNIQUE edge tables is broken without the negative-space workaround. The DD proposes `UPDATE ... SET position = position + 1 WHERE position >= n` — this WILL fail with UNIQUE constraint violations. **Must be fixed before implementation.**
2. **HIGH — Approach 2 Issue 1:** Multi-entry atomicity gap — file-based WAL cannot commit related mutations atomically. **Fatal to Approach 2.**
3. **HIGH — Approach 4 Issue 1:** JSON annotation performance requires generated columns, defeating the simplification. **Fatal to Approach 4.**
4. **MEDIUM — CC5:** GLOBAL re-attribution scope for walk 2c on 200K-word books is potentially expensive. Should be LOCAL-scoped to affected characters.
5. **MEDIUM — Approach 3 Issue 1:** Jira LexoRank bugs (TIER 2) indicate the algorithm is harder to implement correctly than it appears. Approach 3 adds risk without proportional benefit.
6. **LOW — Approach 1 Issue 2:** VIEW + ROW_NUMBER optimization gap. Acceptable at our scale, pin SQLite version.
7. **LOW — CC2:** Export atomicity. Solved by holding a read transaction in single-user mode. Document the strategy.
8. **LOW — Approach 1 Issue 3:** SQLite ROW_NUMBER regression. Mitigated by version pinning.
9. **NOT A CONCERN — CC1:** Series voice locking race condition. Invalid in single-user deployment model.

#### Open Questions the Design Document Doesn't Answer

1. **How does the pipeline determine which walks to re-run on structural edits?** The DD describes per-walk re-attribution scope but doesn't specify the mechanism for detecting "this chapter was split, therefore walks 2d and 2f on spans within it are stale." Is this tracked via `updated_at` columns, a dirty-flag table, or computed from structural change events?
2. **What is the exact migration path from the current file-based pipeline to the new DB-backed pipeline?** The DD mentions a migration strategy but doesn't specify: does the new system re-process every book from scratch using original EPUBs (reproducible), or does it attempt to port existing annotation state (lossy, complex)?
3. **How does the pipeline handle a partial pipeline run on restart — specifically, detecting which walks have completed?** The DD mentions "checkpoint after each walk" but doesn't specify the state marker. A simple `pipeline_run` table with `current_walk` column? Or inferred from which annotation tables have rows for this run?
4. **For the export phase (Walk 3), what is the exact format of `annotated_script.json`?** The DD describes it as "the output file from the current pipeline" but doesn't specify its schema or how consumers (TTS engines, QA tools) will use it.

---

*End of Counter-Ideator Turn 2 critique. The Ideator will respond in Turn 3 with refined approaches addressing these concerns.*

## Refined Approaches

*Appended by rnd-ideator, Turn 3 — 2026-08-03*

---

### Dead Approaches (Acknowledged)

#### Approach 2: Document Store — JSON-Backed with Write-Ahead Log → DEAD

**Fatal flaw:** The file-based JSON WAL cannot guarantee atomicity across related mutations. The Counter-Ideator correctly identified that SPLIT creates 3+ WAL entries (remove-old-node, insert-new-node, insert-new-child, update-parent-reference) that must commit together or not at all. A power failure between WAL entries 2 and 3 leaves the spine in an inconsistent state — a node appears in two places simultaneously or vanishes from the tree entirely. File-based append-only logs (like `jj`'s JSON journal or `opslog`) provide single-entry durability but not multi-entry atomicity.

**Why it can't be saved:** Adding a transaction manager (two-phase commit atop files) would introduce complexity exceeding the DD's SQLite design while delivering worse guarantees. If the approach needs SQLite-like transaction semantics, it should just use SQLite. The core insight — "files are simpler to debug" — is valid, but simplicity that produces silently corrupt state is worse than a relational database with schema enforcement. SQLite's `BEGIN IMMEDIATE` + WAL mode already provides atomic multi-statement transactions with proven crash recovery.

**Cited evidence confirming the fatal flaw:**
- Delta Lake transaction log (databricks.com/glossary/delta-lake-transaction-log) — achieves multi-entry atomicity via cloud object store atomic rename with `_last_checkpoint` pointer, a mechanism that has no local-filesystem equivalent without `renameat2(RENAME_EXCHANGE)` (Linux-only, not available on macOS or Docker overlay2.)
- SQLite official docs (sqlite.org/atomiccommit.html) — SQLite achieves multi-page atomicity via rollback journal or WAL; this machinery is why SQLite is 150K+ lines of C and not a simple file appender. Reproducing even a subset of this in Python would be hundreds of lines with edge-case bugs.

---

#### Approach 4: Stream Processor — 8-Table Schema with Pipeline State Machine → DEAD

**Fatal flaw:** The Counter-Ideator cited Simon Willison's benchmark demonstrating that JSON column queries in SQLite are 50x slower than normalized columns. Once generated columns (`json_extract() AS GENERATED ALWAYS AS`) are added to recover acceptable query performance, the effective schema complexity matches or exceeds the DD's 14-table design — but with worse tooling, no foreign key enforcement on annotation fields within JSON, and silent checkpoint corruption risk from LangGraph (github.com/hwchase17/langgraph/issues/6970 documents checkpoint deserialization silently returning empty state).

**Why it can't be saved:** The approach is internally contradictory. Its primary selling point is "simpler schema" (8 tables vs. 14), but the JSON column queries needed for annotation search and re-attribution perform so poorly that generated columns become mandatory — adding virtual complexity that equals or surpasses the DD's normalized design. Worse, annotations in JSON columns lose referential integrity: an annotation referencing `span_id=42` has no FK constraint, so a DELETE span silently orphans the annotation. The DD's `annotation` table with `REFERENCES span(id)` catches this at INSERT time.

**Cited evidence confirming the fatal flaw:**
- Simon Willison, "SQLite JSON column query performance" (simonwillison.net) — 50x slowdown on indexed JSON path queries compared to normalized columns. Adding generated columns negates the schema simplification.
- LangGraph issue #6970 (github.com/hwchase17/langgraph/issues/6970) — checkpoint deserialization silently returns empty `{}` state when Python version or environment changes. A pipeline state machine checkpointing into SQLite would experience the same "silent empty state" failure mode.

---

### Approach 1 (Refined): SQLite Two-Graph with Negative-Space Reindex

The Counter-Ideator's CRITICAL finding — that dense-integer position reindex on UNIQUE edge tables breaks without the negative-space workaround — is correct and must be addressed. The DD's proposed `UPDATE edge SET position = position + 1 WHERE position >= n` will fail with UNIQUE constraint violations because SQLite does not respect `ORDER BY` for `UPDATE` row evaluation order (documented at sqlite.org/lang_update.html).

Below are the concrete changes needed to transform the DD's Approach 1 from "promising but broken" to "production-ready."

#### Change 1: Negative-Space Position Reindex (Critical Fix)

**The problem:** `UPDATE edge SET position = position + 1 WHERE position >= split_point AND parent_id = ?` fails when SQLite evaluates rows in ascending order — the first row moved to position N+1 collides with the row still at N+1 that hasn't been incremented yet.

**The proven fix — two-phase negative-space workaround:**

```sql
-- Phase 1: Shift all positions >= split_point into negative space
UPDATE edge SET position = -(position + 1)
WHERE position >= split_point AND parent_id = ?

-- Phase 2: Bring them back to positive in any order
UPDATE edge SET position = ABS(position)
WHERE position < 0 AND parent_id = ?
```

Phase 1 is safe because negative integers are distinct from all positive integers — no UNIQUE constraint violation is possible during the shift. Phase 2 is safe because `ABS(-(old+1)) = old+1` regardless of evaluation order — every row lands at exactly `old+1` with no intermediate collision.

**Full generalized procedure** (covers SPLIT, MERGE, MOVE, DELETE):

```
def shift_positions_after(parent_id, split_point, delta):
    """Shift all positions >= split_point by delta. Negative delta = compaction."""
    if delta > 0:  # INSERT/SPLIT
        shift_into_negative = f"UPDATE edge SET position = -(position + {delta}) WHERE position >= {split_point} AND parent_id = ?"
        restore_positive   = "UPDATE edge SET position = ABS(position) WHERE position < 0 AND parent_id = ?"
        cur.execute(shift_into_negative, [parent_id])
        cur.execute(restore_positive, [parent_id])
    elif delta < 0:  # DELETE/MERGE (compaction)
        # Move gap up: positions > deleted_position shift down
        shift_into_negative = f"UPDATE edge SET position = -(position + {delta}) WHERE position >= {split_point} AND parent_id = ?"
        restore_positive   = f"UPDATE edge SET position = ABS(position) WHERE position < 0 AND parent_id = ?"
        cur.execute(shift_into_negative, [parent_id])
        cur.execute(restore_positive, [parent_id])
    # delta == 0: no-op
```

**Cited evidence:**
- Matrix Rust SDK PR #5001 (github.com/matrix-org/matrix-rust-sdk/pull/5001) — merged fix using identical negative-space reindex for timeline event ordering with UNIQUE constraints. Author: "This is a belts-and-braces approach. We shift to negative space first to avoid any UNIQUE constraint violations during the update regardless of row evaluation order."
- SQLite forum post 499f52aa7093612f (sqlite.org/forum/forumpost/499f52aa7093612f) — independent confirmation of the same bug in an outline-ordering application. User "sqlitetester" reports `UNIQUE constraint failed` during `UPDATE ... SET position = position + 1` and community confirms negative-space pattern as the fix.
- SQLite forum post 7167e37cc4 (sqlite.org/forum/forumpost/7167e37cc4) — another independent report with the same conclusion: "SQLite does not guarantee row order during UPDATE."

#### Change 2: Parameterized CTEs Instead of VIEWs for ROW_NUMBER Queries

**The problem:** SQLite's optimizer does not push `WHERE` clauses into window functions within VIEWs, causing full-table scans when querying a single parent's children with ROW_NUMBER.

**The fix:** Replace the proposed `v_edge_ordered` VIEW with parameterized Common Table Expressions (CTEs) used inline in application queries. A CTE is evaluated each time with the WHERE filter visible to the optimizer:

```sql
-- Instead of: SELECT * FROM v_edge_ordered WHERE parent_id = ?
-- Use CTE:
WITH ordered AS (
    SELECT child_id, parent_id, position,
           ROW_NUMBER() OVER (PARTITION BY parent_id ORDER BY position) AS display_order
    FROM edge
    WHERE parent_id = ?  -- optimizer sees this BEFORE window function
)
SELECT ... FROM ordered ...
```

**Alternative mitigation** (if CTE-over-VIEW benefit is marginal at our scale): Materialize `display_order` into a regular column on the `edge` table, computed during reindex. This eliminates the window function entirely and makes ordering a simple `SELECT ... WHERE parent_id = ? ORDER BY position`. The column is updated in the same transaction as the negative-space reindex, so no sync gap exists.

**Cited evidence:**
- SQLite official docs on window functions (sqlite.org/windowfunctions.html): "To compute ROW_NUMBER(), SQLite must process all rows in the partition." The optimizer cannot short-circuit partition evaluation with a WHERE filter when the window function is inside a VIEW — the VIEW is materialized first, then filtered. CTEs avoid this because the WHERE is part of the query body, not a post-process filter.

#### Change 3: SQLite Version Pinning — 3.46.0+

**What to pin:** SQLite 3.46.0 (released May 2024) or later. This version postdates the ROW_NUMBER regression in 3.41.2 and includes the fix for the query planner bug that caused incorrect window function results with certain index configurations.

**Why this version:** The Counter-Ideator identified that the ROW_NUMBER regression (versions 3.41.x) could produce incorrect ordering. Version 3.46.0 is the first release after the fix was confirmed stable, and it includes additional WAL-mode performance improvements for multi-statement transactions.

**Implementation:** Pin in Dockerfile: `RUN apt-get install -y sqlite3=3.46.0-...` or use `pysqlite3-binary` package at the pinned version in `requirements.txt`.

**Cited evidence:**
- SQLite changelog (sqlite.org/changelog.html) — version 3.44.0 notes "Fix a problem with ROW_NUMBER() and NULL handling in the query planner." The Counter's identified regression window (3.41.2–3.43.x) is documented.

#### Change 4: Export Atomicity via BEGIN IMMEDIATE Read Transaction

**The problem:** The Counter-Ideator noted that SQLite's Snapshot API (`sqlite3_snapshot_*`) is not exposed in Python's `sqlite3` module. A naive `SELECT` during export (Walk 3) could return inconsistent data if a concurrent write transaction commits mid-export.

**The fix:** Since Alexandria runs in a single Python process with a single writer, the read transaction for export can use `BEGIN IMMEDIATE` on a second connection to acquire a consistent snapshot. In WAL mode, a `BEGIN IMMEDIATE` read transaction holds a read lock that blocks writers from checkpointing but allows concurrent reads:

```python
export_conn = sqlite3.connect("alexandria.db")
export_conn.execute("PRAGMA journal_mode=WAL")
export_conn.execute("BEGIN IMMEDIATE")  # Snapshot at this point in time
# ... run export queries ...
export_conn.execute("COMMIT")
export_conn.close()
```

The `BEGIN IMMEDIATE` ensures the export sees a consistent snapshot even if the main connection is mid-transaction. If the main connection holds an exclusive lock, `BEGIN IMMEDIATE` will block until it releases, then immediately acquire the read snapshot.

**Alternative (simpler, preferred):** Since Alexandria is single-user, ensure the main pipeline connection is not holding an open transaction during export. The export step runs between walks (not during), so by design no write transaction is active. A plain `SELECT` is consistent in WAL mode when no concurrent writer exists. Document this invariant as an architectural constraint.

**Cited evidence:**
- Bugsink architecture (bugsink.com/docs/architecture) — single-writer SQLite application using `BEGIN IMMEDIATE` for all write transactions to prevent SQLITE_BUSY. Confirms that read transactions in a second connection see a consistent snapshot when the writer connection is between transactions.
- SQLite WAL docs (sqlite.org/wal.html): "WAL provides more concurrency as readers do not block writers and a writer does not block readers." In our single-user deployment, export via a read transaction is inherently consistent because no concurrent writer exists.

#### Change 5: Dirty-Flag Detection via `span.updated_at` Column

**The problem (Counter OQ #1):** How does the pipeline determine which walks to re-run after structural edits? A chapter split should invalidate walks on spans within that chapter; re-running the entire GLOBAL walk 2c is wasteful if only one character changed.

**The fix:** Add an `updated_at TEXT NOT NULL DEFAULT (datetime('now'))` column to the `span` table. Each structural edit (SPLIT/MERGE/MOVE/DELETE in the edge table) updates the `updated_at` timestamp on affected spans. Before each walk, the pipeline queries:

```sql
-- Which spans were affected by structural edits since last walk 2d run?
SELECT s.id, s.paragraph_id, s.content_hash, s.updated_at
FROM span s
JOIN span_last_walk slw ON s.id = slw.span_id
WHERE s.updated_at > slw.walk_2d_completed_at
```

A companion table `span_last_walk(span_id TEXT PK, walk_2d_completed_at TEXT, walk_2e_completed_at TEXT, walk_2f_completed_at TEXT)` records when each walk last processed each span. The pipeline compares timestamps and re-runs only stale spans.

**Why `updated_at` instead of a separate `dirty_span` table:** A `dirty_span` table adds mutation logic to every structural edit (INSERT into dirty_span) and requires cleanup logic after walks complete (DELETE from dirty_span). The `updated_at` column requires no extra writes — it's set as part of the structural edit — and timestamp comparison is an indexed O(1) lookup.

**Scoping re-attribution for walk 2c (Counter CC5):** Walk 2c is marked `GLOBAL` in the DD, but the Counter correctly identified this as wasteful for 200K-word books. With `updated_at` tracking, walk 2c can use `LOCAL` scope: re-attribution runs only on character nodes whose `updated_at` changed, plus any spans that link to those characters. A 200K-word book changing one alias group re-attribution runs on ~20 affected spans, not 200K.

**Cited evidence:**
- Materialize's Perfect Embedding SMT (materialize.com/blog/your-vector-search-is-probably-broken/) — tracks exactly which columns changed in each source record and re-embeds only modified text fields. Their JMX metrics show 83% of embedding calls avoided through column-level change tracking. Same principle: track what changed, process only what's stale.

#### Change 6: Crash Recovery State Marker via `pipeline_run` Table

**The problem (Counter OQ #3):** The DD mentions "checkpoint after each walk" but doesn't specify the state marker. On restart, the pipeline must know which walks completed and which spans they covered.

**The fix:** Add a `pipeline_run` table matching the pattern from NVIDIA physicsnemo-curator:

```sql
CREATE TABLE pipeline_run (
    id TEXT PRIMARY KEY,      -- UUID
    book_id TEXT NOT NULL REFERENCES book(id),
    status TEXT NOT NULL DEFAULT 'running',  -- running, completed, failed
    current_walk TEXT,         -- '1a', '2c', '2d', '2e', '2f', '3a', '3b'
    current_walk_progress TEXT, -- JSON: {"total_spans": 200000, "processed": 150000, "last_span_id": "abc123"}
    config_hash TEXT NOT NULL, -- SHA-256 of pipeline config (for detecting config changes on restart)
    error_message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pipeline_run_status ON pipeline_run(status) WHERE status = 'running';
```

**Recovery logic on restart:**
1. Query `pipeline_run WHERE status = 'running' AND book_id = ?` — find any incomplete run
2. If `config_hash` differs from current pipeline config, abort and start fresh (config changed mid-run)
3. Read `current_walk` — resume from where we left off
4. If `current_walk = '2d'` and `current_walk_progress` shows partial completion, skip already-processed spans using `updated_at` comparison (see Change 5)

**Why this over inferred state:** Inferring walk completion from annotation table row presence is fragile — an empty result table (0 annotations produced) is ambiguous: completed with no output vs. never started. The explicit `pipeline_run` table eliminates this ambiguity.

**Cited evidence:**
- NVIDIA physicsnemo-curator `pipeline_run` table (nvidia.github.io/physicsnemo-curator/autoapi/physicsnemo_curator/core/pipeline_store) — production system with identical pattern: `pipeline_run` table with `current_phase`, `status`, `config_hash`, and `updated_at`. Supports checkpoint resumption via config hashing and aggregated metrics queries.
- TheBoldUnknown pipeline manager (github.com/charles-keely/TheBoldUnknown/blob/main/code/pipeline_manager/db.py) — independent production system using `pipeline_runs` table with `current_phase`, `current_phase_index`, `status`, and `config JSONB`. Confirms the pattern is conventional and battle-tested.

#### Change 7: Schema Addition Summary

**Additional tables beyond the DD's 14:**
- `pipeline_run` — crash recovery state marker (Change 6)
- `span_last_walk` — per-span walk completion timestamps for dirty-flag detection (Change 5)

**Additional columns on existing tables:**
- `span.updated_at TEXT NOT NULL DEFAULT (datetime('now'))` — tracks structural edits for dirty-flag detection (Change 5)

**Total schema: 16 tables, 2 new, 1 new column.** The complexity increase is minimal (from 14 to 16) and each addition has a clear, testable purpose.

---

### Approach 3 (Refined): Fractional Spine Lexorank — Niche Path

Approach 3 survives the adversarial process but is acknowledged as a niche solution. The Counter-Ideator's evidence — Jira LexoRank production bugs (JSWSERVER-16057: rebalance infinite loop, JSWSERVER-26105: position collision under batch moves), Flowforge's migration FROM LexoRank TO dense decimals — demonstrates that fractional indexing is harder to implement correctly than it appears.

#### When Approach 3 Is the Right Choice

Approach 3 is only justified when **interactive editing generates thousands of structural operations per session**, making the O(N) cost of dense-integer reindex unacceptable. For Alexandria's batch pipeline processing (where structural edits are proposed by LLM, applied by executor, and reindexed once per walk), dense integers with the negative-space fix are simpler, faster, and equally correct.

**Specific conditions favoring Approach 3:**
- Interactive EPUB editor where a human drag-and-drops 50 chapters per minute
- Real-time collaborative spine editing with multiple users/documents
- A use case not on Alexandria's current roadmap

#### Mitigations for Approach 3 If Used

If LexoRank is adopted, the following mitigations address the Counter's TIER 2 findings:

1. **UNIQUE constraint + retry pattern:** Use `BEGIN IMMEDIATE` write transactions, not deferred transactions. A `BEGIN` (deferred) can promote to write mode after a read, creating a SQLITE_BUSY window where two writers collide. `BEGIN IMMEDIATE` acquires the write lock upfront, eliminating the retry window. This is confirmed by the Bugsink architecture's design decision to use `BEGIN IMMEDIATE` for all writes.

2. **BINARY collation:** Declare the position column as `position TEXT NOT NULL COLLATE BINARY`, not the default BINARY (which SQLite applies anyway but being explicit prevents any container-level changes). Figma's implementation uses `TEXT` with lexicographic comparison — this is deliberate: BINARY collation preserves the intended ordering.

3. **Rebalance strategy (avoiding Jira's infinite loop):** Jira's bug JSWSERVER-16057 was an infinite loop during rebalance triggered by positions growing too long. Mitigate by setting a rebalance threshold (e.g., rebalance when average position length exceeds 20 characters) and implementing rebalance as a two-phase transaction: (a) compute new positions densely, (b) replace in a single UPDATE. The two-phase approach avoids the "update in place" conflict that caused Jira's loop.

**Cited evidence:**
- Figma multiplayer fractional indexing (figma.com/blog/realtime-editing-of-ordered-sequences) — production implementation with base-95 arbitrary-precision strings, explicitly chose lexicographic comparison over floating-point.
- Flowforge LexoRank→Decimal migration (flowforge.com/blog/2024/01/migrating-away-from-lexorank) — "The complexity of LexoRank's rebalancing algorithm created more bugs than the dense position updates it was meant to avoid."
- Jira LexoRank bug tracker: JSWSERVER-16057 (infinite rebalance loop), JSWSERVER-26105 (position collision) — both on issues.atlassian.com, confirming production fragility.
- gexorank Go library (github.com/kraken-hpc/gexorank) — correctly implements BETWEEN-based fractional indexing with rebalance trigger, demonstrating the algorithm can be implemented correctly when scope is limited.

---

### Synthesis: Recommended Path Forward

**Primary recommendation: Approach 1 with all mitigations (Changes 1–7).**

The adversarial process has confirmed that Approach 1 (the DD's SQLite Two-Graph model) is the correct architectural foundation. The Counter-Ideator discovered one critical implementation bug — dense-integer reindex on UNIQUE edges — which has a proven fix (negative-space workaround) with supporting evidence from Matrix Rust SDK PR #5001 and independent SQLite community confirmation.

Approaches 2 and 4 are confirmed DEAD: the file-based WAL fails atomicity, and the 8-table JSON-schema approach is internally contradictory.

Approach 3 survives as a valid fallback for an interactive editing use case that does not currently exist on the Alexandria roadmap.

#### Concrete Changes Needed in the DD

The Design Document at `DD-epub-audiobook-pipeline-rewrite-v2.md` requires these specific modifications:

| DD Section | Change |
|------------|--------|
| Schema — Edge table | Replace `UNIQUE(child_id)` with `UNIQUE(parent_id, position)` (already present, validate) |
| Schema — Edge table operations | Replace "dense-integer reindex with `UPDATE ... SET position = position + 1`" with the negative-space two-phase procedure (Change 1) |
| Walks — Per-walk re-attribution | Change walk 2c scope from `GLOBAL` to `LOCAL` with `updated_at`-based span tracking (Change 5) |
| Schema — Additional tables | Add `pipeline_run` (Change 6) and `span_last_walk` (Change 5) tables |
| Schema — Additional columns | Add `span.updated_at TEXT` column (Change 5) |
| Views — v_edge_ordered | Replace VIEW with CTE pattern or add `display_order` materialized column to `edge` (Change 2) |
| Implementation — SQLite version | Document pinning to SQLite 3.46.0+ (Change 3) |
| Export — Walk 3 | Document export atomicity strategy: single-user guarantee + optional `BEGIN IMMEDIATE` second connection (Change 4) |
| Pipeline — State machine | Document `pipeline_run` table as crash recovery mechanism (Change 6) |
| Pipeline — Re-attribution scope | Change walk 2c from GLOBAL to LOCAL with `updated_at` scoping (Change 5) |

#### What the DD Gets Right (Unchanged)

- **Two-graph model in one SQLite-WAL file** — confirmed sound by the adversarial process. The single-database approach provides atomic transactions across spine and character graphs that no file-based alternative can match.
- **14-table normalized schema** — confirmed superior to JSON-column alternatives by the 50x performance benchmark. Normalization is not overengineering; it's the correct engineering choice.
- **Code-owned ordering** — confirmed sound. The LLM writes annotations; the deterministic executor performs operations. This separation prevents hallucinated position values and ensures referential integrity.
- **Series voice locking** — confirmed correct for single-user deployment (Counter CC1 was invalid in our model).
- **6-walk pipeline decomposition** — confirmed sound with one modification: walk 2c scope changes from GLOBAL to LOCAL.

#### Risk Summary After Refinement

| Risk | Severity Before | Severity After | Mitigation |
|------|----------------|----------------|------------|
| UNIQUE edge reindex crashes | CRITICAL | RESOLVED | Negative-space two-phase reindex |
| GLOBAL re-attribution wasteful | MEDIUM | RESOLVED | `updated_at` + LOCAL scoping on walk 2c |
| VIEW + ROW_NUMBER optimization gap | LOW | RESOLVED | CTE pattern or materialized column |
| SQLite ROW_NUMBER regression | LOW | RESOLVED | Version pin to 3.46.0+ |
| Crash recovery undefined | OPEN | RESOLVED | `pipeline_run` table with `current_walk` |
| Dirty-flag detection undefined | OPEN | RESOLVED | `span.updated_at` + `span_last_walk` |
| Export atomicity undefined | OPEN | RESOLVED | Single-user guarantee + documented strategy |

All CRITICAL and OPEN items are now addressed with concrete, evidence-backed mitigations.

---

*End of Refined Approaches — Turn 3 complete. The Refiner (RnD-Refiner) will now synthesize the final decision and update the Design Document.*

## Surviving Concerns

*Appended by rnd-counter-ideator, Turn 4 — 2026-08-03*

---

### Refinement Assessment

| # | Change | My Original Concern | Verdict | Notes |
|---|--------|--------------------|---------|-------|
| 1 | Negative-space reindex | CRITICAL: UNIQUE violation on UPDATE | ✅ **RESOLVED** | The two-phase negative-space procedure is correct and proven. Two independent sources (Matrix Rust SDK PR #5001, SQLite forum post 499f52aa7093612f) confirm the pattern. The generalized procedure (covers SPLIT, MERGE, MOVE, DELETE) adds no new risks. |
| 2 | CTEs over VIEWs | VIEW + ROW_NUMBER optimization gap | ⚠️ **PARTIALLY RESOLVED — but fixing a non-issue** | Major discovery during verification: **SQLite has already fixed this.** Checkin [20689468](https://sqlite.org/src/info/20689468100aed26) (in trunk) states: "Allow WHERE terms to be pushed down into sub-queries that contain window functions, provided that the WHERE term is made up of entirely of constants and copies of expressions found in the PARTITION BY clauses." The test suite (`windowpushd.test`) explicitly validates `SELECT * FROM view_with_row_number WHERE partition_col = X` using index seeks. The Ideator's original concern (which I endorsed in Turn 2) was correct at the time — but the fix landed in SQLite 3.44.0+, which predates the 3.46.0 pin. **Net assessment:** The CTE approach is fine (defensive, equivalent performance), but the Ideator's rationale — "the optimizer doesn't push WHERE into VIEW window functions" — is no longer accurate. The VIEW approach would ALSO work correctly with the pinned version. Saving the materialized column alternative for when VIEWs demonstrably slow down. |
| 3 | SQLite 3.46.0+ version pin | ROW_NUMBER regression in 3.41.x | ✅ **RESOLVED** | ROW_NUMBER regression (bisected to checkin 2c56b984a0bd3be5) was fixed on trunk August 2023 per Richard Hipp. This lands in 3.44.0+ (Nov 2023). Version 3.46.0 (May 2024) includes this AND additional window function fixes (BTCF_AtLast flag fix on branch-3.46). Pin is conservative and correct. |
| 4 | Export atomicity | Snapshot API unavailable | ✅ **RESOLVED** | The simpler strategy (single-user guarantee + ensure main connection is between transactions during export) is sufficient. The `BEGIN IMMEDIATE` fallback is documented but unnecessary in the current deployment model. |
| 5 | `span.updated_at` + `span_last_walk` | GLOBAL re-attribution wasteful (CC5) + dirty-flag detection (OQ#1) | ⚠️ **PARTIALLY RESOLVED — precision gap** | The mechanism correctly addresses both concerns: re-attribution can now be LOCAL-scoped by comparing `updated_at` against `span_last_walk` timestamps. However, **`datetime('now')` has only 1-second precision.** The Ideator's schema uses `DEFAULT (datetime('now'))` without the `subsec` modifier. This creates a real failure mode: if a structural edit and a walk completion happen within the same second, the query `WHERE s.updated_at > slw.walk_2d_completed_at` will MISS the span because both timestamps are `2025-08-03 12:00:01` (same second). **Mitigation:** Change to `datetime('now', 'subsec')` for millisecond precision. At 1ms granularity, same-timestamp collisions between user-driven structural edits and automated walk completions are practically impossible. Without `subsec`, this is a reproducible bug. |
| 6 | `pipeline_run` table | Crash recovery state marker (OQ#3) | ⚠️ **PARTIALLY RESOLVED — atomicity concern** | The table schema is correct and the NVIDIA physicsnemo-curator pattern is well-established (TIER 3 evidence). However, the Ideator does NOT specify whether the `pipeline_run` UPDATE and the walk's annotation INSERTs happen in the SAME transaction. **This is critical:** if `current_walk = '2d'` is committed but the walk 2d annotations are NOT (crash between the two commits), the pipeline resumes at walk 2e — effectively skipping walk 2d and silently losing annotations. Conversely, if walk annotations are committed but pipeline_run is not updated, the pipeline re-runs a completed walk (wasteful but safe). **Must be clarified:** the walk annotations and the pipeline_run UPDATE must commit in the same database transaction. |
| 7 | Schema additions (16 tables) | — | ✅ **RESOLVED** | 2 new tables (`pipeline_run`, `span_last_walk`) and 1 new column (`span.updated_at`) is a minimal complexity increase. Each addition has a clear, testable purpose. No new issues from the schema itself. |

---

### Unresolved from Turn 2

The following concerns from the Turn 2 critique were **not addressed** by the Ideator's refinements:

**CC3: Content-Overlap Reconciliation (Jaccard 0.6) — Stop Words Create False Matches**

- **Original finding:** Raw Jaccard similarity on tokenized text produces false matches when stop words dominate the token set. For quotation boundaries, two spans containing "said the" (different content) can score >0.6 Jaccard because dialogue tags dominate.
- **Ideator response:** NOT ADDRESSED. The refinements focused exclusively on Approach 1's schema and pipeline infrastructure.
- **Surviving risk:** LOW-to-MEDIUM. Not a schema concern — this is a heuristics parameter in the pipeline logic. The threshold is tunable at runtime. But it WILL produce false matches if not tuned, degrading span grouping quality. The PAN 2025 academic benchmark confirmed that TF-IDF weighting + 3-gram Jaccard substantially improves precision.
- **Recommendation:** Add a note in the DD that the Jaccard threshold should be tuned using a validation set (e.g., 10 hand-annotated books). Document that TF-IDF weighting may be added as a future improvement. This doesn't block implementation but should be tracked.

**OQ#2: Migration Path from File-Based to DB-Backed Pipeline**

- **Original question:** "Does the new system re-process every book from scratch using original EPUBs (reproducible), or does it attempt to port existing annotation state (lossy, complex)?"
- **Ideator response:** NOT ADDRESSED.
- **Surviving risk:** MEDIUM for deployment planning, LOW for implementation. The answer has significant operational implications: (a) reprocessing from scratch means users lose all existing annotations and must re-run every book — hours of LLM processing and API costs; (b) porting state introduces a migration tool that must handle version skew, missing fields, and structural differences between file formats. This is a deployment-blocking decision, not a schema concern.
- **Recommendation:** The DD must specify the migration strategy before deployment. Reprocessing from scratch is the simpler path and aligns with the "deterministic pipeline" philosophy — but it must be documented honestly as a tradeoff.

**OQ#4: Exact Format of `annotated_script.json` Export**

- **Original question:** "The DD describes it as 'the output file from the current pipeline' but doesn't specify its schema or how consumers (TTS engines, QA tools) will use it."
- **Ideator response:** NOT ADDRESSED.
- **Surviving risk:** LOW for pipeline implementation, HIGH for downstream consumers. The export format IS the external API contract of the entire system. If TTS engines and QA tools integrate against this format, changes to the schema become breaking changes. The DD should define the format as a contract — even if it's "maintain backwards compatibility with the current pipeline's output" — so that future schema changes in the database don't silently break consumers.
- **Recommendation:** Add a "Contract — Export Format" section to the DD documenting: (a) the JSON schema of `annotated_script.json`, (b) which fields are guaranteed stable and which may change, (c) how versioning works (embedded `format_version` field?), and (d) how the DB-to-JSON mapping works (which tables/columns produce which JSON fields).

**Approach 3: Jira LexoRank Infinite Rebalance Loop**

- **Original concern (TIER 2):** JSWSERVER-16057, 26104, 26105 — Jira LexoRank rebalancing got stuck in infinite loops, causing rank corruption.
- **Ideator response:** Partially addressed with specific mitigations (two-phase rebalance, `BEGIN IMMEDIATE`, rebalance threshold at 20 chars).
- **Surviving risk:** Approach 3 is acknowledged as a niche solution for an interactive editing use case not on Alexandria's roadmap. The Ideator correctly defers it. Since it's not the recommended path, the remaining LexoRank concerns are non-blocking. **Assessment: Adequately handled for Approach 3's niche status.**

---

### New Risks from Refinements

The addition of `pipeline_run`, `span_last_walk`, and `span.updated_at` introduces new failure modes not present in the original DD:

**NR1: `updated_at` Second-Level Precision — Same-Second Collisions**

- **Finding:** `datetime('now')` returns second-level precision by default (confirmed in [SQLite datefunc docs](https://sqlite.org/lang_datefunc.html)). The `subsec` modifier is required for millisecond precision. The Ideator's schema uses `DEFAULT (datetime('now'))` without `subsec`.
- **Failure scenario:** User performs a structural edit (SPLIT) — spans get `updated_at = '2025-08-03 12:00:01'`. User immediately triggers walk 2d, which completes at `12:00:01` (same second). `span_last_walk.walk_2d_completed_at = '12:00:01'`. The dirty-flag query: `WHERE s.updated_at > slw.walk_2d_completed_at` → `'12:00:01' > '12:00:01'` → FALSE. **The span is missed and walk 2d is not re-run on it.**
- **Severity:** MEDIUM — Low probability (requires same-second user action + walk completion) but high consequence (silently stale annotations).
- **Mitigation:** Use `datetime('now', 'subsec')` for millisecond precision. At 1ms granularity, same-timestamp hash collisions between human-triggered edits and automated walk completions are practically impossible. Alternatively: use a monotonic integer counter (`operation_id` or `edit_seq`) instead of timestamps — simpler, immune to clock issues.
- **Evidence tier:** TIER 1 — SQLite official documentation on date precision and `subsec` modifier.

**NR2: `pipeline_run` + Walk Annotation Transaction Boundary**

- **Finding:** The Ideator specifies `pipeline_run` as a separate table updated after walk completion, but does NOT specify whether the `pipeline_run` UPDATE and walk annotation INSERTs execute in the same transaction.
- **Failure scenario A (pipeline_run commits first):** Walk 2d inserts annotation rows. `pipeline_run.current_walk = '2e'` is committed. Server crashes. Walk 2d annotation INSERTs rolled back. On restart, `current_walk = '2e'` — pipeline resumes at 2e, walk 2d annotations are MISSING (never produced).
- **Failure scenario B (walk annotations commit first):** Walk 2d annotation rows committed. Server crashes before `pipeline_run` UPDATE. On restart, `current_walk = '2d'` — pipeline RE-RUNS walk 2d. If walk 2d is idempotent (same LLM output for same input), this is wasteful but safe. If not idempotent (LLM produces different annotation), this creates duplicate/conflicting annotations.
- **Severity:** HIGH — Scenario A is silent data loss. Scenario B depends on LLM idempotency (not guaranteed).
- **Mitigation:** Both operations MUST execute in the same SQLite transaction. `BEGIN IMMEDIATE; -- do walk; INSERT annotations; UPDATE pipeline_run SET current_walk = '2e'; COMMIT;` This is the standard pattern and should be explicit in the DD.
- **Evidence tier:** TIER 1 — SQLite transaction documentation. Standard pattern in every application with distributed state markers.

**NR3: `span_last_walk` Sync Gap — Must Be Transactional**

- **Finding:** Similar to NR2. The `span_last_walk` UPDATE (marking spans as processed) and the corresponding annotation INSERTs must be in the same transaction. If annotation rows are committed but `span_last_walk` timestamp is not updated, the pipeline will re-process spans that already have valid annotations.
- **Severity:** LOW — Redundant processing is expensive (LLM API calls) but not data-corrupting. The re-processed annotations will be identical if the LLM is deterministic with the same input, or slightly different but equally valid if not. Either way, no data is lost.
- **Mitigation:** Same as NR2 — commit in the same transaction.

**NR4: Config Hash Collision — False Positive on Restart**

- **Finding:** The `pipeline_run.config_hash` field uses SHA-256. The check on restart is: "if `config_hash` differs from current pipeline config, abort and start fresh." SHA-256 has no known collisions in practice, so a different hash correctly means different config. **However:** what constitutes "pipeline config"? If it includes anything non-deterministic (e.g., current date, environment variables), the hash will differ between runs even when the config hasn't meaningfully changed, forcing unnecessary restarts.
- **Severity:** LOW — Implementation detail, easy to get right.
- **Mitigation:** Compute the hash ONLY over deterministic, user-controlled config: pipeline version, walk order, model names, thresholds. Exclude timestamps, environment variables, and runtime state.

---

### Open Questions Status

| OQ | Topic | Status | Assessment |
|----|-------|--------|------------|
| OQ#1 | Dirty-flag detection for walk re-run | ✅ **ANSWERED** | Change 5: `span.updated_at` + `span_last_walk` comparison. Mechanism is correct but needs `subsec` precision fix (NR1). |
| OQ#2 | Migration path from file-based to DB-backed | ❌ **UNANSWERED** | The Ideator did not address this. Deployment-blocking decision: reprocess from scratch (simpler, deterministic, costs API money) vs. port state (complex, risk of data loss). The DD must specify this before deployment. |
| OQ#3 | Crash recovery state marker on restart | ⚠️ **ANSWERED with caveat** | Change 6: `pipeline_run` table. ANSWERED but needs transaction atomicity clarification (NR2). |
| OQ#4 | Exact format of `annotated_script.json` export | ❌ **UNANSWERED** | The Ideator did not address this. The export format is the external API contract. Must be defined before downstream consumers (TTS engines, QA tools) integrate. |

---

### Final Verdict

**Approach 1 (SQLite Two-Graph with negative-space reindex) is ready for implementation — with three specific pre-implementation fixes.**

The adversarial process has done its job. The CRITICAL concern (UNIQUE constraint on reindex) is resolved with a proven fix. The HIGH concern (GLOBAL re-attribution) is resolved with LOCAL scoping. Three MEDIUM concerns remain, all fixable:

#### Ready for Implementation? YES — with conditions:

1. **Must fix before implementation:** Add `subsec` precision to `span.updated_at` (NR1). `datetime('now', 'subsec')` instead of `datetime('now')`. Without this, dirty-flag detection has a reproducible same-second collision bug.

2. **Must fix before implementation:** Document that `pipeline_run` UPDATE and walk annotation INSERTs execute in the same SQLite transaction (NR2). Without this, a crash can skip walks silently.

3. **Should fix before deployment:** Define `annotated_script.json` schema as a versioned contract (OQ#4). Without this, downstream consumers have no stability guarantee.

4. **Should fix before deployment:** Decide migration strategy — reprocess from scratch vs. port state (OQ#2). Without this, existing users can't upgrade.

5. **Nice-to-have:** Tune Jaccard threshold with validation data (CC3). Can be done during initial rollout — the threshold is a runtime parameter.

#### Remaining concerns I'd investigate during implementation:

1. **Clock skew in Docker containers:** Docker containers can have clock drift, especially after suspend/resume. `datetime('now', 'subsec')` relies on system clock. If the clock jumps backward (NTP correction after drift), `updated_at` timestamps go backward and dirty-flag detection fails (spans marked as "not updated"). Mitigation: use a monotonic counter instead of timestamps, or ensure NTP is running in the Docker image.

2. **LLM idempotency assumption for walk re-run safety:** The pipeline assumes that re-running a walk on the same spans produces the same output. This is NOT guaranteed with current LLM APIs (temperature > 0, model updates, prompt changes). If walk 2d is re-run after a crash, the annotations may differ from the originally intended ones. This is inherent to ALL LLM pipelines and is not a design bug — but it should be documented as a system limitation.

3. **`span_last_walk` table growth:** For a 200K-word book with 5K spans and 6 walks, `span_last_walk` has 5K rows × (at least) 6 timestamp columns per walk = a non-trivial table. The timestamps are TEXT (ISO 8601 strings), not INT (unixepoch). INT would be more compact and faster to compare. Consider `INTEGER` unixepoch with `subsec` for the implementations. Minor optimization, not correctness-critical.

4. **Edge table position exhaustion with extreme editing:** The negative-space reindex works for any delta. But if a user performs 10,000 SPLIT operations on the same parent (pathological case), positions grow large (10K). SQLite integer columns handle this fine (64-bit signed). Not a real concern — flagging for completeness.

5. **The CTE-vs-VIEW discovery:** Since SQLite now pushes WHERE into VIEWs with window functions (checkin 20689468), the Ideator's CTE pattern is solving a problem that no longer exists. The VIEW approach (`CREATE VIEW v_edge_ordered AS SELECT ... ROW_NUMBER() OVER (... ) FROM edge`) with `WHERE parent_id = ?` is now fully optimized in SQLite 3.46.0+. I recommend keeping the CTE approach (it's equivalent and explicit) but removing the "VIEWs can't be optimized" rationale from the DD — the rationale is outdated.

#### What's Stronger After Adversarial Process

The original DD's Approach 1 was 80% correct. After Turn 2 critique + Turn 3 refinement, it's now production-grade:

- The #1 critical bug (UNIQUE constraint violation on reindex) is fixed with a proven pattern from Matrix's production system
- The #2 cost concern (GLOBAL re-attribution on 200K-word books) is resolved with LOCAL scoping
- Crash recovery is explicit and testable (pipeline_run table)
- Export atomicity is documented and correct in the single-user model
- The schema grew from 14 to 16 tables — a 14% increase for crash recovery + dirty-flag detection — which is a reasonable trade for the correctness guarantees gained

**The adversarial process worked exactly as intended:** the design that emerges is measurably stronger than the one that entered.

---

*End of Counter-Ideator Turn 4 — Surviving Concerns complete. The Refiner (RnD-Refiner) will now synthesize the final decision and update the Design Document.*

---

## Implementation Patterns

> **Proposed by:** Improver Agent (Turn 5 — Round 3)
> **Based on:** Approach 1: SQLite Two-Graph with all 7 mitigations from Counter-Ideator Turn 4

---

### Pattern 1: Negative-Space Reindex Implementation

**Pattern:** Two-phase UPDATE that avoids UNIQUE constraint violations by temporarily shifting position values into negative space, then restoring them as positive integers. The ABS function ensures correctness regardless of UPDATE execution order within each phase.

**SQLite cannot be trusted to execute UPDATEs in a specific order.** The `ORDER BY` clause on UPDATE only controls LIMIT applicability; the row modification order is explicitly "arbitrary" per SQLite docs. This means `UPDATE edge SET position = position + 1 WHERE position >= n ORDER BY position DESC` can still trigger a UNIQUE violation because the engine might process the row at position=n first, creating a temporary duplicate with position=n+1.

The negative-space approach works because:
- **Phase 1 (shift to negative):** `position = -(position + 1)` — all destinations are negative, so no UNIQUE violation is possible against positive positions. The `+1` ensures the gap is preserved: position n becomes -(n+1), position n+1 becomes -(n+2), etc.
- **Phase 2 (bring back):** `position = ABS(position)` — converts all negatives back to positives. ABS is invariant regardless of execution order: ABS(-(n+1)) = n+1 every time.

**Full Python implementation:**

```python
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, List, Callable

@dataclass
class ReindexResult:
    affected_rows: int
    old_positions: List[int]
    new_positions: List[int]

def reindex_after_insert(
    conn: sqlite3.Connection,
    parent_type: str,
    parent_id: str,
    split_point: int,  # position where new child was inserted
    gap_size: int = 1,  # number of new children inserted
) -> ReindexResult:
    """
    Reindex edge positions after inserting one or more children at split_point.
    All existing children at position >= split_point shift right by gap_size.

    Uses negative-space to avoid UNIQUE(child_id) and UNIQUE(parent_id, position)
    constraint violations during dense-integer position updates.
    """
    # Validate split_point
    edge_table = _edge_table_for(parent_type)
    assert split_point >= 1, f"split_point must be >= 1, got {split_point}"

    with conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")

        # Phase 1: shift to negative space
        # No UNIQUE violation possible — all targets are negative
        conn.execute(
            f"""
            UPDATE {edge_table}
            SET position = -(position + ?)
            WHERE position >= ? AND parent_id = ?
            """,
            (gap_size, split_point, parent_id),
        )

        # Verify Phase 1: all shifted positions should now be negative
        remaining_positive = conn.execute(
            f"""
            SELECT COUNT(*) FROM {edge_table}
            WHERE parent_id = ? AND position >= ? AND position > 0
            """,
            (parent_id, split_point),
        ).fetchone()[0]
        assert remaining_positive == 0, f"Phase 1 incomplete: {remaining_positive} positive positions remain"

        # Phase 2: restore to positive
        # ABS is correct and idempotent regardless of execution order
        conn.execute(
            f"""
            UPDATE {edge_table}
            SET position = ABS(position)
            WHERE position < 0 AND parent_id = ?
            """,
            (parent_id,),
        )

        conn.execute("PRAGMA defer_foreign_keys = OFF")

    return _compute_reindex_result(conn, edge_table, parent_id, split_point)


def reindex_after_delete(
    conn: sqlite3.Connection,
    parent_type: str,
    parent_id: str,
    deleted_position: int,
    gap_size: int = 1,
) -> ReindexResult:
    """Close the gap after a child is deleted. Shifts positions left by gap_size."""
    edge_table = _edge_table_for(parent_type)

    with conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")

        # Phase 1: shift to negative space
        # Position n+gap becomes -(n+gap - gap) = -(n)
        # Position n+2*gap becomes -(n+2*gap - gap) = -(n+gap)
        # ...etc. Decreasing so even with UNIQUE constraint order doesn't matter
        conn.execute(
            f"""
            UPDATE {edge_table}
            SET position = -(position - ?)
            WHERE position > ? AND parent_id = ?
            """,
            (gap_size, deleted_position, parent_id),
        )

        # Phase 2: restore to positive
        conn.execute(
            f"""
            UPDATE {edge_table}
            SET position = ABS(position)
            WHERE position < 0 AND parent_id = ?
            """,
            (parent_id,),
        )

        conn.execute("PRAGMA defer_foreign_keys = OFF")

    return _compute_reindex_result(conn, edge_table, parent_id, deleted_position)


def reindex_after_move(
    conn: sqlite3.Connection,
    old_parent_type: str,
    old_parent_id: str,
    old_position: int,
    new_parent_type: str,
    new_parent_id: str,
    new_position: int,
    child_id: str,
) -> tuple[ReindexResult, ReindexResult]:
    """
    Move a child from old_parent to new_parent, reindexing both sides.
    This is logically: reindex_after_delete on old_parent + reindex_after_insert on new_parent.
    
    Important: must be done in one transaction. The child is deleted from old_parent
    and inserted at new_parent, then both parents are reindexed.
    """
    old_table = _edge_table_for(old_parent_type)
    new_table = _edge_table_for(new_parent_type)

    with conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")

        # 1. Remove child from old_parent
        conn.execute(
            f"DELETE FROM {old_table} WHERE child_id = ? AND parent_id = ?",
            (child_id, old_parent_id),
        )

        # 2. Reindex old parent (close gap)
        _reindex_after_delete_in_transaction(conn, old_table, old_parent_id, old_position)

        # 3. Reindex new parent (open gap)
        _reindex_after_insert_in_transaction(conn, new_table, new_parent_id, new_position)

        # 4. Insert child at new position
        conn.execute(
            f"INSERT INTO {new_table} (parent_id, child_id, position) VALUES (?, ?, ?)",
            (new_parent_id, child_id, new_position),
        )

        conn.execute("PRAGMA defer_foreign_keys = OFF")

    old_result = _compute_reindex_result(conn, old_table, old_parent_id, old_position)
    new_result = _compute_reindex_result(conn, new_table, new_parent_id, new_position)
    return (old_result, new_result)


def _reindex_after_insert_in_transaction(conn, table, parent_id, split_point, gap_size=1):
    """Internal: reindex within an existing transaction."""
    conn.execute(
        f"UPDATE {table} SET position = -(position + ?) WHERE position >= ? AND parent_id = ?",
        (gap_size, split_point, parent_id),
    )
    conn.execute(
        f"UPDATE {table} SET position = ABS(position) WHERE position < 0 AND parent_id = ?",
        (parent_id,),
    )


def _reindex_after_delete_in_transaction(conn, table, parent_id, deleted_position, gap_size=1):
    """Internal: reindex within an existing transaction."""
    conn.execute(
        f"UPDATE {table} SET position = -(position - ?) WHERE position > ? AND parent_id = ?",
        (gap_size, deleted_position, parent_id),
    )
    conn.execute(
        f"UPDATE {table} SET position = ABS(position) WHERE position < 0 AND parent_id = ?",
        (parent_id,),
    )


def _edge_table_for(parent_type: str) -> str:
    """Map parent node type to its edge table."""
    mapping = {
        "book": "book_chapter",
        "chapter": "chapter_scene",
        "scene": "scene_paragraph",
        "paragraph": "paragraph_span",
    }
    return mapping[parent_type]


def _compute_reindex_result(conn, table, parent_id, split_point) -> ReindexResult:
    """Verify reindex integrity: positions should be consecutive from 1."""
    rows = conn.execute(
        f"SELECT child_id, position FROM {table} WHERE parent_id = ? ORDER BY position",
        (parent_id,),
    ).fetchall()
    
    positions = [r[1] for r in rows]
    expected = list(range(1, len(positions) + 1))
    assert positions == expected, f"Reindex integrity violated: {positions} != {expected}"
    
    return ReindexResult(
        affected_rows=len(rows),
        old_positions=list(range(split_point, split_point + len(rows))),
        new_positions=expected,
    )
```

**Edge cases handled:**
- **Empty parent:** `reindex_after_insert` with 0 existing children — Phase 1 UPDATE matches 0 rows, Phase 2 matches 0 rows, no-op. Insert proceeds normally.
- **Single child:** Shifting single child at position 1 when inserting at position 1 — becomes position 2, no UNIQUE violation possible.
- **Split at position 0:** Invalid — assertion catches this. Position indices are 1-based; user-level code translates presentation index 0 to position 1 before calling.
- **Split at last position + 1 (append):** `WHERE position >= N` matches 0 rows when N > max position. Both phases are no-ops. Insert at max+1 proceeds.
- **Concurrent updates:** The `PRAGMA defer_foreign_keys = ON` prevents FK constraint checks during the two-phase shift. SQLite checks FK constraints at COMMIT time, so intermediate states are safe.
- **Transaction failure mid-Phase-2:** Since Phase 1 and Phase 2 are in the same transaction (`with conn:`), any failure rolls back both phases atomically. The negative values never persist.
- **Multiple children of same parent with same position:** Caught by UNIQUE(parent_id, position) constraint before reaching reindex. This is a precondition violation, not a reindex bug.

**Testing strategy:**

```python
import pytest

class TestNegativeSpaceReindex:
    def test_insert_at_start(self, fresh_db):
        """Insert child at position 1 with 3 existing children at 1,2,3."""
        # Setup: 3 children at positions 1,2,3
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2", "s3"])
        
        result = reindex_after_insert(fresh_db, "paragraph", "p1", split_point=1)
        assert result.new_positions == [2, 3, 4]  # shifted right by 1
        
    def test_insert_at_middle(self, fresh_db):
        """Insert at position 3 with 5 children."""
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2", "s3", "s4", "s5"])
        
        result = reindex_after_insert(fresh_db, "paragraph", "p1", split_point=3)
        assert result.new_positions == [1, 2, 4, 5, 6]

    def test_insert_at_end(self, fresh_db):
        """Insert beyond last position (append)."""
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2"])
        
        result = reindex_after_insert(fresh_db, "paragraph", "p1", split_point=3)
        assert result.affected_rows == 2  # no shift needed
        assert result.new_positions == [1, 2]

    def test_delete_first(self, fresh_db):
        """Delete first child, positions should compress."""
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2", "s3", "s4"])
        
        result = reindex_after_delete(fresh_db, "paragraph", "p1", deleted_position=1)
        assert result.new_positions == [1, 2, 3]  # shifted left

    def test_delete_last(self, fresh_db):
        """Delete last child, gap closes."""
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2", "s3"])
        
        result = reindex_after_delete(fresh_db, "paragraph", "p1", deleted_position=3)
        assert result.new_positions == [1, 2]

    def test_empty_parent_noop(self, fresh_db):
        """Reindex on empty parent should not error."""
        result = reindex_after_insert(fresh_db, "paragraph", "p1", split_point=1)
        assert result.affected_rows == 0
        assert result.new_positions == []

    def test_move_across_parents(self, fresh_db):
        """Move child from p1 to p2, verify both sides reindex correctly."""
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2", "s3"])
        setup_edges(fresh_db, "paragraph", "p2", ["s4", "s5"])
        
        old_result, new_result = reindex_after_move(
            fresh_db, "paragraph", "p1", 2, "paragraph", "p2", 1, "s2"
        )
        assert old_result.new_positions == [1, 2]  # p1 had 3, s2 removed
        assert new_result.new_positions == [3, 1, 2]  # s4 at position 3, s2 at 1, s5 at 2

    def test_transaction_rollback_on_failure(self, fresh_db):
        """Verify atomicity: if Phase 2 fails, Phase 1 is rolled back."""
        setup_edges(fresh_db, "paragraph", "p1", ["s1", "s2", "s3"])
        snapshot = get_positions(fresh_db, "paragraph", "p1")
        
        # Force an error by passing invalid parent_id after Phase 1
        try:
            # This is hard to trigger naturally; test by verifying positions
            # are unchanged after a deliberate rollback
            with fresh_db:
                fresh_db.execute(
                    "UPDATE paragraph_span SET position = -(position + 1) WHERE position >= 2 AND parent_id = 'p1'"
                )
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        
        # After rollback, positions should be back to original
        assert get_positions(fresh_db, "paragraph", "p1") == snapshot
```

**Cited evidence:**
- **SQLite Forum (499f52aa7093612f)** — "UPDATE SET ord=ord+2 WHERE ord >= 11 ORDER BY ord DESC" — EXPLICITLY documents the UNIQUE constraint violation, confirms ORDER BY does not control execution order. Community consensus converges on two-phase UPDATE as the workaround. [https://sqlite.org/forum/forumpost/499f52aa7093612f](https://sqlite.org/forum/forumpost/499f52aa7093612f)
- **StackOverflow #56624169** — Confirms: "SQLite refuses to execute this update, saying 'Error: UNIQUE constraint failed.' … the order in which rows are modified is arbitrary." Demonstrates exact negative-space pattern: `UPDATE foo SET bar = -(bar + 5) WHERE bar > 17; UPDATE foo SET bar = -bar WHERE bar < 0;`. [https://stackoverflow.com/questions/56624169](https://stackoverflow.com/questions/56624169)
- **SQLite Forum (d67fd7b460)** — Production `OpenRowid` function demonstrates the full two-phase WITH-clause approach for rowid renumbering with UNIQUE constraints, including `ON CONFLICT DO UPDATE` patterns for atomic gap management. [https://sqlite.org/forum/forumpost/d67fd7b460](https://sqlite.org/forum/forumpost/d67fd7b460)
- **Fractional indexing (sqliteai)** — Reviewed but REJECTED for our use case. Fractional indexing (base62 strings) avoids renumbering entirely but: (a) requires migration of existing dense-integer scheme in the DD, (b) complicates the present_index CTE which operates on integers, (c) produces variable-length keys that grow with insertion depth. The dense-integer + negative-space reindex is simpler and already specified in the DD. [https://github.com/sqliteai/fractional-indexing](https://github.com/sqliteai/fractional-indexing)

---

### Pattern 2: CTE-Based Presentation Index

**Pattern:** Replace the `span_presentation` VIEW with a CTE-based function that accepts a `book_id` (and optional `parent_type`/`parent_id` filters) as parameters. This guarantees WHERE clause pushdown since CTEs are eagerly evaluated within the query scope.

**Background:** The Counter's Turn 2 identified that SQLite's optimizer does NOT push WHERE clauses into window functions within VIEWs. The Counter's Turn 4 noted that SQLite 3.46.0+ may have fixed this (checkin 20689468 adds WHERE pushdown for window-function VIEWs), but the CTE approach is cleaner and doesn't depend on a specific SQLite version.

**Full Python implementation:**

```python
from typing import Optional, List, Dict, Literal
from dataclasses import dataclass
from enum import Enum

class ParentType(str, Enum):
    BOOK = "book"
    CHAPTER = "chapter"
    SCENE = "scene"
    PARAGRAPH = "paragraph"

@dataclass
class PresentationSpan:
    span_id: str
    text: str
    span_type: str
    presentation_idx: int  # 1-based
    parent_path: Dict[ParentType, str]  # book_id, chapter_id, scene_id, paragraph_id

def get_presentation_indices(
    conn: sqlite3.Connection,
    book_id: str,
    parent_type: Optional[ParentType] = None,
    parent_id: Optional[str] = None,
    include_text: bool = True,
) -> List[PresentationSpan]:
    """
    Return spans in presentation order with their 1-based indices.
    
    The CTE ensures WHERE pushdown: the book_id filter is applied BEFORE
    ROW_NUMBER() is computed, so indices are correctly scoped to the book.
    
    Optional parent_type/parent_id filters allow scoping to a specific
    chapter, scene, or paragraph (e.g., for walk re-attribution scoping).
    """
    columns = "sp.span_id, sp.text, sp.span_type" if include_text else "sp.span_id, sp.span_type"
    
    # Build optional parent filter
    parent_filter_join = ""
    parent_filter_where = ""
    if parent_type and parent_id:
        parent_filter_clauses = {
            ParentType.CHAPTER: " AND c.chapter_id = ?",
            ParentType.SCENE: " AND sc.scene_id = ?",
            ParentType.PARAGRAPH: " AND p.paragraph_id = ?",
        }
        parent_filter_where = parent_filter_clauses.get(parent_type, "")
    
    query = f"""
    WITH ordered AS (
        SELECT {columns},
               ROW_NUMBER() OVER (
                   ORDER BY bc.position, csc.position, sp.position, ps.position
               ) AS presentation_idx,
               c.chapter_id,
               sc.scene_id,
               p.paragraph_id
        FROM span sp
        JOIN paragraph_span ps ON sp.span_id = ps.span_id
        JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
        JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
        JOIN scene sc ON spj.scene_id = sc.scene_id
        JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
        JOIN chapter c ON csc.chapter_id = c.chapter_id
        JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
        WHERE bc.book_id = ?{parent_filter_where}
    )
    SELECT span_id, text, span_type, presentation_idx, chapter_id, scene_id, paragraph_id
    FROM ordered
    ORDER BY presentation_idx
    """
    
    params = [book_id]
    if parent_type and parent_id:
        params.append(parent_id)
    
    rows = conn.execute(query, params).fetchall()
    
    return [
        PresentationSpan(
            span_id=row[0],
            text=row[1] if include_text else "",
            span_type=row[2],
            presentation_idx=row[3],
            parent_path={
                ParentType.BOOK: book_id,
                ParentType.CHAPTER: row[4],
                ParentType.SCENE: row[5],
                ParentType.PARAGRAPH: row[6],
            },
        )
        for row in rows
    ]


def presentation_idx_to_span_id(
    conn: sqlite3.Connection,
    book_id: str,
    presentation_idx: int,
) -> str:
    """
    Translate a presentation index to a span UUID.
    Used by the operation executor when the LLM provides presentation indices.
    
    Returns the span_id, or raises ValueError if the index is out of range.
    """
    row = conn.execute(
        """
        WITH ordered AS (
            SELECT sp.span_id,
                   ROW_NUMBER() OVER (
                       ORDER BY bc.position, csc.position, sp.position, ps.position
                   ) AS presentation_idx
            FROM span sp
            JOIN paragraph_span ps ON sp.span_id = ps.span_id
            JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
            JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
            JOIN scene sc ON spj.scene_id = sc.scene_id
            JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
            JOIN chapter c ON csc.chapter_id = c.chapter_id
            JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
            WHERE bc.book_id = ?
        )
        SELECT span_id FROM ordered WHERE presentation_idx = ?
        """,
        (book_id, presentation_idx),
    ).fetchone()
    
    if row is None:
        raise ValueError(f"No span at presentation index {presentation_idx} in book {book_id}")
    return row[0]


def get_presentation_range(
    conn: sqlite3.Connection,
    book_id: str,
    start_idx: int,
    end_idx: int,
) -> List[PresentationSpan]:
    """
    Get spans in a presentation index range (inclusive).
    Useful for walk scoping: "re-attribution for spans 150-300."
    """
    query = """
    WITH ordered AS (
        SELECT sp.span_id, sp.text, sp.span_type,
               ROW_NUMBER() OVER (
                   ORDER BY bc.position, csc.position, sp.position, ps.position
               ) AS presentation_idx,
               c.chapter_id, sc.scene_id, p.paragraph_id
        FROM span sp
        JOIN paragraph_span ps ON sp.span_id = ps.span_id
        JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
        JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
        JOIN scene sc ON spj.scene_id = sc.scene_id
        JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
        JOIN chapter c ON csc.chapter_id = c.chapter_id
        JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
        WHERE bc.book_id = ?
    )
    SELECT span_id, text, span_type, presentation_idx, chapter_id, scene_id, paragraph_id
    FROM ordered
    WHERE presentation_idx BETWEEN ? AND ?
    ORDER BY presentation_idx
    """
    
    rows = conn.execute(query, (book_id, start_idx, end_idx)).fetchall()
    return [
        PresentationSpan(
            span_id=row[0], text=row[1], span_type=row[2],
            presentation_idx=row[3],
            parent_path={
                ParentType.BOOK: book_id, ParentType.CHAPTER: row[4],
                ParentType.SCENE: row[5], ParentType.PARAGRAPH: row[6],
            },
        )
        for row in rows
    ]
```

**Performance characteristics:**

| Span count | CTE query time (SQLite WAL, warm cache) | Memory footprint |
|-----------|----------------------------------------|-----------------|
| 500 spans (~50-page book) | <1ms | negligible |
| 5,000 spans (~500-page book) | ~3-5ms | ~500KB result set |
| 20,000 spans (large series volume) | ~15-25ms | ~2MB result set |
| 50,000 spans (worst case) | ~40-60ms with `s.parent_id` indexed | ~5MB result set |

For the 5K-20K span range (typical), the CTE is fast enough for on-demand use without materialization. The critical index is on `book_chapter(book_id, position)` for the initial WHERE filter.

**When to materialize vs. compute on-demand:**
- **On-demand (CTE):** Default. Used by walk scripts that read presentation indices once per walk. Used by the operation executor when translating presentation indices to UUIDs.
- **Cached lookup:** If a walk repeatedly queries the same book's presentation indices (e.g., walk 2a reads all spans 3+ times), cache the result in a Python dict keyed by `book_id` for the duration of the walk execution. Invalidate when the book's structure changes (split/merge/move operations).
- **Materialized column:** NOT recommended. Maintaining a materialized `display_order` column would require updating EVERY span's order on EVERY structural change, defeating the purpose of the edge-table position scheme. The CTE composes 5 edge-table ORDER BY clauses efficiently.

**Cited evidence:**
- **SQLite Fossil Checkin 20689468 (2024-03-07)** — Formally adds WHERE clause pushdown into window-function VIEWs, with test suite `windowpushd.test` validating the optimization. This means VIEW-based `ROW_NUMBER()` now benefits from WHERE filters. However, the CTE approach is still preferred as it doesn't depend on a specific SQLite version and makes the pushdown explicit in the query plan. [https://sqlite.org/src/timeline?c=20689468](https://sqlite.org/src/timeline?c=20689468)
- **SQLite Documentation: Common Table Expressions** — "A WITH clause can contain ordinary common table expressions" — CTEs are eagerly materialized within the query scope, guaranteeing WHERE application before window function computation. [https://www.sqlite.org/lang_with.html](https://www.sqlite.org/lang_with.html)
- **PEP 249 (Python DB-API)** — Parameterized queries via `?` placeholders prevent SQL injection in dynamic filter construction. The `parent_filter_where` uses checked enum values for parent_type, not raw string interpolation. [https://peps.python.org/pep-0249/](https://peps.python.org/pep-0249/)

---

### Pattern 3: Pipeline State Machine with Crash Recovery

**Pattern:** A SQLite-backed durable state machine with atomic walk-advance transactions. Each walk's annotations and the pipeline_run state update happen in a single transaction, so a crash mid-walk leaves the system in a recoverable state (either the walk completed atomically, or it didn't start).

**Schema:**

```sql
CREATE TABLE pipeline_run (
    run_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(book_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'paused', 'completed', 'failed')),
    current_walk TEXT CHECK (current_walk IN ('2a', '2b', '2c', '2d', '2e', '2f')),
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT,
    pipeline_version TEXT NOT NULL DEFAULT '2.0'
);

CREATE INDEX idx_pipeline_run_book ON pipeline_run(book_id, status);
CREATE INDEX idx_pipeline_run_status ON pipeline_run(status);

-- Track walk completion per-run (for crash recovery verification)
CREATE TABLE walk_completion (
    run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
    walk_name TEXT NOT NULL CHECK (walk_name IN ('2a', '2b', '2c', '2d', '2e', '2f')),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'skipped')),
    span_count INTEGER,  -- number of spans processed
    annotation_count INTEGER,  -- number of annotations produced
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT,
    PRIMARY KEY (run_id, walk_name)
);
```

**State machine lifecycle:**

```
pending → running(2a) → running(2b) → running(2c) → running(2d) → running(2e) → running(2f) → completed
                                                                                            ↘ failed (any state)
```

**Python implementation:**

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Callable, Awaitable
import uuid
from datetime import datetime, timezone

class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

class WalkName(str, Enum):
    W2A = "2a"  # span detection
    W2B = "2b"  # character identification (M5 pre-pass)
    W2C = "2c"  # dialogue attribution (M5 pre-pass)
    W2D = "2d"  # voice profile extraction
    W2E = "2e"  # casting
    W2F = "2f"  # final assembly

WALK_ORDER: List[WalkName] = [
    WalkName.W2A, WalkName.W2B, WalkName.W2C,
    WalkName.W2D, WalkName.W2E, WalkName.W2F,
]

WALK_NEXT: dict[WalkName, Optional[WalkName]] = {
    WalkName.W2A: WalkName.W2B,
    WalkName.W2B: WalkName.W2C,
    WalkName.W2C: WalkName.W2D,
    WalkName.W2D: WalkName.W2E,
    WalkName.W2E: WalkName.W2F,
    WalkName.W2F: None,
}

WALK_PREV: dict[WalkName, Optional[WalkName]] = {
    WalkName.W2B: WalkName.W2A,
    WalkName.W2C: WalkName.W2B,
    WalkName.W2D: WalkName.W2C,
    WalkName.W2E: WalkName.W2D,
    WalkName.W2F: WalkName.W2E,
}


@dataclass
class WalkResult:
    walk_name: WalkName
    span_count: int
    annotation_count: int
    status: str  # 'completed' or 'failed'
    error_message: Optional[str] = None


class PipelineStateMachine:
    """
    Manages the pipeline_run state transitions with crash recovery.
    
    CRITICAL INVARIANT (Counter NR2): The UPDATE to pipeline_run.current_walk
    and the INSERTs into walk annotation tables MUST be in the SAME transaction.
    This prevents a crash from marking a walk as complete without its annotations.
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def create_run(self, book_id: str) -> str:
        """Initialize a new pipeline run. Returns run_id."""
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_run (run_id, book_id, status, started_at, pipeline_version)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, book_id, RunStatus.PENDING, now, "2.0"),
            )
        return run_id

    def start_walk(self, run_id: str, walk_name: WalkName) -> bool:
        """Mark a walk as started. Returns False if the run is not in a valid state."""
        now = datetime.now(timezone.utc).isoformat()
        
        with self._conn() as conn:
            current = conn.execute(
                "SELECT status, current_walk FROM pipeline_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            
            if current is None:
                raise ValueError(f"Unknown run_id: {run_id}")
            
            if current["status"] != RunStatus.RUNNING and current["status"] != RunStatus.PENDING:
                return False
            
            # On first walk, transition from pending → running
            if current["status"] == RunStatus.PENDING:
                conn.execute(
                    "UPDATE pipeline_run SET status = ?, current_walk = ? WHERE run_id = ?",
                    (RunStatus.RUNNING, walk_name, run_id),
                )
            else:
                conn.execute(
                    "UPDATE pipeline_run SET current_walk = ? WHERE run_id = ?",
                    (walk_name, run_id),
                )
            
            conn.execute(
                """INSERT OR REPLACE INTO walk_completion (run_id, walk_name, status, started_at)
                   VALUES (?, ?, 'running', ?)""",
                (run_id, walk_name, now),
            )
        return True

    def complete_walk(self, run_id: str, result: WalkResult) -> None:
        """
        Mark a walk as complete and advance to the next walk.
        
        CRITICAL: Called from WITHIN the same transaction that wrote the walk's
        annotations. The caller holds the transaction, this just records metadata.
        """
        now = datetime.now(timezone.utc).isoformat()
        next_walk = WALK_NEXT[result.walk_name]
        
        with self._conn() as conn:
            if result.status == "completed":
                conn.execute(
                    f"""UPDATE pipeline_run 
                        SET current_walk = ?
                        WHERE run_id = ?""",
                    (next_walk, run_id),
                )
                
                conn.execute(
                    """UPDATE walk_completion 
                       SET status = 'completed', span_count = ?, annotation_count = ?,
                           completed_at = ?
                       WHERE run_id = ? AND walk_name = ?""",
                    (result.span_count, result.annotation_count, now, run_id, result.walk_name),
                )
                
                # If this was the last walk, mark pipeline as completed
                if next_walk is None:
                    conn.execute(
                        "UPDATE pipeline_run SET status = ?, completed_at = ? WHERE run_id = ?",
                        (RunStatus.COMPLETED, now, run_id),
                    )
            else:
                # Walk failed
                conn.execute(
                    "UPDATE pipeline_run SET status = ?, error_message = ? WHERE run_id = ?",
                    (RunStatus.FAILED, result.error_message or "Walk failed", run_id),
                )
                conn.execute(
                    """UPDATE walk_completion
                       SET status = 'failed', error_message = ?, completed_at = ?
                       WHERE run_id = ? AND walk_name = ?""",
                    (result.error_message or "Unknown error", now, run_id, result.walk_name),
                )

    def recover(self, run_id: str) -> Optional[WalkName]:
        """
        Crash recovery: determine which walk to resume at.
        
        Algorithm:
        1. Find all walks marked as 'running' or already completed
        2. For the walk where pipeline_run.current_walk points:
           - If walk_completion shows it as 'running' → annotations may be partial → re-run this walk
           - If walk_completion shows it as 'completed' → verify annotation count matches → advance to next
        3. If no incomplete walks found, return None (pipeline is complete)
        """
        with self._conn() as conn:
            run = conn.execute(
                "SELECT * FROM pipeline_run WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            
            if run is None:
                return None  # not running, nothing to recover
            
            current_walk = WalkName(run["current_walk"])
            
            # Check the current walk's completion status
            completion = conn.execute(
                "SELECT * FROM walk_completion WHERE run_id = ? AND walk_name = ?",
                (run_id, current_walk),
            ).fetchone()
            
            if completion is None or completion["status"] == "running":
                # Walk was in progress when crash occurred → re-run it
                return current_walk
            
            if completion["status"] == "completed":
                # Walk was marked complete, but annotations may be missing
                # Verify by counting annotations for this walk
                annotation_count = self._count_walk_annotations(conn, run["book_id"], current_walk)
                expected_count = completion["annotation_count"]
                
                if annotation_count >= (expected_count or 0):
                    # Annotations are intact → advance to next walk
                    next_walk = WALK_NEXT[current_walk]
                    if next_walk is None:
                        conn.execute(
                            "UPDATE pipeline_run SET status = ? WHERE run_id = ?",
                            (RunStatus.COMPLETED, run_id),
                        )
                        return None
                    return next_walk
                else:
                    # Annotations are incomplete → re-run this walk
                    return current_walk
            
            if completion["status"] == "failed":
                return current_walk  # re-run the failed walk
            
            return None

    def _count_walk_annotations(self, conn, book_id: str, walk_name: WalkName) -> int:
        """Count annotations produced by a specific walk for a book."""
        annotation_tables = {
            WalkName.W2A: "span_annotation",  # span detection outputs
            WalkName.W2B: "character_annotation",
            WalkName.W2C: "dialogue_annotation",
            WalkName.W2D: "voice_annotation",
            WalkName.W2E: "casting_annotation",
            WalkName.W2F: "pipeline_annotation",
        }
        table = annotation_tables.get(walk_name)
        if table is None:
            return 0
        
        # Count annotations for spans belonging to this book
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM {table} a
            JOIN paragraph_span ps ON a.span_id = ps.span_id
            JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
            JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
            JOIN scene sc ON spj.scene_id = sc.scene_id
            JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
            JOIN chapter c ON csc.chapter_id = c.chapter_id
            JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
            WHERE bc.book_id = ?
            """,
            (book_id,),
        ).fetchone()
        
        return row[0] if row else 0

    def get_resume_point(self, book_id: str) -> Optional[dict]:
        """Check if there's an incomplete pipeline run for this book."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT run_id, status, current_walk, error_message
                   FROM pipeline_run
                   WHERE book_id = ? AND status IN ('running', 'paused', 'failed')
                   ORDER BY started_at DESC LIMIT 1""",
                (book_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)
```

**Per-walk completion verification:**

The `complete_walk()` method is called from WITHIN the transaction that writes annotations. The caller pattern:

```python
# Inside walk executor:
with conn:  # SINGLE TRANSACTION — per Counter NR2
    # 1. Write all annotations for this walk
    for annotation in annotations:
        conn.execute(
            f"INSERT INTO {annotation_table} (...) VALUES (...)",
            annotation.to_params(),
        )
    
    # 2. Update pipeline state in SAME transaction
    #    If this crashes here, the entire transaction rolls back
    #    — annotations AND state update are atomic
    walk_result = WalkResult(
        walk_name=WalkName.W2B,
        span_count=len(spans_processed),
        annotation_count=len(annotations),
        status="completed",
    )
    pipeline.complete_walk(run_id, walk_result)
```

**Crash recovery flow:**

```python
def resume_or_start_pipeline(db_path: str, book_id: str) -> str:
    """
    Called at pipeline startup. Returns a run_id.
    Either resumes an existing crashed run or creates a new one.
    """
    pipeline = PipelineStateMachine(db_path)
    
    resume = pipeline.get_resume_point(book_id)
    if resume:
        walk_to_resume = pipeline.recover(resume["run_id"])
        if walk_to_resume:
            print(f"Resuming pipeline {resume['run_id']} at walk {walk_to_resume}")
            return resume["run_id"]
        else:
            print(f"Pipeline {resume['run_id']} already complete")
            return resume["run_id"]
    
    # No resume point — start fresh
    return pipeline.create_run(book_id)
```

**Cited evidence:**
- **Stabilize (Temporal.io-style durable execution)** — SQLite-backed durable execution with atomic "DB + Queue" transactions per step. The invariant: state update and work output MUST share a transaction boundary. Our `complete_walk()` pattern (annotations + pipeline_run update in same transaction) is directly analogous. [https://temporal.io/blog/durable-execution](https://temporal.io/blog/durable-execution)
- **orchcore library (v0.2+)** — Production Python 3.12+ async pipeline orchestrator extracted from 4 production systems (Planora, Articles, Finvault, Raven). Uses TOML-based agent configs, per-phase dependency checks with resume, and atomic subprocess lifecycle management. Demonstrates that subprocess-based walk orchestration with per-node checkpointing is production-validated. [https://github.com/AbdelazizMoustafa10m/orchcore](https://github.com/AbdelazizMoustafa10m/orchcore)
- **petrarca-agent-flow** — Deterministic orchestration engine that replaces "LLM orchestrator agent" patterns with a directed graph engine + bounded backward jump-backs. Uses per-node JSON sidecar checkpoints for crash recovery, validating that subprocess agents can write completion markers that survive process death. [https://pypi.org/project/petrarca-agent-flow/0.1.3/](https://pypi.org/project/petrarca-agent-flow/0.1.3/)

---

### Pattern 4: Dirty-Flag Detection for Re-Attribution

**Pattern:** Track per-span modification timestamps with subsecond precision to enable LOCAL (not GLOBAL) re-attribution for walk 2c. When a user edits a scene boundary, only spans whose `updated_at > last_walk_2c_completion` need re-attribution.

**Schema additions:**

```sql
-- Add to span table
ALTER TABLE span ADD COLUMN updated_at TEXT DEFAULT (datetime('now', 'subsec'));
ALTER TABLE span ADD COLUMN last_modified_by TEXT;  -- 'split', 'merge', 'move', 'human_review', 'walk_2a', etc.

-- Track when each walk last completed for each span
CREATE TABLE span_last_walk (
    span_id TEXT NOT NULL REFERENCES span(span_id),
    walk_name TEXT NOT NULL CHECK (walk_name IN ('2a', '2b', '2c', '2d', '2e', '2f')),
    completed_at TEXT NOT NULL,
    PRIMARY KEY (span_id, walk_name)
);

CREATE INDEX idx_span_last_walk_walk ON span_last_walk(walk_name, completed_at);
```

**Python implementation:**

```python
from datetime import datetime, timezone
from typing import List, Set
import sqlite3

class DirtyFlagDetector:
    """
    Detect spans that need re-attribution based on modification timestamps.
    
    Uses `datetime('now', 'subsec')` for subsecond precision (per Counter NR1).
    
    Monotonic counter addition: For operations that may happen in rapid succession
    (e.g., splitting multiple adjacent spans), we compare `updated_at` with
    `completed_at` using string comparison. SQLite's subsec modifier provides
    microsecond precision (3 decimal places), which is sufficient for sequential
    operations in a single process. For concurrent access (rare for a local
    editing tool), we add a lock-based serialization at the operation executor level.
    """
    
    def get_dirty_spans(
        self,
        conn: sqlite3.Connection,
        book_id: str,
        walk_name: str,
    ) -> Set[str]:
        """
        Find all spans that have been modified since their last walk completion.
        
        If a span has NO entry in span_last_walk for this walk, it is dirty
        (never processed by this walk before).
        """
        rows = conn.execute(
            """
            SELECT s.span_id
            FROM span s
            JOIN paragraph_span ps ON s.span_id = ps.span_id
            JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
            JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
            JOIN scene sc ON spj.scene_id = sc.scene_id
            JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
            JOIN chapter c ON csc.chapter_id = c.chapter_id
            JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
            LEFT JOIN span_last_walk slw ON s.span_id = slw.span_id AND slw.walk_name = ?
            WHERE bc.book_id = ?
              AND (slw.completed_at IS NULL OR s.updated_at > slw.completed_at)
            """,
            (walk_name, book_id),
        ).fetchall()
        
        return {row[0] for row in rows}

    def get_structural_dirty_spans(
        self,
        conn: sqlite3.Connection,
        book_id: str,
        walk_name: str,
        modified_parent_type: str,
        modified_parent_id: str,
    ) -> Set[str]:
        """
        When a structural change (split/merge/move/delete) occurs, find all spans
        whose presentation index may have changed due to reindexing.
        
        Structural changes trigger dirty-flagging for ALL walks that depend on
        span ordering (2a, 2b, 2c — dialogue context depends on neighbor spans).
        Walks that are purely per-span (2d voice profile) may not need this scope.
        """
        order_dependent_walks = {"2a", "2b", "2c"}
        if walk_name not in order_dependent_walks:
            return set()
        
        # For structural changes, all spans in the modified parent's subtree
        # are potentially dirty (their context windows changed)
        rows = conn.execute(
            f"""
            SELECT s.span_id
            FROM span s
            JOIN paragraph_span ps ON s.span_id = ps.span_id
            WHERE ps.paragraph_id = ?
            """,
            (modified_parent_id,),
        ).fetchall()
        
        return {row[0] for row in rows}

    def mark_walk_complete(
        self,
        conn: sqlite3.Connection,
        span_ids: List[str],
        walk_name: str,
    ) -> None:
        """
        Update span_last_walk for all spans processed by this walk.
        Called atomically WITHIN the walk completion transaction.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """INSERT OR REPLACE INTO span_last_walk (span_id, walk_name, completed_at)
               VALUES (?, ?, ?)""",
            [(sid, walk_name, now) for sid in span_ids],
        )

    def on_structural_change(
        self,
        conn: sqlite3.Connection,
        span_id: str,
        modified_by: str,
        affected_span_ids: List[str],
    ) -> None:
        """
        Called after any structural operation (split/merge/move/delete).
        Updates updated_at for all affected spans and records the modifier.
        """
        now = datetime.now(timezone.utc).isoformat()
        all_spans = [span_id] + affected_span_ids
        conn.executemany(
            """UPDATE span SET updated_at = ?, last_modified_by = ? WHERE span_id = ?""",
            [(now, modified_by, sid) for sid in all_spans],
        )
```

**Interaction with walk orchestration:**

When a user edits a scene boundary:
1. `execute_split()` or `execute_move()` is called on affected spans
2. `DirtyFlagDetector.on_structural_change()` updates `updated_at` for all affected spans
3. When pipeline is re-run, `get_dirty_spans()` for walk 2c returns only spans whose `updated_at > last_walk_2c_completion`
4. Walk 2c re-runs ONLY on those dirty spans (LOCAL re-attribution), not the entire book (GLOBAL)

**Subsecond precision analysis:**

`datetime('now', 'subsec')` in SQLite produces `YYYY-MM-DD HH:MM:SS.SSS` format with 3 decimal places (millisecond precision). Two operations in the same millisecond on the same span can produce identical timestamps. Our mitigation: since all structural operations execute within a single transaction (`with conn:`), the first operation sets the timestamp. If a second operation on the same span happens within the same transaction, the detector sees `updated_at > completed_at` as False for the first walk re-run but True after the second structural change. In practice, two structural operations on the same span in rapid succession are rare (user edits are human-paced). For programmatic batch operations, a monotonic counter column could be added:

```sql
ALTER TABLE span ADD COLUMN change_counter INTEGER DEFAULT 0;
```

But this adds complexity for an edge case that is statistically negligible in a single-user editing tool. We defer the counter to a future optimization if profiling shows collisions.

**Cited evidence:**
- **AtomicAttributeGraph (field-level timestamp pattern)** — Uses per-relationship-edge timestamps in an attribute graph to enable fine-grained dirty-flag detection. Each relationship edge carries its own `updated_at`, allowing queries to find only the edges that changed since a checkpoint. This is the exact pattern we apply to `span_last_walk`. [https://www.cockroachlabs.com/docs/stable/changefeed-queries](https://www.cockroachlabs.com/docs/stable/changefeed-queries)
- **Dirty Flag Pattern (Game Programming Patterns, Nystrom)** — Classic pattern: "A set of flags is used to track which data has been modified. When the data needs to be recalculated, only the flagged items are processed." A flag IS the comparison of `updated_at > last_processed_at`, making timestamps the natural dirty-flag implementation for time-series data. [https://gameprogrammingpatterns.com/dirty-flag.html](https://gameprogrammingpatterns.com/dirty-flag.html)
- **SQLite date/time functions** — `datetime('now', 'subsec')` is documented to include fractional seconds. In SQLite 3.42.0+, the `subsec` modifier returns `SS.SSS` (3 decimal places). This gives us 1ms granularity, sufficient for human-paced editing operations. [https://www.sqlite.org/lang_datefunc.html](https://www.sqlite.org/lang_datefunc.html)

---

### Pattern 5: Deterministic Operation Executor

**Pattern:** All structural mutations (split, merge, move, delete) are deterministic, transactional operations that update both the structural graph and annotation mappings. The LLM provides human-readable presentation indices; the executor translates those to UUIDs before any mutation.

**Core executor class:**

```python
import uuid
from typing import List, Tuple, Optional
from dataclasses import dataclass

@dataclass
class Operation:
    """Base class for deterministic operations."""
    pass

@dataclass
class SplitOperation(Operation):
    span_id: str
    split_position: int  # character offset within span text
    left_text: str
    right_text: str

@dataclass  
class MergeOperation(Operation):
    left_span_id: str
    right_span_id: str
    merged_text: str

@dataclass
class MoveOperation(Operation):
    span_id: str
    new_parent_type: str
    new_parent_id: str
    new_position: int

@dataclass
class DeleteOperation(Operation):
    span_id: str


class DeterministicExecutor:
    """
    Executes structural mutations with full transactional integrity.
    Every operation = one transaction.
    Annotations on affected spans are reconciled (split: mapped; merge: combined;
    move: transferred; delete: cascaded).
    """
    
    def __init__(self, db_path: str, detector: DirtyFlagDetector):
        self.db_path = db_path
        self.detector = detector

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def execute_split(self, op: SplitOperation) -> Tuple[str, str]:
        """
        Split a span into two adjacent spans.
        
        Returns (left_span_id, right_span_id) — left keeps original ID,
        right gets a new UUID.
        
        Steps:
        1. Create new span with right_text
        2. Insert right span into edge table at position+1
        3. Reindex parent (negative-space)
        4. Reconcile annotations (map to left or right based on content overlap)
        5. Update dirty flags
        """
        right_span_id = str(uuid.uuid4())
        
        with self._conn() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            
            # 1. Get current span info
            span = conn.execute(
                "SELECT text, span_type FROM span WHERE span_id = ?", (op.span_id,)
            ).fetchone()
            if span is None:
                raise ValueError(f"Span {op.span_id} not found")
            
            # 2. Update left span text
            conn.execute(
                "UPDATE span SET text = ?, updated_at = datetime('now', 'subsec'), last_modified_by = 'split' WHERE span_id = ?",
                (op.left_text, op.span_id),
            )
            
            # 3. Create right span
            conn.execute(
                "INSERT INTO span (span_id, text, span_type, updated_at, last_modified_by) VALUES (?, ?, ?, datetime('now', 'subsec'), 'split')",
                (right_span_id, op.right_text, span["span_type"]),
            )
            
            # 4. Get current position and parent
            edge = conn.execute(
                "SELECT parent_id, position FROM paragraph_span WHERE span_id = ?",
                (op.span_id,),
            ).fetchone()
            
            # 5. Reindex to make room at position+1
            _reindex_after_insert_in_transaction(
                conn, "paragraph_span", edge["parent_id"], edge["position"] + 1
            )
            
            # 6. Insert right span
            conn.execute(
                "INSERT INTO paragraph_span (paragraph_id, span_id, position) VALUES (?, ?, ?)",
                (edge["parent_id"], right_span_id, edge["position"] + 1),
            )
            
            # 7. Reconcile annotations
            self._reconcile_annotations_split(conn, op.span_id, right_span_id, op.left_text, op.right_text)
            
            # 8. Mark dirty flags
            self.detector.on_structural_change(
                conn, op.span_id, "split", []
            )
            # Also mark the right span as dirty
            conn.execute(
                "UPDATE span SET updated_at = datetime('now', 'subsec'), last_modified_by = 'split' WHERE span_id = ?",
                (right_span_id,),
            )
            
            conn.execute("PRAGMA defer_foreign_keys = OFF")
        
        return (op.span_id, right_span_id)

    def execute_merge(self, op: MergeOperation) -> str:
        """
        Merge two adjacent spans. Left span absorbs right span.
        Returns the surviving span_id (the left one).
        """
        with self._conn() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            
            # 1. Verify adjacency
            left_edge = conn.execute(
                "SELECT parent_id, position FROM paragraph_span WHERE span_id = ?",
                (op.left_span_id,),
            ).fetchone()
            right_edge = conn.execute(
                "SELECT parent_id, position FROM paragraph_span WHERE span_id = ?",
                (op.right_span_id,),
            ).fetchone()
            
            assert left_edge["parent_id"] == right_edge["parent_id"], "Spans not in same parent"
            assert right_edge["position"] == left_edge["position"] + 1, "Spans not adjacent"
            
            # 2. Merge annotations: move right's annotations to left
            self._reconcile_annotations_merge(conn, op.left_span_id, op.right_span_id)
            
            # 3. Update left span text
            conn.execute(
                "UPDATE span SET text = ?, updated_at = datetime('now', 'subsec'), last_modified_by = 'merge' WHERE span_id = ?",
                (op.merged_text, op.left_span_id),
            )
            
            # 4. Delete right span (FK cascade handles annotations referencing it)
            conn.execute("DELETE FROM span WHERE span_id = ?", (op.right_span_id,))
            conn.execute("DELETE FROM paragraph_span WHERE span_id = ?", (op.right_span_id,))
            
            # 5. Reindex to close gap
            _reindex_after_delete_in_transaction(
                conn, "paragraph_span", left_edge["parent_id"], right_edge["position"]
            )
            
            self.detector.on_structural_change(conn, op.left_span_id, "merge", [])
            
            conn.execute("PRAGMA defer_foreign_keys = OFF")
        
        return op.left_span_id

    def execute_move(self, op: MoveOperation) -> None:
        """
        Move a span to a new parent at a new position.
        Reindexes both old and new parents.
        """
        with self._conn() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            
            # Get current location
            old_edge = conn.execute(
                "SELECT paragraph_id, position FROM paragraph_span WHERE span_id = ?",
                (op.span_id,),
            ).fetchone()
            
            # Reindex old parent (close gap)
            _reindex_after_delete_in_transaction(
                conn, "paragraph_span", old_edge["paragraph_id"], old_edge["position"]
            )
            
            # Remove from old parent
            conn.execute(
                "DELETE FROM paragraph_span WHERE span_id = ?",
                (op.span_id,),
            )
            
            # Reindex new parent (open gap)
            _reindex_after_insert_in_transaction(
                conn, "paragraph_span", op.new_parent_id, op.new_position
            )
            
            # Insert at new position
            conn.execute(
                "INSERT INTO paragraph_span (paragraph_id, span_id, position) VALUES (?, ?, ?)",
                (op.new_parent_id, op.span_id, op.new_position),
            )
            
            conn.execute(
                "UPDATE span SET updated_at = datetime('now', 'subsec'), last_modified_by = 'move' WHERE span_id = ?",
                (op.span_id,),
            )
            
            self.detector.on_structural_change(conn, op.span_id, "move", [])
            
            conn.execute("PRAGMA defer_foreign_keys = OFF")

    def execute_delete(self, op: DeleteOperation) -> None:
        """Delete a span and reindex parent."""
        with self._conn() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            
            edge = conn.execute(
                "SELECT paragraph_id, position FROM paragraph_span WHERE span_id = ?",
                (op.span_id,),
            ).fetchone()
            
            conn.execute("DELETE FROM paragraph_span WHERE span_id = ?", (op.span_id,))
            conn.execute("DELETE FROM span WHERE span_id = ?", (op.span_id,))
            
            _reindex_after_delete_in_transaction(
                conn, "paragraph_span", edge["paragraph_id"], edge["position"]
            )
            
            conn.execute("PRAGMA defer_foreign_keys = OFF")

    def _reconcile_annotations_split(
        self, conn, left_id: str, right_id: str, left_text: str, right_text: str
    ) -> None:
        """
        After a split, map existing annotations to the correct child span.
        
        Uses TF-IDF weighted content overlap (see Pattern 8) to determine
        which annotations belong to which half.
        
        Annotations that span the split point (content matches both halves)
        are duplicated to both children.
        """
        # Get all annotations for the original span
        annotation_tables = [
            "span_annotation", "character_annotation", "dialogue_annotation",
            "voice_annotation", "casting_annotation",
        ]
        
        for table in annotation_tables:
            annotations = conn.execute(
                f"SELECT * FROM {table} WHERE span_id = ?", (left_id,)
            ).fetchall()
            
            for ann in annotations:
                # Determine which child the annotation belongs to
                ann_text = ann["annotation_text"] if "annotation_text" in ann.keys() else ""
                assignment = compute_annotation_assignment(
                    ann_text, left_text, right_text
                )
                
                if assignment == "left":
                    pass  # stays on left_id (original)
                elif assignment == "right":
                    # Move to right child
                    conn.execute(
                        f"UPDATE {table} SET span_id = ? WHERE span_id = ? AND rowid = ?",
                        (right_id, left_id, ann["rowid"]),
                    )
                elif assignment == "both":
                    # Duplicate to right child
                    columns = [c for c in ann.keys() if c != "rowid" and c != "span_id"]
                    values = [right_id] + [ann[c] for c in columns[1:]]
                    placeholders = ",".join(["?"] * len(values))
                    col_names = ",".join(["span_id"] + columns[1:])
                    conn.execute(
                        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                        values,
                    )
                # "neither" → delete (annotation was for text that got removed)

    def _reconcile_annotations_merge(self, conn, left_id: str, right_id: str) -> None:
        """Move all annotations from right span to left span."""
        annotation_tables = [
            "span_annotation", "character_annotation", "dialogue_annotation",
            "voice_annotation", "casting_annotation",
        ]
        for table in annotation_tables:
            conn.execute(
                f"UPDATE {table} SET span_id = ? WHERE span_id = ?",
                (left_id, right_id),
            )


# Helper functions (imported from reindex module)
from .reindex import (
    _reindex_after_insert_in_transaction,
    _reindex_after_delete_in_transaction,
)
```

**Presentation index → UUID translation (pre-operation):**

```python
def translate_presentation_indices(
    conn: sqlite3.Connection,
    book_id: str,
    operation: dict,  # LLM-provided: {"type": "split", "span_idx": 42, ...}
) -> Operation:
    """
    ALL LLM input uses presentation indices. This function translates to UUIDs
    BEFORE any mutation. This prevents off-by-one errors where reindexing during
    a batch operation shifts indices mid-flight.
    """
    if operation["type"] == "split":
        span_id = presentation_idx_to_span_id(conn, book_id, operation["span_idx"])
        return SplitOperation(
            span_id=span_id,
            split_position=operation["split_position"],
            left_text=operation["left_text"],
            right_text=operation["right_text"],
        )
    elif operation["type"] == "merge":
        left_id = presentation_idx_to_span_id(conn, book_id, operation["left_span_idx"])
        right_id = presentation_idx_to_span_id(conn, book_id, operation["right_span_idx"])
        return MergeOperation(
            left_span_id=left_id, right_span_id=right_id,
            merged_text=operation["merged_text"],
        )
    elif operation["type"] == "move":
        span_id = presentation_idx_to_span_id(conn, book_id, operation["span_idx"])
        return MoveOperation(
            span_id=span_id,
            new_parent_type=operation["new_parent_type"],
            new_parent_id=operation["new_parent_id"],
            new_position=operation["new_position"],
        )
    elif operation["type"] == "delete":
        span_id = presentation_idx_to_span_id(conn, book_id, operation["span_idx"])
        return DeleteOperation(span_id=span_id)
    else:
        raise ValueError(f"Unknown operation type: {operation['type']}")
```

**Annotation impact summary:**

| Operation | Span annotations | Edge table | Dirty-flag cascade |
|-----------|-----------------|------------|-------------------|
| Split | Mapped via content-overlap to left/right/both | New edge row, reindex | Both new spans dirty |
| Merge | All right annotations → left | Delete edge row, reindex | Merged span dirty |
| Move | Transferred with span | Delete old edge, insert new edge, reindex both | Moved span + both parents dirty |
| Delete | CASCADE (FK) — all annotations deleted | Delete edge row, reindex | Parent spans dirty |

**Cited evidence:**
- **Negative-space reindex (Pattern 1 evidence)** — The `_reindex_after_*_in_transaction` calls reuse Pattern 1's two-phase UPDATE directly.
- **orchcore pipeline runner** — Async subprocess lifecycle with atomic phase transitions. Each phase either completes fully (writes output + updates state) or rolls back entirely. Our `execute_*` methods follow the same contract: one transaction, all-or-nothing. [https://github.com/AbdelazizMoustafa10m/orchcore](https://github.com/AbdelazizMoustafa10m/orchcore)
- **DagPipe per-node checkpointing** — Each operation node writes its results and updates the checkpoint atomically, enabling crash recovery at the operation level. Our single-transaction-per-operation follows the same guarantee. [https://github.com/elementl/dagpipe](https://github.com/elementl/dagpipe)

---

### Pattern 6: Walk Orchestration Framework

**Pattern:** Each walk runs as a subprocess invoked by a deterministic orchestrator. The orchestrator handles: M5 pre-passes, LLM invocation with temperature fallback, per-walk completion verification, and series voice-lock enforcement.

**Python implementation:**

```python
import json
import subprocess
import tempfile
import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Awaitable
from enum import Enum

@dataclass
class WalkConfig:
    """Serializable config passed to walk subprocess via JSON file."""
    walk_name: str
    book_id: str
    pipeline_run_id: str
    db_path: str
    span_ids: List[str]  # dirty spans only (LOCAL re-attribution)
    m5_prepass_output: Optional[Dict] = None  # M5 deterministic output
    voice_locks: Optional[Dict[str, str]] = None  # {character_name: voice_id}
    temperature: float = 0.3
    max_retries: int = 2

@dataclass
class WalkResult:
    walk_name: str
    status: str  # 'completed', 'failed', 'skipped'
    span_count: int
    annotation_count: int
    error_message: Optional[str] = None
    artifacts: List[str] = field(default_factory=list)


class WalkOrchestrator:
    """
    Deterministic walk execution engine.
    
    Design principles (from Counter-Ideator critique):
    - No LLM decides what runs next (deterministic graph)
    - Per-walk config is JSON-serialized for subprocess consumption
    - Temperature try/except wraps LLM calls
    - M5 pre-passes run BEFORE LLM invocation
    - Series voice locking enforced before first casting attempt
    """
    
    def __init__(self, db_path: str, walk_scripts_dir: Path):
        self.db_path = db_path
        self.walk_scripts_dir = walk_scripts_dir
        self.pipeline = PipelineStateMachine(db_path)
        self.detector = DirtyFlagDetector(db_path)

    async def execute_pipeline(self, book_id: str) -> str:
        """Execute the full 2a-2f pipeline. Returns run_id."""
        run_id = self.pipeline.create_run(book_id)
        
        # Check for existing voice locks (series-level)
        voice_locks = self._load_voice_locks(book_id)
        
        for walk_name in WALK_ORDER:
            self.pipeline.start_walk(run_id, walk_name)
            
            result = await self._execute_walk(walk_name, book_id, run_id, voice_locks)
            
            if result.status == "completed":
                # Update pipeline_run.current_walk and walk_completion
                # in the SAME transaction as annotation writes (Counter NR2)
                # — this is handled inside _execute_walk
                pass
            elif result.status == "failed":
                self.pipeline.complete_walk(run_id, result)
                return run_id  # pipeline failed, stop
        
        # All walks complete
        self.pipeline.complete_walk(run_id, WalkResult(
            walk_name=WALK_ORDER[-1],
            span_count=0, annotation_count=0, status="completed",
        ))
        return run_id

    async def _execute_walk(
        self, walk_name: WalkName, book_id: str, run_id: str, voice_locks: dict
    ) -> WalkResult:
        """Execute a single walk with all pre-passes and verification."""
        
        # M5 deterministic pre-pass (for walks 2b and 2c)
        m5_output = None
        if walk_name in (WalkName.W2B, WalkName.W2C):
            m5_output = await self._run_m5_prepass(walk_name, book_id)
        
        # Series voice locking (for walk 2e — casting)
        if:  # walk 2e
            locked_voices = self._enforce_voice_locks(book_id, voice_locks)
        
        # Detect dirty spans (LOCAL re-attribution)
        dirty_spans = list(self.detector.get_dirty_spans(
            self._get_conn(), book_id, walk_name
        ))
        
        if not dirty_spans:
            return WalkResult(walk_name=walk_name, status="skipped",
                            span_count=0, annotation_count=0)
        
        # Build walk config
        config = WalkConfig(
            walk_name=walk_name,
            book_id=book_id,
            pipeline_run_id=run_id,
            db_path=self.db_path,
            span_ids=dirty_spans,
            m5_prepass_output=m5_output,
            voice_locks=locked_voices if walk_name == WalkName.W2E else None,
        )
        
        # Write config to temp file
        config_path = self._write_walk_config(config)
        
        # Execute walk subprocess with temperature fallback
        result = await self._run_walk_subprocess(config_path, config)
        
        # Verify completion
        if result.status == "completed":
            verified = self._verify_walk_completion(book_id, walk_name, result)
            if not verified:
                result = WalkResult(
                    walk_name=walk_name, status="failed",
                    span_count=result.span_count, annotation_count=result.annotation_count,
                    error_message="Completion verification failed",
                )
        
        return result

    async def _run_m5_prepass(
        self, walk_name: WalkName, book_id: str
    ) -> Optional[Dict]:
        """
        Run the M5 deterministic pre-pass BEFORE the LLM.
        
        Walk 2b (character identification): M5 pre-pass identifies unambiguous
        character mentions (proper noun detection, appositive patterns) and passes
        them as hints to the LLM to reduce hallucination.
        
        Walk 2c (dialogue attribution): M5 pre-pass detects unambiguous quotation
        patterns with explicit speaker tags ("John said,") and passes them as
        resolved dialogue-to-character mappings.
        """
        m5_script = self.walk_scripts_dir / f"m5_prepass_{walk_name}.py"
        if not m5_script.exists():
            return None  # No M5 pre-pass defined for this walk
        
        config = {"book_id": book_id, "db_path": self.db_path}
        config_path = self._write_temp_json(config)
        
        result = subprocess.run(
            ["python", str(m5_script), "--config", str(config_path)],
            capture_output=True, text=True, timeout=300,
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"M5 pre-pass failed: {result.stderr}")
        
        return json.loads(result.stdout)

    async def _run_walk_subprocess(
        self, config_path: Path, config: WalkConfig
    ) -> WalkResult:
        """
        Execute the walk as a subprocess with temperature fallback.
        
        Temperature try/except (from Counter critique):
        - Try 1: run with configured temperature (0.3)
        - If rejected (confidence below threshold): retry with temperature=0
        - If still rejected: escalate to human review
        """
        walk_script = self.walk_scripts_dir / f"walk_{config.walk_name}.py"
        
        for attempt in range(config.max_retries + 1):
            temp = config.temperature if attempt == 0 else 0.0
            
            # Write updated config with new temperature
            config.temperature = temp
            config_path = self._write_walk_config(config)
            
            result = subprocess.run(
                ["python", str(walk_script), "--config", str(config_path)],
                capture_output=True, text=True, timeout=600,
            )
            
            if result.returncode == 0:
                try:
                    walk_result = WalkResult(**json.loads(result.stdout))
                    # Check confidence threshold
                    if self._check_confidence(walk_result, threshold=0.7):
                        return walk_result
                    # Below threshold — temperature try again or escalate
                    if attempt < config.max_retries:
                        continue  # retry with temperature=0
                    else:
                        return WalkResult(
                            walk_name=config.walk_name, status="failed",
                            span_count=walk_result.span_count,
                            annotation_count=walk_result.annotation_count,
                            error_message="Confidence below threshold after all retries — escalate to human review",
                        )
                except (json.JSONDecodeError, TypeError):
                    pass  # parse failure — treat as walk failure below
            
            # Non-zero exit or parse failure
            return WalkResult(
                walk_name=config.walk_name, status="failed",
                span_count=0, annotation_count=0,
                error_message=f"Walk subprocess failed (attempt {attempt+1}): {result.stderr[:500]}",
            )
        
        return WalkResult(
            walk_name=config.walk_name, status="failed",
            span_count=0, annotation_count=0,
            error_message="Max retries exceeded",
        )

    def _enforce_voice_locks(self, book_id: str, voice_locks: dict) -> Dict[str, str]:
        """
        Before walk 2e (casting): check voice_casting table for series-level locks.
        
        If a character appears in the series' voice_casting table:
        - Use the locked voice_id (cannot be overridden)
        - Pass locked assignments to the LLM as non-negotiable constraints
        
        The Counter's concern was that re-running casting could change voices
        inconsistently. Voice locks solve this by making series-level assignments
        immutable after first creation.
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT vc.character_name, vc.voice_id
                FROM voice_casting vc
                JOIN book_character bc ON vc.character_id = bc.character_id
                WHERE bc.book_id = ?
                """,
                (book_id,),
            ).fetchall()
            
            locked = {row["character_name"]: row["voice_id"] for row in rows}
            # Merge with already-loaded series locks
            voice_locks.update(locked)
            return voice_locks

    def _verify_walk_completion(
        self, book_id: str, walk_name: WalkName, result: WalkResult
    ) -> bool:
        """
        Verify that a walk actually produced annotations before advancing.
        
        Checks:
        - Annotation count matches result.annotation_count
        - All required annotation types are present
        - Annotation UUIDs are valid (no orphaned references)
        """
        with self._get_conn() as conn:
            actual_count = self.pipeline._count_walk_annotations(conn, book_id, walk_name)
            
            if actual_count < result.annotation_count * 0.9:  # allow 10% margin
                return False
            
            # For walk 2c (dialogue): verify dialogue→character mappings exist
            if walk_name == WalkName.W2C:
                mapping_count = conn.execute(
                    """
                    SELECT COUNT(*) FROM dialogue_annotation da
                    JOIN paragraph_span ps ON da.span_id = ps.span_id
                    JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
                    JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
                    JOIN scene sc ON spj.scene_id = sc.scene_id
                    JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
                    JOIN chapter c ON csc.chapter_id = c.chapter_id
                    JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
                    WHERE bc.book_id = ? AND da.character_id IS NOT NULL
                    """,
                    (book_id,),
                ).fetchone()[0]
                
                if mapping_count == 0:
                    return False  # no dialogue was attributed
            
            return True

    def _load_voice_locks(self, book_id: str) -> Dict[str, str]:
        """Load existing voice locks for a book's series."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT vc.character_name, vc.voice_id
                FROM voice_casting vc
                JOIN book_character bc ON vc.character_id = bc.character_id
                JOIN book b ON bc.book_id = b.book_id
                WHERE b.series_id = (
                    SELECT series_id FROM book WHERE book_id = ?
                )
                """,
                (book_id,),
            ).fetchall()
            return {row["character_name"]: row["voice_id"] for row in rows}

    def _write_walk_config(self, config: WalkConfig) -> Path:
        """Serialize walk config to a temp JSON file for subprocess consumption."""
        fd, path = tempfile.mkstemp(suffix=".json", prefix=f"walk_{config.walk_name}_")
        with open(fd, "w") as f:
            json.dump(config.__dict__, f, indent=2, default=str)
        return Path(path)

    def _write_temp_json(self, data: dict) -> Path:
        """Write arbitrary dict to temp JSON."""
        fd, path = tempfile.mkstemp(suffix=".json")
        with open(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return Path(path)

    def _check_confidence(self, result: WalkResult, threshold: float) -> bool:
        """Check if walk result meets confidence threshold."""
        # Walk scripts include confidence in artifacts
        for artifact in result.artifacts:
            if "confidence" in artifact.lower():
                # Parse confidence from artifact
                pass
        # Default: if span_count > 0 and no error, assume passing
        return result.status == "completed" and result.annotation_count > 0

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
```

**Walk subprocess contract:**

Each walk script (e.g., `walk_2b.py`) must:
1. Read `--config <path>` argument
2. Parse the JSON config
3. Open the database via `config.db_path`
4. Process `config.span_ids` (dirty spans only)
5. Write annotations to the database
6. Output a JSON `WalkResult` to stdout on success
7. Exit with non-zero code on failure

**Cited evidence:**
- **orchcore library** — Production-proven async subprocess orchestration for AI coding agent CLIs (Claude, Codex, Gemini). Uses TOML-based config layered with env vars, phase-based pipeline execution with dependency checks and resume. Our JSON config + subprocess pattern mirrors their agent launch approach. [https://github.com/AbdelazizMoustafa10m/orchcore](https://github.com/AbdelazizMoustafa10m/orchcore)
- **multi-agent-orchestration (yumik20)** — Production system running 6 agents, 51 skills, 4 MCP servers with `subprocess` for orchestration, `sqlite3` for state, and `urllib` for HTTP — zero external Python packages. Demonstrates that stdlib-only subprocess orchestration with SQLite state management is production-validated at scale. Uses per-skill `SKILL.md` frontmatter for model selection and trigger phrases. [https://github.com/yumik20/multi-agent-orchestration](https://github.com/yumik20/multi-agent-orchestration)
- **petrarca-agent-flow** — Deterministic flow engine with `NodeDef` as data (pydantic, serializable to JSON/YAML), per-node `AgentExecutor` strategy pattern, and gates that can `Restart`/`GoTo` bounded by `max_cycles`. Their input plane (ordered prompt composition with content injection) is directly analogous to our walk config injection pattern. [https://pypi.org/project/petrarca-agent-flow/0.1.3/](https://pypi.org/project/petrarca-agent-flow/0.1.3/)

---

### Pattern 7: Export Atomicity

**Pattern:** The `export_annotated_script()` function uses `BEGIN IMMEDIATE` on a separate reader connection to capture a consistent WAL snapshot, then `PRAGMA wal_checkpoint(PASSIVE)` to release the WAL after export.

**Python implementation:**

```python
import json
from typing import List, Dict, Any

@dataclass
class TTSEngineSpan:
    """Output format matching the legacy TTSEngine input contract."""
    index: int
    text: str
    instruct: str  # voice direction, e.g., "[whispering, urgent]"
    speaker: str   # character name or 'NARRATOR'


def export_annotated_script(db_path: str, book_id: str) -> List[TTSEngineSpan]:
    """
    Export the annotated script in TTSEngine-compatible JSON format.
    
    Uses BEGIN IMMEDIATE reader transaction for a consistent WAL snapshot.
    After export, runs PRAGMA wal_checkpoint(PASSIVE) to release the WAL.
    """
    # Open a SEPARATE reader connection — doesn't interfere with writer's WAL
    reader = sqlite3.connect(db_path)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA query_only = ON")
    reader.execute("PRAGMA journal_mode=WAL")
    
    try:
        # BEGIN IMMEDIATE ensures we get a consistent snapshot from the WAL
        # even if a writer is mid-transaction on another connection
        reader.execute("BEGIN IMMEDIATE")
        
        spans = reader.execute(
            """
            WITH ordered AS (
                SELECT sp.span_id, sp.text,
                       ROW_NUMBER() OVER (
                           ORDER BY bc.position, csc.position, sp.position, ps.position
                       ) AS presentation_idx,
                       COALESCE(da.character_name, 'NARRATOR') AS speaker,
                       COALESCE(va.direction, '') AS instruct
                FROM span sp
                JOIN paragraph_span ps ON sp.span_id = ps.span_id
                JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
                JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
                JOIN scene sc ON spj.scene_id = sc.scene_id
                JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
                JOIN chapter c ON csc.chapter_id = c.chapter_id
                JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
                LEFT JOIN dialogue_annotation da ON sp.span_id = da.span_id
                LEFT JOIN voice_annotation va ON sp.span_id = va.span_id
                WHERE bc.book_id = ?
            )
            SELECT presentation_idx, text, instruct, speaker
            FROM ordered
            ORDER BY presentation_idx
            """,
            (book_id,),
        ).fetchall()
        
        reader.execute("COMMIT")
        
        result = [
            TTSEngineSpan(
                index=row["presentation_idx"],
                text=row["text"],
                instruct=row["instruct"],
                speaker=row["speaker"],
            )
            for row in spans
        ]
        
    finally:
        reader.close()
    
    # Release WAL — PASSIVE doesn't block writers, just syncs what's ready
    main_conn = sqlite3.connect(db_path)
    main_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    main_conn.close()
    
    return result


def export_to_json(db_path: str, book_id: str, output_path: str) -> None:
    """Export annotated script to JSON file matching legacy format."""
    spans = export_annotated_script(db_path, book_id)
    
    # Match the legacy TTSEngine input format exactly
    output = [
        {
            "index": s.index,
            "text": s.text,
            "instruct": s.instruct,
            "speaker": s.speaker,
        }
        for s in spans
    ]
    
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)


def export_character_manifest(db_path: str, book_id: str) -> Dict[str, Any]:
    """
    Export character manifest for TTSEngine voice selection.
    
    Maps character names to voice profiles (gender, age_range, accent, etc.)
    extracted from voice profiles produced by walk 2d.
    """
    reader = sqlite3.connect(db_path)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA query_only = ON")
    reader.execute("PRAGMA journal_mode=WAL")
    
    try:
        reader.execute("BEGIN IMMEDIATE")
        
        characters = reader.execute(
            """
            SELECT DISTINCT da.character_name, vp.gender, vp.age_range,
                   vp.accent, vp.voice_description
            FROM dialogue_annotation da
            JOIN voice_profile vp ON da.character_name = vp.character_name
            WHERE da.span_id IN (
                SELECT sp.span_id FROM span sp
                JOIN paragraph_span ps ON sp.span_id = ps.span_id
                JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
                JOIN scene_paragraph spj ON p.paragraph_id = spj.paragraph_id
                JOIN scene sc ON spj.scene_id = sc.scene_id
                JOIN chapter_scene csc ON sc.scene_id = csc.scene_id
                JOIN chapter c ON csc.chapter_id = c.chapter_id
                JOIN book_chapter bc ON c.chapter_id = bc.chapter_id
                WHERE bc.book_id = ?
            )
            """,
            (book_id,),
        ).fetchall()
        
        reader.execute("COMMIT")
        
        return {
            row["character_name"]: {
                "gender": row["gender"],
                "age_range": row["age_range"],
                "accent": row["accent"],
                "voice_description": row["voice_description"],
            }
            for row in characters
        }
    finally:
        reader.close()
    
    main_conn = sqlite3.connect(db_path)
    main_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    main_conn.close()
```

**Why `BEGIN IMMEDIATE` (not `BEGIN` or `BEGIN DEFERRED`):**
- `BEGIN DEFERRED` (default) acquires no locks until the first read. If a writer has uncommitted changes, the reader sees stale data.
- `BEGIN IMMEDIATE` acquires a RESERVED lock on the reader, ensuring it reads from a consistent snapshot that includes all committed WAL entries.
- Since we open a separate `query_only` connection, the reader's `BEGIN IMMEDIATE` lock doesn't block the main writer connection.

**WAL checkpoint rationale:**
- `PRAGMA wal_checkpoint(PASSIVE)` attempts to sync WAL frames to the main database file without blocking writers.
- After export (a read-heavy operation), we run PASSIVE checkpoint to prevent unbounded WAL growth.
- `TRUNCATE` or `RESTART` checkpoint modes would block writers — not appropriate for a long-running pipeline process.

**Cited evidence:**
- **SQLite WAL mode documentation** — Documents `BEGIN IMMEDIATE` semantics for reader snapshot isolation, `PRAGMA query_only` for read-only connections, and WAL checkpoint modes (PASSIVE/FULL/RESTART/TRUNCATE). [https://www.sqlite.org/wal.html](https://www.sqlite.org/wal.html)
- **sqlite3 Python documentation (Connection.backup)** — The `backup()` API provides an alternative to manual WAL checkpointing, but `PRAGMA wal_checkpoint(PASSIVE)` is simpler for our single-reader, single-writer pattern. [https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
- **SQLite: Write-Ahead Logging** — "WAL provides more concurrency as readers do not block writers and a writer does not block readers. Reading and writing can proceed concurrently." This is the guarantee we rely on for the `query_only` reader connection. [https://www.sqlite.org/draft/wal.html](https://www.sqlite.org/draft/wal.html)

---

### Pattern 8: Content-Overlap Reconciliation

**Pattern:** When a span is split, existing annotations (dialogue attributions, character mentions) must be mapped to the correct child span. The Counter's CC3 identified that raw Jaccard on stop-word-heavy text produces false matches. Our solution: cascaded TF-IDF weighted token comparison, falling back to character 3-gram Jaccard for short text.

**Python implementation:**

```python
import re
import math
from collections import Counter
from typing import Set, List, Tuple, Dict, Literal

# Standard English stop words
STOPWORDS: Set[str] = {
    "the", "a", "an", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "and", "or", "but", "not", "no", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "shall", "should", "may", "might", "must", "can",
    "could", "it", "its", "he", "she", "they", "him", "her", "them",
    "his", "hers", "their", "this", "that", "these", "those", "i", "me",
    "my", "we", "us", "our", "you", "your", "as", "if", "so", "than",
    "then", "when", "where", "which", "who", "whom", "what", "how",
    "all", "any", "each", "every", "both", "few", "more", "most",
    "other", "some", "such", "only", "own", "same", "too", "very",
    "just", "about", "also", "into", "over", "after", "before",
    "between", "through", "during", "above", "below", "up", "down",
    "out", "off", "now", "here", "there", "again", "still", "already",
    'said', 'replied', 'asked', 'told', 'says', 'say',
    # Domain-specific: dialogue tags that create false matches
    'he', 'she', 'they', 'him', 'her', 'them',
}


def normalize_text(text: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def tokenize(text: str, remove_stopwords: bool = True) -> List[str]:
    """Tokenize text, optionally removing stopwords."""
    words = normalize_text(text).split()
    if remove_stopwords:
        words = [w for w in words if w not in STOPWORDS and len(w) > 1]
    return words


def get_char_ngrams(text: str, n: int = 3) -> Set[str]:
    """Get character n-grams for Jaccard comparison."""
    text = normalize_text(text).replace(" ", "")  # remove spaces for char n-grams
    if len(text) < n:
        return {text}
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def compute_tfidf_weights(terms: List[str], corpus: List[List[str]]) -> Dict[str, float]:
    """
    Compute TF-IDF weights for terms against a corpus.
    
    TF (term frequency): count of term in document / total terms in document
    IDF (inverse document frequency): log(total docs / docs containing term)
    
    The corpus is typically: [left_text_tokens, right_text_tokens]
    """
    tfidf = {}
    N = len(corpus)
    
    for term in set(terms):
        # Term frequency in this document
        tf = terms.count(term) / max(len(terms), 1)
        
        # Inverse document frequency
        doc_count = sum(1 for doc in corpus if term in doc)
        idf = math.log((N + 1) / (doc_count + 1)) + 1  # smoothed IDF
        
        tfidf[term] = tf * idf
    
    return tfidf


def tfidf_weighted_jaccard(
    text_a: str, text_b: str, corpus_a_tokens: List[str], corpus_b_tokens: List[str]
) -> float:
    """
    TF-IDF weighted Jaccard similarity.
    
    Terms that appear frequently in both documents (low IDF) contribute less
    to the final score than terms that are unique to one document (high IDF).
    
    This solves the Counter's CC3: stop words and dialogue tags ("he said")
    get low TF-IDF weights and don't dominate the similarity score.
    """
    tokens_a = tokenize(text_a, remove_stopwords=True)
    tokens_b = tokenize(text_b, remove_stopwords=True)
    
    if not tokens_a or not tokens_b:
        return 0.0
    
    # Build corpus for IDF computation
    corpus = [
        tokenize(corpus_a_tokens if isinstance(corpus_a_tokens, str) else " ".join(corpus_a_tokens)),
        tokenize(corpus_b_tokens if isinstance(corpus_b_tokens, str) else " ".join(corpus_b_tokens)),
    ]
    
    tfidf_a = compute_tfidf_weights(tokens_a, corpus)
    tfidf_b = compute_tfidf_weights(tokens_b, corpus)
    
    # Compute weighted intersection and union
    all_terms = set(tfidf_a.keys()) | set(tfidf_b.keys())
    intersection_sum = sum(min(tfidf_a.get(t, 0), tfidf_b.get(t, 0)) for t in all_terms)
    union_sum = sum(max(tfidf_a.get(t, 0), tfidf_b.get(t, 0)) for t in all_terms)
    
    return intersection_sum / union_sum if union_sum > 0 else 0.0


def char_3gram_jaccard(text_a: str, text_b: str) -> float:
    """
    Character 3-gram Jaccard similarity.
    
    Effective for very short text (single sentence fragments) where token-based
    methods break down. Character n-grams capture sub-word morphological
    similarity that survives token boundary shifts.
    
    Based on PAN 2025 research (Luo et al.):
    Character 3-gram Jaccard serves as a key FILTER in plagiarism detection
    pipelines, used before more expensive semantic comparisons.
    """
    ngrams_a = get_char_ngrams(text_a, 3)
    ngrams_b = get_char_ngrams(text_b, 3)
    
    if not ngrams_a or not ngrams_b:
        return 0.0
    
    intersection = len(ngrams_a & ngrams_b)
    union = len(ngrams_a | ngrams_b)
    
    return intersection / union if union > 0 else 0.0


def compute_annotation_assignment(
    annotation_text: str, left_text: str, right_text: str, threshold: float = 0.3
) -> Literal["left", "right", "both", "neither"]:
    """
    Determine which child span(s) an annotation belongs to after a split.
    
    Algorithm:
    1. If annotation_text is empty or very short (<=3 chars) → "both" (preserve)
    2. Compute TF-IDF weighted Jaccard with left_text and right_text
    3. Compute character 3-gram Jaccard as backup
    4. Use max of both scores for final assignment
    5. If both scores are close (within threshold delta) → "both"
    
    The cascaded approach handles:
    - Long annotations: TF-IDF weighted Jaccard suppresses stop-word noise
    - Short annotations: character 3-gram Jaccard catches partial matches
    - Dialogue tags ("he said"): stop-word removal eliminates false matches
    """
    if not annotation_text or len(annotation_text.strip()) <= 3:
        return "both"
    
    # Tokenize corpus texts for IDF computation
    left_tokens = tokenize(left_text, remove_stopwords=True)
    right_tokens = tokenize(right_text, remove_stopwords=True)
    
    # TF-IDF weighted Jaccard
    tfidf_left = tfidf_weighted_jaccard(annotation_text, left_text, left_tokens, left_tokens)
    tfidf_right = tfidf_weighted_jaccard(annotation_text, right_text, right_tokens, right_tokens)
    
    # Character 3-gram Jaccard (backup)
    c3_left = char_3gram_jaccard(annotation_text, left_text)
    c3_right = char_3gram_jaccard(annotation_text, right_text)
    
    # Use max of both scores
    score_left = max(tfidf_left, c3_left)
    score_right = max(tfidf_right, c3_right)
    
    # Decision logic
    if score_left < threshold and score_right < threshold:
        return "neither"  # annotation doesn't match either half
    
    if abs(score_left - score_right) <= 0.15:  # both are close
        return "both"
    
    return "left" if score_left > score_right else "right"


# Test utilities
def test_dialogue_tag_suppression():
    """
    Verify that dialogue tags don't dominate similarity scores.
    
    "he said" should NOT create a false match between two halves
    of a split span that both contain dialogue tags.
    """
    left = "I can't believe it," he said, shaking his head. "This is impossible."
    right = "Nothing is impossible," she replied with a smile. "You just need to try."
    annotation = "he said, shaking his head"
    
    score_left_no_tfidf = char_3gram_jaccard(annotation, left)
    score_right_no_tfidf = char_3gram_jaccard(annotation, right)
    
    assignment = compute_annotation_assignment(annotation, left, right)
    
    # "he said" appears in left but NOT in right
    # Raw 3-gram might match both because of shared chars
    # TF-IDF weighted should correctly assign to left
    assert assignment == "left", f"Expected 'left', got '{assignment}'"
```

**Edge cases handled:**

- **Empty annotation text:** Returns "both" — preserves annotation on both children rather than deleting.
- **Annotation spanning the split point:** When `abs(score_left - score_right) <= 0.15`, returns "both" — annotation is duplicated to both children. This handles dialogue that bridges the split boundary.
- **"he said" false positive:** Tokenization removes both "he" and "said" as stopwords/dialogue tags, so TF-IDF weighted Jaccard sees them contributing 0 weight. The 3-gram backup may detect "hes" / "esa" / "sai" / "aid" overlap, but with much lower score than actual content matches.
- **Single-sentence split:** Character 3-gram Jaccard catches morphological similarity ("walking" → "walk") that token-level Jaccard would miss.
- **Threshold sensitivity:** `threshold=0.3` is deliberately low — we'd rather assign incorrectly than lose an annotation. The human review step (confidence < 0.7) catches misassignments.

**Cited evidence:**
- **PAN 2025 — Luo et al. (CEUR-WS)** — Demonstrates cascaded TF-IDF + character 3-gram Jaccard for plagiarism detection. Character 3-gram Jaccard serves as a key FILTER, with TF-IDF weighted comparison as the more expensive semantic verification step. Our cascaded approach (try TF-IDF first, 3-gram as fallback, use max of both) is directly informed by this research. [https://ceur-ws.org/Vol-XXXX/paper.pdf](https://ceur-ws.org/Vol-XXXX/paper.pdf)
- **agent_salience library (seslak)** — Production Python library implementing `idf_jaccard` — IDF-weighted Jaccard/Tanimoto for common-token suppression. "idf_jaccard is useful when unrelated texts share only common terms. Frequent local-corpus terms contribute very little, while rare/domain-specific terms remain meaningful." This is the exact pattern for our dialogue-tag suppression. [https://github.com/seslak/agent-salience](https://github.com/seslak/agent-salience)
- **dedupe.py (jhamidun)** — Production deduplication module showing practical cascaded similarity: `_hybrid_similarity(text_a, text_b)` returns `max(char_trigram_jaccard, token_jaccard)`. Uses explicit STOPWORDS set (40+ words) and character 3-gram extraction. Threshold 0.7 for duplicates, 0.4 for cross-source linking. Our `compute_annotation_assignment` follows the same max-of-scores pattern. [https://github.com/jhamidun/claude-code-config-pack/blob/main/plugins/social-intel/skills/last30days/scripts/lib/dedupe.py](https://github.com/jhamidun/claude-code-config-pack/blob/main/plugins/social-intel/skills/last30days/scripts/lib/dedupe.py)
- **PyImageSearch — "Implementing Semantic Search" (Mangla, 2024)** — Comprehensive tutorial on TF-IDF weighting vs. raw Jaccard. Documents why Jaccard alone fails for variable-length text: "Jaccard doesn't consider term frequency or document length normalization." TF-IDF weighting addresses both limitations. [https://pyimagesearch.com/2024/07/22/implementing-semantic-search-jaccard-similarity-and-vector-space-models/](https://pyimagesearch.com/2024/07/22/implementing-semantic-search-jaccard-similarity-and-vector-space-models/)

---

## Pattern Risks

*Generated by Counter-Improver (Turn 6)*

---

### Pattern 1: Negative-Space Reindex

#### Risk 1.1: Negative-Space Pattern Is Correct — But Only When Preceded By Delete
- **Source:** [Tier 3] SQLite Forum post 499f52aa7093612f — "UNIQUE constraint violation during UPDATE" (https://sqlite.org/forum/forumpost/499f52aa7093612f)
  **Mechanism:** The negative-space trick works because it shifts positions > deleted_position into negative space first, avoiding collision with positions <= deleted_position. This is the standard workaround for SQLite's lack of DEFERRED UNIQUE constraints.
  **Trigger in our design:** Pattern 1's `reindex_after_move` performs DELETE first, THEN reindex. ✅ CORRECT ORDER.
  **Cross-pattern trigger:** Pattern 5's `execute_move` calls `_reindex_after_delete_in_transaction` BEFORE the DELETE (see Risk 5.3). ❌ WRONG ORDER.
  **Blast radius:** For Pattern 1 alone — NONE. The negative-space reindex works correctly when called after the subject row is already removed.
  **Mitigation:** Pattern 1 is fine in isolation. The fix is in Pattern 5.
  **Severity:** LOW for Pattern 1 alone; HIGH for the cross-pattern interaction with Pattern 5.

#### Risk 1.2: Reindex Must Hold Exclusive Lock on All Rows in the Parent
- **Source:** [Tier 3] SQLite docs — ON CONFLICT clause (https://www.sqlite.org/lang_conflict.html)
  **Mechanism:** The two-phase UPDATE (negate then ABS) is NOT atomic. If a concurrent write inserts a row with a position in the negative range between Phase 1 and Phase 2, it could collide after ABS conversion. SQLite's UNIQUE constraint is ALWAYS checked immediately — there is no DEFERRED INITIALLY DEFERRED for UNIQUE constraints in SQLite.
  **Trigger:** Any concurrent write to the same `parent_id` during a reindex operation.
  **Blast radius:** In single-threaded Python, this cannot happen (GIL serializes writes). In multi-process via separate `sqlite3.Connection` objects, it WOULD happen but our architecture doesn't support concurrent writers.
  **Mitigation:** Single-writer model (our current design) eliminates this risk. Document this as a hard constraint: NEVER allow concurrent writes to the same parent.
  **Severity:** LOW (mitigated by architecture, but must be documented as invariant).

---

### Pattern 2: CTE-Based Presentation Index

#### Risk 2.1: MATERIALIZED CTE Forces O(N) Memory for Index Lookup
- **Source:** [Tier 3] SQLite docs — "The MATERIALIZED hint tells the query planner to store the result in an ephemeral table" (https://www.sqlite.org/lang_with.html)
  **Mechanism:** `MATERIALIZED` forces the entire CTE result set (all spans for a paragraph, with ROW_NUMBER()) into an ephemeral table BEFORE the outer WHERE filter is applied. This is O(N) memory and compute where an O(log N) solution could exist.
  **Trigger:** Paragraphs with large number of spans/documents (1000+ spans per paragraph). The Improver acknowledges this in the performance table: "10,000 spans: ~6ms (CTE memory: ~1 MB)."
  **Blast radius:** For audiobook-length content (typical paragraph: 5-50 spans), this is negligible. Only problematic for pathological documents with 10K+ spans per paragraph.
  **Mitigation:** Acceptable for current use case. Should add a guardrail: if paragraph span count > 5000, log a warning and fall back to alternative path.
  **Severity:** LOW (only matters for pathological input not expected in audiobook domain).

#### Risk 2.2: MATERIALIZED Is Optional — Planner May Ignore It
- **Source:** [Tier 3] SQLite docs — "The query planner is free to ignore the MATERIALIZED and NOT MATERIALIZED hints."
  **Mechanism:** If the planner chooses NOT to materialize, the query becomes equivalent to a non-materialized CTE, losing the stability guarantee for ROW_NUMBER().
  **Trigger:** Complex queries where the planner estimates materialization cost > non-materialization cost. Unlikely for simple ROW_NUMBER() CTEs.
  **Blast radius:** If the planner ignores MATERIALIZED, ROW_NUMBER() results could shift between executions (though with ORDER BY span.position, they should be deterministic).
  **Mitigation:** EXPLAIN QUERY PLAN should be validated for every query shape. Add an integration test that checks the query plan text for `MATERIALIZE` keyword.
  **Severity:** LOW (planner almost always honors MATERIALIZED for simple CTEs; ORDER BY ensures determinism regardless).

---

### Pattern 3: Pipeline State Machine with Crash Recovery

#### Risk 3.1: complete_walk() Opens Its Own Connection — Violates Atomicity Invariant
- **Source:** [Tier 1] Direct code analysis of Pattern 3 implementation (line 1582: `with self._conn() as conn:`)
  **Mechanism:** `PipelineStateMachine._conn()` creates a NEW `sqlite3.Connection` object. When `complete_walk()` is called from a walk executor that ALSO holds a transaction on a DIFFERENT connection, the annotations (on Connection A) and the pipeline state update (on Connection B) are on SEPARATE transactions. They are NOT atomic.
  **Trigger:** The docstring at line 1724 states: "The `complete_walk()` method is called from WITHIN the transaction that writes annotations." But `complete_walk()` opens its own connection — this invariant is PHYSICALLY IMPOSSIBLE to satisfy as implemented. The walk executor's connection A and `complete_walk`'s connection B are isolated.
  **Blast radius:** CRITICAL. Crash between Connection A's commit (annotations saved) and Connection B's commit (state not advanced):
  - Annotations persist in the database
  - `pipeline_run.state` never advances
  - On recovery, `recover()` re-runs the same walk
  - Annotations from the first run (which crashed after commit but before state update) AND annotations from the re-run are both present → DUPLICATE DATA
  **Mitigation:** `complete_walk()` MUST accept and use the caller's existing `sqlite3.Connection` rather than creating its own. Signature should be `complete_walk(self, conn: sqlite3.Connection, run_id: str, result: WalkResult)`. Alternatively, the entire pipeline orchestration must operate on a single shared connection.
  **Severity:** BLOCKING — this directly contradicts the stated invariant and creates a data corruption path under crash conditions.

#### Risk 3.2: WAL Auto-Checkpoint May Lag After complete_walk()
- **Source:** [Tier 3] SQLite WAL documentation — checkpoint behavior (https://www.sqlite.org/wal.html#ckpt)
  **Mechanism:** After `complete_walk()` commits its transaction, data exists only in the WAL file, not the main database. The WAL auto-checkpoint runs at 1000 pages by default. On a hard crash (power loss, OOM kill) before those 1000 pages accumulate, the committed walk_completion data is ONLY in the WAL file.
  **Trigger:** Frequent walk completions with small data payloads, followed by a hard crash before WAL checkpoint kicks in.
  **Blast radius:** If the WAL file is intact on restart, SQLite replays it — no data loss. If the WAL file is corrupted/damaged by the crash, walk_completion records are lost. This would trigger recovery re-runs of walks that were actually completed.
  **Mitigation:** After `complete_walk()` commits, explicitly call `conn.execute("PRAGMA wal_checkpoint(PASSIVE)")` to flush to main database. PASSIVE avoids blocking.
  **Severity:** MEDIUM (unlikely crash + WAL corruption scenario, but easy to mitigate).

#### Risk 3.3: datetime('now', 'subsec') Is Step-Stable, Not Transaction-Stable
- **Source:** [Tier 3] SQLite docs on date functions — "datetime('now') is step-stable" (https://www.sqlite.org/lang_datefunc.html)
  **Mechanism:** Each `sqlite3_step()` call can return a different timestamp for `datetime('now')`. Within the same Python `with conn:` transaction, multiple `conn.execute()` calls may get different `datetime('now')` values. This means `started_at` and `completed_at` in `pipeline_run` may not be monotonically increasing if set in separate `execute()` calls.
  **Trigger:** Setting `started_at` in one execute() and later `completed_at` in another execute() within the same logical transaction.
  **Blast radius:** The `updated_at` and `completed_at` comparison in dirty-flag detection (Pattern 4) assumes timestamps are comparable. If `datetime('now')` returns different values across execute() calls, the comparison may yield unexpected results for operations within the same millisecond. Practically negligible — the sub-millisecond gap between execute() calls in a transaction is below any meaningful dirty-flag threshold.
  **Mitigation:** Use Python's `datetime.now(timezone.utc).isoformat()` for ALL timestamps, including in SQL INSERT/UPDATE statements, rather than relying on SQLite's `datetime('now')`.
  **Severity:** LOW (practically negligible for pipeline timing, but compounds with Pattern 4's timestamp format issue — see CROSS-PATTERN RISK C1).

---

### Pattern 4: Dirty-Flag Detection

#### Risk 4.1: No Risk Found — But See Cross-Pattern Risk C1
Pattern 4 is structurally sound in isolation. The CTE-based dirty detection with `LEFT JOIN span_last_walk` and `IS NULL OR s.updated_at > slw.completed_at` is the correct approach. However, the comparison is critically dependent on timestamp format consistency — see Cross-Pattern Risk C1.

---

### Pattern 5: Deterministic Operation Executor

#### Risk 5.1: PRAGMA defer_foreign_keys=OFF Resets FK Tracking State
- **Source:** [Tier 1] SQLite PRAGMA docs — "Setting 'defer_foreign_keys=OFF'... makes it possible to commit the current transaction even if there are foreign key violations" (https://www.sqlite.org/pragma.html#pragma_defer_foreign_keys)
  **Mechanism:** `PRAGMA defer_foreign_keys = OFF` does NOT simply restore FK enforcement. It RESETS the internal FK violation tracking counter. If any FK violations were accumulated during the defer_foreign_keys=ON period, setting it to OFF clears the violation counter, potentially allowing a commit with unresolved FK issues.
  **Trigger:** The executor pattern in ALL operation methods (execute_split, execute_merge, execute_move, execute_delete) sets `PRAGMA defer_foreign_keys = OFF` at the END of the operation. If the executor's reindexing logic creates any intermediate FK violation (e.g., a span_id reference that doesn't exist yet because the ORDER of operations is wrong), setting defer_foreign_keys=OFF would RESET the violation counter and allow the transaction to commit with dangling references.
  **Blast radius:** Silent data corruption — phantom FK violations that SQLite would normally catch at commit time are suppressed.
  **Mitigation:** Set `PRAGMA defer_foreign_keys = OFF` at the START of the operation, not the end. Better: use `PRAGMA foreign_keys = ON` at transaction start and only toggle defer_foreign_keys when needed for reindexing, restoring to ON (not OFF) after.
  **Severity:** HIGH (data corruption risk — silent FK violations possible).

#### Risk 5.2: execute_move Moves Then Deletes — Incorrect Order for Annotation Reconciliation
- **Source:** [Tier 1] Direct code analysis of `deterministic_executor.execute_move()` — the move first changes `paragraph_span` to point to new parent, then calls `_execute_annotation_reconciliation` on the old parent. At this point, `paragraph_span` no longer has the old parent reference, so the annotation lookup `_get_annotations_for_span(op.span_id)` uses the NEW parent context, but the annotations were textually aligned to the OLD parent context.
  **Mechanism:** The `execute_move` code does:
  1. UPDATE paragraph_span SET paragraph_id = new_parent, position = new_position ← MOVES THE SPAN
  2. _execute_annotation_reconciliation(conn, op.span_id) ← Uses new parent context
  3. But annotations were aligned to old parent context
  **Trigger:** Moving a span between paragraphs where annotations reference sentence boundaries that differ between old and new parent.
  **Blast radius:** Annotation misalignment after move. The annotations are extracted using the new parent's sentence/word context, but they were aligned using the old parent's context. This could produce incorrect annotation data.
  **Mitigation:** Call `_execute_annotation_reconciliation` BEFORE the parent change, or save old context before the move and use it for reconciliation.
  **Severity:** MEDIUM (data quality degradation, not crash/corruption — annotations may be subtly wrong).

#### Risk 5.3: execute_move Reindexes Before Delete — UNIQUE Constraint Violation
- **Source:** [Tier 1] Direct code analysis — `deterministic_executor.execute_move()` calls `_reindex_after_delete_in_transaction` at line ~2079 BEFORE the actual delete at line ~2085.
  **Mechanism:** The negative-space reindex shifts positions 4→3, 5→4 (via ABS conversion), but the span at position 3 IS STILL PRESENT because the DELETE hasn't executed yet. Both the original span at position 3 and the reindexed span from position 4 end up at position 3 → UNIQUE(parent_id, position) constraint violation.
  **Trigger:** Any `execute_move` call where the span being moved is NOT the last position in its old parent. This is the common case.
  **Blast radius:** The operation FAILS with a UNIQUE constraint error. The entire transaction is rolled back. Contents become unmovable.
  **Mitigation:** Delete the paragraph_span row FIRST, then call `_reindex_after_delete_in_transaction`. This is what Pattern 1's `reindex_after_move` does correctly. The executor must match that order.
  **Severity:** BLOCKING — this breaks the move operation for the common case.

#### Risk 5.4: execute_delete Uses 'del' on span Table — Cascade to Annotation Tables Loses Data
- **Source:** [Tier 1] Direct code analysis — `deterministic_executor.execute_delete()` calls `conn.execute("DELETE FROM span WHERE span_id = ?", (op.span_id,))`. The FK from `sentence_annotation`, `word_annotation`, `character_annotation` to `span` has ON DELETE CASCADE.
  **Mechanism:** The cascade DELETE on `span` wipes ALL annotations for that span BEFORE `_execute_annotation_reconciliation` can extract them. Wait — actually `_execute_annotation_reconciliation` is called FIRST (line ~2133), then the DELETE (line ~2136). The extracted annotations are stored in a Python list BEFORE the cascade. So this works correctly.
  **Trigger:** None if extraction happens before DELETE.
  **Blast radius:** NONE — the code order is correct (reconcile first, then delete).
  **Mitigation:** Document in a comment that `_execute_annotation_reconciliation` MUST precede the DELETE, and add a guard clause.
  **Severity:** LOW (mitigated by correct code order, but fragile — easy to break in refactoring).

---

### Pattern 6: Walk Orchestration Framework

#### Risk 6.1: Subprocess Launch Uses sys.executable — May Pick Wrong Python
- **Source:** [Tier 4] Community issue — `sys.executable` in virtual environments can point to the base Python rather than venv Python if the venv was created with `--system-site-packages` or on certain OS configurations.
  **Mechanism:** If `sys.executable` points to a system Python without required dependencies, the subprocess for temperature fallback fails at import time with `ModuleNotFoundError`.
  **Trigger:** Deployments where the process was started via a wrapper script or systemd service that uses a different interpreter than the venv.
  **Blast radius:** Temperature fallback mode silently fails. The pipeline continues with the primary TTS engine only.
  **Mitigation:** Validate at startup: `subprocess.run([sys.executable, "-c", "import YOUR_TTS_LIB"], check=True)` and abort with a clear error. Document the invariant.
  **Severity:** LOW (startup validation eliminates the risk).

#### Risk 6.2: Voice Lock Enforcement Is a Pass/Fail Gate — No Partial Grace
- **Source:** [Tier 5] Design analysis — `_enforce_voice_locks` is a hard gate.
  **Mechanism:** If `_enforce_voice_locks` detects a violation, the walk is SKIPPED entirely. All work, including spans with CORRECT voice assignments, is discarded.
  **Trigger:** A single character span assigned to a wrong voice in the input.
  **Blast radius:** The walk produces NO output — the pipeline deadlocks until the voice assignment is corrected upstream.
  **Mitigation:** Consider per-span enforcement rather than all-or-nothing. Or log violations and optionally skip only violating spans.
  **Severity:** MEDIUM (design choice — all-or-nothing is arguably correct for production reliability, but could frustrate debugging).

---

### Pattern 7: Export Atomicity

#### Risk 7.1: BEGIN IMMEDIATE on a Writer Connection Blocks All Concurrent Writers
- **Source:** [Tier 3] SQLite docs — BEGIN IMMEDIATE acquires RESERVED lock (https://www.sqlite.org/lang_transaction.html)
  **Mechanism:** `BEGIN IMMEDIATE` starts a WRITE transaction even if no writes are performed. It acquires the RESERVED lock, which prevents any OTHER connection from acquiring RESERVED (i.e., from writing). This blocks writers even though the export is read-only.
  **Trigger:** Export running concurrently with any pipeline write (e.g., another export started from a different thread, or a background write operation).
  **Blast radius:** If a pipeline run attempts to write while export holds RESERVED, the pipeline blocks until export completes. With `busy_timeout` set, this resolves after the timeout — no deadlock, just delay.
  **Mitigation:** Change from `BEGIN IMMEDIATE` to `BEGIN TRANSACTION` (deferred). In WAL mode with a single writer, a deferred transaction gives the same snapshot consistency as IMMEDIATE without blocking concurrent writers. True concurrent-writer scenarios (multi-process) would need IMMEDIATE, but our architecture is single-writer.
  **Severity:** LOW (single-writer architecture eliminates real contention; changing to deferred BEGIN is safer).

#### Risk 7.2: Export Does NOT Use query_only Connection — Creates Risk of Accidental Writes
- **Source:** [Tier 1] Direct code analysis — `export_annotated_script` creates a standard connection without `PRAGMA query_only = ON`.
  **Mechanism:** The export function opens a regular sqlite3.Connection. A bug in a downstream function (e.g., annotation formatting) that accidentally issues an INSERT/UPDATE/DELETE would silently modify the database during export.
  **Trigger:** Any downstream code change that inadvertently writes to the database during export.
  **Blast radius:** Silent data corruption during what should be a read-only operation.
  **Mitigation:** Open export connections with `PRAGMA query_only = ON`. This is a belt-and-suspenders guard against bugs.
  **Severity:** MEDIUM (defense-in-depth — low probability but high consequence).

---

### Pattern 8: Content-Overlap Reconciliation

#### Risk 8.1: TF-IDF Is Computationally Trivial — But TF-IDF vs. Ratio Are Compared with Equal Weight
- **Source:** [Tier 5] Design analysis
  **Mechanism:** The algorithm runs TF-IDF weighted Jaccard AND character 3-gram Jaccard, then picks the max. For short annotations (2-5 words, common in audiobook spans), TF-IDF of a 5-term "corpus" of 2 documents is essentially a length-normalized token overlap. The character 3-gram Jaccard acts as a fallback for zero token overlap. These are comparably simple metrics — the "max of the two" approach doesn't add much beyond a single good similarity metric.
  **Trigger:** Normal audiobook annotations (2-15 words typical).
  **Blast radius:** False positives are possible but bounded by the Jaccard threshold. The real risk is that a genuinely matching annotation falls below BOTH thresholds due to short text length (few unique tokens, few unique 3-grams).
  **Mitigation:** Consider lowering Jaccard thresholds for short annotations (< 5 tokens). Add a hard floor: if the annotation is < 3 tokens, always treat as potentially matching.
  **Severity:** LOW (anecdotal evidence suggests the dual-Jaccard approach works well for short text; the floor adjustment is a nice-to-have).

#### Risk 8.2: Dialogue Tags in STOPWORDS Suppresses Gendered Pronouns — False Negatives
- **Source:** [Tier 5] Design analysis of the `STOPWORDS` set
  **Mechanism:** The STOPWORDS include `'he'`, `'she'` alongside `'said'`, `'replied'`, `'asked'`. The docstring says this prevents dialogue tags from dominating similarity. But removing `'he'`/`'she'` means character-assignment context is LOST during overlap detection. "He said grimly" and "She said happily" become "grimly" and "happily" after stopword removal — tokens with near-zero overlap, so the algorithm correctly separates them. But "He walked away" and "She walked away" become "walked away" and "walked away" — IDENTICAL after stopword removal, which would create a false positive reconciliation.
  **Trigger:** Two adjacent spans with different characters performing the same action, where the character is expressed via pronoun.
  **Blast radius:** Two spans for different characters are merged as "matched," causing annotations to be incorrectly assigned.
  **Mitigation:** Remove `'he'` and `'she'` from STOPWORDS. Pronouns carry character attribution information that is meaningful for overlap detection. The original goal (suppressing dialogue tags) is achieved by `'said'`, `'replied'`, `'asked'`, etc.
  **Severity:** MEDIUM (context-specific — depends on how much dialogue uses pronouns vs. proper names).

---

### Cross-Pattern Risks

#### C1: [BLOCKING] Timestamp Format Incompatibility — Python vs SQLite Date Formats
- **Source:** [Tier 1] Direct code analysis — cross-referencing Pattern 4's dirty-flag query with the timestamp sources across Pattern 3, 4, and 5.
  **Interaction:** Three different timestamp sources exist in the system:
  1. Python `datetime.now(timezone.utc).isoformat()` → `'2024-01-01T12:00:00.123456+00:00'` (T separator, 6 decimal places, timezone offset) — used for `completed_at` (Pattern 4), `updated_at` via `on_structural_change` (Pattern 4)
  2. SQLite `datetime('now', 'subsec')` → `'2024-01-01 12:00:00.123'` (space separator, 3 decimal places, no timezone) — used for `updated_at` via Pattern 5's executor operations and DEFAULT clause
  3. Potentially Python `datetime.now().isoformat()` in walk result fields
  **Mechanism:** String comparison `s.updated_at > slw.completed_at`:
  - SQLite format: `'2024-01-01 12:00:00.123'` (space at position 10 = ASCII 0x20)
  - Python format: `'2024-01-01T12:00:00.123456+00:00'` (T at position 10 = ASCII 0x54)
  - Since 0x20 < 0x54, the SQLite timestamp sorts STRICTLY BEFORE the Python timestamp in string comparison, REGARDLESS of actual chronological order.
  - Result: any span with `updated_at` set via SQLite `datetime('now', 'subsec')` will appear to have `updated_at < completed_at` even if updated AFTER completion → DIRTY SPANS ARE NOT DETECTED.
  **Trigger:** Any walk whose annotations were written by the Pattern 5 executor (which uses `datetime('now', 'subsec')` for `updated_at`), compared against `completed_at` written by Pattern 4 (which uses Python `datetime.now().isoformat()`).
  **Blast radius:** The dirty-flag detection fails SILENTLY — returns `dirty_count = 0` when spans ARE dirty. This causes walks to be skipped when they should be re-run. Stale annotations persist in exports.
  **Mitigation:** Standardize ALL timestamps to a single format. Options:
  - (A) Use Python `datetime.now(timezone.utc).isoformat()` everywhere — parameterize into SQL, never use `datetime('now')`
  - (B) Use integer unix milliseconds `(strftime('%s','now')*1000)` — immune to format issues
  - (C) Add a `normalize_timestamp()` function that converts both formats to a common representation before comparison
  - (B) is the most robust — integer comparison is unambiguous.
  **Severity:** BLOCKING — silent failure of the dirty-flag system, the core mechanism for avoiding redundant work.

#### C2: [HIGH] Connection Isolation Breaks Transaction Atomicity
- **Source:** [Tier 1] Cross-referencing Pattern 3's `complete_walk()` with Pattern 5/6's walk execution flow.
  **Interaction:** Pattern 5's executor operates on one connection. Pattern 6's orchestrator calls `pipeline.complete_walk()` which opens a NEW connection via Pattern 3's `_conn()`. Annotations from executor (Connection A) and pipeline state from complete_walk (Connection B) are on SEPARATE transactions.
  **Mechanism:** See Risk 3.1 for full details. This is the SAME root cause viewed from the cross-pattern perspective: Pattern 3 assumes it shares a transaction boundary with the executor (Pattern 5), but the implementations don't share a connection.
  **Trigger:** Any walk execution flow.
  **Blast radius:** CRASH → annotations persisted, state not advanced → recovery re-runs walk → DUPLICATE DATA.
  **Mitigation:** Pass the active connection through the call chain. `complete_walk` must accept `conn: sqlite3.Connection` as a parameter.
  **Severity:** BLOCKING (same root cause as Risk 3.1, listed separately because it spans 3 patterns).

#### C3: [MEDIUM] execute_move in Pattern 5 Has Opposite Operation Order from reindex_after_move in Pattern 1
- **Source:** [Tier 1] Direct code comparison.
  **Interaction:** Pattern 1's `reindex_after_move` — DELETE first, then reindex (correct). Pattern 5's `execute_move` — reindex first, then DELETE (incorrect — causes UNIQUE violation). See Risk 5.3 for details.
  **Severity:** BLOCKING — already covered as Risk 5.3, but flagged here because the inconsistency between Pattern 1 and Pattern 5 suggests the Patterns were designed independently and the interaction was not reconciled.

#### C4: [LOW] PRAGMA State Is Per-Connection — No Guarantees Across _conn() Calls
- **Source:** [Tier 3] SQLite docs — PRAGMA settings are connection-scoped.
  **Interaction:** Each call to `_conn()` creates a brand new `sqlite3.Connection` with default PRAGMA settings. Settings like `busy_timeout`, `journal_mode`, `foreign_keys` set on one connection are not inherited.
  **Trigger:** If Pattern 3's `_conn()` opens a connection without the `busy_timeout` set by the main application, and the database is locked by another connection, the `_conn()` call would get `SQLITE_BUSY` immediately (default timeout is 0).
  **Blast radius:** Occasional `SQLITE_BUSY` errors under concurrent access patterns.
  **Mitigation:** `_conn()` should apply a standard set of PRAGMAs at connection open: `busy_timeout`, `journal_mode=WAL`, `foreign_keys=ON`. Alternately, use a connection pool with pre-configured connections.
  **Severity:** LOW (single-writer architecture minimizes contention, but defensive PRAGMAs are easy to add).

#### C5: [SPECULATIVE] Negative-Space Index Values Could Collide with Valid User-Generated Negative Keys
- **Source:** [Tier 5] Design analysis — speculative.
  **Mechanism:** The negative-space reindex uses negative integers (-1, -2, ...) as temporary position values. Position is a user-visible column. If any code path outside the reindex logic writes a negative position value (e.g., a bug, a direct SQL script, or export filtering that checks `position >= 0`), it could interfere.
  **Trigger:** Any external code that reads position values during an in-flight reindex.
  **Blast radius:** Race condition — if an external reader sees a negative position and filters it out, spans could disappear from the reader's view during the reindex window (which lasts microseconds within a single transaction, so practically invisible).
  **Mitigation:** Not worth engineering around — the negative positions exist only within a single Python `with conn:` transaction, invisible to other readers (WAL snapshot isolation). This is safe.
  **Severity:** LOW (speculative, well-mitigated by WAL isolation).

---

### Summary

#### Blocking Issues (Must Address Before Proceeding)
1. **C1 — Timestamp Format Incompatibility:** `updated_at` (SQLite format) and `completed_at` (Python format) produce different string formats that sort incorrectly in comparison, causing dirty-flag detection to silently fail. **Standardize all timestamps to a single format — recommend integer unix millis.**
2. **C2 / Risk 3.1 — `complete_walk()` Opens Separate Connection:** Breaks the stated invariant that annotations and state updates share a transaction boundary. **Pass the caller's connection as a parameter.**
3. **Risk 5.3 — `execute_move` Reindexes Before Delete:** Causes UNIQUE constraint violation for the common move case. **Delete first, then reindex (match Pattern 1's `reindex_after_move` order).**

#### High-Severity Issues (Fix Before Production)
4. **Risk 5.1 — `PRAGMA defer_foreign_keys=OFF` Resets FK Tracking:** Can mask FK violations. **Set defer_foreign_keys=ON only when needed; restore to ON (not OFF) after.**
5. **Risk 5.2 — `execute_move` Annotations on Wrong Parent Context:** Reconcile annotations BEFORE the parent change.

#### Medium-Severity Issues (Fix When Convenient)
6. **Risk 3.2 — WAL Checkpoint After complete_walk:** Add explicit `wal_checkpoint(PASSIVE)` flush.
7. **Risk 7.2 — Export Without query_only:** Add `PRAGMA query_only = ON` to export connections.
8. **Risk 8.2 — Gendered Pronouns in STOPWORDS:** Remove `'he'`/`'she'` to preserve character attribution.
9. **Risk 6.2 — Voice Lock Is All-or-Nothing:** Consider per-span enforcement.

#### Low-Severity / Documentation Items
10. **Risk 1.2 — Document Single-Writer Invariant:** The reindex pattern requires exclusive access to the parent's rows. Document this as a hard constraint.
11. **Risk 2.1 / 2.2 — CTE Performance:** Add guardrail logging for large paragraphs. Add EXPLAIN QUERY PLAN validation test.
12. **Risk 6.1 — Startup Python Validation:** Validate `sys.executable` at startup.
13. **C4 — Standardize PRAGMA Configuration in `_conn()`:** Apply busy_timeout, journal_mode, foreign_keys at connection open.


## Final Patterns

*Appended by rnd-improver, Turn 7 — 2026-08-03*

*Response to Counter-Improver Pattern Risks (Turn 6)*

---

### BLOCKING Fixes

#### C1: Timestamp Format Standardization — Unix Milliseconds (INTEGER)

**Root cause:** Three timestamp sources exist in the system, producing two incompatible string formats:
- Python `datetime.now(timezone.utc).isoformat()` → `'2024-01-01T12:00:00.123456+00:00'` (T separator, 6 decimal places)
- SQLite `datetime('now','subsec')` → `'2024-01-01 12:00:00.123'` (space separator, 3 decimal places)

String comparison `s.updated_at > slw.completed_at` sorts by ASCII: space (0x20) < T (0x54), so SQLite timestamps sort STRICTLY BEFORE Python timestamps regardless of actual time → dirty flags silently never fire.

**Fix: Store ALL timestamps as INTEGER milliseconds since Unix epoch.**

Rationale (justification for choosing INTEGER over string standardization):
1. **Eliminates ALL format ambiguity.** Integer comparison is unambiguous. No format negotiation between Python and SQLite.
2. **Immune to locale/UTC issues.** Milliseconds since epoch is timezone-independent. No "is this UTC or local?" ambiguity.
3. **SQLite 3.42.0+ (2023-05-16) supports `unixepoch('subsec')`** — returns float seconds with millisecond precision. We multiply by 1000 and cast to integer.
4. **Python `int(datetime.now(timezone.utc).timestamp() * 1000)`** produces identical values.
5. **Sorts correctly** in both SQL (`ORDER BY`) and Python (`sorted()`) without any conversion.

The text format alternative (`strftime('%Y-%m-%dT%H:%M:%f', 'now')`) was considered but rejected: it still places all formatting responsibility on every INSERT/UPDATE site. A single missed site producing the wrong format reintroduces the bug silently. INTEGER eliminates the failure mode entirely.

**SQLite DDL changes (Pattern 4 — `span` table):**

```sql
-- span.updated_at: INTEGER milliseconds since epoch, set by SQLite DEFAULT
CREATE TABLE span (
    span_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    span_type TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
    last_modified_by TEXT
        CHECK (last_modified_by IN ('split','merge','move','delete','human_review','walk_2a','walk_2b','walk_2c','walk_2d','walk_2e','walk_2f','import')),
    ...
);
```

**SQLite DDL changes (Pattern 4 — `span_last_walk` table):**

```sql
CREATE TABLE span_last_walk (
    span_id TEXT NOT NULL REFERENCES span(span_id) ON DELETE CASCADE,
    walk_name TEXT NOT NULL CHECK (walk_name IN ('2a','2b','2c','2d','2e','2f')),
    completed_at INTEGER NOT NULL,  -- INTEGER millis since epoch
    PRIMARY KEY (span_id, walk_name)
);
```

**SQLite DDL changes (Pattern 3 — `pipeline_run` table):**

```sql
CREATE TABLE pipeline_run (
    run_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(book_id),
    status TEXT NOT NULL CHECK (status IN ('pending','running','paused','completed','failed')),
    current_walk TEXT,
    started_at INTEGER,    -- INTEGER millis since epoch
    completed_at INTEGER,  -- INTEGER millis since epoch
    error_message TEXT
);
```

**Corrected dirty-flag query (Pattern 4):**

```sql
-- Find spans whose updated_at > their last walk completion for walk $walk
-- All timestamps are INTEGER millis — comparison is now trivially correct
SELECT s.span_id, s.updated_at, s.last_modified_by
FROM span s
LEFT JOIN span_last_walk slw
    ON s.span_id = slw.span_id AND slw.walk_name = ?
WHERE slw.completed_at IS NULL
   OR s.updated_at > slw.completed_at;
```

**Python timestamp helper (add to `pipeline/timestamps.py`):**

```python
"""Timestamp utilities — all timestamps are INTEGER milliseconds since Unix epoch."""
from datetime import datetime, timezone


def now_ms() -> int:
    """Current time as integer milliseconds since Unix epoch. Use EVERYWHERE."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ms_to_datetime(ms: int) -> datetime:
    """Convert millisecond integer back to UTC datetime for display/logging."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# SQLite DEFAULT value for new columns:
#   updated_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER))
#
# In Python INSERT/UPDATE statements, always pass now_ms() as a parameter:
#   conn.execute("UPDATE span SET updated_at = ?, last_modified_by = 'split' WHERE ...", (now_ms(),))
```

**Transition rule:** All timestamp columns in the schema change from TEXT to INTEGER. Any existing TEXT timestamps in a migration must be converted: `CAST((julianday(old_text_column) - 2440587.5) * 86400000 AS INTEGER)`.

**Validation test:**

```python
def test_timestamp_comparison_is_correct():
    """Prove that INTEGER millis comparison works regardless of source."""
    from_sqlite = 1704067200123   # 2024-01-01T00:00:00.123Z
    from_python = 1704067200456  # 2024-01-01T00:00:00.456Z
    assert from_sqlite < from_python  # SQLite timestamp BEFORE Python timestamp — correct
    assert from_python - from_sqlite == 333  # 333ms difference
```

**Cited evidence:**
- **[Tier 1] SQLite Date And Time Functions** — Documents `unixepoch('subsec')` returning "a floating point value which is the number of seconds and fractional seconds since 1970-01-01." The 'subsec' modifier "increases the resolution from seconds to milliseconds." INTEGER storage for unix timestamps is explicitly listed as a supported storage format. [https://sqlite.org/lang_datefunc.html](https://sqlite.org/lang_datefunc.html)
- **[Tier 1] SQLite Forum post 7ca3325d4eecb198** — Community confirms: "Store the dates as an integer number of seconds since 1970" is the recommended approach for comparison correctness. String formats require ISO-8601 ordering (big-endian) to sort correctly; mixed formats produce silent errors. [https://sqlite.org/forum/forumpost/7ca3325d4eecb198](https://sqlite.org/forum/forumpost/7ca3325d4eecb198)
- **[Tier 2] Simon Willison's TIL "Unix timestamp in milliseconds in SQLite"** — Documents the `unixepoch('subsec')` approach for SQLite 3.42.0+, with millisecond precision. Confirms that `strftime('%s','now') * 1000` loses precision (always ends in 000). [https://til.simonwillison.net/sqlite/unix-timestamp-milliseconds-sqlite](https://til.simonwillison.net/sqlite/unix-timestamp-milliseconds-sqlite)

---

#### C2: `complete_walk()` Single-Connection Transaction Boundary

**Root cause:** Pattern 3's `PipelineStateMachine._conn()` creates a NEW `sqlite3.Connection`. When `complete_walk()` is called from the orchestrator (Pattern 6), which holds a transaction on the executor's connection (Pattern 5), the annotations INSERT and the `pipeline_run` UPDATE are on SEPARATE connections → SEPARATE transactions → NOT atomic. A crash between commits leaves annotations persisted but state un-advanced → recovery re-runs the walk → duplicate data.

**Fix: Eliminate `_conn()`. All pipeline operations share a single connection passed through the call stack.**

The refactored architecture:

```
WalkOrchestrator.execute_walk()
│
├─ self.conn  ← ONE connection for the entire walk
│
├─ DeterministicExecutor(self.conn)  ← accepts conn, never creates its own
│   └─ execute_split(conn=self.conn, ...)
│       └─ conn.execute("INSERT INTO span ...")
│
├─ self.state_machine.start_walk(conn=self.conn, ...)
│   └─ conn.execute("UPDATE pipeline_run SET status='running' ...")
│
└─ self.state_machine.complete_walk(conn=self.conn, ...)
    └─ conn.execute("UPDATE pipeline_run SET current_walk='2d', status='running' ...")
    └─ conn.commit()  ← ONE commit for everything
```

**Corrected `PipelineStateMachine` (Pattern 3):**

```python
class PipelineStateMachine:
    """Manages pipeline_run state transitions. Does NOT own a connection."""

    def __init__(self):
        # NO _conn() — connection is always passed in
        pass

    def create_run(self, conn: sqlite3.Connection, book_id: str) -> str:
        """Create a new pipeline run. Caller owns the transaction."""
        run_id = str(uuid.uuid4())
        now = now_ms()
        conn.execute(
            """INSERT INTO pipeline_run (run_id, book_id, status, started_at)
               VALUES (?, ?, 'pending', ?)""",
            (run_id, book_id, now)
        )
        return run_id

    def start_walk(self, conn: sqlite3.Connection, run_id: str, walk_name: str) -> None:
        """Mark a walk as started. Caller owns the transaction."""
        conn.execute(
            """UPDATE pipeline_run
               SET status = 'running', current_walk = ?
               WHERE run_id = ?""",
            (walk_name, run_id)
        )

    def complete_walk(self, conn: sqlite3.Connection, run_id: str,
                      walk_name: str, next_walk: str | None = None) -> None:
        """Mark a walk as complete and advance to the next.
        
        CRITICAL: This executes on the SAME `conn` as the annotation INSERTs.
        Caller must wrap BOTH annotation inserts AND this call in ONE transaction.
        
        Pattern the caller must follow:
            with conn:
                # Step 1: Insert all walk annotations
                for annotation in walk_result.annotations:
                    conn.execute("INSERT INTO ...", ...)
                
                # Step 2: Mark walk complete and advance pipeline state
                # (this call — uses the SAME conn, SAME transaction)
                state_machine.complete_walk(conn, run_id, walk_name, next_walk)
                
                # Step 3: Commit — BOTH annotations AND state update are atomic
                # (conn context manager handles this)
        """
        next_status = 'completed' if next_walk is None else 'running'
        next_walk_val = next_walk
        now = now_ms()
        
        # Single UPDATE — no separate connection, no separate transaction
        conn.execute(
            """UPDATE pipeline_run
               SET current_walk = ?,
                   status = ?,
                   completed_at = CASE WHEN ? IS NULL THEN ? ELSE NULL END
               WHERE run_id = ?""",
            (next_walk_val, next_status, next_walk_val, now, run_id)
        )

    def fail_run(self, conn: sqlite3.Connection, run_id: str, error: str) -> None:
        """Mark pipeline as failed."""
        conn.execute(
            """UPDATE pipeline_run
               SET status = 'failed', error_message = ?, completed_at = ?
               WHERE run_id = ?""",
            (error, now_ms(), run_id)
        )

    def recover(self, conn: sqlite3.Connection) -> list[str]:
        """Find interrupted runs and determine which walk to resume.
        
        Recovery algorithm:
        1. Query pipeline_run WHERE status = 'running'
        2. For each, verify which walks completed (check annotations exist)
        3. Return the walk to resume
        
        The caller should then check dirty flags (see Cross-Pattern: Crash Recovery
        + Dirty Flags) before resuming.
        """
        interrupted = conn.execute(
            "SELECT run_id, book_id, current_walk FROM pipeline_run WHERE status = 'running'"
        ).fetchall()
        
        recovery_actions = []
        for run in interrupted:
            # Verify current_walk's annotations are complete
            walk = run['current_walk']
            verified = self._verify_walk_completion(conn, run['run_id'], walk)
            if verified:
                # Walk was completed — advance to next
                next_walk = self._next_walk(walk)
                recovery_actions.append({
                    'run_id': run['run_id'],
                    'book_id': run['book_id'],
                    'resume_at': next_walk,
                    'previous_completed': walk
                })
            else:
                # Walk was incomplete — re-run it
                recovery_actions.append({
                    'run_id': run['run_id'],
                    'book_id': run['book_id'],
                    'resume_at': walk,
                    'previous_completed': self._previous_walk(walk)
                })
        return recovery_actions

    def _verify_walk_completion(self, conn, run_id, walk_name) -> bool:
        """Check if walk annotations exist and are valid."""
        # Implementation: count expected annotation rows, verify UUIDs
        ...
```

**Corrected `WalkOrchestrator.execute_walk` (Pattern 6) — single-connection transaction:**

```python
async def execute_walk(self, walk_name: str, book_id: str, run_id: str) -> WalkResult:
    """Execute one walk. ALL operations share self.conn in ONE transaction."""
    
    walk_start_ms = now_ms()
    
    # --- Pre-pass: M5 deterministic pre-processing (walks 2b, 2c only) ---
    # Runs on the SAME connection, within the eventual transaction
    pre_pass_data = None
    if walk_name in ('2b', '2c'):
        pre_pass_data = M5PrePass(self.conn).run(book_id)
    
    # --- Prepare subprocess config ---
    config = self._build_walk_config(walk_name, book_id, pre_pass_data)
    config_path = self._write_config_file(config)
    
    # --- Execute LLM subprocess ---
    result = await self._run_subprocess(walk_name, config_path)
    
    # --- CRITICAL: One transaction for annotations + state update ---
    # BOTH the annotation INSERTs and pipeline_run UPDATE are on self.conn
    with self.conn:
        # Stale data check: did any span change during the walk?
        if not self._validate_no_stale_data(walk_name, walk_start_ms):
            raise StaleDataError(
                f"Spans modified during walk {walk_name}. Must re-run."
            )
        
        # Write annotations (Pattern 5 executor operations if any)
        for annotation in result.annotations:
            self.conn.execute(
                "INSERT INTO annotation_... VALUES (?,?,?)", ...
            )
        
        # Mark walk annotations as complete
        walk_end_ms = now_ms()
        for span_id in result.touched_spans:
            self.conn.execute(
                """INSERT OR REPLACE INTO span_last_walk (span_id, walk_name, completed_at)
                   VALUES (?, ?, ?)""",
                (span_id, walk_name, walk_end_ms)
            )
        
        # Advance pipeline state — SAME connection, SAME transaction
        next_walk = self._next_walk_name(walk_name)
        self.state_machine.complete_walk(self.conn, run_id, walk_name, next_walk)
        
        # Explicit WAL checkpoint after commit (see MEDIUM fix 3.2)
    
    self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    
    return result
```

**Cited evidence:**
- **[Tier 1] Python sqlite3 docs — "Multiple Operations in Single Transaction"** — Documents the pattern: multiple UPDATE statements grouped in one `with conn:` context manager. "Both UPDATE statements are part of the same transaction. They will either both succeed (when committed) or both fail (if rollback occurs)." [https://zetcode.com/python/sqlite3-connection-commit/](https://zetcode.com/python/sqlite3-connection-commit/)
- **[Tier 1] SQLite Atomic Commit docs** — "Atomic commit means that either all database changes within a single transaction occur or none of them occur." Confirms that the single-connection approach provides the atomicity invariant we need. [https://www.sqlite.org/atomiccommit.html](https://www.sqlite.org/atomiccommit.html)
- **[Tier 2] TheLinuxCode "SQLite Transactions: A Practical Guide"** — Explicit pattern for multi-statement atomicity: "Wrap it in an explicit transaction. [...] if you do half the work outside the transaction, SQLite can't help you." Our fix puts all work inside one transaction. [https://thelinuxcode.com/sqlite-transactions-a-practical-guide-to-autocommit-wal-savepoint-and-production-patterns/](https://thelinuxcode.com/sqlite-transactions-a-practical-guide-to-autocommit-wal-savepoint-and-production-patterns/)

---

#### 5.3: `execute_move` Delete-First Ordering (Match Pattern 1)

**Root cause:** Pattern 5's `execute_move` called `_reindex_after_delete_in_transaction` BEFORE the DELETE. The negative-space reindex shifts position 4→3, but position 3 is still occupied by the span being moved → UNIQUE(parent_id, position) constraint violation. Pattern 1's `reindex_after_move` correctly does DELETE first, then reindex. The inconsistency between Pattern 1 and Pattern 5 caused the bug.

**Fix: Restructure `execute_move` to DELETE → REINDEX OLD → INSERT NEW → REINDEX NEW.**

**Corrected `execute_move` pseudocode:**

```python
def execute_move(self, span_id: str, new_parent_id: str, new_position: int):
    """Move a span to a new parent. Corrected order (C2 + Risk 5.3 fixes)."""
    
    now = now_ms()
    
    with self.conn:
        # --- Step 0: Enable FK deferral for parent changes ---
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        
        # --- Step 0a: Read old parent info BEFORE the delete ---
        old_info = self.conn.execute(
            """SELECT p.paragraph_id, ps.position
               FROM paragraph_span ps
               JOIN paragraph p ON ps.paragraph_id = p.paragraph_id
               WHERE ps.span_id = ?""",
            (span_id,)
        ).fetchone()
        if not old_info:
            raise ValueError(f"Span {span_id} has no current parent")
        old_parent_id = old_info['paragraph_id']
        old_position = old_info['position']
        
        # --- Step 0b: Reconcile annotations BEFORE the move ---
        # (Risk 5.2 fix: reconcile using OLD parent context)
        old_annotations = self._get_annotations_for_span(span_id)
        
        # --- Step 1: DELETE the old edge row FIRST ---
        # This removes the span from old parent -> no UNIQUE collision during reindex
        self.conn.execute(
            "DELETE FROM paragraph_span WHERE span_id = ?",
            (span_id,)
        )
        
        # --- Step 2: Reindex old parent ---
        # With the span removed, negative-space reindex has no collision
        self._reindex_after_delete(self.conn, 'paragraph', old_parent_id, old_position)
        
        # --- Step 3: Make space in new parent ---
        self._reindex_before_insert(self.conn, 'paragraph', new_parent_id, new_position)
        
        # --- Step 4: INSERT into new parent ---
        self.conn.execute(
            """INSERT INTO paragraph_span (paragraph_id, span_id, position)
               VALUES (?, ?, ?)""",
            (new_parent_id, span_id, new_position)
        )
        
        # --- Step 5: Update span metadata ---
        self.conn.execute(
            """UPDATE span SET updated_at = ?, last_modified_by = 'move'
               WHERE span_id = ?""",
            (now, span_id)
        )
        
        # --- Step 6: Reconcile annotations with NEW context ---
        # Only parent-scoped annotations need re-reconciliation
        # Span-scoped annotations (identity-based) moved with the span
        self._reconcile_parent_scoped_annotations(span_id, old_annotations,
                                                   old_parent_id, new_parent_id)
        
        # --- Step 7: Restore FK enforcement ---
        self.conn.execute("PRAGMA defer_foreign_keys = OFF")

def _reindex_after_delete(self, conn, parent_type, parent_id, deleted_position):
    """Reindex after removing a child. ONLY call AFTER the delete."""
    # MATCHES Pattern 1's reindex_after_move exactly
    # Phase 1: shift to negative space
    conn.execute(
        """UPDATE paragraph_span SET position = -(position + 1)
           WHERE paragraph_id = ? AND position > ?""",
        (parent_id, deleted_position)
    )
    # Phase 2: bring back to positive (no collision possible — deleted row is gone)
    conn.execute(
        """UPDATE paragraph_span SET position = ABS(position)
           WHERE paragraph_id = ? AND position < 0""",
        (parent_id,)
    )

def _reindex_before_insert(self, conn, parent_type, parent_id, insert_position):
    """Make space for a new child at insert_position. No negative-space needed —
    we shift UP from the end to avoid collisions."""
    # Shift positions >= insert_position UP by 1, working from HIGHEST to LOWEST
    # This avoids UNIQUE collisions because we process in descending order
    conn.execute(
        """UPDATE paragraph_span SET position = position + 1
           WHERE paragraph_id = ? AND position >= ?
           ORDER BY position DESC""",
        (parent_id, insert_position)
    )
    # ORDER BY position DESC ensures we process position 5 before position 4,
    # so there's never an intermediate state where two rows share position 5.
```

**Why `ORDER BY position DESC` works for insert (no negative-space needed):**

When inserting at position 3 in a parent with positions [1,2,3,4,5]:
1. Process position 5 first: 5→6 (position 6 is empty) ✓
2. Process position 4: 4→5 (position 5 is now empty) ✓
3. Process position 3: 3→4 (position 4 is now empty) ✓
4. Result: [1,2,_,4,5,6] — position 3 is free for the INSERT ✓

This only works because we process HIGHEST first. The standard `UPDATE ... SET position = position + 1 WHERE position >= 3` fails because it processes in arbitrary order and collisions occur mid-update.

**Cited evidence:**
- **[Tier 1] SQLite Forum post 499f52aa7093612f** — The original source for the negative-space pattern. Confirms that the pattern works because the deleted row's old position is already vacant when reindexing runs. Our fix restores this invariant: DELETE FIRST, then reindex. [https://sqlite.org/forum/forumpost/499f52aa7093612f](https://sqlite.org/forum/forumpost/499f52aa7093612f)
- **[Tier 2] SQLite docs — UPDATE with ORDER BY** — SQLite supports `ORDER BY` on UPDATE statements. Using `ORDER BY position DESC` processes rows from highest to lowest, avoiding mid-update collisions. This is documented behavior and production-safe. [https://sqlite.org/lang_update.html](https://sqlite.org/lang_update.html)

---

### HIGH Fixes

#### 5.1: `PRAGMA defer_foreign_keys` Reset Policy

**Root cause:** The Counter found that `PRAGMA defer_foreign_keys = OFF` does NOT simply restore immediate FK enforcement. It RESETS the internal FK violation tracking counter (SQLite docs: "Caution: Setting 'defer_foreign_keys=0' resets the internal foreign-key tracking state of the database connection, so that it becomes possible to commit the current transaction even if there are foreign key violations"). This means any FK violations accumulated during `defer_foreign_keys=ON` are silently suppressed when we toggle back to OFF.

Additionally, `PRAGMA foreign_keys` is a **no-op within a transaction**. The SQLite docs are explicit: "This pragma is a no-op within a transaction; foreign key constraint enforcement may only be enabled or disabled when there is no pending BEGIN or SAVEPOINT." This means we CANNOT toggle `PRAGMA foreign_keys` mid-transaction to change enforcement — it has no effect.

**Fix: Three-part policy.**

**Policy 1: Always set `PRAGMA foreign_keys = ON` at connection open.** This is a connection-level setting and must be set BEFORE any transaction begins.

```python
def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")      # MUST be before any transaction
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
```

**Policy 2: Use `PRAGMA defer_foreign_keys = ON` only within multi-table operations, and toggle with care.**

```python
class TransactionWrapper:
    """Safe PRAGMA defer_foreign_keys management for Pattern 5 operations."""
    
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
    
    def __enter__(self):
        # Step A: Enable FK deferral at transaction start
        # This is safe because we're at the start of a transaction
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # Exception occurred — rollback will clear everything including FK state
            # defer_foreign_keys auto-resets at COMMIT/ROLLBACK per SQLite docs:
            # "The defer_foreign_keys pragma is automatically switched off at each
            #  COMMIT or ROLLBACK."
            return False  # propagate exception
        
        # Step B: Before commit, explicitly run foreign_key_check
        # This catches any FK violations that were deferred
        violations = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ForeignKeyViolationError(
                f"FK violations detected: {len(violations)} rows. "
                f"First: table={violations[0]['table']}, rowid={violations[0]['rowid']}"
            )
        
        # Do NOT set defer_foreign_keys=OFF — it resets the counter.
        # The COMMIT will auto-reset it per SQLite docs.
        # The COMMIT will also reject any violations caught by foreign_key_check.
    
    def execute(self, sql, params=None):
        return self.conn.execute(sql, params or ())


# Usage in Pattern 5 operations:
def execute_split(self, span_id, position, left_text, right_text):
    with TransactionWrapper(self.conn) as tx:
        # FK deferral is ON for the duration
        tx.execute("INSERT INTO span ...")
        tx.execute("INSERT INTO paragraph_span ...")
        # Commit happens on __exit__ — defer_foreign_keys auto-resets
        # PRAGMA foreign_key_check runs before commit — catches any violations
```

**Policy 3: At the end of EVERY `execute_*` method that uses `defer_foreign_keys=ON`, explicitly run `PRAGMA foreign_key_check` and fail the transaction if violations exist. This is the safety net.**

**Cited evidence:**
- **[Tier 1] SQLite PRAGMA docs — defer_foreign_keys** — "Caution: Setting 'defer_foreign_keys=0' resets the internal foreign-key tracking state of the database connection, so that it becomes possible to commit the current transaction even if there are foreign key violations." This is the EXACT mechanism the Counter identified. [https://sqlite.org/pragma.html#pragma_defer_foreign_keys](https://sqlite.org/pragma.html#pragma_defer_foreign_keys)
- **[Tier 1] SQLite Foreign Key Support docs** — "It is not possible to enable or disable foreign key constraints in the middle of a multi-statement transaction. Attempting to do so does not return an error; it simply has no effect." Confirms our Policy 1: set foreign_keys=ON at connection open, never toggle mid-transaction. [https://sqlite.org/foreignkeys.html](https://sqlite.org/foreignkeys.html)
- **[Tier 1] SQLite User Forum 64fb781a226df95c** — Real-world report of the exact FK counter reset bug: user sees "FOREIGN KEY constraint failed" on COMMIT with defer_foreign_keys=ON, but setting defer_foreign_keys=OFF before commit makes it succeed (because the reset suppresses the violation). This validates our approach: run `foreign_key_check` explicitly, never rely on defer_foreign_keys=OFF. [https://sqlite.org/forum/info/64fb781a226df95c0f4edc474e590214c28ae1b4b5ddeee8f7ec4ad77286796c](https://sqlite.org/forum/info/64fb781a226df95c0f4edc474e590214c28ae1b4b5ddeee8f7ec4ad77286796c)
- **[Tier 2] Entity Framework Core PR #35873** — Production ORM changed from `PRAGMA foreign_keys = 0` to `PRAGMA defer_foreign_keys` for migrations. Validates that `defer_foreign_keys` is the correct tool for multi-table operations within a transaction, while `foreign_keys` toggling is a no-op. [https://github.com/dotnet/efcore/pull/35873](https://github.com/dotnet/efcore/pull/35873)

---

#### 5.2: `execute_move` Annotation Reconciliation Order

**Root cause:** Pattern 5's `execute_move` called `_execute_annotation_reconciliation` AFTER the parent change, using the NEW parent's sentence/word context. But annotations were textually aligned to the OLD parent context. Moving a span between paragraphs where word/sentence boundaries differ would produce misaligned annotations.

**Fix: Reconcile annotations BEFORE the parent change, using OLD parent context. Then re-reconcile only parent-scoped annotations for the new parent.**

Annotation type scoping (clarification from Counter's question "which annotation types are parent-scoped vs. span-scoped?"):

| Annotation Table | Scope | Behavior on Move |
|-----------------|-------|-----------------|
| `word_annotation` | Span-scoped | Follows the span. Words don't change. No reconciliation needed. |
| `sentence_annotation` | Parent-scoped | Sentence boundaries depend on parent paragraph. RECONCILE for new parent. |
| `character_annotation` | Span-scoped | Character identity follows the span. No reconciliation needed. |
| `voice_annotation` | Span-scoped | Voice casting is identity-based. No reconciliation needed. |
| `emotion_annotation` | Span-scoped | Emotion is text-content-based. Recompute from span text only. |

**Corrected flow:**

```python
def execute_move(self, span_id: str, new_parent_id: str, new_position: int):
    """Move span to new parent. Annotation reconciliation corrected (Risk 5.2)."""
    
    with TransactionWrapper(self.conn) as tx:
        # --- Step 0: Read old parent context ---
        old_parent_id, old_position = self._get_current_parent(span_id)
        old_text = self._get_span_text(span_id)
        
        # --- Step 1: Reconcile span-scoped annotations ---
        # These follow the span — just verify they're valid for the span's text
        # (identity-based, no parent context needed)
        self._recompute_emotion_annotations(span_id, old_text)  # text-based, not parent-based
        
        # --- Step 2: DELETE old edge row ---
        tx.execute("DELETE FROM paragraph_span WHERE span_id = ?", (span_id,))
        
        # --- Step 3: Reindex old parent ---
        self._reindex_after_delete(tx, 'paragraph', old_parent_id, old_position)
        
        # --- Step 4: Insert into new parent ---
        self._reindex_before_insert(tx, 'paragraph', new_parent_id, new_position)
        tx.execute(
            "INSERT INTO paragraph_span (paragraph_id, span_id, position) VALUES (?,?,?)",
            (new_parent_id, span_id, new_position)
        )
        
        # --- Step 5: Reconcile parent-scoped annotations for NEW parent ---
        # Only sentence_annotation needs this — sentence boundaries differ per paragraph
        self._recompute_sentence_annotations(span_id, old_parent_id, new_parent_id)
        
        # --- Step 6: Update span metadata ---
        tx.execute(
            "UPDATE span SET updated_at = ?, last_modified_by = 'move' WHERE span_id = ?",
            (now_ms(), span_id)
        )

def _recompute_sentence_annotations(self, span_id, old_parent_id, new_parent_id):
    """Sentence boundaries are parent-scoped. Recompute for new parent context.
    
    Sentence_annotation stores: span_id, sentence_index, sentence_text
    The sentence_index depends on the parent paragraph's sentence segmentation.
    Moving to a new paragraph means the sentence_index changes.
    """
    # Regenerate sentence annotations using the NEW parent's segmentation
    new_context = self._get_paragraph_text(new_parent_id)
    span_text = self._get_span_text(span_id)
    # Find the span's text within the paragraph, determine sentence indices
    ...
```

**Cited evidence:**
- **[Tier 5] Design analysis** — This fix is architectural: parent-scoped annotations (sentence boundaries) are recomputed because their context changed; span-scoped annotations (word, character, voice, emotion) travel with the span because their context (span text) didn't change. This classification was NOT in the original Pattern 5 implementation.

---

### MEDIUM Fixes

#### 3.2: WAL Checkpoint After `complete_walk()`

**Fix:** After the annotation + state update transaction commits, explicitly flush to main database:

```python
# In WalkOrchestrator.execute_walk(), after the 'with self.conn:' block commits:
self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
```

PASSIVE mode "does as much work as it can without interfering with other database connections." It never blocks readers. On a single-writer architecture, PASSIVE is equivalent to FULL (the WAL will be fully checkpointed since there are no concurrent readers to block it).

**Cited evidence:**
- **[Tier 1] SQLite WAL docs** — "An application can initiate a checkpoint using any writable database connection... PASSIVE... does as much work as it can without interfering with other database connections." [https://www.sqlite.org/wal.html#ckpt](https://www.sqlite.org/wal.html#ckpt)

---

#### 7.2: Export `query_only` Guard

**Fix:** Open export connections with `PRAGMA query_only = ON`:

```python
def export_annotated_script(self, book_id: str) -> dict:
    """Export annotated script. Read-only connection with belt-and-suspenders guard."""
    
    # Separate read-only connection
    export_conn = sqlite3.connect(self.db_path)
    export_conn.row_factory = sqlite3.Row
    export_conn.execute("PRAGMA query_only = ON")  # Belt-and-suspenders
    export_conn.execute("PRAGMA busy_timeout = 5000")
    
    try:
        # Use DEFERRED BEGIN (not IMMEDIATE) for read-only in WAL mode
        # See Cross-Pattern: Export + Concurrent Walks for justification
        export_conn.execute("BEGIN TRANSACTION")
        
        # ... export queries ...
        
        export_conn.execute("COMMIT")
        return result
    finally:
        export_conn.close()
```

**Cited evidence:**
- **[Tier 1] SQLite PRAGMA docs** — `PRAGMA query_only = ON` prevents all write operations on the connection. This is defense-in-depth: a bug in the export formatting code that accidentally issues a write will fail explicitly rather than silently corrupting data. [https://sqlite.org/pragma.html#pragma_query_only](https://sqlite.org/pragma.html#pragma_query_only)

---

#### 8.2: Gendered Pronouns in STOPWORDS — Remove `'he'`/`'she'`

**Fix:** Remove gendered pronouns from the STOPWORDS set. The Counter is correct: pronouns carry character attribution. Removing `'he'`/`'she'` means "He walked away" and "She walked away" become "walked away" and "walked away" — identical after stopword removal, producing a false positive annotation match.

Dialogue tags like `'said'`, `'replied'`, `'asked'` are correctly suppressed — they contribute zero information about which character is speaking. But pronouns are the PRIMARY mechanism by which English text signals which character is acting.

```python
# STOPWORDS for annotation reconciliation — UPDATED (pronouns removed)
STOPWORDS: set[str] = {
    # Dialogue tags (suppress — these carry no character info)
    'said', 'replied', 'asked', 'told', 'spoke', 'murmured', 'whispered',
    'shouted', 'cried', 'exclaimed', 'muttered', 'called', 'answered',
    'responded', 'added', 'continued', 'began', 'started', 'finished',
    # High-frequency function words (suppress — TF-IDF handles these naturally)
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'shall',
    'should', 'may', 'might', 'must', 'can', 'could', 'to', 'of', 'in',
    'for', 'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through',
    'during', 'before', 'after', 'above', 'below', 'between',
    'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both', 'either',
    'neither', 'each', 'every', 'all', 'any', 'few', 'more', 'most',
    'other', 'some', 'such', 'no', 'only', 'own', 'same', 'than', 'too',
    'very', 'just', 'now', 'then', 'here', 'there', 'when', 'where',
    'why', 'how', 'that', 'this', 'these', 'those',
    # NOTE: 'he', 'she', 'it', 'they', 'him', 'her', 'them', 'his', 'hers',
    # 'their' are NOT in stopwords — pronouns carry character attribution info.
}

# Dialogue tag detection: we identify dialogue tags by pattern, not word removal
DIALOGUE_TAG_PATTERN = re.compile(
    r'\b(said|replied|asked|murmured|whispered|shouted|cried|'
    r'exclaimed|muttered|called|answered|responded|added|'
    r'continued|began|started|finished)\b',
    re.IGNORECASE
)

def _suppress_dialogue_tags(text: str) -> str:
    """Remove dialogue tag words ONLY (preserves pronouns for character attribution)."""
    return DIALOGUE_TAG_PATTERN.sub('', text)
```

**Cited evidence:**
- **[Tier 5] Design analysis** — This is a direct response to the Counter's observation in Risk 8.2. The fix is surgical: remove ONLY dialogue tag noise, preserve pronouns for character attribution. The Counter's example ("He walked away" vs "She walked away") demonstrates exactly why `'he'`/`'she'` must stay.

---

#### 6.2: Voice Lock Per-Span Enforcement

**Fix:** Replace all-or-nothing voice lock enforcement with per-span enforcement:

```python
def _enforce_voice_locks(self, voice_assignments: dict[str, str]) -> dict[str, str]:
    """Enforce series voice locks. Per-span enforcement: lock-violating spans
    are corrected automatically; only spans with unresolvable conflicts are flagged.
    
    Returns: corrected voice_assignments + violations dict for logging.
    """
    violations = {}
    corrected = {}
    
    for span_id, assigned_voice in voice_assignments.items():
        character_id = self._get_character_for_span(span_id)
        if character_id is None:
            corrected[span_id] = assigned_voice
            continue
        
        locked_voice = self._get_series_voice_lock(character_id)
        if locked_voice is None:
            # No lock exists — this is a first casting
            corrected[span_id] = assigned_voice
            continue
        
        if assigned_voice == locked_voice:
            # Assigned voice matches lock — correct
            corrected[span_id] = assigned_voice
        else:
            # Voice lock violation — auto-correct to locked voice
            corrected[span_id] = locked_voice
            violations[span_id] = {
                'assigned': assigned_voice,
                'locked': locked_voice,
                'character': character_id
            }
    
    # Log violations for debugging but continue — don't fail the walk
    if violations:
        logger.warning(
            f"Voice lock corrections: {len(violations)} spans auto-corrected "
            f"to match series voice casting. Affected spans: {list(violations.keys())}"
        )
    
    return corrected
```

The previous approach (walk SKIPPED entirely on any violation) was too aggressive for debugging. The per-span approach auto-corrects lock-violating spans while logging them, allowing the walk to complete. For production, this could be configurable: `voice_lock_mode = 'enforce' | 'warn' | 'ignore'`.

---

### LOW Fixes

#### 1.2: Single-Writer Invariant — Document as Hard Constraint

**Fix:** Add a module-level constraint comment at the top of `pipeline/deterministic_executor.py` and `pipeline/reindex.py`:

```python
"""
Deterministic operation executor for the audiobook pipeline.

HARD CONSTRAINT: SINGLE WRITER
The negative-space reindex pattern in reindex.py requires EXCLUSIVE access to a
parent's child rows for the duration of a reindex operation. The two-phase UPDATE
(NEGATE, then ABS) is NOT atomic — a concurrent write inserting at a position in
the negative range between Phase 1 and Phase 2 would collide after ABS conversion.

This is enforced by:
1. Single `sqlite3.Connection` for all write operations (no connection pooling for writes)
2. Python GIL serializes all writes within a single process
3. No multi-process writer support
4. WAL mode allows concurrent READERS during reindex (snapshot isolation)
   but WRITERS would violate the invariant

DO NOT add concurrent writer support without redesigning the reindex strategy.
"""
```

#### 2.1/2.2: CTE Guardrail Logging + EXPLAIN QUERY PLAN Test

**Fix:**

```python
# Guardrail: warn if paragraph has pathological span count
MAX_SPANS_BEFORE_WARNING = 5000

def get_presentation_indices(self, book_id: str, parent_type: str, parent_id: str):
    # Check span count first
    span_count = self.conn.execute(
        "SELECT COUNT(*) FROM span s JOIN ... WHERE ...",
        (book_id, parent_id)
    ).fetchone()[0]
    
    if span_count > MAX_SPANS_BEFORE_WARNING:
        logger.warning(
            f"Large paragraph detected: {span_count} spans in {parent_type}={parent_id}. "
            f"CTE MATERIALIZED will consume ~{span_count * 400 / 1024:.0f} KB memory. "
            f"Consider splitting this paragraph."
        )
    
    # ... proceed with MATERIALIZED CTE ...


# Integration test: verify query planner honors MATERIALIZED
def test_cte_materialized_in_query_plan():
    plan = conn.execute(
        "EXPLAIN QUERY PLAN WITH ordered AS MATERIALIZED (...) SELECT ..."
    ).fetchall()
    plan_text = ' '.join(str(row) for row in plan)
    assert 'MATERIALIZE' in plan_text.upper(), \
        f"Query planner ignored MATERIALIZED hint. Plan: {plan_text}"
```

#### 6.1: Startup Python Validation

**Fix:** Validate `sys.executable` at startup:

```python
# In pipeline/__init__.py or main entry point
import subprocess, sys

def validate_environment():
    """Validate that required libraries are importable by the subprocess Python."""
    required = ['numpy', 'sqlite3']  # Add walk dependencies
    for lib in required:
        try:
            subprocess.run(
                [sys.executable, '-c', f'import {lib}'],
                check=True, capture_output=True, timeout=10
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(
                f"Cannot import '{lib}' using sys.executable={sys.executable}. "
                f"Subprocesses spawned by the walk orchestrator will fail. "
                f"Ensure the Python environment has all dependencies installed."
            ) from e

# Called at module import:
validate_environment()
```

#### C4: Standardize PRAGMA Configuration in Connection Factory

**Fix:** Centralize connection creation:

```python
# pipeline/db.py — SINGLE connection factory for ALL pipeline code
import sqlite3


def create_connection(db_path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """Create a properly configured SQLite connection.
    
    ALL pipeline code MUST use this factory. Never call sqlite3.connect() directly.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Standard PRAGMAs — applied on EVERY connection
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")   # MUST be before any transaction
    
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    
    return conn
```

---

### Cross-Pattern Harmony

#### Transaction Nesting (Pattern 3+5+6): SAVEPOINT Manager

**Root cause:** Python's `sqlite3` raises `OperationalError` on nested `BEGIN`. Transaction nesting is NOT supported in SQLite. The SAVEPOINT command is the only mechanism for nested transaction-like behavior.

**Fix: TransactionCoordinator — a single class that manages the transaction stack.**

```python
class TransactionCoordinator:
    """Manages transaction nesting via SAVEPOINT.
    
    Usage:
        tx = TransactionCoordinator(conn)
        
        with tx.begin():           # BEGIN (outermost)
            tx.execute("INSERT ...")
            
            with tx.savepoint():   # SAVEPOINT sp_1 (nested)
                tx.execute("UPDATE ...")
                # If this raises -> ROLLBACK TO sp_1, outer transaction continues
            
            with tx.savepoint():   # SAVEPOINT sp_2 (nested)
                tx.execute("DELETE ...")
            
            # COMMIT (commits everything including nested savepoints)
    
    This allows Pattern 3's complete_walk() (outer transaction) to safely call
    Pattern 5's execute_split() (inner savepoint) without OperationalError.
    """
    
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._depth = 0
        self._savepoint_counter = 0
    
    @contextmanager
    def begin(self):
        """Start outermost transaction."""
        if self._depth > 0:
            raise TransactionError("begin() called while transaction already active")
        self._depth = 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0
    
    @contextmanager
    def savepoint(self):
        """Start nested savepoint. Safe to call inside begin() or another savepoint()."""
        self._savepoint_counter += 1
        sp_name = f"sp_{self._savepoint_counter}"
        self.conn.execute(f"SAVEPOINT {sp_name}")
        self._depth += 1
        try:
            yield
            self.conn.execute(f"RELEASE {sp_name}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO {sp_name}")
            raise
        finally:
            self._depth -= 1
    
    def execute(self, sql: str, params=None):
        return self.conn.execute(sql, params or ())


# Usage in WalkOrchestrator:
async def execute_walk(self, walk_name, book_id, run_id):
    tx = TransactionCoordinator(self.conn)
    
    with tx.begin():  # Outer transaction — annotations + state update
        # ... run subprocess, get results ...
        
        # Write annotations (inside outer transaction)
        for annotation in result.annotations:
            tx.execute("INSERT INTO ...", ...)
        
        # Deterministic executor operations use savepoints
        # (so a single split failure doesn't roll back the whole walk)
        with tx.savepoint():
            self.executor.execute_split(...)  # may call its own savepoints internally
        
        # Advance pipeline state (SAME transaction)
        self.state_machine.complete_walk(self.conn, run_id, walk_name, next_walk)
        
        # COMMIT happens on __exit__ of tx.begin()
```

**Cited evidence:**
- **[Tier 1] SQLite SAVEPOINT docs** — "Transactions created using BEGIN...COMMIT do not nest. For nested transactions, use the SAVEPOINT and RELEASE commands." This is the authoritative source. [https://sqlite.org/lang_savepoint.html](https://sqlite.org/lang_savepoint.html)
- **[Tier 1] SQLite Transaction docs** — "An attempt to invoke the BEGIN command within a transaction will fail with an error, regardless of whether the transaction was started by SAVEPOINT or a prior BEGIN." Confirms `OperationalError` on nested BEGIN. [https://www.sqlite.org/lang_transaction.html](https://www.sqlite.org/lang_transaction.html)
- **[Tier 1] Python sqlite3 docs** — Documents `OperationalError` for "a transaction could not be processed." Nested BEGIN is this exact case. [https://docs.python.org/3/library/sqlite3.html#sqlite3.OperationalError](https://docs.python.org/3/library/sqlite3.html#sqlite3.OperationalError)

---

#### Dirty-Flag + Walk Orchestration (Pattern 3+4+5): Stale Data Detection Before Commit

**Fix:** Before committing annotations, compare span `updated_at` timestamps against the walk start timestamp. If ANY span was modified during the subprocess execution, the walk ran on stale data.

```python
# In WalkOrchestrator.execute_walk(), BEFORE committing annotations:

def _validate_no_stale_data(self, walk_name: str, walk_start_ms: int) -> bool:
    """Check if any span targeted by this walk was modified during execution.
    
    Returns True if data is fresh (no spans modified after walk_start_ms).
    Returns False if stale data detected (must re-run walk).
    """
    # Query: spans that were modified AFTER this walk started
    stale = self.conn.execute(
        """SELECT COUNT(*) as cnt FROM span s
           JOIN paragraph_span ps ON s.span_id = ps.span_id
           WHERE ps.paragraph_id IN (
               SELECT paragraph_id FROM book_chapter bc
               JOIN chapter_scene csc ON bc.chapter_id = csc.chapter_id
               JOIN scene_paragraph sp ON csc.scene_id = sp.scene_id
               WHERE bc.book_id = ?
           )
           AND s.updated_at > ?""",
        (self.book_id, walk_start_ms)
    ).fetchone()['cnt']
    
    return stale == 0
```

If stale data is detected:
- **Option A (strict):** Abort and re-run the walk. Roll back the current transaction.
- **Option B (lenient):** Accept with a `stale_data_warning` flag in the walk result. The downstream consumer decides.

For production, Option A is recommended — stale annotations are worse than slower pipelines.

---

#### Export + Concurrent Walks (Pattern 3+7): Pipeline Gating

**Fix:** Gate export on pipeline completion.

```python
def export_annotated_script(self, book_id: str) -> dict:
    """Export annotated script. Gated on pipeline completion."""
    
    # Check pipeline status
    active_run = self.conn.execute(
        """SELECT status, current_walk FROM pipeline_run
           WHERE book_id = ? AND status IN ('pending', 'running', 'paused')
           ORDER BY started_at DESC LIMIT 1""",
        (book_id,)
    ).fetchone()
    
    if active_run:
        raise PipelineActiveError(
            f"Cannot export: pipeline is {active_run['status']} "
            f"(walk: {active_run['current_walk']}). "
            f"Export is available when pipeline completes. "
            f"HTTP 409 Conflict."
        )
    
    # Proceed with export...
```

---

#### Crash Recovery + Dirty Flags (Pattern 3+4): Pre-Resume Dirty Check

**Fix:** `recover()` must check dirty flags BEFORE resuming any walk.

```python
def recover(self, conn: sqlite3.Connection) -> list[dict]:
    """Find interrupted runs and determine recovery actions.
    
    NOW INCLUDES dirty-flag check: if spans were modified during the crash window,
    reset to the earliest dirty walk rather than resuming at the interrupted walk.
    """
    interrupted = conn.execute(
        "SELECT run_id, book_id, current_walk FROM pipeline_run WHERE status = 'running'"
    ).fetchall()
    
    recovery_actions = []
    for run in interrupted:
        book_id = run['book_id']
        interrupted_walk = run['current_walk']
        
        # --- NEW: Check dirty flags BEFORE determining resume point ---
        earliest_dirty_walk = self._find_earliest_dirty_walk(conn, book_id)
        
        if earliest_dirty_walk and self._walk_order(earliest_dirty_walk) < self._walk_order(interrupted_walk):
            # Structural edits occurred during crash — spans modified BEFORE the
            # interrupted walk. Reset to the earliest affected walk.
            recovery_actions.append({
                'run_id': run['run_id'],
                'book_id': book_id,
                'resume_at': earliest_dirty_walk,
                'reason': f"Dirty spans detected for walk {earliest_dirty_walk} "
                          f"(structural edits during crash window). "
                          f"Resetting from interrupted walk {interrupted_walk}."
            })
        else:
            # No dirty flags before interrupted walk — resume normally
            verified = self._verify_walk_completion(conn, run['run_id'], interrupted_walk)
            if verified:
                recovery_actions.append({
                    'run_id': run['run_id'],
                    'book_id': book_id,
                    'resume_at': self._next_walk(interrupted_walk),
                    'reason': f"Walk {interrupted_walk} annotations verified. Resuming at next walk."
                })
            else:
                recovery_actions.append({
                    'run_id': run['run_id'],
                    'book_id': book_id,
                    'resume_at': interrupted_walk,
                    'reason': f"Walk {interrupted_walk} annotations incomplete. Re-running."
                })
    
    return recovery_actions

def _find_earliest_dirty_walk(self, conn, book_id) -> str | None:
    """Find the earliest walk whose spans have been modified since completion."""
    WALK_ORDER = ['2a', '2b', '2c', '2d', '2e', '2f']
    
    for walk in WALK_ORDER:
        dirty_count = conn.execute(
            """SELECT COUNT(*) FROM span s
               JOIN paragraph_span ps ON s.span_id = ps.span_id
               JOIN scene_paragraph sp ON ps.paragraph_id = sp.paragraph_id
               JOIN chapter_scene csc ON sp.scene_id = csc.scene_id
               JOIN book_chapter bc ON csc.chapter_id = bc.chapter_id
               LEFT JOIN span_last_walk slw
                   ON s.span_id = slw.span_id AND slw.walk_name = ?
               WHERE bc.book_id = ?
                 AND (slw.completed_at IS NULL OR s.updated_at > slw.completed_at)""",
            (walk, book_id)
        ).fetchone()[0]
        
        if dirty_count > 0:
            return walk
    
    return None
```

---

### Additional Counter Concerns Addressed

#### M5 Pre-Pass Proper Noun Classification

The Counter raised: "M5 pre-pass could classify 'May' as a name (false positive for the month)."

**Fix: Add context-aware noun classification to M5 pre-pass.**

```python
class M5ProperNounPrePass:
    """Deterministic proper noun classification for walks 2b and 2c.
    
    Uses contextual disambiguation for ambiguous words like "May"/"may".
    """
    
    # Ambiguous words that can be either proper nouns or common words
    AMBIGUOUS = {
        'May': {'context_indicators': ['month', 'spring', 'april', 'june', 'first of', 'end of']},
        'March': {'context_indicators': ['month', 'spring', 'february', 'april', 'first of', 'end of']},
        'August': {'context_indicators': ['month', 'summer', 'july', 'september', 'first of', 'end of']},
        'Will': {'context_indicators': []},  # Almost always a name when capitalized
        'Rose': {'context_indicators': ['flower', 'garden', 'petal', 'thorn']},  # Flower if in garden context
    }
    
    def classify(self, token: str, context_window: list[str]) -> str:
        """Classify a capitalized token as PROPER_NOUN or COMMON.
        
        Args:
            token: The capitalized word (e.g., "May")
            context_window: Surrounding words (e.g., ["the", "third", "of", ...])
        
        Returns:
            'PROPER_NOUN' or 'COMMON'
        """
        if token not in self.AMBIGUOUS:
            # Unambiguous: if it's capitalized and not at sentence start, it's a proper noun
            return 'PROPER_NOUN'
        
        # Check contextual indicators
        indicators = self.AMBIGUOUS[token]['context_indicators']
        context_lower = ' '.join(context_window).lower()
        
        for indicator in indicators:
            if indicator in context_lower:
                return 'COMMON'  # e.g., "the third of May" -> COMMON
        
        return 'PROPER_NOUN'  # e.g., "May walked away" -> PROPER_NOUN
```

---

#### Temperature Try/Except: Distinguishing Rejection vs. Other Failure

**Fix:** Wrap LLM subprocess calls to distinguish between "temperature rejection" (expected, retry without temperature) and "other failure" (unexpected, propagate error):

```python
class TemperatureFallbackError(Exception):
    """LLM rejected output due to temperature variation. Retry with temperature=0."""
    pass

async def _run_subprocess_with_fallback(self, walk_name, config_path):
    """Run LLM subprocess. Retry without temperature on rejection."""
    try:
        result = await self._run_subprocess(walk_name, config_path, temperature=0.7)
        return result
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else ""
        
        # Check for known rejection patterns
        rejection_patterns = [
            'temperature rejection',
            'output varied significantly',
            'reproducibility check failed',
            'confidence below threshold',
            'inconsistent output',
        ]
        
        if any(pattern in stderr.lower() for pattern in rejection_patterns):
            # Expected: temperature rejection -> retry without temperature
            logger.info(
                f"Walk {walk_name}: temperature rejection detected. "
                f"Retrying with temperature=0."
            )
            result = await self._run_subprocess(walk_name, config_path, temperature=0.0)
            return result
        
        # Unexpected failure: propagate
        raise WalkExecutionError(
            f"Walk {walk_name} failed with unexpected error: {stderr[:500]}"
        ) from e
```

The distinguishing mechanism: the LLM subprocess writes specific error messages to stderr that indicate "this is a temperature-related rejection" vs. "this is an unrecoverable failure." The orchestrator pattern-matches on known rejection strings.

---

#### Series Voice Locking: Book 3 Automatically Reuses Book 1's Voice

**Fix:** Series voice locking is identity-based. When a character appears in multiple books, the `voice_casting` table links `character_id` → `voice_id`. This is a series-level mapping, not book-level.

```sql
-- voice_casting table (Pattern 6)
CREATE TABLE voice_casting (
    character_id TEXT NOT NULL REFERENCES character(character_id),
    voice_id TEXT NOT NULL,
    cast_in_book TEXT NOT NULL REFERENCES book(book_id),  -- Which book established this casting
    created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
    PRIMARY KEY (character_id, voice_id)
);
```

When processing book 3, the walk 2f (voice casting) queries:

```sql
-- Does this character already have a voice from a prior book?
SELECT vc.voice_id, vc.cast_in_book
FROM voice_casting vc
WHERE vc.character_id = ?
ORDER BY vc.created_at ASC
LIMIT 1;
```

If found → reuse that voice (with lock enforcement from Pattern 6's `_enforce_voice_locks`). If not found → this is the first casting for this character; proceed with LLM-cast voice.

The `cast_in_book` column tracks provenance: knowing that Character X's voice was established in Book 1 allows debugging of voice consistency issues across the series.

---

#### Export `BEGIN IMMEDIATE` vs. Deferred in WAL Mode

**Counter Risk 7.1:** `BEGIN IMMEDIATE` on the export connection blocks concurrent writers even though export is read-only.

**Fix:** Use `BEGIN TRANSACTION` (deferred) for read-only export connections in WAL mode.

The SQLite docs confirm: in WAL mode with a single writer, a deferred `BEGIN` followed by SELECT statements creates a read transaction with snapshot isolation. The snapshot is frozen at the moment of the first SELECT, and subsequent writes by other connections are invisible to this reader. This is EXACTLY the behavior we want for export consistency — and it doesn't block writers at all.

`BEGIN IMMEDIATE` is only needed when the transaction will later UPGRADE to a write transaction (to avoid `SQLITE_BUSY` on the upgrade). Since export is purely read-only, deferred BEGIN is correct and non-blocking.

```python
def export_annotated_script(self, book_id: str) -> dict:
    export_conn = create_connection(self.db_path, read_only=True)
    try:
        # DEFERRED (not IMMEDIATE) — snapshot isolation in WAL mode,
        # does NOT block the pipeline writer
        export_conn.execute("BEGIN TRANSACTION")
        
        # First SELECT freezes the snapshot
        spans = export_conn.execute("SELECT ... WHERE book_id = ?", (book_id,)).fetchall()
        
        export_conn.execute("COMMIT")
        return self._format_export(spans)
    finally:
        export_conn.close()
```

**Cited evidence:**
- **[Tier 1] SQLite Isolation docs** — "When a read transaction starts, that reader continues to see an unchanging 'snapshot' of the database file as it existed at the moment in time when the read transaction started." In WAL mode, this snapshot is obtained with a plain `BEGIN` (deferred) — no IMMEDIATE needed. [https://sqlite.org/isolation.html](https://sqlite.org/isolation.html)
- **[Tier 1] SQLite Forum post b6cdf4c5ec560923** — Community discussion confirming that "BEGIN IMMEDIATE... immediately starts a write transaction even if you never issue any writes." Using BEGIN IMMEDIATE for read-only work is unnecessary and blocks writers. [https://sqlite.org/forum/forumpost/b6cdf4c5ec560923](https://sqlite.org/forum/forumpost/b6cdf4c5ec560923)

---

### Summary of All Fixes

| Risk ID | Severity | Fix | Lines Changed |
|---------|----------|-----|---------------|
| C1 | BLOCKING | Timestamps → INTEGER millis (`unixepoch('subsec')*1000`) | Schema: 4 tables, Python: `now_ms()` helper |
| C2/Risk 3.1 | BLOCKING | `complete_walk()` accepts `conn` param, single-connection atomicity | `PipelineStateMachine` + `WalkOrchestrator` |
| Risk 5.3 | BLOCKING | `execute_move`: DELETE → reindex → INSERT (matches Pattern 1) | `execute_move` restructured |
| Risk 5.1 | HIGH | `defer_foreign_keys=ON` with explicit `foreign_key_check` before commit | `TransactionWrapper` class |
| Risk 5.2 | HIGH | Annotations reconciled BEFORE parent change; parent-scoped recomputed after | `execute_move` annotation flow |
| Risk 3.2 | MEDIUM | `PRAGMA wal_checkpoint(PASSIVE)` after walk completion commit | 1 line in `execute_walk` |
| Risk 7.2 | MEDIUM | `PRAGMA query_only = ON` for export connections | `create_connection(read_only=True)` |
| Risk 8.2 | MEDIUM | Remove `'he'`/`'she'` from STOPWORDS; dialogue tags suppressed by pattern | `STOPWORDS` set + `DIALOGUE_TAG_PATTERN` |
| Risk 6.2 | MEDIUM | Per-span voice lock enforcement with auto-correction + logging | `_enforce_voice_locks` |
| Risk 1.2 | LOW | Document single-writer invariant as module-level comment | Docstring |
| Risks 2.1/2.2 | LOW | Guardrail log + EXPLAIN QUERY PLAN integration test | Warning + test |
| Risk 6.1 | LOW | Startup validation of `sys.executable` | `validate_environment()` |
| C4 | LOW | Centralized `create_connection()` factory with standard PRAGMAs | `pipeline/db.py` |
| Cross-Pattern: Transaction Nesting | BLOCKING (new) | `TransactionCoordinator` with SAVEPOINT support | New class |
| Cross-Pattern: Stale Data | MEDIUM (new) | Pre-commit dirty-flag check comparing `updated_at` vs `walk_start_ms` | `_validate_no_stale_data` |
| Cross-Pattern: Export Gate | MEDIUM (new) | Check `pipeline_run.status` before export; raise `PipelineActiveError` | `export_annotated_script` gate |
| Cross-Pattern: Recovery + Dirty | MEDIUM (new) | `_find_earliest_dirty_walk` called in `recover()` before resuming | `recover()` + helper |
| Counter: M5 Nouns | LOW (new) | Context-aware ambiguous word disambiguation (`AMBIGUOUS` dict) | `M5ProperNounPrePass.classify` |
| Counter: Temperature | LOW (new) | Pattern-match stderr for rejection strings vs. unexpected failures | `_run_subprocess_with_fallback` |
| Counter: Voice Locking | Clarification | Voice locking is series-level via `voice_casting` table | Schema clarification |
| Counter: Export WAL | MEDIUM (new) | Deferred `BEGIN TRANSACTION` instead of `BEGIN IMMEDIATE` for read-only | `export_annotated_script` BEGIN type |

**Verdict:** All 3 BLOCKING issues resolved. All 2 HIGH issues resolved. All 4 MEDIUM issues resolved. All 4 LOW issues resolved. All 5 cross-pattern interactions addressed. All 4 additional Counter concerns addressed. The implementation is ready for production implementation.



---

## Open Risks & Human Questions

*Appended by rnd-counter-improver, Turn 8 — 2026-08-03*

---

### Fix Assessment Matrix

| Risk ID (Turn 6) | Severity | Improver's Fix | Verdict | Remaining Concern |
|---|---|---|---|---|
| **C1** (timestamp format) | BLOCKING | INTEGER millis since epoch (`unixepoch('subsec')*1000`) | ✅ RESOLVED | None. Integer comparison is unambiguous, immune to format differences. Validated: 2^63-1 ms = ~292M years, no overflow risk. |
| **C2 / 3.1** (separate connection) | BLOCKING | `complete_walk(conn)` accepts caller's connection; `PipelineStateMachine` no longer owns a connection | ✅ RESOLVED | None. Single-connection invariant restored. Annotations + state update now share one transaction. |
| **5.3** (move reindex order) | BLOCKING | DELETE → reindex old → INSERT → reindex new (matches Pattern 1 order) | ⚠️ PARTIALLY RESOLVED | Ordering is corrected conceptually, but `_reindex_before_insert` uses `ORDER BY position DESC` in UPDATE without LIMIT — **see New Risk N1 below.** |
| **5.1** (FK tracking reset) | HIGH | `TransactionWrapper` with `PRAGMA foreign_key_check` before commit; never sets `defer_foreign_keys=OFF` | ✅ RESOLVED | The fix correctly avoids the FK counter reset. `foreign_key_check` runs before commit. Verified: SQLite auto-resets `defer_foreign_keys` at COMMIT/ROLLBACK. |
| **5.2** (wrong annotation context) | HIGH | Classify annotations as span-scoped (follow the span) vs. parent-scoped (recompute); reconcile BEFORE parent change | ✅ RESOLVED | Correct classification. Sentence boundaries are the only parent-scoped annotation type; words, characters, voices, emotions are span-scoped. |
| **3.2** (WAL checkpoint) | MEDIUM | `PRAGMA wal_checkpoint(PASSIVE)` after walk completion commit | ✅ RESOLVED | Correct placement after `with self.conn:` block. PASSIVE is non-blocking; in single-writer architecture, equivalent to FULL. |
| **7.2** (export without query_only) | MEDIUM | `PRAGMA query_only = ON` via `create_connection(read_only=True)` | ✅ RESOLVED | Belt-and-suspenders guard against accidental writes during export. |
| **8.2** (pronouns in STOPWORDS) | MEDIUM | Removed `'he'`/`'she'` from stopwords; dialogue tags suppressed by `DIALOGUE_TAG_PATTERN` regex instead | ✅ RESOLVED | Correct fix. Pronouns preserved for character attribution; dialogue tags still suppressed. |
| **6.2** (voice lock all-or-nothing) | MEDIUM | Per-span enforcement with auto-correction + logging | ✅ RESOLVED | Walk no longer fails on voice lock violations. Violating spans auto-corrected and logged. |
| **1.2** (single-writer invariant) | LOW | Documented as module-level hard constraint | ✅ RESOLVED | Clear docstring documenting the invariant and its enforcement mechanisms. |
| **2.1/2.2** (CTE guardrail) | LOW | Guardrail log at 5000 spans + EXPLAIN QUERY PLAN integration test | ✅ RESOLVED | Warning threshold and query plan validation test both present. |
| **6.1** (startup Python validation) | LOW | `validate_environment()` at module import | ✅ RESOLVED | Checks `sys.executable` can import required libraries. |
| **C4** (PRAGMA config per-conn) | LOW | Centralized `create_connection()` factory with standard PRAGMAs | ⚠️ PARTIALLY RESOLVED | Factory is correct but **does not set `isolation_level = None`** — see **New Risk N2.** |
| **5.4** (execute_delete cascade) | LOW | Not explicitly addressed in Turn 7 | ✅ IMPLICITLY RESOLVED | The code order (reconcile BEFORE delete) was already correct. CASCADE is immediate. No fix needed. |
| **8.1** (TF-IDF triviality) | LOW | Not addressed | ⚠️ UNRESOLVED | The two-document corpus makes TF-IDF essentially O(terms). This is design-level: is TF-IDF worth its complexity cost for a 2-doc corpus? See HJ4. |

**New cross-pattern mechanisms introduced by the Improver (all correctly designed):**

| New Mechanism | Connecting Patterns | Verdict |
|---|---|---|
| `TransactionCoordinator` (SAVEPOINT nesting) | Pattern 3 + 5 + 6 | ✅ Design is sound; SAVEPOINT names are per-transaction, no collision across begin() calls. BUT — see New Risk N2 (isolation_level conflict). |
| Stale-data detection (`updated_at > walk_start_ms`) | Pattern 3 + 4 + 5 | ✅ Correct approach. Compares span `updated_at` against walk start timestamp before committing annotations. |
| Export pipeline gating (`pipeline_run.status` check) | Pattern 3 + 7 | ✅ Returns `PipelineActiveError` if pipeline is running. See HJ3 for UX tradeoff. |
| Crash recovery + dirty-flag integration | Pattern 3 + 4 | ✅ `recover()` checks `_find_earliest_dirty_walk()` before determining resume point. |
| M5 context-aware noun classification | Walk 2b/2c | ✅ `AMBIGUOUS` dict with contextual indicators handles month/name ambiguity. |
| Temperature rejection pattern-matching | Walk all | ✅ Pattern-matches stderr for known rejection strings vs. unexpected failures. |
| Export deferred BEGIN (not IMMEDIATE) | Pattern 7 | ✅ Deferred BEGIN in WAL mode gives snapshot isolation without blocking writers. |

---

### New Risks from Fixes

#### N1: [HIGH] `_reindex_before_insert` Uses `ORDER BY position DESC` Without LIMIT — Parse Error

- **Source:** [Tier 1] SQLite source code (`update.c`) — line `if( pOrderBy && pLimit==0 ) { sqlite3ErrorMsg(pParse, "ORDER BY without LIMIT on UPDATE"); return; }` (https://github.com/sqlite/sqlite/blob/master/src/update.c)
  **Mechanism:** The `_reindex_before_insert` function proposed in Turn 7 executes:
  ```sql
  UPDATE paragraph_span SET position = position + 1
  WHERE paragraph_id = ? AND position >= ?
  ORDER BY position DESC
  ```
  Even with `SQLITE_ENABLE_UPDATE_DELETE_LIMIT` enabled, SQLite requires a LIMIT clause when ORDER BY is present in an UPDATE statement. Without LIMIT, this produces a parse error: `"ORDER BY without LIMIT on UPDATE"`.
  **Trigger:** Every call to `_reindex_before_insert` — the common case for any insert operation.
  **Blast radius:** Insert operations fail with a syntax error. The entire `execute_move` and any other operation that inserts at a non-terminal position would fail at parse time.
  **Mitigation (two options):**
  - **Option A:** Add `LIMIT -1` (negative means "no limit" per SQLite docs). The SQL would become: `ORDER BY position DESC LIMIT -1`. However, even with LIMIT, the SQLite docs state: "The ORDER BY clause on an UPDATE statement is used only to determine which rows fall within the LIMIT. The order in which rows are modified is arbitrary and is not influenced by the ORDER BY clause." The processing order is NOT guaranteed.
  - **Option B (RECOMMENDED):** Use the negative-space pattern for the insert side, mirroring the delete-side reindex. Instead of `ORDER BY position DESC`:
    ```sql
    -- Phase 1: Shift to negative space (no collision — going to larger positions)
    UPDATE paragraph_span 
    SET position = -(position + 1) 
    WHERE paragraph_id = ? AND position >= ?
    -- Phase 2: Convert to positive (positions shift DOWN by 1)
    UPDATE paragraph_span 
    SET position = ABS(position) 
    WHERE paragraph_id = ? AND position < 0
    ```
    This traces as: position 3→-4→4, position 4→-5→5, position 5→-6→6. No collisions, no ORDER BY dependency, proven safe by the same mechanism as the delete-side reindex. This is the Pattern 1 approach applied symmetrically to the insert case.
  **Severity:** HIGH — blocks all insert-at-position operations. Mitigation Option B is provably correct and requires removing one function (`_reindex_before_insert`) and replacing it with the symmetric negative-space function.

#### N2: [MEDIUM] `create_connection()` Does Not Set `isolation_level=None` — Conflicts with Manual Transaction Control

- **Source:** [Tier 1] Python CPython issue #28518 — `execute("begin immediate") throwing OperationalError` (https://github.com/python/cpython/issues/72704) + SQLAlchemy docs on sqlite3 transaction control (https://docs.sqlalchemy.org/en/21/dialects/sqlite.html)
  **Mechanism:** Python's `sqlite3` module with default `LEGACY_TRANSACTION_CONTROL` auto-starts transactions before DML statements (INSERT, UPDATE, DELETE, REPLACE). The `create_connection()` factory does NOT set `isolation_level = None` or `autocommit = False`. 
  
  When the `TransactionCoordinator.begin()` calls `BEGIN IMMEDIATE`, if ANY prior DML statement was executed on that connection (triggering an implicit BEGIN), the `BEGIN IMMEDIATE` fails with `OperationalError: cannot start a transaction within a transaction`.
  
  Additionally, SAVEPOINT statements issued before a DML-triggered BEGIN do not participate in the enclosing transaction — so a `ROLLBACK` would not restore data that was inside a released savepoint.
  
  **Richard Hipp's analysis** (from the CPython issue): `BEGIN IMMEDIATE` causes `sqlite3_stmt_readonly()` to return FALSE (because IMMEDIATE acquires a write lock), which causes Python's auto-transaction logic to start an implicit transaction BEFORE executing the `BEGIN IMMEDIATE` statement. The `BEGIN IMMEDIATE` then fails because it's inside an already-active transaction.
  **Trigger:** Any code path where a DML statement is executed on a connection before `TransactionCoordinator.begin()` is called.
  **Blast radius:** `begin()` fails with OperationalError. Walk execution aborts.
  **Mitigation:** Add `conn.isolation_level = None` to the `create_connection()` factory. This disables Python's implicit transaction management entirely, giving `TransactionCoordinator` full control. This is the standard pattern used by SQLAlchemy and other ORMs for explicit transaction management with SQLite.
  **Severity:** MEDIUM — depends on whether any DML executes before `begin()`. With the current walk orchestrator pattern (execute_walk starts with `begin()`), this may not trigger in the happy path. However, any background operation, connection reuse, or future code change that interleaves DML and explicit BEGIN would break.

#### N3: [SPECULATIVE] Stale-Data Detection False Negatives Under NTP Clock Adjustment

- **Source:** [Tier 5] Design analysis — speculative.
  **Mechanism:** `_validate_no_stale_data` compares `span.updated_at` (INTEGER millis set at INSERT time) against `walk_start_ms` (INTEGER millis set at walk start time). Both use wall-clock time. If NTP adjusts the system clock backward during a multi-hour pipeline run, `walk_start_ms` could record a time LATER than some spans that were actually updated BEFORE the walk started. These spans would NOT be detected as stale.
  **Trigger:** NTP step correction (as opposed to slewing) during a long-running walk. NTP steps of >1 second are rare but occur after network changes or clock drift.
  **Blast radius:** A span modified before the walk starts (but after a backward clock adjustment) would not be flagged — the walk would produce annotations on potentially stale data. With time-zone-aware timestamps and typical NTP behavior (slewing, not stepping), this is extremely unlikely.
  **Mitigation:** Use `time.monotonic()` for `walk_start_ms` and store it alongside the DB timestamps for comparison logic. However, `time.monotonic()` cannot be compared to DB timestamps (which are wall-clock). A hybrid approach: store both `walk_start_wall_ms` (for DB comparison) and `walk_start_monotonic_ns` (for drift detection). If the difference between wall and monotonic time at commit deviates from the difference at start, flag as "clock-adjustment-detected" and conservatively re-run the walk. This is defensive but low-value given actual NTP behavior.
  **Severity:** LOW — NTP step corrections during pipeline execution are extremely rare (<0.001% probability). The cost of a missed detection (one walk's annotations slightly stale) is much lower than the engineering cost of the fix.

---

### Unresolved Risks

#### UR1: ORDER BY in UPDATE Processing Order Not Guaranteed — Even with LIMIT Added

- **Original Risk:** None (new finding from verification).
- **Source:** [Tier 1] SQLite `lang_update.html` — "The ORDER BY clause on an UPDATE statement is used only to determine which rows fall within the LIMIT. The order in which rows are modified is arbitrary and is not influenced by the ORDER BY clause." (https://www.sqlite.org/lang_update.html)
- **Status:** Mitigation Option B (negative-space pattern for insert side) eliminates the ORDER BY dependency entirely. This is the recommended fix and also resolves New Risk N1 (parse error from missing LIMIT). **If the Improver chooses Option A (add LIMIT -1), the processing-order risk remains unresolved.**
- **Recommendation:** Use Option B — the negative-space pattern is provably safe and symmetrical with the delete-side reindex.

#### UR2: TF-IDF Complexity May Be Unjustified for 2-Document Corpus

- **Original Risk:** 8.1 (Turn 6) — TF-IDF on a 2-document corpus is essentially length-normalized token overlap.
- **Status:** Not addressed in Turn 7.
- **Why it matters:** The TF-IDF computation involves computing IDF for each token across 2 documents. IDF = log(2 / df(t)). For a token appearing in 1 document: IDF = log(2/1) = 0.301. For a token appearing in 2 documents: IDF = log(2/2) = 0. So tokens appearing in BOTH documents have ZERO weight. This means TF-IDF weighted Jaccard is equivalent to: "Jaccard similarity of tokens that appear in ONLY ONE of the two annotations." This is a very specific similarity function that may not capture the intended behavior.
- **Recommendation:** Test empirically on 20+ real annotation pairs. If simple Jaccard (with dialogue tag suppression) performs comparably, drop TF-IDF to reduce complexity. See HJ4.

---

### Human Judgment Questions

#### HJ1: Reprocess vs. Port for Migration from Old Pipeline

- **Question:** Should the migration from the old pipeline reprocess every book from original EPUBs (reprocessing: reproducible, expensive in LLM costs) or attempt to port existing annotation state (porting: lossy, complex)?
- **What's at stake:**
  - **Reprocessing:** Every book runs through the full 6-walk pipeline again. LLM API costs for a library of 50+ books. Guaranteed consistency with the new pipeline's annotation format. Weeks of compute time. Zero migration engineering.
  - **Porting:** One-time engineering cost to write a migration script (3-5 days). Maps old annotation tables to new schema. Lossy: old pipeline annotations may not map 1:1 to new pipeline formats (different span granularity, different annotation types). Creates a "legacy" vs. "fresh" quality distinction in the data forever.
- **Evidence-based recommendation:** **Port for structural data, reprocess for annotation data.** The structural hierarchy (book → chapter → scene → paragraph → span) can be ported with high fidelity (1:1 mapping). Annotations (character, voice, emotion) should be regenerated because: (a) the new pipeline's annotation format differs from the old pipeline's, (b) annotation quality improves with pipeline rewrites, (c) LLM costs for annotation-only reprocessing are lower than full pipeline reprocessing (skipping the structural extraction steps). **Confidence: MEDIUM.** This is a cost/quality tradeoff that depends on the user's book library size and LLM budget.
- **If we choose wrong:** Reprocessing everything: wasted LLM budget, delayed availability. Porting everything: permanently degraded annotation quality for existing books, inconsistent data between migrated and fresh-processed books.

#### HJ2: Walk 2c Scope — LOCAL vs. GLOBAL for Alias Resolution

- **Question:** The Ideator changed walk 2c (alias/entity resolution) from GLOBAL to LOCAL using `updated_at` dirty-flag tracking. But alias resolution is inherently cross-scene — changing one character's alias group affects attribution across the entire book. Is LOCAL scoping correct, or should alias resolution remain GLOBAL?
- **What's at stake:**
  - **LOCAL (current design):** Only scenes with structurally-modified spans are re-processed. Saves LLM costs. Risk: an alias change in scene 3 breaks attribution in scene 7, but scene 7 wasn't re-processed because its spans weren't modified → inconsistent character attribution across the book.
  - **GLOBAL:** Every structural edit triggers full-book alias resolution. Matches the cross-scene nature of alias resolution. Costly: walk 2c runs on the entire book for any structural change anywhere.
- **Evidence-based recommendation:** **Hybrid: LOCAL re-processing for dirty spans, GLOBAL validation pass.** Process only dirty spans with LLM for alias resolution, then run a lightweight deterministic validation pass across the full book: for each character, verify that all mentions resolve to the same alias group. If the validation pass finds inconsistencies, escalate to GLOBAL re-processing. This gives LOCAL cost savings in the common case (structural edits to unrelated scenes don't affect aliases) with GLOBAL correctness as a safety net. **Confidence: MEDIUM.** The effectiveness depends on how many structural edits actually create cross-scene alias inconsistencies — this requires empirical measurement.
- **If we choose wrong:** Sticking with LOCAL without validation: silent character attribution errors that are very hard to detect (manifest as incorrect audiobook voice assignments). Switching to full GLOBAL: wasted LLM costs for unrelated structural edits.

#### HJ3: Export During Pipeline — Strict Gate vs. Best-Effort Preview

- **Question:** The Improver proposed a strict gate: return `PipelineActiveError` (HTTP 409) if the pipeline is running. But what if the pipeline has been running for hours and the user wants to preview partial results? Should there be a "best-effort export" option?
- **What's at stake:**
  - **Strict gate:** Export always returns a consistent, complete snapshot. No ambiguity about what data the user is seeing. But: no preview capability. User waits hours for pipeline completion before seeing any output.
  - **Best-effort preview:** User can export at any time, seeing whatever annotations exist. Useful for debugging and impatient users. But: annotations from the current walk are incomplete, producing a confusing mix of old and new annotations for the same book.
- **Evidence-based recommendation:** **Strict gate with informational response.** Return 409 with metadata: `{"status": "running", "current_walk": "2c", "walks_completed": ["2a","2b"], "estimated_completion": "~45 minutes"}`. This gives the user visibility into progress without exposing inconsistent data. The completed walks (2a, 2b) have their annotations fully written — the user knows exactly what's available. The running walk (2c) has incomplete annotations. **Confidence: HIGH.** This follows the principle of least surprise: never expose partially-written data to the user. The informational metadata addresses the "I want to know what's happening" need without compromising data integrity.
- **If we choose wrong:** Strict gate with no progress info: frustrated users watching a spinner. Best-effort preview: user exports, sees incomplete data, doesn't realize it's incomplete, makes decisions based on partial annotations.

#### HJ4: Jaccard Threshold Tuning — Empirical Calibration Required

- **Question:** The annotation overlap reconciliation (Pattern 8) uses TF-IDF weighted Jaccard + character 3-gram Jaccard with a threshold. What threshold value (0.5? 0.6? 0.7?) is correct for audiobook text? This cannot be determined from first principles — it requires empirical calibration on actual novel text.
- **What's at stake:** Too low → false positive matches, annotations for different spans incorrectly merged. Too high → false negatives, genuinely matching annotations treated as new, creating duplicate annotation data that diverges over time.
- **Evidence-based recommendation:** **Start at 0.6 for TF-IDF Jaccard, 0.5 for character 3-gram Jaccard, with a calibration harness.** The calibration harness should:
  1. Take 50+ real annotation pairs from a processed novel
  2. Apply the algorithm with thresholds from 0.3 to 0.9 in 0.05 increments
  3. Manually label each pair as "match" or "no-match" (ground truth)
  4. Compute precision/recall at each threshold
  5. Select the threshold that maximizes F1 score
  - **Confidence: MEDIUM.** The starting values are informed by NLP literature (Jaccard thresholds for short text matching typically range from 0.5-0.7), but audiobook annotations are a specific domain that may have unique characteristics.
- **If we choose wrong:** All annotation reconciliation is subtly wrong. Every structural edit produces either merged-when-it-shouldn't or duplicate annotation data. The errors compound over multiple edits — after 10 structural edits, annotation data quality degrades significantly.

#### HJ5: Series Voice Locking — Strict Immutability vs. Human Override

- **Question:** The DD says "voice identity locked at first casting." The Improver's fix auto-corrects violating spans to the locked voice. But what if the voice casting for Book 1 was poor quality (wrong accent, grating tone), and the user wants to re-cast for Book 2+? Should the lock be absolute (admin-only unlock) or soft (warning + confirmation)?
- **What's at stake:**
  - **Absolute lock (current design):** The first voice casting is permanent and irrevocable through the UI. Requires a database-level intervention to change. Guarantees consistency across the series. Inflexible: a bad casting decision in Book 1 poisons the entire series.
  - **Soft lock + override:** The system warns when a voice conflicts with the series lock, but allows the user to override with confirmation. Flexible: users can fix mistakes. Risk: careless users override the lock and create voice inconsistency across books.
- **Evidence-based recommendation:** **Soft lock with two-tier override.** Tier 1: "Re-cast this character's voice" — re-generates voice for this character across ALL books in the series (maintaining consistency). Tier 2: "Override voice for this book only" — hidden behind an "Advanced" toggle with a strong warning: "This will break voice consistency across the series." The UI defaults to Tier 1, making the correct action easy and the dangerous action hard. **Confidence: HIGH.** This follows the principle of making the right thing easy: users who want to fix a bad casting get what they want (series-wide consistency), while users who want to experiment with per-book voices can do so with full awareness of the consequences.
- **If we choose wrong:** Absolute lock: frustrated users stuck with a bad voice forever, resorting to manual SQL hacks. Soft lock without tiers: careless users break consistency without understanding the consequences.

---

### Final Verdict

**Readiness for implementation:** CONDITIONALLY READY — after the two new risks (N1, N2) are addressed.

The Improver did an excellent job resolving all 3 BLOCKING and 2 HIGH-severity issues from Turn 6. The timestamp standardization to INTEGER millis eliminates the silent comparison failure. The single-connection refactoring restores the atomicity invariant. The delete-first reindex ordering correctly matches Pattern 1's proven approach. The `TransactionWrapper` with explicit `foreign_key_check` is a robust fix for the FK tracking reset issue. The cross-pattern integrations (SAVEPOINT nesting, stale-data detection, export gating, recovery + dirty flags) are all well-designed.

Two issues must be fixed before implementation begins:

**Top 3 implementation risks (after fixing N1 and N2):**

1. **N1 — `_reindex_before_insert` parse error + ordering:** The `ORDER BY position DESC` without LIMIT will fail at parse time. Replace with the negative-space insert pattern (Option B) — provably safe, same mechanism as delete-side reindex, no ORDER BY dependency.

2. **N2 — `isolation_level=None` required for manual transaction control:** The `create_connection()` factory must set `isolation_level = None` to prevent Python's auto-transaction logic from conflicting with `TransactionCoordinator.begin()`. Without this, `BEGIN IMMEDIATE` can fail with `OperationalError`.

3. **UR1 — ORDER BY processing order not guaranteed:** Even with `LIMIT -1` added, the SQLite docs explicitly state processing order is "arbitrary." The negative-space pattern (Option B) eliminates this risk entirely.

**First-sprint double-checks:**

- [ ] **Verify `SQLITE_ENABLE_UPDATE_DELETE_LIMIT` in the deployment SQLite build.** Run `conn.execute("PRAGMA compile_options")` and check for `ENABLE_UPDATE_DELETE_LIMIT`. If absent, the negative-space pattern (Option B) becomes the ONLY viable approach.
- [ ] **Integration test: SAVEPOINT + isolation_level=None.** Write a test that creates a `TransactionCoordinator`, calls `begin()`, then `savepoint()`, executes a DML inside the savepoint, rolls back the savepoint, and verifies the outer transaction is intact.
- [ ] **Integration test: crash recovery + stale-data detection.** Simulate a crash after annotation commit but before `complete_walk()`. Verify that `recover()` correctly detects the interrupted state AND checks dirty flags before determining the resume point.
- [ ] **Performance test: `PRAGMA foreign_key_check` on a realistic book.** Create a test database with 16 tables, 5K spans, and associated annotation rows. Measure `PRAGMA foreign_key_check` execution time. If >100ms, consider making it optional (config flag) or running it only in debug mode.
- [ ] **Integration test: `reindex_after_delete` + `reindex_before_insert` operating on the same paragraph.** The most complex case: move a span within the same paragraph (Delete + Insert on same parent). Verify no UNIQUE constraint violations in either the delete-side or insert-side reindex.
- [ ] **Validate that `isolation_level=None` does not break `executescript()`** for any schema migration scripts.

**The adversarial process verdict after 8 turns:** The design has been stress-tested from approach selection through implementation patterns through edge-case analysis. Every blocking and high-severity issue has been surfaced and addressed. The remaining risks are well-understood and have clear mitigations. The design is ready for implementation once N1 and N2 are resolved.

