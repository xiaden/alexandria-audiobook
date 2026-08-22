"""Walk 2c: Alias resolution (GLOBAL scope).

Resolves character aliases across the *entire* book by sending the full
character list (names + current aliases) to an LLM and merging characters
that refer to the same person.  This is the structural fix for the
late-introduction failure: because the scope is GLOBAL (not per-scene),
the LLM can connect "the old man" from chapter 1 with "Gandalf" named
in chapter 3.

For each merge group returned by the LLM:
1. Pick a canonical character (matched by ``canonical_name`` or first UUID).
2. Atomically (SAVEPOINT) update all junction tables to point to canonical.
3. Delete non-canonical character rows.
4. Consolidate aliases on the canonical character.

Confidence filter:
- ≥0.7: auto-accept (merge is applied)
- <0.5: auto-reject (merge group is skipped)
- 0.5–0.7: flagged for user review (merge is applied but tracked)

LLM configuration is resolved via ``resolve_task_config('script_alias_resolution', storage, book_id)``
with temperature=0.1 for format stability.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ._llm_helpers import chat_completion, extract_json_from_llm_response


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.pipeline.adapter import PipelineStorage

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute(book_id: str, storage: PipelineStorage, config: dict[str, Any]) -> dict:
    """Run Walk 2c alias resolution for a book.

    Collects ALL characters for the book (GLOBAL scope), sends the full list
    to an LLM for alias identification, parses the response, and merges
    duplicate characters.

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
        Summary with keys: ``book_id``, ``characters_collected``,
        ``merge_groups``, ``characters_merged``, ``characters_remaining``,
        ``merges_for_review``, ``merges_rejected``, ``errors``.
    """
    from app.utils import create_llm_client, resolve_task_config

    result: dict[str, Any] = {
        "book_id": book_id,
        "characters_collected": 0,
        "merge_groups": 0,
        "characters_merged": 0,
        "characters_remaining": 0,
        "merges_for_review": 0,
        "merges_rejected": 0,
        "errors": [],
    }

    # Resolve LLM config for alias resolution (GLOBAL scope)
    llm_config = resolve_task_config("script_alias_resolution", storage, book_id)
    client, _ = create_llm_client(config_path=None)
    model_name = llm_config["model_name"]
    temperature = llm_config["temperature"]
    reasoning_effort = llm_config.get("reasoning_effort")
    # Effective prompt override (None when unset) — the GLOBAL alias
    # resolution chat_completion call is inside execute() itself, so the
    # capture is consumed directly below (no per-unit threading needed).
    system_prompt_override = llm_config.get("prompt")

    # ------------------------------------------------------------------
    # GLOBAL scope: collect ALL characters for the book in ONE query.
    # This is THE critical fix for late-introduction — the LLM sees the
    # full character list and can connect names introduced in different
    # chapters (e.g. "the old man" in ch1 ↔ "Gandalf" in ch3).
    # ------------------------------------------------------------------
    characters = storage.execute_query(
        """
        SELECT DISTINCT c.id, c.name, c.aliases
        FROM character c
        JOIN character_book cb ON c.id = cb.character_id
        WHERE cb.book_id = ?
        """,
        (book_id,),
    )

    result["characters_collected"] = len(characters)

    if not characters:
        logger.info("No characters found for book %s — nothing to resolve", book_id)
        return result

    # Invalidate downstream generated/review rows for this book's 2d runs
    # (invalidation DAG: 2c -> 2d).  Marking stale is idempotent.
    _invalidate_downstream(storage, book_id)

    # Build prompt with ALL character names and aliases
    prompt = _build_alias_resolution_prompt(characters)

    # Call LLM
    try:
        response_text = chat_completion(
            client=client,
            model_name=model_name,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            system_prompt=(
                system_prompt_override
                or "You are a literary analyst specializing in character identity "
                "resolution across long narratives."
            ),
            user_prompt=prompt,
        )
    except Exception as e:
        logger.error("LLM call failed for book %s: %s", book_id, e)
        result["errors"].append({"book_id": book_id, "error": str(e)})
        result["characters_remaining"] = len(characters)
        return result

    # Parse response into merge groups
    merge_groups = _parse_llm_response(response_text, characters)
    result["merge_groups"] = len(merge_groups)

    # Build a set of character IDs that are part of any accepted merge
    merged_ids: set[str] = set()

    # Process each merge group with confidence filter
    for group in merge_groups:
        confidence = group.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)):
            confidence = 0.0
        confidence = max(0.0, min(1.0, float(confidence)))

        character_ids = group.get("character_ids", [])
        canonical_name = group.get("canonical_name", "")

        if len(character_ids) < 2:
            # Need at least 2 characters to merge
            continue

        # Confidence filter
        if confidence < 0.5:
            # Auto-reject
            result["merges_rejected"] += 1
            continue

        is_review = 0.5 <= confidence < 0.7

        try:
            _merge_group(
                character_ids=character_ids,
                canonical_name=canonical_name,
                all_characters=characters,
                storage=storage,
                merged_ids=merged_ids,
                result=result,
                is_review=is_review,
            )
        except Exception as e:
            logger.error(
                "Error merging group %s for book %s: %s",
                character_ids,
                book_id,
                e,
            )
            result["errors"].append({
                "character_ids": character_ids,
                "error": str(e),
            })

    # Count remaining characters
    remaining = storage.execute_query(
        """
        SELECT COUNT(*) AS cnt
        FROM character c
        JOIN character_book cb ON c.id = cb.character_id
        WHERE cb.book_id = ?
        """,
        (book_id,),
    )
    result["characters_remaining"] = remaining[0]["cnt"] if remaining else 0

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_alias_resolution_prompt(characters: list[dict]) -> str:
    """Build the LLM prompt for alias resolution across the entire book."""
    character_lines = []
    for char in characters:
        char_id = char["id"]
        name = char["name"]
        aliases_raw = char.get("aliases", "[]")
        if isinstance(aliases_raw, str):
            try:
                aliases = json.loads(aliases_raw)
            except (json.JSONDecodeError, TypeError):
                aliases = []
        elif isinstance(aliases_raw, list):
            aliases = aliases_raw
        else:
            aliases = []

        aliases_str = ", ".join(aliases) if aliases else "none"
        character_lines.append(f"- ID: {char_id} | Name: {name} | Aliases: {aliases_str}")

    characters_text = "\n".join(character_lines)

    prompt = f"""You are analyzing a book's character list to identify which characters are actually the same person referred to by different names or aliases. Below is a list of character names and their current known aliases. Identify which characters should be merged — these are characters that are the same person but appear under different names (e.g., 'the old man' in early chapters may be 'Gandalf' introduced later, or 'Dr. Smith' and 'John Smith' are the same person). Return a JSON array of merge groups. Each merge group contains an array of character UUIDs that should be merged together. Characters NOT in any merge group should remain separate.

