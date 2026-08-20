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
import re
import sys
import time
import uuid
from typing import Callable

from pydub import AudioSegment

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

#: Global pause defaults — mirror the ``TTSConfig`` pydantic defaults
#: (app/app.py) and the legacy ``combine_audio_with_pauses`` defaults
#: (app/tts.py).  Carried through the render boundary (Plan L) so the
#: effective pair (request → book → config → these defaults) resolved by
#: ``resolve_effective_pauses`` is applied to the paused assembly.  The
#: per-span ``pause_after_ms`` variant is separately persisted (P1) and
#: applied per-boundary by ``_assemble_paused_artifact`` (P3).
PAUSE_BETWEEN_SPEAKERS_MS: int = 500
PAUSE_SAME_SPEAKER_MS: int = 250

#: Documented maximum acceptable pause value in milliseconds (Plan L).
#: Pauses are bounded non-negative integers; ``10_000`` ms (10 s) is the
#: documented ceiling, shared by config, project/book override, and per-span
#: edit validation (``validate_pause_ms``) and by the render resolver
#: (``resolve_effective_pauses``).  No unbounded values are accepted.
PAUSE_MAX_MS: int = 10_000

#: Canonical filename of the deterministic render-time paused artifact (Plan L
#: P3-S4).  This is the whole-book WAV produced by ``combine_audio_with_pauses``
#: during ``render_audiobook``, after all per-span TTS WAVs are durably written.
#: Phase 4 export surfaces (M4B/MP3/Audacity/whole-book audio) consume this file.
PAUSED_ARTIFACT_NAME: str = "audiobook-paused.wav"


def validate_pause_ms(value: object) -> int:
    """Validate and return a pause value as a bounded integer millisecond count.

    Plan L pause contract — a pause is a non-negative integer in
    ``[0, PAUSE_MAX_MS]`` (``10_000`` ms).  ``0`` is an intentional no-gap
    override, distinct from ``NULL``/missing which means "resolve the
    applicable default"; it is never coerced.  Rejects negative, fractional,
    boolean, NaN/±infinity, string, and above-maximum values with
    ``ValueError``.

    This is the single shared source of truth for the pause bound: the
    ``TTSConfig`` field validators in app.py (config saves → 422), the
    project/book override and per-span edit endpoints (Plan L Phase 2 → 422),
    and the render resolver all call it, so a bound change is one constant
    plus one function.
    """
    if isinstance(value, bool):
        raise ValueError("pause must be an integer, not a boolean")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("pause must be a finite integer, not NaN/infinity")
        truncated = int(value)
        if truncated != value:
            raise ValueError("pause must be an integer (no fractional milliseconds)")
        value = truncated
    if not isinstance(value, int):
        raise ValueError("pause must be an integer millisecond value")
    if value < 0:
        raise ValueError("pause must be non-negative")
    if value > PAUSE_MAX_MS:
        raise ValueError(f"pause exceeds the maximum of {PAUSE_MAX_MS} ms")
    return value


def _resolve_pause_field(
    *tiers: dict | None,
    key: str,
    fallback: int,
) -> int:
    """First non-``None`` *key* across *tiers*, else *fallback* (per field).

    Walk the precedence tiers in order; the first tier that carries the key
    with a non-``None`` value wins.  ``0`` is honored (a non-``None`` value).
    ``None``/missing at a higher tier means "resolve the next tier".  The
    winning value is validated via ``validate_pause_ms``.
    """
    for tier in tiers:
        if tier is None:
            continue
        value = tier.get(key)
        if value is None:
            continue
        return validate_pause_ms(value)
    return fallback


def resolve_effective_pauses(
    *,
    request_overrides: dict | None = None,
    book_overrides: dict | None = None,
    config_defaults: dict | None = None,
) -> tuple[int, int]:
    """Resolve the effective pause pair by documented precedence (Plan L).

    Per field, precedence is: render-request override → project/book override
    → persisted config default → ``PAUSE_BETWEEN_SPEAKERS_MS`` /
    ``PAUSE_SAME_SPEAKER_MS`` fallback.  A ``NULL``/missing value at a higher
    tier means "resolve the next tier"; ``0`` is an intentional no-gap
    override and is honored (never coerced to the fallback).

    Each tier is a dict with optional ``pause_between_speakers_ms`` /
    ``pause_same_speaker_ms`` keys (``None`` or missing → fall through).
    Returns the resolved ``(between, same)`` pair, each validated via
    ``validate_pause_ms`` so no unbounded value can reach the renderer.
    """
    between = _resolve_pause_field(
        request_overrides,
        book_overrides,
        config_defaults,
        key="pause_between_speakers_ms",
        fallback=PAUSE_BETWEEN_SPEAKERS_MS,
    )
    same = _resolve_pause_field(
        request_overrides,
        book_overrides,
        config_defaults,
        key="pause_same_speaker_ms",
        fallback=PAUSE_SAME_SPEAKER_MS,
    )
    return between, same


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
        voice_config["NARRATOR"] = _resolve_narrator_config(storage)

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


