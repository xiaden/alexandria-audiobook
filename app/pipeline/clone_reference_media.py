"""Pipeline-native clone-reference media helpers.

Narrowly scoped upload validation and persistence for clone-voice reference
audio.  Independent of the legacy manifest / ``CLONE_VOICES_DIR`` machinery:
only contained, application-relative paths are ever stored or returned.
``app/tts.py`` consumes ``reference_root`` for clone-reference resolution.

Duration probing is a *bounded media probe* — a single ffprobe pass with a
hard timeout, the pipeline's established media probe seam — with an optional
TTS-engine hook (``get_tts_engine``) preferred when the engine exposes a
duration probe on its public interface.  No arbitrary engine arguments are
used.  Every failure path removes the partially-written file.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import BinaryIO, Callable, cast

# Allow-listed reference-audio content types, keyed by lowercase extension.
# Matches the media types FFmpeg's demuxer detects so extension spoofing is
# rejected at the boundary.
REFERENCE_MEDIA_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}

# Default configured bounds.  Read from the environment at call time so tests
# (and operational tuning) can override them per-invocation.
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_DEFAULT_MAX_DURATION_MS = 10 * 60 * 1000
_FFPROBE_TIMEOUT_S = 60
_SNIFF_BYTES = 32

# Magic-byte sniff signatures (FFmpeg-probe-aligned) per allow-listed type.
_MAGIC: dict[str, bytes] = {
    "audio/wav": b"RIFF",
    "audio/mpeg": b"\xff\xfb",
    "audio/ogg": b"OggS",
    "audio/flac": b"fLaC",
    "audio/mp4": b"\x00\x00\x00\x18ftyp",
    "audio/aac": b"\xff\xf1",
}


class CloneReferenceMediaError(ValueError):
    """Rejected reference-audio upload (mapped to HTTP 400 by the API layer)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configured_max_bytes() -> int:
    """Byte-size bound for a single reference upload (env-overridable)."""
    return int(os.environ.get("CLONE_REFERENCE_MAX_BYTES", str(_DEFAULT_MAX_BYTES)))


def configured_max_duration_ms() -> int:
    """Decoded-duration bound for a single reference upload (env-overridable)."""
    return int(
        os.environ.get("CLONE_REFERENCE_MAX_DURATION_MS", str(_DEFAULT_MAX_DURATION_MS))
    )


def reference_root() -> str:
    """Configured reference storage root (env-overridable).

    Defaults to ``<repo_root>/designed_voices/references`` — under the same
    storage root as ``_PREVIEWS_DIR`` (``designed_voices/previews``), matching
    the DD's "``designed_voices/references`` style" guidance.
    """
    default = os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ),
        "designed_voices",
        "references",
    )
    return os.environ.get("CLONE_REFERENCE_ROOT", default)


# ---------------------------------------------------------------------------
# Path / content validation
# ---------------------------------------------------------------------------


def media_type_from_path(path: str) -> str:
    """Return the allow-listed media type for *path*, raising when unsupported."""
    ext = os.path.splitext(path)[1].lower()
    media_type = REFERENCE_MEDIA_TYPES.get(ext)
    if media_type is None:
        raise CloneReferenceMediaError(
            f"Unsupported reference audio type: {ext or '(none)'}"
        )
    return media_type


def sniff_allows(media_type: str, head: bytes) -> bool:
    """Magic-byte gate: *head* must begin with the allow-listed signature."""
    prefix = _MAGIC.get(media_type, b"")
    if not prefix:
        return False
    return head.startswith(prefix)


def canonical_contain(dest_root: str, candidate: str) -> str | None:
    """Resolve *candidate* inside *dest_root*, or ``None`` if it escapes.

    Both sides are ``realpath``'d before the containment check, so symlinked
    parents cannot smuggle a ``..`` or link escape past the boundary.
    """
    root = os.path.realpath(dest_root)
    resolved = os.path.realpath(candidate)
    if os.path.commonpath([root, resolved]) != root:
        return None
    return resolved


# ---------------------------------------------------------------------------
# Media validation / persistence
# ---------------------------------------------------------------------------


