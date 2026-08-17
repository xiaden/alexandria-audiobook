"""External-engine capability discovery harness (test/validation, outside routers).

Phase 1 (S1) of TASK-voice-persona-prompt-parity-D: a deterministic capability
matrix that reports, per capability, a distinct ``supported | unavailable |
failed`` state — never a fabricated green.

The harness probes two independent groups:

* **Engine capabilities** (``clone``, ``design``, ``builtin_lora``, ``lora``,
  ``model-runtime``) — these require a real TTS engine.  In deterministic CI
  the engine is not exercised, so they report ``unavailable`` with an explicit
  reason.  Real-engine probing is opt-in behind the ``ALEXANDRIA_EXTERNAL=1``
  environment marker and is *availability-aware*: a missing engine reports
  ``unavailable`` (with a clear reason), a present-but-broken engine reports
  ``failed``, and a present, capable engine reports ``supported``.

* **Media capabilities** (``media_decode``, ``media_encode``) — these probe the
  real ffprobe/ffmpeg binaries against a deterministic in-memory WAV fixture.
  They are always probed (no engine needed), and only report ``supported`` when
  the actual binary probe succeeds.

Isolation: every probe writes only to a temp directory (or runs a read-only
binary probe); no production state is mutated.  The report records fixture
provenance and tool versions (ffprobe/ffmpeg/python/torch) so CI results are
reproducible.

``discover_capabilities`` accepts an injectable ``engine_provider`` so the
deterministic test can exercise the ``supported`` / ``failed`` paths with a
fake engine through the same public seam the production code uses
(``app.engine.get_tts_engine``) — matching the fake-engine convention already
used across ``tests/pipeline/``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import struct
from dataclasses import dataclass, field
from typing import Callable

#: Opt-in environment marker that enables real-engine probing.  Deterministic
#: CI does NOT set this; engine capabilities then report ``unavailable``.
ENABLE_EXTERNAL_ENV = "ALEXANDRIA_EXTERNAL"

#: Capabilities that require a real TTS engine (gated behind the opt-in marker).
ENGINE_CAPABILITIES: tuple[str, ...] = (
    "clone",
    "design",
    "builtin_lora",
    "lora",
    "model-runtime",
)

#: Capabilities probed against real ffmpeg/ffprobe binaries (always probed).
MEDIA_CAPABILITIES: tuple[str, ...] = ("media_decode", "media_encode")

ALL_CAPABILITIES: tuple[str, ...] = ENGINE_CAPABILITIES + MEDIA_CAPABILITIES

#: Valid status values.  ``unavailable`` is never a green pass.
VALID_STATUSES: tuple[str, ...] = ("supported", "unavailable", "failed")

#: Public surface each engine capability requires on the engine object.
_ENGINE_REQUIRED_ATTR: dict[str, str] = {
    "clone": "generate_clone_voice",
    "design": "generate_voice_design",
    "builtin_lora": "generate_lora_voice",
    "lora": "generate_lora_voice",
    "model-runtime": "mode",
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityResult:
    """One row of the capability matrix."""

    capability: str
    status: str
    detail: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "status": self.status,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# Tool discovery / versions
# ---------------------------------------------------------------------------


def _run(args: list[str], timeout_s: int = 30) -> subprocess.CompletedProcess:
    """Run a tool, capturing output, with a bounded timeout."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout_s, check=False
    )


