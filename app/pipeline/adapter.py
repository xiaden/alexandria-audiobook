"""Storage adapter interface for the audiobook pipeline.

Provides:
- ``PipelineStorage`` — abstract base class defining the storage contract
- ``SQLiteAdapter`` — on-disk SQLite implementation (WAL mode, FK enforcement)
- ``InMemorySQLiteAdapter`` — in-memory SQLite for testing (same schema, no disk)

Concurrency: both adapters connect with ``isolation_level=None`` (explicit
autocommit) and serialize writes through the owner-thread ``transaction()``
context manager (``BEGIN IMMEDIATE`` + explicit COMMIT/ROLLBACK); writes from
a non-owner thread — or a ``BEGIN IMMEDIATE`` that times out under contention —
raise ``ConcurrentTransactionError``, mapped to HTTP 503 + ``Retry-After``.

Garbage collection: expired artifacts are deleted by ``gc_expired_artifacts``
(per-adapter entry point wrapping the module-level ``_gc_sweep``), which
consults the snapshot-reference union built by ``_snapshot_referenced_run_dirs``
so referenced runs are never collected, and re-derives manifests for surviving
runs via ``_rebuild_manifests``.  The scheduler (``start_gc_scheduler`` /
``stop_gc_scheduler``) runs ``_gc_sweep`` on an interval.  Retention and
scheduler behavior are env-tunable: ``JOB_RETENTION_DAYS``,
``CHUNK_RETENTION_DAYS``, ``GC_INTERVAL_HOURS``, and ``PIPELINE_GC_SCHEDULER``
(``"0"`` disables the scheduler).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager

from app.pipeline.schema import create_schema


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------


class ConcurrentTransactionError(RuntimeError):
    """Raised when a write is attempted from a thread that does not own the
    open transaction, or when ``BEGIN IMMEDIATE`` times out under contention.

    The API layer maps this to HTTP 503 + ``Retry-After`` so a concurrent
    walk/render writer can back off and retry its idempotent write phase.
    """


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class PipelineStorage(ABC):
    """Abstract storage interface for the pipeline.

    All concrete adapters must implement these methods.  The interface is
    deliberately narrow: callers interact through ``execute_*`` helpers
    rather than raw cursors so that the backend can be swapped without
    touching call-sites.
    """

    @abstractmethod
    def init_db(self) -> None:
        """Create the schema (tables + views) if they do not exist."""

    @abstractmethod
    def get_connection(self) -> sqlite3.Connection:
        """Return the underlying ``sqlite3.Connection``."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection and release resources."""

    @abstractmethod
    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT and return rows as a list of dicts."""

    @abstractmethod
    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT and return ``lastrowid``."""

    @abstractmethod
    def execute_update(self, sql: str, params: tuple = ()) -> int:
        """Execute an UPDATE and return ``rowcount``."""

    @abstractmethod
    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        """Execute a DELETE and return ``rowcount``."""

    @abstractmethod
    def get_walk_overrides(self, book_id: str) -> list[dict]:
        """Return all ``walk_override`` rows for *book_id* as a list of dicts
        ``{"book_id", "walk_name", "key", "value_json"}`` (``value_json`` is
        the raw TEXT string — callers decide whether to ``json.loads``).
        A missing ``walk_override`` table degrades gracefully to ``[]``.
        """

    @abstractmethod
    def upsert_walk_override(
        self, book_id: str, walk_name: str, key: str, value_json: str
    ) -> None:
        """Insert or update the ``walk_override`` row for
        ``(book_id, walk_name, key)``; ``value_json`` is JSON-encoded."""

    @abstractmethod
    def delete_walk_override(self, book_id: str, walk_name: str, key: str) -> None:
        """Delete the ``walk_override`` row (no-op if absent)."""

    @abstractmethod
    def list_project_snapshots(self, book_id: str | None = None) -> list[dict]:
        """Return ``project_snapshot`` rows, newest first.

        Each dict is ``{"name", "book_id", "snapshot_json", "created_ms"}``
        with ``snapshot_json`` as the raw TEXT string — callers decide
        whether to ``json.loads``.  When *book_id* is given, only that
        book's snapshots are returned.  Ordering is ``created_ms DESC``
        with ``name ASC`` as a deterministic tiebreak.
        """

    @abstractmethod
    def get_project_snapshot(self, name: str) -> dict | None:
        """Return the ``project_snapshot`` row for *name*, or ``None``."""

    @abstractmethod
    def create_project_snapshot(
        self, name: str, book_id: str, snapshot_json: str, created_ms: int
    ) -> None:
        """Insert a new ``project_snapshot`` row.

        Raises ``sqlite3.IntegrityError`` when *name* already exists (PK).
        """

    @abstractmethod
    def delete_project_snapshot(self, name: str) -> bool:
        """Delete the ``project_snapshot`` row for *name*.

        Returns ``True`` when a row was removed, ``False`` for an unknown
        name.
        """

    @abstractmethod
    def rename_project_snapshot(self, name: str, new_name: str) -> bool:
        """Rename a ``project_snapshot`` row from *name* to *new_name*.

        Returns whether a row was updated.  Raises
        ``sqlite3.IntegrityError`` when *new_name* collides with an
        existing name (PK UNIQUE constraint).
        """


# ---------------------------------------------------------------------------
# Startup reconciliation (contract rule #5)
# ---------------------------------------------------------------------------

