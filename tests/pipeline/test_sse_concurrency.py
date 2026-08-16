"""Concurrency tests (P4-S2) for the Part C SSE endpoint's live bridge.

These tests prove the replay/live handoff is gap- and duplicate-free, that a
valid future ``Last-Event-ID`` cursor waits and receives only later records,
that terminal completion is delivered exactly once per subscription, and that
closing a subscriber never blocks the synchronous walk or changes the walk row.

Architecture under test (Part A + C ownership boundary):

- The synchronous background publisher (the walk runner's sink thread) appends
  to ``WalkLogSink``; the sink flushes to the authoritative JSONL file and
  publishes live through the per-run broker.
- The async consumer is driven EXACTLY as the endpoint drives it: the route
  calls ``service.open_subscription(run_id, after_seq=..., loop=...)`` (file
  replay + live registration atomic) and consumes the subscription; the
  ``_event_stream`` generator (imported from the endpoint module) applies the
  ``seq <= after_seq`` cursor suppression to live records too. Subscription
  consumption mirrors ``WalkLogSubscription.__aiter__``: the replay snapshot
  first, then ``next_event`` live records until ``None``.

TestClient is deliberately NOT used for non-terminating streams: Starlette
1.6.0's TestClient runs the ASGI app to completion synchronously, so it
deadlocks on a never-ending active stream (see exec-worker log L21). These
tests therefore drive the subscription / generator level with ``asyncio.run``
-- the same surface the endpoint consumes -- plus a background synchronous
publisher thread, which is exactly the production reader/writer topology.

Hermeticity: every test roots ``WalkLogService`` at ``tmp_path`` (never
``/tmp``) and uses ``InMemorySQLiteAdapter`` for the ``walk_run`` row authority.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.api_walks import _event_stream
from app.pipeline.walks.log_service import WalkLogService, WalkLogSubscription


def _canonical_uuid() -> str:
    return str(uuid.uuid4())


def _insert_run(
    storage,
    run_id: str,
    status: str = "running",
    book_id: str = "b1",
) -> None:
    """Insert a ``walk_run`` row (status authority) like the API reservation."""
    now = int(time.time() * 1000)
    storage.execute_insert(
        "INSERT INTO walk_run (run_id, book_id, walk_name, status, created_ms, "
        "heartbeat_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, book_id, "walk_2a_scene_segmentation", status, now, now),
    )


def _row_status(storage, run_id: str) -> str:
    rows = storage.execute_query(
        "SELECT status FROM walk_run WHERE run_id = ?", (run_id,)
    )
    assert rows, "walk_run row missing"
    return rows[0]["status"]


async def _collect_records(
    sub: WalkLogSubscription, timeout: float = 15.0
) -> list:
    """Drive a subscription exactly as the endpoint's ``_event_stream`` does:
    file replay snapshot first, then live ``next_event`` until ``None``. A hard
    timeout turns a misbehaving (hanging) implementation into a failure instead
    of a suite hang."""
    out: list = []

    async def _gen():
        out.extend(sub.replay)
        while True:
            rec = await sub.next_event()
            if rec is None:
                return
            out.append(rec)

    await asyncio.wait_for(_gen(), timeout=timeout)
    return out


async def _drain_records(
    service: WalkLogService, run_id: str, *, after_seq: int = -1
) -> list:
    """Open a fresh subscription and collect all records it yields."""
    loop = asyncio.get_running_loop()
    sub = service.open_subscription(run_id, after_seq=after_seq, loop=loop)
    try:
        return await _collect_records(sub)
    finally:
        sub.close()


def _stream_event_ids(body_chunks: list[str]) -> list[tuple[str, int]]:
    """Parse ``_event_stream`` chunks into ``(event, seq)`` pairs."""
    out: list[tuple[str, int]] = []
    for chunk in body_chunks:
        event_line = next(
            (ln for ln in chunk.splitlines() if ln.startswith("event:")), ""
        )
        id_line = next((ln for ln in chunk.splitlines() if ln.startswith("id:")), "")
        seq = -1
        if id_line:
            seq = int(id_line[3:].split(":")[1])
        out.append((event_line[6:].strip(), seq))
    return out


class _Publisher:
    """Background synchronous publisher: appends to the active sink and then
    terminalizes the run, exactly like the walk runner's sync ownership."""

    def __init__(self, service: WalkLogService, run_id: str, sink) -> None:
        self._service = service
        self._run_id = run_id
        self._sink = sink
        self.error: BaseException | None = None

    def append_all(self, seqs: list[int], delay: float = 0.01) -> None:
        """Append records for the given payload indices with tiny pauses so the
        async consumer can interleave (mimics real append timing)."""
        for i in seqs:
            self._sink.append("llm", {"i": i})
            time.sleep(delay)

    def close_run(self) -> None:
        self._service.close_run(self._run_id, "completed", {"status": "completed"})


