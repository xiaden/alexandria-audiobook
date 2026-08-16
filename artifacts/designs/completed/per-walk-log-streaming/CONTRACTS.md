# Per-Walk Log Streaming — Contracts Ledger

**Design doc:** `artifacts/designs/pending/DD-per-walk-log-streaming.md`
**Last updated:** 2026-08-16 (amended)

---

## Architectural Rules

- Zero changes to the nine walk module files (`app/pipeline/walks/walk_2*.py`) — enforced by static/import audit test
- `walk_run` DB row remains the status authority; logs are ephemeral diagnostics in `/tmp/alexandria-walks/` (destroyed on container restart)
- SSE chosen over WebSocket/polling: one-way logs, native `EventSource`, auto-reconnect, FastAPI `StreamingResponse`; no multi-worker guarantee (writer + SSE must share one process)
- Walk runner is the sync execution owner; the async SSE side must never block a walk; disconnect removes subscribers only
- Frontend renders log content with `textContent`, never HTML interpolation
- Existing status/history polling (2s) and `main.ts` module-scope init / DOMContentLoaded wiring remain unchanged
- Backend tests: pytest under `tests/pipeline/` with `tmp_path`, fake SDK responses, FastAPI `TestClient`. Frontend tests: `vitest run` under `frontend/tests/frontend/` with mocked EventSource
- TDD: tests first (RED → GREEN → REFACTOR); backend coverage target and frontend 499 existing tests stay green

---

## Collections & Methods

### Part A walk-log service

- `WalkLogService(root_dir: str = "/tmp/alexandria-walks") -> None`
- `WalkLogService.start() -> None`
- `WalkLogService.shutdown() -> None`
- `WalkLogService.open_run(run_id: str, book_id: str, walk_name: str, started_ms: int | None = None) -> WalkLogSink`
- `WalkLogService.get_run(run_id: str) -> WalkLogSink | None`
- `WalkLogService.close_run(run_id: str, status: Literal["completed", "failed", "cancelled", "interrupted"], payload: Mapping[str, Any] | None = None) -> WalkLogRecord | None` appends the final terminal record, publishes it before closing the broker, closes the sink, and removes the run from active state
- `WalkLogService.replay(run_id: str, after_seq: int = -1) -> tuple[WalkLogRecord, ...]`
- `WalkLogService.open_subscription(run_id: str, after_seq: int = -1, loop: asyncio.AbstractEventLoop | None = None) -> WalkLogSubscription` (replay snapshot comes from the authoritative file for active AND completed runs; broker bridges only records after the file tail)
- `WalkLogSink.append(event: str, payload: Mapping[str, Any] | None = None, *, terminal: bool = False) -> WalkLogRecord | None`
- `WalkLogSink.append_terminal(status: str, payload: Mapping[str, Any] | None = None) -> WalkLogRecord | None`
- `WalkLogSink.close_partial(status: Literal["partial", "aborted"] = "aborted") -> None`
- `WalkLogSubscription.replay -> tuple[WalkLogRecord, ...]`
- `WalkLogSubscription.next_event() -> Awaitable[WalkLogRecord | None]`
- `WalkLogSubscription.close() -> None`
- `WalkLogSubscription.__aiter__() -> AsyncIterator[WalkLogRecord]`
- `WalkLogRecord`: immutable DTO with `run_id`, monotonic integer `seq`, opaque `{run_id}:{seq}` `id`, `event`, normalized `data`, and `terminal`.

The sink serializes and flushes under its lock; the broker publishes only after a
successful flushed append. `open_subscription` captures replay and registers the
subscriber under one broker lock. Notifications use `asyncio` loop-safe scheduling,
never timers or polling. The service is process-owned and is not a multi-worker
delivery mechanism.

---

## API Contracts