# A running row whose last activity is at least this old is treated as stale
# (leftover from a dead process) and flipped to ``interrupted`` on startup.
# A grace period keeps the flip conservative: rows that started or heartbeated
# within the window are never touched (no false positives on live work).
_STALE_RUN_GRACE_MS = 5 * 60 * 1000  # 5 minutes

# Terminal cause stamped on rows flipped by startup reconciliation.
_INTERRUPTED_ERROR = "interrupted by process restart"


def _reconcile_stale_runs(conn: sqlite3.Connection) -> dict[str, int]:
    """Flip stale running rows to ``interrupted`` in one pass (startup-only).

    Exactly one UPDATE per table (contract rule #5 — no on-read sweeper, no
    periodic reaper).  A row is stale when its last activity predates the
    cutoff (``now - _STALE_RUN_GRACE_MS``):

    - ``render_job``: ``started_ms < cutoff`` (no heartbeat column exists)
    - ``walk_run``: ``created_ms < cutoff`` AND no recent heartbeat
      (``heartbeat_ms IS NULL OR heartbeat_ms < cutoff``)

    Terminal rows (completed/failed/cancelled/interrupted/pending) are never
    touched — the predicate matches ``status = 'running'`` only.

    Returns per-table counts of flipped rows, e.g. ``{"render_job": 2,
    "walk_run": 1}``.  Safe to call on an empty database.
    """
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - _STALE_RUN_GRACE_MS
    was_in_transaction = conn.in_transaction
    cursor = conn.execute(
        "UPDATE render_job "
        "SET status = 'interrupted', error = ?, finished_ms = ? "
        "WHERE status = 'running' AND started_ms < ?",
        (_INTERRUPTED_ERROR, now_ms, cutoff),
    )
    render_count = cursor.rowcount
    cursor = conn.execute(
        "UPDATE walk_run "
        "SET status = 'interrupted', error = ?, finished_ms = ? "
        "WHERE status = 'running' AND created_ms < ? "
        "AND (heartbeat_ms IS NULL OR heartbeat_ms < ?)",
        (_INTERRUPTED_ERROR, now_ms, cutoff, cutoff),
    )
    walk_count = cursor.rowcount
    if not was_in_transaction:
        conn.commit()
    return {"render_job": render_count, "walk_run": walk_count}


# ---------------------------------------------------------------------------
# Startup manifest rebuild (contract rule #3 — rows = truth, manifest = derived)
# ---------------------------------------------------------------------------

# Terminal cause stamped on a completed row whose run dir is gone.  The row is
# marked ``expired`` — the schema-valid GC terminal state for a job whose
# artifacts no longer exist (DD decision #10: rows tombstoned as evicted/
# expired when their artifacts are removed).
_ARTIFACT_MISSING_ERROR = "artifact missing: run dir not found"


