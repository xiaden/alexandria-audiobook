# Per-Walk Log Streaming — Design Document

- **Status:** Pending — amended 2026-08-16 (Approved design change: synthetic overflow event removed; file-authoritative replay for active runs; seq gaps treated as normal per industry-standard SSE gap handling)
- **Author:** rnd-dd-author
- **Date:** 2026-08-15

## Problem Statement

Plan H removed the legacy live subprocess-output pipeline, leaving the Script tab's
Generation Logs card without a producer. Users need live LLM generation and parse
activity for an individual `walk_run`, including failures. `walk_run` remains the
status authority; logs are ephemeral diagnostics. Delivery is event-driven and no
one of the nine walk modules may change.

## Requirements and Scope

Create `/tmp/alexandria-walks/{run_id}.log`, append/flush records during execution,
show them live, and append terminal status plus traceback on failure. Preserve the
existing status/history polling and open viewer state. Tests are written first.

## Architecture

### Layer Mapping

| Component | Layer | Responsibility |
|---|---|---|
| `api_walks/service` layer | service | Reserve and persist IDs |
| `walks/runner.py` | service | Consume reserved IDs and own execution/finalization only |
| `walks/_llm_helpers.py` | shared seam | Capture LLM response and parse outcomes |
| New walk-log service | service | JSONL sink and thread-safe per-run broker |
| `api_walks.py` | API | Mounted SSE route and run validation |
| `tabs/script.ts` | frontend | Stable keyed viewer and EventSource lifecycle |

The runner sets a `ContextVar` containing the current sink before
`walk_module.execute(book_id, HeartbeatStorage(...), config)` and resets it in a
`finally` block on every success, exception, cancellation, and setup failure path.
The shared helpers read it, preserving the hard zero-walk-module-change boundary.
`HeartbeatStorage` remains the runner's per-run carrier; it is not the async bridge.

### Run-ID Client Contract

The API layer owns reservation, not the background runner. For `run_walk`, it
generates a canonical UUID `run_id`, inserts a `walk_run` row with the existing
book/walk association, `status='pending'`, `cancel_requested=0`,
`heartbeat_ms=created_ms`, and null result/error/finished fields, then schedules
`runner.run_walk_reserved(run_id, ...)`. The runner receives this ID, verifies the
pending row, transitions it to `running`, and never generates a replacement.
Allocation, insertion, or scheduling failure is API-owned; a failed schedule
marks the pending row `failed` and does not execute it.

The endpoint returns existing compatibility fields `{started: true, walk_name}`
plus `run_id`. It is returned only after reservation and before scheduling, so the
client never discovers a latest row or races the first event.

`run_all_walks` generates a canonical UUID `batch_id` (correlation only), generates
all nine child UUIDs in `WALK_ORDER`, inserts nine pending `walk_run` rows, then
schedules `runner.run_all_walks_reserved(batch_id, [{walk_name, run_id}, ...])`.
The runner receives the complete reservation, transitions children one at a time,
and never allocates or discovers children. The response is
`{started: true, batch_id, run_ids: [...], runs: [{walk_name, run_id}, ...]}`;
`batch_id` is the parent request ID and each listed ID is a child stream ID.
There is no parent status row: child rows are truth. The API owns allocation,
persistence, compatibility responses, and reservation cleanup; the runner owns
execution, sinks, terminal status, and not-yet-started child cancellation. A
failed batch schedule marks pending children failed; explicit cancellation marks
them cancelled. Pre-execution terminal children receive DB terminal status only
(no sink is opened for a child that never started, so no walk-log record is
written for it) — matching CONTRACTS.md and the implementation.

### File Sink, Metadata, and Thread/Async Bridge

The process-owned sink creates the directory with mode `0700`; files are mode
`0600`, UTF-8 JSONL, and flushed after every record. A header contains book ID,
walk name, run ID, and start time. Each LLM record contains timestamp, model,
temperature, reasoning effort, prompts, response, finish reason, and usage.

