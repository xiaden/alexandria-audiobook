"""Walk 2b: Character discovery.

Identifies characters mentioned or present in each scene by sending scene text
(paragraphs within the scene) to an LLM and parsing the response.  Creates
``character`` entities, ``character_book`` / ``character_series`` junctions,
``character_scene`` junctions (always ``relation_type='present'``), and seeds
``character_span`` junctions (``speaker`` / ``mentioned`` / ``present``) for
downstream refinement by Walk 2e.

Confidence filter:
- ≥0.7: auto-accept (character + junctions are created)
- <0.5: auto-reject (character is discarded)
- 0.5–0.7: flagged for user review (character is created but tracked)

LLM configuration is resolved via ``resolve_task_llm('character_discovery')``
with temperature=0.1 for format stability.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute(book_id: str, storage: PipelineStorage, config: dict[str, Any]) -> dict:
    """Run Walk 2b character discovery for a book.

    Queries scenes for the book, sends each scene's paragraph text to an LLM
    for character identification, parses the response, and creates character
    entities with junctions.

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
        Summary with keys: ``book_id``, ``scenes_processed``,
        ``characters_created``, ``characters_for_review``, ``errors``.
    """
    from app.utils import create_llm_client, resolve_task_llm

    result: dict[str, Any] = {
        "book_id": book_id,
        "scenes_processed": 0,
        "characters_created": 0,
        "characters_for_review": 0,
        "errors": [],
    }

    # Resolve LLM config for character discovery
    llm_config = resolve_task_llm("character_discovery", config_path=None)
    client, _ = create_llm_client(config_path=None)
    model_name = llm_config["model_name"]
    temperature = llm_config["temperature"]
    reasoning_effort = llm_config.get("reasoning_effort")

    # Look up series_id for character_series junctions
    book_rows = storage.execute_query(
        "SELECT series_id FROM book WHERE id = ?", (book_id,)
    )
    if not book_rows:
        logger.error(f"Book {book_id} not found")
        result["errors"].append({"book_id": book_id, "error": "Book not found"})
        return result
    series_id = book_rows[0]["series_id"]

    # Cache of character name → character_id for this book (avoids duplicate
    # creation and redundant DB lookups within a single walk execution).
    name_to_id: dict[str, str] = _load_existing_characters(book_id, storage)

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
                series_id=series_id,
                storage=storage,
                client=client,
                model_name=model_name,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                name_to_id=name_to_id,
                result=result,
            )
        except Exception as e:
            logger.error(f"Error processing scene {scene_id}: {e}")
            result["errors"].append({"scene_id": scene_id, "error": str(e)})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_existing_characters(
    book_id: str, storage: PipelineStorage
) -> dict[str, str]:
    """Load existing character name → id mappings for this book."""
    rows = storage.execute_query(
        """
        SELECT c.id, c.name
        FROM character c
        JOIN character_book cb ON cb.character_id = c.id
        WHERE cb.book_id = ?
        """,
        (book_id,),
    )
    return {row["name"]: row["id"] for row in rows}


def _process_scene(
    scene_id: str,
    book_id: str,
    series_id: str,
    storage: PipelineStorage,
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    name_to_id: dict[str, str],
    result: dict,
) -> None:
    """Process a single scene: query paragraphs, call LLM, create characters."""
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

    # Query spans in this scene (for character_span seeding)
    spans = storage.execute_query(
        """
        SELECT s.id AS span_id
        FROM span s
        JOIN paragraph_span ps ON ps.child_id = s.id
        JOIN scene_paragraph sp ON ps.parent_id = sp.child_id
        WHERE sp.parent_id = ?
        ORDER BY sp.position, ps.position
        """,
        (scene_id,),
    )
    span_ids = [row["span_id"] for row in spans]

    # Build LLM prompt
    prompt = _build_character_discovery_prompt(paragraphs)

    # Call LLM
    response_text = _call_llm(
        client=client,
        model_name=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        prompt=prompt,
    )

    # Parse response
    characters = _parse_llm_response(response_text)

    # Process each discovered character
    conn = storage.get_connection()
    conn.execute("SAVEPOINT walk_2b_scene")
    try:
        for char_data in characters:
            _process_character(
                char_data=char_data,
                scene_id=scene_id,
                book_id=book_id,
                series_id=series_id,
                span_ids=span_ids,
                storage=storage,
                name_to_id=name_to_id,
                result=result,
            )
        conn.execute("RELEASE SAVEPOINT walk_2b_scene")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT walk_2b_scene")
        raise


def _process_character(
    char_data: dict,
    scene_id: str,
    book_id: str,
    series_id: str,
    span_ids: list[str],
    storage: PipelineStorage,
    name_to_id: dict[str, str],
    result: dict,
) -> None:
    """Process a single character from the LLM response.

    Applies confidence filter, creates character entity if needed, and inserts
    character_scene + character_span junctions.
    """
    name = char_data.get("name", "").strip()
    if not name:
        return

    confidence = char_data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)):
        confidence = 0.8
    # Clamp to [0, 1]
    confidence = max(0.0, min(1.0, float(confidence)))

    role = char_data.get("role", "present").strip().lower()
    if role not in ("speaker", "mentioned", "present"):
        role = "present"

    aliases = char_data.get("aliases", [])
    if not isinstance(aliases, list):
        aliases = []
    aliases_json = json.dumps(aliases)

    # Confidence filter
    if confidence < 0.5:
        # Auto-reject
        return

    is_review = 0.5 <= confidence < 0.7

    # Get or create character
    character_id = name_to_id.get(name)
    if character_id is None:
        character_id = str(uuid.uuid4())
        storage.execute_insert(
            "INSERT INTO character (id, name, aliases) VALUES (?, ?, ?)",
            (character_id, name, aliases_json),
        )
        # character_book junction
        storage.execute_insert(
            "INSERT INTO character_book (character_id, book_id, source, confidence, human_override) "
            "VALUES (?, ?, 'walk', ?, 0)",
            (character_id, book_id, confidence),
        )
        # character_series junction
        storage.execute_insert(
            "INSERT INTO character_series (character_id, series_id, source, confidence, human_override) "
            "VALUES (?, ?, 'walk', ?, 0)",
            (character_id, series_id, confidence),
        )
        name_to_id[name] = character_id
        result["characters_created"] += 1

        # Only count for review when first created
        if is_review:
            result["characters_for_review"] += 1

    # character_scene junction — ALWAYS relation_type='present' for walk 2b
    storage.execute_insert(
        "INSERT INTO character_scene "
        "(character_id, scene_id, relation_type, source, confidence, human_override) "
        "VALUES (?, ?, 'present', 'walk', ?, 0)",
        (character_id, scene_id, confidence),
    )

    # character_span junctions — seed based on role
    # For 'speaker': insert all spans with relation_type='speaker'
    # For 'mentioned': insert all spans with relation_type='mentioned'
    # For 'present': insert all spans with relation_type='present'
    span_relation_type = role  # speaker, mentioned, or present
    for span_id in span_ids:
        storage.execute_insert(
            "INSERT INTO character_span "
            "(character_id, span_id, relation_type, source, confidence, human_override) "
            "VALUES (?, ?, ?, 'walk', ?, 0)",
            (character_id, span_id, span_relation_type, confidence),
        )


def _build_character_discovery_prompt(paragraphs: list[dict]) -> str:
    """Build the LLM prompt for character identification in a scene."""
    paragraph_lines = []
    for idx, para in enumerate(paragraphs, start=1):
        text = (para.get("text") or "").strip()
        if text:
            paragraph_lines.append(f"[P{idx}] {text}")

    paragraphs_text = "\n".join(paragraph_lines)

    prompt = f"""You are analyzing a scene from a novel to identify characters.

