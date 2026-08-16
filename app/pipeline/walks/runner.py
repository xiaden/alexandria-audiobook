"""Walk runner infrastructure for the audiobook pipeline.

Provides ``WalkRunner`` which orchestrates serial execution of walk modules.
Each walk module lives under ``app.pipeline.walks`` and exposes an
``execute(book_id, storage, config)`` function. Walks run one at a time;
each consumes the prior walk's output.

Walk status is tracked in-memory (the ``_status`` dict — frontend contract)
AND persisted: every ``run_walk`` invocation records a fresh ``walk_run``
row (running → completed | failed | cancelled) with created_ms,
finished_ms, result_json, error and heartbeat_ms (rows = truth).
``is_cancel_requested`` is the single cancellation dispatcher over the DB
row flag, a persisted stop-file, and the in-process per-book event.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import random
import time
import traceback
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage
    from app.pipeline.walks.log_service import WalkLogService

from app.pipeline.adapter import ConcurrentTransactionError

from ._llm_helpers import WALK_LOG_SINK
from .order import WALK_ORDER

logger = logging.getLogger(__name__)

# Type alias for verification functions.
# Signature: (book_id, storage) -> bool
VerifyFn = Callable[[str, "PipelineStorage"], bool]


def _now_ms() -> int:
    """Current time as INTEGER unix milliseconds (schema convention)."""
    return int(time.time() * 1000)


def _is_canonical_uuid(value: str) -> bool:
    """Return True only for a strictly canonical (lowercase-dashed) UUID string.

    Uses a strict ``uuid.UUID(str)`` round-trip so non-canonical spellings
    (missing dashes, ``urn:uuid:`` prefixes, non-hex digits) are rejected.
    This mirrors the Part A ``WalkLogService`` canonical-UUID semantics.
    """
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# API-owned reservation contract (Part B, P2-S1)
# ---------------------------------------------------------------------------
# The API layer owns reservation: it generates canonical UUIDs and persists
# ``pending`` rows; the runner only consumes those reservations. These helpers
# are the reservation boundary Part C calls. SQL is parameterized throughout.


def reserve_walk_run(
    storage: PipelineStorage,
    run_id: str,
    book_id: str,
    walk_name: str,
    created_ms: int | None = None,
) -> str:
    """Reserve one ``walk_run`` row for a caller-supplied canonical run ID.

    Validates a canonical UUID and an allowed walk name, then inserts exactly
    one ``pending`` row with ``cancel_requested=0``, ``heartbeat_ms=created_ms``
    (or ``_now_ms()`` when omitted) and null result/error/finished fields.
    Returns the same ``run_id``. The caller owns UUID generation and scheduling.
    """
    if not _is_canonical_uuid(run_id):
        raise ValueError(f"invalid run_id (must be a canonical UUID): {run_id!r}")
    if walk_name not in WALK_ORDER:
        raise ValueError(f"unknown walk: {walk_name!r}")
    now = _now_ms() if created_ms is None else created_ms
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, "
        "cancel_requested, created_ms, heartbeat_ms) "
        "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
        (run_id, book_id, walk_name, now, now),
    )
    return run_id


def reserve_all_walk_runs(
    storage: PipelineStorage,
    book_id: str,
    reservations: Sequence[tuple[str, str]],
    created_ms: int | None = None,
) -> tuple[tuple[str, str], ...]:
    """Reserve nine pending ``walk_run`` rows covering ``WALK_ORDER`` exactly.

    ``reservations`` is a sequence of ``(walk_name, run_id)`` tuples. Validates
    that the reservation covers ``WALK_ORDER`` exactly (no missing/extra/duplicate
    walk), that every run ID is a unique canonical UUID, then inserts all nine
    pending rows. Returns the normalized ``(walk_name, run_id)`` pairs in
    ``WALK_ORDER`` order regardless of input order.
    """
    by_walk: dict[str, str] = {}
    seen_run_ids: set[str] = set()
    for walk_name, run_id in reservations:
        if not _is_canonical_uuid(run_id):
            raise ValueError(f"invalid run_id (must be a canonical UUID): {run_id!r}")
        if run_id in seen_run_ids:
            raise ValueError(f"duplicate run_id in reservation: {run_id!r}")
        seen_run_ids.add(run_id)
        if walk_name in by_walk:
            raise ValueError(f"duplicate walk in reservation: {walk_name!r}")
        by_walk[walk_name] = run_id
    missing = [w for w in WALK_ORDER if w not in by_walk]
    if missing:
        raise ValueError(f"reservation missing walks: {missing}")
    extra = [w for w in by_walk if w not in WALK_ORDER]
    if extra:
        raise ValueError(f"reservation contains unknown walks: {extra}")
    ordered = tuple((walk, by_walk[walk]) for walk in WALK_ORDER)
    now = _now_ms() if created_ms is None else created_ms
    for walk_name, run_id in ordered:
        storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, "
            "cancel_requested, created_ms, heartbeat_ms) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
            (run_id, book_id, walk_name, now, now),
        )
    return ordered


def mark_reserved_runs_failed(
    storage: PipelineStorage, run_ids: Iterable[str], error: str
) -> None:
    """Mark still-pending reservations failed without executing them.

    Schedule-failure cleanup at the reservation boundary: updates ONLY rows
    still ``pending`` to ``failed`` (leaving non-pending rows untouched) and
    never invokes reserved runner execution.
    """
    for run_id in run_ids:
        storage.execute_update(
            "UPDATE walk_run SET status = 'failed', error = ?, finished_ms = ? "
            "WHERE run_id = ? AND status = 'pending'",
            (error, _now_ms(), run_id),
        )


# ---------------------------------------------------------------------------
# Walk-side retry on ConcurrentTransactionError (contract rule #6)
# ---------------------------------------------------------------------------

# A ConcurrentTransactionError on the idempotent write phase is retried with a
# 50-100ms backoff x3 (contract rule #6 / DD-universal-upgrade.md line 105:
# "retry idempotent write phase x3, then fail unit"), i.e. 4 total attempts
# (initial + 3 retries), then the unit fails. The retry wraps ONLY
# the storage write call: it is a pure re-dispatch of one execute_* method
# (SQL + params only), never a re-execution of the walk unit — so the walk's
# SELECT -> LLM -> write flow (including the LLM call in _llm_helpers) is never
# re-invoked by a retry.
_MAX_WRITE_RETRIES = 3  # contract rule #6: retry the idempotent write x3
# Total attempts = initial + 3 retries (contract counts retries, not attempts).
_MAX_WRITE_ATTEMPTS = _MAX_WRITE_RETRIES + 1
_BACKOFF_MIN_S = 0.05    # contract lower bound: 50 ms
_BACKOFF_MAX_S = 0.10    # contract upper bound: 100 ms


def _retry_write(write_fn: Callable[[], int]) -> int:
    """Dispatch an idempotent storage write under the retry contract.

    ``write_fn`` must be a thunk performing exactly one write-method dispatch
    (``execute_insert``/``execute_update``/``execute_delete``) on the
    underlying adapter — it closes over only the SQL and params, nothing else,
    so a retry can never re-execute the walk unit or re-invoke an LLM call.
    On ``ConcurrentTransactionError`` the write is re-dispatched after a
    50-100ms sleep, for ``_MAX_WRITE_RETRIES`` retries (4 total attempts =
    initial + 3 retries, per contract rule #6); the final error is re-raised
    so the runner's existing failure path records it on the ``walk_run`` row
    and marks the unit failed.
    """
    for attempt in range(1, _MAX_WRITE_ATTEMPTS + 1):
        try:
            return write_fn()
        except ConcurrentTransactionError:
            if attempt == _MAX_WRITE_ATTEMPTS:
                logger.warning(
                    "ConcurrentTransactionError persisted after %d attempts; "
                    "failing walk unit",
                    _MAX_WRITE_ATTEMPTS,
                )
                raise
            logger.warning(
                "ConcurrentTransactionError on write attempt %d/%d; "
                "backing off %d-%d ms",
                attempt,
                _MAX_WRITE_ATTEMPTS,
                int(_BACKOFF_MIN_S * 1000),
                int(_BACKOFF_MAX_S * 1000),
            )
            time.sleep(random.uniform(_BACKOFF_MIN_S, _BACKOFF_MAX_S))


def _verify_walk_2a(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2a_scene_segmentation produced scenes for chapters."""
    rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM chapter_scene "
        "WHERE parent_id IN "
        "(SELECT id FROM chapter WHERE book_id = ?)",
        (book_id,),
    )
    scene_count = rows[0]["cnt"] if rows else 0
    return scene_count > 0


