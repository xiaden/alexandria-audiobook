"""Deterministic tests for the external capability discovery harness (P1-S2).

These tests run WITHOUT ``ALEXANDRIA_EXTERNAL=1`` (the deterministic CI path):
engine-gated capabilities MUST report ``unavailable`` — never a fabricated
``supported`` — and the harness must never invent a green for a capability it
did not actually probe.  The ``supported``/``failed`` paths are exercised with
an injected fake engine through the same public ``engine_provider`` seam the
production code uses, so the full status vocabulary is covered without a real
engine.

Gate: ``/tmp/qa-venv/bin/pytest -q tests/external/`` — deterministic green.
"""

from __future__ import annotations

import os

import pytest

from tests.external.capability_matrix import (
    ALL_CAPABILITIES,
    ENABLE_EXTERNAL_ENV,
    ENGINE_CAPABILITIES,
    MEDIA_CAPABILITIES,
    VALID_STATUSES,
    capability_report,
    discover_capabilities,
    make_wav,
    tool_versions,
)


@pytest.fixture(autouse=True)
def _ensure_external_disabled(monkeypatch):
    """Deterministic CI: force the opt-in marker OFF for every test here."""
    monkeypatch.delenv(ENABLE_EXTERNAL_ENV, raising=False)


# ---------------------------------------------------------------------------
# Matrix shape
# ---------------------------------------------------------------------------


class TestMatrixShape:
    def test_reports_every_required_capability(self):
        report = discover_capabilities()
        capabilities = {row["capability"] for row in report}
        assert capabilities == set(ALL_CAPABILITIES)

    def test_every_row_has_required_fields(self):
        for row in discover_capabilities():
            assert set(row) == {"capability", "status", "detail", "evidence"}
            assert isinstance(row["detail"], str) and row["detail"]
            assert isinstance(row["evidence"], dict)

    def test_every_status_is_valid_vocabulary(self):
        for row in discover_capabilities():
            assert row["status"] in VALID_STATUSES


# ---------------------------------------------------------------------------
# Engine-gated capabilities (deterministic CI, marker OFF)
# ---------------------------------------------------------------------------


class TestEngineCapabilitiesWithoutExternal:
    def test_all_engine_capabilities_report_unavailable(self):
        by_name = {row["capability"]: row for row in discover_capabilities()}
        for cap in ENGINE_CAPABILITIES:
            assert by_name[cap]["status"] == "unavailable"
            assert ENABLE_EXTERNAL_ENV in by_name[cap]["detail"]

    def test_no_engine_capability_is_ever_supported_in_deterministic_ci(self):
        """The harness must not fabricate a green for an unprobed capability."""
        by_name = {row["capability"]: row for row in discover_capabilities()}
        for cap in ENGINE_CAPABILITIES:
            assert by_name[cap]["status"] != "supported"


# ---------------------------------------------------------------------------
# Media capabilities (real-binary probes, always run)
# ---------------------------------------------------------------------------