- `get_walk_log_service() -> WalkLogService` returns the process-owned Part A service used by the mounted pipeline router; tests may override this dependency, but API and runner wiring share one service instance.
- `GET /api/pipeline/walks/log/{run_id}` validates a canonical UUID before lookup or subscription, queries `walk_run` by parameterized ID, returns `400` for malformed UUID or invalid `Last-Event-ID`, `404` for unknown run, and `410` for a known run whose ephemeral `{root}/{run_id}.log` is absent. The database row/status is never changed by a missing-file response.
- The log route returns `200` `text/event-stream` with `Cache-Control: no-cache`, `Connection: keep-alive`, and `X-Accel-Buffering: no`. It derives the file path only from the validated canonical UUID and rejects traversal or symlink escape.
- `Last-Event-ID` is empty or exactly `{run_id}:{non-negative integer}` for the path run. Malformed, foreign-run, negative, or impossible values return `400` before `WalkLogService.open_subscription` is called. A valid sequence greater than the current tail waits for future events unless the run is already terminal.
- The route calls `WalkLogService.open_subscription(run_id, after_seq=..., loop=...)` once after validation; its file replay snapshot and subscriber registration are atomic. The file is the replay source for both active and completed runs (AMENDED 2026-08-16: no longer broker-bounded for active runs; no synthetic overflow event is ever emitted).
- The SSE generator emits each `WalkLogRecord` as `id: {run_id}:{seq}`, `event: log`, and one JSON `data:` payload followed by a blank line. The terminal log record is followed by `event: complete` with JSON `{run_id,status}` and then the stream ends. Partial trailing JSONL lines are ignored/logged by the Part A replay contract and are never emitted.
- Subscription closure is performed in generator `finally` for normal completion, client cancellation, and disconnect; it is non-blocking and cannot stop or delay synchronous walk execution.
- `POST /api/pipeline/run_walk` preserves `status: "started"` and `walk_name`, adds `started: true` and `run_id`, and uses `reserve_walk_run(storage, run_id, book_id, walk_name)` followed by `runner.run_walk_reserved(run_id, walk_name, book_id, config)`.
- `POST /api/pipeline/run_all_walks` returns `started: true`, `batch_id`, ordered `run_ids`, and ordered `runs: [{walk_name, run_id}, ...]` (while retaining the existing `status: "started"` compatibility field), uses `reserve_all_walk_runs(storage, book_id, reservations)`, and schedules `runner.run_all_walks_reserved(batch_id, reservations, book_id, config)`.
- Allocation or scheduling failure invokes `mark_reserved_runs_failed(storage, run_ids, error)` for still-pending rows and returns the existing API error shape; no failed reservation is executed.

### Part B runner integration

