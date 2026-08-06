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

import json
import os
import sys
import time
import uuid
from typing import Callable

from app.pipeline.adapter import PipelineStorage
from app.pipeline.assembly import export_annotated_script


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
# Render job / chunk row persistence (rows = truth)
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Current wall-clock time as INTEGER unix milliseconds (schema convention)."""
    return int(time.time() * 1000)


def _ensure_render_job_row(
    storage: PipelineStorage,
    job_id: str,
    book_id: str,
    mode: str,
    output_dir: str | None,
) -> None:
    """Create the ``render_job`` row (status ``running``) unless one exists.

    The API layer (``POST /render``) pre-creates the row so the tracked
    ``job_id`` matches its in-process tracker; direct callers of
    ``render_audiobook`` get the row created here.  Rows are the source of
    truth, so every render has exactly one row — the pre-created row is
    reused untouched when present.
    """
    existing = storage.execute_query(
        "SELECT job_id FROM render_job WHERE job_id = ?",
        (job_id,),
    )
    if existing:
        return
    now = _now_ms()
    storage.execute_insert(
        "INSERT INTO render_job "
        "(job_id, book_id, mode, status, output_dir, created_ms, started_ms) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (job_id, book_id, mode, output_dir, now, now),
    )


def _insert_chunk_row(
    storage: PipelineStorage, job_id: str, idx: int, wav_path: str
) -> None:
    """Record a per-chunk ``render_chunk`` row (status ``pending``).

    Individual mode only — batch mode persists job-level rows only.
    """
    storage.execute_insert(
        "INSERT INTO render_chunk (job_id, idx, status, wav_path) "
        "VALUES (?, ?, 'pending', ?)",
        (job_id, idx, wav_path),
    )


def _mark_chunk_done(
    storage: PipelineStorage, job_id: str, idx: int, wav_path: str
) -> None:
    """Transition a chunk row to ``done`` only after the WAV is durable.

    Callers must have completed the 2-fsync discipline first (tmp write →
    ``fsync`` → rename → fsync parent dir); the row update is the very last
    step so a crash between rename and row-update leaves a detectable
    ``pending`` row with a durable file (Phase 2 reconciles that state).
    """
    storage.execute_update(
        "UPDATE render_chunk SET status = 'done', wav_path = ? "
        "WHERE job_id = ? AND idx = ?",
        (wav_path, job_id, idx),
    )


def _mark_chunk_failed(
    storage: PipelineStorage, job_id: str, idx: int, error: str
) -> None:
    """Transition a chunk row to ``failed`` with the recorded error."""
    storage.execute_update(
        "UPDATE render_chunk SET status = 'failed', error = ? "
        "WHERE job_id = ? AND idx = ?",
        (error, job_id, idx),
    )


def _format_batch_failure(failed: list[tuple[int, str] | int]) -> str:
    """Render ``generate_batch``'s ``failed`` list as a readable string.

    Entries are ``(index, error)`` tuples per the ``TTSEngine`` contract;
    bare indices are tolerated defensively.
    """
    parts = []
    for item in failed:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            parts.append(f"chunk {item[0]}: {item[1]}")
        else:
            parts.append(f"chunk {item}")
    return "; ".join(parts)


def _finalize_job(
    storage: PipelineStorage,
    job_id: str,
    status: str,
    *,
    error: str | None = None,
    resolved_dir: str | None = None,
) -> None:
    """Write the final ``render_job`` transition inside a single transaction.

    ``completed`` also records ``output_dir`` and ``output_artifact_path``
    (the ``audiobook.m4b`` path when present, otherwise the output dir).
    ``failed`` records the error; ``cancelled`` transitions status only.
    Always stamps ``finished_ms``.
    """
    with storage.transaction():
        if status == "completed":
            artifact_path = resolved_dir
            if resolved_dir:
                m4b_path = os.path.join(resolved_dir, "audiobook.m4b")
                if os.path.isfile(m4b_path):
                    artifact_path = m4b_path
            storage.execute_update(
                "UPDATE render_job SET status = 'completed', output_dir = ?, "
                "output_artifact_path = ?, finished_ms = ? WHERE job_id = ?",
                (resolved_dir, artifact_path, _now_ms(), job_id),
            )
        elif status == "failed":
            storage.execute_update(
                "UPDATE render_job SET status = 'failed', error = ?, "
                "finished_ms = ? WHERE job_id = ?",
                (error, _now_ms(), job_id),
            )
        else:  # cancelled
            storage.execute_update(
                "UPDATE render_job SET status = 'cancelled', finished_ms = ? "
                "WHERE job_id = ?",
                (_now_ms(), job_id),
            )


# ---------------------------------------------------------------------------
# RENDER_ROOT resolution + fsync discipline + derived manifest
# ---------------------------------------------------------------------------


def get_render_root() -> str:
    """Resolve the durable render root directory.

    Reads ``RENDER_ROOT`` from the environment **at call time** so tests
    and callers can override it per render (e.g. ``monkeypatch.setenv``).
    Falls back to ``data/render_root`` under the current working directory
    (``data/`` is gitignored).
    """
    return os.environ.get("RENDER_ROOT") or os.path.join(
        os.getcwd(), "data", "render_root"
    )


def _resolve_run_dir(book_id: str, job_id: str) -> str:
    """Create and return the durable run dir ``RENDER_ROOT/book-{id}/{job_id}/``."""
    run_dir = os.path.join(get_render_root(), f"book-{book_id}", job_id)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _fsync_file(path: str) -> None:
    """Flush *path*'s contents to stable storage (2-fsync discipline)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    """Flush *path*'s directory entry so a rename inside it is durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_batch_outputs(resolved_dir: str) -> None:
    """Apply the 2-fsync discipline to a batch render's produced files.

    Batch mode has no per-chunk rows (whole-book playback only), so the
    discipline applies at file level: every ``*.wav`` in the run dir is
    fsynced, then the parent directory entry is fsynced.
    """
    for name in sorted(os.listdir(resolved_dir)):
        path = os.path.join(resolved_dir, name)
        if name.endswith(".wav") and os.path.isfile(path):
            _fsync_file(path)
    _fsync_dir(resolved_dir)


