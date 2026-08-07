"""Walk 2i: Delivery instruction generation.

Generates TTS delivery instructions (the ``instruct`` field) for each span
in a book by sending the span text along with speaker character context
(description, voice profile, voice assignment) to an LLM.  Instructions
are stored directly in the ``span.instruct`` column.

For each span in presentation order, the walk:
1. Queries spans via the ``span_presentation`` VIEW (global_index ordering).
2. Finds the speaker character (if any) via ``character_span`` junction.
3. Collects character description, voice profile, and voice assignment.
4. Sends span text + speaker context to the LLM for delivery instructions.
5. Parses the response (instruct string + confidence).
6. Applies the confidence filter and stores the instruct field.

Confidence filter:
- ≥0.7: auto-accept (instruct stored)
- <0.5: auto-reject (instruct stays NULL)
- 0.5–0.7: flagged for user review (instruct stored but tracked)

For narrative/non-speaker spans (no speaker character), the LLM is still
called with "NARRATOR" as the speaker and a note that the text is narrative.

LLM configuration is resolved via ``resolve_task_config('delivery', storage, book_id)``
with temperature=0.3 for interpretive delivery characterization.

CRITICAL: This walk MUST use the LLM for every span — no rule-based fallback.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from app.pipeline.review import supersede_targets
from ._llm_helpers import chat_completion, extract_json_from_llm_response

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute(book_id: str, storage: PipelineStorage, config: dict[str, Any]) -> dict:
    """Run Walk 2i delivery instruction generation for a book.

    Queries spans in presentation order, determines the speaker for each span,
    sends span text + speaker context to an LLM for delivery instructions,
    parses the response, and stores instructions in ``span.instruct``.

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
        ``instructs_generated``, ``instructs_for_review``, ``errors``.
    """
    from app.utils import create_llm_client, resolve_task_config

    result: dict[str, Any] = {
        "book_id": book_id,
        "spans_processed": 0,
        "instructs_generated": 0,
        "instructs_for_review": 0,
        "errors": [],
    }

    # Targets whose write committed this run; the FINAL transaction supersedes
    # prior pending items of the same kind for exactly these targets.
    committed_target_ids: list[str] = []

    # Resolve LLM config for delivery
    llm_config = resolve_task_config("delivery", storage, book_id)
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

    # Load spans in presentation order
    spans = _load_spans_in_presentation_order(book_id, storage)

    for span_row in spans:
        span_id = span_row["id"]
        span_type = span_row["span_type"]
        span_text = span_row["text"]

        result["spans_processed"] += 1

        try:
            _process_span(
                span_id=span_id,
                span_type=span_type,
                span_text=span_text,
                book_id=book_id,
                storage=storage,
                client=client,
                model_name=model_name,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                system_prompt_override=prompt,
                result=result,
                committed_target_ids=committed_target_ids,
            )
        except Exception as e:
            logger.error(f"Error processing span {span_id}: {e}")
            result["errors"].append({"span_id": span_id, "error": str(e)})

    # Completion-time per-target supersede (contract rule #9): the walk's
    # FINAL transaction marks prior pending items of the same kind for the
    # targets THIS run regenerated as superseded.  Nothing is superseded when
    # a unit raised (a top-level exception never reaches this point) or when
    # the run was cancelled (run_walk never invokes execute).  A raw adapter
    # without a run context (direct-call tests) has no run to supersede from.
    run_id = getattr(storage, "run_id", None)
    if run_id is not None:
        supersede_targets(
            storage,
            book_id=book_id,
            run_id=run_id,
            kind="instruction",
            target_ids=committed_target_ids,
        )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_spans_in_presentation_order(
    book_id: str, storage: PipelineStorage
) -> list[dict[str, Any]]:
    """Load spans for a book in presentation order via span_presentation VIEW.

    Returns a list of dicts with id, span_type, and text.
    """
    rows = storage.execute_query(
        """
        SELECT sp.id, sp.span_type, s.text
        FROM span_presentation sp
        JOIN span s ON sp.id = s.id
        JOIN paragraph_span ps ON ps.child_id = sp.id
        JOIN scene_paragraph sp_edge ON sp_edge.child_id = ps.parent_id
        JOIN chapter_scene cs ON cs.child_id = sp_edge.parent_id
        JOIN book_chapter bc ON bc.child_id = cs.parent_id
        WHERE bc.parent_id = ?
        ORDER BY sp.global_index
        """,
        (book_id,),
    )
    return [
        {
            "id": row["id"],
            "span_type": row["span_type"],
            "text": row["text"] or "",
        }
        for row in rows
    ]


def _find_speaker_character(
    span_id: str, storage: PipelineStorage
) -> dict[str, Any] | None:
    """Find the speaker character for a span, if any.

    Returns a dict with character_id, or None if no speaker found.
    """
    rows = storage.execute_query(
        """
        SELECT cs.character_id
        FROM character_span cs
        WHERE cs.span_id = ?
          AND cs.relation_type = 'speaker'
        LIMIT 1
        """,
        (span_id,),
    )
    if rows:
        return {"character_id": rows[0]["character_id"]}
    return None


def _get_character_description(
    character_id: str, storage: PipelineStorage
) -> str:
    """Retrieve the character description from character_metadata.

    Returns the description string, or empty string if not found.
    """
    rows = storage.execute_query(
        "SELECT value FROM character_metadata WHERE character_id = ? AND key = 'description'",
        (character_id,),
    )
    if rows:
        return rows[0]["value"] or ""
    return ""


def _get_character_voice_profile(
    character_id: str, storage: PipelineStorage
) -> str:
    """Retrieve the character voice profile from character_metadata.

    Returns the voice profile JSON string, or empty string if not found.
    """
    rows = storage.execute_query(
        "SELECT value FROM character_metadata WHERE character_id = ? AND key = 'voice_profile'",
        (character_id,),
    )
    if rows:
        return rows[0]["value"] or ""
    return ""


def _get_character_voice_assignment(
    character_id: str, storage: PipelineStorage
) -> dict[str, str] | None:
    """Retrieve the character's voice assignment from voice_config.

    Returns a dict with voice id, name, description, or None if not assigned.
    """
    rows = storage.execute_query(
        """
        SELECT vc.id, vc.name, vc.description
        FROM character c
        JOIN voice_config vc ON c.voice_assignment_id = vc.id
        WHERE c.id = ?
        """,
        (character_id,),
    )
    if rows:
        return {
            "id": rows[0]["id"],
            "name": rows[0]["name"] or "",
            "description": rows[0]["description"] or "",
        }
    return None


def _process_span(
    span_id: str,
    span_type: str,
    span_text: str,
    book_id: str,
    storage: PipelineStorage,
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    system_prompt_override: str | None,
    result: dict,
    committed_target_ids: list[str],
) -> None:
    """Process a single span: find speaker, call LLM, store instruct."""
    # Find speaker character
    speaker_info = _find_speaker_character(span_id, storage)

    if speaker_info is not None:
        character_id = speaker_info["character_id"]
        speaker_name = _get_character_name(character_id, storage)
        character_description = _get_character_description(character_id, storage)
        voice_profile = _get_character_voice_profile(character_id, storage)
        voice_assignment = _get_character_voice_assignment(character_id, storage)
    else:
        speaker_name = "NARRATOR"
        character_description = ""
        voice_profile = ""
        voice_assignment = None

    # Build LLM prompt
    prompt = _build_delivery_prompt(
        span_text=span_text,
        span_type=span_type,
        speaker_name=speaker_name,
        character_description=character_description,
        voice_profile=voice_profile,
        voice_assignment=voice_assignment,
        is_narrative=(speaker_info is None),
    )

    # Call LLM
    response_text = chat_completion(
        client=client,
        model_name=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        system_prompt=(
            system_prompt_override
            or "You are a TTS delivery specialist for audiobook production, "
            "generating performance instructions for text-to-speech rendering."
        ),
        user_prompt=prompt,
    )

    # Parse response
    instruct_data = _parse_llm_response(response_text)

    if not instruct_data:
        logger.warning(f"Failed to parse delivery instruct for span {span_id}")
        return

    instruct = instruct_data.get("instruct", "").strip()
    if not instruct:
        logger.warning(f"Empty instruct for span {span_id}")
        return

    confidence = instruct_data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)):
        confidence = 0.8
    # Clamp to [0, 1]
    confidence = max(0.0, min(1.0, float(confidence)))

    # Confidence filter
    if confidence < 0.5:
        # Auto-reject — instruct stays NULL
        return

    is_review = 0.5 <= confidence < 0.7

    # Capture the prior instruct value OUTSIDE the savepoint so the review
    # item records the pre-write state (None if absent).
    prior_instruct = _get_prior_instruct(span_id, storage)

    # Store instruct in span table
    conn = storage.get_connection()
    conn.execute("SAVEPOINT walk_2i_delivery")
    try:
        storage.execute_update(
            "UPDATE span SET instruct = ? WHERE id = ?",
            (instruct, span_id),
        )
        if is_review:
            _insert_review_item(
                storage=storage,
                book_id=book_id,
                span_id=span_id,
                prior_value=prior_instruct,
            )
        conn.execute("RELEASE SAVEPOINT walk_2i_delivery")
        committed_target_ids.append(span_id)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT walk_2i_delivery")
        raise

    result["instructs_generated"] += 1

    if is_review:
        result["instructs_for_review"] += 1


def _get_prior_instruct(
    span_id: str, storage: PipelineStorage
) -> str | None:
    """Return the span's current instruct value, or None if absent.

    Read OUTSIDE the savepoint so ``prior_value`` reflects the pre-write
    state of the target row (contract: prior_value from pre-write state).
    """
    rows = storage.execute_query(
        "SELECT instruct FROM span WHERE id = ?",
        (span_id,),
    )
    if rows:
        return rows[0]["instruct"]
    return None


def _insert_review_item(
    storage: PipelineStorage,
    book_id: str,
    span_id: str,
    prior_value: str | None,
) -> None:
    """Write a walk_review_item row for a review-band delivery instruct.

    Called inside the per-unit savepoint so the item row commits (or rolls
    back) atomically with the span.instruct update.  Auto-accept (>=0.7)
    and auto-reject (<0.5) paths never reach this helper.
    """
    run_id = storage.run_id
    storage.execute_insert(
        """
        INSERT INTO walk_review_item
            (id, book_id, run_id, kind, target_table, target_id,
             prior_value, status, created_ms)
        VALUES (?, ?, ?, 'instruction', 'span', ?, ?, 'pending', ?)
        """,
        (
            f"{run_id}:instruction:{span_id}",
            book_id,
            run_id,
            span_id,
            prior_value,
            int(time.time() * 1000),
        ),
    )


def _get_character_name(
    character_id: str, storage: PipelineStorage
) -> str:
    """Retrieve the character name."""
    rows = storage.execute_query(
        "SELECT name FROM character WHERE id = ?",
        (character_id,),
    )
    if rows:
        return rows[0]["name"] or ""
    return ""


def _build_delivery_prompt(
    span_text: str,
    span_type: str,
    speaker_name: str,
    character_description: str,
    voice_profile: str,
    voice_assignment: dict[str, str] | None,
    is_narrative: bool,
) -> str:
    """Build the LLM prompt for delivery instruction generation.

    Includes span text, speaker context (character description, voice profile,
    voice assignment), and whether the span is narrative or dialogue.
    """
    if is_narrative:
        speaker_context = (
            "This is narrative text (no specific speaker character). "
            "The speaker is NARRATOR."
        )
    else:
        desc_text = (
            character_description.strip()
            if character_description
            else "(no description available)"
        )
        voice_text = (
            voice_profile.strip() if voice_profile else "(no voice profile available)"
        )

        if voice_assignment is not None:
            voice_name = voice_assignment.get("name", "")
            voice_desc = voice_assignment.get("description", "")
            assignment_text = f'Voice: "{voice_name}" — {voice_desc}'
        else:
            assignment_text = "(no voice assigned)"

        speaker_context = (
            f"Speaker: {speaker_name}\n"
            f"Character description: {desc_text}\n"
            f"Voice profile: {voice_text}\n"
            f"{assignment_text}"
        )

    prompt = f"""You are generating TTS delivery instructions for an audiobook span.