- `reserve_walk_run(storage: PipelineStorage, run_id: str, book_id: str, walk_name: str, created_ms: int | None = None) -> str` validates a canonical UUID and allowed walk name, inserts one exact `pending` `walk_run` row with `cancel_requested=0`, `heartbeat_ms=created_ms`, and null result/error/finished fields, and returns the same run ID. The caller owns UUID generation and scheduling.
- `reserve_all_walk_runs(storage: PipelineStorage, book_id: str, reservations: Sequence[tuple[str, str]], created_ms: int | None = None) -> tuple[tuple[str, str], ...]` validates nine unique canonical child UUIDs covering `WALK_ORDER` exactly, inserts all pending rows, and returns normalized `(walk_name, run_id)` pairs in `WALK_ORDER`.
- `mark_reserved_runs_failed(storage: PipelineStorage, run_ids: Iterable[str], error: str) -> None` marks still-pending reservations failed without executing them; it is used when allocation or scheduling fails.
- `WalkRunner.run_walk_reserved(run_id: str, walk_name: str, book_id: str, config: dict) -> dict` verifies the exact pending row, transitions it to running, executes with `HeartbeatStorage(self._storage, run_id)`, and owns sink/terminal/finalization. It never allocates or discovers a run ID.
- `WalkRunner.run_all_walks_reserved(batch_id: str, reservations: Sequence[tuple[str, str]], book_id: str, config: dict) -> dict[str, dict]` consumes the complete ordered child reservation, executes serially, and terminalizes unstarted children on abort/cancellation; `batch_id` is correlation-only and has no `walk_run` row.
- `WALK_LOG_SINK: ContextVar[WalkLogSink | None]` is the shared helper seam. `get_walk_log_sink() -> WalkLogSink | None` reads it; the runner sets it with `WALK_LOG_SINK.set(sink)` immediately before `walk_module.execute(...)` and always resets the returned token in `finally`. Helpers never mutate the variable or import a walk module.
- `WalkRunner.__init__(storage: PipelineStorage, log_service: WalkLogService | None = None) -> None` — the reserved methods perform NO sink operations when `log_service` is None (the default; all pre-existing `WalkRunner(storage)` callers construct this way). The constructor accepts the optional Part A service for per-run sinks.
- `chat_completion(...) -> str` emits an optional `llm` sink record containing timestamp, model, temperature, reasoning effort, prompts, response, finish reason, and usage; SDK-omitted metadata is null and argument temperature/reasoning effort are null only when passed null. The helper's original stripped return value is preserved exactly, a sink failure is logged and swallowed (never propagated), and `app.utils.log_llm_response` is NEVER called. `extract_json_from_llm_response(...)` emits an optional `parse` record containing success and expected type (plus an `error` outcome key) at EVERY return outcome — direct success, regex fallback success, invalid expected type, type mismatch, and malformed — without changing parser decisions or return values.
- ContextVar seam location: `WALK_LOG_SINK` and `get_walk_log_sink()` live in `app/pipeline/walks/_llm_helpers.py`. Only the runner calls `set`/`reset` (via the normal `ContextVar.set(sink)` token, reset in `finally` on every terminal path — success, exception, import failure, verification failure, and when no sink was opened the token is None so reset is a no-op). Helpers only read it; walk modules must never import the seam symbols (static-audit enforced).
- Terminal record contract: the runner appends exactly ONE terminal record before `_finalize_run` via `log_service.close_run(run_id, status, payload)`; Part A appends the terminal record, publishes it, closes the sink, and deregisters the run. Failure payload is `{error, traceback}` with a bounded traceback (`traceback.format_exc()`; 32 KiB truncation and secret redaction are owned by Part A's sink). Sink failures never alter `walk_run` status/error/result — the DB row is authoritative.
- Part B ownership boundary for Part C: the API layer owns production wiring of the process-owned singleton (`get_walk_log_service()` / `app.state.walk_log_service`) and passes it to `WalkRunner(log_service=...)`. Part C owns the reservation endpoints and SSE. The runner consumes reserved IDs only and never allocates or discovers a run ID.

### Part D frontend viewer

- `renderWalkRuns(runs: WalkRunRow[]) -> void` reconciles `#walk-runs-container` by `run_id`; existing rows update status/history fields without replacing the viewer, its open state, rendered event IDs, or the run-keyed EventSource entry. Missing rows are closed and removed.
- `openWalkLog(runId: string) -> void` is idempotent for an already-open run and creates one EventSource for `/api/pipeline/walks/log/{run_id}` using the server-provided run ID. `closeWalkLog(runId: string) -> void` is idempotent and closes/removes the active source.
- Each row contains `button[data-walk-log-open="{run_id}"]` and sibling `div[data-walk-log-viewer="{run_id}"]`, with stable IDs `walk-log-{run_id}` and `walk-log-status-{run_id}`. Delegated controls are not re-registered by repeated initialization.
- `log` events are JSON-parsed and deduped by opaque `{run_id}:{seq}` IDs across reconnects and status refreshes. `complete` updates the stable status element and closes the source. 410 and other terminal errors close/clean the source and publish bounded status text.
- Records use text nodes/`textContent` only, bounded readable fields and fixed client rendered-element/text caps. Prompt/response/error values are never HTML-interpolated and the registry is keyed by server run IDs.

---

## DTOs Created

*(empty)*

### Part B DTOs

- Reservation pairs are immutable `(walk_name: str, run_id: str)` tuples normalized in `WALK_ORDER`; `batch_id` is a canonical UUID correlation value generated by the API and is not persisted as a parent row.

---

## Decisions Made

- Part A uses a process-owned JSONL sink plus an in-memory per-run broker; SQLite is
  not used for delivery or replay.
- Sink limits are strict encoded-byte limits: 10 MiB total with a 64 KiB terminal
  reserve, projected before every append; ordinary records may be dropped, but one
  bounded overflow marker and the terminal record are attempted when space permits.
- Broker limits are 256 events or 1 MiB, whichever comes first; oldest non-terminal
  records are dropped and one synthetic `overflow` event is emitted.
- **AMENDED 2026-08-16 (approved design change):** the synthetic broker `overflow`
  event is removed. Broker eviction drops oldest non-terminal records **silently**;
  live stream gaps in `seq` are expected and normal (industry-standard SSE gap
  handling — WHATWG #8297, django-eventstream, server-sent-events.com). The `id` is
  an opaque cursor, not an event count. The authoritative JSONL file is the replay
  source for **both active and completed runs**; the broker only bridges records
  after the file tail. File-cap overflow markers remain real file records with real
  sink seqs (not broker-side synthetic events).
- Startup cleanup removes only UUID-named log files older than 24 hours. Shutdown
  closes remaining runs as `aborted`/`partial` and releases subscribers/brokers.
- The Part A boundary permits only the app lifespan integration; runner, API,
  helper, and nine walk-module wiring are deferred to Parts B/C and remain untouched.
