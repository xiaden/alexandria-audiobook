"""Regression tests for the Dataset Builder state.json concurrency fix.

The batch generator runs on a background thread while single-sample generation,
row updates, and metadata updates run on the event loop. Before the fix, every
read-modify-write on a project's ``state.json`` was an unlocked
load -> mutate -> write cycle performed with a non-atomic plain-file write:

- concurrent cycles could lose each other's updates (each reader working from a
  stale copy), and
- a concurrent reader could observe a partially-written (truncated) file.

The fix serializes every read-modify-write per dataset (``_update_builder_state``,
which re-reads under the per-dataset lock) and writes state via
``atomic_json_write`` (atomic ``os.replace``). These tests regress both failure
modes.

Harness setup mirrors tests/pipeline/test_status_endpoints.py: the app-local
``utils``/``hf_utils`` modules are loaded into ``sys.modules`` under their bare
names before ``app.app`` is imported (app/ cannot go on sys.path because
``app/app.py`` would shadow the ``app`` package).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
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


@pytest.fixture()
def builder_api(tmp_path, monkeypatch) -> None:
    """Isolate the dataset builder working dir and process state for a test.

    The process_state reset includes BOTH the legacy flat keys (running/logs/
    cancel) and the per-project ``projects`` dict so the fixture works against
    either batch-state layout.
    """
    monkeypatch.setattr(app.app, "DATASET_BUILDER_DIR", str(tmp_path / "dataset_builder"))
    app.app.process_state["dataset_builder"] = {
        "running": False,
        "logs": [],
        "cancel": False,
        "projects": {},
    }


def _new_client() -> TestClient:
    return TestClient(app.app.app)


# ---------------------------------------------------------------------------
# Direct unit-level regression: _update_builder_state serializes RMW cycles
# ---------------------------------------------------------------------------

class TestUpdateBuilderStateSerialization:
    def test_concurrent_mutators_lose_no_updates(self, builder_api) -> None:
        """Concurrent read-modify-writes on one project must not lose updates.

        Each thread appends one sample row inside a deliberately slow critical
        section (sleep inside the mutator). Without per-dataset serialization,
        threads that loaded state before a sibling's save would overwrite it and
        their rows would be lost; with the lock every append must survive.
        """
        name = "serialized"
        app.app._save_builder_state(name, {"description": "", "global_seed": "", "samples": []})

        n_threads = 8
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []

        def worker(tid: int) -> None:
            def mutator(state):
                # Widen the load->save window so an unlocked implementation
                # would interleave; the lock makes this harmless.
                time.sleep(0.01)
                samples = state.get("samples", [])
                samples.append({"status": "pending", "text": f"row-{tid}"})
                state["samples"] = samples
                return state

            try:
                barrier.wait(timeout=5)
                app.app._update_builder_state(name, mutator)
            except BaseException as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        state = app.app._load_builder_state(name)
        rows = {s["text"] for s in state["samples"]}
        assert rows == {f"row-{tid}" for tid in range(n_threads)}

    def test_save_is_atomic_valid_json(self, builder_api) -> None:
        """state.json must be parseable after save, with no temp files left over."""
        name = "atomic"
        app.app._save_builder_state(name, {"description": "d", "global_seed": "1", "samples": [{"status": "done"}]})

        work_dir = Path(app.app.DATASET_BUILDER_DIR) / name
        state_file = work_dir / "state.json"
        assert state_file.is_file()
        with state_file.open("r", encoding="utf-8") as f:
            assert json.load(f) == {"description": "d", "global_seed": "1", "samples": [{"status": "done"}]}
        # atomic_json_write cleans up its temp file on every path
        assert [p for p in work_dir.iterdir() if p.name.startswith(".tmp_")] == []


# ---------------------------------------------------------------------------
# Endpoint-level regression: single-sample generation (done + error paths)
# ---------------------------------------------------------------------------

class _FakeEngine:
    """Minimal stand-in for the TTS engine's generate_voice_design contract."""

    def __init__(self, wav_dir: Path, fail_for: set[str] | None = None, started: threading.Event | None = None, gate: threading.Event | None = None):
        self.wav_dir = wav_dir
        self.fail_for = fail_for or set()
        self.started = started
        self.gate = gate

    def generate_voice_design(self, description: str, sample_text: str, seed: int):
        if sample_text in self.fail_for:
            raise RuntimeError(f"boom:{sample_text}")
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=10)
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        p = self.wav_dir / f"{sample_text}.wav"
        p.write_bytes(b"RIFF-fake-audio")
        return str(p), 22050


