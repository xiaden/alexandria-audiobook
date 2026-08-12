"""Workbench storage/domain service for walks 2b/2c/2d (DD-combined-walks).

This module provides the shared backend foundation behind the combined
Characters & Scenes workbench: sole per-book generation revision allocation,
stable-anchor reads, append-only decisions/provenance, presence/absence
tombstones, convergent alias preview/commit/unmerge, boundary overrides, the
effective-config model, and stale-write (``base_revision``) checks.

Design invariants (registered in CONTRACTS.md):

* ``workbench_generation`` is the sole per-book revision allocator; it has no
  FK to ``book`` and ``book.version`` is never read or incremented here.
* Every revision allocation runs inside ``BEGIN IMMEDIATE`` via
  ``PipelineStorage.transaction()``.  The allocator upsert and the associated
  write commit together -- a rollback also rolls back the increment.
* Generated and manual presence projections live in separate tables with
  independent target uniqueness.  The read projection prefers the manual row
  and treats an active absence tombstone as authoritative; a manual/generated
  disagreement is surfaced as a conflict, never a duplicate insert.
* Human decisions and absence tombstones are append-only / tombstone-based and
  are never deleted by a rerun or by this service.

All methods validate book scope and use parameterized SQL.  Callers map the
raised exceptions to HTTP status codes (StaleRevisionError/ConflictError ->
409, BookNotFoundError -> 404, ValidationError -> 422,
ConcurrentTransactionError -> 503 + Retry-After: 5).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any
from uuid import uuid4

from app.pipeline.adapter import PipelineStorage
from app.utils import load_app_config, resolve_task_config


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class WorkbenchError(Exception):
    """Base class for workbench domain errors."""


class BookNotFoundError(WorkbenchError):
    """The requested book does not exist (HTTP 404)."""


class ValidationError(WorkbenchError):
    """Malformed or unsupported input (HTTP 422)."""


class StaleRevisionError(WorkbenchError):
    """A ``base_revision`` did not match the current generation revision.

    Mapped to HTTP 409: every stale write is rejected.
    """


class ConflictError(WorkbenchError):
    """A write conflicted with a newer decision (HTTP 409)."""


class PreviewExpiredError(WorkbenchError):
    """An alias preview token is unknown, already used, or expired."""


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

_WORKBENCH_WALK_NAMES = frozenset(
    (
        "walk_2b_character_discovery",
        "walk_2c_alias_resolution",
        "walk_2d_scene_presence",
    )
)
_ALLOWED_OVERRIDE_KEYS = frozenset(
    ("model_name", "reasoning_effort", "temperature", "prompt")
)
_REASONING_EFFORTS = frozenset(("low", "medium", "high"))
_PRESENCE_TYPES = frozenset(("present", "speaker", "absent"))
_BOUNDARY_OPS = frozenset(("split", "merge", "resegment"))
_PREVIEW_TTL_MS = 10 * 60 * 1000  # ten-minute alias preview lifetime
_MAX_PROMPT_LEN = 20000

_DECISION_KINDS = frozenset(("presence", "alias_merge", "review", "boundary"))


def _now_ms() -> int:
    return int(time.time() * 1000)


class Workbench:
    """Storage/domain facade over a ``PipelineStorage``.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation (``SQLiteAdapter`` or
        ``InMemorySQLiteAdapter``).
    """

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage
        # Short-lived alias previews are book-scoped and single-use; they live
        # in memory because the registered schema defines no preview table.
        self._previews: dict[str, dict[str, Any]] = {}
        self._previews_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Book scope / revision allocation
    # ------------------------------------------------------------------

    def require_book(self, book_id: str) -> None:
        """Raise :class:`BookNotFoundError` if *book_id* does not exist."""
        if not self._storage.execute_query(
            "SELECT id FROM book WHERE id = ?", (book_id,)
        ):
            raise BookNotFoundError(f"unknown book: {book_id}")

    def get_generation(self, book_id: str) -> dict[str, Any] | None:
        """Return the ``workbench_generation`` row for *book_id*, or ``None``."""
        rows = self._storage.execute_query(
            "SELECT generation_id, book_id, revision, updated_ms"
            " FROM workbench_generation WHERE book_id = ?",
            (book_id,),
        )
        return rows[0] if rows else None

    def get_revision(self, book_id: str) -> int:
        """Return the current generation revision (0 when uninitialized)."""
        row = self.get_generation(book_id)
        return int(row["revision"]) if row else 0

    def allocate_revision(self, book_id: str) -> int:
        """Allocate the next sole per-book revision.

        Runs inside ``BEGIN IMMEDIATE`` (the enclosing transaction if one is
        open -- nested re-entry joins it).  One atomic upsert increments the
        ``workbench_generation.revision`` for *book_id* and ``RETURNING`` the
        new value, which commits together with the caller's associated write.
        A rollback of the enclosing transaction also rolls back the increment.

        Callers must validate book scope (``require_book``) separately.
        """
        now = _now_ms()
        with self._storage.transaction():
            rows = self._storage.execute_query(
                """INSERT INTO workbench_generation
                       (generation_id, book_id, revision, updated_ms)
                   VALUES (?, ?, 0, ?)
                   ON CONFLICT (book_id)
                   DO UPDATE SET revision = revision + 1,
                                 updated_ms = excluded.updated_ms
                   RETURNING revision""",
                ("wg-" + book_id, book_id, now),
            )
        return int(rows[0]["revision"])

    def check_revision(self, book_id: str, base_revision: int) -> None:
        """Raise :class:`StaleRevisionError` when *base_revision* is stale.

        Every stale write is rejected (409).  A missing generation row means
        current revision 0.
        """
        if self.get_revision(book_id) != base_revision:
            raise StaleRevisionError(
                f"stale base_revision {base_revision} for book {book_id};"
                f" current revision {self.get_revision(book_id)}"
            )

    # ------------------------------------------------------------------
    # Stable anchors
    # ------------------------------------------------------------------

    def get_stable_anchors(self, book_id: str) -> list[dict[str, Any]]:
        """Return immutable per-span stable anchors for *book_id*.

        Each anchor carries ``book_id``, ``scene_id``, ``chapter_id``,
        ``paragraph_id``, ``span_id`` and paragraph character offsets
        (``start_offset``/``end_offset``) derived from the ordered span text.
        Presentation position is display metadata only; identity is these
        stable IDs.
        """
        rows = self._storage.execute_query(
            """SELECT cs.child_id AS scene_id, cs.parent_id AS chapter_id,
                      p.id AS paragraph_id, scp.position AS paragraph_position,
                      sp.id AS span_id, sp.text AS span_text,
                      psp.position AS span_position
                 FROM book_chapter bc
                 JOIN chapter_scene cs ON cs.parent_id = bc.child_id
                 JOIN scene_paragraph scp ON scp.parent_id = cs.child_id
                 JOIN paragraph p ON p.id = scp.child_id
                 JOIN paragraph_span psp ON psp.parent_id = p.id
                 JOIN span sp ON sp.id = psp.child_id
                WHERE bc.parent_id = ?
                ORDER BY bc.position, cs.position, scp.position, psp.position""",
            (book_id,),
        )
        para_offset: dict[str, int] = {}
        anchors: list[dict[str, Any]] = []
        for row in rows:
            para_id = row["paragraph_id"]
            start = para_offset.get(para_id, 0)
            text = row["span_text"] or ""
            end = start + len(text)
            para_offset[para_id] = end
            anchors.append(
                {
                    "book_id": book_id,
                    "scene_id": row["scene_id"],
                    "chapter_id": row["chapter_id"],
                    "paragraph_id": para_id,
                    "span_id": row["span_id"],
                    "start_offset": start,
                    "end_offset": end,
                }
            )
        return anchors

    # ------------------------------------------------------------------
    # Append-only decisions / provenance
    # ------------------------------------------------------------------

    def record_decision(
        self,
        *,
        book_id: str,
        target_kind: str,
        target_key: str,
        decision_type: str,
        base_revision: int,
        payload: dict[str, Any],
        source: str = "human",
        status: str = "active",
        supersedes_id: str | None = None,
    ) -> str:
        """Append a decision record and return its ``decision:{uuid}`` ID.

        Decisions are append-only durable history (never rewritten).  ``status``
        starts ``active`` and transitions to ``undone``/``superseded``/``conflict``.
        """
        if target_kind not in _DECISION_KINDS:
            raise ValidationError(f"unsupported target_kind: {target_kind}")
        decision_id = f"decision:{uuid4()}"
        self._storage.execute_insert(
            """INSERT INTO workbench_decision
                   (decision_id, book_id, target_kind, target_key, decision_type,
                    base_revision, payload_json, status, source, created_ms,
                    supersedes_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                book_id,
                target_kind,
                target_key,
                decision_type,
                int(base_revision),
                json.dumps(payload),
                status,
                source,
                _now_ms(),
                supersedes_id,
            ),
        )
        return decision_id

    def record_provenance(
        self,
        *,
        book_id: str,
        target_kind: str,
        target_key: str,
        generation_revision: int,
        source: str,
        run_id: str | None = None,
    ) -> str:
        """Append a provenance record and return its ID."""
        provenance_id = f"prov:{uuid4()}"
        self._storage.execute_insert(
            """INSERT INTO workbench_provenance
                   (provenance_id, book_id, target_kind, target_key, run_id,
                    generation_revision, source, created_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                provenance_id,
                book_id,
                target_kind,
                target_key,
                run_id,
                int(generation_revision),
                source,
                _now_ms(),
            ),
        )
        return provenance_id

    def list_decisions(self, book_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Return *book_id*'s decision records, newest first."""
        return self._storage.execute_query(
            "SELECT * FROM workbench_decision WHERE book_id = ?"
            " ORDER BY created_ms DESC, decision_id DESC LIMIT ?",
            (book_id, int(limit)),
        )

    # ------------------------------------------------------------------
    # Presence / absence (generated + manual projections)
    # ------------------------------------------------------------------

    def _require_scene_character(self, scene_id: str, character_id: str) -> None:
        if not self._storage.execute_query(
            "SELECT id FROM scene WHERE id = ?", (scene_id,)
        ):
            raise ValidationError(f"unknown scene: {scene_id}")
        if not self._storage.execute_query(
            "SELECT id FROM character WHERE id = ?", (character_id,)
        ):
            raise ValidationError(f"unknown character: {character_id}")

    def get_generated_rows(self, book_id: str) -> list[dict[str, Any]]:
        """Return the generated presence projection rows for *book_id*."""
        return self._storage.execute_query(
            "SELECT id, book_id, character_id, scene_id, relation_type,"
            " confidence, generation_revision, source_run_id"
            " FROM character_scene_generated WHERE book_id = ?",
            (book_id,),
        )

    def get_manual_rows(self, book_id: str) -> list[dict[str, Any]]:
        """Return the manual presence projection rows for *book_id*."""
        return self._storage.execute_query(
            "SELECT id, book_id, character_id, scene_id, relation_type, decision_id"
            " FROM character_scene_manual WHERE book_id = ?",
            (book_id,),
        )

    def _active_absences(self, book_id: str) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self._storage.execute_query(
            "SELECT book_id, scene_id, character_id, decision_id, created_ms"
            " FROM character_scene_absence"
            " WHERE book_id = ? AND active = 1",
            (book_id,),
        )
        return {(r["scene_id"], r["character_id"]): r for r in rows}

    def set_presence(
        self,
        *,
        book_id: str,
        scene_id: str,
        character_id: str,
        relation_type: str,
        base_revision: int,
    ) -> dict[str, Any]:
        """Set a human presence decision for one character in one scene.

        ``relation_type`` is ``present``, ``speaker``, or ``absent``.  Writing
        ``absent`` creates/activates the ``character_scene_absence`` tombstone so
        a 2d rerun can never re-add the character; writing ``present``/``speaker``
        restores presence by deactivating the tombstone in the same transaction.
        A manual projection row records the decision; the current revision is
        allocated so the write is optimistically concurrency-safe.
        """
        self.require_book(book_id)
        self._require_scene_character(scene_id, character_id)
        if relation_type not in _PRESENCE_TYPES:
            raise ValidationError(
                f"relation_type must be one of {sorted(_PRESENCE_TYPES)}"
            )
        self.check_revision(book_id, base_revision)
        target_key = f"{scene_id}:{character_id}:{relation_type}"
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            decision_id = self.record_decision(
                book_id=book_id,
                target_kind="presence",
                target_key=target_key,
                decision_type=f"presence:{relation_type}",
                base_revision=base_revision,
                payload={
                    "scene_id": scene_id,
                    "character_id": character_id,
                    "relation_type": relation_type,
                },
                source="human",
            )
            self._storage.execute_insert(
                """INSERT INTO character_scene_manual
                       (id, book_id, character_id, scene_id, relation_type, decision_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (book_id, character_id, scene_id, relation_type)
                   DO UPDATE SET decision_id = excluded.decision_id, id = excluded.id""",
                (
                    f"pm-{uuid4()}",
                    book_id,
                    character_id,
                    scene_id,
                    relation_type,
                    decision_id,
                ),
            )
            active = 1 if relation_type == "absent" else 0
            self._storage.execute_insert(
                """INSERT INTO character_scene_absence
                       (book_id, scene_id, character_id, decision_id, active, created_ms)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (book_id, scene_id, character_id)
                   DO UPDATE SET active = excluded.active, decision_id = excluded.decision_id""",
                (book_id, scene_id, character_id, decision_id, active, _now_ms()),
            )
            self.record_provenance(
                book_id=book_id,
                target_kind="presence",
                target_key=target_key,
                generation_revision=revision,
                source="human",
            )
        return {
            "scene_id": scene_id,
            "character_id": character_id,
            "relation_type": relation_type,
            "decision_id": decision_id,
            "generation_revision": revision,
        }

    def get_presence(self, book_id: str) -> list[dict[str, Any]]:
        """Return the effective presence projection for *book_id*.

        Manual rows win over generated rows for the same ``(scene, character)``.
        An active absence tombstone is authoritative (renders ``absent``) over
        any present/speaker row.  Generated rows appear only when no manual row
        or absence governs that key.  Each returned row carries ``scene_id``,
        ``character_id``, ``relation_type``, ``source`` (``human``/``walk``),
        ``confidence``, and the governing revision.
        """
        absences = self._active_absences(book_id)
        manual = self._storage.execute_query(
            "SELECT character_id, scene_id, relation_type, decision_id"
            " FROM character_scene_manual WHERE book_id = ? AND relation_type != 'absent'",
            (book_id,),
        )
        generated = self._storage.execute_query(
            "SELECT character_id, scene_id, relation_type, confidence,"
            " generation_revision, source_run_id"
            " FROM character_scene_generated WHERE book_id = ?",
            (book_id,),
        )
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for m in manual:
            key = (m["scene_id"], m["character_id"])
            if key in absences:
                continue
            seen.add(key)
            rows.append(
                {
                    "book_id": book_id,
                    "scene_id": m["scene_id"],
                    "character_id": m["character_id"],
                    "relation_type": m["relation_type"],
                    "source": "human",
                    "confidence": 1.0,
                    "human_override": 1,
                    "decision_id": m["decision_id"],
                }
            )
        for g in generated:
            key = (g["scene_id"], g["character_id"])
            if key in seen or key in absences:
                continue
            rows.append(
                {
                    "book_id": book_id,
                    "scene_id": g["scene_id"],
                    "character_id": g["character_id"],
                    "relation_type": g["relation_type"],
                    "source": "walk",
                    "confidence": g["confidence"],
                    "human_override": 0,
                    "generation_revision": g["generation_revision"],
                    "source_run_id": g["source_run_id"],
                }
            )
        for (scene_id, character_id), a in absences.items():
            rows.append(
                {
                    "book_id": book_id,
                    "scene_id": scene_id,
                    "character_id": character_id,
                    "relation_type": "absent",
                    "source": "human",
                    "confidence": 1.0,
                    "human_override": 1,
                    "decision_id": a["decision_id"],
                }
            )
        rows.sort(key=lambda r: (r["scene_id"], r["character_id"]))
        return rows

    def get_conflicts(self, book_id: str) -> list[dict[str, Any]]:
        """Return generated/manual disagreements as conflict records.

        A conflict is recorded when a generated row and a manual row for the
        same ``(scene, character)`` disagree (and the character is not absent).
        The manual value remains effective; the conflict surfaces the request.
        """
        absences = self._active_absences(book_id)
        manual: dict[tuple[str, str], dict[str, Any]] = {
            (r["scene_id"], r["character_id"]): r
            for r in self._storage.execute_query(
                "SELECT character_id, scene_id, relation_type, decision_id"
                " FROM character_scene_manual WHERE book_id = ? AND relation_type != 'absent'",
                (book_id,),
            )
        }
        generated = self._storage.execute_query(
            "SELECT character_id, scene_id, relation_type, confidence,"
            " generation_revision FROM character_scene_generated WHERE book_id = ?",
            (book_id,),
        )
        conflicts: list[dict[str, Any]] = []
        for g in generated:
            key = (g["scene_id"], g["character_id"])
            if key in absences:
                continue
            m = manual.get(key)
            if m is not None and m["relation_type"] != g["relation_type"]:
                conflicts.append(
                    {
                        "code": "projection_disagreement",
                        "current_revision": g["generation_revision"],
                        "current_value": m["relation_type"],
                        "requested_value": g["relation_type"],
                        "decision_id": m["decision_id"],
                        "item_id": None,
                        "scene_id": g["scene_id"],
                        "character_id": g["character_id"],
                    }
                )
        return conflicts

    # ------------------------------------------------------------------
    # Alias conversion (preview -> commit -> reversible unmerge)
    # ------------------------------------------------------------------

    def _member_scene_keys(self, book_id: str, member_ids: list[str]) -> set[str]:
        """Return the scene IDs that reference any member character in *book_id*."""
        if not member_ids:
            return set()
        marks = ", ".join("?" for _ in member_ids)
        rows = self._storage.execute_query(
            f"""SELECT cs.scene_id AS scene_id
                  FROM character_scene cs
                  JOIN chapter_scene chs ON cs.scene_id = chs.child_id
                  JOIN book_chapter bc ON chs.parent_id = bc.child_id
                 WHERE bc.parent_id = ? AND cs.character_id IN ({marks})""",
            (book_id, *member_ids),
        )
        return {r["scene_id"] for r in rows}

    def _affected_rows(self, book_id: str, member_ids: list[str]) -> list[dict[str, Any]]:
        """Enumerate every junction/span/scene row touched by the members.

        Each row carries ``table``, ``character_id``, ``entity_id`` and, where
        applicable, ``relation_type``/``payload``.  The canonical key tuple
        ``(table, character_id, entity_id)`` is what preview/commit must agree
        on, so a stale commit can be rejected when the set diverges.
        """
        if not member_ids:
            return []
        marks = ", ".join("?" for _ in member_ids)
        affected: list[dict[str, Any]] = []
        # character_book (book-scoped)
        for r in self._storage.execute_query(
            f"""SELECT character_id, book_id AS entity_id, confidence
                  FROM character_book WHERE book_id = ? AND character_id IN ({marks})""",
            (book_id, *member_ids),
        ):
            affected.append(
                {
                    "table": "character_book",
                    "character_id": r["character_id"],
                    "entity_id": r["entity_id"],
                    "confidence": r["confidence"],
                }
            )
        # character_scene (book-scoped via spine)
        for r in self._storage.execute_query(
            f"""SELECT cs.character_id, cs.scene_id, cs.relation_type, cs.confidence
                  FROM character_scene cs
                  JOIN chapter_scene chs ON cs.scene_id = chs.child_id
                  JOIN book_chapter bc ON chs.parent_id = bc.child_id
                 WHERE bc.parent_id = ? AND cs.character_id IN ({marks})""",
            (book_id, *member_ids),
        ):
            affected.append(
                {
                    "table": "character_scene",
                    "character_id": r["character_id"],
                    "entity_id": r["scene_id"],
                    "relation_type": r["relation_type"],
                    "confidence": r["confidence"],
                }
            )
        # character_span (book-scoped via spine)
        for r in self._storage.execute_query(
            f"""SELECT cs.character_id, cs.span_id, cs.relation_type, cs.confidence
                  FROM character_span cs
                  JOIN paragraph_span psp ON cs.span_id = psp.child_id
                  JOIN scene_paragraph scp ON psp.parent_id = scp.child_id
                  JOIN chapter_scene chs ON scp.parent_id = chs.child_id
                  JOIN book_chapter bc ON chs.parent_id = bc.child_id
                 WHERE bc.parent_id = ? AND cs.character_id IN ({marks})""",
            (book_id, *member_ids),
        ):
            affected.append(
                {
                    "table": "character_span",
                    "character_id": r["character_id"],
                    "entity_id": r["span_id"],
                    "relation_type": r["relation_type"],
                    "confidence": r["confidence"],
                }
            )
        # Generated + manual workbench presence rows for members.
        for r in self._storage.execute_query(
            f"""SELECT character_id, scene_id, relation_type, confidence
                  FROM character_scene_generated
                 WHERE book_id = ? AND character_id IN ({marks})""",
            (book_id, *member_ids),
        ):
            affected.append(
                {
                    "table": "character_scene_generated",
                    "character_id": r["character_id"],
                    "entity_id": r["scene_id"],
                    "relation_type": r["relation_type"],
                    "confidence": r["confidence"],
                }
            )
        for r in self._storage.execute_query(
            f"""SELECT character_id, scene_id, relation_type
                  FROM character_scene_manual
                 WHERE book_id = ? AND character_id IN ({marks})""",
            (book_id, *member_ids),
        ):
            affected.append(
                {
                    "table": "character_scene_manual",
                    "character_id": r["character_id"],
                    "entity_id": r["scene_id"],
                    "relation_type": r["relation_type"],
                }
            )
        return affected

    def _protected_decisions(self, book_id: str, member_ids: list[str]) -> list[dict]:
        """Return active decisions referencing the members' presence/review keys."""
        rows = self._storage.execute_query(
            "SELECT decision_id, target_kind, target_key, decision_type, status,"
            " base_revision, created_ms"
            " FROM workbench_decision WHERE book_id = ? AND status = 'active'",
            (book_id,),
        )
        protected = []
        for r in rows:
            key = r["target_key"] or ""
            tokens = set(key.replace(",", ":").split(":"))
            if set(member_ids) & tokens:
                protected.append(r)
        return protected

    def preview_alias_conversion(
        self,
        *,
        book_id: str,
        canonical_id: str,
        member_ids: list[str],
        base_revision: int,
    ) -> dict[str, Any]:
        """Preview an alias conversion and return a short-lived preview token.

        The preview is book-scoped, single-use, and expires after ten minutes.
        It binds the exact member set and revision; commit re-derives the
        affected-row set and rejects the commit if it diverges.  The response
        enumerates every affected row, protected decisions, voice assignments,
        affected review items, downstream 2d invalidations, and conflicts.
        """
        self.require_book(book_id)
        self.check_revision(book_id, base_revision)
        if canonical_id in member_ids:
            raise ValidationError("canonical_id must not be a member")
        if not member_ids:
            raise ValidationError("member_ids must be non-empty")
        if len(set(member_ids)) != len(member_ids):
            raise ValidationError("member_ids must be distinct")
        known = {
            r["id"]
            for r in self._storage.execute_query(
                "SELECT id FROM character WHERE id IN (%s)"
                % ", ".join("?" for _ in [canonical_id, *member_ids]),
                (canonical_id, *member_ids),
            )
        }
        missing = {canonical_id, *member_ids} - known
        if missing:
            raise ValidationError(f"unknown characters: {sorted(missing)}")

        affected_rows = self._affected_rows(book_id, member_ids)
        protected = self._protected_decisions(book_id, member_ids)
        voice_assignments = [
            {
                "character_id": r["id"],
                "voice_assignment_id": r["voice_assignment_id"],
                "name": r["name"],
                "aliases": r["aliases"],
            }
            for r in self._storage.execute_query(
                "SELECT id, name, aliases, voice_assignment_id FROM character"
                " WHERE id IN (%s)" % ", ".join("?" for _ in member_ids),
                tuple(member_ids),
            )
        ]
        review_items = self._storage.execute_query(
            "SELECT id, kind, target_table, target_id, prior_value, status"
            " FROM walk_review_item WHERE book_id = ? AND status = 'pending'"
            " AND (target_id IN (%s) OR kind = 'alias_merge')"
            % ", ".join("?" for _ in member_ids),
            (book_id, *member_ids),
        )
        downstream_scenes = sorted(self._member_scene_keys(book_id, member_ids))
        downstream = [
            {"walk_name": "walk_2d_scene_presence", "scenes": downstream_scenes}
        ]
        conflicts = [
            c
            for c in self.get_conflicts(book_id)
            if c["character_id"] in member_ids
        ]

        token = f"ap-{secrets.token_urlsafe(18)}"
        payload = {
            "book_id": book_id,
            "canonical_id": canonical_id,
            "member_ids": sorted(set(member_ids)),
            "base_revision": base_revision,
            "expires_ms": _now_ms() + _PREVIEW_TTL_MS,
            "affected_keys": {
                (r["table"], r["character_id"], r["entity_id"])
                for r in affected_rows
            },
        }
        with self._previews_lock:
            self._previews[token] = payload
        return {
            "preview_token": token,
            "expires_ms": payload["expires_ms"],
            "base_revision": base_revision,
            "affected_rows": affected_rows,
            "protected_decisions": protected,
            "voice_assignments": voice_assignments,
            "review_items": review_items,
            "downstream_invalidations": downstream,
            "conflicts": conflicts,
        }

    def commit_alias_conversion(
        self,
        *,
        book_id: str,
        preview_token: str,
        base_revision: int,
        confirm_consequences: bool,
    ) -> dict[str, Any]:
        """Atomically apply an alias preview.

        Single-use: the token is consumed on commit (success or validation
        failure after re-derivation).  The current revision and the preview's
        bound revision must both equal *base_revision*, and the re-derived
        affected-row set must match the preview's, or the commit is rejected as
        stale.  A reversible decision record and per-member merge rows (with
        prior voice assignment + consequences) are written before any effect.
        """
        with self._previews_lock:
            payload = self._previews.pop(preview_token, None)
        if payload is None:
            raise PreviewExpiredError("preview token unknown or already used")
        if payload["book_id"] != book_id:
            raise PreviewExpiredError("preview is scoped to a different book")
        if payload["expires_ms"] < _now_ms():
            raise PreviewExpiredError("preview token expired")
        if payload["base_revision"] != base_revision:
            raise StaleRevisionError("preview bound to a different base_revision")
        if not confirm_consequences:
            raise ValidationError("confirm_consequences must be true to commit")
        self.check_revision(book_id, base_revision)
        current_keys = {
            (r["table"], r["character_id"], r["entity_id"])
            for r in self._affected_rows(book_id, payload["member_ids"])
        }
        if current_keys != payload["affected_keys"]:
            raise StaleRevisionError(
                "affected rows changed since preview; re-preview before commit"
            )

        canonical_id = payload["canonical_id"]
        member_ids = payload["member_ids"]
        consequence_json = json.dumps(
            {
                "affected_rows": [
                    {"table": t, "character_id": c, "entity_id": e}
                    for (t, c, e) in sorted(current_keys)
                ],
                "downstream_invalidations": {
                    "walk_2d_scene_presence": sorted(
                        self._member_scene_keys(book_id, member_ids)
                    )
                },
            }
        )
        merge_ids: list[str] = []
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            decision_id = self.record_decision(
                book_id=book_id,
                target_kind="alias_merge",
                target_key=f"{canonical_id}:{','.join(sorted(member_ids))}",
                decision_type="alias_merge:merge",
                base_revision=base_revision,
                payload={
                    "canonical_id": canonical_id,
                    "member_ids": member_ids,
                    "consequences": json.loads(consequence_json),
                },
                source="human",
            )
            for mid in member_ids:
                char = self._storage.execute_query(
                    "SELECT name, aliases, voice_assignment_id FROM character WHERE id = ?",
                    (mid,),
                )[0]
                merge_id = f"merge-{uuid4()}"
                merge_ids.append(merge_id)
                self._storage.execute_insert(
                    """INSERT INTO character_alias_merge
                           (merge_id, book_id, canonical_id, member_id,
                            merge_revision, decision_id, status, prior_member_name,
                            prior_member_aliases_json,
                            prior_member_voice_assignment_id,
                            consequence_json, created_ms)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
                    (
                        merge_id,
                        book_id,
                        canonical_id,
                        mid,
                        revision,
                        decision_id,
                        char["name"] or "",
                        char["aliases"] or "[]",
                        char["voice_assignment_id"],
                        consequence_json,
                        _now_ms(),
                    ),
                )
                self.record_provenance(
                    book_id=book_id,
                    target_kind="alias_merge",
                    target_key=merge_id,
                    generation_revision=revision,
                    source="human",
                )
            # Supersede pending review items whose target is a merged member so
            # the actionable queue no longer offers stale member targets.
            for row in self._storage.execute_query(
                "SELECT id FROM walk_review_item WHERE book_id = ?"
                " AND status = 'pending' AND target_id IN (%s)"
                % ", ".join("?" for _ in member_ids),
                (book_id, *member_ids),
            ):
                self._storage.execute_update(
                    "UPDATE walk_review_item SET status = 'superseded' WHERE id = ?",
                    (row["id"],),
                )
        return {
            "decision_id": decision_id,
            "merge_ids": merge_ids,
            "status": "active",
            "generation_revision": revision,
            "superseded_item_ids": [],
            "conflict": False,
        }

    def unmerge_alias(
        self, *, book_id: str, merge_id: str, base_revision: int
    ) -> dict[str, Any]:
        """Reverse an active alias merge.

        Creates an inverse decision, marks the merge ``undone``, restores the
        member's voice assignment only when no newer human assignment exists,
        and reactivates the review items the merge superseded.  Raises
        :class:`ConflictError` (409) when the merge is not active.
        """
        self.require_book(book_id)
        self.check_revision(book_id, base_revision)
        row = self._storage.execute_query(
            "SELECT * FROM character_alias_merge"
            " WHERE book_id = ? AND merge_id = ?",
            (book_id, merge_id),
        )
        if not row:
            raise ValidationError(f"unknown merge: {merge_id}")
        merge = row[0]
        if merge["status"] != "active":
            raise ConflictError("merge is not active; nothing to undo")

        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            decision_id = self.record_decision(
                book_id=book_id,
                target_kind="alias_merge",
                target_key=merge_id,
                decision_type="alias_merge:unmerge",
                base_revision=base_revision,
                payload={"merge_id": merge_id, "member_id": merge["member_id"]},
                source="human",
            )
            self._storage.execute_update(
                "UPDATE character_alias_merge SET status = 'undone' WHERE merge_id = ?",
                (merge_id,),
            )
            self._storage.execute_update(
                "UPDATE workbench_decision SET status = 'undone', undone_by = ?"
                " WHERE decision_id = ?",
                (decision_id, merge["decision_id"]),
            )
            # Restore prior voice assignment only if the member still holds the
            # merge-time assignment (i.e. no newer human assignment replaced it).
            current = self._storage.execute_query(
                "SELECT voice_assignment_id FROM character WHERE id = ?",
                (merge["member_id"],),
            )[0]["voice_assignment_id"]
            prior = merge["prior_member_voice_assignment_id"]
            if prior is not None and current == prior:
                self._storage.execute_update(
                    "UPDATE character SET voice_assignment_id = ? WHERE id = ?",
                    (merge["prior_member_voice_assignment_id"], merge["member_id"]),
                )
            # Reactivate review items this merge superseded.
            self._storage.execute_update(
                "UPDATE walk_review_item SET status = 'pending'"
                " WHERE book_id = ? AND target_id = ? AND status = 'superseded'",
                (book_id, merge["member_id"]),
            )
            self.record_provenance(
                book_id=book_id,
                target_kind="alias_merge",
                target_key=merge_id,
                generation_revision=revision,
                source="human",
            )
        return {
            "decision_id": decision_id,
            "merge_id": merge_id,
            "status": "undone",
            "generation_revision": revision,
            "conflict": False,
        }

    # ------------------------------------------------------------------
    # Boundary overrides
    # ------------------------------------------------------------------

    def _validate_boundary_anchor(
        self, book_id: str, anchor: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate a boundary anchor is non-empty and reachable within the book."""
        chapter_id = anchor.get("chapter_id")
        scene_id = anchor.get("scene_id")
        paragraph_id = anchor.get("paragraph_id")
        if not any((chapter_id, scene_id, paragraph_id)):
            raise ValidationError(
                "anchor must provide at least one of chapter_id, scene_id, paragraph_id"
            )
        if chapter_id:
            if not self._storage.execute_query(
                "SELECT child_id FROM book_chapter"
                " WHERE parent_id = ? AND child_id = ?",
                (book_id, chapter_id),
            ):
                raise ValidationError(
                    f"chapter {chapter_id} not reachable from book {book_id}"
                )
        if scene_id:
            if not self._storage.execute_query(
                "SELECT chs.child_id FROM chapter_scene chs"
                " JOIN book_chapter bc ON chs.parent_id = bc.child_id"
                " WHERE bc.parent_id = ? AND chs.child_id = ?",
                (book_id, scene_id),
            ):
                raise ValidationError(
                    f"scene {scene_id} not reachable from book {book_id}"
                )
        if paragraph_id:
            if not self._storage.execute_query(
                "SELECT child_id FROM scene_paragraph scp"
                " JOIN chapter_scene chs ON scp.parent_id = chs.child_id"
                " JOIN book_chapter bc ON chs.parent_id = bc.child_id"
                " WHERE bc.parent_id = ? AND scp.child_id = ?",
                (book_id, paragraph_id),
            ):
                raise ValidationError(
                    f"paragraph {paragraph_id} not reachable from book {book_id}"
                )
        return {
            "chapter_id": chapter_id,
            "scene_id": scene_id,
            "paragraph_id": paragraph_id,
        }

    @staticmethod
    def _validate_boundary_payload(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object")
        op = payload.get("operation")
        if op not in _BOUNDARY_OPS:
            raise ValidationError(f"operation must be one of {sorted(_BOUNDARY_OPS)}")
        offsets = payload.get("boundary_offsets")
        if not isinstance(offsets, list) or not offsets:
            raise ValidationError("boundary_offsets must be a non-empty list of ints")
        if not all(isinstance(o, int) and not isinstance(o, bool) and o >= 0 for o in offsets):
            raise ValidationError("boundary_offsets must be non-negative integers")

    def get_boundary_overrides(self, book_id: str) -> list[dict[str, Any]]:
        """Return the active boundary overrides for *book_id* as DTOs."""
        self.require_book(book_id)
        revision = self.get_revision(book_id)
        rows = self._storage.execute_query(
            "SELECT override_id, book_id, chapter_id, scene_id, paragraph_id,"
            " decision_id, payload_json, active, created_ms"
            " FROM boundary_override WHERE book_id = ? AND active = 1",
            (book_id,),
        )
        dtos = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            dtos.append(
                {
                    "override_id": r["override_id"],
                    "book_id": r["book_id"],
                    "anchor": {
                        "chapter_id": r["chapter_id"],
                        "scene_id": r["scene_id"],
                        "paragraph_id": r["paragraph_id"],
                    },
                    "payload": payload,
                    "decision_id": r["decision_id"],
                    "active": bool(r["active"]),
                    "created_ms": r["created_ms"],
                    "generation_revision": revision,
                }
            )
        return dtos

    def put_boundary_override(
        self,
        *,
        book_id: str,
        override_id: str | None = None,
        anchor: dict[str, Any],
        payload: dict[str, Any],
        base_revision: int,
    ) -> dict[str, Any]:
        """Create or replace a boundary override for *book_id*.

        The anchor must be reachable within the book and the payload must match
        the registered boundary DTO shape.  The override is stored active with a
        new decision and revision.  A provided ``override_id`` replaces that
        override (keeping identity) instead of inserting a duplicate.
        """
        self.require_book(book_id)
        self.check_revision(book_id, base_revision)
        clean_anchor = self._validate_boundary_anchor(book_id, anchor)
        self._validate_boundary_payload(payload)
        target_id = override_id or f"bo-{uuid4()}"
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            decision_id = self.record_decision(
                book_id=book_id,
                target_kind="boundary",
                target_key=target_id,
                decision_type="boundary:override",
                base_revision=base_revision,
                payload={
                    "anchor": clean_anchor,
                    "payload": payload,
                },
                source="human",
            )
            self._storage.execute_insert(
                """INSERT INTO boundary_override
                       (override_id, book_id, chapter_id, scene_id, paragraph_id,
                        decision_id, payload_json, active, created_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT (override_id)
                   DO UPDATE SET chapter_id = excluded.chapter_id,
                                 scene_id = excluded.scene_id,
                                 paragraph_id = excluded.paragraph_id,
                                 decision_id = excluded.decision_id,
                                 payload_json = excluded.payload_json,
                                 active = 1""",
                (
                    target_id,
                    book_id,
                    clean_anchor["chapter_id"],
                    clean_anchor["scene_id"],
                    clean_anchor["paragraph_id"],
                    decision_id,
                    json.dumps(payload),
                    _now_ms(),
                ),
            )
            self.record_provenance(
                book_id=book_id,
                target_kind="boundary",
                target_key=target_id,
                generation_revision=revision,
                source="human",
            )
        return {
            "override_id": target_id,
            "book_id": book_id,
            "anchor": clean_anchor,
            "payload": payload,
            "decision_id": decision_id,
            "active": True,
            "generation_revision": revision,
        }

    def apply_boundary_override(
        self, *, book_id: str, override_id: str
    ) -> dict[str, Any]:
        """Atomically apply an active boundary override.

        Records the application decision + provenance and allocates a revision;
        the actual segmentation/merge/resegment mutation is owned by the
        pipeline operation layer.  Returns the (still active) override DTO with
        the new revision.
        """
        self.require_book(book_id)
        row = self._storage.execute_query(
            "SELECT * FROM boundary_override"
            " WHERE book_id = ? AND override_id = ? AND active = 1",
            (book_id, override_id),
        )
        if not row:
            raise ValidationError(
                f"no active boundary override {override_id} for book {book_id}"
            )
        override = row[0]
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            decision_id = self.record_decision(
                book_id=book_id,
                target_kind="boundary",
                target_key=override_id,
                decision_type="boundary:apply",
                base_revision=revision - 1,
                payload={
                    "chapter_id": override["chapter_id"],
                    "scene_id": override["scene_id"],
                    "paragraph_id": override["paragraph_id"],
                    "payload": json.loads(override["payload_json"] or "{}"),
                },
                source="human",
            )
            self.record_provenance(
                book_id=book_id,
                target_kind="boundary",
                target_key=override_id,
                generation_revision=revision,
                source="human",
            )
        return {
            "override_id": override_id,
            "book_id": book_id,
            "decision_id": decision_id,
            "active": True,
            "generation_revision": revision,
        }

    def deactivate_boundary_override(
        self, *, book_id: str, override_id: str, base_revision: int
    ) -> dict[str, Any]:
        """Deactivate a boundary override while retaining its decision/provenance.

        The override row is not deleted; ``active`` is set to 0 and an inverse
        decision + provenance are recorded.  Returns the inactive DTO and the
        new revision.
        """
        self.require_book(book_id)
        self.check_revision(book_id, base_revision)
        row = self._storage.execute_query(
            "SELECT * FROM boundary_override"
            " WHERE book_id = ? AND override_id = ? AND active = 1",
            (book_id, override_id),
        )
        if not row:
            raise ValidationError(
                f"no active boundary override {override_id} for book {book_id}"
            )
        override = row[0]
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            decision_id = self.record_decision(
                book_id=book_id,
                target_kind="boundary",
                target_key=override_id,
                decision_type="boundary:deactivate",
                base_revision=base_revision,
                payload={"override_id": override_id},
                source="human",
            )
            self._storage.execute_update(
                "UPDATE boundary_override SET active = 0 WHERE override_id = ?",
                (override_id,),
            )
            self.record_provenance(
                book_id=book_id,
                target_kind="boundary",
                target_key=override_id,
                generation_revision=revision,
                source="human",
            )
        return {
            "override_id": override_id,
            "book_id": book_id,
            "anchor": {
                "chapter_id": override["chapter_id"],
                "scene_id": override["scene_id"],
                "paragraph_id": override["paragraph_id"],
            },
            "payload": json.loads(override["payload_json"] or "{}"),
            "decision_id": decision_id,
            "active": False,
            "generation_revision": revision,
        }

    # ------------------------------------------------------------------
    # Effective configuration model
    # ------------------------------------------------------------------

    def get_overrides(self, book_id: str) -> list[dict[str, Any]]:
        """Return the DB-tier walk override rows for *book_id*."""
        self.require_book(book_id)
        rows = self._storage.get_walk_overrides(book_id)
        out = []
        for r in rows:
            try:
                value = json.loads(r["value_json"])
            except (json.JSONDecodeError, TypeError):
                value = None
            out.append(
                {
                    "walk_name": r["walk_name"],
                    "key": r["key"],
                    "value": value,
                }
            )
        return out

    @staticmethod
    def _validate_override_value(key: str, value: Any) -> Any:
        """Validate a typed override value; return a normalized copy."""
        if key == "model_name":
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("model_name must be a non-empty string")
            return value.strip()
        if key == "reasoning_effort":
            if value not in (None,) and value not in _REASONING_EFFORTS:
                raise ValidationError(
                    f"reasoning_effort must be one of {sorted(_REASONING_EFFORTS)}"
                )
            return value
        if key == "temperature":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (0.0 <= float(value) <= 1.0)
            ):
                raise ValidationError("temperature must be a number in [0.0, 1.0]")
            return float(value)
        if key == "prompt":
            if not isinstance(value, str):
                raise ValidationError("prompt must be a string")
            if len(value) > _MAX_PROMPT_LEN:
                raise ValidationError(
                    f"prompt exceeds {_MAX_PROMPT_LEN} characters"
                )
            return value
        raise ValidationError(f"unsupported override key: {key}")

    def put_override(
        self,
        *,
        book_id: str,
        walk_name: str,
        key: str,
        value: Any,
        base_revision: int,
    ) -> dict[str, Any]:
        """Set a DB-tier walk override for one field, returning the effective value.

        Only approved keys/types for 2b/2c/2d are accepted; malformed values are
        rejected before persistence.  A new revision is allocated so the write is
        concurrency-safe.
        """
        self.require_book(book_id)
        if walk_name not in _WORKBENCH_WALK_NAMES:
            raise ValidationError(f"unsupported walk_name: {walk_name}")
        if key not in _ALLOWED_OVERRIDE_KEYS:
            raise ValidationError(
                f"unsupported override key {key}; allowed {sorted(_ALLOWED_OVERRIDE_KEYS)}"
            )
        clean = self._validate_override_value(key, value)
        self.check_revision(book_id, base_revision)
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            self._storage.upsert_walk_override(
                book_id, walk_name, key, json.dumps(clean)
            )
            self.record_decision(
                book_id=book_id,
                target_kind="review",
                target_key=f"{walk_name}:{key}",
                decision_type="override:set",
                base_revision=base_revision,
                payload={"key": key, "value": clean},
                source="human",
            )
        effective = self.resolve_effective_config(book_id, walk_name)
        return {
            "walk_name": walk_name,
            "key": key,
            "value": clean,
            "source": effective["sources"][key],
            "generation_revision": revision,
        }

    def delete_override(
        self, *, book_id: str, walk_name: str, key: str, base_revision: int
    ) -> dict[str, Any]:
        """Remove the DB-tier override for one field, returning the new effective value."""
        self.require_book(book_id)
        if walk_name not in _WORKBENCH_WALK_NAMES:
            raise ValidationError(f"unsupported walk_name: {walk_name}")
        if key not in _ALLOWED_OVERRIDE_KEYS:
            raise ValidationError(f"unsupported override key: {key}")
        self.check_revision(book_id, base_revision)
        with self._storage.transaction():
            revision = self.allocate_revision(book_id)
            self._storage.delete_walk_override(book_id, walk_name, key)
            self.record_decision(
                book_id=book_id,
                target_kind="review",
                target_key=f"{walk_name}:{key}",
                decision_type="override:delete",
                base_revision=base_revision,
                payload={"key": key},
                source="human",
            )
        effective = self.resolve_effective_config(book_id, walk_name)
        return {
            "walk_name": walk_name,
            "key": key,
            "value": effective["values"][key],
            "source": effective["sources"][key],
            "generation_revision": revision,
        }

    def _source_for(self, field: str, walk_name: str, book_id: str) -> str:
        """Return the source tier that currently provides *field*'s value.

        Mirrors the precedence in ``resolve_task_config``: the DB ``walk_override``
        row wins; ``prompt`` then falls through config top-level
        ``walk_override[task].prompt`` -> ``llm.task_overrides[task].prompt``;
        model/reasoning/temperature fall through ``task_overrides`` -> global
        ``llm`` -> hardcoded fallback.  Empty/non-string values fall through.
        """
        row = None
        try:
            rows = self._storage.execute_query(
                "SELECT key, value_json FROM walk_override"
                " WHERE book_id = ? AND walk_name = ? AND key = ?",
                (book_id, walk_name, field),
            )
            row = rows[0] if rows else None
        except Exception:
            row = None
        if row is not None:
            try:
                value = json.loads(row["value_json"])
            except (json.JSONDecodeError, TypeError):
                value = None
            if field == "temperature":
                if value is not None:
                    return "row"
            elif field == "prompt":
                if isinstance(value, str) and value:
                    return "row"
            elif value:
                return "row"

        config = load_app_config() or {}
        llm = config.get("llm", {})
        task_overrides = llm.get("task_overrides", {})
        task_override = (
            task_overrides.get(walk_name, {})
            if isinstance(task_overrides, dict)
            else {}
        )
        if field == "prompt":
            config_walk = config.get("walk_override", {})
            config_task = (
                config_walk.get(walk_name, {}) if isinstance(config_walk, dict) else {}
            )
            if isinstance(config_task.get("prompt"), str) and config_task["prompt"]:
                return "config"
            if isinstance(task_override.get("prompt"), str) and task_override["prompt"]:
                return "task"
            if isinstance(llm.get("prompt"), str) and llm["prompt"]:
                return "global"
            return "fallback"
        if field == "temperature":
            if task_override.get("temperature") is not None:
                return "task"
            if llm.get("temperature") is not None:
                return "global"
            return "fallback"
        # model_name / reasoning_effort use truthiness.
        if task_override.get(field):
            return "task"
        if llm.get(field):
            return "global"
        return "fallback"

    def resolve_effective_config(
        self, book_id: str, walk_name: str
    ) -> dict[str, Any]:
        """Return the effective config for *walk_name* plus per-field sources.

        Values reuse ``resolve_task_config`` so the workbench display always
        matches what the walk runner actually uses; ``sources`` reports the
        resolving tier per field (``row``/``config``/``task``/``global``/``fallback``).
        """
        values = resolve_task_config(walk_name, self._storage, book_id)
        sources = {
            field: self._source_for(field, walk_name, book_id)
            for field in ("model_name", "reasoning_effort", "temperature", "prompt")
        }
        return {"book_id": book_id, "walk_name": walk_name, "values": values, "sources": sources}
