"""Walk 2a: Scene segmentation.

Detects scene boundaries within chapters by sending paragraph text to an LLM
and parsing the response. Creates new scene entities and redistributes
paragraphs from placeholder scenes to the newly identified scenes.

Confidence filter:
- ≥0.7: auto-accept (scene is created)
- <0.5: auto-reject (scene is discarded)
- 0.5–0.7: flagged for user review (scene is created but marked)

LLM configuration is resolved via ``resolve_task_llm('scene_segmentation')``
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
    """Run Walk 2a scene segmentation for a book.

    Queries paragraphs per chapter, sends them to an LLM for scene boundary
    detection, parses the response, and creates scenes via ``insert_scene()``.

    Parameters
    ----------
    book_id:
        UUID of the book to process.
    storage:
        Pipeline storage adapter.
    config:
        App config dict (passed to ``resolve_task_llm``).

    Returns
    -------
    dict:
        Summary with keys: ``book_id``, ``chapters_processed``,
        ``scenes_created``, ``scenes_rejected``, ``scenes_for_review``,
        ``errors``.
    """
    from app.pipeline.populate import insert_scene
    from app.utils import create_llm_client, resolve_task_llm

    result = {
        "book_id": book_id,
        "chapters_processed": 0,
        "scenes_created": 0,
        "scenes_rejected": 0,
        "scenes_for_review": 0,
        "errors": [],
    }

    # Resolve LLM config for scene segmentation
    llm_config = resolve_task_llm("scene_segmentation", config_path=None)
    client, _ = create_llm_client(config_path=None)
    model_name = llm_config["model_name"]
    temperature = llm_config["temperature"]
    reasoning_effort = llm_config.get("reasoning_effort")

    # Query chapters for this book
    chapters = storage.execute_query(
        "SELECT child_id AS chapter_id FROM book_chapter WHERE parent_id = ? ORDER BY position",
        (book_id,),
    )

    for chapter_row in chapters:
        chapter_id = chapter_row["chapter_id"]
        result["chapters_processed"] += 1

        try:
            _process_chapter(
                chapter_id=chapter_id,
                storage=storage,
                client=client,
                model_name=model_name,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                insert_scene_fn=insert_scene,
                result=result,
            )
        except Exception as e:
            logger.error(f"Error processing chapter {chapter_id}: {e}")
            result["errors"].append({"chapter_id": chapter_id, "error": str(e)})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _process_chapter(
    chapter_id: str,
    storage: PipelineStorage,
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    insert_scene_fn: Any,
    result: dict,
) -> None:
    """Process a single chapter: query paragraphs, call LLM, create scenes."""
    # Query paragraphs for this chapter (via placeholder scene)
    paragraphs = storage.execute_query(
        """
        SELECT p.id AS paragraph_id, p.text
        FROM paragraph p
        JOIN scene_paragraph sp ON sp.child_id = p.id
        JOIN chapter_scene cs ON cs.child_id = sp.parent_id
        WHERE cs.parent_id = ?
        ORDER BY sp.position
        """,
        (chapter_id,),
    )

    if not paragraphs:
        logger.warning(f"No paragraphs found for chapter {chapter_id}")
        return

    # Build LLM prompt
    prompt = _build_scene_segmentation_prompt(paragraphs)

    # Call LLM
    response_text = chat_completion(
        client=client,
        model_name=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        system_prompt="You are a literary analyst specializing in narrative structure.",
        user_prompt=prompt,
    )

    # Parse response
    scenes = _parse_llm_response(response_text, paragraphs)

    # Apply confidence filter and create scenes
    for scene_data in scenes:
        confidence = scene_data.get("confidence", 0.0)
        paragraph_ids = scene_data.get("paragraph_ids", [])

        if not paragraph_ids:
            continue

        if confidence >= 0.7:
            # Auto-accept
            scene_id = str(uuid.uuid4())
            insert_scene_fn(scene_id, chapter_id, paragraph_ids, storage)
            result["scenes_created"] += 1
        elif confidence < 0.5:
            # Auto-reject
            result["scenes_rejected"] += 1
        else:
            # Between 0.5 and 0.7: flag for review but still create
            scene_id = str(uuid.uuid4())
            insert_scene_fn(scene_id, chapter_id, paragraph_ids, storage)
            result["scenes_for_review"] += 1


def _build_scene_segmentation_prompt(paragraphs: list[dict]) -> str:
    """Build the LLM prompt for scene boundary detection."""
    paragraph_lines = []
    for idx, para in enumerate(paragraphs, start=1):
        text = para.get("text", "").strip()
        if text:
            paragraph_lines.append(f"[P{idx}] {text}")

    paragraphs_text = "\n".join(paragraph_lines)

    prompt = f"""You are analyzing a chapter to identify scene boundaries.

A scene is a continuous sequence of paragraphs that share a common location, time, or set of characters. Scene boundaries occur when there is a significant shift in location, time, or character focus.

Here are the paragraphs in this chapter:

{paragraphs_text}

Identify scene boundaries and return a JSON array of scenes. Each scene should include:
- "paragraph_ids": list of paragraph IDs (e.g., ["P1", "P2", "P3"])
- "confidence": float between 0.0 and 1.0 indicating your confidence in this scene boundary

Return ONLY a JSON array, no other text. Example:
[
  {{"paragraph_ids": ["P1", "P2", "P3"], "confidence": 0.9}},
  {{"paragraph_ids": ["P4", "P5"], "confidence": 0.8}}
]
"""
    return prompt


def _parse_llm_response(response_text: str, paragraphs: list[dict]) -> list[dict]:
    """Parse the LLM response into a list of scene dicts.

    Maps paragraph indices (P1, P2, etc.) back to actual paragraph IDs.
    """
    # Build index mapping: P1 -> paragraphs[0]["paragraph_id"], etc.
    index_to_id = {}
    for idx, para in enumerate(paragraphs, start=1):
        index_to_id[f"P{idx}"] = para["paragraph_id"]

    # Try to extract JSON from response
    scenes = extract_json_from_llm_response(response_text, expected_type="list")
    if scenes is None:
        logger.error(f"Failed to parse LLM response as JSON: {response_text[:200]}")
        return []

    if not isinstance(scenes, list):
        logger.error(f"LLM response is not a list: {type(scenes)}")
        return []

    # Map paragraph indices to actual IDs
    result = []
    for scene_data in scenes:
        if not isinstance(scene_data, dict):
            continue

        para_indices = scene_data.get("paragraph_ids", [])
        paragraph_ids = []
        for idx in para_indices:
            if idx in index_to_id:
                paragraph_ids.append(index_to_id[idx])
            else:
                logger.warning(f"Unknown paragraph index: {idx}")

        if paragraph_ids:
            result.append({
                "paragraph_ids": paragraph_ids,
                "confidence": scene_data.get("confidence", 0.5),
            })

    return result
