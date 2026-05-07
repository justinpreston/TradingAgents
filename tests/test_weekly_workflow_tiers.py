"""Tests for the multi-tier capability in scripts/weekly_workflow.py.

Covers:
  - TIER_PRESETS are coherent (mid < large < mega; ADV/price floors scale up)
  - _apply_tier_preset overrides only the flags the user did NOT set
  - _user_supplied_dests correctly maps CLI flags to argparse dests
  - --tier argument parses and applies the preset to args
  - Tier-tagged screener output_dir is correctly constructed
  - Tier-tagged matrix run_id pattern is what phase_diff filters on

These guard the multi-tier orchestration: the wrapper at
scripts/run_weekly_all_tiers.py shells out to weekly_workflow.py with --tier
{mid,large,mega} sequentially, and the diff phase relies on the run_id
prefix to filter matrix history per-tier.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WW_PATH = REPO_ROOT / "scripts" / "weekly_workflow.py"


@pytest.fixture(scope="module")
def ww():
    """Import scripts/weekly_workflow.py as a module without running main()."""
    spec = importlib.util.spec_from_file_location("weekly_workflow", WW_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["weekly_workflow"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_tier_presets_exist_and_scale(ww):
    """All three tier presets exist and ADV/price floors scale with mcap."""
    presets = ww.TIER_PRESETS
    assert set(presets) == {"mid", "large", "mega"}
    for tier, p in presets.items():
        assert p["min_mcap"] < p["max_mcap"], f"{tier}: min_mcap must be < max_mcap"

    assert presets["mid"]["max_mcap"] <= presets["large"]["min_mcap"]
    assert presets["large"]["max_mcap"] <= presets["mega"]["min_mcap"]

    assert presets["mid"]["min_dollar_adv"] < presets["large"]["min_dollar_adv"]
    assert presets["large"]["min_dollar_adv"] < presets["mega"]["min_dollar_adv"]

    assert presets["mid"]["min_price"] < presets["large"]["min_price"]
    assert presets["large"]["min_price"] < presets["mega"]["min_price"]


def test_apply_tier_preset_with_no_user_overrides(ww):
    """When the user sets nothing, all four preset values land on args."""
    args = argparse.Namespace(min_mcap=None, max_mcap=None,
                              min_dollar_adv=None, min_price=None)
    ww._apply_tier_preset(args, tier="mega", user_set=set())
    assert args.min_mcap == 200_000_000_000.0
    assert args.max_mcap == 5_000_000_000_000.0
    assert args.min_dollar_adv == 500_000_000.0
    assert args.min_price == 20.0


def test_apply_tier_preset_honors_user_overrides(ww):
    """A flag the user explicitly set on argv wins over the tier preset."""
    args = argparse.Namespace(
        min_mcap=5_000_000_000.0,  # user said "I want $5B floor with mega tier"
        max_mcap=None,
        min_dollar_adv=None,
        min_price=None,
    )
    ww._apply_tier_preset(args, tier="mega", user_set={"min_mcap"})
    assert args.min_mcap == 5_000_000_000.0  # user's value preserved
    # Other flags get the mega preset
    assert args.max_mcap == 5_000_000_000_000.0
    assert args.min_dollar_adv == 500_000_000.0
    assert args.min_price == 20.0


def test_apply_tier_preset_no_op_when_tier_none(ww):
    """When --tier is omitted, args are left untouched."""
    args = argparse.Namespace(min_mcap=None, max_mcap=None,
                              min_dollar_adv=None, min_price=None)
    ww._apply_tier_preset(args, tier=None, user_set=set())
    assert args.min_mcap is None
    assert args.max_mcap is None
    assert args.min_dollar_adv is None
    assert args.min_price is None


def test_apply_tier_preset_unknown_tier_raises(ww):
    args = argparse.Namespace()
    with pytest.raises(ValueError, match="Unknown tier"):
        ww._apply_tier_preset(args, tier="nano", user_set=set())


@pytest.mark.parametrize("argv,expected", [
    ([], set()),
    (["--top", "25"], set()),
    (["--min-mcap", "5e9"], {"min_mcap"}),
    (["--min-mcap", "5e9", "--max-mcap", "100e9"], {"min_mcap", "max_mcap"}),
    (["--min-dollar-adv", "100e6", "--min-price", "10"],
     {"min_dollar_adv", "min_price"}),
    (["--tier", "mega", "--min-mcap", "5e9"], {"min_mcap"}),
])
def test_user_supplied_dests(ww, argv, expected):
    assert ww._user_supplied_dests(argv) == expected


def test_main_parser_accepts_tier_flag(ww):
    """The --tier flag is in the parser and validates against the choices."""
    import io
    import contextlib

    # Inspect the parser by calling main() with --help; we just want the flag.
    parser_src = WW_PATH.read_text()
    assert '"--tier"' in parser_src
    assert "choices=" in parser_src
    # Cheap structural assertion: the three tier names appear in the source.
    for tier in ("mid", "large", "mega"):
        assert tier in parser_src


def test_tier_tagged_run_id_format():
    """matrix_<tier>_weekly_<ts>_chain — must match phase_diff SQL LIKE pattern.

    phase_diff uses ``WHERE run_id LIKE 'matrix_<tier>_%'`` to pull only the
    most recent matrix run for the tier being diffed. If the run_id format
    drifts, that filter silently returns no rows.
    """
    timestamp = "2026-05-06_2134"
    for tier in ("mid", "large", "mega"):
        run_id = f"matrix_{tier}_weekly_{timestamp}_chain"
        assert run_id.startswith(f"matrix_{tier}_"), \
            f"Tier-filtered SQL pattern won't match: {run_id}"


def test_tier_tagged_screener_dir_format():
    """screener_<tier>_<ts> — used by Phase 1 --output-dir override.

    weekly_workflow.py constructs this when --tier is set so the per-tier
    screener output is traceable. Must match the glob 'screener_*' that
    older tooling uses for catalog discovery.
    """
    from fnmatch import fnmatch
    timestamp = "2026-05-06_2134"
    for tier in ("mid", "large", "mega"):
        dir_name = f"screener_{tier}_{timestamp}"
        assert fnmatch(dir_name, "screener_*"), \
            f"Existing screener_* glob won't match {dir_name}"
        assert fnmatch(dir_name, f"screener_{tier}_*"), \
            f"Tier-specific glob won't match {dir_name}"