def _verify_walk_2b(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2b_character_discovery produced character rows for the book."""
    rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = ?",
        (book_id,),
    )
    char_count = rows[0]["cnt"] if rows else 0
    if char_count == 0:
        return False
    # Also verify character_scene junctions exist for the book's scenes
    scene_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_scene cs "
        "JOIN chapter_scene cscene ON cs.scene_id = cscene.child_id "
        "JOIN chapter c ON cscene.parent_id = c.id "
        "WHERE c.book_id = ?",
        (book_id,),
    )
    scene_char_count = scene_rows[0]["cnt"] if scene_rows else 0
    return scene_char_count > 0


def _verify_walk_2c(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2c_alias_resolution preserved character links.

    Walk 2c may merge (delete) characters, so we can't check for a minimum
    count.  Instead we verify that every character_book row points to a
    character that still exists — no dangling references from deleted
    characters.
    """
    rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_book cb "
        "LEFT JOIN character c ON cb.character_id = c.id "
        "WHERE cb.book_id = ? AND c.id IS NULL",
        (book_id,),
    )
    orphan_count = rows[0]["cnt"] if rows else 0
    return orphan_count == 0


def _verify_walk_2d(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2d_scene_presence produced character_scene junctions.

    Checks that at least one character_scene junction exists for the book's
    scenes.  Walk 2d refines walk 2b's junctions, so if walk 2b created any,
    walk 2d's verification should pass.
    """
    rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_scene cs "
        "JOIN chapter_scene cscene ON cs.scene_id = cscene.child_id "
        "JOIN chapter c ON cscene.parent_id = c.id "
        "WHERE c.book_id = ?",
        (book_id,),
    )
    junction_count = rows[0]["cnt"] if rows else 0
    return junction_count > 0


def _verify_walk_2e(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2e_span_attribution produced speaker attributions.

    Checks that quotation spans exist for the book. If quotations exist, at
    least one should have a character_span junction with relation_type='speaker'.
    Empty books (no quotations) are acceptable.
    """
    # Check if any quotation spans exist for this book
    quotation_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM span s "
        "JOIN paragraph_span ps ON ps.child_id = s.id "
        "JOIN scene_paragraph sp ON sp.child_id = ps.parent_id "
        "JOIN chapter_scene cs ON cs.child_id = sp.parent_id "
        "JOIN chapter c ON c.id = cs.parent_id "
        "WHERE c.book_id = ? AND s.span_type = 'quotation'",
        (book_id,),
    )
    quotation_count = quotation_rows[0]["cnt"] if quotation_rows else 0

    # If no quotations exist, that's fine (empty book)
    if quotation_count == 0:
        return True

    # If quotations exist, at least one should have a speaker junction
    speaker_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_span cs "
        "JOIN span s ON cs.span_id = s.id "
        "JOIN paragraph_span ps ON ps.child_id = s.id "
        "JOIN scene_paragraph sp ON sp.child_id = ps.parent_id "
        "JOIN chapter_scene cscene ON cscene.child_id = sp.parent_id "
        "JOIN chapter c ON c.id = cscene.parent_id "
        "WHERE c.book_id = ? AND s.span_type = 'quotation' "
        "AND cs.relation_type = 'speaker'",
        (book_id,),
    )
    speaker_count = speaker_rows[0]["cnt"] if speaker_rows else 0
    return speaker_count > 0


