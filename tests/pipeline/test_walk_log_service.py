"""Implemented-spec (GREEN) tests for the Part A walk-log service.

These tests are written against ``artifacts/designs/parts/per-walk-log-streaming/
CONTRACTS.md`` and ``artifacts/designs/pending/DD-per-walk-log-streaming.md`` and
exercise the implemented module ``app.pipeline.walks.log_service``. The module is
fully implemented (~1000 lines); every test in this file is green.

Clock seam: the design requires startup cleanup of log files older than 24h and
a header start timestamp, so a controllable module-level time source
``log_service._now_ms() -> int (epoch milliseconds)`` is monkeypatched via the
``set_now`` fixture.

Async note: this repository does not use pytest-asyncio; the established pattern
(see ``tests/pipeline/test_api.py``) is to drive a coroutine with ``asyncio.run``
inside a plain sync test function. That pattern is used for the broker and
loop-safe-wakeup tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

# The implemented service module is imported here; it exists and these tests
# run green against it.
from app.pipeline.walks import log_service
from app.pipeline.walks.log_service import (
    WalkLogRecord,
    WalkLogService,
    WalkLogSink,
    WalkLogSubscription,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

# The nine walk modules must always remain byte-identical to git HEAD (zero
# edits, zero imports as implementation targets). ``api_walks`` was protected
# while Part C had not yet modified it; since this Phase 2 (Part C SSE endpoint)
# legitimately adds the SSE route to app/pipeline/api_walks.py, it is no longer
# byte-immutable and was removed from the protected set. Enforced by the
# static audit tests at the bottom of P1-S1.
PROTECTED_MODULES = [
    "walk_2a_scene_segmentation",
    "walk_2b_character_discovery",
    "walk_2c_alias_resolution",
    "walk_2d_scene_presence",
    "walk_2e_span_attribution",
    "walk_2f_character_description",
    "walk_2g_voice_audition",
    "walk_2h_voice_assignment",
    "walk_2i_delivery",
]

VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"
TRUNCATION_MARKER = "[truncated]"
SINK_OVERFLOW_EVENT = "overflow"
SINK_CAP_BYTES = 10 * 1024 * 1024  # 10 MiB
SINK_TERMINAL_RESERVE_BYTES = 64 * 1024  # 64 KiB
BROKER_MAX_EVENTS = 256
BROKER_MAX_BYTES = 1024 * 1024  # 1 MiB
EVENT_MAX_BYTES = 128 * 1024  # 128 KiB
PROMPT_MAX_BYTES = 64 * 1024  # 64 KiB
TRACEBACK_MAX_BYTES = 32 * 1024  # 32 KiB
DAY_MS = 24 * 60 * 60 * 1000  # 24h in milliseconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    """Return every non-empty line of the JSONL log file, trimmed."""
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def _read_header(path: Path) -> dict:
    """Return the header (first JSONL line) parsed as a dict."""
    return json.loads(_read_lines(path)[0])


def _read_records(path: Path) -> list[dict]:
    """Return every real record (a JSONL line carrying a ``seq``) as a dict.

    The header line (no ``seq``) is excluded so record-oriented tests can
    iterate over actual logged events.
    """
    out = []
    for ln in _read_lines(path):
        obj = json.loads(ln)
        if "seq" in obj:
            out.append(obj)
    return out


def _started_service(tmp_path, set_now=None, root_name="alexandria-walks"):
    """Return a started WalkLogService rooted under tmp_path."""
    service = WalkLogService(root_dir=str(tmp_path / root_name))
    service.start()
    return service


def _serialized_len(rec: WalkLogRecord) -> int:
    """Return the encoded byte length of a record as the service serializes it
    (compact-ish JSONL with ``", "``/``": "`` separators)."""
    d = {
        "run_id": rec.run_id,
        "seq": rec.seq,
        "id": rec.id,
        "event": rec.event,
        "data": rec.data,
        "terminal": rec.terminal,
    }
    return len(
        json.dumps(d, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8")
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def set_now(monkeypatch):
    """Controllable clock: monkeypatches ``log_service._now_ms``.

    Returns a callable ``set_now(ms: int)`` that sets the value the service's
    time source will report next. ``log_service._now_ms()`` must return epoch
    milliseconds."""
    state = {"ms": 1_752_000_000_000}

    def _set(ms):
        state["ms"] = int(ms)

    monkeypatch.setattr(log_service, "_now_ms", lambda: state["ms"])
    return _set


@pytest.fixture()
def svc(tmp_path):
    """A started service with its root directory under tmp_path."""
    return _started_service(tmp_path)


# ---------------------------------------------------------------------------
# P1-S1 — Public DTO / service / sink / subscription signatures, paths,
#         header, permissions, UTF-8 JSONL, sequence/ID shape, static audit
# ---------------------------------------------------------------------------


class TestPublicSignatures:
    """The public DTO/service/sink/subscription signatures from CONTRACTS.md."""

    def test_service_constructor_default_root_dir(self):
        """WalkLogService defaults root_dir to /tmp/alexandria-walks."""
        sig = inspect.signature(WalkLogService.__init__)
        assert sig.parameters["root_dir"].default == "/tmp/alexandria-walks"

    def test_service_exposes_public_methods(self):
        """The service surface matches CONTRACTS.md exactly."""
        for name in (
            "start",
            "shutdown",
            "open_run",
            "get_run",
            "close_run",
            "replay",
            "open_subscription",
        ):
            assert callable(getattr(WalkLogService, name)), f"missing {name}()"

    def test_open_run_signature(self):
        """open_run(run_id, book_id, walk_name, started_ms=None)."""
        params = inspect.signature(WalkLogService.open_run).parameters
        assert list(params)[:4] == ["self", "run_id", "book_id", "walk_name"]
        assert params["started_ms"].default is None

    def test_sink_surface(self):
        """The sink exposes append / append_terminal / close_partial."""
        for name in ("append", "append_terminal", "close_partial"):
            assert callable(getattr(WalkLogSink, name)), f"missing sink.{name}()"

    def test_subscription_surface(self):
        """The subscription exposes replay, next_event, close, __aiter__."""
        for name in ("next_event", "close", "__aiter__"):
            assert callable(getattr(WalkLogSubscription, name)), f"missing {name}()"
        # replay is a property/attribute returning a tuple, not a method call.
        assert "replay" in WalkLogSubscription.__dict__ or hasattr(
            WalkLogSubscription, "replay"
        )

    def test_walk_log_record_is_frozen_dto(self):
        """WalkLogRecord is an immutable DTO with the contract fields."""
        assert dataclasses.is_dataclass(WalkLogRecord)
        assert WalkLogRecord.__dataclass_params__.frozen is True
        fields = {f.name for f in dataclasses.fields(WalkLogRecord)}
        assert {"run_id", "seq", "id", "event", "data", "terminal"} <= fields


class TestSinkBasics:
    """Sink path shape, header metadata, permissions, UTF-8 JSONL, seq/IDs."""

    def test_uuid_derived_path_shape(self, tmp_path):
        """The log lives at {root}/{run_id}.log and get_run returns the sink."""
        root = tmp_path / "alexandria-walks"
        service = WalkLogService(root_dir=str(root))
        service.start()
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        path = root / f"{VALID_UUID}.log"
        assert path.exists()
        assert service.get_run(VALID_UUID) is sink
        service.shutdown()

    def test_header_metadata(self, tmp_path, set_now):
        """Header records book_id, walk_name, run_id, and start time."""
        set_now(1_752_000_000_000)
        service = _started_service(tmp_path)
        service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        header = _read_header(tmp_path / "alexandria-walks" / f"{VALID_UUID}.log")
        assert header["book_id"] == "book-7"
        assert header["walk_name"] == "walk_2a_scene_segmentation"
        assert header["run_id"] == VALID_UUID
        assert header["started_ms"] == 1_752_000_000_000
        service.shutdown()

    def test_directory_created_0700(self, tmp_path):
        """The service creates its root directory with mode 0700."""
        root = tmp_path / "alexandria-walks"
        assert not root.exists()
        service = WalkLogService(root_dir=str(root))
        service.start()
        mode = stat.S_IMODE(os.stat(root).st_mode)
        assert mode == 0o700
        service.shutdown()

    def test_file_created_0600(self, tmp_path):
        """The log file is written with mode 0600 (owner-only)."""
        service = _started_service(tmp_path)
        service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        service.shutdown()

    def test_utf8_jsonl_output(self, tmp_path):
        """Output is UTF-8 JSONL: every non-empty line parses as JSON."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("llm", {"prompts": "café ☕", "response": "日本語 テキスト"})
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        for ln in _read_lines(path):
            obj = json.loads(ln)  # must not raise
            assert isinstance(obj, dict)
        service.shutdown()

    def test_monotonic_seq_and_opaque_ids(self, tmp_path):
        """seq increments monotonically; id is the opaque {run_id}:{seq}."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        recs = [sink.append("work", {"i": i}) for i in range(5)]
        assert [r.seq for r in recs] == [0, 1, 2, 3, 4]
        assert all(r.id == f"{VALID_UUID}:{r.seq}" for r in recs)
        assert all(r.run_id == VALID_UUID for r in recs)
        assert all(r.terminal is False for r in recs)
        service.shutdown()

    def test_append_record_dto(self, tmp_path):
        """append returns a WalkLogRecord with normalized event/data."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        rec = sink.append("llm", {"prompts": "hi", "response": "yo", "x": None})
        assert isinstance(rec, WalkLogRecord)
        assert rec.seq == 0
        assert rec.event == "llm"
        assert rec.data == {"prompts": "hi", "response": "yo", "x": None}
        service.shutdown()