Identify all characters mentioned or present in this scene. For each character, determine whether they are a speaker (someone who speaks dialogue), merely mentioned (referenced but not speaking), or present (in the scene but not speaking and not specifically mentioned by name).

Here are the paragraphs in this scene:

{paragraphs_text}

Return a JSON array of characters found. Each entry should include:
- "name": the character's name (string)
- "aliases": list of alternative names or references for this character (array of strings, may be empty)
- "role": one of "speaker", "mentioned", or "present"
- "confidence": float between 0.0 and 1.0 indicating your confidence in this identification

Return ONLY a JSON array, no other text. Example:
[
  {{"name": "John Smith", "aliases": ["Mr. Smith", "John"], "role": "speaker", "confidence": 0.95}},
  {{"name": "Mary", "aliases": [], "role": "mentioned", "confidence": 0.8}}
]
"""
    return prompt


def _call_llm(
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    prompt: str,
) -> str:
    """Call the LLM and return the response text."""
    messages = [
        {
            "role": "system",
            "content": "You are a literary analyst specializing in character identification in fiction.",
        },
        {"role": "user", "content": prompt},
    ]

    extra_body = {}
    if reasoning_effort is not None:
        extra_body["reasoning_effort"] = reasoning_effort

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        extra_body=extra_body if extra_body else None,
    )

    return response.choices[0].message.content.strip()


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse the LLM response into a list of character dicts."""
    # Use clean_json_string pattern to extract JSON array
    try:
        characters = json.loads(response_text)
    except json.JSONDecodeError:
        # Try to find JSON array in response
        match = re.search(r"\[[\s\S]*\]", response_text)
        if match:
            try:
                characters = json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.error(
                    f"Failed to parse LLM response as JSON: {response_text[:200]}"
                )
                return []
        else:
            logger.error(f"No JSON array found in LLM response: {response_text[:200]}")
            return []

    if not isinstance(characters, list):
        logger.error(f"LLM response is not a list: {type(characters)}")
        return []

    # Validate and normalize entries
    result = []
    for entry in characters:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            continue
        result.append(
            {
                "name": name.strip(),
                "aliases": entry.get("aliases", []) if isinstance(entry.get("aliases"), list) else [],
                "role": entry.get("role", "present"),
                "confidence": entry.get("confidence", 0.8),
            }
        )

    return result
