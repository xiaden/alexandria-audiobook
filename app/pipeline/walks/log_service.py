"""Bounded secure per-walk JSONL log service (Part A).

This module owns the process-local JSONL sink and the service orchestration for
per-walk ephemeral logs under a configured root directory
(default ``/tmp/alexandria-walks``). It provides:

* :class:`WalkLogRecord` -- an immutable normalized record DTO.
* :class:`WalkLogSink` -- a thread-safe, capacity-bounded JSONL sink that
  redacts secrets, truncates oversized fields, enforces a strict 10 MiB cap
  with a 64 KiB terminal reserve, and flushes every successful write.
* :class:`WalkLogService` -- the process-owned service that validates canonical
  UUIDs, derives secure ``{root}/{run_id}.log`` paths, owns startup cleanup and
  shutdown closure, and provides replay.
* :class:`WalkLogSubscription` -- the subscription surface carrying the immutable
  replay snapshot plus loop-safe live delivery from the per-run broker via
  ``asyncio`` ``call_soon_threadsafe`` (no timers, file polling, or SQLite
  polling).

Security invariants: paths are derived only from validated canonical UUIDs,
files are created with mode ``0600`` and opened with ``O_NOFOLLOW``, the root
directory is created with mode ``0700``, secrets are redacted to a fixed safe
replacement, and operational logs carry run ID/status/counts only -- never
prompt/response bodies.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Explicit marker appended to truncated fields.
TRUNCATION_MARKER = "[truncated]"

#: Redacted replacement that never echoes the secret.
REDACTED = "[REDACTED]"

#: Per-field byte caps for LLM-shaped payload keys.
PROMPT_MAX_BYTES = 64 * 1024  # 64 KiB
TRACEBACK_MAX_BYTES = 32 * 1024  # 32 KiB
EVENT_MAX_BYTES = 128 * 1024  # 128 KiB (whole serialized event incl. framing)

#: Whole-file strict cap and the terminal reserve protected from ordinary records.
SINK_CAP_BYTES = 10 * 1024 * 1024  # 10 MiB
SINK_TERMINAL_RESERVE_BYTES = 64 * 1024  # 64 KiB

#: File-cap overflow marker event name (a real sink record with a real sink seq).
SINK_OVERFLOW_EVENT = "overflow"

#: Per-run live broker caps: at most 256 events or 1 MiB of retained payload
#: bytes, whichever comes first. Oldest non-terminal records are evicted
#: SILENTLY (no synthetic event, no broker-created sequence); live stream gaps
#: are normal. The terminal record is never evicted. Sequences are exclusively
#: real sink sequences -- the broker never creates one.
BROKER_MAX_EVENTS = 256
BROKER_MAX_BYTES = 1024 * 1024  # 1 MiB

#: Stale log-file cleanup window in seconds (24 hours).
_STALE_LOG_SECONDS = 24 * 60 * 60

#: Finite iteration budget for ``_shrink_to_fit`` so an oversized component
#: that cannot be shrunk below the whole-event cap can never cause an infinite
#: loop (DoS guard).
_SHRINK_PASS_BUDGET = 10

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Field names that receive an explicit per-field byte cap (applied at any depth
# so oversized nested payloads cannot bypass the whole-event cap).
_FIELD_LIMITS: dict[str, int] = {
    "prompts": PROMPT_MAX_BYTES,
    "response": PROMPT_MAX_BYTES,
    "text": TRACEBACK_MAX_BYTES,
    "traceback": TRACEBACK_MAX_BYTES,
}


def _now_ms() -> int:
    """Return the current wall-clock time in epoch milliseconds.

    This module-level seam is monkeypatched by tests to drive lifecycle/header
    behavior deterministically.
    """
    return int(time.time() * 1000)


def _is_valid_uuid(value: str) -> bool:
    """Return True only for a syntactically canonical UUID string."""
    return bool(_UUID_RE.match(value))


# ---------------------------------------------------------------------------
# Redaction / bounded normalization
# ---------------------------------------------------------------------------


_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+")
_API_KEY_ASSIGN_RE = re.compile(
    r"(?i)(api[ _-]?key\s*=\s*)([\"']?)[^\"'\s,;]+"
)
#: A dict key that names a credential: ``api_key`` / ``apikey`` / ``api-key``
#: / ``api key`` (any casing). When such a key holds a string value (as opposed
#: to an ``api_key = ...`` assignment inside a free-text string), the value is
#: treated as the credential and redacted wholesale — covering the JSON
#: ``{"api_key": "<secret>"}`` shape at any nesting depth.
_API_KEY_DICT_KEY_RE = re.compile(r"(?i)^api[ _-]?key$")


def _redact_text(text: str) -> str:
    """Replace common secret shapes with a fixed, non-echoing replacement."""
    text = _OPENAI_KEY_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    text = _API_KEY_ASSIGN_RE.sub(REDACTED, text)
    return text


def _redact_strings(node: Any) -> Any:
    """Recursively redact every string value in a nested structure.

    Two redaction passes apply at any depth:

    * every string is scanned for sk- / Bearer / ``api_key =`` shapes
      (``_redact_text``); and
    * the value of any dict key matching an api-key name is redacted wholesale
      (the JSON ``{"api_key": "<secret>"}`` shape, where the secret would not
      otherwise match an sk-/Bearer pattern).
    """
    if isinstance(node, str):
        return _redact_text(node)
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(value, str) and _API_KEY_DICT_KEY_RE.match(str(key)):
                out[key] = REDACTED
            else:
                out[key] = _redact_strings(value)
        return out
    if isinstance(node, list):
        return [_redact_strings(x) for x in node]
    return node


def _truncate_str(value: str, max_bytes: int, marker: str = TRUNCATION_MARKER) -> str:
    """Truncate ``value`` to at most ``max_bytes`` UTF-8 bytes, appending the
    explicit marker when truncated. The result is always valid UTF-8."""
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    budget = max_bytes - len(marker.encode("utf-8"))
    if budget <= 0:
        return marker
    kept = value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return kept + marker


def _truncate_fields(node: Any) -> Any:
    """Recursively apply the per-field byte caps to matching keys."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(value, str) and key in _FIELD_LIMITS:
                out[key] = _truncate_str(value, _FIELD_LIMITS[key])
            else:
                out[key] = _truncate_fields(value)
        return out
    if isinstance(node, list):
        return [_truncate_fields(x) for x in node]
    return node


