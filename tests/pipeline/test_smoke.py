"""Smoke-check harness for the audiobook pipeline API.

Re-establishes the smoke-check harness per the Universal Upgrade design
document test strategy (evidence-based adjustment #4 in the feature README:
no ``**/*smoke*`` file existed anywhere in the repository, so the harness
location had to be re-established). This file lives at
``tests/pipeline/test_smoke.py`` so it runs with the rest of the pipeline
suite and reuses the established fixture/DI conventions there.

What the harness does
---------------------
- Boots the REAL production FastAPI app (``app.app``) through the
  guard-compatible import path: ``app/utils.py`` and ``app/hf_utils.py`` are
  preloaded into ``sys.modules`` BEFORE ``import app.app`` — exactly the
  pattern ``test_legacy_removed.py`` uses (see its module docstring for why
  ``app/`` cannot simply be on ``sys.path``: ``app/app.py`` is a top-level
  module named ``app`` and imports ``utils`` / ``hf_utils`` by bare name).
- Stubs the TTS engine and storage via FastAPI ``dependency_overrides``
  (the ``test_api.py`` pattern) so the run is hermetic: no real engine is
  ever constructed and no production database (``data/pipeline.db``) is
  touched.
- Asserts the pipeline routes are reachable on the production app:

  - ``POST /api/pipeline/onboard`` — reachable (non-EPUB upload → 400)
  - ``GET /api/pipeline/export/{book_id}`` — reachable (unknown book → 200 [])
  - ``POST /api/pipeline/render`` — reachable and 503 without a TTS engine

The render 503 check uses the REAL dependency resolution: in this
environment ``app.engine.get_tts_engine()`` cannot construct ``TTSEngine``
(``app.tts`` is not importable — see ``app/engine.py``), so it returns
``None`` and the endpoint maps that to 503. This is deterministic here and
mirrors ``TestGetTTSEngineProduction`` in ``test_api.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_app_local_module(name: str) -> None:
    """Load an ``app/*.py`` module under its bare name (e.g. ``utils``).

    ``app/app.py`` imports ``utils`` and ``hf_utils`` by bare name, so those
    modules must exist in ``sys.modules`` under those names before ``app.app``
    is imported. See ``test_legacy_removed.py`` for the full rationale.
    """
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

import app.app as app_module  # noqa: E402  (after harness import-path setup)

from app import engine as engine_factory  # noqa: E402
from app.pipeline.adapter import InMemorySQLiteAdapter  # noqa: E402
from app.pipeline.api import get_storage, get_tts_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage() -> InMemorySQLiteAdapter:
    """In-memory SQLite adapter — keeps the smoke run hermetic."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture()
def smoke_client(storage) -> TestClient:
    """TestClient over the REAL production FastAPI app with DI overrides.

    Boots ``app.app`` (the full production app: all routers, static mounts)
    exactly as the guard suite does, then stubs ``get_storage`` and
    ``get_tts_engine`` via ``dependency_overrides`` so no real engine is
    constructed and no production DB is touched.
    """
    app = app_module.app
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_tts_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_tts_engine, None)


# ---------------------------------------------------------------------------
# Route-reachability smoke checks
# ---------------------------------------------------------------------------


class TestPipelineRouteReachability:
    def test_onboard_route_reachable(self, smoke_client) -> None:
        """POST /api/pipeline/onboard is wired: non-EPUB upload → 400."""
        response = smoke_client.post(
            "/api/pipeline/onboard",
            files={"file": ("test.txt", b"not an epub", "text/plain")},
        )
        assert response.status_code == 400
        assert "must be an EPUB" in response.json()["detail"]

    def test_export_route_reachable(self, smoke_client) -> None:
        """GET /api/pipeline/export/{book_id} is wired: unknown book → 200 []."""
        response = smoke_client.get("/api/pipeline/export/nonexistent-book")
        assert response.status_code == 200
        assert response.json() == []

    def test_render_route_returns_503_without_engine(self, smoke_client) -> None:
        """POST /api/pipeline/render is wired and 503s without a TTS engine.

        Uses the REAL dependency resolution: the stub override is removed and
        ``app.engine.get_tts_engine()`` resolves to ``None`` because the
        engine cannot be constructed in this environment (``app.tts`` is not
        importable — soundfile/pydub chain incomplete), so the endpoint maps
        that to 503. ``reset_tts_engine()`` before/after guarantees a
        cache-miss regardless of other tests in the session.
        """
        app = smoke_client.app
        stubbed = app.dependency_overrides.pop(get_tts_engine, None)
        engine_factory.reset_tts_engine()
        try:
            response = smoke_client.post(
                "/api/pipeline/render",
                json={"book_id": "b1", "use_batch": True},
            )
        finally:
            engine_factory.reset_tts_engine()
            if stubbed is not None:
                app.dependency_overrides[get_tts_engine] = stubbed

        assert response.status_code == 503
        assert "TTS engine not available" in response.json()["detail"]
