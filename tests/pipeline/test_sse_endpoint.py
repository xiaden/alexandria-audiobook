"""Spec-first tests for the Part C SSE endpoint.

This module encodes the Part C API/SSE contract from
``artifacts/designs/parts/per-walk-log-streaming/CONTRACTS.md`` and
``artifacts/designs/pending/DD-per-walk-log-streaming.md``:

- ``GET /api/pipeline/walks/log/{run_id}`` enforces a canonical UUID (400),
  unknown run (404), known run with a missing ephemeral file (410), and
  traversal/symlink rejection (never a 200 file read).
- Successful streams carry ``text/event-stream`` + ``Cache-Control: no-cache``,
  ``Connection: keep-alive`` and ``X-Accel-Buffering: no``.
- Each ``WalkLogRecord`` is framed as ``id: {run_id}:{seq}``, ``event: log``,
  one JSON ``data:`` line and a blank line; the terminal record is the last
  ``log`` event, followed by ``event: complete`` with ``{run_id, status}``.
- Authoritative-file replay for active AND completed runs (including records
  evicted from the broker), suppression of partial trailing JSONL, unique IDs
  with normal seq gaps, and NO synthetic overflow event.
- ``Last-Event-ID`` accepts empty or ``{run_id}:{non-negative integer}``;
  malformed / foreign / negative / impossible values return 400 BEFORE
  ``WalkLogService.open_subscription`` is called.
- Subscription closure on client cancellation (generator ``finally``).

These tests are GREEN against the current code: the SSE route exists on the
mounted pipeline router, the ``get_walk_log_service`` dependency is present
(overridable in tests), and every request exercises the specified statuses,
framing, replay, and client-cancellation behavior.

Test infrastructure rules (from the plan): tests live in ``tests/pipeline/``,
use ``tmp_path`` + FastAPI ``TestClient`` with dependency overrides, and inject
a temporary ``WalkLogService`` rooted at ``tmp_path`` (never ``/tmp`` directly).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api import get_storage
from app.pipeline.api import router as combined_router
from app.pipeline.api_walks import _event_stream
from app.pipeline.api_walks import router as walks_router
from app.pipeline.walks.log_service import WalkLogService, WalkLogSubscription

# The Part C route exposes a ``get_walk_log_service()`` dependency that tests
# override with a tmp_path-rooted service. The try/except import is retained
# as a historical safeguard so this module still imports in contexts where the
# symbol is absent; in GREEN the import always succeeds, and the
# ``_get_walk_log_service is not None`` check in the ``client`` fixture is a
# no-op.
try:
    from app.pipeline.api_walks import get_walk_log_service as _get_walk_log_service
except ImportError:  # Historical safeguard: dependency absent only in pre-GREEN snapshots
    _get_walk_log_service = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_uuid() -> str:
    """Return a fresh canonical UUID string."""
    return str(uuid.uuid4())


def test_event_stream_emits_keepalive_and_completes():
    """A quiet live subscription stays observable and still terminates."""
    class Subscription:
        def __init__(self):
            self.closed = False
            self.calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.02)
                return SimpleNamespace(
                    seq=0,
                    id="run:0",
                    data={"status": "completed"},
                    terminal=True,
                )
            raise StopAsyncIteration

        def close(self):
            self.closed = True

    async def collect():
        import app.pipeline.api_walks as module
        old = module._SSE_KEEPALIVE_SECONDS
        module._SSE_KEEPALIVE_SECONDS = 0.001
        try:
            return [item async for item in _event_stream(Subscription(), "run", "running")]
        finally:
            module._SSE_KEEPALIVE_SECONDS = old

    events = asyncio.run(collect())
    assert any("event: heartbeat" in event for event in events)


def _insert_run(
    storage,
    run_id: str,
    walk_name: str = "walk_2a_scene_segmentation",
    book_id: str = "b1",
    status: str = "completed",
) -> None:
    """Insert a ``walk_run`` row for the run id (status authority)."""
    now = int(time.time() * 1000)
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms, "
        "heartbeat_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, book_id, walk_name, status, now, now),
    )


def _record(run_id: str, seq: int, event: str = "llm", data=None, terminal: bool = False) -> dict:
    """Build a JSONL record dict in the Part A on-disk shape."""
    return {
        "run_id": run_id,
        "seq": seq,
        "id": f"{run_id}:{seq}",
        "event": event,
        "data": data if data is not None else {},
        "terminal": terminal,
    }


def _write_log(
    root,
    run_id: str,
    records: list[dict],
    header: dict | None = None,
    partial_tail: bool = False,
):
    """Write a JSONL log file (header + records) under ``root`` for ``run_id``.

    When ``partial_tail`` is set, a non-JSON trailing line is appended so the
    partial-trailing-line-suppression contract can be asserted.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{run_id}.log"
    hdr = header or {
        "book_id": "b1",
        "walk_name": "walk_2a_scene_segmentation",
        "run_id": run_id,
        "started_ms": 1,
    }
    lines = [json.dumps(hdr, ensure_ascii=False)]
    for rec in records:
        lines.append(json.dumps(rec, ensure_ascii=False))
    text = "\n".join(lines) + "\n"
    if partial_tail:
        text += '{"run_id": "partial incomplete trailing'  # intentionally not JSON
    path.write_text(text, encoding="utf-8")
    return path


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


