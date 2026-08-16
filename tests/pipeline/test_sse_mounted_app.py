"""Mounted-app lifecycle tests (P4-S3) for the Part C SSE endpoint.

These tests enter the REAL ``app.app`` FastAPI lifespan through
``fastapi.testclient.TestClient`` used as a context manager (the only path that
runs ``lifespan``) and prove:

1. The real combined ``app.app`` router exposes ``GET /api/pipeline/walks/log/
   {run_id}`` (malformed-UUID 400 + completed-run 200 stream through the
   production app, not a test-built FastAPI).
2. The lifespan-owned service (``app.state.walk_log_service``, set by
   ``app.app``'s lifespan from the module-level singleton) is the SAME instance
   the SSE route's ``get_walk_log_service()`` dependency resolves to -- never a
   fresh/second ``WalkLogService``.
3. PRODUCTION wiring (CONTRACTS.md line 75): the real ``get_walk_runner``
   dependency -- NOT overridden -- constructs the runner singleton with
   ``log_service=app.state.walk_log_service``, so a run reserved through
   ``POST /api/pipeline/run_walk`` executes through that wired production
   runner and its sink records flow to the SSE route -- one service, one
   process, never a second WalkLogService.

Single-process / no-multi-worker guarantee (documented, asserted): the service
is a process-local module-level singleton created at import time and bound into
the app by the lifespan. SSE and the synchronous writer MUST share one process
(uvicorn single worker); the endpoint ``get_walk_log_service`` reads
``request.app.state.walk_log_service``, so a multi-worker deployment would give
each worker its own service and the contract does not hold across workers.

Streaming discipline (L21): the SSE route is only drained via TestClient for a
run that is already TERMINAL (completed) -- TestClient runs the ASGI app to
completion synchronously, so a never-ending ACTIVE stream would deadlock it. The
lifecycle runner below closes its run (terminal) before the log is fetched.

Hermeticity: every test monkeypatches the module-level ``app_module.
walk_log_service`` singleton to a ``tmp_path``-rooted service BEFORE entering
the lifespan (the real ``/tmp/alexandria-walks`` is never written), opts the GC
scheduler out (``PIPELINE_GC_SCHEDULER=0``), and restores all dependency
overrides it sets on the REAL app, so no cross-test pollution.

Import bootstrap mirrors ``tests/pipeline/test_walk_log_service_lifespan.py``:
``app/app.py`` imports ``utils``/``hf_utils`` by bare name, so they are loaded
into ``sys.modules`` before ``import app.app``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.pipeline.api_walks as api_walks_module
from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api import get_storage
from app.pipeline.walks._llm_helpers import get_walk_log_sink
from app.pipeline.walks.log_service import WalkLogService
from app.pipeline.walks.runner import WalkRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_app_local_module(name: str) -> None:
    """Load an ``app/*.py`` module under its bare name (e.g. ``utils``)."""
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

import app.app as app_module  # after harness import-path setup


def _canonical_uuid() -> str:
    return str(uuid.uuid4())


def _parse_sse(text: str) -> list[dict]:
    """Parse a buffered SSE body into ``[{id, event, data}, ...]`` events."""
    events: list[dict] = []
    cur: dict = {"id": None, "event": None, "data": []}
    for line in text.splitlines():
        if line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"].append(line[5:].strip())
        elif line == "":
            if cur["event"] is not None or cur["id"] is not None or cur["data"]:
                events.append(cur)
            cur = {"id": None, "event": None, "data": []}
    if cur["event"] is not None or cur["data"] or cur["id"] is not None:
        events.append(cur)
    for e in events:
        e["data"] = json.loads("\n".join(e["data"])) if e["data"] else None
    return events


@pytest.fixture()
def walk_log_service(tmp_path, monkeypatch):
    """tmp_path-rooted WalkLogService swapped into the app module BEFORE the
    lifespan runs, so ``app.app.lifespan`` binds this instance to app.state."""
    root = tmp_path / "alexandria-walks"
    service = WalkLogService(root_dir=str(root))
    monkeypatch.setattr(app_module, "walk_log_service", service)
    # Keep the lifespan's GC scheduler daemon-thread startup inert in tests.
    monkeypatch.setenv("PIPELINE_GC_SCHEDULER", "0")
    return service, root


@pytest.fixture()
def clean_overrides():
    """Snapshot and restore the REAL app's dependency_overrides so tests that
    override get_storage/get_walk_runner on the production app never leak into
    other modules (the app object is shared process-wide)."""
    before = dict(app_module.app.dependency_overrides)
    yield
    app_module.app.dependency_overrides.clear()
    app_module.app.dependency_overrides.update(before)


class TestMountedAppLifecycle:
    def test_mounted_app_exposes_sse_route_and_lifespan_service(
        self, walk_log_service, clean_overrides
    ):
        """The REAL app.app router serves the SSE endpoint, and the endpoint's
        ``get_walk_log_service()`` resolves to the SAME lifespan-owned instance
        (``app.state.walk_log_service`` — the monkeypatched module singleton) --
        not a fresh/second WalkLogService."""
        service, root = walk_log_service
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        # Override ONLY storage; get_walk_log_service stays the real dependency.
        app_module.app.dependency_overrides[get_storage] = lambda: storage
        with TestClient(app_module.app) as client:
            # Lifespan-owned instance IS the module-level singleton we placed.
            exposed = client.app.state.walk_log_service
            assert exposed is service
            # Route is mounted through the real combined router.
            resp = client.get("/api/pipeline/walks/log/not-a-canonical-uuid")
            assert resp.status_code == 400
            # A completed run written THROUGH the exposed service is streamed by
            # the route -- proving the dependency resolved to THIS tmp_path-rooted
            # instance (any fresh/default service would 410 on this file).
            rid = _canonical_uuid()
            now = 1
            storage.execute_insert(
                "INSERT INTO walk_run (run_id, book_id, walk_name, status, "
                "created_ms, heartbeat_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (rid, "b1", "walk_2a_scene_segmentation", "completed", now, now),
            )
            sink = exposed.open_run(rid, "b1", "walk_2a_scene_segmentation")
            sink.append("llm", {"i": 0})
            exposed.close_run(rid, "completed", {"status": "completed"})
            assert (root / f"{rid}.log").exists()
            resp = client.get(f"/api/pipeline/walks/log/{rid}")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = _parse_sse(resp.text)
            # appended "llm" record (log) + terminal record (log) + complete.
            assert [e["event"] for e in events] == ["log", "log", "complete"]
            assert events[0]["id"] == f"{rid}:0"
            assert events[0]["data"] == {"i": 0}
            assert events[1]["id"] == f"{rid}:1"
            assert events[1]["data"]["status"] == "completed"
            assert events[2]["data"] == {"run_id": rid, "status": "completed"}
            # Still one service: the app never constructed a second one.
            assert client.app.state.walk_log_service is service
        service.shutdown()

    def test_production_runner_wiring_shares_one_service_and_streams(
        self, walk_log_service, clean_overrides, monkeypatch
    ):
        """PRODUCTION wiring (CONTRACTS.md line 75) through the REAL app: the
        real ``get_walk_runner`` dependency -- NOT overridden -- constructs the
        runner singleton with ``log_service=app.state.walk_log_service``, a run
        reserved through ``POST /api/pipeline/run_walk`` executes through that
        wired production runner, and the SSE route streams the exact records
        its sink wrote -- one lifespan-owned service shared by API, runner, and
        SSE route; never a second WalkLogService."""
        service, root = walk_log_service
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        # Override ONLY storage. get_walk_runner stays the REAL dependency.
        app_module.app.dependency_overrides[get_storage] = lambda: storage
        # Forget any instance cached by earlier router-only tests so the real
        # factory constructs INSIDE the lifespan-active context below.
        monkeypatch.setattr(api_walks_module, "_walk_runner", None)

        def fake_execute(book_id, _storage, _config):
            # Production helper seam: the wired runner sets WALK_LOG_SINK
            # before module execution; helpers append through it exactly as
            # real walk modules do via _llm_helpers (no direct service access).
            sink = get_walk_log_sink()
            if sink is not None:
                sink.append("llm", {"model": "mounted-app-production", "i": 0})
            return {"status": "completed", "book_id": book_id}

        # Hermetic walk execution (real walks invoke LLM SDKs): the runner
        # still runs the FULL production path -- pending verification, sink
        # open, ContextVar seam, terminal record, DB finalization.
        fake_module = types.ModuleType("mock_walk")
        fake_module.execute = fake_execute
        with (
            patch.object(WalkRunner, "_load_walk_module", return_value=fake_module),
            patch.object(WalkRunner, "_run_verification", return_value=True),
            TestClient(app_module.app) as client,
        ):
            assert client.app.state.walk_log_service is service
            resp = client.post(
                "/api/pipeline/run_walk",
                json={
                    "walk_name": "walk_2a_scene_segmentation",
                    "book_id": "b1",
                    "config": {},
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "started" and body["started"] is True
            rid = body["run_id"]
            # The runner the API resolved IS the module singleton, wired to
            # the lifespan-owned service (same object identity -- never a
            # second instance; a fresh/default service would carry a
            # different root and 410 on this run's file).
            resolved = api_walks_module._walk_runner
            assert resolved is not None
            assert resolved._log_service is service
            assert resolved._storage is storage
            # BackgroundTasks ran within the request lifecycle (TestClient
            # runs the ASGI app to completion): the row is completed and
            # the sink file was written under the service root.
            rows = storage.execute_query(
                "SELECT status FROM walk_run WHERE run_id = ?", (rid,)
            )
            assert rows and rows[0]["status"] == "completed"
            assert (root / f"{rid}.log").exists()
            # The SSE route streams the record the PRODUCTION runner wrote
            # through the shared service -- had get_walk_runner not wired
            # log_service, no sink would exist and this would be a 410.
            resp = client.get(f"/api/pipeline/walks/log/{rid}")
            assert resp.status_code == 200
            events = _parse_sse(resp.text)
            # run_walk_reserved sink append (log) + terminal record (log)
            # + complete.
            assert [e["event"] for e in events] == ["log", "log", "complete"]
            assert events[0]["id"] == f"{rid}:0"
            assert events[0]["data"] == {
                "model": "mounted-app-production",
                "i": 0,
            }
            assert events[1]["data"]["status"] == "completed"
            assert events[2]["data"] == {"run_id": rid, "status": "completed"}
            # Still one service: the app never constructed a second one.
            assert client.app.state.walk_log_service is service
        service.shutdown()

    def test_single_process_no_multi_worker_contract(self, walk_log_service):
        """Documented + asserted: the service is a process-local module-level
        singleton bound by the lifespan. SSE and the writer share one process;
        the contract assumes a single uvicorn worker (a multi-worker deployment
        would give each worker its own service and cross-worker SSE delivery is
        explicitly out of scope)."""
        service, _root = walk_log_service
        with TestClient(app_module.app) as client:
            # The exposed instance is the very module-level object created at
            # import time and monkeypatched into the app module -- process-local
            # by construction, shared by lifespan and get_walk_log_service().
            assert client.app.state.walk_log_service is service
            assert client.app.state.walk_log_service is app_module.walk_log_service
        service.shutdown()