def _spawn(publisher: _Publisher, work) -> threading.Thread:
    def _run() -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - capture any thread failure
            # for the main-thread ``assert publisher.error is None`` diagnostic.
            publisher.error = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# P4-S2: replay/live handoff — no gap, no duplicate, regardless of attach timing
# ---------------------------------------------------------------------------


class TestReplayLiveHandoff:
    def test_no_gap_no_duplicate_attach_before_live(self, tmp_path):
        """Attach while the file still holds records: replay + live records form
        one contiguous, unique seq sequence through the terminal record."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(3):
            sink.append("llm", {"i": i})  # file tail (replay source): 0..2

        pub = _Publisher(service, rid, sink)
        ready = threading.Event()

        def work() -> None:
            ready.wait(timeout=5)  # consumer attaches before live appends
            pub.append_all([3, 4, 5, 6])
            pub.close_run()

        thread = _spawn(pub, work)

        async def main() -> None:
            ready.set()  # release the publisher BEFORE draining
            seqs = [r.seq for r in await _drain_records(service, rid)]
            thread.join(timeout=10)
            assert pub.error is None
            # file replay 0..2 + live 3..6 + terminal 7: contiguous, unique.
            assert seqs == list(range(8))
            assert len(set(seqs)) == len(seqs)

        asyncio.run(main())
        service.shutdown()

    def test_no_gap_no_duplicate_attach_mid_stream(self, tmp_path):
        """Attach MID-STREAM: a background appender is already publishing live
        records. The subscription's atomic file-replay + broker-bridge must
        still yield every seq exactly once from replay tail into live."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_run(storage, rid, status="running")
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(5):
            sink.append("llm", {"i": i})  # file: 0..4

        pub = _Publisher(service, rid, sink)
        midstream = threading.Event()
        reached = threading.Event()

        def work() -> None:
            pub.append_all([5, 6, 7], delay=0.02)  # live appends race the attach
            midstream.set()
            reached.wait(timeout=5)  # let the consumer attach mid-stream
            pub.append_all([8, 9], delay=0.02)
            pub.close_run()

        thread = _spawn(pub, work)

        async def main() -> None:
            midstream.wait(timeout=5)
            seqs = [r.seq for r in await _drain_records(service, rid)]
            reached.set()  # release the publisher after we drain
            thread.join(timeout=10)
            assert pub.error is None
            # 0..4 replay + 5..9 live + terminal 10: contiguous, unique.
            assert seqs == list(range(11))
            assert len(set(seqs)) == len(seqs)
            # The walk row was never touched by reading/disconnecting.
            assert _row_status(storage, rid) == "running"

        asyncio.run(main())
        service.shutdown()


# ---------------------------------------------------------------------------
# P4-S2: valid future sequence waits; only later records then terminal
# ---------------------------------------------------------------------------