def _manifest_is_stale(run_dir: str, row: sqlite3.Row, chunk_paths: list[str]) -> bool:
    """True when ``manifest.json`` is missing or diverges from the rows.

    Comparison mirrors the phase-1 manifest shape (job/book/mode/chunk_count/
    relative chunk paths/status).  ``created_ms`` is intentionally excluded so
    a cache that already matches the rows is never rewritten.
    """
    manifest_path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return True
    try:
        with open(manifest_path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        return True
    if not isinstance(existing, dict):
        return True
    expected = {
        "job_id": row["job_id"],
        "book_id": row["book_id"],
        "mode": row["mode"],
        "chunk_count": len(chunk_paths),
        "chunks": [
            {"idx": i, "wav_path": os.path.relpath(path, run_dir)}
            for i, path in enumerate(chunk_paths)
        ],
        "status": "completed",
    }
    return any(existing.get(key) != value for key, value in expected.items())


def _rebuild_manifests(conn: sqlite3.Connection, render_root: str) -> dict[str, int]:
    """Rebuild the derived ``manifest.json`` for completed renders (startup).

    Contract rule #3 — rows are the truth, ``manifest.json`` is a derived
    cache regenerated here at startup.  Regeneration is DRIVEN BY ROWS; the
    filesystem is never scanned as authority.  For every ``completed`` row:

    - the run dir is located from the row (``output_dir`` when set, else
      derived as ``RENDER_ROOT/book-{book_id}/{job_id}/`` — the same rule the
      renderer uses);
    - a run dir that no longer exists flags the job artifact-missing: the
      row is marked ``expired`` with ``_ARTIFACT_MISSING_ERROR``;
    - otherwise ``manifest.json`` is (re)written when missing or stale, in
      the exact phase-1 format and atomicity via
      ``tts_integration._write_manifest`` (reused, not duplicated).
      Individual mode takes the authoritative chunk list from the
      ``render_chunk`` rows with status ``done``; batch mode enumerates the
      ``*.wav`` files in the run dir (batch renders have no per-chunk rows).

    Runs in the same transaction style as ``_reconcile_stale_runs``: UPDATEs
    join an open transaction when one is owned by the caller (owner-thread
    discipline enforced by the adapter method) and otherwise auto-commit.

    Returns ``{"manifests_rebuilt": int, "jobs_marked_expired": int}``.
    Safe to call on an empty database.
    """
    # Deferred import: tts_integration imports PipelineStorage from this
    # module at load time, so a module-level import here would be circular.
    from app.pipeline.tts_integration import _write_manifest

    conn.row_factory = sqlite3.Row
    now_ms = int(time.time() * 1000)
    was_in_transaction = conn.in_transaction
    rows = conn.execute(
        "SELECT job_id, book_id, mode, output_dir FROM render_job "
        "WHERE status = 'completed'"
    ).fetchall()
    rebuilt = 0
    expired = 0
    for row in rows:
        run_dir = row["output_dir"] or os.path.join(
            render_root, f"book-{row['book_id']}", row["job_id"]
        )
        if not os.path.isdir(run_dir):
            conn.execute(
                "UPDATE render_job SET status = 'expired', error = ?, "
                "finished_ms = ? WHERE job_id = ?",
                (_ARTIFACT_MISSING_ERROR, now_ms, row["job_id"]),
            )
            expired += 1
            continue
        if row["mode"] == "individual":
            chunk_paths = [
                chunk["wav_path"]
                for chunk in conn.execute(
                    "SELECT wav_path FROM render_chunk "
                    "WHERE job_id = ? AND status = 'done' ORDER BY idx",
                    (row["job_id"],),
                ).fetchall()
            ]
        else:
            chunk_paths = [
                os.path.join(run_dir, name)
                for name in sorted(os.listdir(run_dir))
                if name.endswith(".wav")
                and os.path.isfile(os.path.join(run_dir, name))
            ]
        if _manifest_is_stale(run_dir, row, chunk_paths):
            _write_manifest(
                run_dir,
                job_id=row["job_id"],
                book_id=row["book_id"],
                mode=row["mode"],
                chunk_paths=chunk_paths,
                status="completed",
            )
            rebuilt += 1
    if not was_in_transaction:
        conn.commit()
    return {"manifests_rebuilt": rebuilt, "jobs_marked_expired": expired}


# ---------------------------------------------------------------------------
# Tombstoning GC (contract rule #12) — retention >= 7d, hourly sweep, never
# on the hot request path; snapshot references join the eligibility union.
# ---------------------------------------------------------------------------

# Retention defaults lock open item #1 of DD-universal-upgrade: both job and
# chunk retention default to 7 days and are env-tunable at call time (float
# days, so sub-day overrides work in tests).  The effective retention for a
# job is the LONGER of the two — a job and its chunk rows are swept as one
# unit and must never be split (files must not disappear while ``done`` chunk
# rows still reference them).
_GC_DEFAULT_RETENTION_DAYS = 7.0
_MS_PER_DAY = 24 * 3600 * 1000

# Terminal cause stamped on a swept job's row.  Chunks are tombstoned to
# ``evicted``; the job row is tombstoned to ``expired`` — the schema-valid
# GC terminal state (DD decision #10).
_GC_EXPIRED_ERROR = "expired by GC: artifacts removed after retention"


# ---------------------------------------------------------------------------
# GC eligibility union (Plan C phase-3): project_snapshot artifact refs
# ---------------------------------------------------------------------------


def _walk_json_strings(value: object, out: set[str]) -> None:
    """Collect every string value anywhere in a JSON document (recursive)."""
    if isinstance(value, dict):
        for v in value.values():
            _walk_json_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _walk_json_strings(v, out)
    elif isinstance(value, str):
        out.add(value)


def _snapshot_referenced_run_dirs(
    conn: sqlite3.Connection, render_root: str
) -> set[str]:
    """Eligibility union: run dirs referenced by any ``project_snapshot`` row.

    Plan I (snapshot projects) is not yet implemented, so the snapshot
    manifest schema is UNKNOWN — parsing is deliberately defensive:
    ``snapshot_json`` is parsed with ``json.loads`` (rows that fail to parse
    are skipped — a broken snapshot must never pin artifacts forever), and
    every string value anywhere in the document is checked for artifact-path
    shape.  A string counts as a reference when it names a path at/under
    ``render_root`` — matched by raw substring (relative and URI-embedded
    forms such as ``file://…``), by absolute-path containment (snapshot
    stores an absolute path while ``RENDER_ROOT`` is relative), or by
    normalized path containment (a ``scheme://`` prefix is stripped before
    resolution).  Returning normalized absolute paths.

    With an empty ``project_snapshot`` table (the expected state while Plan I
    is pending) the union is trivially empty and the sweep is unchanged.
    """
    render_root_abs = os.path.abspath(render_root)
    root_prefix = render_root_abs + os.sep
    references: set[str] = set()
    for row in conn.execute("SELECT snapshot_json FROM project_snapshot").fetchall():
        try:
            document = json.loads(row["snapshot_json"])
        except (TypeError, ValueError):
            continue  # defensive: unparseable snapshot never pins artifacts
        strings: set[str] = set()
        _walk_json_strings(document, strings)
        for value in strings:
            normalized = value.split("://", 1)[1] if "://" in value else value
            abs_value = os.path.abspath(normalized)
            if (
                render_root in value
                or render_root_abs in value
                or abs_value == render_root_abs
                or abs_value.startswith(root_prefix)
            ):
                # Substring-level false positives (e.g. a ``render_root``
                # prefix inside an unrelated token) are filtered later by
                # ``_run_dir_is_referenced`` — the conservative direction
                # (never delete a referenced dir) is safe.
                references.add(abs_value)
    return references


def _run_dir_is_referenced(run_dir: str, references: set[str]) -> bool:
    """True when a snapshot reference is the run dir itself or lies inside it."""
    run_dir_abs = os.path.abspath(run_dir)
    prefix = run_dir_abs + os.sep
    return any(ref == run_dir_abs or ref.startswith(prefix) for ref in references)


# ---------------------------------------------------------------------------
# GC sweep
# ---------------------------------------------------------------------------


def _gc_retention_days(env_name: str, override: float | None) -> float:
    """Resolve a retention in days: explicit override > env (float) > default.

    Env values are parsed as float so tests can pin sub-day retentions (e.g.
    ``JOB_RETENTION_DAYS=0.001``); an unset or malformed value falls back to
    the 7-day default rather than crashing the sweep.
    """
    if override is not None:
        return float(override)
    raw = os.environ.get(env_name, "")
    try:
        return float(raw) if raw else _GC_DEFAULT_RETENTION_DAYS
    except ValueError:
        return _GC_DEFAULT_RETENTION_DAYS


def _gc_sweep(
    conn: sqlite3.Connection,
    render_root: str,
    *,
    job_retention_days: float | None = None,
    chunk_retention_days: float | None = None,
) -> dict[str, int | list[str]]:
    """Tombstoning GC sweep — expired run dirs + rows reclaimed in one pass.

    Contract rule #12: retention defaults to 7 days for both jobs and chunks
    (env-tunable at call time via ``JOB_RETENTION_DAYS``/``CHUNK_RETENTION_DAYS``,
    float days); the sweep is scheduled hourly off the hot request path (see
    ``start_gc_scheduler``); ``project_snapshot`` artifact references join the
    eligibility union (see ``_snapshot_referenced_run_dirs``); rows are NEVER
    time-deleted — tombstoned only (chunks ``evicted``, job ``expired``).

    Candidates are ``completed`` render_job rows with ``finished_ms`` older
    than the effective retention cutoff.  The effective retention is the
    LONGER of the job/chunk retentions — the run dir contains the chunk
    files, so the unit is swept as one and both retentions must have
    elapsed.  Non-completed rows are never candidates; rows already
    ``expired`` are skipped.

    Crash-safety ordering (documented decision): run dirs are deleted FIRST,
    then rows are tombstoned.  A crash between the two leaves a ``completed``
    row with a missing run dir — exactly the state ``_rebuild_manifests()``
    converts to ``expired`` at the next startup (the phase-2 safety net), so
    the window self-heals.  The reverse order would orphan run dirs that no
    row ever references again (rows are the truth; nothing would reclaim
    them).  ``run_dirs_deleted`` counts swept run dirs, whether or not the
    dir still existed at sweep time.

    Trust boundary: GC deletes the row-recorded ``output_dir`` tree.  That
    path originates from the RenderRequest body (Plan B contract), so any
    client with render access can influence what is removed after retention
    — the deletion path is not further validated.  The ``project_snapshot``
    reference union protects snapshotted artifacts from collection.

    Runs in the same transaction style as ``_reconcile_stale_runs``: UPDATEs
    join an open transaction when one is owned by the caller (owner-thread
    discipline enforced by the adapter method) and otherwise auto-commit.

    Returns ``{"run_dirs_deleted": int, "chunks_evicted": int,
    "jobs_expired": int, "skipped_snapshot_referenced": list[str]}``.
    Safe to call on an empty database.
    """
    job_days = _gc_retention_days("JOB_RETENTION_DAYS", job_retention_days)
    chunk_days = _gc_retention_days("CHUNK_RETENTION_DAYS", chunk_retention_days)
    now_ms = int(time.time() * 1000)
    cutoff_ms = min(
        now_ms - int(job_days * _MS_PER_DAY),
        now_ms - int(chunk_days * _MS_PER_DAY),
    )

    conn.row_factory = sqlite3.Row
    references = _snapshot_referenced_run_dirs(conn, render_root)

    candidates: list[tuple[sqlite3.Row, str]] = []
    skipped: list[str] = []
    for row in conn.execute(
        "SELECT job_id, book_id, mode, output_dir FROM render_job "
        "WHERE status = 'completed' AND finished_ms IS NOT NULL AND finished_ms < ?",
        (cutoff_ms,),
    ).fetchall():
        run_dir = row["output_dir"] or os.path.join(
            render_root, f"book-{row['book_id']}", row["job_id"]
        )
        if _run_dir_is_referenced(run_dir, references):
            skipped.append(row["job_id"])
        else:
            candidates.append((row, run_dir))

    # Phase 1 — destructive step first (crash-safety rationale in docstring).
    for _row, run_dir in candidates:
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)

    # Phase 2 — row tombstones in one logical pass (files first, rows after).
    was_in_transaction = conn.in_transaction
    chunks_evicted = 0
    jobs_expired = 0
    for row, _run_dir in candidates:
        cursor = conn.execute(
            "UPDATE render_chunk SET status = 'evicted' "
            "WHERE job_id = ? AND status != 'evicted'",
            (row["job_id"],),
        )
        chunks_evicted += cursor.rowcount
        cursor = conn.execute(
            "UPDATE render_job SET status = 'expired', error = ?, finished_ms = ? "
            "WHERE job_id = ? AND status = 'completed'",
            (_GC_EXPIRED_ERROR, now_ms, row["job_id"]),
        )
        jobs_expired += cursor.rowcount
    if not was_in_transaction:
        conn.commit()

    return {
        "run_dirs_deleted": len(candidates),
        "chunks_evicted": chunks_evicted,
        "jobs_expired": jobs_expired,
        "skipped_snapshot_referenced": skipped,
    }