def compute_sha256(fileobj: BinaryIO) -> str:
    """Streamed SHA-256 hex digest of an open binary file object."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(1 << 16), b""):
        digest.update(chunk)
    return digest.hexdigest()


def probe_duration_ms(
    path: str, tts_engine: object | None = None
) -> int:
    """Integer-ms decoded duration of *path* (bounded; raises on failure).

    Prefers the TTS-engine seam when the engine exposes a public duration
    probe (``probe_duration_ms`` or ``get_duration_ms``), otherwise falls back
    to a single bounded ffprobe pass.  Never guesses a duration.
    """
    if tts_engine is not None:
        probe = getattr(tts_engine, "probe_duration_ms", None) or getattr(
            tts_engine, "get_duration_ms", None
        )
        if callable(probe):
            probe_fn = cast(Callable[[str], int], probe)
            try:
                return int(probe_fn(path))
            except (TypeError, ValueError):
                pass  # engine probe unusable — fall through to ffprobe
    return _probe_duration_ms_ffprobe(path)


def _probe_duration_ms_ffprobe(path: str) -> int:
    """Bounded ffprobe pass: integer-ms stream duration of *path*."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise CloneReferenceMediaError("ffprobe not found on system")
    except subprocess.TimeoutExpired:
        raise CloneReferenceMediaError("ffprobe timed out probing reference audio")
    if result.returncode != 0:
        stderr = (result.stderr or "")[-300:]
        raise CloneReferenceMediaError(
            f"cannot decode reference audio: {stderr}"
        )
    text = (result.stdout or "").strip()
    try:
        return int(round(float(text) * 1000))
    except ValueError:
        raise CloneReferenceMediaError("unparseable reference audio duration")


def validate_and_copy(
    src: BinaryIO,
    dest_root: str,
    reference_id: str,
    original_filename: str,
    *,
    max_bytes: int | None = None,
    max_duration_ms: int | None = None,
    tts_engine: object | None = None,
) -> dict[str, object]:
    """Stream-validate and persist *src* under *dest_root*.

    The file is written to a sibling ``.tmp`` name first, validated (content
    sniff, byte-size bound, decoded-duration bound), then atomically
    ``os.replace``'d into place.  Every rejection removes the partial file.

    Returns metadata:
        ``{relative_path, original_filename, media_type, byte_size,
          duration_ms, sha256}``

    Raises ``CloneReferenceMediaError`` on rejection; never leaves partial
    files behind.
    """
    media_type = media_type_from_path(original_filename)  # allow-list extension

    src.seek(0)
    head = src.read(_SNIFF_BYTES)
    if not sniff_allows(media_type, head):
        raise CloneReferenceMediaError(
            "reference audio content does not match its declared type"
        )
    src.seek(0)

    os.makedirs(dest_root, exist_ok=True)
    relative_path = f"{reference_id}{os.path.splitext(original_filename)[1].lower()}"
    dest = os.path.join(dest_root, relative_path)
    canonical = canonical_contain(dest_root, dest)
    if canonical is None:
        raise CloneReferenceMediaError("reference path escapes the storage root")

    byte_limit = max_bytes if max_bytes is not None else configured_max_bytes()
    duration_limit = (
        max_duration_ms
        if max_duration_ms is not None
        else configured_max_duration_ms()
    )

    tmp_path = f"{canonical}.tmp"
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = src.read(1 << 16)
                if not chunk:
                    break
                byte_size += len(chunk)
                if byte_size > byte_limit:
                    raise CloneReferenceMediaError(
                        "reference audio exceeds byte-size limit"
                    )
                digest.update(chunk)
                out.write(chunk)
        duration_ms = probe_duration_ms(tmp_path, tts_engine=tts_engine)
        if duration_ms > duration_limit:
            raise CloneReferenceMediaError(
                "reference audio exceeds duration limit"
            )
        os.replace(tmp_path, canonical)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return {
        "relative_path": relative_path,
        "original_filename": original_filename,
        "media_type": media_type,
        "byte_size": byte_size,
        "duration_ms": duration_ms,
        "sha256": digest.hexdigest(),
    }


def remove_if_exists(path: str) -> None:
    """Best-effort removal of an individual reference file (never follows links)."""
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            os.remove(path)
    except OSError:
        pass


def cleanup_expired_references(
    storage, dest_root: str, *, older_than_ms: int, now_ms: int
) -> list[str]:
    """Remove tombstoned, unreferenced reference files past the retention window.

    Never follows symlinks: a link (or anything that is not a regular file)
    is skipped untouched.  Returns the list of removed ``reference_id``s.
    """
    removed: list[str] = []
    for row in storage.get_tombstoned_references_unreferenced(
        older_than_ms, now_ms
    ):
        dest = os.path.join(dest_root, row["relative_path"])
        canonical = canonical_contain(dest_root, dest)
        if canonical is None:
            continue
        if os.path.islink(canonical) or not os.path.isfile(canonical):
            continue
        try:
            os.remove(canonical)
        except OSError:
            continue
        if storage.delete_clone_reference_row(row["reference_id"]):
            removed.append(row["reference_id"])
    return removed