class TestFutureSequence:
    def test_cursor_beyond_tail_waits_and_delivers_only_later_records(
        self, tmp_path
    ):
        """A valid cursor beyond the current tail on an ACTIVE run waits for
        future events and delivers ONLY ``seq > cursor`` records then the
        terminal -- enforced through the endpoint's ``_event_stream`` (the same
        suppression the route applies to live records)."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(3):
            sink.append("llm", {"i": i})  # file tail = 2

        pub = _Publisher(service, rid, sink)

        def work() -> None:
            time.sleep(0.05)  # subscription attaches before these land
            pub.append_all([3, 4, 5, 6], delay=0.02)
            pub.close_run()

        thread = _spawn(pub, work)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            sub = service.open_subscription(rid, after_seq=5, loop=loop)
            chunks: list[str] = []
            try:
                stream = _event_stream(sub, rid, "running", after_seq=5)
                while True:
                    try:
                        chunk = await asyncio.wait_for(anext(stream), timeout=10)
                    except StopAsyncIteration:
                        break
                    chunks.append(chunk)
            finally:
                sub.close()
            thread.join(timeout=10)
            assert pub.error is None
            events = _stream_event_ids(chunks)
            # Only seq 6 + terminal(7) as log events; then complete.
            assert [e for e in events if e[0] == "log"] == [
                ("log", 6),
                ("log", 7),
            ]
            # exactly one complete event, after the terminal record.
            assert len([e for e in events if e[0] == "complete"]) == 1
            assert events[-1][0] == "complete"

        asyncio.run(main())
        service.shutdown()


# ---------------------------------------------------------------------------
# P4-S2: terminal completion is delivered exactly once per subscription
# ---------------------------------------------------------------------------


class TestTerminalOnce:
    def test_two_subscriptions_each_get_terminal_exactly_once(self, tmp_path):
        """Two independent subscribers on the same active run each receive the
        terminal record exactly once, then None -- no duplicate terminals and
        no hang after completion."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        for i in range(2):
            sink.append("llm", {"i": i})

        pub = _Publisher(service, rid, sink)

        def work() -> None:
            time.sleep(0.05)
            pub.append_all([2, 3])
            pub.close_run()

        thread = _spawn(pub, work)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            sub1 = service.open_subscription(rid, loop=loop)
            sub2 = service.open_subscription(rid, loop=loop)
            results = await asyncio.gather(
                _collect_records(sub1), _collect_records(sub2)
            )
            sub1.close()
            sub2.close()
            thread.join(timeout=10)
            assert pub.error is None
            for seqs in ([r.seq for r in res] for res in results):
                # 0..3 + terminal 4, exactly once each, unique within sub.
                assert seqs == list(range(5))
                assert len(set(seqs)) == len(seqs)

        asyncio.run(main())
        service.shutdown()

    def test_terminal_record_is_last_and_stream_ends(self, tmp_path):
        """The terminal record precedes the end-of-stream None; nothing is
        delivered after it."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        sink.append("llm", {"i": 0})
        service.close_run(rid, "completed", {"status": "completed"})

        records = asyncio.run(_drain_records(service, rid))
        assert [r.seq for r in records] == [0, 1]
        assert records[-1].terminal is True
        assert records[-1].data.get("status") == "completed"
        service.shutdown()


# ---------------------------------------------------------------------------
# P4-S2: closing a subscriber never blocks and never changes the walk
# ---------------------------------------------------------------------------


class TestCloseSubscriber:
    def test_close_non_blocking_and_walk_unchanged(self, tmp_path):
        """``subscription.close()`` returns immediately (sync, non-blocking),
        does not affect the synchronous walk: the sink keeps accepting appends,
        the run stays registered, and the ``walk_run`` row is untouched."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        storage = InMemorySQLiteAdapter()
        storage.init_db()
        _insert_run(storage, rid, status="running")
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        sink.append("llm", {"i": 0})

        async def main() -> None:
            loop = asyncio.get_running_loop()
            sub = service.open_subscription(rid, loop=loop)
            # File-replay records are delivered via the replay snapshot (the
            # queue carries only live/bridged records), so assert seq 0 from the
            # replay rather than ``next_event`` (which would block awaiting
            # live records this test never produces).
            assert sub.replay and sub.replay[0].seq == 0
            # Closing must NOT block and must not raise.
            sub.close()
            # The walk continues unaffected: the sink still accepts appends.
            rec = sink.append("llm", {"i": 1})
            assert rec is not None and rec.seq == 1
            # The run is still registered and the DB row is unchanged.
            assert service.get_run(rid) is sink
            assert _row_status(storage, rid) == "running"

        asyncio.run(main())

        # A publisher can still drive the run to completion after a close.
        service.close_run(rid, "completed", {"status": "completed"})
        assert _row_status(storage, rid) == "running"  # reader never wrote
        service.shutdown()

    def test_close_only_removes_that_subscriber(self, tmp_path):
        """Closing one subscriber leaves other subscribers and the publisher
        unaffected: a second subscriber still receives the live records."""
        service = WalkLogService(root_dir=str(tmp_path / "walks"))
        service.start()
        rid = _canonical_uuid()
        sink = service.open_run(rid, "b1", "walk_2a_scene_segmentation")
        sink.append("llm", {"i": 0})

        pub = _Publisher(service, rid, sink)

        def work() -> None:
            time.sleep(0.05)
            pub.append_all([1, 2])
            pub.close_run()

        thread = _spawn(pub, work)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            sub1 = service.open_subscription(rid, loop=loop)
            sub2 = service.open_subscription(rid, loop=loop)
            # Read one replay record on sub1, then close it. The queue carries
            # only live/bridged records, so consume from the replay snapshot.
            assert sub1.replay and sub1.replay[0].seq == 0
            sub1.close()
            # sub2 -- opened before the live appends -- gets every record.
            seqs = [r.seq for r in await _collect_records(sub2)]
            sub2.close()
            thread.join(timeout=10)
            assert pub.error is None
            assert seqs == list(range(4))  # 0..2 + terminal 3

        asyncio.run(main())
        service.shutdown()