`chat_completion` extracts `response.model`, the first choice's
`finish_reason`, and `response.usage` (including prompt/completion/total tokens
when present); model, finish reason, reasoning effort, and usage fields are null
when the SDK response omits them. Temperature and reasoning effort come from the
function arguments and are null only when the argument is null. Parse records
capture success, expected type, or failure. This is new per-run capture and does
not call or alter legacy `app.utils.log_llm_response`, whose `logs/{log_name}`
append format has no pipeline callers.

The synchronous background writer publishes a flushed record to a thread-safe
per-run broker. The async SSE generator waits on that broker through an
`asyncio`-loop-safe notification (no timer, file polling, or SQLite polling),
then reads replay/live records. The app lifespan owns broker startup and shutdown:
shutdown closes subscribers and sinks; terminal completion closes that run's
broker after publishing its final record. Each broker retains at most 256 events
or 1 MiB, whichever comes first; overflow drops oldest non-terminal events
**silently** — no synthetic event is emitted and no broker-side seq is consumed.
Live stream gaps in `seq` are expected and normal: per industry-standard SSE gap
handling (WHATWG #8297, django-eventstream, server-sent-events.com), the `id` is
an opaque cursor, not an event count, and clients must not assume contiguity. The
file remains authoritative for replay and is the replay source for **both active
and completed runs**, so a client that reconnects recovers everything the file
holds regardless of broker eviction. No multi-worker guarantee is made: SSE and
writer must share one process/container unless a future shared broker is
introduced.

### API and SSE Event Contract

Add `GET /api/pipeline/walks/log/{run_id}` through the existing mounted pipeline
router. Validate UUID syntax, look up the run, and derive the path only from the
validated UUID. Unknown IDs return 404. A known run whose ephemeral file is gone
after restart returns **410 Gone**; its database status remains usable. Response
type is `text/event-stream`.

Every JSONL record has monotonically increasing per-run integer `seq` and an
opaque `id` equal to `{run_id}:{seq}`. SSE sends `id`, `event: log`, and JSON data;
the terminal status is the last log record, followed by `event: complete` with
`{run_id,status}`. Clients send `Last-Event-ID`; the server replays records with
`seq` greater than that ID **from the authoritative file — for both active and
completed runs** — then attaches the live subscription atomically (the broker
bridges only records after the file tail). A completed run replays its file and
immediately completes; reconnects may still duplicate records, so the client
ignores already-rendered IDs. The stream never contains a synthetic overflow
event; when the broker evicts, the live client simply observes a `seq` gap, which
is normal. `Last-Event-ID` must be empty or exactly
`{run_id}:{non-negative integer}` matching the path.
Malformed, foreign-run, negative, or impossible values return 400 before opening
the stream. A valid sequence beyond the current tail waits for future events (or
completes immediately when already terminal). Partial trailing JSONL lines are
ignored and logged, never emitted as events.

## Lifecycle, Concurrency, Error, and Security Behavior

The file/header is created after the persisted run exists and before execution.
Different books may run concurrently; each UUID has an independent locked sink.
Finalization always records `completed`, `failed`, or `cancelled`; exceptions write
bounded `traceback.format_exc()` before the terminal record and preserve existing
database error/result behavior. Disconnects remove subscribers and never block a
walk. Prompts and responses are redacted for common API-key/bearer-token patterns
and truncated with an explicit marker: each is at most 64 KiB; traceback 32 KiB;
one event 128 KiB. The 10 MiB file cap is strict: the sink reserves 64 KiB for a
compact terminal record, drops ordinary records that would consume that reserve,
and writes one bounded overflow marker when space permits. The sink checks the
projected encoded byte size before every append, so neither an ordinary record nor
the marker can consume the terminal reserve. The file-cap overflow marker is a
real file record (it receives a real sink seq; it is not a broker-side synthetic
event). Terminal status is always attempted and compacted
to the reserve; dropped ordinary records do not change database status/error.
Filesystem failure during terminal write is logged while database finalization is
preserved. Frontend rendering uses `textContent`, never HTML interpolation.

At startup, the service creates the directory and removes only UUID-named files
older than 24 hours; active in-process runs cannot be stale at that point. At
shutdown it closes all remaining sinks as partial/aborted and releases brokers.
Partial files remain readable until cleanup; restart does not reconstruct them.

## Frontend Viewer Contract

Each row is keyed by `data-walk-run-row="{run_id}"` and contains
`button[data-walk-log-open="{run_id}"]` plus a sibling
`div[data-walk-log-viewer="{run_id}"]` with stable IDs
`walk-log-{run_id}` and `walk-log-status-{run_id}`. `renderWalkRuns` reconciles
rows by run ID: it updates status text while preserving existing viewer nodes,
open state, rendered event IDs, and an `EventSource` registry keyed by run ID;
it does not replace an open viewer with `innerHTML`. Removed rows close and delete
their source/registry entry. Opening an already-open run is idempotent; complete,
410, and error close it. The client deduplicates by the opaque `{run_id}:{seq}`
id and treats seq gaps as normal (no overflow-event handling exists on the client
side, per the SSE contract). The 2-second status poll remains unchanged, as do
`main.ts` module-scope initialization and DOMContentLoaded wiring.

## Transport Trade-offs and Decision

**SSE is chosen:** native `EventSource`, one-way logs, automatic reconnect, and
FastAPI `StreamingResponse` fit the contract. **WebSocket is rejected** because
bidirectional control is not required and adds lifecycle/operations complexity.
**Polling is rejected** because it violates the hard event-driven requirement and
adds latency/read load. **File-only/in-memory-only** alternatives respectively
lack live delivery/replay. SSE is a future architectural constraint; recommend an
ADR when decision infrastructure is onboarded (none exists now).

## TDD Test Plan

Tests precede implementation. Backend pytest tests use the active
`tests/pipeline/test_api.py` location (and focused pipeline modules), run with
`pytest tests/pipeline` or the repository full `pytest` command, `tmp_path`, fake
SDK responses, and FastAPI `TestClient`. `app/test_api.py` is an application/live
server script (`__test__ = False`), not a pytest location; retain it only as a
compatibility smoke harness. `tests/external/` remains the external-test
convention. Include reservation handoff and returned parent/child IDs, pending
cleanup, sink permissions/limits/redaction/flush,
null metadata, parse outcomes, ContextVar reset on every terminal path, concurrent
runs, broker-eviction gap behavior (silent drops, no synthetic events, gaps
treated as normal, file-authoritative replay for active runs),
file-cap drop-marker/terminal-guarantee behavior, startup cleanup/shutdown,
malformed/foreign/negative/impossible Last-Event-ID handling, replay, duplicate
IDs, 404/410/traversal, and a mounted-router SSE smoke test through the real app.
Static/import audit asserts all nine walk module files are unchanged and a
representative instrumented execution proves both shared helper seams emit logs.

Frontend uses `npm test -- --run` (`vitest run`) under `frontend/` and
`frontend/tests/frontend/`: mock EventSource and test exact URL/run association,
event parsing, replay deduplication, safe rendering/limits, completion/error
cleanup, stable DOM reconciliation across refresh, and removal cleanup. The
existing 499 tests remain green.

## Effort Estimate

**LARGE** (Architect confidence high; Estimator confidence medium): approximately
10–11 files, about 16 edit sections, a sync/async concurrency bridge, SSE replay,
security limits, stable DOM reconciliation, and a substantial tests-first surface.

## Scope Validation

This is one cohesive feature: capture, transport, viewer, and tests land together.
It does not redesign `walk_run`, persist logs across restarts, add log search or
download, replace status polling, or add server control messages.

**Zero walk-module change is achievable: yes.** Runner setup/finalization plus the
existing `chat_completion` and `extract_json_from_llm_response` choke points cover
all nine walks; the static/import audit is an explicit acceptance test.

## Rollout and Observability

Log sink creation/failure, subscriber counts, overflow, disconnects, partial-file
cleanup, terminal delivery, and SSE 404/410/error counts with run ID/status only;
never log prompt/response bodies. Validate single-process deployment and document
the absent multi-worker guarantee. Database run status/error remains the fallback
when a log is missing or streaming fails.

## Open Questions

1. Should a future authenticated deployment authorize access by user/book in
   addition to the current run-ID contract?
2. Should a future release provide a separate completed-log download endpoint?
