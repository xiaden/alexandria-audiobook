"""TTS integration — bridges pipeline output to the existing TTSEngine.

Provides ``render_audiobook`` which takes the annotated script from
``export_annotated_script``, maps speakers to voice configurations, and
dispatches audio generation through ``TTSEngine.generate_batch`` (batch
mode) or ``TTSEngine.generate_voice`` (individual mode).

Usage::

    from app.pipeline.adapter import PipelineStorage
    from app.pipeline.tts_integration import render_audiobook

    storage: PipelineStorage = ...
    tts_engine: TTSEngine = ...
    job_id = render_audiobook("book-001", storage, tts_engine)
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import TYPE_CHECKING, Callable

from app.pipeline.adapter import PipelineStorage
from app.pipeline.assembly import export_annotated_script

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CancelledError(Exception):
    """Raised when a render job is cancelled via the cancel_check callback."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default voice configuration for the NARRATOR speaker.
#: Used when a span has no speaker attribution (UNKNOWN→NARRATOR).
NARRATOR_VOICE: dict = {
    "type": "custom",
    "voice": "Ryan",
}

#: batch_seed value indicating random (non-reproducible) generation.
BATCH_SEED_RANDOM: int = -1


# ---------------------------------------------------------------------------
# Voice config resolution
# ---------------------------------------------------------------------------


def _build_voice_config(
    script: list[dict], storage: PipelineStorage
) -> dict[str, dict]:
    """Build a speaker → voice-config mapping for all speakers in *script*.

    For ``'NARRATOR'`` the ``voice_config`` table is queried for a row with
    ``id='NARRATOR'``.  If found, all columns from that row are used to build
    the voice config (type, voice, description, etc.).  If no NARRATOR row
    exists in the database, the module-level ``NARRATOR_VOICE`` constant is
    used as a fallback.

    For character speakers the ``voice_assignment_id`` is looked up from the
    ``character`` table and the corresponding ``voice_config`` row provides
    the voice name and description.

    Parameters
    ----------
    script:
        Annotated script entries from ``export_annotated_script``.
    storage:
        An active ``PipelineStorage`` implementation.

    Returns
    -------
    dict[str, dict]
        Mapping of speaker name to voice config dict suitable for
        ``TTSEngine.generate_batch`` and ``TTSEngine.generate_voice``.
    """
    # Collect unique non-NARRATOR speaker names
    speaker_names: set[str] = set()
    for entry in script:
        speaker = entry["speaker"]
        if speaker != "NARRATOR":
            speaker_names.add(speaker)

    voice_config: dict[str, dict] = {}

    # Add NARRATOR if present in script
    has_narrator = any(e["speaker"] == "NARRATOR" for e in script)
    if has_narrator:
        # Try to resolve NARRATOR from voice_config table first
        narrator_rows = storage.execute_query(
            "SELECT type, voice, description, character_style, seed, "
            "ref_audio, ref_text, adapter_id, adapter_path, alias_of "
            "FROM voice_config WHERE id = 'NARRATOR'",
        )
        if narrator_rows:
            row = narrator_rows[0]
            voice_config["NARRATOR"] = {
                "type": row.get("type") or "custom",
                "voice": row.get("voice") or "",
                "character_style": row.get("character_style") or "",
                "seed": row.get("seed") or "-1",
                "ref_audio": row.get("ref_audio"),
                "ref_text": row.get("ref_text"),
                "adapter_id": row.get("adapter_id"),
                "adapter_path": row.get("adapter_path"),
                "description": row.get("description") or "",
                "alias_of": row.get("alias_of"),
            }
        else:
            # Fallback to hardcoded constant
            voice_config["NARRATOR"] = dict(NARRATOR_VOICE)

    # Resolve character speakers via character → voice_config tables
    if speaker_names:
        # Query all characters with their voice assignments in one pass
        placeholders = ", ".join("?" for _ in speaker_names)
        rows = storage.execute_query(
            f"""
            SELECT c.name AS character_name,
                   c.voice_assignment_id,
                   vc.id AS vc_id,
                   vc.name AS vc_name,
                   vc.description AS vc_description,
                   vc.type AS vc_type,
                   vc.voice AS vc_voice,
                   vc.character_style AS vc_character_style,
                   vc.seed AS vc_seed,
                   vc.ref_audio AS vc_ref_audio,
                   vc.ref_text AS vc_ref_text,
                   vc.adapter_id AS vc_adapter_id,
                   vc.adapter_path AS vc_adapter_path,
                   vc.alias_of AS vc_alias_of
              FROM character c
              LEFT JOIN voice_config vc ON c.voice_assignment_id = vc.id
             WHERE c.name IN ({placeholders})
            """,
            tuple(speaker_names),
        )

        # Build a lookup by character name
        char_voice_map: dict[str, dict] = {}
        for row in rows:
            char_voice_map[row["character_name"]] = row

        # Build voice config for each speaker
        for speaker in speaker_names:
            info = char_voice_map.get(speaker)
            if info and info.get("voice_assignment_id") and info.get("vc_id"):
                voice_config[speaker] = {
                    "type": info.get("vc_type") or "custom",
                    "voice": info.get("vc_name") or "",
                    "character_style": info.get("vc_character_style") or "",
                    "seed": info.get("vc_seed") or "-1",
                    "ref_audio": info.get("vc_ref_audio"),
                    "ref_text": info.get("vc_ref_text"),
                    "adapter_id": info.get("vc_adapter_id"),
                    "adapter_path": info.get("vc_adapter_path"),
                    "description": info.get("vc_description") or "",
                    "alias_of": info.get("vc_alias_of"),
                }
            else:
                # Fallback: character exists but has no voice assignment
                # Use NARRATOR voice as safe default
                voice_config[speaker] = dict(NARRATOR_VOICE)

    return voice_config