# ---------------------------------------------------------------------------
# GC scheduler — hourly daemon thread, never on the hot request path
# ---------------------------------------------------------------------------

# The scheduler is a singleton daemon thread.  It is started EXPLICITLY via
# ``start_gc_scheduler()`` — never at module import time — so importing
# app.pipeline.adapter (or app.app, which wires it into the FastAPI
# lifespan) can never spawn threads under pytest.  The existing tests build
# ``TestClient(app)`` without entering the context manager, so the lifespan
# (and therefore this thread) never runs during ``pytest tests/pipeline``.
_GC_THREAD_NAME = "alexandria-gc-scheduler"
_gc_thread: threading.Thread | None = None
_gc_stop_event: threading.Event | None = None
_gc_start_lock = threading.Lock()


def _gc_interval_seconds() -> float:
    """Resolve the sweep interval from ``GC_INTERVAL_HOURS`` (default 1h)."""
    try:
        hours = float(os.environ.get("GC_INTERVAL_HOURS", "1"))
    except ValueError:
        hours = 1.0
    return hours * 3600


def _gc_scheduler_loop(stop_event: threading.Event, interval_seconds: float) -> None:
    """Run the sweep every *interval_seconds* until *stop_event* is set.

    Sleeps BEFORE the first sweep: starting the scheduler is never itself a
    sweep moment, and the first pass happens one full interval later.  Each
    sweep opens its own short-lived adapter over ``PIPELINE_DB_PATH`` (WAL
    allows the concurrent API writer) and reads ``RENDER_ROOT`` at call time
    via ``get_render_root()`` — the same single source of truth as
    ``rebuild_manifests``.  Exceptions are swallowed per-iteration so one bad
    sweep cannot kill the loop.
    """
    while not stop_event.wait(interval_seconds):
        try:
            # Deferred import: tts_integration imports PipelineStorage from
            # this module at load time (same circular-import note as
            # _rebuild_manifests).
            from app.pipeline.tts_integration import get_render_root

            adapter = SQLiteAdapter(
                os.environ.get("PIPELINE_DB_PATH", "./data/pipeline.db")
            )
            try:
                adapter.init_db()
                adapter.gc_expired_artifacts(get_render_root())
            finally:
                adapter.close()
        except Exception as exc:  # noqa: BLE001 — the loop must survive a bad sweep
            print(f"warning: GC sweep failed: {exc}", file=sys.stderr)