def _normalize_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secrets and apply per-field truncation to a payload dict."""
    redacted = _redact_strings(data)
    bounded = _truncate_fields(redacted)
    return bounded if isinstance(bounded, dict) else dict(bounded)


def _largest_string_holder(node: Any) -> tuple[Any, Any] | None:
    """Return ``(container, key)`` of the largest string in a nested structure."""
    best: tuple[Any, Any] | None = None
    best_len = -1

    def walk(curr: Any, holder: Any, key: Any) -> None:
        nonlocal best, best_len
        if isinstance(curr, dict):
            for k, v in curr.items():
                walk(v, curr, k)
        elif isinstance(curr, list):
            for i, v in enumerate(curr):
                walk(v, curr, i)
        elif isinstance(curr, str) and len(curr) > best_len:
            best_len = len(curr)
            best = (holder, key)

    walk(node, None, None)
    return best


def _contains_bounded_field(node: Any) -> bool:
    """Return True if ``node`` contains any per-field-bounded key.

    The whole-event 128 KiB cap applies to LLM/traceback-shaped records (those
    carrying ``prompts``/``response``/``text``/``traceback`` fields), where the
    per-field truncation alone can still exceed the event cap. Generic event
    records without these fields are bounded only by the sink's 10 MiB file cap,
    so large opaque payloads (e.g. work blobs) still reach the file cap.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FIELD_LIMITS:
                return True
            if isinstance(value, (dict, list)) and _contains_bounded_field(value):
                return True
    elif isinstance(node, list):
        return any(_contains_bounded_field(x) for x in node)
    return False