class TestMediaCapabilities:
    def test_media_capabilities_present(self):
        by_name = {row["capability"]: row for row in discover_capabilities()}
        for cap in MEDIA_CAPABILITIES:
            row = by_name[cap]
            assert row["status"] in VALID_STATUSES
            # Every media result carries fixture provenance in its evidence.
            assert "fixture" in row["evidence"]

    def test_media_probe_status_consistent_with_tool_presence(self):
        """A 'supported' media capability requires the matching binary present."""
        versions = tool_versions()
        by_name = {row["capability"]: row for row in discover_capabilities()}
        if versions["ffprobe"] is None:
            assert by_name["media_decode"]["status"] == "unavailable"
        if versions["ffmpeg"] is None:
            assert by_name["media_encode"]["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Supported / failed paths via injected fake engine (same public seam)
# ---------------------------------------------------------------------------


class _FakeSupportedEngine:
    mode = "local"

    def generate_clone_voice(self, text, speaker, voice_config, output_path):
        pass

    def generate_voice_design(self, description, sample_text, language=None, seed=-1):
        pass

    def generate_lora_voice(self, text, instruct_text, voice_data, output_path):
        pass


class _FakeBrokenEngine:
    mode = "local"

    def generate_clone_voice(self, text, speaker, voice_config, output_path):
        pass

    @property
    def generate_voice_design(self):
        raise RuntimeError("design surface broken")

    def generate_lora_voice(self, text, instruct_text, voice_data, output_path):
        pass


class TestInjectedEnginePaths:
    def test_missing_engine_is_unavailable(self):
        """Factory returning None → unavailable (availability-aware), never green."""
        report = discover_capabilities(enable_external=True, engine_provider=lambda: None)
        by_name = {row["capability"]: row for row in report}
        for cap in ENGINE_CAPABILITIES:
            assert by_name[cap]["status"] == "unavailable"
            assert "no TTS engine available" in by_name[cap]["detail"]

    def test_supported_engine_reports_supported(self):
        report = discover_capabilities(
            enable_external=True, engine_provider=lambda: _FakeSupportedEngine()
        )
        by_name = {row["capability"]: row for row in report}
        for cap in ENGINE_CAPABILITIES:
            assert by_name[cap]["status"] == "supported"

    def test_broken_engine_surface_reports_failed(self):
        report = discover_capabilities(
            enable_external=True, engine_provider=lambda: _FakeBrokenEngine()
        )
        by_name = {row["capability"]: row for row in report}
        assert by_name["design"]["status"] == "failed"
        assert "errored" in by_name["design"]["detail"]

    def test_factory_raising_reports_failed(self):
        def boom():
            raise RuntimeError("factory exploded")

        report = discover_capabilities(enable_external=True, engine_provider=boom)
        by_name = {row["capability"]: row for row in report}
        for cap in ENGINE_CAPABILITIES:
            assert by_name[cap]["status"] == "failed"

    def test_opt_in_marker_enables_real_probing(self, monkeypatch):
        """ALEXANDRIA_EXTERNAL=1 routes engine caps through the provider."""
        monkeypatch.setenv(ENABLE_EXTERNAL_ENV, "1")
        calls = []

        def provider():
            calls.append(1)
            return _FakeSupportedEngine()

        discover_capabilities(engine_provider=provider)
        # Provider consulted once per engine capability (not per row).
        assert len(calls) == len(ENGINE_CAPABILITIES)


# ---------------------------------------------------------------------------
# Fixture provenance + tool versions
# ---------------------------------------------------------------------------


class TestFixtureAndVersions:
    def test_report_includes_fixture_provenance_and_tool_versions(self):
        report = capability_report()
        assert set(report) == {
            "capabilities",
            "fixture_provenance",
            "tool_versions",
        }
        prov = report["fixture_provenance"]
        assert prov["source"] == "deterministic in-memory PCM WAV generation"
        assert "license" in prov
        versions = report["tool_versions"]
        assert "ffmpeg" in versions and "ffprobe" in versions

    def test_tool_versions_shape(self):
        versions = tool_versions()
        assert set(versions) == {"ffmpeg", "ffprobe", "python", "torch"}
        assert versions["python"]  # non-empty

    def test_make_wav_fixture_is_valid_pcm(self):
        blob = make_wav()
        assert blob[:4] == b"RIFF"
        assert blob[8:12] == b"WAVE"
        assert len(blob) > 44


# ---------------------------------------------------------------------------
# End-to-end determinism: running twice yields identical results
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_runs_identical(self):
        assert discover_capabilities() == discover_capabilities()

    def test_env_var_absent_equals_marker_off(self, monkeypatch):
        monkeypatch.setenv(ENABLE_EXTERNAL_ENV, "0")
        assert discover_capabilities() == discover_capabilities(enable_external=False)
