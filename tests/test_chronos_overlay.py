"""Unit tests for scripts/build_chronos_overlay.py.

Tests the pure-Python helpers (quantile interpolation, agreement labels,
tier classification, agent-PT comparison) without invoking Chronos or
hitting Polygon. The actual Chronos inference is exercised by an
end-to-end smoke test on a stored matrix run.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest

from build_chronos_overlay import (
    _agreement_label,
    _select_device,
    _tier,
    _value_to_quantile,
)


# ──────────────────────────────────────────────────────────────────────
# _value_to_quantile — linear interpolation across the quantile grid
# ──────────────────────────────────────────────────────────────────────

class TestValueToQuantile:
    def test_exact_quantile_matches(self):
        levels = [0.1, 0.5, 0.9]
        values = [100.0, 110.0, 120.0]
        assert _value_to_quantile(100.0, levels, values) == pytest.approx(0.1)
        assert _value_to_quantile(110.0, levels, values) == pytest.approx(0.5)
        assert _value_to_quantile(120.0, levels, values) == pytest.approx(0.9)

    def test_midpoints_interpolate_linearly(self):
        levels = [0.1, 0.5, 0.9]
        values = [100.0, 110.0, 120.0]
        # Midpoint between p10 and p50 → 0.3
        assert _value_to_quantile(105.0, levels, values) == pytest.approx(0.3)
        # Midpoint between p50 and p90 → 0.7
        assert _value_to_quantile(115.0, levels, values) == pytest.approx(0.7)

    def test_above_max_extrapolates_beyond_one(self):
        levels = [0.1, 0.5, 0.9]
        values = [100.0, 110.0, 120.0]
        # 130 is one full p50→p90 step beyond p90, so quantile pos > 0.9
        assert _value_to_quantile(130.0, levels, values) > 0.9

    def test_below_min_extrapolates_below_zero(self):
        levels = [0.1, 0.5, 0.9]
        values = [100.0, 110.0, 120.0]
        assert _value_to_quantile(90.0, levels, values) < 0.1

    def test_finer_quantile_grid(self):
        levels = [0.1, 0.25, 0.5, 0.75, 0.9]
        values = [80.0, 90.0, 100.0, 115.0, 130.0]
        # 95 is midpoint between p25 (90) and p50 (100), so should be ~p37.5
        assert _value_to_quantile(95.0, levels, values) == pytest.approx(0.375)

    def test_unsorted_input_handled_via_internal_sort(self):
        # Caller passes levels/values in non-monotonic order — function
        # should still work because it sorts internally.
        levels = [0.5, 0.1, 0.9]
        values = [110.0, 100.0, 120.0]
        assert _value_to_quantile(110.0, levels, values) == pytest.approx(0.5)


# ──────────────────────────────────────────────────────────────────────
# _agreement_label — bucketing the quantile position into a human label
# ──────────────────────────────────────────────────────────────────────

class TestAgreementLabel:
    @pytest.mark.parametrize("quantile_pos,expected", [
        (0.05, "below cone"),
        (0.09, "below cone"),
        (0.10, "low"),       # boundary inclusive at lower end
        (0.20, "low"),
        (0.25, "inside"),    # boundary becomes inside at p25
        (0.50, "inside"),
        (0.74, "inside"),
        (0.75, "high"),      # boundary becomes high at p75
        (0.85, "high"),
        (0.90, "above cone"),
        (0.95, "above cone"),
        (None, "—"),
    ])
    def test_bucket_thresholds(self, quantile_pos, expected):
        assert _agreement_label(quantile_pos) == expected


# ──────────────────────────────────────────────────────────────────────
# _tier — same logic as build_options_overlay / build_run_accounting
# ──────────────────────────────────────────────────────────────────────

class TestTier:
    def test_vetoed_returns_veto(self):
        assert _tier({"classification": "VETOED"}) == "VETO"

    def test_pick_with_no_conservative_pt_is_tier_c(self):
        assert _tier({
            "classification": "PICK",
            "conservative_pt": None,
        }) == "C"

    def test_pick_with_tight_compression_is_tier_a(self):
        assert _tier({
            "classification": "PICK",
            "conservative_pt": 100.0,
            "pt_compression_pct": 3.5,
        }) == "A"

    def test_pick_at_exactly_5pct_compression_is_tier_b(self):
        # Threshold is < 5.0 (strictly less than); 5.0 itself falls to B
        assert _tier({
            "classification": "PICK",
            "conservative_pt": 100.0,
            "pt_compression_pct": 5.0,
        }) == "B"

    def test_pick_with_loose_compression_is_tier_b(self):
        assert _tier({
            "classification": "PICK",
            "conservative_pt": 100.0,
            "pt_compression_pct": 12.0,
        }) == "B"

    def test_neither_pick_nor_vetoed_is_dash(self):
        assert _tier({"classification": "PENDING"}) == "—"
        assert _tier({}) == "—"


# ──────────────────────────────────────────────────────────────────────
# _select_device — auto resolution
# ──────────────────────────────────────────────────────────────────────

class TestSelectDevice:
    def test_explicit_preference_returned_unchanged(self):
        assert _select_device("cpu") == "cpu"
        assert _select_device("mps") == "mps"
        assert _select_device("cuda") == "cuda"

    def test_auto_returns_one_of_the_known_devices(self):
        # We can't assert which is available on the test machine,
        # but auto must never return 'auto' itself.
        result = _select_device("auto")
        assert result in ("mps", "cuda", "cpu")