def start_gc_scheduler() -> None:
    """Start the hourly GC daemon thread (idempotent, env-guarded).

    Never runs on the hot request path: the sweep is invoked only from this
    background thread, whose first pass is deferred one full interval (see
    ``_gc_scheduler_loop``) and which is daemonized so it can never block
    process exit.  No-op when ``PIPELINE_GC_SCHEDULER`` is set to ``"0"``
    (explicit opt-out, e.g. tests) or when already running.
    """
    global _gc_thread, _gc_stop_event
    if os.environ.get("PIPELINE_GC_SCHEDULER", "1") != "1":
        return
    with _gc_start_lock:
        if _gc_thread is not None and _gc_thread.is_alive():
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_gc_scheduler_loop,
            args=(stop_event, _gc_interval_seconds()),
            name=_GC_THREAD_NAME,
            daemon=True,
        )
        _gc_stop_event = stop_event
        _gc_thread = thread
        thread.start()


def stop_gc_scheduler() -> None:
    """Signal the GC thread to stop and join it (idempotent).

    Safe to call when the scheduler was never started (e.g. under pytest) —
    it simply returns.
    """
    global _gc_thread, _gc_stop_event
    with _gc_start_lock:
        thread = _gc_thread
        stop_event = _gc_stop_event
        _gc_thread = None
        _gc_stop_event = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# SQLite on-disk adapter
# ---------------------------------------------------------------------------


