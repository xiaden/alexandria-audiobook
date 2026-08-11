"""Plan L Phase 1 tests: pause validation bounds and resolution precedence.

Covers P1-S1 (``validate_pause_ms`` shared bound) and P1-S3
(``resolve_effective_pauses`` precedence: request override → book/project
override → config default → 500/250 fallback, with NULL-vs-0 honored and no
unbounded value reaching the resolver's output).
"""

from __future__ import annotations

import pytest

from app.pipeline.tts_integration import (
    PAUSE_BETWEEN_SPEAKERS_MS,
    PAUSE_MAX_MS,
    PAUSE_SAME_SPEAKER_MS,
    resolve_effective_pauses,
    validate_pause_ms,
)


class TestValidatePauseMs:
    """P1-S1: the shared bounded-integer pause validator."""

    @pytest.mark.parametrize(
        "value", [0, 1, 250, 500, 10_000]
    )
    def test_accepts_boundary_values(self, value: int) -> None:
        assert validate_pause_ms(value) == value

    @pytest.mark.parametrize("value", [-1, -500, -10_000])
    def test_rejects_negative(self, value: int) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            validate_pause_ms(value)

    @pytest.mark.parametrize("value", [1.5, 250.5, -0.5])
    def test_rejects_fractional(self, value: float) -> None:
        with pytest.raises(ValueError, match="integer"):
            validate_pause_ms(value)

    def test_rejects_float_that_is_integral_value(self) -> None:
        # An integral float (500.0) is accepted and normalized to int.
        assert validate_pause_ms(500.0) == 500

    def test_rejects_boolean(self) -> None:
        with pytest.raises(ValueError, match="boolean"):
            validate_pause_ms(True)
        with pytest.raises(ValueError, match="boolean"):
            validate_pause_ms(False)

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf")]
    )
    def test_rejects_nan_and_infinity(self, value: float) -> None:
        with pytest.raises(ValueError, match="NaN"):
            validate_pause_ms(value)

    def test_rejects_string(self) -> None:
        with pytest.raises(ValueError, match="integer"):
            validate_pause_ms("500")

    def test_rejects_above_max(self) -> None:
        with pytest.raises(ValueError, match=str(PAUSE_MAX_MS)):
            validate_pause_ms(PAUSE_MAX_MS + 1)

    def test_rejects_huge_value(self) -> None:
        with pytest.raises(ValueError):
            validate_pause_ms(2**63)

    def test_documented_max_is_positive(self) -> None:
        assert PAUSE_MAX_MS == 10_000


class TestResolveEffectivePauses:
    """P1-S3: precedence request → book → config → 500/250 fallback."""

    def test_all_tiers_empty_falls_back_to_defaults(self) -> None:
        assert resolve_effective_pauses() == (
            PAUSE_BETWEEN_SPEAKERS_MS,
            PAUSE_SAME_SPEAKER_MS,
        )

    def test_config_default_applies(self) -> None:
        out = resolve_effective_pauses(
            config_defaults={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400}
        )
        assert out == (900, 400)

    def test_book_override_beats_config(self) -> None:
        out = resolve_effective_pauses(
            book_overrides={"pause_between_speakers_ms": 700},
            config_defaults={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400},
        )
        assert out == (700, 400)  # same field from config still applies

    def test_request_override_beats_all(self) -> None:
        out = resolve_effective_pauses(
            request_overrides={"pause_between_speakers_ms": 60, "pause_same_speaker_ms": 30},
            book_overrides={"pause_between_speakers_ms": 700},
            config_defaults={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400},
        )
        assert out == (60, 30)

    def test_zero_override_is_honored_not_coerced(self) -> None:
        # 0 is an intentional no-gap override — beats the config default.
        out = resolve_effective_pauses(
            book_overrides={"pause_between_speakers_ms": 0},
            config_defaults={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400},
        )
        assert out == (0, 400)

    def test_none_at_higher_tier_means_resolve_next(self) -> None:
        out = resolve_effective_pauses(
            book_overrides={"pause_between_speakers_ms": None, "pause_same_speaker_ms": 123},
            config_defaults={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400},
        )
        assert out == (900, 123)

    def test_partial_book_override_uses_config_for_other_field(self) -> None:
        out = resolve_effective_pauses(
            book_overrides={"pause_between_speakers_ms": 700},
            config_defaults={"pause_between_speakers_ms": 900, "pause_same_speaker_ms": 400},
        )
        assert out == (700, 400)

    def test_out_of_range_override_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_effective_pauses(
                book_overrides={"pause_between_speakers_ms": PAUSE_MAX_MS + 1}
            )