def _drain_sse(response) -> list[dict]:
    """Drain a streaming SSE response (live) into parsed events."""
    events: list[dict] = []
    cur: dict = {"id": None, "event": None, "data": []}
    for line in response.iter_lines():
        if line is None or line == "":
            if cur["event"] is not None or cur["data"] or cur["id"] is not None:
                events.append(cur)
            cur = {"id": None, "event": None, "data": []}
        elif line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"].append(line[5:].strip())
    if cur["event"] is not None or cur["data"] or cur["id"] is not None:
        events.append(cur)
    for e in events:
        e["data"] = json.loads("\n".join(e["data"])) if e["data"] else None
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage() -> InMemorySQLiteAdapter:
    """In-memory SQLite adapter for the walk_run table."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture(params=["router_only", "mounted"])
def client(request, tmp_path, storage):
    """TestClient over the api_walks router ONLY, or over the real combined
    mounted router (app.pipeline.api), with storage + service overrides.

    ``get_walk_log_service`` exists on the mounted router and is always
    overridden with the tmp_path-rooted service on both the router-only and
    mounted clients; the override is conditional only to keep the fixture safe
    for pre-GREEN snapshots (a no-op in GREEN).
    """
    service = WalkLogService(root_dir=str(tmp_path / "alexandria-walks"))
    service.start()
    if request.param == "router_only":
        app = FastAPI()
        app.include_router(walks_router)
    else:
        app = FastAPI()
        app.include_router(combined_router)
    app.dependency_overrides[get_storage] = lambda: storage
    if _get_walk_log_service is not None:
        app.dependency_overrides[_get_walk_log_service] = lambda: service
    yield TestClient(app), service
    service.shutdown()


# ---------------------------------------------------------------------------
# P1-S4: routing, status codes, headers, traversal/symlink rejection
# ---------------------------------------------------------------------------


class TestSseRouting:
    def test_malformed_uuid_returns_400(self, client):
        tc, _service = client
        resp = tc.get("/api/pipeline/walks/log/not-a-canonical-uuid")
        assert resp.status_code == 400

    def test_unknown_run_returns_404_with_run_detail(self, client):
        tc, _service = client
        rid = _canonical_uuid()
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 404
        # A run-specific detail, not FastAPI's generic "Not Found".
        assert rid in resp.json()["detail"]

    def test_known_run_missing_file_returns_410(self, client, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 410
        # CONTRACTS line 53: a missing-file response never changes the DB
        # row/status — the 'running' row survives untouched.
        rows = storage.execute_query(
            "SELECT status FROM walk_run WHERE run_id = ?", (rid,)
        )
        assert rows[0]["status"] == "running"

    def test_successful_stream_headers(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        _write_log(root, rid, [_record(rid, 0)])
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["connection"] == "keep-alive"
        assert resp.headers["x-accel-buffering"] == "no"

    def test_traversal_run_id_rejected(self, client):
        tc, _service = client
        # Encoded path traversal as the run_id path segment — never a file read.
        resp = tc.get("/api/pipeline/walks/log/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code == 400  # non-canonical UUID

    def test_symlink_escape_rejected(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        secret = tmp_path / "secret.txt"
        secret.write_text("TOPSECRETCONTENT", encoding="utf-8")
        try:
            os.symlink(str(secret), root / f"{rid}.log")
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        # Known row but the log path is a symlink escape -> rejected (410/400),
        # and the symlink target is never read into the response.
        assert resp.status_code in (400, 410)
        assert "TOPSECRETCONTENT" not in resp.text


# ---------------------------------------------------------------------------
# P1-S5: stream content — framing, terminal, replay, gaps, overflow, cancel
# ---------------------------------------------------------------------------


class TestStreamContent:
    def test_framing_and_complete(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [
            _record(rid, 0, "llm", {"m": "a"}),
            _record(rid, 1, "parse", {"ok": True}),
            _record(rid, 2, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert [e["event"] for e in events] == ["log", "log", "log", "complete"]
        assert [e["id"] for e in events[:3]] == [f"{rid}:0", f"{rid}:1", f"{rid}:2"]
        assert events[0]["data"] == {"m": "a"}
        # The terminal record is the last log event and carries the status.
        assert events[2]["data"].get("status") == "completed"
        # complete carries only {run_id, status}.
        assert events[3]["data"] == {"run_id": rid, "status": "completed"}

    def test_terminal_log_before_complete(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="failed")
        root = tmp_path / "alexandria-walks"
        records = [
            _record(rid, 0, "llm", {"m": 0}),
            _record(rid, 1, "terminal", {"status": "failed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        # terminal log record is last, followed by complete with status=failed.
        assert [e["event"] for e in events] == ["log", "log", "complete"]
        assert events[1]["id"] == f"{rid}:1"
        assert events[1]["data"].get("status") == "failed"
        assert events[2]["data"] == {"run_id": rid, "status": "failed"}

    def test_partial_trailing_line_suppressed(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [
            _record(rid, 0, "llm", {"m": 0}),
            _record(rid, 1, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records, partial_tail=True)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        # Only the two real records + complete; the partial trailing line is
        # never emitted as an event.
        assert [e["event"] for e in events] == ["log", "log", "complete"]
        assert all(
            e["event"] != "log" or e["id"] in (f"{rid}:0", f"{rid}:1")
            for e in events
        )

    def test_unique_ids_with_normal_seq_gaps(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        # Broker eviction leaves real seq gaps (uniqueness, not contiguity).
        records = [
            _record(rid, 0, "llm", {"i": 0}),
            _record(rid, 5, "llm", {"i": 5}),
            _record(rid, 9, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        ids = [e["id"] for e in events if e["event"] == "log"]
        assert ids == [f"{rid}:0", f"{rid}:5", f"{rid}:9"]
        assert len(set(ids)) == len(ids)  # unique

    def test_no_synthetic_overflow_event(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [_record(rid, i, "llm", {"i": i}) for i in range(300)]
        records.append(
            _record(rid, 300, "terminal", {"status": "completed"}, terminal=True)
        )
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert "overflow" not in [e["event"] for e in events]

    def test_active_run_file_replay_after_broker_eviction(self, client, storage):
        """An ACTIVE run that wrote >256 records (broker evicted the oldest)
        then completed: the authoritative file replays ALL records, including
        the evicted ones."""
        tc, service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(300):
            sink.append("llm", {"i": i})
        service.close_run(rid, "completed", {"status": "completed"})
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        log_ids = [e["id"] for e in events if e["event"] == "log"]
        assert len(log_ids) == 301  # 300 + terminal
        assert log_ids[0] == f"{rid}:0"
        assert log_ids[-1] == f"{rid}:300"
        assert len(set(log_ids)) == len(log_ids)

    def test_live_attach_no_loss_no_duplication(self, client, storage):
        """Replay-then-live attach for an ACTIVE run: file records replayed, live
        records after the file tail delivered, none lost and none duplicated."""
        tc, service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(5):
            sink.append("llm", {"i": i})  # file replay: seq 0..4

        def appender():
            time.sleep(0.05)
            sink.append("llm", {"i": 5})  # live, after the file tail
            service.close_run(rid, "completed", {"status": "completed"})

        threading.Thread(target=appender).start()
        with tc.stream("GET", f"/api/pipeline/walks/log/{rid}") as r:
            assert r.status_code == 200
            events = _drain_sse(r)
        log_ids = [e["id"] for e in events if e["event"] == "log"]
        # seq 0..5 plus the terminal record (seq 6) -> 0,1,2,3,4,5,6.
        assert log_ids == [f"{rid}:{i}" for i in range(7)]
        assert len(set(log_ids)) == len(log_ids)  # no duplicate anywhere
        assert events[-1]["event"] == "complete"
        assert events[-1]["data"] == {"run_id": rid, "status": "completed"}

    def test_client_cancellation_closes_subscription(self, client, storage, monkeypatch):
        """Closing a client stream on an ACTIVE run cancels the generator whose
        ``finally`` closes the subscription (non-blocking, no hang)."""
        # ``client`` is still requested (but unused here) so the test keeps the
        # same router_only / mounted parametrization as the rest of the suite.
        _tc, service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        sink.append("llm", {"i": 0})

        closed = threading.Event()
        original_close = WalkLogSubscription.close

        def spy_close(self):
            closed.set()
            return original_close(self)

        monkeypatch.setattr(WalkLogSubscription, "close", spy_close)

        # The contract is asserted at the generator level, not through the
        # TestClient's stream-disconnect simulation: Starlette 1.6.0's TestClient
        # runs the ASGI app to completion synchronously, so ``tc.stream``'s
        # __enter__ blocks forever on a never-terminating (ACTIVE, no further
        # live data) stream -- the ASGI disconnect is never delivered and the
        # client can never read a line and cancel (see exec-worker log L21). The
        # endpoint's own ``_event_stream`` IS the cancellation surface, so
        # driving it directly verifies the same finally-closes-subscription
        # contract without weakening the assertion.
        async def _drive():
            loop = asyncio.get_running_loop()
            subscription = service.open_subscription(rid, after_seq=-1, loop=loop)
            stream = _event_stream(subscription, rid, "running", after_seq=-1)
            # Read the first record (replayed from the authoritative file)...
            first = await anext(stream)
            assert f"id: {rid}:0" in first
            assert "event: log" in first
            # ...then cancel the stream: aclose() must run the generator's
            # ``finally``, which closes the subscription (non-blocking, no hang).
            await stream.aclose()

        asyncio.run(_drive())
        assert closed.wait(5)  # generator finally closed the subscription


# ---------------------------------------------------------------------------
# P1-S6: Last-Event-ID — empty, replay-after, live wait, terminal-complete,
#         and malformed/foreign/negative/impossible 400-before-open
# ---------------------------------------------------------------------------


class TestLastEventId:
    def test_empty_last_event_id_full_replay(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [
            _record(rid, 0, "llm", {"i": 0}),
            _record(rid, 1, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}", headers={"Last-Event-ID": ""})
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert [e["id"] for e in events if e["event"] == "log"] == [
            f"{rid}:0",
            f"{rid}:1",
        ]

    def test_last_event_id_replays_strictly_after_seq(self, client, tmp_path, storage):
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [
            _record(rid, i, "llm", {"i": i}) for i in range(5)
        ] + [_record(rid, 5, "terminal", {"status": "completed"}, terminal=True)]
        _write_log(root, rid, records)
        resp = tc.get(
            f"/api/pipeline/walks/log/{rid}",
            headers={"Last-Event-ID": f"{rid}:2"},
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        # strictly after seq 2 -> 3, 4, terminal(5).
        assert [e["id"] for e in events if e["event"] == "log"] == [
            f"{rid}:3",
            f"{rid}:4",
            f"{rid}:5",
        ]

    def test_last_event_id_beyond_tail_active_waits_for_future(
        self, client, storage
    ):
        """A valid cursor beyond the current file tail on an ACTIVE run waits
        for future events (only seq > cursor are delivered)."""
        tc, service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(3):
            sink.append("llm", {"i": i})  # file tail = 2

        def appender():
            time.sleep(0.05)
            for i in (3, 4, 5, 6):
                sink.append("llm", {"i": i})
            service.close_run(rid, "completed", {"status": "completed"})

        threading.Thread(target=appender).start()
        with tc.stream(
            "GET",
            f"/api/pipeline/walks/log/{rid}",
            headers={"Last-Event-ID": f"{rid}:5"},
        ) as r:
            assert r.status_code == 200
            events = _drain_sse(r)
        log_ids = [e["id"] for e in events if e["event"] == "log"]
        # seq 6 and the terminal record (seq 7) delivered; seq 0..5 suppressed.
        assert log_ids == [f"{rid}:6", f"{rid}:7"]
        assert events[-1]["event"] == "complete"

    def test_last_event_id_beyond_tail_terminal_completes_immediately(
        self, client, tmp_path, storage
    ):
        """A cursor beyond the file tail on a TERMINAL run completes immediately
        (no logs, just complete)."""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [
            _record(rid, 0, "llm", {"i": 0}),
            _record(rid, 1, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(
            f"/api/pipeline/walks/log/{rid}",
            headers={"Last-Event-ID": f"{rid}:100"},
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert [e["event"] for e in events] == ["complete"]
        assert events[0]["data"] == {"run_id": rid, "status": "completed"}

    def test_last_event_id_invalid_400_before_open(self, client, storage, monkeypatch):
        """Malformed / negative / impossible Last-Event-ID -> 400 BEFORE
        open_subscription is invoked (spied service double)."""
        tc, service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        monkeypatch.setattr(service, "open_subscription", MagicMock())
        for value in (
            "garbage",
            "not-a-uuid:3",
            f"{rid}:abc",  # non-integer
            f"{rid}:-1",  # negative
            f"{rid}:99999999999999999999999999",  # > 2^63, impossible
        ):
            resp = tc.get(
                f"/api/pipeline/walks/log/{rid}",
                headers={"Last-Event-ID": value},
            )
            assert resp.status_code == 400, value
        service.open_subscription.assert_not_called()

    def test_last_event_id_foreign_run_400_before_open(self, client, storage, monkeypatch):
        """A Last-Event-ID naming a DIFFERENT run -> 400 before open."""
        tc, service = client
        rid = _canonical_uuid()
        other = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        monkeypatch.setattr(service, "open_subscription", MagicMock())
        resp = tc.get(
            f"/api/pipeline/walks/log/{rid}",
            headers={"Last-Event-ID": f"{other}:0"},
        )
        assert resp.status_code == 400
        service.open_subscription.assert_not_called()


# ---------------------------------------------------------------------------
# P4-S1: security boundaries — control-char injection, error-body path leak,
#        oversized pass-through, bounded stream termination
# ---------------------------------------------------------------------------
#
# Already covered above (do NOT duplicate): traversal encodings
# (test_traversal_run_id_rejected), non-canonical UUIDs
# (test_malformed_uuid_returns_400), symlinked log paths
# (test_symlink_escape_rejected), and cross-run Last-Event-ID
# (test_last_event_id_foreign_run_400_before_open).
#
# These tests add the missing surfaces: (a) SSE control-character injection —
# record data containing control chars/newlines must be JSON-escaped by the
# framing so the wire format cannot be spoofed; (b) secret-bearing error paths —
# error bodies never leak server file paths or secret content; (c) oversized
# data — large-but-within-cap records stream fine and the route passes records
# through WITHOUT re-processing (truncation/redaction is Part A's sink domain);
# (d) bounded stream behavior — the stream terminates after the terminal record
# for completed runs (never unbounded).


class TestSecurityBoundaries:
    def test_control_chars_and_newlines_json_escaped_no_wire_spoof(
        self, client, tmp_path, storage
    ):
        """Record data containing raw newlines / control chars / SSE-shaped
        text must be JSON-escaped by the framing: the wire stays one data line
        per event and cannot inject fake events or reframe the stream."""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        # Adversarial payload: an injected complete event + control chars
        # inside a string VALUE. If the framing naively concatenated the raw
        # data, this would create a spoofed "event: complete" + "data:" and
        # break the single-data-line-per-event contract.
        payload = {
            "text": "line1\nline2\n\nevent: complete\ndata: {\"run_id\": \"fake\"}",
            "control": "a\x00b\x1bc\x07d",
        }
        records = [
            _record(rid, 0, "llm", payload),
            _record(rid, 1, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        body = resp.text
        # Every data: line is a single line holding the COMPLETE JSON payload.
        # A raw newline inside a JSON string would split the payload across
        # lines and json.loads(line[5:]) would fail on the partial first line.
        data_lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
        # log payload + terminal status + complete status = 3 data lines.
        assert len(data_lines) == 3
        assert json.loads(data_lines[0][5:]) == payload
        assert json.loads(data_lines[1][5:]) == {"status": "completed"}
        complete = json.loads(data_lines[2][5:])
        assert complete == {"run_id": rid, "status": "completed"}
        # The injected "event: complete" text survives ONLY as escaped JSON
        # content inside data -- never as a real, framing event: line. Count
        # framing lines (line-start) rather than raw substring, because the
        # escaped content legitimately contains the same characters.
        framing_complete = [
            ln for ln in body.splitlines() if ln == "event: complete"
        ]
        assert len(framing_complete) == 1
        assert len([ln for ln in body.splitlines() if ln == "event: log"]) == 2

    def test_control_char_event_names_cannot_spoof_framing(
        self, client, tmp_path, storage
    ):
        """Even an event field riddled with newlines is emitted only as the
        fixed 'log' framing; the record's internal event string is never
        serialized onto the wire as an event: line."""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [
            # The sink's event string is opaque to the route -- the route
            # always frames "event: log".
            _record(rid, 0, "llm\nevent: complete\ndata: x", {"i": 0}),
            _record(rid, 1, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        body = resp.text
        event_lines = [ln for ln in body.splitlines() if ln.startswith("event:")]
        # Only the fixed framing ever appears on the wire.
        assert event_lines == ["event: log", "event: log", "event: complete"]
        events = _parse_sse(body)
        assert [e["event"] for e in events] == ["log", "log", "complete"]

    def test_410_missing_file_body_leaks_no_paths(self, client, tmp_path, storage):
        """The 410 body names the run only -- never the service root directory
        or the derived file path (a filesystem-local information leak)."""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="running")
        root = tmp_path / "alexandria-walks"
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 410
        assert str(root) not in resp.text
        assert root.name not in resp.text
        assert f"/{rid}.log" not in resp.text
        assert "Traceback" not in resp.text and "line " not in resp.text
        # The detail still identifies the run (usability) without the path.
        assert rid in resp.text

    def test_400_404_bodies_leak_no_paths_or_secret_content(
        self, client, tmp_path, storage
    ):
        """Malformed-UUID 400 and unknown-run 404 bodies echo the caller's
        run id only -- never server paths, the service root, or any secret
        file content an attacker would like read back."""
        tc, _service = client
        root = tmp_path / "alexandria-walks"
        secret = tmp_path / "secret.txt"
        secret.write_text("SUPERSECRETWALLLOGCONTENT", encoding="utf-8")
        # Traversal encoded id resembles a path; the body must not echo the
        # decoded path or any file content.
        # Encoded traversal ids (Starlette URL-decodes %2F into extra path
        # segments that bypass the single-segment route and hit the catch-all
        # 400; backslash encodings never decode to a canonical UUID). The raw
        # dot-segment form is normalized client-side by httpx before the
        # request (never reaches the route), so only encoded forms are used.
        for bad in (
            "..%2F..%2Fetc%2Fpasswd",
            "%2e%2e%2Fetc%2Fpasswd",
            "..%5C..%5Cwin.ini",
            "..%5C..%5Cetc%5Cshadow",
        ):
            resp = tc.get(f"/api/pipeline/walks/log/{bad}")
            assert resp.status_code == 400, bad
            assert "SUPERSECRETWALLLOGCONTENT" not in resp.text
            assert str(root) not in resp.text
            assert str(tmp_path) not in resp.text
        rid = _canonical_uuid()
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 404
        assert str(root) not in resp.text
        assert "SUPERSECRETWALLLOGCONTENT" not in resp.text
        assert "Traceback" not in resp.text

    def test_symlink_rejection_body_leaks_neither_path_nor_target(
        self, client, tmp_path, storage
    ):
        """The symlink escape response contains the run id only -- neither the
        symlink target's path nor its content appears anywhere in the body.
        (Complements the P1 symlink test which asserted status; this one pins
        the body content of the rejection.)"""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        secret = tmp_path / "target-secret.txt"
        secret.write_text("TARGETSECRETCONTENT", encoding="utf-8")
        try:
            os.symlink(str(secret), root / f"{rid}.log")
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code in (400, 410)
        assert "TARGETSECRETCONTENT" not in resp.text
        assert "target-secret.txt" not in resp.text
        assert str(secret) not in resp.text
        assert str(root) not in resp.text

    def test_large_within_cap_record_streams_verbatim_no_reprocessing(
        self, client, tmp_path, storage
    ):
        """A large-but-within-cap record streams fine and the ROUTE passes it
        through without re-processing: no truncation marker is added and no
        redaction occurs at the route level (both are Part A sink domains).
        Written directly to the file (bypassing the sink) so any truncation or
        redaction would prove route-side re-processing."""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        # ~60 KiB of payload (well under EVENT_MAX_BYTES 128 KiB) containing
        # secret-shaped text that Part A's sink WOULD redact at write time.
        big_text = ("sk-LARGEplaceholderSECRETtoken" * 3000)[: 60 * 1024]
        payload = {
            "response": big_text,
            "api_key": "sk-clear-secret-shape",
            "i": 7,
        }
        records = [
            _record(rid, 0, "llm", payload),
            _record(rid, 1, "terminal", {"status": "completed"}, terminal=True),
        ]
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        body = resp.text
        data_lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
        # log payload + terminal status + complete status = 3 data lines.
        assert len(data_lines) == 3
        streamed = json.loads(data_lines[0][5:])
        # Verbatim pass-through: identical payload, no [truncated] marker, no
        # [REDACTED] substitution -- the route never re-processes payloads.
        assert streamed == payload
        assert "[truncated]" not in data_lines[0]
        assert "[REDACTED]" not in data_lines[0]
        assert streamed["api_key"] == "sk-clear-secret-shape"

    def test_completed_run_stream_terminates_after_terminal(
        self, client, tmp_path, storage
    ):
        """Bounded stream behavior: a completed run's stream is finite -- it
        delivers exactly the file records plus one complete event, then ends.
        The response body is fully buffered (never unbounded) and the complete
        event is the final line pair."""
        tc, _service = client
        rid = _canonical_uuid()
        _insert_run(storage, rid, status="completed")
        root = tmp_path / "alexandria-walks"
        records = [_record(rid, i, "llm", {"i": i}) for i in range(150)]
        records.append(
            _record(rid, 150, "terminal", {"status": "completed"}, terminal=True)
        )
        _write_log(root, rid, records)
        resp = tc.get(f"/api/pipeline/walks/log/{rid}")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        # 150 logs + terminal log + complete = 152 events, complete last.
        assert len(events) == 152
        assert [e["event"] for e in events[-2:]] == ["log", "complete"]
        assert events[-1]["data"] == {"run_id": rid, "status": "completed"}
        # The body ends with the complete event's blank-line terminator -- the
        # stream provably terminated (nothing trails after complete).
        assert resp.text.endswith('"}\n\n')
        assert resp.text.count("event: complete") == 1
