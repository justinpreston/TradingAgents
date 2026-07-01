"""Tests for the canonical tier derivation in tradingagents/tiers.py.

Covers:
  1. The derivation matrix (every branch of tier_for_row, both variants).
  2. The rank ladder (VETO < — < C < B < A).
  3. Parity: every wrapper site across the repo returns the identical tier
     for a grid of synthetic rows as the canonical function (accounting for
     each site's documented, preserved quirks — e.g. some return None
     instead of '—' for non-PICK rows).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tradingagents.tiers import TIER_RANK, tier_for_row, tier_rank


# ---------------------------------------------------------------------------
# 1. Derivation matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "row,suspect_caps_a,expected",
    [
        # Non-PICK branches
        ({"classification": "VETOED"}, False, "VETO"),
        ({"classification": "VETOED"}, True, "VETO"),
        ({"classification": "HOLD"}, False, "—"),
        ({"classification": None}, False, "—"),
        ({}, False, "—"),
        # PICK, conservative_pt None -> C
        ({"classification": "PICK", "conservative_pt": None,
          "pt_compression_pct": 1.0}, False, "C"),
        ({"classification": "PICK", "conservative_pt": None}, False, "C"),
        # PICK, compression < 5.0 -> A
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 4.999}, False, "A"),
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": -10.0}, False, "A"),
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 0.0}, False, "A"),
        # PICK, compression >= 5.0 -> B
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 5.0}, False, "B"),
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 50.0}, False, "B"),
        # PICK, compression None (but cons_pt set) -> B
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": None}, False, "B"),
        # suspect_caps_a=True: tight compression but suspect flags -> B
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 1.0,
          "pt_quality_flags": ["suspect_pt"]}, True, "B"),
        # suspect_caps_a=True: tight compression, no flags -> A
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 1.0,
          "pt_quality_flags": []}, True, "A"),
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 1.0}, True, "A"),
        # suspect_caps_a=False ignores pt_quality_flags entirely
        ({"classification": "PICK", "conservative_pt": 100.0,
          "pt_compression_pct": 1.0,
          "pt_quality_flags": ["suspect_pt"]}, False, "A"),
    ],
)
def test_tier_for_row_matrix(row, suspect_caps_a, expected):
    assert tier_for_row(row, suspect_caps_a=suspect_caps_a) == expected


# ---------------------------------------------------------------------------
# 2. Rank ladder
# ---------------------------------------------------------------------------

def test_tier_rank_ladder_order():
    assert tier_rank("VETO") < tier_rank("—") < tier_rank("C") < tier_rank("B") < tier_rank("A")


def test_tier_rank_none_equals_dash():
    assert tier_rank(None) == tier_rank("—")


def test_tier_rank_unknown_defaults_to_dash_rank():
    assert tier_rank("bogus") == tier_rank("—")


def test_tier_rank_table_values():
    assert TIER_RANK == {"VETO": 0, "—": 1, None: 1, "C": 2, "B": 3, "A": 4}


# ---------------------------------------------------------------------------
# 3. Cross-site parity
# ---------------------------------------------------------------------------

# Synthetic grid of PICK-classified rows spanning the interesting boundary
# conditions (compression thresholds, missing cons_pt, suspect flags).
_PICK_GRID = [
    {"classification": "PICK", "conservative_pt": None, "pt_compression_pct": None},
    {"classification": "PICK", "conservative_pt": None, "pt_compression_pct": 2.0},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": None},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": -5.0},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 0.0},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 4.999},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 5.0},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 5.001},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 30.0},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 1.0,
     "pt_quality_flags": ["suspect_pt"]},
    {"classification": "PICK", "conservative_pt": 50.0, "pt_compression_pct": 1.0,
     "pt_quality_flags": []},
]

_VETOED_ROW = {"classification": "VETOED"}
_OTHER_ROW = {"classification": "HOLD"}


def test_parity_build_options_overlay():
    from scripts.build_options_overlay import _tier as site_tier
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=True)
    # Site returns '—' for VETOED (documented deviation from canonical 'VETO')
    assert site_tier(_VETOED_ROW) == "—"
    assert site_tier(_OTHER_ROW) == "—"


def test_parity_build_chronos_overlay():
    from scripts.build_chronos_overlay import _tier as site_tier
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=True)
    assert site_tier(_VETOED_ROW) == "VETO"
    assert site_tier(_OTHER_ROW) == "—"


def test_parity_build_html_report():
    from scripts.build_html_report import _tier as site_tier
    # Pre-delegation html report capped Tier A on pt_quality_flags
    # (`comp < 5.0 and not suspect_flags`), so parity is against the
    # suspect-capping variant, not the plain one.
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=True)
    assert site_tier(_VETOED_ROW) == "VETO"
    assert site_tier(_OTHER_ROW) == "—"


def test_parity_index_runs():
    from scripts.index_runs import _classification_to_tier as site_tier
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=True)
    # Site returns None (not '—') for non-PICK/non-VETOED — documented deviation
    assert site_tier(_VETOED_ROW) == "VETO"
    assert site_tier(_OTHER_ROW) is None


def test_parity_quick_verdict():
    from scripts.quick_verdict import _tier as site_tier
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=False)
    assert site_tier(_VETOED_ROW) == "VETO"
    assert site_tier(_OTHER_ROW) == "—"


def test_parity_compare_insider_ablation():
    from scripts.compare_insider_ablation import _tier as site_tier, _tier_rank as site_rank
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=False)
    assert site_tier(_VETOED_ROW) == "VETO"
    assert site_tier(_OTHER_ROW) == "—"
    for t in ("VETO", "—", "C", "B", "A", None, "bogus"):
        assert site_rank(t) == tier_rank(t)


def test_parity_grounding_audit():
    from scripts.grounding_audit import _tier_for_row as site_tier
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=True)
    assert site_tier(_VETOED_ROW) == "VETO"
    # Site returns None (not '—') for non-PICK/non-VETOED — documented deviation
    assert site_tier(_OTHER_ROW) is None


def test_parity_portfolio_load_context():
    from scripts.portfolio_load_context import _row_tier as site_tier, _tier_rank as site_rank
    for row in _PICK_GRID:
        assert site_tier(row) == tier_for_row(row, suspect_caps_a=False)
    assert site_tier(_VETOED_ROW) == "VETO"
    assert site_tier(_OTHER_ROW) == "—"
    assert site_tier(None) is None
    for t in ("VETO", "—", "C", "B", "A", None, "bogus"):
        assert site_rank(t) == tier_rank(t)