def _batch_output_paths(resolved_dir: str) -> list[str]:
    """Enumerate the audio files a batch render produced (best-effort)."""
    paths = []
    for name in sorted(os.listdir(resolved_dir)):
        path = os.path.join(resolved_dir, name)
        if name.endswith(".wav") and os.path.isfile(path):
            paths.append(path)
    return paths


def _write_manifest(
    resolved_dir: str,
    *,
    job_id: str,
    book_id: str,
    mode: str,
    chunk_paths: list[str],
    status: str,
) -> None:
    """Atomically write ``manifest.json`` into the run dir (derived cache).

    The manifest is **derived** — ``render_job`` / ``render_chunk`` rows
    remain the authority.  Chunk ``wav_path`` entries are stored relative to
    the run dir (portable across relocations); the rows carry absolute paths.

    Write is crash-safe: tmp file → ``fsync`` → ``os.replace`` → fsync parent.
    """
    manifest = {
        "job_id": job_id,
        "book_id": book_id,
        "mode": mode,
        "chunk_count": len(chunk_paths),
        "chunks": [
            {"idx": i, "wav_path": os.path.relpath(path, resolved_dir)}
            for i, path in enumerate(chunk_paths)
        ],
        "status": status,
        "created_ms": _now_ms(),
    }
    manifest_path = os.path.join(resolved_dir, "manifest.json")
    tmp_path = f"{manifest_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, manifest_path)
    _fsync_dir(resolved_dir)