class TestStaticAudit:
    """The nine walk modules must remain byte-identical to git HEAD.

    Only the immutable ``walk_2*.py`` modules are protected now: runner/
    _llm_helpers were removed because the B-runner-integration plan legitimately
    modified them, and api_walks was removed when Part C (this Phase 2) added
    the SSE endpoint to it. The protected files must stay byte-identical to the
    committed git HEAD versions, and this test file must never import them as
    implementation targets.
    """

    def test_protected_modules_byte_identical_to_git_head(self):
        """git show HEAD:<path> must match the on-disk bytes for every
        protected module."""
        for name in PROTECTED_MODULES:
            # The walk modules live under app/pipeline/walks/; api_walks.py lives
            # in the parent app/pipeline/ package.
            subdir = "" if name == "api_walks" else "walks/"
            rel = f"app/pipeline/{subdir}{name}.py"
            proc = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                capture_output=True,
                cwd=str(REPO_ROOT),
                check=True,
            )
            head_bytes = proc.stdout
            disk_bytes = (REPO_ROOT / rel).read_bytes()
            assert disk_bytes == head_bytes, f"{name}.py was modified vs git HEAD"

    def test_no_import_of_protected_modules(self):
        """This test file must not import any protected module as a target."""
        src = Path(__file__).read_text(encoding="utf-8")
        imports = re.findall(
            r"(?:from app\.pipeline\.walks import |from app\.pipeline\.walks\.)(\w+)",
            src,
        )
        for name in PROTECTED_MODULES:
            assert name not in imports, f"test file imports protected module {name}"


# ---------------------------------------------------------------------------
# P1-S2 — Redaction, truncation, whole-event bounding, null metadata,
#         non-ASCII JSON serialization
# ---------------------------------------------------------------------------