def _resolve_narrator_config(storage: PipelineStorage) -> dict:
    """Resolve the NARRATOR voice config: DB row wins, constant fallback.

    Queries the ``voice_config`` table for the ``'NARRATOR'`` row; when found,
    all columns are mapped into the voice-config shape.  When no row exists
    the module-level ``NARRATOR_VOICE`` constant is used (the same fallback
    ``_build_voice_config`` has always applied).

    Shared by ``_build_voice_config`` (NARRATOR spans in the script) and
    ``_enforce_single_speaker`` (books rendered single-speaker even when no
    NARRATOR span exists) so both paths resolve the narrator identically.
    """
    narrator_rows = storage.execute_query(
        "SELECT type, voice, description, character_style, seed, "
        "ref_audio, ref_text, adapter_id, adapter_path, alias_of "
        "FROM voice_config WHERE id = 'NARRATOR'",
    )
    if narrator_rows:
        row = narrator_rows[0]
        return {
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
    return dict(NARRATOR_VOICE)


def _enforce_single_speaker(
    voice_config: dict[str, dict],
    storage: PipelineStorage,
    book_id: str,
) -> dict[str, dict]:
    """Force every speaker to the NARRATOR voice config when single-speaker.

    Render-boundary-only normalization (DD decision #9, open item #7): reads
    ``book.single_speaker`` and, when set, replaces every entry in
    *voice_config* — including the ``NARRATOR`` entry itself, which may be
    absent when the script has no NARRATOR spans — with a copy of the
    resolved NARRATOR config.  The annotated-script export is untouched:
    the editor keeps real multi-speaker data, only the render forces a
    single voice (audition-multi-voice-then-ship-single workflow).

    When ``single_speaker`` is 0 (or the book row is missing) the mapping
    is returned unchanged.  The same dict is passed to both the batch
    (``generate_batch``) and individual (``generate_voice`` per chunk)
    dispatch paths, so overriding it here covers both modes.
    """
    rows = storage.execute_query(
        "SELECT single_speaker FROM book WHERE id = ?", (book_id,)
    )
    if not rows or not rows[0].get("single_speaker"):
        return voice_config
    narrator_config = _resolve_narrator_config(storage)
    voice_config["NARRATOR"] = dict(narrator_config)
    for speaker in list(voice_config):
        voice_config[speaker] = dict(narrator_config)
    return voice_config


# ---------------------------------------------------------------------------
# Chunk construction
# ---------------------------------------------------------------------------


def _build_chunks(
    script: list[dict],
    *,
    pause_between_speakers_ms: int = PAUSE_BETWEEN_SPEAKERS_MS,
    pause_same_speaker_ms: int = PAUSE_SAME_SPEAKER_MS,
) -> list[dict]:
    """Convert annotated script entries to TTSEngine chunk format.

    Each chunk has the keys required by ``TTSEngine.generate_batch``:
    ``index`` (0-based), ``text``, ``instruct``, and ``speaker``.  The
    global pause values (``pause_between_speakers_ms`` /
    ``pause_same_speaker_ms``) are carried on every chunk so the resolved
    effective pair survives the render boundary.  The per-span
    ``pause_after_ms`` variant is applied separately by
    ``_assemble_paused_artifact``.

    Parameters
    ----------
    script:
        Annotated script entries from ``export_annotated_script``.
    pause_between_speakers_ms:
        Global silence (ms) between different speakers (default 500).
    pause_same_speaker_ms:
        Global silence (ms) when the same speaker continues (default 250).

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
                "pause_between_speakers_ms": pause_between_speakers_ms,
                "pause_same_speaker_ms": pause_same_speaker_ms,
            }
        )
    return chunks


def _resolve_pause_ms(tts_config: dict | None) -> tuple[int, int]:
    """Resolve the global pause values from a TTSConfig dict.

    ``pause_between_speakers_ms`` / ``pause_same_speaker_ms`` fall back to
    ``PAUSE_BETWEEN_SPEAKERS_MS`` / ``PAUSE_SAME_SPEAKER_MS`` (500 / 250 ms
    — the ``TTSConfig`` pydantic defaults) per field, so a partial config
    keeps the untouched field's default.  ``None`` (no config) returns both
    defaults.
    """
    if not tts_config:
        return PAUSE_BETWEEN_SPEAKERS_MS, PAUSE_SAME_SPEAKER_MS
    between = tts_config.get(
        "pause_between_speakers_ms", PAUSE_BETWEEN_SPEAKERS_MS
    )
    same = tts_config.get("pause_same_speaker_ms", PAUSE_SAME_SPEAKER_MS)
    return between, same


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
    paused_artifact_path: str | None = None,
) -> None:
    """Write the final ``render_job`` transition inside a single transaction.

    ``completed`` also records ``output_dir`` and ``output_artifact_path``.
    The artifact priority is: the canonical paused artifact (when present and
    committed) → ``audiobook.m4b`` → the output dir.  ``failed`` records the
    error; ``cancelled`` transitions status only.  Always stamps ``finished_ms``.
    """
    with storage.transaction():
        if status == "completed":
            artifact_path = resolved_dir
            if paused_artifact_path and os.path.isfile(paused_artifact_path):
                artifact_path = paused_artifact_path
            elif resolved_dir:
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


def _ordered_batch_paths(resolved_dir: str, script: list[dict]) -> list[str]:
    """Return batch output paths ordered to match the render-time script.

    Batch mode has no per-chunk rows, so the produced ``temp_batch_<idx>.wav``
    files must be re-aligned to the in-memory script's order.  The chunk index
    in the filename equals the script position (``_build_chunks`` assigns
    ``index = enumerate(script)``), so position *i* of the script maps to
    ``temp_batch_<i>.wav``.  Lexicographic sorting breaks past 9 chunks
    (``temp_batch_10`` before ``temp_batch_2``), so we order by the numeric
    chunk index.  A chunk whose file is missing (partial batch failure) is
    dropped — the assembly length check then fails non-fatally.
    """
    by_index: dict[int, str] = {}
    for name in os.listdir(resolved_dir):
        match = re.fullmatch(r"temp_batch_(\d+)\.wav", name)
        if match:
            by_index[int(match.group(1))] = os.path.join(resolved_dir, name)
    return [by_index[i] for i in range(len(script)) if i in by_index]


def _write_manifest(
    resolved_dir: str,
    *,
    job_id: str,
    book_id: str,
    mode: str,
    chunk_paths: list[str],
    status: str,
    paused_artifact: str | None = None,
) -> None:
    """Atomically write ``manifest.json`` into the run dir (derived cache).

    The manifest is **derived** — ``render_job`` / ``render_chunk`` rows
    remain the authority.  Chunk ``wav_path`` entries are stored relative to
    the run dir (portable across relocations); the rows carry absolute paths.

    When *paused_artifact* (the canonical whole-book paused WAV) is provided it
    is recorded as a relative ``paused_artifact`` entry.  It is only written
    after the artifact has been atomically committed, so its presence in the
    manifest is proof the canonical artifact exists.

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
    if paused_artifact:
        manifest["paused_artifact"] = os.path.relpath(paused_artifact, resolved_dir)
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
    paused_artifact: str | None = None,
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
            paused_artifact=paused_artifact,
        )
    except Exception as exc:  # noqa: BLE001 — derived cache; see docstring
        print(f"warning: failed to write manifest for {job_id}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# P3: Deterministic render-time paused assembly
# ---------------------------------------------------------------------------


def _reject_unsafe_assembly_paths(
    wav_paths: list[str],
    run_dir: str,
) -> None:
    """Reject missing / non-WAV / out-of-run-dir paths **before** any loading.

    Defensive validation (Plan L P3-S2): every input path must resolve to a
    real file *inside* the run dir.  Absolute paths outside the run dir, path
    traversal (``..``), symlink escapes, missing files, and non-``.wav``
    inputs are all rejected up front so we never hand a hostile path to
    ``AudioSegment.from_wav``.
    """
    run_real = os.path.realpath(run_dir)
    for path in wav_paths:
        if not isinstance(path, str) or not path:
            raise ValueError("paused assembly: empty or non-string WAV path")
        if not path.endswith(".wav"):
            raise ValueError(f"paused assembly: not a .wav path: {path!r}")
        real = os.path.realpath(path)
        # Must live strictly inside the run dir (traversal / absolute-out-of-run
        # both fail this containment check).
        if not (real == run_real or real.startswith(run_real + os.sep)):
            raise ValueError(f"paused assembly: path outside run dir: {path!r}")
        if not os.path.isfile(real):
            raise ValueError(f"paused assembly: missing WAV file: {path!r}")


def _assemble_paused_artifact(
    wav_paths: list[str],
    script: list[dict],
    *,
    pause_between_speakers_ms: int,
    pause_same_speaker_ms: int,
    span_pause_after_ms: dict[str, int | None],
    run_dir: str,
    output_path: str,
) -> None:
    """Deterministic post-render assembly of the canonical paused artifact.

    Receives the ordered per-span WAV paths plus the ordered in-memory
    ``script`` (each entry carries the span ``id`` and ``speaker``) so the
    speaker order is taken from the render-time script — NOT from filenames or
    the manifest.  Loads each WAV via ``pydub.AudioSegment.from_wav``, validates
    they share a frame rate / channels / sample width, resolves nullable
    per-span pause overrides (``None`` → default logic, ``0`` → explicit
    no-gap, positive → explicit override), and calls the existing immutable
    ``combine_audio_with_pauses`` helper.

    The ``pause_overrides`` list is aligned with the segments: entry *i* is the
    pause inserted after segment *i* (the helper ignores the final entry).  A
    per-span override is ``None`` when the span resolves its default (speaker
    change → *pause_between_speakers_ms*, same speaker → *pause_same_speaker_ms*);
    ``0`` inserts a true zero-gap; a positive value inserts that exact pause.

    The combined segment is exported preserving the source format, written to a
    temp sibling, ``fsync``-ed, atomically renamed over *output_path* (same run
    dir), and the parent dir is fsync-ed.  Temp files are removed on **every**
    error path via ``finally``.
    """
    if len(wav_paths) != len(script):
        raise ValueError(
            "paused assembly: WAV path count does not match script "
            f"({len(wav_paths)} != {len(script)})"
        )
    _reject_unsafe_assembly_paths(wav_paths, run_dir)

    segments = [AudioSegment.from_wav(path) for path in wav_paths]
    ref_frame_rate = segments[0].frame_rate
    ref_channels = segments[0].channels
    ref_sample_width = segments[0].sample_width
    for segment in segments[1:]:
        if (
            segment.frame_rate != ref_frame_rate
            or segment.channels != ref_channels
            or segment.sample_width != ref_sample_width
        ):
            raise ValueError(
                "paused assembly: source WAVs differ in format "
                f"({ref_frame_rate}/{ref_channels}/{ref_sample_width} vs "
                f"{segment.frame_rate}/{segment.channels}/{segment.sample_width})"
            )

    # Speaker order and per-span overrides come from the in-memory script.
    from app.tts import combine_audio_with_pauses  # local import: app.tts chain

    speakers = [entry["speaker"] for entry in script]
    pause_overrides = [
        span_pause_after_ms.get(entry["id"]) for entry in script
    ]
    combined = combine_audio_with_pauses(
        segments,
        speakers,
        pause_ms=pause_between_speakers_ms,
        same_speaker_pause_ms=pause_same_speaker_ms,
        pause_overrides=pause_overrides,
    )

    # Preserve the source format explicitly (the helper's silent() gaps default
    # to 11025 Hz and _sync() would otherwise bump low-rate sources up).
    combined = (
        combined.set_frame_rate(ref_frame_rate)
        .set_channels(ref_channels)
        .set_sample_width(ref_sample_width)
    )

    tmp_path = f"{output_path}.tmp"
    try:
        combined.export(tmp_path, format="wav")
        _fsync_file(tmp_path)
        os.replace(tmp_path, output_path)
        _fsync_dir(run_dir)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:  # best-effort cleanup
                pass


def _span_pause_overrides(
    storage: PipelineStorage, script: list[dict]
) -> dict[str, int | None]:
    """Load each span's raw ``pause_after_ms`` keyed by span id (P3-S1).

    Only the spans referenced by *script* are queried.  A missing span or a
    ``NULL`` value maps to ``None`` (resolve default); ``0`` is kept as an
    explicit no-gap.  Values are read straight from the row (already bounded by
    ``validate_pause_ms`` at the write boundary / schema CHECK).
    """
    if not script:
        return {}
    ids = tuple(entry["id"] for entry in script)
    placeholders = ",".join("?" * len(ids))
    rows = storage.execute_query(
        f"SELECT id, pause_after_ms FROM span WHERE id IN ({placeholders})",
        ids,
    )
    return {row["id"]: row["pause_after_ms"] for row in rows}


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
    tts_config: dict | None = None,
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
    tts_config:
        Optional TTS config dict (the ``tts`` section of ``config.json``,
        e.g. from ``load_tts_config``).  Serves as the config tier of the
        effective-pause resolution (request → book → config → 500 / 250 ms
        fallback): its global pause values (``pause_between_speakers_ms`` /
        ``pause_same_speaker_ms``) apply when the current book's nullable
        pause columns are NULL.  The resolved pair is carried onto the batch
        chunk dicts and threaded into the render-time paused assembly
        (``_assemble_paused_artifact``).  Individual mode has no chunk
        channel (per-span preview clips are generated standalone) but the
        resolved pair still feeds the paused assembly.

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

        # Step 4b: Single-speaker render boundary — when book.single_speaker is
        # set, force every speaker's voice config to the NARRATOR config.  This
        # is render-only normalization: the script from export_annotated_script
        # (Step 1) and the chunk ``speaker`` fields below stay faithful.
        voice_config = _enforce_single_speaker(voice_config, storage, book_id)

        # Step 4c: Resolve the effective pause pair by documented precedence
        # (request → book → config → 500/250).  The current book's nullable
        # pause_between_speakers_ms / pause_same_speaker_ms overrides (NULL =
        # fall through to the config tier, 0 = intentional no-gap) are read
        # from storage, then resolve_effective_pauses applies the full
        # precedence against the config tier (the ``tts`` section carried in
        # as ``tts_config``).  The resolved pair is threaded into both the
        # batch chunk boundary (_build_chunks) and the render-time paused
        # assembly (_assemble_paused_artifact) so a book-tier override
        # genuinely reaches the audio — matching the value export reports.
        book_pause_rows = storage.execute_query(
            "SELECT pause_between_speakers_ms, pause_same_speaker_ms"
            " FROM book WHERE id = ?",
            (book_id,),
        )
        book_pause_overrides = book_pause_rows[0] if book_pause_rows else {}
        pause_between_ms, pause_same_ms = resolve_effective_pauses(
            book_overrides=book_pause_overrides, config_defaults=tts_config
        )

        # Step 5: Build chunks and dispatch
        if use_batch:
            # Check cancellation once before the batch dispatch
            if cancel_check is not None and cancel_check():
                raise CancelledError("Render cancelled before batch dispatch")
            chunks = _build_chunks(
                script,
                pause_between_speakers_ms=pause_between_ms,
                pause_same_speaker_ms=pause_same_ms,
            )
            result = tts_engine.generate_batch(
                chunks,
                voice_config,
                resolved_dir,
                batch_seed,
                cancel_check=cancel_check,
            )
            if result and result.get("cancelled"):
                raise CancelledError("Render cancelled during batch dispatch")
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

    # Step 6 (P3): Deterministic render-time paused assembly.  Runs after every
    # per-span WAV is durably written (chunk rows done / batch files fsynced)
    # and before the final job transaction, while speaker + pause metadata is
    # still in-memory.  The canonical paused artifact becomes the job's
    # output_artifact_path.  Assembly is best-effort: a failure (e.g. missing/
    # malformed WAV) must NOT fail the render — the job still completes with
    # the per-span WAVs intact and pauses_applied stays false (tri-state).
    paused_artifact_path = None
    try:
        paused_artifact_path = os.path.join(resolved_dir, PAUSED_ARTIFACT_NAME)
        # Speaker order follows the render-time script.  Individual-mode
        # chunk_paths are already script-ordered; batch mode re-aligns the
        # temp_batch_<idx>.wav files by numeric chunk index.
        assembly_paths = (
            _ordered_batch_paths(resolved_dir, script)
            if mode == "batch"
            else chunk_paths
        )
        span_pause = _span_pause_overrides(storage, script)
        _assemble_paused_artifact(
            assembly_paths,
            script,
            pause_between_speakers_ms=pause_between_ms,
            pause_same_speaker_ms=pause_same_ms,
            span_pause_after_ms=span_pause,
            run_dir=resolved_dir,
            output_path=paused_artifact_path,
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal; see docstring
        paused_artifact_path = None
        print(
            f"warning: paused assembly skipped for {resolved_job_id}: {exc}",
            file=sys.stderr,
        )

    # Final transaction: job completed + output artifact path
    _finalize_job(
        storage,
        resolved_job_id,
        "completed",
        resolved_dir=resolved_dir,
        paused_artifact_path=paused_artifact_path,
    )
    # Derived cache (rows stay the authority): written after the row reaches
    # 'completed' so a missing manifest is exactly the state Phase 2 rebuilds.
    _write_manifest_best_effort(
        resolved_dir,
        job_id=resolved_job_id,
        book_id=book_id,
        mode=mode,
        chunk_paths=chunk_paths,
        status="completed",
        paused_artifact=paused_artifact_path,
    )

    return resolved_job_id