def _write_manifest_best_effort(
    resolved_dir: str,
    *,
    job_id: str,
    book_id: str,
    mode: str,
    chunk_paths: list[str],
    status: str,
) -> None:
    """Write the derived manifest without failing the render on cache errors.

    The manifest is regenerated by startup reconciliation (Plan C phase 2),
    so a failed write here is recoverable state, never a render failure.
    """
    try:
        _write_manifest(
            resolved_dir,
            job_id=job_id,
            book_id=book_id,
            mode=mode,
            chunk_paths=chunk_paths,
            status=status,
        )
    except Exception as exc:  # noqa: BLE001 — derived cache; see docstring
        print(f"warning: failed to write manifest for {job_id}: {exc}", file=sys.stderr)


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

    Generates audio via ``TTSEngine.generate_batch`` (batch mode) or
    ``TTSEngine.generate_voice`` (individual mode).  Persists progress to
    ``render_job`` / ``render_chunk`` rows (rows = truth): the job row is
    created ``running`` at start and transitions to ``completed`` in the
    final transaction; individual mode writes one ``render_chunk`` row per
    chunk (``pending`` → ``done`` after the WAV write returns).

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
        Directory for generated audio files.  If ``None``, a durable run
        directory is created at ``RENDER_ROOT/book-{book_id}/{job_id}/``
        (``RENDER_ROOT`` env var, default ``data/render_root``; ``data/``
        is gitignored).  An explicit ``output_dir`` is always honored.

    Individual-mode chunk WAVs follow the 2-fsync discipline: the engine
    writes a ``.tmp`` sibling, it is fsynced, renamed into place, the parent
    dir is fsynced, and only then the ``render_chunk`` row is marked done.
    Batch mode applies the same discipline at file level.  On completion a
    derived ``manifest.json`` (rows stay the authority) is written into the
    run dir atomically.
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
    RuntimeError
        If batch generation reports no completed chunks (all failed).
    """
    # Generate (or reuse) the job identifier
    resolved_job_id = job_id if job_id is not None else str(uuid.uuid4())

    # Rows = truth: ensure the render_job row exists with status 'running'.
    # The API layer pre-creates it; direct callers get it created here.
    mode = "batch" if use_batch else "individual"
    _ensure_render_job_row(storage, resolved_job_id, book_id, mode, output_dir)

    resolved_dir = output_dir
    chunk_paths: list[str] = []
    try:
        # Step 1: Get annotated script from assembly
        script = export_annotated_script(book_id, storage)

        # Step 2: Build voice config mapping
        voice_config = _build_voice_config(script, storage)

        # Step 3: Determine output directory — explicit output_dir wins;
        # otherwise a durable run dir under RENDER_ROOT/book-{id}/{job_id}/.
        resolved_dir = output_dir or _resolve_run_dir(book_id, resolved_job_id)

        # Step 4: Handle empty script — nothing to render, job completes
        if not script:
            _finalize_job(storage, resolved_job_id, "completed", resolved_dir=resolved_dir)
            _write_manifest_best_effort(
                resolved_dir,
                job_id=resolved_job_id,
                book_id=book_id,
                mode=mode,
                chunk_paths=[],
                status="completed",
            )
            return resolved_job_id

        # Step 5: Build chunks and dispatch
        if use_batch:
            # Check cancellation once before the batch dispatch
            if cancel_check is not None and cancel_check():
                raise CancelledError("Render cancelled before batch dispatch")
            chunks = _build_chunks(script)
            result = tts_engine.generate_batch(
                chunks, voice_config, resolved_dir, batch_seed
            )
            # Batch mode persists job-level rows only (no per-chunk rows), so
            # interpret the engine's report at job level.  A ``None`` result
            # (some engines / test doubles) reports no failures — treat as
            # all chunks completed.  When every reported outcome is a
            # failure the job itself failed; any completed chunk means the
            # job completed (partial batch failures surface in Plan C
            # reconciliation and are not representable without chunk rows).
            if result is None:
                result = {"completed": [c["index"] for c in chunks], "failed": []}
            failed = list(result.get("failed") or [])
            completed = list(result.get("completed") or [])
            if failed and not completed:
                raise RuntimeError(
                    f"Batch render failed: all {len(failed)} chunks failed "
                    f"({_format_batch_failure(failed)})"
                )
            # Batch mode has no per-chunk rows; apply the 2-fsync discipline
            # at file level to everything the engine produced, then collect
            # the produced paths for the derived manifest.
            _fsync_batch_outputs(resolved_dir)
            chunk_paths = _batch_output_paths(resolved_dir)
        else:
            # Individual generation — check cancellation before each chunk
            for i, entry in enumerate(script):
                if cancel_check is not None and cancel_check():
                    raise CancelledError(f"Render cancelled before chunk {i}")
                speaker = entry["speaker"]
                text = entry["text"]
                instruct = entry.get("instruct", "")
                final_path = os.path.join(resolved_dir, f"chunk_{i:04d}.wav")
                tmp_path = f"{final_path}.tmp"
                _insert_chunk_row(storage, resolved_job_id, i, final_path)
                try:
                    # 2-fsync discipline: write to the tmp sibling, fsync the
                    # file, rename into the final place, fsync the parent dir
                    # — and only then mark the chunk row done (rows = truth).
                    tts_engine.generate_voice(
                        text, instruct, speaker, voice_config, tmp_path
                    )
                    _fsync_file(tmp_path)
                    os.replace(tmp_path, final_path)
                    _fsync_dir(resolved_dir)
                except Exception as exc:
                    # Record the failed chunk, then propagate — the caller
                    # (and the finalize below) marks the job failed.
                    _mark_chunk_failed(storage, resolved_job_id, i, str(exc))
                    raise
                _mark_chunk_done(storage, resolved_job_id, i, final_path)
                chunk_paths.append(final_path)
    except CancelledError:
        _finalize_job(storage, resolved_job_id, "cancelled")
        raise
    except Exception as exc:
        _finalize_job(storage, resolved_job_id, "failed", error=str(exc))
        raise

    # Final transaction: job completed + output artifact path
    _finalize_job(storage, resolved_job_id, "completed", resolved_dir=resolved_dir)
    # Derived cache (rows stay the authority): written after the row reaches
    # 'completed' so a missing manifest is exactly the state Phase 2 rebuilds.
    _write_manifest_best_effort(
        resolved_dir,
        job_id=resolved_job_id,
        book_id=book_id,
        mode=mode,
        chunk_paths=chunk_paths,
        status="completed",
    )

    return resolved_job_id