class TestRedaction:
    """Prompts/responses must never echo secrets in the written log."""

    def test_redacts_sk_key(self, tmp_path):
        """An sk-... API key inside a prompt must not appear in output."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        secret = "sk-proj-abcdef1234567890wxyz"
        sink.append("llm", {"prompts": f"use the key {secret} now", "response": "ok"})
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content
        service.shutdown()

    def test_redacts_bearer_token(self, tmp_path):
        """A bearer token inside a prompt must not appear in output."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefgh"
        sink.append("llm", {"prompts": f"Authorization: Bearer {token}", "response": "ok"})
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in content
        assert "abcdefgh" not in content
        service.shutdown()

    def test_redacts_plain_secret_all_assignment_spellings(self, tmp_path):
        """A plain (non-``sk-``) alphanumeric secret is redacted across all three
        free-text assignment spellings -- ``api_key=``, ``api-key=``, and
        ``api key = `` -- at any depth. The marker replaces each assignment."""
        secret = "hunter2secretX42"
        service = _started_service(tmp_path)
        run_id = str(uuid.uuid4())
        sink = service.open_run(run_id, "book-7", "walk_2a_scene_segmentation")
        payload = {
            "prompts": "p",
            "response": "r",
            "nested": {
                "config": {
                    "a": f"token api_key={secret} end",
                    "b": f"token api-key={secret} end",
                    "c": f"token api key = {secret} end",
                }
            },
        }
        sink.append("llm", payload)
        content = (tmp_path / "alexandria-walks" / f"{run_id}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content
        assert content.count(log_service.REDACTED) >= 3
        assert "api_key=" + secret not in content
        assert "api-key=" + secret not in content
        assert "api key = " + secret not in content
        service.shutdown()

    def test_redacts_api_key_assignment(self, tmp_path):
        """An api_key= assignment in a response must not appear in output."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        secret = "sk-abc-xyz-secret-0001"
        sink.append("llm", {"prompts": "call api", "response": f'api_key = "{secret}"'})
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content
        service.shutdown()

    def test_redacts_space_form_api_key_dict_value(self, tmp_path):
        """A plain secret stored as the value of a space-form ``api key`` dict
        key is redacted wholesale at any depth (parity with the underscore and
        hyphen forms already covered)."""
        service = _started_service(tmp_path)
        run_id = str(uuid.uuid4())
        secret = "somePlainSecret123"
        sink = service.open_run(run_id, "book-7", "walk_2a_scene_segmentation")
        payload = {
            "prompts": "p",
            "response": "r",
            "config": {"api key": secret},
        }
        sink.append("llm", payload)
        content = (tmp_path / "alexandria-walks" / f"{run_id}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content  # persisted JSONL never echoes the secret
        assert log_service.REDACTED in content  # the marker appears in its place
        service.shutdown()


class TestTruncationAndBounding:
    """Prompts/responses 64 KiB, tracebacks 32 KiB, whole events 128 KiB."""

    def _llm_line(self, path: Path, event="llm") -> dict:
        for obj in _read_records(path):
            if obj["event"] == event:
                return obj["data"]
        raise AssertionError(f"no {event} record found")

    def test_prompt_truncated_64kib_with_marker(self, tmp_path):
        """A >64 KiB prompt is truncated with an explicit bounded marker."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        big = "p" * 200_000
        sink.append("llm", {"prompts": big, "response": "ok"})
        data = self._llm_line(tmp_path / "alexandria-walks" / f"{VALID_UUID}.log")
        encoded = data["prompts"].encode("utf-8")
        assert len(encoded) <= PROMPT_MAX_BYTES
        assert data["prompts"] != big  # actually truncated
        assert TRUNCATION_MARKER in data["prompts"]
        service.shutdown()

    def test_response_truncated_64kib_with_marker(self, tmp_path):
        """A >64 KiB response is truncated with an explicit bounded marker."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        big = "r" * 200_000
        sink.append("llm", {"prompts": "ok", "response": big})
        data = self._llm_line(tmp_path / "alexandria-walks" / f"{VALID_UUID}.log")
        encoded = data["response"].encode("utf-8")
        assert len(encoded) <= PROMPT_MAX_BYTES
        assert data["response"] != big
        assert TRUNCATION_MARKER in data["response"]
        service.shutdown()

    def test_traceback_truncated_32kib(self, tmp_path):
        """A >32 KiB traceback is truncated to 32 KiB."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        big = "t" * 100_000
        sink.append("traceback", {"text": big})
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        data = self._llm_line(path, event="traceback")
        encoded = data["text"].encode("utf-8")
        assert len(encoded) <= TRACEBACK_MAX_BYTES
        assert data["text"] != big
        assert TRUNCATION_MARKER in data["text"]
        service.shutdown()

    def test_whole_event_bounded_128kib(self, tmp_path):
        """A single serialized event line (incl. JSON framing) is <= 128 KiB."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("llm", {"prompts": "p" * 100_000, "response": "r" * 100_000})
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        for ln in _read_lines(path):
            assert len(ln.encode("utf-8")) <= EVENT_MAX_BYTES
        service.shutdown()

    def test_oversized_top_level_event_with_bounded_data_terminates(self, tmp_path):
        """An oversized top-level ``event`` combined with bounded-field data must
        complete in finite time (never hang) and produce a bounded record (at
        most ``EVENT_MAX_BYTES``) or ``None``. Covers the ``prompts=None`` case,
        the oversized-string ``prompts`` case, and the 'data strings at
        [truncated] floor' case (both ``prompts`` and ``response`` oversized)."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        big_event = "x" * 200_000
        scenarios = [
            {"prompts": None},  # no string inside data to shrink
            {"prompts": "p" * 200_000},  # oversized bounded string
            {"prompts": "p" * 200_000, "response": "r" * 200_000},  # floor case
        ]
        for payload in scenarios:
            rec = sink.append(big_event, payload)  # must return (no hang)
            assert rec is None or _serialized_len(rec) <= log_service.EVENT_MAX_BYTES
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        for ln in _read_lines(path):
            assert len(ln.encode("utf-8")) <= log_service.EVENT_MAX_BYTES
        service.shutdown()

    def test_shrink_budget_exhaustion_emits_truncated_marker(self, tmp_path):
        """When an oversized event plus many mid-size strings cannot be shrunk
        below the 128 KiB cap within the finite shrink budget (10 passes), the
        record is replaced by a minimal bounded form whose ``data`` is exactly
        ``{"truncated": True}``. The call terminates (no hang) and the persisted
        serialized line is bounded."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        big_event = "x" * 300_000
        # Hundreds of ~500-char strings keep the total far above the 128 KiB cap
        # even after 10 halving passes, forcing the budget-exhaustion fallback.
        blobs = [f"i={i};" + "z" * 500 for i in range(400)]
        rec = sink.append(big_event, {"prompts": "ok", "blobs": blobs})
        assert rec is not None  # must return (no hang)
        assert rec.data == {"truncated": True}  # minimal bounded fallback
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        records = _read_records(path)
        assert any(r["data"] == {"truncated": True} for r in records)
        for ln in _read_lines(path):
            assert len(ln.encode("utf-8")) <= log_service.EVENT_MAX_BYTES
        service.shutdown()

    def test_null_metadata_preserved(self, tmp_path):
        """SDK-omitted metadata stays null (never omitted or coerced)."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append(
            "llm",
            {
                "prompts": "p",
                "response": "r",
                "model": None,
                "usage": None,
                "finish_reason": None,
                "reasoning_effort": None,
            },
        )
        data = self._llm_line(tmp_path / "alexandria-walks" / f"{VALID_UUID}.log")
        assert data["model"] is None
        assert data["usage"] is None
        assert data["finish_reason"] is None
        assert data["reasoning_effort"] is None
        service.shutdown()

    def test_non_ascii_json_serialization(self, tmp_path):
        """Non-ASCII payloads survive UTF-8 JSON serialization intact."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("llm", {"prompts": "café ☕", "response": "日本語 テキスト ✓"})
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert "café ☕" in content
        assert "日本語 テキスト ✓" in content
        service.shutdown()


# ---------------------------------------------------------------------------
# P1-S3 — Sink boundaries: projected byte size, 10 MiB cap, 64 KiB terminal
#         reserve, drops, overflow marker, flush, terminal-write failure
# ---------------------------------------------------------------------------


class TestSinkBoundaries:
    """Strict encoded-byte limits and drop/marker/terminal guarantee."""

    def test_10MiB_cap_drop_reserve_terminal_and_single_overflow(self, tmp_path):
        """Ordinary records beyond the 10 MiB cap are dropped (None) without
        consuming the 64 KiB terminal reserve; exactly one bounded overflow
        marker is emitted; the terminal record still lands in the reserve."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        blob = "y" * 1_000_000  # ~1 MiB payload per record
        accepted: list[int] = []
        for i in range(20):
            rec = sink.append("work", {"chunk": i, "blob": blob})
            if rec is not None:
                accepted.append(i)
        # Some trailing ordinary records must have been dropped.
        assert len(accepted) < 20
        assert 0 in accepted
        # The terminal record must still succeed inside the reserved space.
        terminal = sink.append_terminal("completed", {"ok": True})
        assert terminal is not None
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        records = _read_records(path)
        # Projected-size check: every accepted chunk is in the file, and no
        # dropped chunk leaked in.
        chunks_in_file = {
            r["data"]["chunk"] for r in records if r["event"] == "work"
        }
        assert chunks_in_file == set(accepted)
        # Strict cap: total file bytes stay within 10 MiB.
        assert path.stat().st_size <= SINK_CAP_BYTES
        # Exactly one bounded overflow marker.
        overflow = [r for r in records if r["event"] == SINK_OVERFLOW_EVENT]
        assert len(overflow) == 1
        # The terminal record is present and is the last real record.
        assert any(r.get("terminal") is True for r in records)
        assert records[-1].get("terminal") is True
        service.shutdown()

    def test_ordinary_record_drop_returns_none(self, tmp_path):
        """An ordinary record that would consume the terminal reserve returns
        None and is not written."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        blob = "z" * 1_000_000
        # Fill until the cap/reserve is reached.
        dropped = None
        for i in range(30):
            rec = sink.append("work", {"chunk": i, "blob": blob})
            if rec is None:
                dropped = i
                break
        assert dropped is not None
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        chunks = {r["data"]["chunk"] for r in _read_records(path) if r["event"] == "work"}
        assert dropped not in chunks
        service.shutdown()

    def test_flush_after_every_successful_record(self, tmp_path):
        """After each successful append the record is visible to an
        independent fresh read (i.e. it was flushed, not held in a buffer)."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        for i in range(3):
            sink.append("work", {"i": i})
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            assert f'"i": {i}' in content
        service.shutdown()

    def test_terminal_write_failure_preserves_callable_close_path(
        self, tmp_path, monkeypatch
    ):
        """A filesystem failure during the terminal write must not raise into
        callers and must leave the close path callable."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})

        # Force the sink's terminal write flush to fail by patching the sink
        # instance's own ``_flush`` seam. ``_flush`` is called inside
        # ``WalkLogSink._write``'s try/except OSError, so a raising ``_flush``
        # is logged and swallowed — the sink never raises into callers. This
        # avoids patching ``io.BufferedWriter.flush``, which is immutable
        # (unpatchable) on Python 3.13.
        def _raising_flush():
            raise OSError("simulated disk full")

        monkeypatch.setattr(sink, "_flush", _raising_flush)

        # append_terminal / close_run / shutdown must not raise into callers.
        sink.append_terminal("completed", {"ok": True})  # no raise
        service.close_run(VALID_UUID, "completed")  # no raise
        service.shutdown()  # no raise

    def test_no_non_terminal_record_lands_after_terminal(self, tmp_path):
        """append_terminal makes the terminal write and the closed transition
        atomic under one lock acquisition (finding-3 regression): concurrent
        writer appends either land strictly before the terminal record or are
        dropped -- never after it, so no non-terminal record outranks (has a
        higher seq than) the terminal record in the file."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})

        stop = threading.Event()

        def writer():
            i = 1
            while not stop.is_set():
                sink.append("work", {"i": i})
                i += 1

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()
        terminal = sink.append_terminal("completed", {"ok": True})
        stop.set()
        for t in threads:
            t.join()

        assert terminal is not None
        assert sink._closed is True
        path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
        records = _read_records(path)
        terminal_seqs = [r["seq"] for r in records if r.get("terminal")]
        assert len(terminal_seqs) == 1
        terminal_seq = terminal_seqs[0]
        # No non-terminal record may have a larger seq than the terminal record
        # (i.e. nothing may land after it in the retained file).
        after = [r for r in records if not r.get("terminal") and r["seq"] > terminal_seq]
        assert after == []

    def test_append_terminal_marks_closed_even_on_failed_write(self, tmp_path, monkeypatch):
        """A failed terminal write still closes the sink (best-effort no-raise):
        later appends are dropped and ``close_partial`` remains callable."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})

        def _raising_flush():
            raise OSError("simulated disk full")

        monkeypatch.setattr(sink, "_flush", _raising_flush)

        # Best-effort terminal: the failed write does not raise into callers.
        assert sink.append_terminal("completed", {"ok": True}) is None
        # The sink is closed regardless of the failed write.
        assert sink._closed is True
        # A post-close append is dropped (never lands in the file).
        assert sink.append("work", {"i": 99}) is None
        # close_partial after the failed terminal write is still safe (no raise).
        sink.close_partial("aborted")
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        assert '"i": 99' not in path.read_text(encoding="utf-8")
        service.shutdown()


# ---------------------------------------------------------------------------
# P1-S4 — Per-run broker: ordering, 256-event/1 MiB limits, SILENT
#         oldest-non-terminal eviction (no synthetic overflow, gaps normal),
#         terminal retention, file-authoritative replay recovery for active
#         runs, atomic replay/live attach, loop-safe cross-thread wakeup,
#         disconnect, completion/shutdown closure
# ---------------------------------------------------------------------------

RUN_ID = "223e4567-e89b-12d3-a456-426614174111"


def _run_broker(root: str, fn):
    """Drive an async broker scenario with asyncio.run."""
    service = WalkLogService(root_dir=root)
    service.start()

    async def scenario():
        try:
            await fn(service)
        finally:
            service.shutdown()

    asyncio.run(scenario())


class TestBrokerOrdering:
    def test_concurrent_append_ordering(self, tmp_path):
        """Concurrent sink appends yield strictly increasing, contiguous seq
        per run regardless of thread interleaving."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")

            def writer(offset):
                for i in range(offset, offset + 25):
                    sink.append("work", {"i": i})

            threads = [threading.Thread(target=writer, args=(n * 25,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
            seqs = [r["seq"] for r in _read_records(path) if r["event"] == "work"]
            assert seqs == list(range(100))  # exactly 0..99, no dup/no gap

        _run_broker(root, scenario)


class TestBrokerLimits:
    def test_broker_eviction_silent_no_synthetic_overflow(self, tmp_path):
        """Broker eviction emits NO synthetic overflow event: after 300
        appends the broker's retained snapshot contains exactly zero records
        with event ``overflow``. The only ``overflow`` record allowed anywhere
        is the real file-cap sink record (covered in TestSinkBoundaries) --
        never a broker artifact."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            for i in range(300):
                sink.append("work", {"i": i})
            snap = service._brokers[RUN_ID].snapshot(after_seq=-1)
            overflow = [r for r in snap if r.event == SINK_OVERFLOW_EVENT]
            assert len(overflow) == 0  # no broker-synthetic overflow record

        _run_broker(root, scenario)

    def test_broker_caps_events_at_256_silent_eviction_unique_ids(self, tmp_path):
        """The broker retains at most BROKER_MAX_EVENTS events, silently
        evicting the oldest non-terminal records. The retained snapshot is
        SHORTER than the total appended (normal gaps), has strictly increasing
        seqs with no duplicate ids/seqs, and contains no synthetic overflow."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            total = 300
            for i in range(total):
                sink.append("work", {"i": i})
            snap = service._brokers[RUN_ID].snapshot(after_seq=-1)
            assert len(snap) <= BROKER_MAX_EVENTS
            assert len(snap) < total  # evicted -> shorter than total (gaps)
            seqs = [r.seq for r in snap]
            assert 0 not in seqs  # oldest non-terminal evicted
            assert seqs == sorted(seqs)  # strictly increasing
            assert len(set(seqs)) == len(seqs)  # no duplicate seq
            ids = [r.id for r in snap]
            assert len(set(ids)) == len(ids)  # no duplicate id
            assert all(r.event != SINK_OVERFLOW_EVENT for r in snap)

        _run_broker(root, scenario)

    def test_broker_caps_bytes_at_1MiB_silent_eviction_unique_ids(self, tmp_path):
        """The broker also caps total retained bytes at BROKER_MAX_BYTES,
        silently evicting oldest non-terminal events. Retained snapshot seqs/ids
        stay unique and no synthetic overflow appears."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            blob = "x" * 5_000
            for _ in range(300):
                sink.append("llm", {"blob": blob})
            snap = service._brokers[RUN_ID].snapshot(after_seq=-1)
            total = sum(len(json.dumps(r.data, ensure_ascii=False)) for r in snap)
            assert total <= BROKER_MAX_BYTES
            seqs = [r.seq for r in snap]
            assert len(set(seqs)) == len(seqs)
            ids = [r.id for r in snap]
            assert len(set(ids)) == len(ids)
            assert all(r.event != SINK_OVERFLOW_EVENT for r in snap)

        _run_broker(root, scenario)

    def test_active_run_file_replay_recovers_evicted_records(self, tmp_path):
        """After broker eviction (300+ appends), open_subscription replays the
        authoritative FILE, so the replay snapshot contains ALL records
        including those the broker evicted; live delivery continues from after
        the file tail (gap recovery)."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            for i in range(300):
                sink.append("work", {"i": i})
            # The broker evicted the oldest records...
            assert 0 not in [r.seq for r in service._brokers[RUN_ID].snapshot(-1)]
            # ...but open_subscription replays the FILE, which has everything.
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            seqs = [r.seq for r in sub.replay if not r.terminal]
            assert seqs == list(range(300))  # evicted records recovered
            assert len(set(seqs)) == len(seqs)
            assert len({r.id for r in sub.replay}) == len(seqs)
            # Live delivery continues from after the file tail.
            sink.append("work", {"i": 300})
            ev = await asyncio.wait_for(
                asyncio.ensure_future(sub.next_event()), timeout=10
            )
            assert ev is not None and ev.seq == 300
            sub.close()

        _run_broker(root, scenario)

    def test_atomic_replay_then_live_no_duplicates(self, tmp_path):
        """Atomic replay-then-live attach: records present in the file replay
        are NOT duplicated live, and live records strictly follow the file tail."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            for i in range(5):
                sink.append("work", {"i": i})
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            replayed = [r.seq for r in sub.replay if not r.terminal]
            assert replayed == [0, 1, 2, 3, 4]
            file_tail = 4

            async def collect():
                return [rec async for rec in sub]

            task = asyncio.ensure_future(collect())
            await asyncio.sleep(0)
            sink.append("work", {"i": 5})  # live, seq > file_tail (4)
            sink.append_terminal("completed", {"ok": True})  # seq 6 terminal
            out = await asyncio.wait_for(task, timeout=10)
            seqs = [r.seq for r in out]
            # Replay records first, then live records strictly after the tail.
            assert seqs[:5] == [0, 1, 2, 3, 4]
            assert seqs[5:] == [5, 6]
            assert all(r.seq > file_tail for r in out[5:])
            assert len(set(seqs)) == len(seqs)  # no duplicate anywhere
            assert out[-1].terminal is True
            sub.close()

        _run_broker(root, scenario)

    def test_terminal_event_retained(self, tmp_path):
        """The terminal record is never evicted and is the last broker event."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            blob = "x" * 5_000
            for _ in range(250):
                sink.append("llm", {"blob": blob})
            sink.append_terminal("completed", {"ok": True})
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            snap = sub.replay
            assert snap[-1].terminal is True
            assert snap[-1].event == "terminal" or snap[-1].data.get("status") == "completed"
            sub.close()

        _run_broker(root, scenario)

    def test_service_replay_filters_by_after_seq(self, tmp_path):
        """service.replay(run_id, after_seq=N) returns only records with
        seq > N (terminal retained as the trailing record)."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        for i in range(5):
            sink.append("work", {"i": i})
        service.close_run(RUN_ID, "completed")
        got = service.replay(RUN_ID, after_seq=2)
        non_terminal = [r.seq for r in got if not r.terminal]
        assert non_terminal == [3, 4]
        assert all(r.seq > 2 for r in got)
        service.shutdown()


class TestBrokerSubscription:
    def test_atomic_replay_plus_subscribe(self, tmp_path):
        """open_subscription captures the replay snapshot atomically with
        registration: events appended afterwards arrive live, none are lost."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sink.append("work", {"i": 0})
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            snap = sub.replay
            assert [r.seq for r in snap if not r.terminal] == [0]
            sink.append("work", {"i": 1})  # live event after registration
            ev = asyncio.wait_for(
                asyncio.ensure_future(sub.next_event()), timeout=10
            )
            ev = await ev
            assert ev is not None and ev.seq == 1
            sub.close()

        _run_broker(root, scenario)

    def test_loop_safe_cross_thread_wakeup(self, tmp_path):
        """A writer thread appending on a non-asyncio thread wakes an asyncio
        consumer via loop-safe scheduling — no timers or polling."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            loop = asyncio.get_running_loop()
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sub = service.open_subscription(RUN_ID, after_seq=-1, loop=loop)

            def writer():
                time.sleep(0.05)  # writer idle, then a single append
                sink.append("live", {"x": 1})

            threading.Thread(target=writer).start()
            ev = asyncio.wait_for(asyncio.ensure_future(sub.next_event()), timeout=10)
            ev = await ev
            assert ev is not None and ev.event == "live"
            sub.close()

        _run_broker(root, scenario)

    def test_subscriber_disconnect_does_not_affect_sink(self, tmp_path):
        """Closing a subscriber removes it without breaking the sink/writer."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            sub.close()
            rec = sink.append("work", {"i": 1})
            assert rec is not None  # append still works after disconnect

        _run_broker(root, scenario)

    def test_completion_ends_subscription_with_none(self, tmp_path):
        """After the terminal record, next_event() yields the terminal then
        returns None (stream closed)."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            sink.append("work", {"i": 0})
            sink.append_terminal("completed", {"ok": True})
            ev0 = await asyncio.wait_for(
                asyncio.ensure_future(sub.next_event()), timeout=10
            )
            assert ev0 is not None and ev0.event == "work"
            ev1 = await asyncio.wait_for(
                asyncio.ensure_future(sub.next_event()), timeout=10
            )
            assert ev1 is not None and ev1.terminal is True
            ev2 = await asyncio.wait_for(
                asyncio.ensure_future(sub.next_event()), timeout=10
            )
            assert ev2 is None
            sub.close()

        _run_broker(root, scenario)

    def test_aiter_yields_replay_then_live_then_terminates(self, tmp_path):
        """__aiter__ yields the replay snapshot, then a subsequent live record,
        then terminates (``None`` / stream exhaustion)."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sink.append("work", {"i": 0})  # captured in the replay snapshot
            sub = service.open_subscription(RUN_ID, after_seq=-1)

            async def collect():
                return [rec async for rec in sub]

            task = asyncio.ensure_future(collect())
            await asyncio.sleep(0)  # let the generator consume replay, then wait
            sink.append("work", {"i": 1})  # arrives live
            sink.append_terminal("completed", {"ok": True})  # closes the stream
            out = await asyncio.wait_for(task, timeout=10)
            assert [r.seq for r in out if not r.terminal] == [0, 1]
            assert out[-1].terminal is True
            sub.close()

        _run_broker(root, scenario)

    def test_terminal_miss_race_does_not_lose_terminal(self, tmp_path, monkeypatch):
        """Terminal-miss race in the already-terminal closed branch is fatal to
        the amended contract's 'no lost records under normal operation': a
        subscriber attaching exactly as the run completes may read the file BEFORE
        append_terminal writes the terminal, then observe the broker already
        closed. The closed branch must bridge the broker snapshot (yielding the
        terminal and any stragglers) before closing, so the stream delivers the
        terminal record before None.

        Deterministic wiring: monkeypatch ``_replay_and_tail`` to capture a
        PRE-terminal file snapshot (via the original implementation) and block;
        a writer thread completes the run (append_terminal/publish, closing the
        broker) while the subscriber is blocked; release the block so
        open_subscription proceeds with the now-stale snapshot into the closed
        branch. Against the pre-fix code the closed branch drops the terminal;
        with the bridge it is delivered."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sink.append("work", {"i": 0})  # file now holds header + record seq 0

            orig = service._replay_and_tail
            snapshot_captured = threading.Event()
            release = threading.Event()

            def stale_replay(run_id, after_seq):
                # Capture the file state BEFORE the terminal lands, then hold so
                # the writer can complete the run in the same window the race
                # describes. Return the stale (pre-terminal) snapshot.
                stale = orig(run_id, after_seq)
                snapshot_captured.set()
                release.wait(timeout=10)
                return stale

            monkeypatch.setattr(service, "_replay_and_tail", stale_replay)

            # Writer completes the run once the subscriber has taken its stale
            # snapshot (blocked in _replay_and_tail).
            def writer():
                snapshot_captured.wait(timeout=10)
                sink.append_terminal("completed", {"ok": True})
                release.set()

            threading.Thread(target=writer).start()

            # Subscriber attaches exactly as the run completes. open_subscription
            # runs synchronously on this loop; it blocks in stale_replay until the
            # writer closes the broker, then proceeds into the closed branch with a
            # stale snapshot that lacks the terminal.
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            out = [rec async for rec in sub]
            # The stream must deliver the terminal record before None -- the race
            # fix bridges it from the closed broker's snapshot.
            assert any(r.terminal for r in out), (
                "terminal record silently lost in the terminal-miss window"
            )
            assert out[-1].terminal is True
            sub.close()

        _run_broker(root, scenario)

    def test_close_run_race_does_not_lose_terminal_on_file_only_path(
        self, tmp_path, monkeypatch
    ):
        """close_run must publish the terminal BEFORE deregistering the run, so a
        subscriber attaching exactly as the run closes on the FILE-ONLY path can
        never observe ``broker is None`` with a pre-terminal file snapshot.

        This is the physical sibling of ``test_terminal_miss_race_...`` but on
        the broker-``None`` slice: the writer completes the run via
        ``service.close_run`` (not a direct ``append_terminal``, so the broker is
        actually popped). It blocks the sink's ``_flush`` seam so the terminal is
        buffered but not yet on disk while the subscriber attaches. Pre-fix
        ``close_run`` popped the broker first, so this subscriber saw ``None`` and
        took the file-only branch with a pre-flush read -> terminal lost. The
        reordered ``close_run`` keeps the broker registered until after the
        terminal flushes, so the subscriber takes the active/closed broker branch
        and the terminal is delivered before ``None``.
        """
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(
                RUN_ID, "book-7", "walk_2a_scene_segmentation"
            )
            sink.append("work", {"i": 0})  # file has header + record seq 0

            orig_flush = sink._flush
            flush_entered = threading.Event()
            release_flush = threading.Event()

            def blocking_flush():
                # Terminal is written to the Python buffer but NOT yet flushed to
                # disk -> a concurrent file read does not see it.
                flush_entered.set()
                release_flush.wait(timeout=10)
                return orig_flush()

            monkeypatch.setattr(sink, "_flush", blocking_flush)

            # Writer completes the run via the SERVICE (pops the broker -- the
            # difference from the direct append_terminal Round-5 test). Blocks at
            # the terminal flush.
            def writer():
                service.close_run(RUN_ID, "completed")

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            flush_entered.wait(timeout=10)

            # Subscriber attaches now: its broker lookup and file read happen in
            # the window where the terminal is buffered but not on disk. Pre-fix
            # the broker is already popped (None -> file-only branch, stale read,
            # terminal lost); post-fix it is still registered so the broker branch
            # bridges/delivers the terminal.
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            # Release the writer so the terminal flush/publish lands, then collect:
            # the broker branch delivers the terminal followed by None.
            release_flush.set()
            writer_thread.join(timeout=10)
            out = [rec async for rec in sub]
            # The stream must still deliver the terminal before None.
            assert any(r.terminal for r in out), (
                "terminal record silently lost in the close_run file-only window"
            )
            assert out[-1].terminal is True
            sub.close()

        _run_broker(root, scenario)

    def test_open_subscription_file_only_completed_run_path(self, tmp_path):
        """A completed (file-only, no live broker) run replays its records and
        returns an immediately-exhausted stream with no live broker registration
        (the path Part C SSE depends on for finished runs)."""
        root = str(tmp_path / "alexandria-walks")
        service = WalkLogService(root_dir=root)
        service.start()

        async def scenario():
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sink.append("work", {"i": 0})
            sink.append("work", {"i": 1})
            service.close_run(RUN_ID, "completed")  # broker removed -> file-only
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            assert [r.seq for r in sub.replay if not r.terminal] == [0, 1]
            assert sub._broker is None  # no live broker registration
            ev = await asyncio.wait_for(
                asyncio.ensure_future(sub.next_event()), timeout=10
            )
            assert ev is None  # stream exhausted immediately
            sub.close()
            service.shutdown()

        asyncio.run(scenario())


class TestReplayRobustness:
    """replay() gracefully handles malformed and foreign lines in the .log file.

    Covers the contract-documented branches: a partial (non-JSON) trailing line,
    a JSON-serialized non-dict line, and a record whose run_id does not match are
    all logged and skipped without ever raising.
    """

    def test_replay_skips_partial_trailing_line(self, tmp_path):
        """A partial (non-JSON) trailing line is skipped by replay without
        raising (contract: 'ignores/logs a partial trailing line')."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})
        sink.append("work", {"i": 1})
        service.close_run(RUN_ID, "completed")
        path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"partial": "unfinished\n')  # broken JSON, no closing brace
        recs = service.replay(RUN_ID)  # must not raise
        assert [r.seq for r in recs if not r.terminal] == [0, 1]
        service.shutdown()

    def test_replay_skips_foreign_run_id_record(self, tmp_path):
        """A record whose run_id does not match is skipped by replay (cross-run
        disclosure guard)."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})
        service.close_run(RUN_ID, "completed")
        path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
        foreign = {
            "run_id": str(uuid.uuid4()),
            "seq": 999,
            "id": "foreign:999",
            "event": "work",
            "data": {"owner": "other-run"},
            "terminal": False,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(foreign, separators=(", ", ": ")) + "\n")
        recs = service.replay(RUN_ID)  # must not raise
        assert all(r.data.get("owner") != "other-run" for r in recs)
        assert [r.seq for r in recs if not r.terminal] == [0]
        service.shutdown()

    def test_replay_skips_non_dict_json_line(self, tmp_path):
        """A JSON-serialized non-dict line (bare list / bare string) is skipped
        by replay without raising."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})
        service.close_run(RUN_ID, "completed")
        path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]\n")
            fh.write('"just a string"\n')
        recs = service.replay(RUN_ID)  # must not raise
        assert [r.seq for r in recs if not r.terminal] == [0]
        service.shutdown()


# ---------------------------------------------------------------------------
# P1-S5 — Lifecycle: idempotent start, startup cleanup, retention, shutdown
#         partial/aborted closure, duplicate-run rejection, unknown-run
#         behavior, and no SQLite/file polling loop
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_is_idempotent(self, tmp_path):
        """Calling start() more than once is safe."""
        root = tmp_path / "alexandria-walks"
        service = WalkLogService(root_dir=str(root))
        service.start()
        service.start()  # must not raise
        service.start()
        assert root.exists()
        service.shutdown()

    def test_startup_removes_only_old_uuid_log_files(self, tmp_path, set_now):
        """At startup, only UUID-named *.log files older than 24h are removed."""
        root = tmp_path / "alexandria-walks"
        root.mkdir(mode=0o700)
        now = 1_752_000_000_000
        set_now(now)
        old_ts = (now - DAY_MS - 3_600_000) / 1000.0  # ~25h old
        fresh_ts = (now - 3_600_000) / 1000.0  # ~1h old

        old_uuid = str(uuid.uuid4())
        fresh_uuid = str(uuid.uuid4())

        # Old UUID-named log -> removed.
        old_path = root / f"{old_uuid}.log"
        old_path.write_text("old", encoding="utf-8")
        os.utime(old_path, (old_ts, old_ts))
        # Fresh UUID-named log -> kept.
        fresh_path = root / f"{fresh_uuid}.log"
        fresh_path.write_text("fresh", encoding="utf-8")
        os.utime(fresh_path, (fresh_ts, fresh_ts))
        # Old but non-UUID-named log -> kept.
        non_uuid = root / "not-a-uuid.log"
        non_uuid.write_text("x", encoding="utf-8")
        os.utime(non_uuid, (old_ts, old_ts))
        # Old non-.log file -> kept.
        txt = root / "notes.txt"
        txt.write_text("x", encoding="utf-8")
        os.utime(txt, (old_ts, old_ts))

        service = WalkLogService(root_dir=str(root))
        service.start()

        assert not old_path.exists()
        assert fresh_path.exists()
        assert non_uuid.exists()
        assert txt.exists()
        service.shutdown()

    def test_shutdown_closes_open_sink_as_partial_or_aborted(self, tmp_path):
        """shutdown closes still-open sinks with a partial/aborted terminal
        marker when possible."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})
        service.shutdown()
        path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
        records = _read_records(path)
        last = records[-1]
        assert last.get("terminal") is True
        assert last["data"].get("status") in ("partial", "aborted")

    def test_shutdown_releases_subscribers(self, tmp_path):
        """After shutdown, an open subscription is closed (next_event returns
        None rather than hanging)."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sub = service.open_subscription(RUN_ID, after_seq=-1)
            sink.append("work", {"i": 0})
            service.shutdown()  # closes the broker/subscribers
            ev = asyncio.wait_for(asyncio.ensure_future(sub.next_event()), timeout=10)
            ev = await ev
            assert ev is None
            sub.close()

        _run_broker(root, scenario)

    def test_duplicate_active_run_rejected(self, tmp_path):
        """Opening a run twice while the first is still active is rejected."""
        service = _started_service(tmp_path)
        service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        with pytest.raises(ValueError):
            service.open_run(RUN_ID, "book-8", "walk_2b_character_discovery")
        service.shutdown()

    def test_get_run_unknown_returns_none(self, tmp_path):
        """get_run on an unknown run returns None."""
        service = _started_service(tmp_path)
        assert service.get_run("unknown-run-123") is None
        service.shutdown()

    def test_open_subscription_unknown_raises_keyerror(self, tmp_path):
        """open_subscription on an unknown run raises KeyError."""
        service = _started_service(tmp_path)

        async def scenario():
            try:
                service.open_subscription("unknown-run-123")
            finally:
                service.shutdown()

        with pytest.raises(KeyError):
            asyncio.run(scenario())

    def test_replay_unknown_run_returns_empty(self, tmp_path):
        """replay on an unknown run returns an empty tuple."""
        service = _started_service(tmp_path)
        assert service.replay("unknown-run-123") == ()
        service.shutdown()


class TestNoPolling:
    def test_live_delivery_uses_no_polling_loop(self, tmp_path, monkeypatch):
        """A subscriber receives a live event while the writer thread is idle;
        delivery must be event-driven, not an asyncio.sleep polling loop."""
        sleeps: list = []
        real_sleep = asyncio.sleep

        async def tracked_sleep(delay, result=None):
            sleeps.append(delay)
            return await real_sleep(delay, result)

        monkeypatch.setattr(asyncio, "sleep", tracked_sleep)
        root = str(tmp_path / "alexandria-walks")

        async def scenario():
            service = WalkLogService(root_dir=root)
            service.start()
            try:
                loop = asyncio.get_running_loop()
                sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
                sub = service.open_subscription(RUN_ID, after_seq=-1, loop=loop)

                def writer():
                    time.sleep(0.05)  # writer thread idle, then one append
                    sink.append("live", {"x": 1})

                threading.Thread(target=writer).start()
                ev = asyncio.wait_for(
                    asyncio.ensure_future(sub.next_event()), timeout=10
                )
                ev = await ev
                assert ev is not None and ev.event == "live"
                # No asyncio.sleep was used for delivery => no polling loop.
                assert sleeps == []
                sub.close()
            finally:
                service.shutdown()

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# P4-S3 — OWASP security review: path traversal / arbitrary write, symlink
#         safety, permission regressions, redaction depth/spacing/casing and
#         redaction-before-truncation, oversized nested payloads, cross-run
#         disclosure. Focused tests for each OWASP finding.
# ---------------------------------------------------------------------------


class TestSecurityReview:
    """OWASP Top 10 focused security tests for the walk-log service."""

    # -- Path traversal / arbitrary file write -----------------------------

    def test_open_run_rejects_non_uuid_no_path_escape(self, tmp_path):
        """A non-UUID run id (path-traversal candidate) is rejected before any
        path is derived, and no file is created outside the root."""
        service = _started_service(tmp_path)
        outside = tmp_path / "escaped.log"
        for bad in ("../../etc/passwd", "../escape", "foo/../../etc/passwd",
                    "..%2f..%2fetc%2fpasswd", "0" * 32, "x" * 40):
            with pytest.raises(ValueError):
                service.open_run(bad, "book-7", "walk_2a_scene_segmentation")
            assert not outside.exists()
        service.shutdown()

    def test_replay_non_uuid_returns_empty_no_path_escape(self, tmp_path):
        """replay on a traversal-shaped id returns empty and never reads a file
        derived from unvalidated input."""
        service = _started_service(tmp_path)
        assert service.replay("../../etc/passwd") == ()
        assert service.replay("../escape") == ()
        service.shutdown()

    def test_open_subscription_non_uuid_raises_keyerror(self, tmp_path):
        """open_subscription on a traversal-shaped id raises KeyError rather
        than deriving a path from unvalidated input."""
        service = _started_service(tmp_path)

        async def scenario():
            try:
                service.open_subscription("../../etc/passwd")
            finally:
                service.shutdown()

        with pytest.raises(KeyError):
            asyncio.run(scenario())

    def test_open_subscription_non_uuid_keyerror_even_if_file_exists(self, tmp_path):
        """open_subscription('not-a-uuid') raises KeyError before any path
        derivation, even when a file matching the derived path already exists."""
        service = _started_service(tmp_path)
        # The naive path derivation would find this file, but the non-UUID id
        # must be rejected before any filesystem existence check.
        (tmp_path / "alexandria-walks" / "not-a-uuid.log").write_text(
            "{}", encoding="utf-8"
        )

        async def scenario():
            try:
                service.open_subscription("not-a-uuid")
            finally:
                service.shutdown()

        with pytest.raises(KeyError):
            asyncio.run(scenario())

    # -- Symlink safety under /tmp -----------------------------------------

    def test_open_run_refuses_symlink_target(self, tmp_path):
        """A pre-existing symlink at the target path must not be followed: the
        victim file stays untouched and open_run fails (O_NOFOLLOW)."""
        service = _started_service(tmp_path)
        root = tmp_path / "alexandria-walks"
        victim = root / "victim.txt"
        victim.write_text("SECRET", encoding="utf-8")
        target = root / f"{VALID_UUID}.log"
        target.symlink_to(victim.name)
        with pytest.raises(OSError):
            service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        # The victim was never written through the symlink.
        assert victim.read_text(encoding="utf-8") == "SECRET"
        service.shutdown()

    # -- Permission regressions (umask independence) -----------------------

    def test_file_mode_0600_independent_of_umask(self, tmp_path):
        """The log file is 0600 regardless of a permissive real umask: the
        service passes explicit mode ``0o600`` to ``os.open`` and enforces it
        with ``fchmod`` on the opened fd, so a permissive umask cannot broaden
        the created file's mode. Restores the process umask in ``finally``."""
        old = os.umask(0o000)  # most permissive: only explicit mode keeps 0600
        try:
            service = _started_service(tmp_path)
            service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
            path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600
            service.shutdown()
        finally:
            os.umask(old)

    def test_directory_mode_0700_independent_of_umask(self, tmp_path):
        """The root directory is 0700 regardless of a permissive real umask:
        the service creates it with explicit mode ``0o700`` and ``chmod``s it,
        so a permissive umask cannot broaden the directory's mode. Restores the
        process umask in ``finally``."""
        old = os.umask(0o000)  # most permissive: only explicit mode keeps 0700
        try:
            root = tmp_path / "alexandria-walks"
            service = WalkLogService(root_dir=str(root))
            service.start()
            mode = stat.S_IMODE(os.stat(root).st_mode)
            assert mode == 0o700
            service.shutdown()
        finally:
            os.umask(old)

    # -- Redaction: depth, spacing/casing, and before-truncation -----------

    def test_redacts_secret_nested_deep_in_json(self, tmp_path):
        """A secret nested many levels deep (dict/list) is still redacted."""
        service = _started_service(tmp_path)
        secret = "sk-nested-0001-secret"
        payload = {
            "prompts": "p",
            "response": "r",
            "nested": {
                "level1": [{"level2": {"level3": {"value": f"Bearer {secret}"}}}]
            },
        }
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("llm", payload)
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content
        service.shutdown()

    def test_redacts_api_key_dict_value_at_any_depth(self, tmp_path):
        """A secret stored as the VALUE of an api_key-named dict key (not an
        assignment string) is redacted at any depth."""
        service = _started_service(tmp_path)
        secret = "supersecret12345"
        payload = {
            "prompts": "p",
            "response": "r",
            "config": {"nested": {"api_key": secret, "apiKey": secret,
                                   "API_KEY": secret, "other": "visible"}},
        }
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("llm", payload)
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content
        service.shutdown()

    def test_redacts_api_key_assignment_varied_spacing_casing(self, tmp_path):
        """api_key assignments with varied spacing/casing are redacted."""
        service = _started_service(tmp_path)
        secrets = ["sk-a", "sk-b"]
        for i, s in enumerate(secrets):
            run_id = str(uuid.uuid4())
            sink = service.open_run(run_id, "book-7", "walk_2a_scene_segmentation")
            sink.append("llm", {"prompts": "p",
                                "response": f"Api_Key = {s}, API-KEY={s}, api key = {s}"})
            content = (tmp_path / "alexandria-walks" / f"{run_id}.log").read_text(
                encoding="utf-8"
            )
            assert s not in content
        service.shutdown()

    def test_redaction_before_truncation_secret_never_in_tail(self, tmp_path):
        """Redaction runs before truncation: a secret placed at the END of an
        oversized payload must not survive in the truncated tail."""
        service = _started_service(tmp_path)
        secret = "sk-tail-9999-secret"
        big = ("p" * 200_000) + f" use-key {secret}"
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        sink.append("llm", {"prompts": big, "response": "ok"})
        content = (tmp_path / "alexandria-walks" / f"{VALID_UUID}.log").read_text(
            encoding="utf-8"
        )
        assert secret not in content
        service.shutdown()

    # -- Oversized nested payloads ----------------------------------------

    def test_oversized_nested_payload_bounded_to_128kib(self, tmp_path):
        """A deeply nested dict that would exceed 128 KiB when serialized is
        shrunk so the whole serialized line is <= 128 KiB."""
        service = _started_service(tmp_path)
        sink = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
        nested = {}
        cursor = nested
        for _ in range(20):  # deeply nested chain
            cursor["next"] = {}
            cursor = cursor["next"]
        cursor["prompts"] = "p" * 120_000  # bounded field deep in the tree
        sink.append("llm", {"nested": nested})
        path = tmp_path / "alexandria-walks" / f"{VALID_UUID}.log"
        for ln in _read_lines(path):
            assert len(ln.encode("utf-8")) <= EVENT_MAX_BYTES
        service.shutdown()

    # -- Cross-run disclosure ---------------------------------------------

    def test_replay_cross_run_no_disclosure(self, tmp_path):
        """replay for one run never returns another run's records."""
        service = _started_service(tmp_path)
        run1 = VALID_UUID
        run2 = RUN_ID
        s1 = service.open_run(run1, "book-7", "walk_2a_scene_segmentation")
        s1.append("work", {"owner": "one"})
        s2 = service.open_run(run2, "book-7", "walk_2a_scene_segmentation")
        s2.append("work", {"owner": "two"})
        r1 = service.replay(run1)
        r2 = service.replay(run2)
        assert [r.data.get("owner") for r in r1] == ["one"]
        assert [r.data.get("owner") for r in r2] == ["two"]
        service.shutdown()

    def test_open_subscription_cross_run_no_disclosure(self, tmp_path):
        """open_subscription for one run's replay snapshot contains only that
        run's records."""
        root = str(tmp_path / "alexandria-walks")
        service = WalkLogService(root_dir=root)
        service.start()

        async def scenario():
            s1 = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
            s1.append("work", {"owner": "one"})
            s2 = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            s2.append("work", {"owner": "two"})
            sub1 = service.open_subscription(VALID_UUID, after_seq=-1)
            sub2 = service.open_subscription(RUN_ID, after_seq=-1)
            owners1 = [r.data.get("owner") for r in sub1.replay]
            owners2 = [r.data.get("owner") for r in sub2.replay]
            assert owners1 == ["one"]
            assert owners2 == ["two"]
            sub1.close()
            sub2.close()
            service.shutdown()

        asyncio.run(scenario())

class TestAmendedSecuritySurfaces:
    """Amended broker/replay security surfaces (Phase 3 rewrite, re-reviewed in
    P4-S3): silent eviction leaves no secret residue, gapped live snapshots are
    isolated per run, the file-tail bridge cannot disclose another run's live
    records, and file-authoritative replay of an ACTIVE run still blocks path
    traversal and skips foreign/partial lines.
    """

    def test_silent_eviction_leaves_no_secret_residue(self, tmp_path):
        """Silent eviction drops records without residue: the retained (gapped)
        broker snapshot contains no secret and only this run's records, and the
        authoritative file never persisted the secret."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            secret = "sk-evict-secret-0001"
            for i in range(300):
                sink.append("work", {"i": i, "nested": {"prompts": f"Bearer {secret}"}})
            snap = service._brokers[RUN_ID].snapshot(after_seq=-1)
            # Gaps normal (evicted), but no retained record carries the secret
            # and every retained record is this run's own.
            assert len(snap) < 300
            assert all(r.run_id == RUN_ID for r in snap)
            assert all(secret not in json.dumps(r.data) for r in snap)
            # The only persistent artifact (the file) never held the secret.
            content = (tmp_path / "alexandria-walks" / f"{RUN_ID}.log").read_text(
                encoding="utf-8"
            )
            assert secret not in content

        _run_broker(root, scenario)

    def test_gapped_live_snapshot_isolated_per_run(self, tmp_path):
        """Broker state is per-run: two active runs, each forced to evict, yield
        gapped live snapshots containing ONLY their own run's records."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            s1 = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
            s2 = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            for _ in range(300):
                s1.append("work", {"owner": "one"})
                s2.append("work", {"owner": "two"})
            snap1 = service._brokers[VALID_UUID].snapshot(after_seq=-1)
            snap2 = service._brokers[RUN_ID].snapshot(after_seq=-1)
            assert len(snap1) < 300 and len(snap2) < 300  # both evicted (gaps)
            assert all(r.run_id == VALID_UUID for r in snap1)
            assert all(r.run_id == RUN_ID for r in snap2)
            assert all(r.data.get("owner") == "one" for r in snap1)
            assert all(r.data.get("owner") == "two" for r in snap2)

        _run_broker(root, scenario)

    def test_live_bridge_cannot_disclose_other_run(self, tmp_path):
        """The file-tail live bridge is per-run: a record published to run B
        never reaches run A's live stream; A only receives its own records."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            loop = asyncio.get_running_loop()
            sa = service.open_run(VALID_UUID, "book-7", "walk_2a_scene_segmentation")
            sb = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sa.append("work", {"owner": "one"})  # A seq 0
            subA = service.open_subscription(VALID_UUID, after_seq=-1, loop=loop)
            assert all(r.run_id == VALID_UUID for r in subA.replay)
            # B publishes a secret-bearing record that must never reach A.
            sb.append("work", {"owner": "two", "secret": "sk-b-secret"})
            # A appends its own next record (A seq 1).
            sa.append("work", {"owner": "one", "i": 1})
            ev = await asyncio.wait_for(
                asyncio.ensure_future(subA.next_event()), timeout=10
            )
            assert ev is not None
            assert ev.run_id == VALID_UUID  # A's own record, not B's
            assert ev.seq == 1
            assert "sk-b-secret" not in json.dumps(ev.data)
            subA.close()

        _run_broker(root, scenario)

    def test_active_run_path_traversal_still_blocked(self, tmp_path):
        """With an ACTIVE run present, replay()/open_subscription() still block
        path traversal (UUID-first) and read only the validated run's file."""
        root = str(tmp_path / "alexandria-walks")

        async def scenario(service):
            sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
            sink.append("work", {"i": 0})
            assert service.replay("../../etc/passwd") == ()
            with pytest.raises(KeyError):
                service.open_subscription("../../etc/passwd")
            # The validated run's own file is still readable (non-empty replay).
            assert service.replay(RUN_ID) != ()

        _run_broker(root, scenario)

    def test_active_run_replay_skips_foreign_and_partial(self, tmp_path):
        """File-authoritative replay of an ACTIVE run still ignores partial
        trailing lines and skips foreign run_id records (cross-run guard)."""
        service = _started_service(tmp_path)
        sink = service.open_run(RUN_ID, "book-7", "walk_2a_scene_segmentation")
        sink.append("work", {"i": 0})
        path = tmp_path / "alexandria-walks" / f"{RUN_ID}.log"
        foreign = {
            "run_id": str(uuid.uuid4()),
            "seq": 999,
            "id": "foreign:999",
            "event": "work",
            "data": {"owner": "other"},
            "terminal": False,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(foreign, separators=(", ", ": ")) + "\n")
            fh.write('{"partial": "unfinished\n')  # broken JSON line
        recs = service.replay(RUN_ID)  # must not raise
        assert all(r.run_id == RUN_ID for r in recs)
        assert all(r.data.get("owner") != "other" for r in recs)
        assert [r.seq for r in recs if not r.terminal] == [0]
        service.shutdown()
