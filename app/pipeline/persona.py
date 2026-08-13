"""Persona domain helper for the pipeline (DD-voice-persona-prompt-parity).

Owns the persona-revision contract enforced at the domain layer:

* **Owner/book bounded** — every persona write references a real character (and
  optionally a real book) from the ledger; unknown characters/books raise
  ``CharacterNotFoundError`` / ``BookNotFoundError`` (HTTP 404).
* **Append-only + revision-checked** — a persona is a chain of immutable
  revisions.  A new write cites its ``base_revision`` and is rejected with
  ``StaleRevisionError`` (HTTP 409) when it does not match the current head; the
  prior record is preserved and marked ``superseded_by``.
* **Protected records** — a ``protected`` revision cannot be replaced by a
  ``source="rerun"`` write (``ProtectedRevisionError``); a ``source="human"``
  write may supersede it but must carry forward the protected flag and evidence.
* **Deterministic rejection** — invalid anchors, aliases, scene scopes, and
  review states raise ``ValidationError`` (HTTP 422).
* **Derived voice consequences** — ``voice_consequences`` is explainable output
  (style/instruction implications) computed from the profile fields; it never
  assigns a resolved ``voice_config`` id.

The domain talks to ``PipelineStorage`` exactly like ``Workbench`` does; a
``storage`` without ``persona_revision``/``prompt_config_revision`` tables
degrades gracefully through the adapter's ``execute_*`` contract.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from app.pipeline.adapter import PipelineStorage

# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class PersonaError(Exception):
    """Base class for persona domain errors."""


class CharacterNotFoundError(PersonaError):
    """The referenced character does not exist (HTTP 404)."""


class BookNotFoundError(PersonaError):
    """The referenced book does not exist (HTTP 404)."""


class ValidationError(PersonaError):
    """Malformed or unsupported persona input (HTTP 422)."""


class StaleRevisionError(PersonaError):
    """A ``base_revision`` did not match the current persona head (HTTP 409)."""


class ProtectedRevisionError(PersonaError):
    """A protected revision cannot be replaced by a rerun write (HTTP 409)."""


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

_PERSONA_FIELD_KEYS = frozenset(
    ("identity", "appearance", "manner", "speech", "role")
)
_REVIEW_STATES = frozenset(("draft", "needs_review", "accepted", "rejected"))
_SCOPE_TYPES = frozenset(("book", "scenes"))
_ALIAS_MAX = 200
_FIELD_MAX = 4000
_EVIDENCE_MAX = 20
# Scene scope lives inside ``fields_json`` under this reserved key because the
# registered schema has no dedicated ``scene_ids`` column.
_SCENE_IDS_KEY = "scene_ids"
_DEFAULT_AUTHOR = "local"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_truthy_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class PersonaDomain:
    """Storage/domain facade for the persona-revision contract.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation (``SQLiteAdapter`` or
        ``InMemorySQLiteAdapter``).
    """

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Ledger scope helpers
    # ------------------------------------------------------------------

    def require_character(self, character_id: str) -> None:
        """Raise :class:`CharacterNotFoundError` if *character_id* is unknown."""
        if not self._storage.execute_query(
            "SELECT id FROM character WHERE id = ?", (character_id,)
        ):
            raise CharacterNotFoundError(f"unknown character: {character_id}")

    def require_book(self, book_id: str) -> None:
        """Raise :class:`BookNotFoundError` if *book_id* is unknown."""
        if not self._storage.execute_query(
            "SELECT id FROM book WHERE id = ?", (book_id,)
        ):
            raise BookNotFoundError(f"unknown book: {book_id}")

    # ------------------------------------------------------------------
    # Reachability
    # ------------------------------------------------------------------

    def get_stable_anchor_ids(self, book_id: str) -> set[str]:
        """Return the set of reachable stable ``span_id`` anchors for *book_id*."""
        rows = self._storage.execute_query(
            """SELECT sp.id AS span_id
                 FROM book_chapter bc
                 JOIN chapter_scene cs ON cs.parent_id = bc.child_id
                 JOIN scene_paragraph scp ON scp.parent_id = cs.child_id
                 JOIN paragraph_span psp ON psp.parent_id = scp.child_id
                 JOIN span sp ON sp.id = psp.child_id
                WHERE bc.parent_id = ?""",
            (book_id,),
        )
        return {row["span_id"] for row in rows}

    def get_reachable_scene_ids(self, book_id: str) -> set[str]:
        """Return the set of ``scene_id`` values reachable from *book_id*."""
        rows = self._storage.execute_query(
            """SELECT cs.child_id AS scene_id
                 FROM book_chapter bc
                 JOIN chapter_scene cs ON cs.parent_id = bc.child_id
                WHERE bc.parent_id = ?""",
            (book_id,),
        )
        return {row["scene_id"] for row in rows}

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_fields(self, fields: Any) -> None:
        if not isinstance(fields, dict):
            raise ValidationError("fields must be an object")
        unknown = set(fields) - _PERSONA_FIELD_KEYS
        if unknown:
            raise ValidationError(
                f"unknown persona field(s): {sorted(unknown)}"
            )
        for key, value in fields.items():
            if not _is_truthy_str(value):
                raise ValidationError(f"persona field '{key}' must be a non-empty string")
            if len(value) > _FIELD_MAX:
                raise ValidationError(f"persona field '{key}' exceeds {_FIELD_MAX} chars")

    def _validate_evidence(self, evidence: Any, book_id: str | None) -> None:
        if not isinstance(evidence, list):
            raise ValidationError("evidence must be a list")
        if len(evidence) > _EVIDENCE_MAX:
            raise ValidationError(f"evidence exceeds {_EVIDENCE_MAX} entries")
        anchors = self.get_stable_anchor_ids(book_id) if book_id else set()
        for item in evidence:
            if not isinstance(item, dict) or not _is_truthy_str(item.get("anchor")):
                raise ValidationError("each evidence entry needs a non-empty 'anchor'")
            if book_id and item["anchor"] not in anchors:
                raise ValidationError(
                    f"evidence anchor '{item['anchor']}' is not reachable in book '{book_id}'"
                )

    def _validate_aliases(self, aliases: Any) -> None:
        if not isinstance(aliases, list):
            raise ValidationError("aliases must be a list")
        for alias in aliases:
            if not _is_truthy_str(alias):
                raise ValidationError("aliases must be non-empty strings")

    def _validate_scene_scope(self, write: dict) -> None:
        scope = write.get("scene_scope")
        if scope not in _SCOPE_TYPES:
            raise ValidationError(
                f"scene_scope must be one of {sorted(_SCOPE_TYPES)}"
            )
        scene_ids = write.get("scene_ids") or []
        if not isinstance(scene_ids, list):
            raise ValidationError("scene_ids must be a list")
        if scope == "scenes":
            if not scene_ids:
                raise ValidationError("scenes scope requires at least one scene_id")
            book_id = write.get("book_id")
            reachable = self.get_reachable_scene_ids(book_id) if book_id else set()
            for scene_id in scene_ids:
                if book_id and scene_id not in reachable:
                    raise ValidationError(
                        f"scene '{scene_id}' is not reachable in book '{book_id}'"
                    )

    def _validate_review_state(self, review_state: Any) -> None:
        if review_state not in _REVIEW_STATES:
            raise ValidationError(
                f"review_state must be one of {sorted(_REVIEW_STATES)}"
            )

    def _derive_voice_consequences(self, fields: dict) -> dict:
        """Derive explainable voice consequences from profile fields.

        Never assigns a resolved ``voice_config`` id — the caller (persona
        editor) maps consequences to a voice decision separately.
        """
        explanations = []
        for key in ("identity", "appearance", "manner", "speech", "role"):
            value = fields.get(key)
            if _is_truthy_str(value):
                explanations.append(f"{key}: {value}")
        explanation = "; ".join(explanations) if explanations else "no profile fields"
        return {
            "assignment": None,  # never an implicit voice assignment
            "explanation": explanation,
            "style_hints": [
                f"delivery should reflect persona field '{key}'"
                for key in fields
            ],
        }

    def validate(self, write: dict) -> dict:
        """Side-effect-free validation returning ``{valid, errors, voice_consequences}``."""
        errors: list[str] = []
        try:
            self._validate_fields(write.get("fields"))
        except ValidationError as exc:
            errors.append(str(exc))
        try:
            self._validate_evidence(write.get("evidence"), write.get("book_id"))
        except ValidationError as exc:
            errors.append(str(exc))
        try:
            self._validate_aliases(write.get("aliases"))
        except ValidationError as exc:
            errors.append(str(exc))
        try:
            self._validate_scene_scope(write)
        except ValidationError as exc:
            errors.append(str(exc))
        try:
            self._validate_review_state(write.get("review_state"))
        except ValidationError as exc:
            errors.append(str(exc))
        fields = write.get("fields") if isinstance(write.get("fields"), dict) else {}
        return {
            "valid": not errors,
            "errors": errors,
            "voice_consequences": self._derive_voice_consequences(fields),
        }

    def _current_head(self, character_id: str) -> dict | None:
        rows = self._storage.execute_query(
            "SELECT persona_id, revision, protected FROM persona_revision"
            " WHERE character_id = ? AND superseded_by IS NULL"
            " ORDER BY revision DESC LIMIT 1",
            (character_id,),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _decode(self, row: dict) -> dict:
        fields = json.loads(row["fields_json"] or "{}")
        scene_ids = fields.pop(_SCENE_IDS_KEY, None) if isinstance(fields, dict) else None
        return {
            "persona_id": row["persona_id"],
            "character_id": row["character_id"],
            "book_id": row["book_id"],
            "revision": row["revision"],
            "fields": fields,
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "aliases": json.loads(row["aliases_json"] or "[]"),
            "scene_scope": row["scene_scope"],
            "scene_ids": scene_ids or [],
            "review_state": row["review_state"],
            "protected": bool(row["protected"]),
            "voice_consequences": json.loads(row["voice_consequences_json"] or "{}"),
            "author_id": row["author_id"],
            "created_ms": row["created_ms"],
            "superseded_by": row["superseded_by"],
        }

    def get_revision(self, persona_id: str) -> dict | None:
        """Return a decoded persona revision, or ``None``."""
        rows = self._storage.execute_query(
            "SELECT persona_id, character_id, book_id, revision, fields_json,"
            " evidence_json, aliases_json, scene_scope, review_state, protected,"
            " voice_consequences_json, author_id, created_ms, superseded_by"
            " FROM persona_revision WHERE persona_id = ?",
            (persona_id,),
        )
        return self._decode(rows[0]) if rows else None

    def list_revisions(self, character_id: str) -> list[dict]:
        """Return all revisions for *character_id*, newest first."""
        rows = self._storage.execute_query(
            "SELECT persona_id, character_id, book_id, revision, fields_json,"
            " evidence_json, aliases_json, scene_scope, review_state, protected,"
            " voice_consequences_json, author_id, created_ms, superseded_by"
            " FROM persona_revision WHERE character_id = ?"
            " ORDER BY revision DESC",
            (character_id,),
        )
        return [self._decode(row) for row in rows]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(
        self,
        write: dict,
        *,
        base_revision: int,
        source: str = "human",
        author_id: str = _DEFAULT_AUTHOR,
    ) -> dict:
        """Append a new persona revision, superseding the current head.

        * ``source="human"`` may supersede a protected head, carrying forward
          the protected flag and evidence.
        * ``source="rerun"`` is rejected against a protected head
          (:class:`ProtectedRevisionError`).
        """
        character_id = write["character_id"]
        self.require_character(character_id)
        book_id = write.get("book_id")
        if book_id:
            self.require_book(book_id)

        fields = write.get("fields", {})
        self._validate_fields(fields)
        self._validate_evidence(write.get("evidence", []), book_id)
        self._validate_aliases(write.get("aliases", []))
        self._validate_scene_scope(write)
        self._validate_review_state(write.get("review_state"))

        head = self._current_head(character_id)
        current = int(head["revision"]) if head else 0
        if base_revision != current:
            raise StaleRevisionError(
                f"stale base_revision {base_revision} for character {character_id};"
                f" current revision {current}"
            )
        if head and bool(head["protected"]) and source == "rerun":
            raise ProtectedRevisionError(
                f"protected persona revision '{head['persona_id']}' cannot be"
                " replaced by a rerun"
            )

        evidence = write.get("evidence", [])
        protected = write.get("protected", False)
        # Human edits preserve evidence + protection from the protected head.
        if head and source == "human" and bool(head["protected"]):
            if not evidence:
                head_row = self.get_revision(head["persona_id"])
                evidence = head_row["evidence"] if head_row else evidence
            protected = True

        next_revision = current + 1
        persona_id = "persona-" + uuid4().hex
        scene_scope = write.get("scene_scope", "book")
        scene_ids = write.get("scene_ids") or []
        fields_json = dict(fields)
        if scene_scope == "scenes":
            fields_json[_SCENE_IDS_KEY] = scene_ids
        now = _now_ms()

        with self._storage.transaction():
            self._storage.execute_insert(
                "INSERT INTO persona_revision (persona_id, character_id, book_id,"
                " revision, fields_json, evidence_json, aliases_json, scene_scope,"
                " review_state, protected, voice_consequences_json, author_id,"
                " created_ms, superseded_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    persona_id,
                    character_id,
                    book_id,
                    next_revision,
                    json.dumps(fields_json),
                    json.dumps(evidence),
                    json.dumps(write.get("aliases", [])),
                    scene_scope,
                    write.get("review_state", "draft"),
                    1 if protected else 0,
                    json.dumps(self._derive_voice_consequences(fields)),
                    author_id,
                    now,
                    None,
                ),
            )
            if head:
                self._storage.execute_update(
                    "UPDATE persona_revision SET superseded_by = ?"
                    " WHERE persona_id = ?",
                    (persona_id, head["persona_id"]),
                )
        return self.get_revision(persona_id)