def _verify_walk_2f(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2f_character_description produced character descriptions.

    Checks that at least one character has a description stored in
    character_metadata. Empty books (no characters) are acceptable.
    """
    # Check if any characters exist for this book
    character_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = ?",
        (book_id,),
    )
    character_count = character_rows[0]["cnt"] if character_rows else 0

    # If no characters exist, that's fine (empty book)
    if character_count == 0:
        return True

    # If characters exist, at least one should have a description
    description_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_metadata cm "
        "JOIN character_book cb ON cm.character_id = cb.character_id "
        "WHERE cb.book_id = ? AND cm.key = 'description'",
        (book_id,),
    )
    description_count = description_rows[0]["cnt"] if description_rows else 0
    return description_count > 0


def _verify_walk_2g(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2g_voice_audition produced voice profiles.

    Checks that at least one character has a voice_profile stored in
    character_metadata. Empty books (no characters) are acceptable.
    """
    # Check if any characters exist for this book
    character_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = ?",
        (book_id,),
    )
    character_count = character_rows[0]["cnt"] if character_rows else 0

    # If no characters exist, that's fine (empty book)
    if character_count == 0:
        return True

    # If characters exist, at least one should have a voice_profile
    profile_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_metadata cm "
        "JOIN character_book cb ON cm.character_id = cb.character_id "
        "WHERE cb.book_id = ? AND cm.key = 'voice_profile'",
        (book_id,),
    )
    profile_count = profile_rows[0]["cnt"] if profile_rows else 0
    return profile_count > 0


