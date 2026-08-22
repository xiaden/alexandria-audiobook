"""Walk 2d: Scene presence refinement.

Refines ``character_scene`` junctions by re-checking character presence in each
scene with additional context.  Walk 2b created initial ``character_scene``
junctions with ``relation_type='present'`` during character discovery.  Walk 2d
re-evaluates presence with the full list of known characters (names + UUIDs)
so the LLM can identify characters that Walk 2b missed.

For each scene, the walk:
1. Queries existing characters for the book (name + UUID).
2. Queries scene paragraphs (text).
3. Sends both to the LLM asking which characters are present.
4. Parses the response (list of character UUIDs with confidence).
5. Reconciles generated ``character_scene`` junctions with the complete result.

Confidence filter:
- ≥0.7: auto-accept (junction created)
- <0.5: auto-reject (junction skipped)
- 0.5–0.7: flagged for user review (junction created but tracked)

LLM configuration is resolved via ``resolve_task_config('scene_presence', storage, book_id)``
with temperature=0.1 for format stability.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from ._llm_helpers import chat_completion, extract_json_from_llm_response

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute(book_id: str, storage: PipelineStorage, config: dict[str, Any]) -> dict:
    """Run Walk 2d scene presence refinement for a book.

    Queries scenes for the book, sends each scene's paragraph text along with
    the list of existing characters to an LLM for presence identification,
    parses the response, and creates missing ``character_scene`` junctions.

    Parameters
    ----------
    book_id:
        UUID of the book to process.
    storage:
        Pipeline storage adapter.
    config:
        App config dict (kept for the runner contract; not consulted by ``resolve_task_config``).

    Returns
    -------
    dict:
        Summary with keys: ``book_id``, ``scenes_processed``,
        ``junctions_created``, ``junctions_for_review``, ``errors``.
    """
    from app.utils import create_llm_client, resolve_task_config

    result: dict[str, Any] = {
        "book_id": book_id,
        "scenes_processed": 0,
        "junctions_created": 0,
        "junctions_for_review": 0,
        "errors": [],
    }

    # Resolve LLM config for scene presence
    llm_config = resolve_task_config("scene_presence", storage, book_id)
    client, _ = create_llm_client(config_path=None)
    model_name = llm_config["model_name"]
    temperature = llm_config["temperature"]
    reasoning_effort = llm_config.get("reasoning_effort")
    prompt = llm_config.get("prompt")

    # Look up book existence
    book_rows = storage.execute_query(
        "SELECT series_id FROM book WHERE id = ?", (book_id,)
    )
    if not book_rows:
        logger.error(f"Book {book_id} not found")
        result["errors"].append({"book_id": book_id, "error": "Book not found"})
        return result

    # Load existing characters for this book (name + UUID)
    existing_characters = _load_existing_characters(book_id, storage)

    # Query scenes for this book (ordered by chapter, then position)
    scenes = storage.execute_query(
        """
        SELECT cs.child_id AS scene_id
        FROM chapter_scene cs
        JOIN chapter c ON cs.parent_id = c.id
        WHERE c.book_id = ?
        ORDER BY c.id, cs.position
        """,
        (book_id,),
    )

    for scene_row in scenes:
        scene_id = scene_row["scene_id"]
        result["scenes_processed"] += 1

        try:
            _process_scene(
                scene_id=scene_id,
                book_id=book_id,
                storage=storage,
                client=client,
                model_name=model_name,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                system_prompt_override=prompt,
                existing_characters=existing_characters,
                result=result,
            )
        except Exception as e:  # noqa: BLE001 — per-item error isolation: any error must log, record, and continue
            logger.error(f"Error processing scene {scene_id}: {e}")
            result["errors"].append({"scene_id": scene_id, "error": str(e)})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Workbench-native helpers
# ---------------------------------------------------------------------------


# Invalidation DAG: 2d invalidates no upstream walk.
_DOWNSTREAM_WALKS: tuple[str, ...] = ()


def _active_absence(storage, book_id, scene_id, character_id):
    """True when an active human absence tombstone protects this pair."""
    rows = storage.execute_query(
        "SELECT 1 FROM character_scene_absence "
        "WHERE book_id = ? AND scene_id = ? AND character_id = ? AND active = 1",
        (book_id, scene_id, character_id),
    )
    return bool(rows)


def _generation_revision(storage, book_id):
    """Read the current per-book workbench generation revision (0 when unset)."""
    rows = storage.execute_query(
        "SELECT revision FROM workbench_generation WHERE book_id = ?", (book_id,)
    )
    return rows[0]["revision"] if rows else 0


def _now_ms():
    import time

    return int(time.time() * 1000)


def _record_provenance(storage, book_id, target_key, generation_revision, run_id):
    """Append a provenance row for a generated presence target."""
    storage.execute_insert(
        "INSERT INTO workbench_provenance "
        "(provenance_id, book_id, target_kind, target_key, run_id, "
        " generation_revision, source, created_ms) "
        "VALUES (?, ?, 'presence', ?, ?, ?, 'walk', ?)",
        (
            f"prov-{uuid.uuid4().hex}",
            book_id,
            target_key,
            run_id,
            generation_revision,
            _now_ms(),
        ),
    )


def _upsert_generated_presence(storage, book_id, character_id, scene_id, confidence):
    """Upsert the generated presence projection by stable target key."""
    run_id = getattr(storage, "run_id", None)
    generation_revision = _generation_revision(storage, book_id)
    storage.execute_insert(
        "INSERT INTO character_scene_generated "
        "(id, book_id, character_id, scene_id, relation_type, confidence, "
        " generation_revision, source_run_id) "
        "VALUES (?, ?, ?, ?, 'present', ?, ?, ?) "
        "ON CONFLICT(book_id, character_id, scene_id, relation_type) "
        "DO UPDATE SET confidence = excluded.confidence, "
        "              generation_revision = excluded.generation_revision, "
        "              source_run_id = excluded.source_run_id",
        (
            f"csg-{uuid.uuid4().hex}",
            book_id,
            character_id,
            scene_id,
            confidence,
            generation_revision,
            run_id,
        ),
    )
    _record_provenance(
        storage, book_id, f"{scene_id}:{character_id}", generation_revision, run_id
    )


def _load_existing_characters(
    book_id: str, storage: PipelineStorage
) -> list[dict[str, str]]:
    """Load existing character id + name mappings for this book."""
    rows = storage.execute_query(
        """
        SELECT c.id, c.name
        FROM character c
        JOIN character_book cb ON cb.character_id = c.id
        WHERE cb.book_id = ?
        ORDER BY c.name
        """,
        (book_id,),
    )
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def _load_existing_junctions(scene_id: str, storage: PipelineStorage) -> set[str]:
    """Load existing character_ids that already have a character_scene junction for this scene."""
    rows = storage.execute_query(
        "SELECT character_id FROM character_scene WHERE scene_id = ?",
        (scene_id,),
    )
    return {row["character_id"] for row in rows}


def _process_scene(
    scene_id: str,
    book_id: str,
    storage: PipelineStorage,
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    system_prompt_override: str | None,
    existing_characters: list[dict[str, str]],
    result: dict,
) -> None:
    """Process a single scene: query paragraphs, call LLM, create junctions."""
    # Query paragraphs for this scene
    paragraphs = storage.execute_query(
        """
        SELECT p.id AS paragraph_id, p.text
        FROM paragraph p
        JOIN scene_paragraph sp ON sp.child_id = p.id
        WHERE sp.parent_id = ?
        ORDER BY sp.position
        """,
        (scene_id,),
    )

    if not paragraphs:
        logger.warning(f"No paragraphs found for scene {scene_id}")
        return

    # Load existing character_scene junctions for this scene
    existing_junctions = _load_existing_junctions(scene_id, storage)

    # Build LLM prompt with existing characters + scene text
    prompt = _build_prompt(paragraphs, existing_characters)

    # Call LLM
    response_text = chat_completion(
        client=client,
        model_name=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        system_prompt=(
            system_prompt_override
            or "You are a literary analyst specializing in character presence in narrative scenes."
        ),
        user_prompt=prompt,
    )

    # Parse response
    presence_list = _parse_llm_response(response_text)

    # Process each character presence
    conn = storage.get_connection()
    conn.execute("SAVEPOINT walk_2d_scene")
    try:
        accepted_presence: dict[str, float] = {}
        for presence_data in presence_list:
            character_id = presence_data.get("character_id", "").strip()
            confidence = presence_data.get("confidence", 0.8)
            if not isinstance(confidence, (int, float)):
                confidence = 0.8
            confidence = max(0.0, min(1.0, float(confidence)))
            if character_id and confidence >= 0.5:
                accepted_presence[character_id] = confidence

        # The model returns positives only. Reconcile all existing walk-owned
        # rows so an earlier false positive cannot survive a later run.
        _remove_omitted_generated_presence(
            scene_id=scene_id,
            book_id=book_id,
            storage=storage,
            accepted_character_ids=set(accepted_presence),
        )
        for character_id, confidence in accepted_presence.items():
            _process_presence(
                presence_data={"character_id": character_id, "confidence": confidence},
                scene_id=scene_id,
                book_id=book_id,
                storage=storage,
                existing_junctions=existing_junctions,
                result=result,
            )
        conn.execute("RELEASE SAVEPOINT walk_2d_scene")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT walk_2d_scene")
        conn.execute("RELEASE SAVEPOINT walk_2d_scene")
        raise


def _process_presence(
    presence_data: dict,
    scene_id: str,
    book_id: str,
    storage: PipelineStorage,
    existing_junctions: set[str],
    result: dict,
) -> None:
    """Process a single character presence from the LLM response.

    Applies confidence filter and creates character_scene junction if needed.
    """
    character_id = presence_data.get("character_id", "").strip()
    if not character_id:
        return

    confidence = presence_data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)):
        confidence = 0.8
    confidence = max(0.0, min(1.0, float(confidence)))
    if confidence < 0.5:
        return

    is_review = confidence < 0.7

    if _active_absence(storage, book_id, scene_id, character_id):
        return
    if _has_manual_presence(storage, book_id, scene_id, character_id):
        return

    if character_id in existing_junctions:
        storage.execute_update(
            "UPDATE character_scene SET confidence = ? "
            "WHERE scene_id = ? AND character_id = ? AND source = 'walk' "
            "AND human_override = 0",
            (confidence, scene_id, character_id),
        )
        _upsert_generated_presence(storage, book_id, character_id, scene_id, confidence)
        return

    storage.execute_insert(
        "INSERT INTO character_scene "
        "(character_id, scene_id, relation_type, source, confidence, human_override) "
        "VALUES (?, ?, 'present', 'walk', ?, 0)",
        (character_id, scene_id, confidence),
    )
    _upsert_generated_presence(storage, book_id, character_id, scene_id, confidence)
    existing_junctions.add(character_id)
    result["junctions_created"] += 1
    if is_review:
        result["junctions_for_review"] += 1


def _has_manual_presence(storage, book_id, scene_id, character_id):
    """True when a human presence decision protects this pair."""
    rows = storage.execute_query(
        "SELECT 1 FROM character_scene_manual "
        "WHERE book_id = ? AND scene_id = ? AND character_id = ? "
        "AND relation_type IN ('present', 'speaker')",
        (book_id, scene_id, character_id),
    )
    return bool(rows)


def _remove_omitted_generated_presence(
    scene_id, book_id, storage, accepted_character_ids: set[str]
):
    """Remove omitted walk-owned presence while preserving human decisions."""
    rows = storage.execute_query(
        "SELECT character_id FROM character_scene_generated "
        "WHERE book_id = ? AND scene_id = ? AND relation_type = 'present'",
        (book_id, scene_id),
    )
    for row in rows:
        character_id = row["character_id"]
        if character_id in accepted_character_ids:
            continue
        if _has_manual_presence(
            storage, book_id, scene_id, character_id
        ) or _active_absence(storage, book_id, scene_id, character_id):
            continue
        storage.execute_update(
            "DELETE FROM character_scene_generated "
            "WHERE book_id = ? AND scene_id = ? AND character_id = ? "
            "AND relation_type = 'present'",
            (book_id, scene_id, character_id),
        )
        storage.execute_update(
            "DELETE FROM character_scene WHERE scene_id = ? AND character_id = ? "
            "AND source = 'walk' AND human_override = 0",
            (scene_id, character_id),
        )


def _build_prompt(
    paragraphs: list[dict], existing_characters: list[dict[str, str]]
) -> str:
    """Build the LLM prompt for scene presence identification.

    Includes the list of existing characters (name + UUID) and the scene text.
    """
    paragraph_lines = []
    for idx, para in enumerate(paragraphs, start=1):
        text = (para.get("text") or "").strip()
        if text:
            paragraph_lines.append(f"[P{idx}] {text}")

    paragraphs_text = "\n".join(paragraph_lines)

    # Build character list
    character_lines = []
    for char in existing_characters:
        character_lines.append(f"- {char['name']} (UUID: {char['id']})")

    characters_text = (
        "\n".join(character_lines) if character_lines else "(no characters yet)"
    )

    prompt = f"""You are analyzing a scene from a novel to identify which characters are present.

Here are the characters that exist in this book:
{characters_text}

Here are the paragraphs in this scene:

{paragraphs_text}

For the following scene text, which of the listed characters are present? Return a JSON array with character_ids (array of UUIDs) and confidence. Return ONLY a JSON array.

Example:
[
  {{"character_id": "uuid-here", "confidence": 0.95}},
  {{"character_id": "uuid-there", "confidence": 0.8}}
]
"""
    return prompt


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse the LLM response into a list of presence dicts."""
    presence_list = extract_json_from_llm_response(response_text, expected_type="list")
    if presence_list is None:
        logger.error(f"Failed to parse LLM response as JSON: {response_text[:200]}")
        return []

    if not isinstance(presence_list, list):
        logger.error(f"LLM response is not a list: {type(presence_list)}")
        return []

    # Validate and normalize entries
    result = []
    for entry in presence_list:
        if not isinstance(entry, dict):
            continue
        character_id = entry.get("character_id", "")
        if not isinstance(character_id, str) or not character_id.strip():
            continue
        result.append(
            {
                "character_id": character_id.strip(),
                "confidence": entry.get("confidence", 0.8),
            }
        )

    return result