def _shrink_to_fit(record: dict[str, Any], max_bytes: int) -> str:
    """Serialize ``record`` to a single JSONL line <= ``max_bytes`` bytes.

    On each pass the largest string among the top-level ``event`` and every
    string inside ``data`` is shrunk (appending the marker) until the whole line
    fits. A finite iteration budget (``_SHRINK_PASS_BUDGET``) guarantees an
    oversized component that cannot be shrunk below the cap can never cause an
    infinite loop. Once the budget is exhausted or no further shrink is
    possible, the record is replaced by a minimal bounded form whose ``event``
    is truncated so the resulting line always fits.
    """
    for _ in range(_SHRINK_PASS_BUDGET):
        line = json.dumps(record, ensure_ascii=False, separators=(", ", ": "))
        if len(line.encode("utf-8")) <= max_bytes:
            return line + "\n"
        target = _largest_shrink_candidate(record)
        if target is None:
            break
        container, key = target
        replacement = _truncate_str(container[key], max(len(container[key]) // 2, 16))
        if replacement == container[key]:
            # At an unshrinkable floor (e.g. already the bare marker): stop.
            break
        container[key] = replacement

    # Budget exhausted or unshrinkable floor: emit a minimal bounded record and
    # truncate the event so the line fits (also a bounded pass loop).
    record["data"] = {"truncated": True}
    for _ in range(_SHRINK_PASS_BUDGET):
        line = json.dumps(record, ensure_ascii=False, separators=(", ", ": "))
        if len(line.encode("utf-8")) <= max_bytes:
            return line + "\n"
        event = record.get("event")
        if not isinstance(event, str):
            record["event"] = TRUNCATION_MARKER
            continue
        replacement = _truncate_str(event, max(len(event) // 2, 16))
        if replacement == event:
            record["event"] = TRUNCATION_MARKER
        else:
            record["event"] = replacement
    # Last resort: a fixed minimal line. Make the ≤``max_bytes`` whole-line
    # guarantee absolute. Only reachable for pathological inputs (e.g. a top-level
    # ``event`` string >~128 MiB that survives the bounded halving loop -- never
    # emitted by current producers). After the bounded pass, ``data`` is the tiny
    # ``{"truncated": True}`` and the only variable left is ``event``; truncate it
    # so the serialized line always fits.
    line = json.dumps(record, ensure_ascii=False, separators=(", ", ": "))
    if len(line.encode("utf-8")) <= max_bytes:
        return line + "\n"
    event = record.get("event")
    if isinstance(event, str) and event != TRUNCATION_MARKER:
        # Reserve generous framing space for the fixed record fields (run_id, seq,
        # id, data, terminal) and truncate the event into the remaining budget.
        budget = max(max_bytes - 256, 1)
        record["event"] = _truncate_str(event, budget)
        line = json.dumps(record, ensure_ascii=False, separators=(", ", ": "))
        if len(line.encode("utf-8")) > max_bytes:
            # Even the fixed framing pushes it over: drop to the tiny marker,
            # guaranteeing the line fits.
            record["event"] = TRUNCATION_MARKER
            line = json.dumps(record, ensure_ascii=False, separators=(", ", ": "))
    return line + "\n"


def _largest_shrink_candidate(record: dict[str, Any]) -> tuple[Any, Any] | None:
    """Return ``(container, key)`` of the largest shrinkable string in a record:
    the biggest string inside ``data`` or the top-level ``event``, whichever is
    longer. ``None`` when there is no string to shrink."""
    best: tuple[Any, Any] | None = None
    best_len = -1
    holder = _largest_string_holder(record.get("data"))
    if holder is not None:
        container, key = holder
        if isinstance(container[key], str) and len(container[key]) > best_len:
            best, best_len = (container, key), len(container[key])
    event = record.get("event")
    if isinstance(event, str) and len(event) > best_len:
        best, best_len = (record, "event"), len(event)
    return best


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WalkLogRecord:
    """Immutable normalized per-walk log record."""

    run_id: str
    seq: int
    id: str
    event: str
    data: Mapping[str, Any]
    terminal: bool


# ---------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------


class WalkLogSink:
    """Thread-safe, capacity-bounded JSONL sink for a single run."""

    def __init__(
        self,
        root_dir: Path,
        run_id: str,
        book_id: str,
        walk_name: str,
        started_ms: int,
    ) -> None:
        self._root_dir = root_dir
        self._run_id = run_id
        self._book_id = book_id
        self._walk_name = walk_name
        self._started_ms = started_ms
        self._path = root_dir / f"{run_id}.log"
        self._lock = threading.Lock()
        self._fh: Any = None
        self._seq = 0
        self._current = 0
        self._overflow_written = False
        self._closed = False
        self._non_reserve_cap = SINK_CAP_BYTES - SINK_TERMINAL_RESERVE_BYTES
        #: The run's live broker, wired by the service at open_run time. Records
        #: are published only after a successful flushed write so live consumers
        #: never observe an unpersisted record.
        self._broker: _RunBroker | None = None

    # -- setup ------------------------------------------------------------

    def _open(self) -> None:
        """Create/open the log file with mode 0600 and write the header."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC
        fd = os.open(self._path, flags, 0o600)
        os.fchmod(fd, 0o600)
        self._fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        header = {
            "book_id": self._book_id,
            "walk_name": self._walk_name,
            "run_id": self._run_id,
            "started_ms": self._started_ms,
        }
        header_line = (
            json.dumps(header, ensure_ascii=False, separators=(", ", ": ")) + "\n"
        )
        self._fh.write(header_line)
        self._fh.flush()
        self._current = len(header_line.encode("utf-8"))

    def _build_record(
        self,
        seq: int,
        event: str,
        payload: Mapping[str, Any] | None,
        terminal: bool,
    ) -> dict[str, Any]:
        data = _normalize_data(payload) if payload is not None else {}
        return {
            "run_id": self._run_id,
            "seq": seq,
            "id": f"{self._run_id}:{seq}",
            "event": event,
            "data": data,
            "terminal": terminal,
        }

    def _serialize_record(self, record: dict[str, Any]) -> str:
        """Serialize a record to a JSONL line, bounding LLM-shaped events to
        128 KiB while leaving generic records at their natural size."""
        if _contains_bounded_field(record.get("data")):
            return _shrink_to_fit(record, EVENT_MAX_BYTES)
        return json.dumps(record, ensure_ascii=False, separators=(", ", ": ")) + "\n"

    def _write(self, line: str) -> bool:
        """Write and flush a line, logging (not raising) on filesystem error."""
        try:
            if self._fh is None:
                return False
            self._fh.write(line)
            self._flush()
            return True
        except OSError:
            logger.warning(
                "walk log write failed; run_id=%s", self._run_id, exc_info=True
            )
            return False

    def _flush(self) -> None:
        """Flush the underlying file handle.

        Kept as a separate per-instance seam so tests can inject filesystem
        failures at the terminal write path: ``self._flush`` runs inside
        ``_write``'s ``try/except OSError``, so a raising ``_flush`` is logged
        and swallowed rather than propagated to callers. (Patching the C-level
        ``io.BufferedWriter.flush`` is impossible on Python 3.13 because ``_io``
        types are immutable.)
        """
        self._fh.flush()

    def _maybe_write_overflow(self) -> bool:
        """Write exactly one bounded overflow marker if space permits."""
        if self._overflow_written:
            return False
        seq = self._seq
        self._seq += 1
        marker = self._build_record(seq, SINK_OVERFLOW_EVENT, {}, False)
        line = self._serialize_record(marker)
        encoded = len(line.encode("utf-8"))
        if self._current + encoded > self._non_reserve_cap:
            return False
        if not self._write(line):
            return False
        self._current += encoded
        self._overflow_written = True
        return True

    # -- public API -------------------------------------------------------

    def append(
        self,
        event: str,
        payload: Mapping[str, Any] | None = None,
        *,
        terminal: bool = False,
    ) -> WalkLogRecord | None:
        """Append one normalized, bounded record; None when dropped."""
        with self._lock:
            if self._closed:
                return None
            seq = self._seq
            self._seq += 1
            record = self._build_record(seq, event, payload, terminal)
            line = self._serialize_record(record)
            projected = len(line.encode("utf-8"))

            if terminal:
                # Terminal records are always attempted within the full cap.
                if self._current + projected > SINK_CAP_BYTES:
                    return None
            elif self._current + projected > self._non_reserve_cap:
                # Ordinary record would consume the terminal reserve -> drop,
                # but emit one overflow marker if it fits in non-reserve space.
                self._maybe_write_overflow()
                return None

            if not self._write(line):
                return None
            self._current += projected
            result = WalkLogRecord(**record)
        # Publish only after the sink lock is released (the broker schedules
        # loop callbacks via call_soon_threadsafe) and only after the write
        # flushed successfully.
        broker = self._broker
        if broker is not None:
            broker.publish(result)
        return result

    def append_terminal(
        self, status: str, payload: Mapping[str, Any] | None = None
    ) -> WalkLogRecord | None:
        """Append a compact terminal record within the reserved space.

        Terminal delivery is best-effort: it returns ``None`` (without raising)
        when the projected terminal exceeds ``SINK_CAP_BYTES`` or the write
        fails -- the terminal lands only when the filesystem permits. In every
        case the sink is marked closed so ``close_partial``/``close`` still
        finalize the sink after a failed terminal write.

        The terminal write and the ``_closed`` transition happen under a single
        lock acquisition, closing the window in which a concurrent writer could
        append a non-terminal record between the terminal write's lock release
        and a later close transition. Any concurrent append either lands strictly
        before the terminal record or is dropped, preserving the 'terminal record
        is last' guarantee.
        """
        data = dict(payload) if payload else {}
        data["status"] = status
        with self._lock:
            # Re-checked under the lock: a pre-lock check would only avoid work;
            # correctness against a concurrent close comes from this check.
            if self._closed:
                return None
            seq = self._seq
            self._seq += 1
            record = self._build_record(seq, "terminal", data, True)
            line = self._serialize_record(record)
            projected = len(line.encode("utf-8"))
            if self._current + projected > SINK_CAP_BYTES:
                self._closed = True
                return None
            wrote = self._write(line)
            if wrote:
                self._current += projected
            self._closed = True
            if not wrote:
                return None
            result = WalkLogRecord(**record)
        # Publish only after the sink lock is released (the broker schedules
        # loop callbacks via call_soon_threadsafe) and only after the write
        # flushed successfully.
        broker = self._broker
        if broker is not None:
            broker.publish(result)
        return result

    def close_partial(self, status: Literal["partial", "aborted"] = "aborted") -> None:
        """Close an unfinished sink with a bounded terminal marker when possible."""
        self.append_terminal(status)

    def close(self) -> None:
        """Close the underlying file handle; idempotent and non-raising."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    logger.warning(
                        "walk log close failed; run_id=%s", self._run_id, exc_info=True
                    )
                self._fh = None
            self._closed = True


# ---------------------------------------------------------------------------
# Per-run live broker
# ---------------------------------------------------------------------------


def _record_bytes(record: WalkLogRecord) -> int:
    """Approximate the live-memory footprint of a broker-retained record.

    Uses the same measure the broker-limit test asserts against (the encoded
    ``data`` payload), so cap accounting and the test's boundary check agree.
    """
    return len(json.dumps(record.data, ensure_ascii=False))


class _RunBroker:
    """Thread-safe bounded live-delivery broker for one run.

    Retains at most ``BROKER_MAX_EVENTS`` non-terminal records or
    ``BROKER_MAX_BYTES`` of payload, whichever comes first, silently evicting the
    oldest non-terminal records. No synthetic ``overflow`` event is ever emitted
    and the broker never creates a sequence: every ``seq``/``id`` is a real sink
    sequence. Live stream gaps are normal (uniqueness, not contiguity, is the
    contract). The terminal record is never evicted and is always last in the
    snapshot. Eviction never blocks the writer.

    Live delivery is push-based: ``publish`` appends the record to each
    subscriber's asyncio queue through ``loop.call_soon_threadsafe`` so writer
    threads never touch the loop directly. A terminal record also closes the
    broker and schedules a ``None`` sentinel on every subscriber queue so
    consumers drain the terminal record and then finish.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._lock = threading.Lock()
        self._records: deque[WalkLogRecord] = deque()
        self._bytes = 0
        self._terminal: WalkLogRecord | None = None
        self._closed = False
        self._subscribers: set[WalkLogSubscription] = set()

    def snapshot(self, after_seq: int = -1) -> tuple[WalkLogRecord, ...]:
        """Return the retained records with ``seq > after_seq``, in order:
        retained non-terminal records, then the terminal record (if any).
        Caller must hold ``_lock``."""
        out = [r for r in self._records if r.seq > after_seq]
        if self._terminal is not None and self._terminal.seq > after_seq:
            out.append(self._terminal)
        return tuple(out)

    def publish(self, record: WalkLogRecord) -> None:
        """Publish one flushed record to live subscribers.

        Called by the sink only after a successful flushed write. Never blocks
        the writer: loop callbacks are scheduled with ``call_soon_threadsafe``
        after the broker lock is released. Eviction (when the caps are exceeded)
        drops the oldest non-terminal record silently -- no synthetic event and
        no broker-created sequence.

        Publish-after-close window (benign by design): a flushed write may
        succeed while a concurrent ``publish`` sees the broker already closed
        (``self._closed``), dropping that record's live delivery. This is
        recoverable because the authoritative JSONL file replay (for both active
        and completed runs) reconstructs the record, and live-sequence gaps are
        the documented norm (uniqueness, not contiguity, is the contract).
        """
        with self._lock:
            if self._closed:
                return
            terminal = record.terminal
            if terminal:
                self._terminal = record
                self._closed = True
            else:
                self._records.append(record)
                self._bytes += _record_bytes(record)
                while (
                    len(self._records) > BROKER_MAX_EVENTS
                    or self._bytes > BROKER_MAX_BYTES
                ) and self._records:
                    old = self._records.popleft()
                    self._bytes -= _record_bytes(old)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            loop = sub._loop
            if loop is not None:
                loop.call_soon_threadsafe(sub._push, record)
        if terminal:
            for sub in subscribers:
                loop = sub._loop
                if loop is not None:
                    loop.call_soon_threadsafe(sub._close_queue, False)

    def unsubscribe(self, sub: WalkLogSubscription) -> None:
        """Remove a subscriber; safe to call concurrently with publish."""
        with self._lock:
            self._subscribers.discard(sub)

    def close_for_shutdown(self) -> None:
        """Release every subscriber on service shutdown: discard buffered live
        records and schedule a ``None`` sentinel so ``next_event`` returns
        immediately instead of draining stale records."""
        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for sub in subscribers:
            loop = sub._loop
            if loop is not None:
                loop.call_soon_threadsafe(sub._close_queue, True)


# ---------------------------------------------------------------------------
# Subscription (replay snapshot + loop-safe live broker delivery)
# ---------------------------------------------------------------------------


class WalkLogSubscription:
    """Immutable replay snapshot plus loop-safe live delivery from the broker.

    ``next_event`` blocks on a per-subscriber asyncio queue that is fed only
    through ``loop.call_soon_threadsafe`` from synchronous writer threads -- no
    timers, file polling, or SQLite polling. ``__aiter__`` yields the atomic
    replay snapshot first, then live records until the run closes (``None``).
    """

    def __init__(
        self,
        run_id: str,
        replay: tuple[WalkLogRecord, ...],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._run_id = run_id
        self._replay: tuple[WalkLogRecord, ...] = tuple(replay)
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._broker: _RunBroker | None = None
        self._closed = False
        self._done = False

    @property
    def replay(self) -> tuple[WalkLogRecord, ...]:
        """Immutable atomic replay snapshot captured at subscription creation."""
        return self._replay

    def _set_broker(self, broker: _RunBroker) -> None:
        self._broker = broker

    def _push(self, record: WalkLogRecord) -> None:
        """Enqueue a live record; no-op once the subscriber is closed. Must be
        called on the subscriber's loop (via ``call_soon_threadsafe``)."""
        if self._closed:
            return
        self._queue.put_nowait(record)

    def _close_queue(self, discard: bool = False) -> None:
        """Terminate the stream. ``discard`` drops any buffered live records so
        the next ``next_event`` returns ``None`` immediately (shutdown);
        otherwise buffered records are drained first, then ``None`` (terminal
        completion). Must be called on the subscriber's loop."""
        self._closed = True
        if discard:
            try:
                while True:
                    self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(None)

    def close(self) -> None:
        """Remove the subscriber; idempotent and non-blocking."""
        self._closed = True
        broker = self._broker
        if broker is not None:
            broker.unsubscribe(self)

    async def next_event(self) -> WalkLogRecord | None:
        """Await the next live record, or None once closed/terminal."""
        if self._done:
            return None
        rec = await self._queue.get()
        if rec is None:
            self._done = True
            return None
        return rec

    def __aiter__(self) -> AsyncIterator[WalkLogRecord]:
        return self._agen()

    async def _agen(self) -> AsyncIterator[WalkLogRecord]:
        for rec in self.replay:
            yield rec
        while True:
            ev = await self.next_event()
            if ev is None:
                return
            yield ev


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WalkLogService:
    """Process-owned per-walk log service: sink ownership, replay, lifecycle."""

    def __init__(self, root_dir: str = "/tmp/alexandria-walks") -> None:
        self._root_dir = Path(root_dir)
        self._lock = threading.Lock()
        self._sinks: dict[str, WalkLogSink] = {}
        #: Per-run live brokers keyed by run id, mirroring the active sinks.
        self._brokers: dict[str, _RunBroker] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Create the root directory (0700) and remove stale UUID-named logs."""
        with self._lock:
            self._ensure_root_dir()
            self._cleanup_old_logs()

    def shutdown(self) -> None:
        """Close every open sink with a partial/aborted terminal marker and
        release all brokers/subscribers (idempotent)."""
        with self._lock:
            sinks = list(self._sinks.values())
            brokers = list(self._brokers.values())
            self._sinks.clear()
            self._brokers.clear()
        for sink in sinks:
            sink.close_partial("aborted")
            sink.close()
        for broker in brokers:
            broker.close_for_shutdown()

    def _ensure_root_dir(self) -> None:
        os.makedirs(self._root_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self._root_dir, 0o700)
        except OSError:
            logger.warning("could not chmod root dir; path=%s", self._root_dir)

    def _cleanup_old_logs(self) -> None:
        """Remove only UUID-named ``*.log`` files older than 24 hours."""
        now_ms = _now_ms()
        try:
            for entry in os.scandir(self._root_dir):
                if not entry.is_file():
                    continue
                if not entry.name.endswith(".log"):
                    continue
                stem = entry.name[: -len(".log")]
                if not _is_valid_uuid(stem):
                    continue
                try:
                    mtime_s = entry.stat().st_mtime
                    age_s = now_ms / 1000.0 - mtime_s
                    if age_s > _STALE_LOG_SECONDS:
                        os.unlink(entry.path)
                except OSError:
                    continue
        except OSError:
            logger.warning("startup cleanup failed; root=%s", self._root_dir)

    # -- run management ---------------------------------------------------

    def open_run(
        self,
        run_id: str,
        book_id: str,
        walk_name: str,
        started_ms: int | None = None,
    ) -> WalkLogSink:
        """Validate the UUID, create the file/header, and register the sink.

        Raises ``ValueError`` for a non-canonical UUID or a run ID that is
        already active (duplicate run).
        """
        if not _is_valid_uuid(run_id):
            raise ValueError(f"invalid run id (must be a canonical UUID): {run_id!r}")
        with self._lock:
            if run_id in self._sinks:
                raise ValueError(f"run already active: {run_id}")
            started = _now_ms() if started_ms is None else started_ms
            broker = _RunBroker(run_id)
            sink = WalkLogSink(
                self._root_dir, run_id, book_id, walk_name, int(started)
            )
            sink._broker = broker
            sink._open()
            self._sinks[run_id] = sink
            self._brokers[run_id] = broker
            return sink

    def get_run(self, run_id: str) -> WalkLogSink | None:
        """Return the active sink without creating one (read-only)."""
        with self._lock:
            return self._sinks.get(run_id)

    def close_run(
        self,
        run_id: str,
        status: Literal["completed", "failed", "cancelled", "interrupted"],
        payload: Mapping[str, Any] | None = None,
    ) -> WalkLogRecord | None:
        """Append the terminal record, deregister the run, then close the sink.

        ``append_terminal`` runs FIRST, while the run is still registered in
        ``_sinks``/``_brokers``, so the terminal is written+flushed and published
        to the broker before the run is removed from active state. This matches
        the CONTRACTS ordering ('appends the final terminal record, publishes it
        before closing the broker, closes the sink, and removes the run from
        active state') and closes the terminal-loss window on the file-only
        ``open_subscription`` path: a subscriber can never observe ``broker is
        None`` before the terminal is flushed -- on the broker-backed path the
        closed-branch bridge, and here the already-flushed file tail, guarantee
        the terminal is delivered before ``None``.

        After this returns, ``get_run(run_id)`` is None and the file ends with
        the terminal record.
        """
        with self._lock:
            sink = self._sinks.get(run_id)
        if sink is None:
            return None
        rec = sink.append_terminal(status, payload)
        with self._lock:
            self._sinks.pop(run_id, None)
            self._brokers.pop(run_id, None)
        sink.close()
        return rec

    def open_subscription(
        self,
        run_id: str,
        after_seq: int = -1,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> WalkLogSubscription:
        """Return a subscription whose replay comes from the authoritative file.

        A non-canonical UUID raises ``KeyError`` before any path is derived;
        unknown in-process runs or absent files also raise ``KeyError``. The
        authoritative JSONL file is replayed for **both** active and completed
        runs (the existing ``replay()`` logic: complete records only, partial
        trailing lines ignored, filtered by ``after_seq``). The file tail (max
        seq) is captured from the same read as the replay. The live subscriber
        is then registered under the broker lock, bridging only broker records
        with ``seq > max(file_tail, after_seq)`` that were published in the
        window between the file read and registration -- so no record is lost
        or double-delivered across the registration boundary. A run whose
        broker is already terminal returns the file snapshot, bridges any broker
        records absent from it (so the terminal is never lost in the as-yet-unread
        window), and an immediately-closed stream. A file-only (completed,
        non-active) run replays the file and finishes immediately.
        """
        # Validate the canonical UUID BEFORE resolving the loop so a non-canonical
        # run id always raises KeyError deterministically, even when called with
        # ``loop=None`` from a non-async context (asyncio.get_running_loop would
        # otherwise raise RuntimeError first).
        if not _is_valid_uuid(run_id):
            raise KeyError(run_id)
        resolved_loop = loop if loop is not None else asyncio.get_running_loop()
        with self._lock:
            broker = self._brokers.get(run_id)
        path = self._root_dir / f"{run_id}.log"
        if not path.exists():
            raise KeyError(run_id)
        # Single read: replay snapshot + file tail captured atomically so the
        # bridge cutoff is consistent with what the snapshot already covers.
        replay, file_tail, tail_terminal = self._replay_and_tail(run_id, after_seq)
        sub = WalkLogSubscription(run_id, replay, resolved_loop)
        if broker is None:
            # File-only (completed) run: replay the file, then finish immediately
            # -- no live broker.
            sub._closed = True
            sub._queue.put_nowait(None)
            return sub
        with broker._lock:
            if broker._closed or tail_terminal:
                # Already terminal: bridge any broker records still absent from the
                # file snapshot, then close. The terminal-miss race: a subscriber
                # attaching exactly as the run completes may read the file BEFORE
                # append_terminal writes the terminal record, then observe the
                # broker already closed here -- so the file replay would lack the
                # most important record (the terminal, carrying final status).
                # Bridging ``snapshot(after_seq=max(file_tail, after_seq))`` before
                # closing yields the terminal (and any stragglers) with no loss and
                # no duplicates: when ``tail_terminal`` is set the terminal is the
                # file tail and ``seq > file_tail`` yields nothing extra; when the
                # read predated the terminal write, the snapshot yields it.
                bridge_cutoff = max(file_tail, after_seq)
                for rec in broker.snapshot(after_seq=bridge_cutoff):
                    sub._queue.put_nowait(rec)
                sub._closed = True
                sub._queue.put_nowait(None)
            else:
                # Active live broker: bridge ONLY records after the file tail that
                # were published between the file read and this registration (they
                # are retained in the broker). No duplicates: bridged records have
                # ``seq > file_tail`` while the file replay covers ``<= file_tail``.
                bridge_cutoff = max(file_tail, after_seq)
                for rec in broker.snapshot(after_seq=bridge_cutoff):
                    sub._queue.put_nowait(rec)
                sub._set_broker(broker)
                broker._subscribers.add(sub)
        return sub

    # -- replay -----------------------------------------------------------

    def replay(self, run_id: str, after_seq: int = -1) -> tuple[WalkLogRecord, ...]:
        """Read complete JSONL records with ``seq > after_seq`` from the file.

        Never follows a path derived from unvalidated input: a non-UUID run id
        yields an empty tuple. Partial trailing lines and records whose run_id
        does not match are logged and skipped.
        """
        records, _tail, _terminal = self._replay_and_tail(run_id, after_seq)
        return records

    def _replay_and_tail(
        self, run_id: str, after_seq: int = -1
    ) -> tuple[tuple[WalkLogRecord, ...], int, bool]:
        """Single read of the run file returning ``(records, tail_seq,
        tail_terminal)``.

        ``records`` are the complete JSONL records with ``seq > after_seq``
        (partial trailing lines ignored/logged; foreign-run and non-dict lines
        skipped). ``tail_seq`` is the maximum ``seq`` in the file (``-1`` when
        empty) and ``tail_terminal`` is whether that maximum-seq record is the
        terminal record -- both captured from the same read as ``records`` so an
        ``open_subscription`` bridge cutoff is consistent with its snapshot.
        """
        if not _is_valid_uuid(run_id):
            return (), -1, False
        path = self._root_dir / f"{run_id}.log"
        if not path.exists():
            return (), -1, False

        parsed: list[tuple[int, dict[str, Any]]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        logger.warning(
                            "ignoring partial log line; run_id=%s", run_id
                        )
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("run_id") != run_id:
                        logger.warning(
                            "skipping record with foreign run_id; run_id=%s", run_id
                        )
                        continue
                    seq = obj.get("seq")
                    if not isinstance(seq, int) or seq < 0:
                        continue
                    parsed.append((seq, obj))
        except OSError:
            logger.warning("replay read failed; run_id=%s", run_id)
            return (), -1, False

        records: list[WalkLogRecord] = []
        tail_seq = -1
        tail_terminal = False
        for seq, obj in sorted(parsed, key=lambda pair: pair[0]):
            if seq > tail_seq:
                tail_seq = seq
                tail_terminal = bool(obj.get("terminal"))
            if seq <= after_seq:
                continue
            # Extract once so the isinstance narrowing correlates with the value
            # assigned (avoids the pyright 'Type Any | dict[str, Any] | None'
            # artifact from calling ``.get`` twice in the same expression).
            data = obj.get("data")
            records.append(
                WalkLogRecord(
                    run_id=run_id,
                    seq=seq,
                    id=str(obj.get("id")) or f"{run_id}:{seq}",
                    event=str(obj.get("event", "")),
                    data=data if isinstance(data, Mapping) else {},
                    terminal=bool(obj.get("terminal")),
                )
            )
        return tuple(records), tail_seq, tail_terminal