class TestGenerateSampleEndpoint:
    def test_success_marks_done_and_persists(self, builder_api, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(app.app, "get_tts_engine", lambda: _FakeEngine(tmp_path / "gen"))
        client = _new_client()

        assert client.post("/api/dataset_builder/create", json={"name": "proj"}).status_code == 200
        resp = client.post("/api/dataset_builder/generate_sample", json={
            "dataset_name": "proj",
            "sample_index": 2,
            "description": "A calm voice",
            "text": "hello world",
            "seed": -1,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"

        status = client.get("/api/dataset_builder/status/proj").json()
        sample = status["samples"][2]
        assert sample["status"] == "done"
        assert sample["text"] == "hello world"
        assert sample["audio_url"].startswith("/dataset_builder/proj/sample_002.wav?t=")

    def test_failure_marks_error_in_state(self, builder_api, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(app.app, "get_tts_engine", lambda: _FakeEngine(tmp_path / "gen", fail_for={"bad"}))
        client = _new_client()

        assert client.post("/api/dataset_builder/create", json={"name": "proj"}).status_code == 200
        resp = client.post("/api/dataset_builder/generate_sample", json={
            "dataset_name": "proj",
            "sample_index": 0,
            "description": "A calm voice",
            "text": "bad",
            "seed": -1,
        })
        assert resp.status_code == 500

        status = client.get("/api/dataset_builder/status/proj").json()
        sample = status["samples"][0]
        assert sample["status"] == "error"
        assert "boom" in sample["error"]

    def test_concurrent_generate_sample_no_lost_updates(self, builder_api, tmp_path, monkeypatch) -> None:
        """Parallel single-sample generations on distinct indices must all persist.

        Each HTTP client runs its own event loop, so the read-modify-write
        cycles genuinely race; the per-dataset lock must serialize them so no
        index's status/audio_url is lost.
        """
        monkeypatch.setattr(app.app, "get_tts_engine", lambda: _FakeEngine(tmp_path / "gen"))
        _new_client().post("/api/dataset_builder/create", json={"name": "proj"})

        n = 6
        barrier = threading.Barrier(n)
        results: list[tuple[int, int]] = []  # (index, http status)
        results_lock = threading.Lock()

        def worker(idx: int) -> None:
            client = _new_client()
            barrier.wait(timeout=5)
            resp = client.post("/api/dataset_builder/generate_sample", json={
                "dataset_name": "proj",
                "sample_index": idx,
                "description": "A calm voice",
                "text": f"line {idx}",
                "seed": -1,
            })
            with results_lock:
                results.append((idx, resp.status_code))

        threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sorted(results) == [(idx, 200) for idx in range(n)]
        samples = _new_client().get("/api/dataset_builder/status/proj").json()["samples"]
        assert len(samples) == n
        for idx in range(n):
            assert samples[idx]["status"] == "done"
            assert samples[idx]["text"] == f"line {idx}"


# ---------------------------------------------------------------------------
# Endpoint-level regression: batch (background thread) vs concurrent meta edit
# ---------------------------------------------------------------------------

class TestBatchThreadVsConcurrentEdit:
    def test_meta_edit_during_batch_is_not_clobbered(self, builder_api, tmp_path, monkeypatch) -> None:
        """A metadata update issued while the batch thread is mid-flight must
        survive: the batch re-reads state under the lock on every status write
        instead of re-saving a stale in-memory snapshot taken at startup.
        """
        started = threading.Event()
        resume = threading.Event()
        engine = _FakeEngine(tmp_path / "gen", started=started, gate=resume)
        monkeypatch.setattr(app.app, "get_tts_engine", lambda: engine)
        client = _new_client()

        assert client.post("/api/dataset_builder/create", json={"name": "batchproj"}).status_code == 200
        resp = client.post("/api/dataset_builder/generate_batch", json={
            "name": "batchproj",
            "description": "root desc",
            "samples": [{"emotion": "", "text": "one"}, {"emotion": "", "text": "two"}],
            "indices": [0, 1],
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

        # Batch thread is now inside generate_voice_design for row 0, i.e. it
        # has already written a "generating" status from its startup snapshot.
        assert started.wait(timeout=10), "batch never reached generate_voice_design"

        # Concurrent write while the batch holds no lock: metadata edit.
        meta = client.post("/api/dataset_builder/update_meta", json={
            "name": "batchproj",
            "description": "META-DESC",
            "global_seed": "42",
        })
        assert meta.status_code == 200

        resume.set()
        # Wait for the background task to finish.
        deadline = time.time() + 15
        while time.time() < deadline:
            status = client.get("/api/dataset_builder/status/batchproj").json()
            if not status["running"]:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - timeout
            raise AssertionError("batch generation did not finish in time")

        final = client.get("/api/dataset_builder/status/batchproj").json()
        # The meta edit must survive the batch's subsequent status writes.
        assert final["description"] == "META-DESC"
        assert final["global_seed"] == "42"
        assert [s["status"] for s in final["samples"][:2]] == ["done", "done"]