def _verify_walk_2h(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2h_voice_assignment produced voice assignments.

    Checks that at least one character for the book has a non-NULL
    voice_assignment_id. Empty books (no characters) are acceptable.
    """
    # Check if any characters exist for this book
    character_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character_book WHERE book_id = ?",
        (book_id,),
    )
    character_count = character_rows[0]["cnt"] if character_rows else 0

    # If no characters exist, that's fine (empty book)
    if character_count == 0:
        return True

    # If characters exist, at least one should have a voice assignment
    assignment_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM character c "
        "JOIN character_book cb ON c.id = cb.character_id "
        "WHERE cb.book_id = ? AND c.voice_assignment_id IS NOT NULL",
        (book_id,),
    )
    assignment_count = assignment_rows[0]["cnt"] if assignment_rows else 0
    return assignment_count > 0


def _verify_walk_2i(book_id: str, storage: PipelineStorage) -> bool:
    """Verify that walk_2i_delivery produced delivery instructions.

    Checks that at least one span for the book has a non-NULL instruct
    column. Empty books (no spans) are acceptable.
    """
    # Check if any spans exist for this book
    span_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM span s "
        "JOIN paragraph_span ps ON ps.child_id = s.id "
        "JOIN scene_paragraph sp ON sp.child_id = ps.parent_id "
        "JOIN chapter_scene cs ON cs.child_id = sp.parent_id "
        "JOIN chapter c ON c.id = cs.parent_id "
        "WHERE c.book_id = ?",
        (book_id,),
    )
    span_count = span_rows[0]["cnt"] if span_rows else 0

    # If no spans exist, that's fine (empty book)
    if span_count == 0:
        return True

    # If spans exist, at least one should have an instruct value
    instruct_rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM span s "
        "JOIN paragraph_span ps ON ps.child_id = s.id "
        "JOIN scene_paragraph sp ON sp.child_id = ps.parent_id "
        "JOIN chapter_scene cs ON cs.child_id = sp.parent_id "
        "JOIN chapter c ON c.id = cs.parent_id "
        "WHERE c.book_id = ? AND s.instruct IS NOT NULL",
        (book_id,),
    )
    instruct_count = instruct_rows[0]["cnt"] if instruct_rows else 0
    return instruct_count > 0


# Per-walk verification registry.
# Maps walk_name -> verification function.
# A verification function returns True if the walk's output is valid.
_VERIFICATIONS: dict[str, VerifyFn] = {
    "walk_2a_scene_segmentation": _verify_walk_2a,
    "walk_2b_character_discovery": _verify_walk_2b,
    "walk_2c_alias_resolution": _verify_walk_2c,
    "walk_2d_scene_presence": _verify_walk_2d,
    "walk_2e_span_attribution": _verify_walk_2e,
    "walk_2f_character_description": _verify_walk_2f,
    "walk_2g_voice_audition": _verify_walk_2g,
    "walk_2h_voice_assignment": _verify_walk_2h,
    "walk_2i_delivery": _verify_walk_2i,
}


class HeartbeatStorage:
    """Storage proxy that refreshes ``walk_run.heartbeat_ms`` after each write
    and retries the write boundary on ``ConcurrentTransactionError``.

    Walk modules receive this wrapper in place of the raw adapter so their
    auto-commit writes double as per-unit heartbeat updates. No walk module
    opens an explicit ``transaction()`` (verified: zero uses in
    ``app/pipeline/walks/``), so per-write is the finest heartbeat
    granularity available without modifying the 9 walk module files — the
    per-unit transaction pattern is forward-looking (Plan D). Heartbeat is
    observability-only (DD decision #14); the reconciliation contract only
    needs the row to carry a heartbeat so a stale ``running`` row is
    distinguishable from an active one.

    Retry boundary (contract rule #6): every write method re-dispatches its
    storage write through ``_retry_write``, so a ``ConcurrentTransactionError``
    (non-owner-thread write / ``BEGIN IMMEDIATE`` timeout — see adapter.py)
    is retried with a 50-100ms backoff x3 (4 total attempts: initial + 3
    retries, per contract rule #6), then re-raised so the runner's failure
    path fails the unit and records the error. The retry
    is a pure re-dispatch of the single write (SQL + params) — the walk's
    SELECT -> LLM -> write flow lives entirely inside the walk module, outside
    this wrapper, so no retry can re-invoke the LLM call. Reads
    (``execute_query``) are never retried: the contract applies only to the
    idempotent write phase. All other attributes (``get_connection``,
    ``transaction``, …) delegate to the wrapped adapter.
    """

    def __init__(self, storage: PipelineStorage, run_id: str) -> None:
        # object.__setattr__ avoids __getattr__ recursion during init.
        object.__setattr__(self, "storage", storage)
        object.__setattr__(self, "run_id", run_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.storage, name)

    def execute_insert(self, sql: str, params: tuple = ()) -> int:
        result = _retry_write(lambda: self.storage.execute_insert(sql, params))
        self._touch()
        return result

    def execute_update(self, sql: str, params: tuple = ()) -> int:
        result = _retry_write(lambda: self.storage.execute_update(sql, params))
        self._touch()
        return result

    def execute_delete(self, sql: str, params: tuple = ()) -> int:
        result = _retry_write(lambda: self.storage.execute_delete(sql, params))
        self._touch()
        return result

    def _touch(self) -> None:
        # The heartbeat stamp is itself an idempotent write on the same
        # boundary, so it gets the same retry treatment.
        _retry_write(
            lambda: self.storage.execute_update(
                "UPDATE walk_run SET heartbeat_ms = ? WHERE run_id = ?",
                (_now_ms(), self.run_id),
            )
        )


class WalkRunner:
    """Orchestrates serial execution of pipeline walk modules.

    Walks are loaded dynamically by name from ``app.pipeline.walks`` and
    executed one at a time. Status is tracked in-memory per book (the
    ``_status`` dict drives ``get_walk_status`` — frontend contract
    unchanged) and persisted to ``walk_run`` rows (rows = truth).

    The ``run_*_reserved`` methods consume caller-reserved run IDs (canonical
    UUIDs persisted as ``pending`` rows by the reservation helpers) and never
    allocate, generate, or discover a replacement run ID — the caller owns
    run identity.

    Parameters
    ----------
    storage:
        Pipeline storage adapter for database operations.
    log_service: ``WalkLogService | None`` = None
        Optional Part A service providing per-run walk-log sinks. When None
        (the default, and how all legacy ``WalkRunner(storage)`` callers
        construct), the reserved methods perform NO sink operations. When
        provided, per-run sinks are opened after pending-row verification and
        exactly one terminal record is written (via ``close_run``) before each
        row is finalized.
    """

    # Directory for persisted cancel stop-files. No location is defined in
    # the DD or CONTRACTS ledger; chosen ``data/cancel/`` to match the
    # adapter's ``./data/pipeline.db`` convention (covered by the
    # gitignored ``data/`` directory). Overridable per instance (tests).
    stop_file_dir: str = "data/cancel"

    def __init__(
        self,
        storage: PipelineStorage,
        log_service: WalkLogService | None = None,
    ) -> None:
        self._storage = storage
        #: Optional Part A service for per-run walk-log sinks. When None (the
        #: default, and how all pre-existing ``WalkRunner(storage)`` callers
        #: construct), the reserved methods perform NO sink operations. Part C
        #: owns production wiring of a real service instance.
        self._log_service = log_service
        # {book_id: OrderedDict(walk_name -> status)}
        self._status: dict[str, OrderedDict[str, str]] = {}
        # {book_id: bool} — cancellation flag per book
        self._cancelled: dict[str, bool] = {}

    def run_walk(
        self, walk_name: str, book_id: str, config: dict
    ) -> dict:
        """Execute a single walk by name.

        Records a fresh ``walk_run`` row (status running) before the walk
        module runs; on success flips the row to completed with
        ``result_json``; on exception flips it to failed with the error
        text. The row carries ``created_ms`` and ``heartbeat_ms`` at start
        and ``finished_ms``/``heartbeat_ms`` at the final transition.

        Parameters
        ----------
        walk_name:
            Module name under ``app.pipeline.walks`` (e.g.
            ``walk_2a_scene_segmentation``).
        book_id:
            UUID of the book to process.
        config:
            Configuration dict passed through to the walk.

        Returns
        -------
        dict
            Result dict from the walk's ``execute()`` function, or an
            error dict with ``status='failed'`` on failure, or
            ``{'status': 'cancelled', ...}`` when cancellation was requested
            before execution.
        """
        self._ensure_book(book_id)
        # Refuse a second concurrent walk for the same book (preserved).
        if self._get_status(book_id, walk_name) == "running":
            return {
                "status": "failed",
                "error": f"Walk '{walk_name}' is already running for book '{book_id}'",
            }
        run_id = str(uuid.uuid4())
        now = _now_ms()
        self._storage.execute_insert(
            "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms, heartbeat_ms) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, book_id, walk_name, now, now),
        )
        # Single cancellation dispatcher — honored before walk execution.
        if self.is_cancel_requested(run_id):
            self._finalize_run(run_id, "cancelled", error="Walk cancelled by user")
            self._set_status(book_id, walk_name, "cancelled")
            logger.info(
                "Walk '%s' cancelled before start for book '%s'",
                walk_name,
                book_id,
            )
            return {"status": "cancelled", "error": "Walk cancelled by user"}
        self._set_status(book_id, walk_name, "running")
        logger.info("Starting walk '%s' for book '%s'", walk_name, book_id)
        try:
            walk_module = self._load_walk_module(walk_name)
        except ImportError as exc:
            self._finalize_run(run_id, "failed", error=str(exc))
            self._set_status(book_id, walk_name, "failed")
            logger.error("Failed to import walk '%s': %s", walk_name, exc)
            return {"status": "failed", "error": str(exc)}
        try:
            result = walk_module.execute(
                book_id, HeartbeatStorage(self._storage, run_id), config
            )
        except Exception as exc:  # noqa: BLE001 — walk boundary: record any failure
            self._finalize_run(run_id, "failed", error=str(exc))
            self._set_status(book_id, walk_name, "failed")
            logger.error(
                "Walk '%s' raised exception for book '%s': %s",
                walk_name,
                book_id,
                exc,
            )
            return {"status": "failed", "error": str(exc)}
        if not self._run_verification(walk_name, book_id):
            self._finalize_run(
                run_id, "failed", error=f"Verification failed for walk '{walk_name}'"
            )
            self._set_status(book_id, walk_name, "failed")
            logger.error(
                "Walk '%s' verification failed for book '%s'",
                walk_name,
                book_id,
            )
            return {
                "status": "failed",
                "error": f"Verification failed for walk '{walk_name}'",
                "result": result,
            }
        self._finalize_run(run_id, "completed", result=result)
        self._set_status(book_id, walk_name, "completed")
        logger.info("Completed walk '%s' for book '%s'", walk_name, book_id)
        return result

    def run_all_walks(self, book_id: str, config: dict) -> dict:
        """Execute all walks in canonical order for a book.

        Each walk goes through ``run_walk`` — its own ``walk_run`` row and
        its own ``is_cancel_requested`` check before execution. Abort-on-
        first-failure preserved.

        Parameters
        ----------
        book_id:
            UUID of the book to process.
        config:
            Configuration dict passed through to each walk.

        Returns
        -------
        dict
            Summary dict with ``{walk_name: result_dict}`` for each walk.
        """
        self._ensure_book(book_id)
        results: dict[str, dict] = {}
        for walk_name in WALK_ORDER:
            result = self.run_walk(walk_name, book_id, config)
            results[walk_name] = result
            if result.get("status") == "failed":
                logger.error(
                    "Walk '%s' failed — aborting remaining walks for book '%s'",
                    walk_name,
                    book_id,
                )
                break
        return results

    def run_walk_reserved(
        self, run_id: str, walk_name: str, book_id: str, config: dict
    ) -> dict:
        """Execute a single reserved walk by its persisted pending run ID.

        Consumes the reservation created by ``reserve_walk_run``: verifies the
        exact ``pending`` row exists for ``(run_id, book_id)``, transitions it
        to ``running``, and executes with
        ``HeartbeatStorage(self._storage, run_id)``. It NEVER allocates,
        generates, or discovers a replacement run ID — the caller owns identity.

        A per-run walk-log sink is opened via ``self._log_service.open_run`` only
        after the pending row is verified and before module execution. When
        ``self._log_service`` is None (the default), no sink operations occur.
        A sink-open failure never alters the DB outcome (row = truth); DB
        finalization proceeds normally. When a sink is opened, the runner sets
        ``WALK_LOG_SINK`` to it immediately before ``walk_module.execute(...)``
        and resets it in ``finally`` on every terminal path (success, exception,
        import failure, verification failure); a cancelled-before-start run opens
        no sink. Exactly one terminal record is appended (via
        ``log_service.close_run``) before the DB row is finalized.
        """
        rows = self._storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ? AND book_id = ?",
            (run_id, book_id),
        )
        if not rows or rows[0]["status"] != "pending":
            return {
                "status": "failed",
                "error": f"Reservation not pending for run '{run_id}'",
            }
        self._ensure_book(book_id)
        now = _now_ms()
        self._storage.execute_update(
            "UPDATE walk_run SET status = 'running', heartbeat_ms = ? "
            "WHERE run_id = ?",
            (now, run_id),
        )
        # Single cancellation dispatcher — honored before walk execution (and
        # before any sink is opened, so a cancelled-before-start run opens none).
        if self.is_cancel_requested(run_id):
            self._finalize_run(run_id, "cancelled", error="Walk cancelled by user")
            self._set_status(book_id, walk_name, "cancelled")
            logger.info(
                "Reserved walk '%s' cancelled before start for book '%s'",
                walk_name,
                book_id,
            )
            return {"status": "cancelled", "error": "Walk cancelled by user"}
        self._set_status(book_id, walk_name, "running")
        logger.info("Starting reserved walk '%s' for book '%s'", walk_name, book_id)

        # Open the per-run sink AFTER the pending row is verified and BEFORE
        # module execution. A setup failure is swallowed so DB finalization still
        # proceeds normally (sink failure never alters DB). When no sink is
        # opened (log_service is None, or open_run failed), the ContextVar is
        # never set and no terminal record is written for this run.
        sink = None
        if self._log_service is not None:
            try:
                sink = self._log_service.open_run(
                    run_id, book_id, walk_name, started_ms=now
                )
            except Exception:
                logger.warning(
                    "Walk log sink open failed; run_id=%s", run_id, exc_info=True
                )

        token = WALK_LOG_SINK.set(sink) if sink is not None else None
        try:
            try:
                walk_module = self._load_walk_module(walk_name)
            except ImportError as exc:
                self._terminal_and_close(
                    run_id,
                    "failed",
                    {"error": str(exc), "traceback": traceback.format_exc()},
                )
                self._finalize_run(run_id, "failed", error=str(exc))
                self._set_status(book_id, walk_name, "failed")
                logger.error("Failed to import walk '%s': %s", walk_name, exc)
                return {"status": "failed", "error": str(exc)}
            try:
                result = walk_module.execute(
                    book_id, HeartbeatStorage(self._storage, run_id), config
                )
            except Exception as exc:  # noqa: BLE001 - walk boundary: record any failure
                self._terminal_and_close(
                    run_id,
                    "failed",
                    {"error": str(exc), "traceback": traceback.format_exc()},
                )
                self._finalize_run(run_id, "failed", error=str(exc))
                self._set_status(book_id, walk_name, "failed")
                logger.error(
                    "Reserved walk '%s' raised exception for book '%s': %s",
                    walk_name,
                    book_id,
                    exc,
                )
                return {"status": "failed", "error": str(exc)}
            if not self._run_verification(walk_name, book_id):
                error = f"Verification failed for walk '{walk_name}'"
                self._terminal_and_close(
                    run_id,
                    "failed",
                    {"error": error, "traceback": traceback.format_exc()},
                )
                self._finalize_run(run_id, "failed", error=error)
                self._set_status(book_id, walk_name, "failed")
                logger.error(
                    "Reserved walk '%s' verification failed for book '%s'",
                    walk_name,
                    book_id,
                )
                return {
                    "status": "failed",
                    "error": error,
                    "result": result,
                }
            self._terminal_and_close(run_id, "completed", {"status": "completed"})
            self._finalize_run(run_id, "completed", result=result)
            self._set_status(book_id, walk_name, "completed")
            logger.info(
                "Completed reserved walk '%s' for book '%s'", walk_name, book_id
            )
            return result
        finally:
            # Reset the sink ContextVar on EVERY terminal path (success,
            # exception, import failure, verification failure). token is None
            # when no sink was opened, so the reset is a no-op then and the
            # variable returns to its prior value in every case.
            if token is not None:
                WALK_LOG_SINK.reset(token)

    def run_all_walks_reserved(
        self,
        batch_id: str,
        reservations: Sequence[tuple[str, str]],
        book_id: str,
        config: dict,
    ) -> dict:
        """Execute the complete ordered reserved batch serially.

        Consumes the full nine-child reservation produced by
        ``reserve_all_walk_runs`` and runs each child via
        ``run_walk_reserved`` in ``WALK_ORDER``. ``batch_id`` is correlation-only
        and creates NO parent ``walk_run`` row (child rows are truth).

        Abort-on-first-failure is preserved: when a child fails (or cancels),
        every not-yet-started child is terminalized WITHOUT executing it. On
        cancellation every child is terminalized.
        """
        self._ensure_book(book_id)
        results: dict[str, dict] = {}
        for walk_name, run_id in reservations:
            result = self.run_walk_reserved(run_id, walk_name, book_id, config)
            results[walk_name] = result
            if result.get("status") != "completed":
                logger.error(
                    "Reserved walk '%s' finished '%s' — aborting remaining walks "
                    "for book '%s'",
                    walk_name,
                    result.get("status"),
                    book_id,
                )
                self._terminalize_reserved(
                    [rid for w, rid in reservations if w not in results],
                    result.get("status", "failed"),
                )
                break
        return results

    def _terminalize_reserved(self, run_ids: Iterable[str], status: str) -> None:
        """Terminalize not-yet-started reserved children without executing them.

        Updates ONLY rows still ``pending`` — children already finalized by
        ``run_walk_reserved`` are left untouched — so no started child is
        overwritten.
        """
        for run_id in run_ids:
            self._storage.execute_update(
                "UPDATE walk_run SET status = ?, finished_ms = ?, heartbeat_ms = ? "
                "WHERE run_id = ? AND status = 'pending'",
                (status, _now_ms(), _now_ms(), run_id),
            )

    def _terminal_and_close(
        self, run_id: str, status: str, payload: dict[str, Any] | None
    ) -> None:
        """Append exactly one terminal record and close the run's sink.

        Delegates to ``log_service.close_run``, which appends the terminal
        record (``sink.append_terminal``), publishes it, deregisters the run,
        and closes the sink — exactly ONE terminal record, never a double-append.
        Any sink failure is logged and swallowed: the DB row is authoritative, so
        a filesystem/terminal failure must never alter DB status/result/error.
        No-op when ``log_service`` is None or no sink was registered for the run
        (``close_run`` returns None for an unknown run).
        """
        if self._log_service is None:
            return
        try:
            self._log_service.close_run(run_id, status, payload)
        except Exception:
            logger.warning(
                "Walk log terminal/close failed; run_id=%s", run_id, exc_info=True
            )

    def get_walk_status(self, book_id: str, walk_name: str) -> str:
        """Return the current status of a walk for a book.

        Returns ``'pending'`` if the walk has not been initialized for
        this book.
        """
        return self._get_status(book_id, walk_name)

    def cancel_walks(self, book_id: str) -> None:
        """Set the cancellation flag for a book and persist the request.

        Three sources, all read by ``is_cancel_requested(run_id)``:

        1. sets the in-process per-book event (existing semantics — tests
           assert the ``_cancelled`` dict directly),
        2. writes ``cancel_requested=1`` on the book's active
           (pending/running) ``walk_run`` rows,
        3. drops a stop-file per active run so the cancel intent survives
           a process restart.
        """
        self._cancelled[book_id] = True
        rows = self._storage.execute_query(
            "SELECT run_id FROM walk_run WHERE book_id = ? "
            "AND status IN ('pending', 'running')",
            (book_id,),
        )
        for row in rows:
            run_id = row["run_id"]
            self._storage.execute_update(
                "UPDATE walk_run SET cancel_requested = 1, heartbeat_ms = ? "
                "WHERE run_id = ?",
                (_now_ms(), run_id),
            )
            self._write_stop_file(run_id)
        logger.info("Walks cancelled for book '%s'", book_id)

    def clear_cancel(self, book_id: str) -> None:
        """Clear the cancellation flag for a book (e.g. before a new run)."""
        self._cancelled.pop(book_id, None)

    def is_cancel_requested(self, run_id: str) -> bool:
        """Single cancellation dispatcher: row flag OR stop-file OR event.

        Reads, in order:

        1. the ``walk_run`` row's ``cancel_requested`` flag (persisted by
           ``cancel_walks``; survives process restart),
        2. a persisted stop-file (``data/cancel/{run_id}.stop`` — chosen
           convention: neither DD nor CONTRACTS define a stop-file
           location; matches the adapter's ``./data/pipeline.db`` layout
           and sits inside the gitignored ``data/``),
        3. the in-process per-book event (``self._cancelled``), looked up
           through the run's ``book_id``.
        """
        rows = self._storage.execute_query(
            "SELECT book_id, cancel_requested FROM walk_run WHERE run_id = ?",
            (run_id,),
        )
        if rows and rows[0]["cancel_requested"]:
            return True
        if os.path.exists(self._stop_file_path(run_id)):
            return True
        book_id = rows[0]["book_id"] if rows else None
        return book_id is not None and self._cancelled.get(book_id, False)

    # -- Internal helpers ---------------------------------------------------

    def _ensure_book(self, book_id: str) -> None:
        """Initialize status tracking for a book if not already present."""
        if book_id not in self._status:
            statuses: OrderedDict[str, str] = OrderedDict()
            for walk_name in WALK_ORDER:
                statuses[walk_name] = "pending"
            self._status[book_id] = statuses

    def _get_status(self, book_id: str, walk_name: str) -> str:
        """Get status for a walk, defaulting to 'pending'."""
        book_statuses = self._status.get(book_id)
        if book_statuses is None:
            return "pending"
        return book_statuses.get(walk_name, "pending")

    def _set_status(self, book_id: str, walk_name: str, status: str) -> None:
        """Set status for a walk under a book."""
        if book_id not in self._status:
            self._ensure_book(book_id)
        self._status[book_id][walk_name] = status

    def _finalize_run(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        result: dict | None = None,
    ) -> None:
        """Write the final status transition for a ``walk_run`` row.

        ``result`` is JSON-encoded into ``result_json`` (completed);
        ``error`` is stored on failure/cancel. Always stamps
        ``finished_ms`` and refreshes ``heartbeat_ms``.
        """
        now = _now_ms()
        if result is not None:
            self._storage.execute_update(
                "UPDATE walk_run SET status = ?, result_json = ?, "
                "finished_ms = ?, heartbeat_ms = ? WHERE run_id = ?",
                (status, json.dumps(result), now, now, run_id),
            )
        else:
            self._storage.execute_update(
                "UPDATE walk_run SET status = ?, error = ?, "
                "finished_ms = ?, heartbeat_ms = ? WHERE run_id = ?",
                (status, error, now, now, run_id),
            )
        # Best-effort stop-file cleanup: the row is now terminal and
        # authoritative, so this run's persisted cancel marker is obsolete.
        # If the process dies before removal, a stale stop-file remains, but
        # is_cancel_requested checks the terminal row first (row flag ->
        # stop-file -> event), so no false cancellation on a completed run.
        try:
            os.remove(self._stop_file_path(run_id))
        except FileNotFoundError:
            pass

    def _stop_file_path(self, run_id: str) -> str:
        """Path of the persisted stop-file for a run (``data/cancel/``)."""
        return os.path.join(self.stop_file_dir, f"{run_id}.stop")

    def _write_stop_file(self, run_id: str) -> None:
        """Persist a stop-file for the run so cancel survives a restart."""
        path = self._stop_file_path(run_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(_now_ms()))

    @staticmethod
    def _load_walk_module(walk_name: str):
        """Dynamically import a walk module by name.

        Parameters
        ----------
        walk_name:
            Module name (e.g. ``walk_2a_scene_segmentation``).

        Returns
        -------
        module
            The imported module, expected to have an ``execute()`` function.

        Raises
        ------
        ImportError
            If the module cannot be found.
        """
        module_path = f"app.pipeline.walks.{walk_name}"
        return importlib.import_module(module_path)

    def _run_verification(self, walk_name: str, book_id: str) -> bool:
        """Run post-walk verification if one is registered.

        Returns True if verification passes or no verification is
        registered for this walk.
        """
        verify_fn = _VERIFICATIONS.get(walk_name)
        if verify_fn is None:
            return True
        return verify_fn(book_id, self._storage)