# ---------------------------------------------------------------------------
# Chunk construction
# ---------------------------------------------------------------------------


def _build_chunks(script: list[dict]) -> list[dict]:
    """Convert annotated script entries to TTSEngine chunk format.

    Each chunk has the keys required by ``TTSEngine.generate_batch``:
    ``index`` (0-based), ``text``, ``instruct``, and ``speaker``.

    Parameters
    ----------
    script:
        Annotated script entries from ``export_annotated_script``.

    Returns
    -------
    list[dict]
        Chunks ready for ``TTSEngine.generate_batch``.
    """
    chunks: list[dict] = []
    for i, entry in enumerate(script):
        chunks.append(
            {
                "index": i,
                "text": entry["text"],
                "instruct": entry.get("instruct", ""),
                "speaker": entry["speaker"],
            }
        )
    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_audiobook(
    book_id: str,
    storage: PipelineStorage,
    tts_engine: object,
    *,
    use_batch: bool = True,
    output_dir: str | None = None,
    batch_seed: int = BATCH_SEED_RANDOM,
    job_id: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Render an audiobook from the pipeline's annotated script.

    Bridges the deterministic assembly output (``export_annotated_script``)
    to the existing ``TTSEngine``.  Speakers are mapped to voice
    configurations: ``'NARRATOR'`` uses the default narrator voice, and
    character speakers are resolved via the ``character.voice_assignment_id``
    → ``voice_config`` lookup chain.

    Parameters
    ----------
    book_id:
        Primary key of the book to render.
    storage:
        An active ``PipelineStorage`` implementation.
    tts_engine:
        A ``TTSEngine`` instance (or compatible duck-type with
        ``generate_batch`` and ``generate_voice`` methods).
    use_batch:
        When ``True`` (default), uses ``tts_engine.generate_batch`` for
        efficient batch generation.  When ``False``, loops over chunks
        and calls ``tts_engine.generate_voice`` individually.
    output_dir:
        Directory for generated audio files.  If ``None``, a temporary
        directory is created automatically.
    batch_seed:
        Seed for reproducible batch generation (``BATCH_SEED_RANDOM`` for random).
    job_id:
        Optional pre-allocated job identifier.  When ``None`` a new UUID is
        generated.  The API layer passes the job_id it registered in its
        render-job tracker so the returned value matches the tracked entry.
    cancel_check:
        Optional zero-argument callable returning ``True`` when the render
        should abort.  Invoked before each chunk (individual mode) or once
        before the batch dispatch (batch mode).  When triggered, raises
        :class:`CancelledError`.

    Returns
    -------
    str
        The job identifier for this render.

    Raises
    ------
    CancelledError
        If ``cancel_check()`` returns ``True`` during rendering.
    """
    # Generate (or reuse) the job identifier
    resolved_job_id = job_id if job_id is not None else str(uuid.uuid4())

    # Step 1: Get annotated script from assembly
    script = export_annotated_script(book_id, storage)

    # Step 2: Build voice config mapping
    voice_config = _build_voice_config(script, storage)

    # Step 3: Determine output directory
    resolved_dir = output_dir or tempfile.mkdtemp(prefix=f"audiobook_{book_id}_")

    # Step 4: Handle empty script
    if not script:
        return resolved_job_id

    # Step 5: Build chunks and dispatch
    if use_batch:
        # Check cancellation once before the batch dispatch
        if cancel_check is not None and cancel_check():
            raise CancelledError("Render cancelled before batch dispatch")
        chunks = _build_chunks(script)
        tts_engine.generate_batch(chunks, voice_config, resolved_dir, batch_seed)
    else:
        # Individual generation — check cancellation before each chunk
        for i, entry in enumerate(script):
            if cancel_check is not None and cancel_check():
                raise CancelledError(
                    f"Render cancelled before chunk {i}"
                )
            speaker = entry["speaker"]
            text = entry["text"]
            instruct = entry.get("instruct", "")
            output_path = os.path.join(resolved_dir, f"chunk_{i:04d}.wav")
            tts_engine.generate_voice(
                text, instruct, speaker, voice_config, output_path
            )

    return resolved_job_id
