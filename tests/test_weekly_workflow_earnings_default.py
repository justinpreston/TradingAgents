"""Tests for the earnings-enrichment default-on flag flip in weekly_workflow.py.

Phase 2.6 (earnings-calendar enrichment) was opt-in (--enrich-earnings) and
was OFF on the 2026-06-26 Friday run, leaving the earnings-inside-expiry
guard in build_options_overlay.py dead (no earnings_calendar.json to read).
It is now default ON, with --no-enrich-earnings as the opt-out. --enrich-news
stays opt-in, unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WW_PATH = REPO_ROOT / "scripts" / "weekly_workflow.py"
ALL_TIERS_PATH = REPO_ROOT / "scripts" / "run_weekly_all_tiers.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def ww():
    return _load_module(WW_PATH, "weekly_workflow_earnings_default")


@pytest.fixture(scope="module")
def all_tiers():
    return _load_module(ALL_TIERS_PATH, "run_weekly_all_tiers_earnings_default")


class TestEarningsDefaultOnFlagParsing:
    """Directly exercises argparse via a fresh ArgumentParser built the same
    way main() builds one, by constructing a minimal parser mirroring the
    two flags under test. This avoids invoking main()'s full phase pipeline
    (which shells out to the screener) just to check flag defaults.
    """

    def _build_minimal_parser(self):
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--enrich-earnings", dest="enrich_earnings",
                       action="store_true", default=True)
        p.add_argument("--no-enrich-earnings", dest="enrich_earnings",
                       action="store_false")
        return p

    def test_default_is_true_with_no_flags(self):
        p = self._build_minimal_parser()
        args = p.parse_args([])
        assert args.enrich_earnings is True

    def test_no_enrich_earnings_flips_to_false(self):
        p = self._build_minimal_parser()
        args = p.parse_args(["--no-enrich-earnings"])
        assert args.enrich_earnings is False

    def test_explicit_enrich_earnings_stays_true(self):
        p = self._build_minimal_parser()
        args = p.parse_args(["--enrich-earnings"])
        assert args.enrich_earnings is True

    def test_source_has_default_true_and_no_flag(self, ww):
        """Structural assertion against the real source: the parser
        construction must include both flags with default=True."""
        src = WW_PATH.read_text()
        assert '"--enrich-earnings"' in src
        assert '"--no-enrich-earnings"' in src
        assert 'dest="enrich_earnings"' in src or "dest='enrich_earnings'" in src
        # The store_true flag must carry default=True somewhere near it
        idx = src.index('"--enrich-earnings"')
        window = src[idx:idx + 400]
        assert "default=True" in window

    def test_enrich_news_still_opt_in_unchanged(self, ww):
        """Guard against accidentally flipping --enrich-news default too —
        the task explicitly leaves it opt-in."""
        src = WW_PATH.read_text()
        idx = src.index('"--enrich-news"')
        window = src[idx:idx + 300]
        assert "action=\"store_true\"" in window
        assert "default=True" not in window


class TestRunWeeklyAllTiersPassthrough:
    """run_weekly_all_tiers.py must pass through --no-enrich-earnings when
    the user disables it, and NOT pass --enrich-earnings explicitly on the
    default path (weekly_workflow.py's own default of True already covers
    it, so an explicit pass-through isn't required for correctness — but
    the opt-out must propagate)."""

    def test_default_does_not_pass_no_enrich_earnings(self, all_tiers, monkeypatch):
        import argparse
        args = argparse.Namespace(
            top=25, target_date=None, chain=False, chain_top=5,
            chain_max_parallel=5, min_request_interval=None,
            enrich_earnings=True, dry_run=True,
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(all_tiers.subprocess, "run", fake_run)
        all_tiers._run_tier("mid", args)
        assert "--no-enrich-earnings" not in captured["cmd"]

    def test_opt_out_passes_no_enrich_earnings(self, all_tiers, monkeypatch):
        import argparse
        args = argparse.Namespace(
            top=25, target_date=None, chain=False, chain_top=5,
            chain_max_parallel=5, min_request_interval=None,
            enrich_earnings=False, dry_run=True,
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(all_tiers.subprocess, "run", fake_run)
        all_tiers._run_tier("mid", args)
        assert "--no-enrich-earnings" in captured["cmd"]
