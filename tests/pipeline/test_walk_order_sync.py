"""Synchronization tests for walk-order contract.

Verifies that the frontend TypeScript constants in
``frontend/src/pipeline/walks.ts`` match the backend Python constants in
``app/pipeline/walks/order.py``.

These tests are deterministic — exact string comparison, no fuzziness.
If any test fails, the developer must update BOTH files to keep them in sync.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.pipeline.walks.order import (
    WALK_DISPLAY_NAMES,
    WALK_ORDER,
    WALK_TASK_NAMES,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolve relative to this test file → project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_WALKS_TS = _PROJECT_ROOT / "frontend" / "src" / "pipeline" / "walks.ts"


# ---------------------------------------------------------------------------
# TypeScript parsing helpers (regex-based — the file is simple and controlled)
# ---------------------------------------------------------------------------


def _read_frontend_source() -> str:
    """Read the frontend TypeScript walks module."""
    assert _FRONTEND_WALKS_TS.exists(), (
        f"Frontend walks.ts not found at {_FRONTEND_WALKS_TS}. "
        "Was the file moved or renamed?"
    )
    return _FRONTEND_WALKS_TS.read_text(encoding="utf-8")


def _parse_ts_string_array(source: str, const_name: str) -> list[str]:
    """Extract a ``const NAME: readonly string[] = [...]`` from TypeScript source.

    Matches the opening ``[`` through the closing ``]`` and extracts every
    single-quoted or double-quoted string literal inside.
    """
    # Pattern: const <NAME> ... = [ ... ]
    pattern = rf"(?:export\s+)?const\s+{re.escape(const_name)}\b[^=]*=\s*\[(.*?)\]"
    match = re.search(pattern, source, re.DOTALL)
    assert match, (
        f"Could not find const {const_name} = [...] in TypeScript source. "
        "Check the constant name and array syntax."
    )
    body = match.group(1)
    return re.findall(r"""['"]([^'"]+)['"]""", body)


def _parse_ts_string_record(
    source: str, const_name: str
) -> dict[str, str]:
    """Extract a ``const NAME: Readonly<Record<string, string>> = { ... }``.

    Matches the opening ``{`` through the closing ``}`` and extracts every
    ``key: 'value'`` or ``key: "value"`` pair.  Keys may be unquoted
    identifiers (TypeScript object shorthand).
    """
    # Pattern: const <NAME> ... = { ... }
    pattern = rf"(?:export\s+)?const\s+{re.escape(const_name)}\b[^=]*=\s*\{{(.*?)\}};"
    match = re.search(pattern, source, re.DOTALL)
    assert match, (
        f"Could not find const {const_name} = {{...}} in TypeScript source. "
        "Check the constant name and object syntax."
    )
    body = match.group(1)
    # Match: optional-quoted-key : 'value' or "value"
    entries = re.findall(
        r"""(?:['"]?)(\w+)(?:['"]?)\s*:\s*['"]([^'"]+)['"]""",
        body,
    )
    return dict(entries)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frontend_source() -> str:
    """Load frontend TypeScript source once per module."""
    return _read_frontend_source()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWalkOrderSync:
    """Verify frontend TypeScript constants match backend Python constants."""

    def test_walk_order_matches_backend(self, frontend_source: str) -> None:
        """``WALK_ORDER`` in TypeScript matches ``WALK_ORDER`` in Python.

        Both the element values and their ordering must be identical.
        """
        ts_walk_order = _parse_ts_string_array(frontend_source, "WALK_ORDER")

        assert ts_walk_order == WALK_ORDER, (
            f"WALK_ORDER mismatch.\n"
            f"  Backend (Python): {WALK_ORDER}\n"
            f"  Frontend (TS):    {ts_walk_order}\n"
            "Update both app/pipeline/walks/order.py and "
            "frontend/src/pipeline/walks.ts to keep them in sync."
        )

    def test_walk_task_names_match_backend(self, frontend_source: str) -> None:
        """``WALK_TASK_NAMES`` in TypeScript matches ``WALK_TASK_NAMES`` in Python.

        Both the keys and values must be identical.
        """
        ts_task_names = _parse_ts_string_record(frontend_source, "WALK_TASK_NAMES")

        assert ts_task_names == WALK_TASK_NAMES, (
            f"WALK_TASK_NAMES mismatch.\n"
            f"  Backend (Python): {WALK_TASK_NAMES}\n"
            f"  Frontend (TS):    {ts_task_names}\n"
            "Update both app/pipeline/walks/order.py and "
            "frontend/src/pipeline/walks.ts to keep them in sync."
        )

    def test_walk_display_names_match_backend(self, frontend_source: str) -> None:
        """``WALK_DISPLAY_NAMES`` in TypeScript matches ``WALK_DISPLAY_NAMES`` in Python.

        Both the keys and values must be identical.
        """
        ts_display_names = _parse_ts_string_record(
            frontend_source, "WALK_DISPLAY_NAMES"
        )

        assert ts_display_names == WALK_DISPLAY_NAMES, (
            f"WALK_DISPLAY_NAMES mismatch.\n"
            f"  Backend (Python): {WALK_DISPLAY_NAMES}\n"
            f"  Frontend (TS):    {ts_display_names}\n"
            "Update both app/pipeline/walks/order.py and "
            "frontend/src/pipeline/walks.ts to keep them in sync."
        )
