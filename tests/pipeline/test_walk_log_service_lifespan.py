"""Spec-first (RED→GREEN) lifespan integration tests for the Part A walk-log service.

These tests exercise the REAL ``app.app`` FastAPI lifespan through
``fastapi.testclient.TestClient`` used as a context manager (the only path that
enters ``lifespan``). They verify, end-to-end through the production wiring:

- startup invokes ``WalkLogService.start()``: the root directory is created with
  mode ``0700`` and only stale UUID-named ``*.log`` files (>24h) are removed.
- shutdown invokes ``WalkLogService.shutdown()``: an open sink is closed with a
  ``partial``/``aborted`` terminal marker and broker/subscriber state is released.
- the service instance is reachable through ``app.state.walk_log_service`` and a
  run opened during the app lifetime is usable through that instance.

Hermeticity: every test roots the service at a ``tmp_path`` directory by
monkeypatching the module-level ``app_module.walk_log_service`` singleton BEFORE
entering the lifespan. The real ``/tmp/alexandria-walks`` is never written. The
GC scheduler is env-opted-out (``PIPELINE_GC_SCHEDULER=0``) so the lifespan's
daemon-thread startup has no side effects here.

Import bootstrap mirrors ``tests/pipeline/test_smoke.py``: ``app/app.py`` imports
``utils``/``hf_utils`` by bare name, so they are loaded into ``sys.modules``
before ``import app.app`` (see that file's module docstring for the rationale).
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.pipeline.walks import log_service
from app.pipeline.walks.log_service import WalkLogService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"
RUN_ID = "223e4567-e89b-12d3-a456-426614174111"
DAY_MS = 24 * 60 * 60 * 1000  # 24h in milliseconds


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


def _read_records(path: Path) -> list[dict]:
    """Return every real record (a JSONL line carrying a ``seq``) as a dict."""
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            obj = __import__("json").loads(ln)
            if "seq" in obj:
                out.append(obj)
    return out


@pytest.fixture()
def walk_log_service(tmp_path, monkeypatch):
    """tmp_path-rooted WalkLogService swapped into the app module BEFORE the
    lifespan runs, so ``app.app.lifespan`` starts/stops THIS instance."""
    root = tmp_path / "alexandria-walks"
    service = WalkLogService(root_dir=str(root))
    monkeypatch.setattr(app_module, "walk_log_service", service)
    # Keep the lifespan's GC scheduler daemon-thread startup inert in tests.
    monkeypatch.setenv("PIPELINE_GC_SCHEDULER", "0")
    return service, root


@pytest.fixture()
def lifespan_app(walk_log_service):
    """A TestClient over the REAL app whose lifespan is entered on enter/exit.

    Yields (client, root). Exiting the ``with`` block triggers the app's
    ``lifespan`` shutdown path (``walk_log_service.shutdown()``).
    """
    service, root = walk_log_service
    with TestClient(app_module.app) as client:
        yield client, service, root


class TestStartup:
    def test_startup_creates_root_dir_0700(self, walk_log_service):
        """Entering the app lifespan creates the root directory with 0700."""
        service, root = walk_log_service
        assert not root.exists()
        with TestClient(app_module.app):
            assert root.exists()
            mode = stat.S_IMODE(os.stat(root).st_mode)
            assert mode == 0o700
        service.shutdown()

    def test_startup_removes_stale_uuid_keeps_recent_and_non_uuid(
        self, walk_log_service, monkeypatch
    ):
        """Startup cleanup through the REAL lifespan removes only stale UUID
        *.log files; recent UUID and non-UUID files are retained."""
        service, root = walk_log_service
        root.mkdir(mode=0o700)
        now = 1_752_000_000_000
        monkeypatch.setattr(log_service, "_now_ms", lambda: now)
        old_ts = (now - DAY_MS - 3_600_000) / 1000.0  # ~25h old
        fresh_ts = (now - 3_600_000) / 1000.0  # ~1h old

        old_uuid = str(uuid.uuid4())
        fresh_uuid = str(uuid.uuid4())

        old_path = root / f"{old_uuid}.log"
        old_path.write_text("old", encoding="utf-8")
        os.utime(old_path, (old_ts, old_ts))
        fresh_path = root / f"{fresh_uuid}.log"
        fresh_path.write_text("fresh", encoding="utf-8")
        os.utime(fresh_path, (fresh_ts, fresh_ts))
        non_uuid = root / "not-a-uuid.log"
        non_uuid.write_text("x", encoding="utf-8")
        os.utime(non_uuid, (old_ts, old_ts))

        with TestClient(app_module.app):
            assert not old_path.exists()
            assert fresh_path.exists()
            assert non_uuid.exists()
        service.shutdown()


class TestShutdown:
    def test_shutdown_closes_open_sink_as_aborted(self, walk_log_service):
        """Exiting the lifespan closes a run opened during the lifetime with an
        aborted/partial terminal marker and releases broker/sink state."""
        service, root = walk_log_service
        with TestClient(app_module.app) as client:
            # Service instance is the same one exposed on app.state.
            assert client.app.state.walk_log_service is service
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sink.append("work", {"i": 0})
        # Context manager exit triggers service.shutdown(); verify the terminal
        # marker was written and no dangling state remains.
        path = root / f"{RUN_ID}.log"
        records = _read_records(path)
        last = records[-1]
        assert last.get("terminal") is True
        assert last["data"].get("status") in ("partial", "aborted")
        assert service.get_run(RUN_ID) is None

    def test_shutdown_idempotent_runs_twice(self, walk_log_service):
        """The lifespan can be entered/exited twice without error and leaves no
        dangling state (shutdown is idempotent)."""
        service, _root = walk_log_service
        with TestClient(app_module.app):
            service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        with TestClient(app_module.app):
            # A second full lifecycle (start + shutdown) is safe.
            assert service.get_run(RUN_ID) is None
            assert not service._sinks
            assert not service._brokers

    def test_service_reachable_via_app_state(self, lifespan_app):
        """The service instance is exposed on app.state.walk_log_service and a
        run opened through it is usable during the app lifetime."""
        client, service, root = lifespan_app
        exposed = client.app.state.walk_log_service
        assert exposed is service
        sink = exposed.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        rec = sink.append("work", {"i": 0})
        assert rec is not None and rec.seq == 0
        assert (root / f"{VALID_UUID}.log").exists()
        assert exposed.replay(VALID_UUID) != ()