Span type: {span_type}
Span text: {span_text}

{speaker_context}

Given this span's text and the speaker's character description and voice profile, generate TTS delivery instructions. Return a JSON object with:
- "instruct": a string describing how the text should be delivered (e.g., "slow and somber", "fast and excited", "whispered", "neutral and narrative", "warm and conversational")
- "confidence": float between 0.0 and 1.0 indicating your confidence in these instructions

Return ONLY a JSON object, no other text.

Example:
{{"instruct": "slow and somber, with a slight pause before the final clause", "confidence": 0.85}}
"""
    return prompt


def _parse_llm_response(response_text: str) -> dict:
    """Parse the LLM response into a delivery instruct dict.

    Returns a dict with instruct (str) and confidence (float). If parsing fails,
    returns empty dict.
    """
    instruct_data = extract_json_from_llm_response(response_text, expected_type="dict")
    if instruct_data is None:
        logger.error(
            f"Failed to parse LLM response as JSON: {response_text[:200]}"
        )
        return {}

    if not isinstance(instruct_data, dict):
        logger.error(f"LLM response is not a dict: {type(instruct_data)}")
        return {}

    # Extract and validate fields
    instruct = instruct_data.get("instruct")
    confidence = instruct_data.get("confidence", 0.8)

    if not isinstance(instruct, str) or not instruct.strip():
        logger.error(f"instruct is not a valid string: {instruct}")
        return {}

    return {
        "instruct": instruct.strip(),
        "confidence": confidence,
    }
