"""Regression tests: starting LoRA training must NOT reset the shared TTS engine.

``lora_start_training`` used to call ``reset_tts_engine()`` at request time,
which drops the module-global engine singleton (``app.engine._tts_engine =
None``).  While a background worker (``dataset_gen`` / ``dataset_builder``
batch) still holds the old engine with its models loaded in VRAM, the next
``get_tts_engine()`` call — from a render job, a preview, or a per-sample
generation — then constructs a SECOND engine instance.  Two live instances
each hold a copy of the Qwen3-TTS weights, so the in-flight worker OOMs on
its next sample and crashes.

The fix unloads the cached models IN PLACE on the shared engine
(``app.engine.unload_tts_engine_models``) instead of dropping the singleton:
VRAM is freed for the ``train_lora.py`` subprocess while every concurrent
consumer keeps the exact same engine object.  These tests regress that
contract.

Harness setup mirrors tests/pipeline/test_dataset_builder_concurrency.py:
the app-local ``utils``/``hf_utils`` modules are loaded into ``sys.modules``
under their bare names before ``app.app`` is imported.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_app_local_module(name: str) -> None:
    """Load an app/*.py module under its bare name (e.g. ``utils`` -> app/utils.py)."""
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

import app.app  # noqa: E402  (after harness setup)
import app.engine  # noqa: E402


class _FakeEngine:
    """Minimal stand-in for ``TTSEngine.unload_models``."""

    def __init__(self) -> None:
        self.unload_calls = 0

    def unload_models(self):
        self.unload_calls += 1


# ---------------------------------------------------------------------------
# Unit-level: unload_tts_engine_models keeps the singleton while unloading
# ---------------------------------------------------------------------------

class TestUnloadTTSEngineModels:
    def test_unloads_in_place_and_keeps_singleton(self, monkeypatch) -> None:
        """The helper must unload models on the SAME object, not swap it."""
        fake = _FakeEngine()
        monkeypatch.setattr(app.engine, "_tts_engine", fake)

        app.engine.unload_tts_engine_models()

        assert fake.unload_calls == 1
        # Regression: reset_tts_engine() would have nulled this; the whole
        # point of the helper is that the global is untouched.
        assert app.engine._tts_engine is fake

    def test_noop_when_engine_not_initialized(self, monkeypatch) -> None:
        """Must not raise (nor construct an engine) when never initialized."""
        monkeypatch.setattr(app.engine, "_tts_engine", None)

        app.engine.unload_tts_engine_models()  # no exception

        assert app.engine._tts_engine is None

    def test_contrast_reset_drops_singleton(self, monkeypatch) -> None:
        """Document the old hazard: reset_tts_engine nulls the global.

        This is exactly what a concurrent get_tts_engine() consumer would
        observe after the pre-fix lora_start_training -> a brand new engine
        instance next time, doubling VRAM while a worker still runs.
        """
        fake = _FakeEngine()
        monkeypatch.setattr(app.engine, "_tts_engine", fake)

        app.engine.reset_tts_engine()

        assert app.engine._tts_engine is None


# ---------------------------------------------------------------------------
# Endpoint-level: POST /api/lora/train preserves the shared engine
# ---------------------------------------------------------------------------

@pytest.fixture()
def train_api(tmp_path, monkeypatch) -> None:
    """Isolate LoRA dirs and the training process state for a test."""
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "ds1").mkdir()
    monkeypatch.setattr(app.app, "LORA_DATASETS_DIR", str(datasets))
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(app.app, "LORA_MODELS_DIR", str(models))
    # Reset the training process state so tests never observe a running job.
    app.app.process_state["lora_training"] = {"running": False, "logs": []}


class TestLoraTrainPreservesEngine:
    def _stub_run_process(self, monkeypatch) -> None:
        """Prevent the background task from spawning a real train_lora.py."""
        def fake_run_process(command, task_name):
            app.app.process_state[task_name]["running"] = False

        monkeypatch.setattr(app.app, "run_process", fake_run_process)

    def test_training_start_keeps_shared_engine_and_unloads_in_place(
        self, train_api, monkeypatch
    ) -> None:
        """Starting training must free VRAM in place, never swap the singleton.

        A concurrent ``dataset_gen``/``dataset_builder`` worker (or render /
        preview request) holds the shared engine; after the old
        ``reset_tts_engine()`` call the global was None, so the next consumer
        built a second engine instance and doubled VRAM.  With the fix the
        POST must: (1) invoke the unload helper on the existing engine, and
        (2) leave ``app.engine._tts_engine`` pointing at the very same object.
        """
        fake = _FakeEngine()
        monkeypatch.setattr(app.engine, "_tts_engine", fake)

        helper_calls: list[bool] = []
        real_helper = app.engine.unload_tts_engine_models

        def wrapper() -> None:
            helper_calls.append(True)
            real_helper()

        monkeypatch.setattr(app.app, "unload_tts_engine_models", wrapper)
        self._stub_run_process(monkeypatch)
        client = TestClient(app.app.app)

        resp = client.post("/api/lora/train", json={
            "name": "adapter1",
            "dataset_id": "ds1",
        })

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        # The unload helper ran...
        assert helper_calls == [True]
        assert fake.unload_calls == 1
        # ...and the shared singleton is untouched: the same object a
        # concurrent render / preview / dataset-gen consumer holds.
        assert app.engine._tts_engine is fake

    def test_training_start_without_engine_constructs_nothing(
        self, train_api, monkeypatch
    ) -> None:
        """Never-initialized engine: starting training must not create one.

        The pre-fix code was a no-op here too, but this pins that the new
        helper (which deliberately reads, rather than constructs, the
        engine) does not introduce engine construction at training start.
        """
        monkeypatch.setattr(app.engine, "_tts_engine", None)
        self._stub_run_process(monkeypatch)
        client = TestClient(app.app.app)

        resp = client.post("/api/lora/train", json={
            "name": "adapter1",
            "dataset_id": "ds1",
        })

        assert resp.status_code == 200
        assert app.engine._tts_engine is None