Here is the full character list for this book:

{characters_text}

Return a JSON array of merge groups. Each group should include:
- "character_ids": array of character UUIDs that should be merged (at least 2)
- "canonical_name": the preferred name for the merged character (string)
- "confidence": float between 0.0 and 1.0 indicating your confidence in this merge

Return ONLY a JSON array, no other text. Example:
[
  {{"character_ids": ["uuid-1", "uuid-2"], "canonical_name": "Gandalf", "confidence": 0.95}},
  {{"character_ids": ["uuid-3", "uuid-4", "uuid-5"], "canonical_name": "Dr. John Smith", "confidence": 0.85}}
]

If no characters should be merged, return an empty array: []
"""
    return prompt


def _parse_llm_response(
    response_text: str, characters: list[dict]
) -> list[dict]:
    """Parse the LLM response into a list of merge group dicts.

    Validates that all character_ids in each group actually exist in the
    provided character list.
    """
    valid_ids = {char["id"] for char in characters}

    groups = extract_json_from_llm_response(response_text, expected_type="list")
    if groups is None:
        logger.error(
            "Failed to parse LLM response as JSON: %s",
            response_text[:200],
        )
        return []

    if not isinstance(groups, list):
        logger.error("LLM response is not a list: %s", type(groups))
        return []

    result = []
    for entry in groups:
        if not isinstance(entry, dict):
            continue

        character_ids = entry.get("character_ids", [])
        if not isinstance(character_ids, list) or len(character_ids) < 2:
            continue

        # Filter to only valid IDs
        valid_group_ids = [cid for cid in character_ids if cid in valid_ids]
        if len(valid_group_ids) < 2:
            continue

        canonical_name = entry.get("canonical_name", "")
        if not isinstance(canonical_name, str):
            canonical_name = ""

        confidence = entry.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)):
            confidence = 0.0

        result.append({
            "character_ids": valid_group_ids,
            "canonical_name": canonical_name.strip(),
            "confidence": float(confidence),
        })

    return result


def _merge_group(
    character_ids: list[str],
    canonical_name: str,
    all_characters: list[dict],
    storage: PipelineStorage,
    merged_ids: set[str],
    result: dict,
    is_review: bool,
    book_id: str | None = None,
) -> None:
    """Merge a group of characters into one canonical character.

    Non-destructive: junction rows are redirected to the canonical character
    and the merge is recorded as a ``character_alias_merge`` relation (plus a
    generated workbench decision and provenance) so it is fully reversible.
    Member ``character`` rows are retained and remain addressable.

    Uses a SAVEPOINT for atomicity — if any junction update fails, the
    entire merge group is rolled back.
    """
    # Build id→character lookup
    id_to_char = {char["id"]: char for char in all_characters}

    # Determine canonical character:
    # If canonical_name matches a character's name, use that character.
    # Otherwise, use the first character in the list.
    canonical_id = character_ids[0]
    if canonical_name:
        for cid in character_ids:
            char = id_to_char.get(cid)
            if char and char["name"] == canonical_name:
                canonical_id = cid
                break

    non_canonical_ids = [cid for cid in character_ids if cid != canonical_id]

    if not non_canonical_ids:
        return

    # Resolve book_id from an existing character_book junction when the caller
    # did not supply it (keeps direct unit-test callers working).
    if book_id is None:
        cb = storage.execute_query(
            "SELECT book_id FROM character_book WHERE character_id = ? LIMIT 1",
            (canonical_id,),
        )
        book_id = cb[0]["book_id"] if cb else None

    conn = storage.get_connection()
    conn.execute("SAVEPOINT merge_group")
    try:
        decision_id = None
        if book_id is not None:
            decision_id = _record_merge_decision(
                storage, book_id, canonical_id, non_canonical_ids, result
            )

        for nc_id in non_canonical_ids:
            # Non-destructive: keep the member character row addressable and
            # record the merge relation (with prior state) for reversal.
            # Record before junction redirect so member_scene consequences are
            # captured from the member's own junctions.
            if book_id is not None:
                _record_member_merge(
                    storage, book_id, canonical_id, nc_id, decision_id
                )
            _redirect_junctions(storage, canonical_id, nc_id)
            merged_ids.add(nc_id)
            result["characters_merged"] += 1

        # Consolidate aliases on canonical character
        _consolidate_aliases(
            canonical_id=canonical_id,
            non_canonical_ids=non_canonical_ids,
            canonical_name=canonical_name,
            all_characters=all_characters,
            storage=storage,
        )

        conn.execute("RELEASE SAVEPOINT merge_group")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT merge_group")
        conn.execute("RELEASE SAVEPOINT merge_group")
        raise

    if is_review:
        result["merges_for_review"] += 1


# ---------------------------------------------------------------------------
# Workbench-native merge recording helpers
# ---------------------------------------------------------------------------


# Invalidation DAG: 2c -> 2d.
_DOWNSTREAM_WALKS = ("walk_2d_scene_presence",)


def _generation_revision(storage, book_id):
    """Read the current per-book workbench generation revision (0 when unset)."""
    rows = storage.execute_query(
        "SELECT revision FROM workbench_generation WHERE book_id = ?", (book_id,)
    )
    return rows[0]["revision"] if rows else 0


def _now_ms():
    import time

    return int(time.time() * 1000)


def _record_merge_decision(storage, book_id, canonical_id, member_ids, result):
    """Record a generated workbench decision documenting the merge group.

    Returns the decision_id so member merge relations can reference it.
    """
    decision_id = f"decision-{uuid.uuid4().hex}"
    revision = _generation_revision(storage, book_id)
    payload = {
        "canonical_id": canonical_id,
        "member_ids": sorted(member_ids),
        "source": "walk_2c_alias_resolution",
    }
    storage.execute_insert(
        "INSERT INTO workbench_decision "
        "(decision_id, book_id, target_kind, target_key, decision_type, "
        " base_revision, payload_json, status, source, created_ms, supersedes_id) "
        "VALUES (?, ?, 'alias_merge', ?, 'alias_merge:merge', ?, ?, "
        " 'active', 'generated', ?, NULL)",
        (
            decision_id,
            book_id,
            f"{canonical_id}:{','.join(sorted(member_ids))}",
            revision,
            json.dumps(payload),
            _now_ms(),
        ),
    )
    return decision_id


def _record_member_merge(storage, book_id, canonical_id, member_id, decision_id):
    """Record one ``character_alias_merge`` relation for a merged member.

    Captures the member's prior name, aliases and voice assignment so the
    merge is fully reversible, plus the downstream impact (member scenes are
    invalidated for 2d re-runs).
    """
    char = storage.execute_query(
        "SELECT id, name, aliases, voice_assignment_id FROM character WHERE id = ?",
        (member_id,),
    )
    name = char[0]["name"] if char else ""
    aliases_json = char[0]["aliases"] if char else "[]"
    voice_id = char[0]["voice_assignment_id"] if char else None

    member_scenes = storage.execute_query(
        "SELECT scene_id FROM character_scene WHERE character_id = ?", (member_id,)
    )
    consequence_json = json.dumps(
        {
            "downstream_invalidations": {
                "walk_2d_scene_presence": sorted(
                    {r["scene_id"] for r in member_scenes}
                )
            }
        }
    )

    revision = _generation_revision(storage, book_id)
    storage.execute_insert(
        "INSERT INTO character_alias_merge "
        "(merge_id, book_id, canonical_id, member_id, merge_revision, decision_id, "
        " status, prior_member_name, prior_member_aliases_json, "
        " prior_member_voice_assignment_id, consequence_json, created_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (
            f"merge-{uuid.uuid4().hex}",
            book_id,
            canonical_id,
            member_id,
            revision,
            decision_id,
            name,
            aliases_json,
            voice_id,
            consequence_json,
            _now_ms(),
        ),
    )


def _invalidate_downstream(storage, book_id):
    """Mark pending review items from downstream (2d) runs as stale."""
    placeholders = ",".join("?" for _ in _DOWNSTREAM_WALKS)
    storage.execute_update(
        f"UPDATE walk_review_item SET status = 'stale' "
        f"WHERE book_id = ? AND status = 'pending' AND run_id IN "
        f"(SELECT run_id FROM walk_run WHERE walk_name IN ({placeholders}))",
        (book_id, *_DOWNSTREAM_WALKS),
    )


def _redirect_junctions(
    storage: PipelineStorage,
    canonical_id: str,
    non_canonical_id: str,
) -> None:
    """Update all junction tables to point from non-canonical to canonical.

    Handles potential duplicate rows by deleting conflicting rows first.
    """
    # character_book: delete non-canonical rows where canonical already has
    # the same book_id, then update remaining
    storage.execute_update(
        "DELETE FROM character_book "
        "WHERE character_id = ? AND book_id IN "
        "(SELECT book_id FROM character_book WHERE character_id = ?)",
        (non_canonical_id, canonical_id),
    )
    storage.execute_update(
        "UPDATE character_book SET character_id = ? WHERE character_id = ?",
        (canonical_id, non_canonical_id),
    )

    # character_series: same pattern
    storage.execute_update(
        "DELETE FROM character_series "
        "WHERE character_id = ? AND series_id IN "
        "(SELECT series_id FROM character_series WHERE character_id = ?)",
        (non_canonical_id, canonical_id),
    )
    storage.execute_update(
        "UPDATE character_series SET character_id = ? WHERE character_id = ?",
        (canonical_id, non_canonical_id),
    )

    # character_scene: delete where canonical already has same (scene_id, relation_type)
    storage.execute_update(
        "DELETE FROM character_scene "
        "WHERE character_id = ? AND (scene_id, relation_type) IN "
        "(SELECT scene_id, relation_type FROM character_scene WHERE character_id = ?)",
        (non_canonical_id, canonical_id),
    )
    storage.execute_update(
        "UPDATE character_scene SET character_id = ? WHERE character_id = ?",
        (canonical_id, non_canonical_id),
    )

    # character_span: delete where canonical already has same (span_id, relation_type)
    storage.execute_update(
        "DELETE FROM character_span "
        "WHERE character_id = ? AND (span_id, relation_type) IN "
        "(SELECT span_id, relation_type FROM character_span WHERE character_id = ?)",
        (non_canonical_id, canonical_id),
    )
    storage.execute_update(
        "UPDATE character_span SET character_id = ? WHERE character_id = ?",
        (canonical_id, non_canonical_id),
    )

    # character_metadata: delete where canonical already has same key
    storage.execute_update(
        "DELETE FROM character_metadata "
        "WHERE character_id = ? AND key IN "
        "(SELECT key FROM character_metadata WHERE character_id = ?)",
        (non_canonical_id, canonical_id),
    )
    storage.execute_update(
        "UPDATE character_metadata SET character_id = ? WHERE character_id = ?",
        (canonical_id, non_canonical_id),
    )


def _consolidate_aliases(
    canonical_id: str,
    non_canonical_ids: list[str],
    canonical_name: str,
    all_characters: list[dict],
    storage: PipelineStorage,
) -> None:
    """Merge aliases from all non-canonical characters onto the canonical.

    Also adds the canonical_name from the LLM (if different from the
    canonical character's current name) and all non-canonical character
    names to preserve the full name history.
    """
    id_to_char = {char["id"]: char for char in all_characters}

    # Start with canonical's existing aliases
    canonical_char = id_to_char.get(canonical_id)
    if not canonical_char:
        return

    existing_aliases_raw = canonical_char.get("aliases", "[]")
    if isinstance(existing_aliases_raw, str):
        try:
            alias_set: set[str] = set(json.loads(existing_aliases_raw))
        except (json.JSONDecodeError, TypeError):
            alias_set = set()
    elif isinstance(existing_aliases_raw, list):
        alias_set = set(existing_aliases_raw)
    else:
        alias_set = set()

    # Add canonical_name from LLM if different from current name
    current_name = canonical_char["name"]
    if canonical_name and canonical_name != current_name:
        alias_set.add(canonical_name)

    # Add names and aliases from all non-canonical characters
    for nc_id in non_canonical_ids:
        nc_char = id_to_char.get(nc_id)
        if not nc_char:
            continue

        # Add the character's name
        alias_set.add(nc_char["name"])

        # Add their aliases
        nc_aliases_raw = nc_char.get("aliases", "[]")
        if isinstance(nc_aliases_raw, str):
            try:
                nc_aliases = json.loads(nc_aliases_raw)
            except (json.JSONDecodeError, TypeError):
                nc_aliases = []
        elif isinstance(nc_aliases_raw, list):
            nc_aliases = nc_aliases_raw
        else:
            nc_aliases = []

        for alias in nc_aliases:
            if isinstance(alias, str) and alias.strip():
                alias_set.add(alias.strip())

    # Remove the canonical character's current name from aliases
    # (it's the primary name, not an alias)
    alias_set.discard(current_name)

    # Update canonical character's aliases
    aliases_json = json.dumps(sorted(alias_set))
    storage.execute_update(
        "UPDATE character SET aliases = ? WHERE id = ?",
        (aliases_json, canonical_id),
    )
