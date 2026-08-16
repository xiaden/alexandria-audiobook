# Per-Walk Log Streaming — Implementation Parts

**Design doc:** `artifacts/designs/pending/DD-per-walk-log-streaming.md`

## Parts

| Part | Title | Depends On | Layers |
| --- | --- | --- | --- |
| A | Walk-log service core: JSONL file sink + per-run broker | None | service (backend) |
| B | Runner integration: ContextVar bridge, reserved run-ID handoff, LLM/parse instrumentation | A | service (backend) |
| C | SSE endpoint: `GET /api/pipeline/walks/log/{run_id}` with replay + Last-Event-ID | A, B | API (backend) |
| D | Frontend viewer: EventSource lifecycle + stable DOM reconciliation | C | interface (frontend) |

## Dependency Graph

```
A → B → C → D
```

## Execution Rounds

Round 1: A
Round 2: B
Round 3: C
Round 4: D

## Per-Part Scope

### Part A: Walk-log service core

Creates the new walk-log service module(s): a process-owned JSONL file sink writing
to `/tmp/alexandria-walks/{run_id}.log` (dir mode 0700, file mode 0600, flush per
record, 10 MiB cap with 64 KiB terminal reserve, overflow marker), redaction
(common API-key/bearer patterns) and truncation (64 KiB prompt/response, 32 KiB
traceback, 128 KiB event), and a thread-safe per-run broker (max 256 events or
1 MiB, SILENT eviction of oldest non-terminal records — no synthetic broker
overflow event, live-seq gaps normal, unique opaque IDs/seqs, terminal always
last and never evicted, async-loop-safe notification). Startup creates the dir
and removes UUID-named files older than 24h; shutdown closes sinks as
partial/aborted. Exposes the SSE-facing replay + live-subscribe interface and the
writer-facing append interface. For BOTH active and completed runs the
authoritative JSONL file is the replay source; open_subscription replays the
file (filtered by after_seq), then atomically bridges only post-file-tail broker
records via the loop-safe queue — no timers/polling. The file-cap overflow marker
remains a real sink record with a real sink seq. No runner or API changes in this
part.

### Part B: Runner integration

Wires the walk-log service into `walks/runner.py` and `walks/_llm_helpers.py`:
runner sets a `ContextVar` with the current sink before `walk_module.execute(...)`
and resets it in `finally` on every terminal path; `chat_completion` and
`extract_json_from_llm_response` read the ContextVar and emit LLM/parse records
(null-tolerant metadata). Refactors `run_walk`/`run_all_walks` to the reserved
run-ID contract: API layer generates UUIDs and inserts pending `walk_run` rows,
runner consumes `run_walk_reserved`/`run_all_walks_reserved`, never allocates IDs.
Finalization writes terminal status + bounded traceback to the sink before
`_finalize_run`. Zero changes to the nine walk module files. Depends on A for the
sink/broker API.

### Part C: SSE endpoint

Adds `GET /api/pipeline/walks/log/{run_id}` through the mounted pipeline router:
UUID syntax validation, run lookup, path derivation from validated UUID only,
404 unknown / 410 known-but-file-gone, `text/event-stream` response, per-record
`id: {run_id}:{seq}` + `event: log` + JSON data, terminal `event: complete`
`{run_id,status}`, `Last-Event-ID` replay then atomic live attach, 400 on
malformed/foreign/negative/impossible Last-Event-ID, partial trailing lines
ignored. Uses the broker's replay/live interface from A and the runner's terminal
state from B.

### Part D: Frontend viewer

Updates `frontend/src/tabs/script.ts`: `renderWalkRuns` reconciles rows by run ID,
preserving open viewers, open state, rendered event IDs, and an EventSource
registry; per-row `button[data-walk-log-open]` + sibling viewer div with stable
IDs; EventSource lifecycle (open/error/complete/410 close, idempotent open,
replay dedup by ID); `textContent` rendering; removal cleanup. The 2s status
poll, `main.ts` module-scope init, and DOMContentLoaded wiring stay unchanged.
Depends on C for the SSE event contract.
