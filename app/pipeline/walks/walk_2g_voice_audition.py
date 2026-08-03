"""Walk 2g: Voice audition.

Generates voice profiles for each character in a book by sending the
character's description (from ``character_metadata`` key='description')
and sampled dialogue excerpts (spans where the character is speaker) to
an LLM.  Voice profiles are stored in the ``character_metadata`` table
with ``key='voice_profile'``.

For each character, the walk:
1. Queries the character's description from ``character_metadata``.
2. Collects up to 5 representative dialogue spans (``relation_type='speaker'``).
3. Sends character name, description, and sampled dialogue to the LLM.
4. Parses the response (voice profile dict + confidence).
5. Stores the voice profile in ``character_metadata`` (UPSERT).

Confidence filter:
- ≥0.7: auto-accept (voice profile stored)
- <0.5: auto-reject (voice profile discarded)
- 0.5–0.7: flagged for user review (voice profile stored but tracked)

LLM configuration is resolved via ``resolve_task_llm('voice_audition')``
with temperature=0.3 for interpretive voice characterization.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute(book_id: str, storage: PipelineStorage, config: dict[str, Any]) -> dict:
    """Run Walk 2g voice audition for a book.

    Queries characters for the book, collects each character's description
    and dialogue samples, sends them to an LLM for voice profile generation,
    parses the response, and stores voice profiles in ``character_metadata``.

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
        Summary with keys: ``book_id``, ``characters_processed``,
        ``profiles_generated``, ``profiles_for_review``, ``errors``.
    """
    from app.utils import create_llm_client, resolve_task_llm

    result: dict[str, Any] = {
        "book_id": book_id,
        "characters_processed": 0,
        "profiles_generated": 0,
        "profiles_for_review": 0,
        "errors": [],
    }

    # Resolve LLM config for voice audition
    llm_config = resolve_task_llm("voice_audition", config_path=None)
    client, _ = create_llm_client(config_path=None)
    model_name = llm_config["model_name"]
    temperature = llm_config["temperature"]
    reasoning_effort = llm_config.get("reasoning_effort")

    # Look up book existence
    book_rows = storage.execute_query(
        "SELECT series_id FROM book WHERE id = ?", (book_id,)
    )
    if not book_rows:
        logger.error(f"Book {book_id} not found")
        result["errors"].append({"book_id": book_id, "error": "Book not found"})
        return result

    # Load existing characters for this book (id, name, aliases)
    existing_characters = _load_existing_characters(book_id, storage)

    for char_row in existing_characters:
        character_id = char_row["id"]
        character_name = char_row["name"]
        character_aliases = char_row["aliases"]

        result["characters_processed"] += 1

        try:
            _process_character(
                character_id=character_id,
                character_name=character_name,
                character_aliases=character_aliases,
                storage=storage,
                client=client,
                model_name=model_name,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                result=result,
            )
        except Exception as e:
            logger.error(f"Error processing character {character_id}: {e}")
            result["errors"].append({"character_id": character_id, "error": str(e)})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_existing_characters(
    book_id: str, storage: PipelineStorage
) -> list[dict[str, Any]]:
    """Load existing character id, name, and aliases for this book."""
    rows = storage.execute_query(
        """
        SELECT c.id, c.name, c.aliases
        FROM character c
        JOIN character_book cb ON cb.character_id = c.id
        WHERE cb.book_id = ?
        ORDER BY c.name
        """,
        (book_id,),
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "aliases": row["aliases"] or "[]",
        }
        for row in rows
    ]


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


def _collect_dialogue_spans(
    character_id: str, storage: PipelineStorage
) -> list[dict[str, str]]:
    """Collect dialogue spans where the character is the speaker.

    Returns a list of dicts with span_id and text.
    """
    rows = storage.execute_query(
        """
        SELECT s.id AS span_id, s.text
        FROM character_span cs
        JOIN span s ON cs.span_id = s.id
        WHERE cs.character_id = ?
          AND cs.relation_type = 'speaker'
        ORDER BY s.id
        """,
        (character_id,),
    )
    return [
        {
            "span_id": row["span_id"],
            "text": row["text"] or "",
        }
        for row in rows
    ]


def _sample_spans(spans: list[dict[str, str]], max_samples: int = 5) -> list[dict[str, str]]:
    """Sample up to max_samples spans spread across the book.

    Takes a spread from the collected spans to cover different parts of the book.
    """
    if len(spans) <= max_samples:
        return spans

    # Take evenly spaced samples
    step = len(spans) / max_samples
    sampled = []
    for i in range(max_samples):
        idx = int(i * step)
        sampled.append(spans[idx])
    return sampled


def _process_character(
    character_id: str,
    character_name: str,
    character_aliases: str,
    storage: PipelineStorage,
    client: Any,
    model_name: str,
    temperature: float,
    reasoning_effort: str | None,
    result: dict,
) -> None:
    """Process a single character: collect description + dialogue, call LLM, store profile."""
    # Collect dialogue spans for this character
    dialogue_spans = _collect_dialogue_spans(character_id, storage)

    if not dialogue_spans:
        logger.warning(f"No dialogue spans found for character {character_id} ({character_name})")
        return

    # Sample up to 5 representative dialogue spans
    sampled_spans = _sample_spans(dialogue_spans, max_samples=5)

    # Get character description (may be empty)
    character_description = _get_character_description(character_id, storage)

    # Build LLM prompt
    prompt = _build_voice_audition_prompt(
        character_name=character_name,
        character_aliases=character_aliases,
        character_description=character_description,
        sampled_spans=sampled_spans,
    )

    # Call LLM
    response_text = _call_llm(
        client=client,
        model_name=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        prompt=prompt,
    )

    # Parse response
    profile_data = _parse_llm_response(response_text)

    if not profile_data:
        logger.warning(f"Failed to parse voice profile for character {character_id}")
        return

    voice_profile = profile_data.get("voice_profile")
    if not isinstance(voice_profile, dict):
        logger.warning(f"Invalid voice_profile for character {character_id}")
        return

    confidence = profile_data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)):
        confidence = 0.8
    # Clamp to [0, 1]
    confidence = max(0.0, min(1.0, float(confidence)))

    # Confidence filter
    if confidence < 0.5:
        # Auto-reject
        return

    is_review = 0.5 <= confidence < 0.7

    # Store voice profile in character_metadata
    conn = storage.get_connection()
    conn.execute("SAVEPOINT walk_2g_voice")
    try:
        _store_voice_profile(character_id, voice_profile, storage)
        conn.execute("RELEASE SAVEPOINT walk_2g_voice")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT walk_2g_voice")
        raise

    result["profiles_generated"] += 1

    if is_review:
        result["profiles_for_review"] += 1


def _store_voice_profile(
    character_id: str, voice_profile: dict, storage: PipelineStorage
) -> None:
    """Store voice profile in character_metadata with key='voice_profile'.

    Uses SQLite UPSERT (INSERT OR REPLACE) to handle existing rows.
    The voice_profile dict is serialized as a JSON string.
    """
    storage.execute_insert(
        """
        INSERT INTO character_metadata (character_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(character_id, key) DO UPDATE SET value = excluded.value
        """,
        (character_id, "voice_profile", json.dumps(voice_profile)),
    )


def _build_voice_audition_prompt(
    character_name: str,
    character_aliases: str,
    character_description: str,
    sampled_spans: list[dict[str, str]],
) -> str:
    """Build the LLM prompt for voice profile generation.

    Includes character name, aliases, description, and sampled dialogue texts.
    """
    # Parse aliases JSON
    try:
        aliases_list = json.loads(character_aliases)
        if not isinstance(aliases_list, list):
            aliases_list = []
    except json.JSONDecodeError:
        aliases_list = []

    aliases_text = ", ".join(aliases_list) if aliases_list else "(none)"
    description_text = character_description.strip() if character_description else "(no description available)"

    # Build dialogue excerpts
    dialogue_lines = []
    for idx, span in enumerate(sampled_spans, start=1):
        text = span.get("text", "").strip()
        if text:
            dialogue_lines.append(f"[Dialogue {idx}] {text}")

    dialogue_text = "\n".join(dialogue_lines)

    prompt = f"""You are analyzing a character from a novel to suggest a voice profile for text-to-speech.

Character name: {character_name}
Aliases: {aliases_text}
Description: {description_text}

Here are representative dialogue excerpts spoken by this character:

{dialogue_text}

Based on this character's description and dialogue samples, suggest a voice profile. Return a JSON object with:
- "voice_profile": an object with voice characteristics such as:
  - "age": e.g. "young", "middle-aged", "elderly"
  - "gender": e.g. "male", "female", "neutral"
  - "tone": e.g. "authoritative but warm", "nervous and high-pitched"
  - "accent": e.g. "British RP", "Southern American", "none"
  - "pitch": e.g. "high", "medium", "low"
  - "pace": e.g. "fast", "measured", "slow"
- "confidence": float between 0.0 and 1.0 indicating your confidence in this voice profile

Return ONLY a JSON object, no other text.

Example:
{{"voice_profile": {{"age": "middle-aged", "gender": "male", "tone": "authoritative but warm", "accent": "British RP", "pitch": "medium-low", "pace": "measured"}}, "confidence": 0.85}}
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
            "content": "You are a voice casting specialist for audiobook production, analyzing characters to suggest appropriate voice profiles.",
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


def _parse_llm_response(response_text: str) -> dict:
    """Parse the LLM response into a voice profile dict.

    Returns a dict with voice_profile (object) and confidence. If parsing fails,
    returns empty dict.
    """
    # Try json.loads first
    try:
        profile_data = json.loads(response_text)
    except json.JSONDecodeError:
        # Try to find JSON object in response with regex fallback
        match = re.search(r"\{[\s\S]*\}", response_text)
        if match:
            try:
                profile_data = json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.error(
                    f"Failed to parse LLM response as JSON: {response_text[:200]}"
                )
                return {}
        else:
            logger.error(f"No JSON object found in LLM response: {response_text[:200]}")
            return {}

    if not isinstance(profile_data, dict):
        logger.error(f"LLM response is not a dict: {type(profile_data)}")
        return {}

    # Extract and validate fields
    voice_profile = profile_data.get("voice_profile")
    confidence = profile_data.get("confidence", 0.8)

    if not isinstance(voice_profile, dict):
        logger.error(f"voice_profile is not a valid dict: {voice_profile}")
        return {}

    return {
        "voice_profile": voice_profile,
        "confidence": confidence,
    }
