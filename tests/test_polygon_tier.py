"""Tests for ``recommended_min_interval_for_tier`` tier-aware default selection."""

from __future__ import annotations

import pytest

from tradingagents.dataflows.polygon_common import (
    _TIER_INTERVALS,
    recommended_min_interval_for_tier,
)


class TestTierLookup:
    def test_explicit_tier_arg(self) -> None:
        assert recommended_min_interval_for_tier("basic") == _TIER_INTERVALS["basic"]
        assert recommended_min_interval_for_tier("free") == _TIER_INTERVALS["free"]

    def test_case_insensitive(self) -> None:
        assert recommended_min_interval_for_tier("BASIC") == _TIER_INTERVALS["basic"]
        assert recommended_min_interval_for_tier(" Starter ") == _TIER_INTERVALS["starter"]

    def test_unknown_tier_falls_back_to_free(self) -> None:
        assert recommended_min_interval_for_tier("enterprise_plus") == _TIER_INTERVALS["free"]
        assert recommended_min_interval_for_tier("") == _TIER_INTERVALS["free"]

    def test_aliases(self) -> None:
        # basic and starter are the same Polygon plan ("Stocks Starter"); they should match
        assert recommended_min_interval_for_tier("basic") == recommended_min_interval_for_tier("starter")


class TestEnvVar:
    def test_polygon_tier_env_picks_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYGON_TIER", "basic")
        monkeypatch.delenv("POLYGON_MIN_REQUEST_INTERVAL_S", raising=False)
        assert recommended_min_interval_for_tier() == _TIER_INTERVALS["basic"]

    def test_no_env_defaults_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POLYGON_TIER", raising=False)
        monkeypatch.delenv("POLYGON_MIN_REQUEST_INTERVAL_S", raising=False)
        assert recommended_min_interval_for_tier() == _TIER_INTERVALS["free"]

    def test_explicit_arg_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYGON_TIER", "basic")
        # Explicit "free" arg overrides env
        assert recommended_min_interval_for_tier("free") == _TIER_INTERVALS["free"]

    def test_min_interval_env_beats_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYGON_TIER", "free")
        monkeypatch.setenv("POLYGON_MIN_REQUEST_INTERVAL_S", "0.10")
        assert recommended_min_interval_for_tier() == 0.10

    def test_min_interval_env_beats_explicit_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The override env var is the most specific signal — it wins over everything."""
        monkeypatch.setenv("POLYGON_MIN_REQUEST_INTERVAL_S", "0.50")
        assert recommended_min_interval_for_tier("basic") == 0.50

    def test_garbage_min_interval_env_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYGON_TIER", "basic")
        monkeypatch.setenv("POLYGON_MIN_REQUEST_INTERVAL_S", "not_a_float")
        assert recommended_min_interval_for_tier() == _TIER_INTERVALS["basic"]

    def test_negative_min_interval_clamped_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYGON_MIN_REQUEST_INTERVAL_S", "-1.0")
        assert recommended_min_interval_for_tier() == 0.0


class TestTierValues:
    """Smoke checks on the canonical tier mapping."""

    def test_paid_tiers_are_below_free(self) -> None:
        free = _TIER_INTERVALS["free"]
        for tier in ("basic", "starter", "developer", "advanced"):
            assert _TIER_INTERVALS[tier] < free, f"{tier} should be lower than free tier"

    def test_paid_tiers_have_non_zero_floor(self) -> None:
        """Even on unlimited tiers, keep some floor to avoid pathological bursts."""
        for tier in ("basic", "starter", "developer", "advanced"):
            assert _TIER_INTERVALS[tier] > 0, f"{tier} interval should be > 0"
