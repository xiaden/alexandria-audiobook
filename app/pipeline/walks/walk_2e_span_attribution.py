"""Walk 2e: Span speaker attribution.

Attributes speakers to quotation spans by sending each quotation along with
surrounding context to an LLM and parsing the response. Creates ``character_span``
junctions with ``relation_type='speaker'`` for identified speakers.

For each quotation span, the walk:
1. Queries the quotation text and surrounding context (2-3 spans before/after).
2. Queries existing characters for the book (name + UUID).
3. Sends both to the LLM asking who is speaking.
4. Parses the response (character UUID with confidence).
5. Creates ``character_span`` junction with ``relation_type='speaker'``.

If the LLM cannot determine the speaker (returns null character_id), no junction
is created. UNKNOWN speakers are handled at TTS boundary later.

Confidence filter:
- ≥0.7: auto-accept (junction created)
- <0.5: auto-reject (junction skipped)
- 0.5–0.7: flagged for user review (junction created but tracked)

LLM configuration is resolved via ``resolve_task_config('span_attribution', storage, book_id)``
with temperature=0.1 for format stability.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ._llm_helpers import chat_completion, extract_json_from_llm_response

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute(book_id: str, storage: PipelineStorage, config: dict[str, Any]) -> dict:
    """Run Walk 2e span speaker attribution for a book.

    Queries quotation spans for the book, sends each quotation's text along with
    surrounding context and the list of existing characters to an LLM for speaker
    identification, parses the response, and creates ``character_span`` junctions.

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
        Summary with keys: ``book_id``, ``spans_processed``,
        ``speakers_attributed``, ``speakers_unknown``, ``attributions_for_review``,
        ``errors``.
    """
    from app.utils import create_llm_client, resolve_task_config

    result: dict[str, Any] = {
        "book_id": book_id,
        "spans_processed": 0,
        "speakers_attributed": 0,
        "speakers_unknown": 0,
        "attributions_for_review": 0,
        "errors": [],
    }

    # Resolve LLM config for span attribution
    llm_config = resolve_task_config("span_attribution", storage, book_id)
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

    # Query quotation spans for this book (ordered by chapter, scene, paragraph, position)
    quotation_spans = storage.execute_query(
        """
        SELECT s.id AS span_id, s.text AS span_text,
               ps.parent_id AS paragraph_id,
               sp.parent_id AS scene_id,
               cs.parent_id AS chapter_id
        FROM span s
        JOIN paragraph_span ps ON ps.child_id = s.id
        JOIN scene_paragraph sp ON sp.child_id = ps.parent_id
        JOIN chapter_scene cs ON cs.child_id = sp.parent_id
        JOIN chapter c ON c.id = cs.parent_id
        WHERE c.book_id = ? AND s.span_type = 'quotation'
        ORDER BY c.id, cs.position, sp.position, ps.position
        """,
        (book_id,),
    )

    for span_row in quotation_spans:
        span_id = span_row["span_id"]
        span_text = span_row["span_text"] or ""
        paragraph_id = span_row["paragraph_id"]
        scene_id = span_row["scene_id"]

        result["spans_processed"] += 1

        try:
            _process_span(
                span_id=span_id,
                span_text=span_text,
                paragraph_id=paragraph_id,
                scene_id=scene_id,
                storage=storage,
                client=client,
                model_name=model_name,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                system_prompt_override=prompt,
                existing_characters=existing_characters,
                result=result,
            )
        except Exception as e:
            logger.error(f"Error processing span {span_id}: {e}")
            result["errors"].append({"span_id": span_id, "error": str(e)})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


def _get_surrounding_context(
    paragraph_id: str, span_id: str, storage: PipelineStorage
) -> dict[str, list[str]]:
    """Get surrounding spans (2-3 before and after) in the same paragraph."""
    # Get all spans in this paragraph ordered by position
    all_spans = storage.execute_query(
        """
        SELECT s.id, s.text, ps.position
        FROM span s
        JOIN paragraph_span ps ON ps.child_id = s.id
        WHERE ps.parent_id = ?
        ORDER BY ps.position
        """,
        (paragraph_id,),
    )

    # Find the current span's position
    current_position = None
    for span in all_spans:
        if span["id"] == span_id:
            current_position = span["position"]
            break

    if current_position is None:
        return {"before": [], "after": []}

    # Get 2-3 spans before and after
    before_spans = []
    after_spans = []
    for span in all_spans:
        if span["position"] < current_position and len(before_spans) < 3:
            before_spans.append(span["text"] or "")
        elif span["position"] > current_position and len(after_spans) < 3:
            after_spans.append(span["text"] or "")

    return {"before": before_spans, "after": after_spans}


def _process_span(
    span_id: str,
    span_text: str,
    paragraph_id: str,
    scene_id: str,
    storage: PipelineStorage,
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    system_prompt_override: str | None,
    existing_characters: list[dict[str, str]],
    result: dict,
) -> None:
    """Process a single quotation span: gather context, call LLM, create junction."""
    # Get surrounding context
    context = _get_surrounding_context(paragraph_id, span_id, storage)

    # Build LLM prompt
    prompt = _build_speaker_attribution_prompt(
        span_text=span_text,
        context_before=context["before"],
        context_after=context["after"],
        existing_characters=existing_characters,
    )

    # Call LLM
    response_text = chat_completion(
        client=client,
        model_name=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        system_prompt=(
            system_prompt_override
            or "You are a literary analyst specializing in dialogue attribution in fiction."
        ),
        user_prompt=prompt,
    )

    # Parse response
    attribution_data = _parse_llm_response(response_text)

    # Process attribution
    conn = storage.get_connection()
    conn.execute("SAVEPOINT walk_2e_span")
    try:
        _process_attribution(
            attribution_data=attribution_data,
            span_id=span_id,
            storage=storage,
            result=result,
        )
        conn.execute("RELEASE SAVEPOINT walk_2e_span")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT walk_2e_span")
        raise


def _process_attribution(
    attribution_data: dict,
    span_id: str,
    storage: PipelineStorage,
    result: dict,
) -> None:
    """Process a single speaker attribution from the LLM response.

    Applies confidence filter and creates character_span junction if needed.
    """
    character_id = attribution_data.get("character_id")
    if not character_id or (isinstance(character_id, str) and not character_id.strip()):
        # Unknown speaker
        result["speakers_unknown"] += 1
        return

    character_id = character_id.strip()

    confidence = attribution_data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)):
        confidence = 0.8
    # Clamp to [0, 1]
    confidence = max(0.0, min(1.0, float(confidence)))

    # Confidence filter
    if confidence < 0.5:
        # Auto-reject
        return

    is_review = 0.5 <= confidence < 0.7

    # Create character_span junction with relation_type='speaker'
    storage.execute_insert(
        "INSERT INTO character_span "
        "(character_id, span_id, relation_type, source, confidence, human_override) "
        "VALUES (?, ?, 'speaker', 'walk', ?, 0)",
        (character_id, span_id, confidence),
    )

    result["speakers_attributed"] += 1

    if is_review:
        result["attributions_for_review"] += 1


def _build_speaker_attribution_prompt(
    span_text: str,
    context_before: list[str],
    context_after: list[str],
    existing_characters: list[dict[str, str]],
) -> str:
    """Build the LLM prompt for speaker attribution.

    Includes the quotation text, surrounding context, and list of known characters.
    """
    # Build context sections
    before_text = "\n".join(f"BEFORE: {text}" for text in context_before) if context_before else ""
    after_text = "\n".join(f"AFTER: {text}" for text in context_after) if context_after else ""

    # Build character list
    character_lines = []
    for char in existing_characters:
        character_lines.append(f"- {char['name']} (UUID: {char['id']})")

    characters_text = "\n".join(character_lines) if character_lines else "(no characters yet)"

    prompt = f"""You are analyzing a quotation from a novel to identify the speaker.

