"""TTS engine factory — decoupled from ``ProjectManager``.

This module is the single access point for the production TTS engine,
replacing ``ProjectManager.get_engine()`` (``app/project.py`` was deleted
in Plan Q).  Every consumer — pipeline render (``api_export``),
voice_design preview, lora, dataset_builder — calls
``get_tts_engine()`` / ``reset_tts_engine()`` / ``unload_tts_engine_models()``
from here.

Config path resolution is identical to the legacy ``ProjectManager``:
``ALEXANDRIA_CONFIG_PATH`` env var, else ``app/config.json`` (relative to
this file, i.e. ``<repo-root>/app/config.json``), so behavior is preserved.
"""

from __future__ import annotations

import json
import os

# Module-level cache of the TTS engine (None = not initialized).  Mirrors the
# legacy ``ProjectManager.engine`` attribute.
_tts_engine = None


def _config_path() -> str:
    """Resolve the TTS config path (env override, else app/config.json)."""
    return os.environ.get("ALEXANDRIA_CONFIG_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"
    )


def _load_config() -> dict:
    """Load config.json as a dict, returning {} on any failure."""
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def get_tts_engine() -> object | None:
    """Return the shared TTS engine, constructing it on first use.

    Config comes from ``ALEXANDRIA_CONFIG_PATH`` or ``app/config.json``
    (identical resolution to the legacy ``ProjectManager``).  Returns
    ``TTSEngine(config)`` from ``app.tts`` on success, ``None`` on any
    exception (mirroring the legacy ``get_engine`` failure behavior).

    ``app.tts`` is imported lazily: importing it pulls in ``soundfile``,
    which may not be installed, and doing so at module level would make
    this module unimportable in environments without that dependency.
    """
    global _tts_engine
    if _tts_engine is not None:
        return _tts_engine

    config = _load_config()
    try:
        from app.tts import TTSEngine  # lazy import — soundfile chain

        _tts_engine = TTSEngine(config)
        print(f"TTS engine initialized (mode={_tts_engine.mode})")
        return _tts_engine
    except Exception as exc:  # noqa: BLE001 — mirror legacy get_engine behavior
        print(f"Failed to initialize TTS engine: {exc}")
        return None


def reset_tts_engine() -> None:
    """Drop the cached engine (matches legacy ``ProjectManager.engine = None``)."""
    global _tts_engine
    _tts_engine = None


def unload_tts_engine_models() -> None:
    """Unload the shared engine's cached models WITHOUT dropping the singleton.

    Unlike ``reset_tts_engine()`` (which sets the global to ``None``), this
    keeps ``_tts_engine`` in place so a concurrent ``get_tts_engine()`` call
    cannot build a SECOND engine instance while another thread is still using
    the first.  Two live instances each hold a copy of the Qwen3-TTS weights
    in VRAM, which OOMs whichever job generates next (e.g. a running
    ``dataset_gen``/``dataset_builder`` worker when a render or preview
    request lands after the reset).

    Used by ``lora_start_training`` to free VRAM for the training subprocess:
    unloading the models in place releases the GPU memory while preserving the
    engine object every concurrent consumer already holds.  In-flight
    generation is unaffected: consumers hold local model references and model
    loads/teardown are serialized by ``TTSEngine._model_lock``.
    """
    engine = _tts_engine
    if engine is not None:
        engine.unload_models()
