"""Unit tests for options-overlay tenor mapping.

These tests guard against regressions in `_target_dte`, which translates the
agent's natural-language horizon (e.g. "12-24 months") into a (min_dte_floor,
target_dte) tuple. Bugs here silently produce the wrong expiration choice
(e.g. NVDA picking an Aug expiration for a 12-24 month thesis), which is
costly because the resulting overlay numbers look superficially fine.

Specifically guards against:

1. The original bug: any horizon longer than "6-18 months" fell through to
   the default (90, 120) bucket because no mapping existed.
2. The first-fix regression: aggressive long-horizon markers ("18 months",
   "24 months") accidentally captured "6-18 months" / "6-24 months" because
   substring matching is order-dependent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_options_overlay.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_options_overlay", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def overlay_mod():
    return _load_module()


class TestTargetDTE:
    """Each case is (horizon_string, expected_(min_floor, target_dte))."""

    @pytest.mark.parametrize(
        "horizon,expected",
        [
            (None, (90, 120)),
            ("", (90, 120)),
            ("unknown phrase", (90, 120)),
        ],
    )
    def test_default_for_missing_or_unknown(self, overlay_mod, horizon, expected):
        assert overlay_mod._target_dte(horizon) == expected

    @pytest.mark.parametrize(
        "horizon,expected",
        [
            ("1-3 months", (30, 75)),
            ("3-6 months", (90, 135)),
            ("3 to 6 months", (90, 135)),
            ("3-9 months", (90, 180)),
            ("3 to 9 months", (90, 180)),
            ("6-12 months", (180, 270)),
            ("6 to 12 months", (180, 270)),
            ("6-18 months", (180, 365)),
            ("6 to 18 months", (180, 365)),
        ],
    )
    def test_short_and_medium_ranges(self, overlay_mod, horizon, expected):
        assert overlay_mod._target_dte(horizon) == expected

    @pytest.mark.parametrize(
        "horizon",
        [
            "12-18 months",
            "12-24 months",
            "12 to 24 months",
            "12-36 months",
            "1-2 years",
            "1 to 2 years",
            "2-year",
            "2 year LEAP",
            "multi-year",
            "multi year",
            "LEAP",
            "leaps",
            "12+ months",
            "12 or more months",
        ],
    )
    def test_long_horizon_maps_to_leap(self, overlay_mod, horizon):
        """All long-horizon phrases should resolve to ~547 DTE (≈18mo LEAP)."""
        assert overlay_mod._target_dte(horizon) == (365, 547)

    def test_case_insensitive(self, overlay_mod):
        assert overlay_mod._target_dte("12-24 MONTHS") == (365, 547)
        assert overlay_mod._target_dte("Multi-Year") == (365, 547)
        assert overlay_mod._target_dte("3-6 Months") == (90, 135)

    def test_explicit_ranges_beat_loose_long_markers(self, overlay_mod):
        """Regression guard: '6-18 months' must NOT trip a loose '18'
        long-horizon match. Explicit ranges are checked before catch-alls.
        """
        # 6-18 months → medium range (180, 365), NOT (365, 547)
        assert overlay_mod._target_dte("6-18 months") == (180, 365)
        # 6-12 months → medium range (180, 270), NOT (365, 547)
        assert overlay_mod._target_dte("6-12 months") == (180, 270)

    def test_nvda_actual_horizon(self, overlay_mod):
        """The exact horizon string emitted by the NVDA aggressive cell that
        triggered this bug — must select a LEAP-sized window.
        """
        assert overlay_mod._target_dte("12-24 months") == (365, 547)