Here are the characters that exist in this book:
{characters_text}

Here is the quotation you need to analyze:
QUOTATION: {span_text}

{before_text}

{after_text}

Who is speaking this quotation? Return a JSON object with:
- "character_id": the UUID of the speaking character (from the list above), or null if you cannot determine the speaker
- "confidence": float between 0.0 and 1.0 indicating your confidence in this attribution

Return ONLY a JSON object, no other text.

Example:
{{"character_id": "uuid-here", "confidence": 0.95}}

If you cannot determine the speaker:
{{"character_id": null, "confidence": 0.3}}
"""
    return prompt


def _parse_llm_response(response_text: str) -> dict:
    """Parse the LLM response into an attribution dict.

    Returns a dict with character_id and confidence. If parsing fails or
    character_id is null/empty, returns empty dict.
    """
    attribution = extract_json_from_llm_response(response_text, expected_type="dict")
    if attribution is None:
        logger.error(
            f"Failed to parse LLM response as JSON: {response_text[:200]}"
        )
        return {}

    if not isinstance(attribution, dict):
        logger.error(f"LLM response is not a dict: {type(attribution)}")
        return {}

    # Extract and validate fields
    character_id = attribution.get("character_id")
    confidence = attribution.get("confidence", 0.8)

    # character_id can be null (unknown speaker) or a string UUID
    if character_id is not None and not isinstance(character_id, str):
        logger.error(f"character_id is not a string or null: {type(character_id)}")
        return {}

    return {
        "character_id": character_id,
        "confidence": confidence,
    }
