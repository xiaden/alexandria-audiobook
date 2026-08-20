"""Focus tests for Dataset Builder process-state isolation.

The batch-generation backend tracks ``running``/``logs``/``cancel`` in
``process_state["dataset_builder"]``. Historically this was a single global
dict, so the running status and logs of one project leaked to every other
project and ``POST /api/dataset_builder/cancel`` (no body) cancelled whatever
was running globally.

After this change the state is keyed by sanitized project name
(``{"projects": {safe_name: {"running", "logs", "cancel"}}}``), cancellation
requires a project name, and ``GET /api/dataset_builder/status/{name}`` reads
only the named project's state. The global one-active-batch guard is retained,
but a running project A must never be observable or cancellable via project B.

This file uses the same top-level ``app.app`` TestClient harness as
``test_status_endpoints.py`` (app-local ``utils``/``hf_utils`` modules are
loaded explicitly before ``app.app`` is imported).
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_app_local_module(name: str) -> None:
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

import app.app  # noqa: E402  (after harness setup)


def _client() -> TestClient:
    return TestClient(app.app.app)


def test_generate_batch_state_isolated_per_project(tmp_path, monkeypatch) -> None:
    module = app.app
    # Isolated workspace so no project files leak into the real repo.
    monkeypatch.setattr(module, "DATASET_BUILDER_DIR", str(tmp_path))
    module.process_state["dataset_builder"]["projects"].clear()

    release = threading.Event()

    class _BlockingEngine:
        def __init__(self) -> None:
            self.calls = 0

        def generate_voice_design(self, description, sample_text, seed=-1):
            self.calls += 1
            release.wait(10)  # block until the test lets the batch finish
            wav = tmp_path / f"gen_{self.calls}.wav"
            wav.write_bytes(b"RIFFxxxxWAVE")
            return str(wav), 22050

    engine = _BlockingEngine()
    monkeypatch.setattr(module, "get_tts_engine", lambda: engine)

    client = _client()
    payload = {
        "name": "Project A",
        "description": "root",
        "samples": [{"emotion": "neutral", "text": "Hello."}],
    }

    # Starting a batch for A immediately marks A's project running.
    r = client.post("/api/dataset_builder/generate_batch", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    a = client.get("/api/dataset_builder/status/project_a")
    assert a.status_code == 200
    assert a.json()["running"] is True

    # B must not observe A's running flag or logs.
    b = client.get("/api/dataset_builder/status/project_b")
    assert b.status_code == 200
    assert b.json()["running"] is False
    assert b.json()["logs"] == []

    # The global one-active-batch guard is preserved: a second start is refused.
    r2 = client.post("/api/dataset_builder/generate_batch", json=payload)
    assert r2.status_code == 400

    # Cancelling B is a no-op and must not touch A.
    rb = client.post("/api/dataset_builder/cancel", json={"name": "project_b"})
    assert rb.status_code == 200
    assert rb.json()["status"] == "not_running"

    # Cancelling A (raw or sanitized name) targets A's own state.
    ra = client.post("/api/dataset_builder/cancel", json={"name": "project_a"})
    assert ra.status_code == 200
    assert ra.json()["status"] == "cancelling"
    ra2 = client.post("/api/dataset_builder/cancel", json={"name": "Project A"})
    assert ra2.status_code == 200
    assert ra2.json()["status"] == "cancelling"

    # Let A's worker finish; A settles back to not-running.
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/dataset_builder/status/project_a").json()["running"]:
            break
        time.sleep(0.05)

    assert client.get("/api/dataset_builder/status/project_a").json()["running"] is False
    assert engine.calls == 1


def test_dataset_builder_cancel_requires_name() -> None:
    """Cancel without a valid name is rejected rather than cancelling any batch."""
    client = _client()
    r = client.post("/api/dataset_builder/cancel", json={})
    assert r.status_code == 422  # missing required body field 'name'


def test_dataset_builder_delete_rejects_unsafe_name(tmp_path, monkeypatch) -> None:
    module = app.app
    monkeypatch.setattr(module, "DATASET_BUILDER_DIR", str(tmp_path))
    module.process_state["dataset_builder"]["projects"].clear()
    outside = tmp_path.parent / "dataset_builder_delete_guard"
    outside.mkdir(exist_ok=True)
    client = _client()

    response = client.delete("/api/dataset_builder/%2E%2E")

    assert response.status_code == 400
    assert outside.exists()


def test_dataset_builder_delete_rejects_running_project(tmp_path, monkeypatch) -> None:
    module = app.app
    monkeypatch.setattr(module, "DATASET_BUILDER_DIR", str(tmp_path))
    module.process_state["dataset_builder"]["projects"].clear()

    release = threading.Event()

    class _BlockingEngine:
        def generate_voice_design(self, description, sample_text, seed=-1):
            release.wait(10)
            wav = tmp_path / "generated.wav"
            wav.write_bytes(b"RIFFxxxxWAVE")
            return str(wav), 22050

    monkeypatch.setattr(module, "get_tts_engine", lambda: _BlockingEngine())
    client = _client()
    response = client.post(
        "/api/dataset_builder/generate_batch",
        json={
            "name": "Project A",
            "description": "root",
            "samples": [{"emotion": "neutral", "text": "Hello."}],
        },
    )
    assert response.status_code == 200, response.text

    delete_response = client.delete("/api/dataset_builder/Project A")
    assert delete_response.status_code == 409
    assert (tmp_path / "project_a").exists()

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/dataset_builder/status/project_a").json()["running"]:
            break
        time.sleep(0.05)
    assert client.get("/api/dataset_builder/status/project_a").json()["running"] is False

    delete_response = client.delete("/api/dataset_builder/Project A")
    assert delete_response.status_code == 200, delete_response.text
    assert "project_a" not in module.process_state["dataset_builder"]["projects"]