class SQLiteAdapter(PipelineStorage):
    """On-disk SQLite adapter with WAL journaling and FK enforcement.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  Parent directories
        are created automatically.  Defaults to ``./data/pipeline.db``.
    """

    def __init__(self, db_path: str = "./data/pipeline.db") -> None:
        self._db_path = db_path
        # Ensure parent directory exists
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # isolation_level=None => explicit autocommit: transactions are only
        # opened through transaction() (BEGIN IMMEDIATE), so a multi-statement
        # write can never interleave with another thread's auto-commit.
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        # WAL mode for concurrent read access
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Enforce foreign keys
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Owner-thread bookkeeping for the transaction() guard.
        self._txn_owner: int | None = None
        self._txn_depth = 0

    # -- PipelineStorage interface ------------------------------------------

    def init_db(self) -> None:
        create_schema(self._conn)
        # Busy timeout: wait up to 5s for a locked database instead of
        # failing immediately with "database is locked". Set at startup,
        # after the WAL/foreign_keys PRAGMAs issued in __init__, so the
        # connection is armed before any transaction() use.
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # -- transaction support ------------------------------------------------

    def _ensure_owner_thread(self) -> None:
        """Reject writes from threads that do not own the open transaction."""
        owner = self._txn_owner
        if owner is not None and owner != threading.get_ident():
            raise ConcurrentTransactionError(
                "write from thread %s while transaction is owned by thread %s"
                % (threading.get_ident(), owner)
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the wrapped block inside a ``BEGIN IMMEDIATE`` transaction.

        The first entry records the owning thread and issues
        ``BEGIN IMMEDIATE``; a clean exit COMMITs and an exception ROLLBACKs.
        Nested re-entry from the same thread joins the outer transaction (the
        inner exit neither commits nor rolls back).  Writes issued from any
        other thread while a transaction is open raise
        ``ConcurrentTransactionError``.
        """
        tid = threading.get_ident()
        if self._txn_depth == 0:
            self._txn_owner = tid
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # The write lock was not acquired within busy_timeout — clear
                # the owner so the guard cannot reject other threads for a
                # transaction that never opened, and surface the contention
                # as the contracted ConcurrentTransactionError.
                self._txn_owner = None
                raise ConcurrentTransactionError(
                    "BEGIN IMMEDIATE timed out under contention"
                ) from exc
            except BaseException:
                self._txn_owner = None
                raise
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.rollback()
                finally:
                    self._txn_owner = None
            raise
        else:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.commit()
                finally:
                    self._txn_owner = None

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def get_walk_overrides(self, book_id: str) -> list[dict]:
        """Return all ``walk_override`` rows for *book_id*.

        Each dict is ``{"book_id", "walk_name", "key", "value_json"}`` with
        ``value_json`` as the raw TEXT string — callers decide whether to
        ``json.loads``.  A missing ``walk_override`` table (uninitialized
        database) degrades gracefully to ``[]`` so config resolution never
        crashes on an older schema.
        """
        try:
            return self.execute_query(
                "SELECT book_id, walk_name, key, value_json"
                " FROM walk_override WHERE book_id = ?",
                (book_id,),
            )
        except sqlite3.OperationalError:
            return []

    def upsert_walk_override(
        self, book_id: str, walk_name: str, key: str, value_json: str
    ) -> None:
        """Insert or update the ``walk_override`` row keyed by the composite
        PK ``(book_id, walk_name, key)``.

        ``value_json`` is a JSON-encoded string.  ``ON CONFLICT ... DO
        UPDATE`` keeps the existing row (true upsert semantics — the PK row
        count never grows).  Composes ``execute_insert`` so it joins an open
        ``transaction()`` when one is active and auto-commits otherwise.
        """
        self.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(book_id, walk_name, key)"
            " DO UPDATE SET value_json = excluded.value_json",
            (book_id, walk_name, key, value_json),
        )

    def delete_walk_override(self, book_id: str, walk_name: str, key: str) -> None:
        """Delete the ``walk_override`` row for ``(book_id, walk_name, key)``.

        Deleting an absent row is a no-op (``execute_delete`` returns 0).
        """
        self.execute_delete(
            "DELETE FROM walk_override"
            " WHERE book_id = ? AND walk_name = ? AND key = ?",
            (book_id, walk_name, key),
        )

    def list_project_snapshots(self, book_id: str | None = None) -> list[dict]:
        """Return ``project_snapshot`` rows for the adapter, newest first.

        Each dict is ``{"name", "book_id", "snapshot_json", "created_ms"}``
        with ``snapshot_json`` as the raw TEXT string — callers decide
        whether to ``json.loads``.  An optional *book_id* restricts the
        result to one book.  Ordering is ``created_ms DESC`` with ``name
        ASC`` as a deterministic tiebreak (Plan I contract: newest-first).
        """
        if book_id is None:
            return self.execute_query(
                "SELECT name, book_id, snapshot_json, created_ms"
                " FROM project_snapshot ORDER BY created_ms DESC, name ASC"
            )
        return self.execute_query(
            "SELECT name, book_id, snapshot_json, created_ms"
            " FROM project_snapshot WHERE book_id = ?"
            " ORDER BY created_ms DESC, name ASC",
            (book_id,),
        )

    def get_project_snapshot(self, name: str) -> dict | None:
        """Return the ``project_snapshot`` row for *name*, or ``None``."""
        rows = self.execute_query(
            "SELECT name, book_id, snapshot_json, created_ms"
            " FROM project_snapshot WHERE name = ?",
            (name,),
        )
        return rows[0] if rows else None

    def create_project_snapshot(
        self, name: str, book_id: str, snapshot_json: str, created_ms: int
    ) -> None:
        """Insert a new ``project_snapshot`` row.

        Raises ``sqlite3.IntegrityError`` when *name* already exists (PK).
        Composes ``execute_insert`` so it joins an open ``transaction()``
        when one is active and auto-commits otherwise.
        """
        self.execute_insert(
            "INSERT INTO project_snapshot (name, book_id, snapshot_json, created_ms)"
            " VALUES (?, ?, ?, ?)",
            (name, book_id, snapshot_json, created_ms),
        )

    def delete_project_snapshot(self, name: str) -> bool:
        """Delete the ``project_snapshot`` row for *name*.

        Returns ``True`` when a row was removed, ``False`` for an unknown
        name.
        """
        return (
            self.execute_delete(
                "DELETE FROM project_snapshot WHERE name = ?", (name,)
            )
            > 0
        )

    def rename_project_snapshot(self, name: str, new_name: str) -> bool:
        """Rename a ``project_snapshot`` row from *name* to *new_name*.

        Returns whether a row was updated.  Raises
        ``sqlite3.IntegrityError`` when *new_name* collides with an
        existing name (PK UNIQUE constraint).
        """
        return (
            self.execute_update(
                "UPDATE project_snapshot SET name = ? WHERE name = ?",
                (new_name, name),
            )
            > 0
        )

    def reconcile_stale_runs(self) -> dict[str, int]:
        """Startup-only: flip stale running rows to ``interrupted`` (one pass).

        Contract rule #5 — runs exactly once at startup, before the API
        accepts requests.  Returns per-table counts of flipped rows, e.g.
        ``{"render_job": 2, "walk_run": 1}``.
        """
        self._ensure_owner_thread()
        return _reconcile_stale_runs(self._conn)

    def rebuild_manifests(self, render_root: str) -> dict[str, int]:
        """Startup-only: rebuild derived manifests for completed renders.

        Contract rule #3 — rows stay the truth; ``manifest.json`` is a derived
        cache regenerated here at startup (Plan C phase 2).  A completed row
        whose run dir is gone is marked ``expired`` (artifact missing).
        Returns ``{"manifests_rebuilt": int, "jobs_marked_expired": int}``.
        """
        self._ensure_owner_thread()
        return _rebuild_manifests(self._conn, render_root)

    def gc_expired_artifacts(
        self,
        render_root: str,
        *,
        job_retention_days: float | None = None,
        chunk_retention_days: float | None = None,
    ) -> dict[str, int | list[str]]:
        """Tombstoning GC sweep — expired run dirs + rows reclaimed in one pass.

        Contract rule #12 (Plan C phase 3): retention >= 7 days by default
        (env-tunable ``JOB_RETENTION_DAYS``/``CHUNK_RETENTION_DAYS``, float
        days, resolved at call time), scheduled hourly off the hot request
        path, ``project_snapshot`` refs join the eligibility union, and rows
        are never time-deleted (chunks ``evicted``, job ``expired``).
        Returns ``{"run_dirs_deleted": int, "chunks_evicted": int,
        "jobs_expired": int, "skipped_snapshot_referenced": list[str]}``.
        """
        self._ensure_owner_thread()
        return _gc_sweep(
            self._conn,
            render_root,
            job_retention_days=job_retention_days,
            chunk_retention_days=chunk_retention_days,
        )


# ---------------------------------------------------------------------------
# In-memory adapter (for testing)
# ---------------------------------------------------------------------------


class InMemorySQLiteAdapter(PipelineStorage):
    """In-memory SQLite adapter for testing.

    Same schema and interface as ``SQLiteAdapter`` but uses ``:memory:``
    so no disk I/O occurs.  Ideal for unit tests.
    """

    def __init__(self) -> None:
        # isolation_level=None => explicit autocommit (see SQLiteAdapter).
        self._conn = sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Owner-thread bookkeeping for the transaction() guard.
        self._txn_owner: int | None = None
        self._txn_depth = 0

    # -- PipelineStorage interface ------------------------------------------

    def init_db(self) -> None:
        create_schema(self._conn)
        # Busy timeout: wait up to 5s for a locked database instead of
        # failing immediately with "database is locked". Set at startup,
        # after the foreign_keys PRAGMA issued in __init__, so the
        # connection is armed before any transaction() use.
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # -- transaction support ------------------------------------------------

    def _ensure_owner_thread(self) -> None:
        """Reject writes from threads that do not own the open transaction."""
        owner = self._txn_owner
        if owner is not None and owner != threading.get_ident():
            raise ConcurrentTransactionError(
                "write from thread %s while transaction is owned by thread %s"
                % (threading.get_ident(), owner)
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the wrapped block inside a ``BEGIN IMMEDIATE`` transaction.

        The first entry records the owning thread and issues
        ``BEGIN IMMEDIATE``; a clean exit COMMITs and an exception ROLLBACKs.
        Nested re-entry from the same thread joins the outer transaction (the
        inner exit neither commits nor rolls back).  Writes issued from any
        other thread while a transaction is open raise
        ``ConcurrentTransactionError``.
        """
        tid = threading.get_ident()
        if self._txn_depth == 0:
            self._txn_owner = tid
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # The write lock was not acquired within busy_timeout — clear
                # the owner so the guard cannot reject other threads for a
                # transaction that never opened, and surface the contention
                # as the contracted ConcurrentTransactionError.
                self._txn_owner = None
                raise ConcurrentTransactionError(
                    "BEGIN IMMEDIATE timed out under contention"
                ) from exc
            except BaseException:
                self._txn_owner = None
                raise
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.rollback()
                finally:
                    self._txn_owner = None
            raise
        else:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                try:
                    self._conn.commit()
                finally:
                    self._txn_owner = None

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        self._ensure_owner_thread()
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        if not was_in_transaction:
            self._conn.commit()
        return cursor.rowcount

    def get_walk_overrides(self, book_id: str) -> list[dict]:
        """Return all ``walk_override`` rows for *book_id*.

        Mirror of ``SQLiteAdapter.get_walk_overrides`` (same schema and
        interface).  ``value_json`` is returned as the raw TEXT string;
        a missing table degrades gracefully to ``[]``.
        """
        try:
            return self.execute_query(
                "SELECT book_id, walk_name, key, value_json"
                " FROM walk_override WHERE book_id = ?",
                (book_id,),
            )
        except sqlite3.OperationalError:
            return []

    def upsert_walk_override(
        self, book_id: str, walk_name: str, key: str, value_json: str
    ) -> None:
        """Insert or update the ``walk_override`` row keyed by the composite
        PK ``(book_id, walk_name, key)``.

        Mirror of ``SQLiteAdapter.upsert_walk_override`` (same schema and
        interface).  ``value_json`` is a JSON-encoded string.
        """
        self.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(book_id, walk_name, key)"
            " DO UPDATE SET value_json = excluded.value_json",
            (book_id, walk_name, key, value_json),
        )

    def delete_walk_override(self, book_id: str, walk_name: str, key: str) -> None:
        """Delete the ``walk_override`` row for ``(book_id, walk_name, key)``.

        Mirror of ``SQLiteAdapter.delete_walk_override`` (same schema and
        interface).  Deleting an absent row is a no-op.
        """
        self.execute_delete(
            "DELETE FROM walk_override"
            " WHERE book_id = ? AND walk_name = ? AND key = ?",
            (book_id, walk_name, key),
        )

    def list_project_snapshots(self, book_id: str | None = None) -> list[dict]:
        """Return ``project_snapshot`` rows, newest first.

        Mirror of ``SQLiteAdapter.list_project_snapshots`` (same schema and
        interface).  Optional *book_id* restricts the result to one book.
        """
        if book_id is None:
            return self.execute_query(
                "SELECT name, book_id, snapshot_json, created_ms"
                " FROM project_snapshot ORDER BY created_ms DESC, name ASC"
            )
        return self.execute_query(
            "SELECT name, book_id, snapshot_json, created_ms"
            " FROM project_snapshot WHERE book_id = ?"
            " ORDER BY created_ms DESC, name ASC",
            (book_id,),
        )

    def get_project_snapshot(self, name: str) -> dict | None:
        """Return the ``project_snapshot`` row for *name*, or ``None``.

        Mirror of ``SQLiteAdapter.get_project_snapshot``.
        """
        rows = self.execute_query(
            "SELECT name, book_id, snapshot_json, created_ms"
            " FROM project_snapshot WHERE name = ?",
            (name,),
        )
        return rows[0] if rows else None

    def create_project_snapshot(
        self, name: str, book_id: str, snapshot_json: str, created_ms: int
    ) -> None:
        """Insert a new ``project_snapshot`` row.

        Mirror of ``SQLiteAdapter.create_project_snapshot`` (same schema
        and interface).  Raises ``sqlite3.IntegrityError`` when *name*
        already exists (PK).
        """
        self.execute_insert(
            "INSERT INTO project_snapshot (name, book_id, snapshot_json, created_ms)"
            " VALUES (?, ?, ?, ?)",
            (name, book_id, snapshot_json, created_ms),
        )

    def delete_project_snapshot(self, name: str) -> bool:
        """Delete the ``project_snapshot`` row for *name*.

        Mirror of ``SQLiteAdapter.delete_project_snapshot``.  Returns
        ``True`` when a row was removed, ``False`` for an unknown name.
        """
        return (
            self.execute_delete(
                "DELETE FROM project_snapshot WHERE name = ?", (name,)
            )
            > 0
        )

    def rename_project_snapshot(self, name: str, new_name: str) -> bool:
        """Rename a ``project_snapshot`` row from *name* to *new_name*.

        Mirror of ``SQLiteAdapter.rename_project_snapshot``.  Returns
        whether a row was updated; raises ``sqlite3.IntegrityError`` when
        *new_name* collides (PK UNIQUE constraint).
        """
        return (
            self.execute_update(
                "UPDATE project_snapshot SET name = ? WHERE name = ?",
                (new_name, name),
            )
            > 0
        )

    def reconcile_stale_runs(self) -> dict[str, int]:
        """Startup-only: flip stale running rows to ``interrupted`` (one pass).

        Mirror of ``SQLiteAdapter.reconcile_stale_runs`` (same schema and
        interface).  Returns per-table counts of flipped rows.
        """
        self._ensure_owner_thread()
        return _reconcile_stale_runs(self._conn)

    def rebuild_manifests(self, render_root: str) -> dict[str, int]:
        """Startup-only: rebuild derived manifests for completed renders.

        Mirror of ``SQLiteAdapter.rebuild_manifests`` (same schema and
        interface).  Returns ``{"manifests_rebuilt": int,
        "jobs_marked_expired": int}``.
        """
        self._ensure_owner_thread()
        return _rebuild_manifests(self._conn, render_root)

    def gc_expired_artifacts(
        self,
        render_root: str,
        *,
        job_retention_days: float | None = None,
        chunk_retention_days: float | None = None,
    ) -> dict[str, int | list[str]]:
        """Tombstoning GC sweep — expired run dirs + rows reclaimed in one pass.

        Mirror of ``SQLiteAdapter.gc_expired_artifacts`` (same schema and
        interface).  Returns ``{"run_dirs_deleted": int, "chunks_evicted":
        int, "jobs_expired": int, "skipped_snapshot_referenced":
        list[str]}``.
        """
        self._ensure_owner_thread()
        return _gc_sweep(
            self._conn,
            render_root,
            job_retention_days=job_retention_days,
            chunk_retention_days=chunk_retention_days,
        )