def _binary_version(binary: str) -> str | None:
    """Return the first ``--version`` line for *binary*, or ``None`` if absent."""
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        proc = _run([path, "-version"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = proc.stdout.splitlines()[0].strip() if proc.stdout.strip() else ""
    return first or None


def tool_versions() -> dict[str, str | None]:
    """Return detected tool/runtime versions for reproducible CI reporting."""
    return {
        "ffmpeg": _binary_version("ffmpeg"),
        "ffprobe": _binary_version("ffprobe"),
        "python": sys.version.split()[0],
        "torch": _torch_version(),
    }


def _torch_version() -> str | None:
    try:
        import torch  # type: ignore

        return torch.__version__
    except Exception:  # noqa: BLE001 — torch may be absent in minimal CI
        return None


# ---------------------------------------------------------------------------
# Deterministic media fixture
# ---------------------------------------------------------------------------

#: A tiny, deterministic PCM WAV fixture generated in memory — no external
#: asset, no license dependency.  0.05 s @ 8000 Hz mono 16-bit.
_FIXTURE_DURATION_S = 0.05
_FIXTURE_SAMPLE_RATE = 8000


def make_wav(duration_seconds: float = _FIXTURE_DURATION_S,
             sample_rate: int = _FIXTURE_SAMPLE_RATE) -> bytes:
    """Minimal valid 16-bit mono PCM WAV blob (same shape as tests/pipeline)."""
    num_samples = int(duration_seconds * sample_rate)
    byte_rate = sample_rate * 2
    data = struct.pack("<h", 0) * num_samples
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + fmt
    header += b"data" + struct.pack("<I", len(data))
    return header + data


_FIXTURE_PROVENANCE = {
    "fixture": f"make_wav({_FIXTURE_DURATION_S}s, {_FIXTURE_SAMPLE_RATE}Hz)",
    "source": "deterministic in-memory PCM WAV generation",
    "license": "none (generated test fixture, no external asset)",
    "bytes": len(make_wav()),
}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _engine_supports(engine: object, capability: str) -> bool:
    """Return True when *engine* exposes the surface for *capability*.

    Built-in LoRA additionally requires the tracked ``builtin_lora/manifest.json``
    (offline fallback) to list at least one voice.
    """
    attr = _ENGINE_REQUIRED_ATTR[capability]
    # ``mode`` is a plain attribute (str), not a callable method.
    if capability == "model-runtime":
        if getattr(engine, attr, None) is None:
            return False
        return _torch_version() is not None
    if not callable(getattr(engine, attr, None)):
        return False
    if capability == "builtin_lora":
        manifest = os.path.join(_REPO_ROOT, "builtin_lora", "manifest.json")
        if not os.path.isfile(manifest):
            return False
        try:
            import json

            with open(manifest, "r", encoding="utf-8") as f:
                entries = json.load(f)
            return isinstance(entries, list) and len(entries) > 0
        except (OSError, ValueError):
            return False
    return True


def _probe_engine_capability(
    capability: str, engine_provider: Callable[[], object | None]
) -> CapabilityResult:
    """Probe one engine-gated capability (opt-in, availability-aware).

    Ordering: missing engine → ``unavailable``; present-but-error → ``failed``;
    present & capable → ``supported``.
    """
    try:
        engine = engine_provider()
    except Exception as exc:  # noqa: BLE001 — a broken factory is a real failure
        return CapabilityResult(
            capability=capability,
            status="failed",
            detail=f"engine factory raised: {exc!r}",
            evidence={"provider": "app.engine.get_tts_engine"},
        )

    if engine is None:
        return CapabilityResult(
            capability=capability,
            status="unavailable",
            detail=(
                "no TTS engine available (factory returned None); "
                f"real-engine probing is opt-in via {ENABLE_EXTERNAL_ENV}=1"
            ),
            evidence={"provider": "app.engine.get_tts_engine"},
        )

    try:
        supported = _engine_supports(engine, capability)
    except Exception as exc:  # noqa: BLE001 — present-but-broken engine surface
        return CapabilityResult(
            capability=capability,
            status="failed",
            detail=f"engine present but capability probe errored: {exc!r}",
            evidence={
                "provider": "app.engine.get_tts_engine",
                "mode": getattr(engine, "mode", None),
            },
        )

    if supported:
        return CapabilityResult(
            capability=capability,
            status="supported",
            detail=f"engine exposes required surface ({_ENGINE_REQUIRED_ATTR[capability]})",
            evidence={
                "provider": "app.engine.get_tts_engine",
                "mode": getattr(engine, "mode", None),
            },
        )

    return CapabilityResult(
        capability=capability,
        status="unavailable",
        detail=(
            f"engine present but does not expose required surface "
            f"({_ENGINE_REQUIRED_ATTR[capability]})"
        ),
        evidence={
            "provider": "app.engine.get_tts_engine",
            "mode": getattr(engine, "mode", None),
        },
    )


def _probe_media_decode() -> CapabilityResult:
    """Probe media decoding via the real ffprobe binary against the fixture."""
    version = _binary_version("ffprobe")
    if version is None:
        return CapabilityResult(
            capability="media_decode",
            status="unavailable",
            detail="ffprobe binary not found on PATH",
            evidence={"fixture": dict(_FIXTURE_PROVENANCE)},
        )
    with tempfile.TemporaryDirectory() as tmp:
        fixture = os.path.join(tmp, "fixture.wav")
        with open(fixture, "wb") as f:
            f.write(make_wav())
        proc = _run(["ffprobe", "-v", "error", "-of", "json", fixture])
        if proc.returncode != 0:
            return CapabilityResult(
                capability="media_decode",
                status="failed",
                detail=f"ffprobe present but failed to decode fixture: {proc.stderr.strip()[:200]}",
                evidence={
                    "tool": version,
                    "fixture": dict(_FIXTURE_PROVENANCE),
                },
            )
        import json

        try:
            probe = json.loads(proc.stdout)
        except ValueError:
            probe = {}
        return CapabilityResult(
            capability="media_decode",
            status="supported",
            detail="ffprobe decoded the deterministic WAV fixture",
            evidence={
                "tool": version,
                "fixture": dict(_FIXTURE_PROVENANCE),
                "format_duration": probe.get("format", {}).get("duration"),
            },
        )


def _probe_media_encode() -> CapabilityResult:
    """Probe media encoding via the real ffmpeg binary (fixture → MP3)."""
    version = _binary_version("ffmpeg")
    if version is None:
        return CapabilityResult(
            capability="media_encode",
            status="unavailable",
            detail="ffmpeg binary not found on PATH",
            evidence={"fixture": dict(_FIXTURE_PROVENANCE)},
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "fixture.wav")
        dst = os.path.join(tmp, "out.mp3")
        with open(src, "wb") as f:
            f.write(make_wav())
        proc = _run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", src, "-codec:a", "libmp3lame", "-b:a", "64k", dst,
            ]
        )
        if proc.returncode != 0:
            return CapabilityResult(
                capability="media_encode",
                status="failed",
                detail=f"ffmpeg present but failed to encode fixture: {proc.stderr.strip()[:200]}",
                evidence={"tool": version, "fixture": dict(_FIXTURE_PROVENANCE)},
            )
        ok = os.path.isfile(dst) and os.path.getsize(dst) > 0
        return CapabilityResult(
            capability="media_encode",
            status="supported" if ok else "failed",
            detail=(
                "ffmpeg encoded the deterministic WAV fixture to MP3"
                if ok
                else "ffmpeg ran but produced no output file"
            ),
            evidence={
                "tool": version,
                "fixture": dict(_FIXTURE_PROVENANCE),
                "output_bytes": os.path.getsize(dst) if ok else 0,
            },
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_capabilities(
    enable_external: bool | None = None,
    engine_provider: Callable[[], object | None] | None = None,
) -> list[dict]:
    """Return the capability matrix as a list of per-capability dicts.

    Args:
        enable_external: Force engine probing on/off.  ``None`` reads the
            ``ALEXANDRIA_EXTERNAL`` env marker (``"1"`` enables).
        engine_provider: Callable returning the TTS engine or ``None``.
            Defaults to the production ``app.engine.get_tts_engine`` seam
            (lazily imported).  Tests inject a fake engine here.

    Engine capabilities are probed only when external probing is enabled;
    otherwise they report ``unavailable``.  Media capabilities are always
    probed against the real binaries.
    """
    if enable_external is None:
        enable_external = os.environ.get(ENABLE_EXTERNAL_ENV, "0") == "1"

    if engine_provider is None:
        engine_provider = _production_engine_provider()

    results: list[CapabilityResult] = []

    for capability in ENGINE_CAPABILITIES:
        if not enable_external:
            results.append(
                CapabilityResult(
                    capability=capability,
                    status="unavailable",
                    detail=(
                        f"real-engine probing not enabled; set {ENABLE_EXTERNAL_ENV}=1 "
                        "to probe against a real TTS engine"
                    ),
                    evidence={"opt_in_marker": ENABLE_EXTERNAL_ENV},
                )
            )
        else:
            results.append(_probe_engine_capability(capability, engine_provider))

    results.append(_probe_media_decode())
    results.append(_probe_media_encode())

    return [r.to_dict() for r in results]


def _production_engine_provider() -> Callable[[], object | None]:
    """Return the production engine seam (lazy import to avoid heavy deps)."""
    import app.engine

    return app.engine.get_tts_engine


def capability_report(
    enable_external: bool | None = None,
    engine_provider: Callable[[], object | None] | None = None,
) -> dict:
    """Return the full report: capability matrix + fixture provenance + versions."""
    return {
        "capabilities": discover_capabilities(enable_external, engine_provider),
        "fixture_provenance": dict(_FIXTURE_PROVENANCE),
        "tool_versions": tool_versions(),
    }
