"""Spec-first tests for the live status endpoints added by Plan Q P11-S1.

Covers:
- GET /api/lora/status — LoRA training process status (idle shape)
- GET /api/preparer/status/{task_name} — preparer subprocess status for
  ``preparer`` and ``batch_preparer``, 404 for unknown task names

These endpoints live on the top-level ``app.app`` FastAPI application (not the
pipeline sub-router tested in test_api.py), so the test-harness import setup
from tests/pipeline/test_legacy_removed.py is required:

- ``app/app.py`` does ``from utils import atomic_json_write`` and
  ``from hf_utils import ...`` at module level. Those modules live in ``app/``
  and resolve in production only because the app dir is on ``sys.path``
  (cwd=app/ or PYTHONPATH=app/). We cannot put ``app/`` on ``sys.path`` here:
  ``app/app.py`` is itself a top-level module named ``app``, so a plain
  ``app/`` path entry would make ``import app.app`` resolve ``app`` to
  ``app/app.py`` and fail with "'app' is not a package". Instead the two
  app-local modules are loaded explicitly into ``sys.modules`` before
  ``app.app`` is imported.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

# pytest already puts the repo root on sys.path (tests/ and tests/pipeline/ are
# packages), but be explicit so this file also runs standalone.
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

import app.app


@pytest.fixture()
def client() -> TestClient:
    """TestClient against the real top-level app (idle process_state)."""
    return TestClient(app.app.app)


class TestLoraStatusEndpoint:
    def test_lora_status_idle_shape(self, client) -> None:
        resp = client.get("/api/lora/status")
        assert resp.status_code == 200
        assert resp.json() == {"logs": [], "running": False, "status": "idle"}


class TestPreparerStatusEndpoint:
    def test_preparer_status_returns_state_without_process(self, client) -> None:
        resp = client.get("/api/preparer/status/preparer")
        assert resp.status_code == 200
        body = resp.json()
        assert "logs" in body
        assert "running" in body
        assert "status" in body
        assert "process" not in body

    def test_batch_preparer_status_returns_tasks(self, client) -> None:
        resp = client.get("/api/preparer/status/batch_preparer")
        assert resp.status_code == 200
        body = resp.json()
        assert "logs" in body
        assert "running" in body
        assert "tasks" in body

    def test_preparer_status_unknown_404(self, client) -> None:
        resp = client.get("/api/preparer/status/unknown")
        assert resp.status_code == 404
