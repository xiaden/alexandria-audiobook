"""Walk runner infrastructure for the audiobook pipeline.

Provides ``WalkRunner`` which orchestrates serial execution of walk modules.
Each walk module lives under ``app.pipeline.walks`` and exposes an
``execute(book_id, storage, config)`` function. Walks run one at a time;
each consumes the prior walk's output.

Walk status is tracked in-memory (no schema change) with states:
pending → running → completed | failed.
"""

from __future__ import annotations

import importlib
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

logger = logging.getLogger(__name__)

# Type alias for verification functions.
# Signature: (book_id, storage) -> bool
VerifyFn = Callable[[str, "PipelineStorage"], bool]


def _verify_walk_2a(book_id: str, storage: "PipelineStorage") -> bool:
    """Verify that walk_2a_scene_segmentation produced scenes for chapters."""
    rows = storage.execute_query(
        "SELECT COUNT(*) AS cnt FROM chapter_scene "
        "WHERE parent_id IN "
        "(SELECT id FROM chapter WHERE book_id = ?)",
        (book_id,),
    )
    scene_count = rows[0]["cnt"] if rows else 0
    return scene_count > 0


def _verify_walk_2b(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2c(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2d(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2e(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2f(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2g(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2h(book_id: str, storage: "PipelineStorage") -> bool:
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


def _verify_walk_2i(book_id: str, storage: "PipelineStorage") -> bool:
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


class WalkRunner:
    """Orchestrates serial execution of pipeline walk modules.

    Walks are loaded dynamically by name from ``app.pipeline.walks`` and
    executed one at a time. Status is tracked in-memory per book.

    Parameters
    ----------
    storage:
        Pipeline storage adapter for database operations.
    """

    # Canonical walk execution order.
    WALK_ORDER: list[str] = [
        "walk_2a_scene_segmentation",
        "walk_2b_character_discovery",
        "walk_2c_alias_resolution",
        "walk_2d_scene_presence",
        "walk_2e_span_attribution",
        "walk_2f_character_description",
        "walk_2g_voice_audition",
        "walk_2h_voice_assignment",
        "walk_2i_delivery",
    ]

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage
        # {book_id: OrderedDict(walk_name -> status)}
        self._status: dict[str, OrderedDict[str, str]] = {}

    def run_walk(
        self, walk_name: str, book_id: str, config: dict
    ) -> dict:
        """Execute a single walk by name.

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
            error dict with ``status='failed'`` on failure.
        """
        self._ensure_book(book_id)
        if self._get_status(book_id, walk_name) == "running":
            return {
                "status": "failed",
                "error": f"Walk '{walk_name}' is already running for book '{book_id}'",
            }
        self._set_status(book_id, walk_name, "running")
        logger.info("Starting walk '%s' for book '%s'", walk_name, book_id)
        try:
            walk_module = self._load_walk_module(walk_name)
        except ImportError as exc:
            self._set_status(book_id, walk_name, "failed")
            logger.error("Failed to import walk '%s': %s", walk_name, exc)
            return {"status": "failed", "error": str(exc)}
        try:
            result = walk_module.execute(book_id, self._storage, config)
        except Exception as exc:
            self._set_status(book_id, walk_name, "failed")
            logger.error(
                "Walk '%s' raised exception for book '%s': %s",
                walk_name,
                book_id,
                exc,
            )
            return {"status": "failed", "error": str(exc)}
        if not self._run_verification(walk_name, book_id):
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
        self._set_status(book_id, walk_name, "completed")
        logger.info("Completed walk '%s' for book '%s'", walk_name, book_id)
        return result

    def run_all_walks(self, book_id: str, config: dict) -> dict:
        """Execute all walks in canonical order for a book.

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
        for walk_name in self.WALK_ORDER:
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

    def get_walk_status(self, book_id: str, walk_name: str) -> str:
        """Return the current status of a walk for a book.

        Returns ``'pending'`` if the walk has not been initialized for
        this book.
        """
        return self._get_status(book_id, walk_name)

    # -- Internal helpers ---------------------------------------------------

    def _ensure_book(self, book_id: str) -> None:
        """Initialize status tracking for a book if not already present."""
        if book_id not in self._status:
            statuses: OrderedDict[str, str] = OrderedDict()
            for walk_name in self.WALK_ORDER:
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
