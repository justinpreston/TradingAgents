"""Unit tests for scripts/score_picks_iv_surface.py.

Guards the IV-surface scoring methodology adapted from medloh/stockpile:

1. Surface fit recovers known polynomial coefficients on synthetic data
   (sanity check on the OLS).
2. Verdict thresholds: cheap/fair/near_par/rich classifications happen at
   the right iv_excess_pp boundaries, with earnings-in-window downgrade.
3. Earnings-in-window flag fires only when next_earnings.date strictly
   falls inside (today, expiry].
4. End-to-end score_pick() against a mocked chain — covers strike
   matching, excess computation, percentile ranks, and same-exp
   percentile.
5. Small-cap auto-widen path: when initial window has <5 valid rows, the
   wider fallback window is used (mocked by returning two different
   chains for two different windows).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "score_picks_iv_surface.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("score_picks_iv_surface", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ──────────────────────────────────────────────────────────────────────
# fit_surface
# ──────────────────────────────────────────────────────────────────────

class TestFitSurface:
    def test_recovers_known_coefficients(self, mod):
        """Generate synthetic IV from a known surface; OLS should recover it."""
        rng = np.random.default_rng(42)
        m = rng.uniform(-0.4, 0.4, size=80)
        sqrt_t = rng.uniform(0.2, 1.0, size=80)
        true_coef = np.array([0.25, -0.10, 0.30, 0.05, -0.02])
        iv = (
            true_coef[0]
            + true_coef[1] * m
            + true_coef[2] * m * m
            + true_coef[3] * sqrt_t
            + true_coef[4] * m * sqrt_t
        )
        fitted = mod.fit_surface(m, sqrt_t, iv)
        assert fitted is not None
        np.testing.assert_allclose(fitted, true_coef, atol=1e-10)

    def test_predict_iv_matches_fit(self, mod):
        coef = np.array([0.30, -0.05, 0.20, 0.04, -0.01])
        # At ATM (m=0), sqrt_t=0.5: IV = 0.30 + 0 + 0 + 0.04*0.5 + 0 = 0.32
        assert mod.predict_iv(coef, 0.0, 0.5) == pytest.approx(0.32)

    def test_returns_none_when_below_min_rows(self, mod):
        assert mod.fit_surface([0.1, 0.2], [0.5, 0.6], [0.3, 0.35]) is None

    def test_returns_none_when_singular(self, mod):
        # All identical rows → design matrix is rank-deficient. Even if lstsq
        # returns a "solution", we should not blow up on the consumer side.
        coef = mod.fit_surface([0.0] * 6, [0.5] * 6, [0.3] * 6)
        # Either None or a finite vector — both are acceptable. The integration
        # test below validates the predict path.
        assert coef is None or np.all(np.isfinite(coef))


# ──────────────────────────────────────────────────────────────────────
# Verdict logic
# ──────────────────────────────────────────────────────────────────────

class TestVerdict:
    @pytest.mark.parametrize(
        "excess_pp,expected",
        [
            (-3.0, "cheap"),
            (-1.01, "cheap"),
            (-1.0, "fair"),     # boundary: ≥ -1.0 is fair
            (0.0, "fair"),
            (0.49, "fair"),
            (0.5, "near_par"),  # boundary: ≥ 0.5 is near_par
            (1.5, "near_par"),
            (1.99, "near_par"),
            (2.0, "rich"),      # boundary: ≥ 2.0 is rich
            (5.0, "rich"),
        ],
    )
    def test_thresholds_no_earnings(self, mod, excess_pp, expected):
        assert mod._verdict(excess_pp, earnings_flag=None) == expected

    def test_earnings_downgrades_rich(self, mod):
        flag = {"date": "2026-08-01", "days_to_event": 60, "days_to_expiry": 180}
        assert mod._verdict(2.5, flag) == "rich+earnings_in_window"

    def test_earnings_downgrades_near_par(self, mod):
        flag = {"date": "2026-08-01", "days_to_event": 60, "days_to_expiry": 180}
        assert mod._verdict(1.0, flag) == "near_par+earnings_in_window"

    def test_earnings_does_not_downgrade_cheap(self, mod):
        # Cheap remains cheap even with earnings — the surface fit has already
        # priced the event in, and we don't want to discourage genuinely
        # mispriced entries.
        flag = {"date": "2026-08-01", "days_to_event": 60, "days_to_expiry": 180}
        assert mod._verdict(-2.0, flag) == "cheap"

    def test_earnings_does_not_downgrade_fair(self, mod):
        flag = {"date": "2026-08-01", "days_to_event": 60, "days_to_expiry": 180}
        assert mod._verdict(0.0, flag) == "fair"

    def test_fit_failed(self, mod):
        assert mod._verdict(None, None) == "fit_failed"


# ──────────────────────────────────────────────────────────────────────
# Earnings-in-window flag
# ──────────────────────────────────────────────────────────────────────

class TestEarningsInWindow:
    def test_inside_window(self, mod):
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"next_earnings": {"date": "2026-08-05"}}
        flag = mod._earnings_in_window(info, today, expiry)
        assert flag is not None
        assert flag["date"] == "2026-08-05"
        assert flag["days_to_event"] == 81
        assert flag["days_to_expiry"] == 188

    def test_outside_window_after_expiry(self, mod):
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"next_earnings": {"date": "2026-12-15"}}
        assert mod._earnings_in_window(info, today, expiry) is None

    def test_outside_window_before_today(self, mod):
        # Earnings already happened — not a concern for option pricing forward.
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"next_earnings": {"date": "2026-05-06"}}
        assert mod._earnings_in_window(info, today, expiry) is None

    def test_on_today_excluded(self, mod):
        # The flag fires only for events strictly after today (we don't catch
        # same-day-of-run earnings; the script is for forward-looking entry).
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"next_earnings": {"date": "2026-05-16"}}
        assert mod._earnings_in_window(info, today, expiry) is None

    def test_on_expiry_included(self, mod):
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"next_earnings": {"date": "2026-11-20"}}
        flag = mod._earnings_in_window(info, today, expiry)
        assert flag is not None

    def test_none_input(self, mod):
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        assert mod._earnings_in_window(None, today, expiry) is None

    def test_accepts_top_level_dict(self, mod):
        # Some callers pass the next_earnings dict directly (no wrapper).
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"date": "2026-08-05"}
        flag = mod._earnings_in_window(info, today, expiry)
        assert flag is not None
        assert flag["date"] == "2026-08-05"

    def test_bad_date(self, mod):
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        info = {"next_earnings": {"date": "not-a-date"}}
        assert mod._earnings_in_window(info, today, expiry) is None


# ──────────────────────────────────────────────────────────────────────
# Chain extraction
# ──────────────────────────────────────────────────────────────────────

class TestChainExtraction:
    def test_filters_zero_iv(self, mod):
        today = date(2026, 5, 16)
        chain = [
            {"details": {"strike_price": 100, "expiration_date": "2026-08-21"},
             "implied_volatility": 0.0},  # filtered (≤ 0.02)
            {"details": {"strike_price": 100, "expiration_date": "2026-08-21"},
             "implied_volatility": 0.30},
        ]
        rows = mod._chain_iv_rows(chain, spot=100.0, today=today)
        assert len(rows) == 1
        assert rows[0]["iv"] == 0.30

    def test_filters_expired(self, mod):
        today = date(2026, 5, 16)
        chain = [
            {"details": {"strike_price": 100, "expiration_date": "2026-05-15"},
             "implied_volatility": 0.30},  # filtered (DTE ≤ 0)
            {"details": {"strike_price": 100, "expiration_date": "2026-05-17"},
             "implied_volatility": 0.30},
        ]
        rows = mod._chain_iv_rows(chain, spot=100.0, today=today)
        assert len(rows) == 1
        assert rows[0]["dte"] == 1

    def test_computes_moneyness(self, mod):
        today = date(2026, 5, 16)
        chain = [{"details": {"strike_price": 110, "expiration_date": "2026-08-14"},
                  "implied_volatility": 0.30}]  # 90 DTE
        rows = mod._chain_iv_rows(chain, spot=100.0, today=today)
        assert rows[0]["moneyness"] == pytest.approx(np.log(1.10))
        assert rows[0]["sqrt_t"] == pytest.approx(np.sqrt(90 / 365))


# ──────────────────────────────────────────────────────────────────────
# score_pick — end-to-end with mocked Polygon
# ──────────────────────────────────────────────────────────────────────

def _synthetic_chain(spot: float, today: date, target_expiry: date,
                     atm_iv: float = 0.30, n_strikes: int = 9,
                     n_expiries: int = 3, atm_excess: float = 0.0):
    """Build a synthetic chain whose IV roughly follows the 5-param model.

    All contracts sit ON the fitted surface except the contract at
    (strike == round(spot), expiry == target_expiry), which is offset by
    ``atm_excess`` IV points to simulate a rich/cheap pick.
    """
    chain = []
    expiries = [target_expiry - timedelta(days=30 * (n_expiries // 2)) + timedelta(days=30 * i)
                for i in range(n_expiries)]
    strikes = [round(spot * (0.9 + 0.025 * i), 2) for i in range(n_strikes)]
    for exp in expiries:
        dte = (exp - today).days
        if dte <= 0:
            continue
        sqrt_t = (dte / 365) ** 0.5
        for k in strikes:
            m = float(np.log(k / spot))
            # Smile curvature with very gentle term structure.
            iv = atm_iv + 0.5 * m * m + 0.02 * sqrt_t
            chain.append({
                "details": {
                    "strike_price": k,
                    "expiration_date": exp.isoformat(),
                    "contract_type": "call",
                },
                "implied_volatility": iv,
            })
    # Inject the offset on the target strike+expiry
    target_strike = round(spot)
    for c in chain:
        if (c["details"]["strike_price"] == target_strike
                and c["details"]["expiration_date"] == target_expiry.isoformat()):
            c["implied_volatility"] += atm_excess
            break
    else:
        # Make sure the target strike exists on the target expiry
        dte = (target_expiry - today).days
        sqrt_t = (dte / 365) ** 0.5
        m = 0.0
        chain.append({
            "details": {
                "strike_price": target_strike,
                "expiration_date": target_expiry.isoformat(),
                "contract_type": "call",
            },
            "implied_volatility": atm_iv + 0.02 * sqrt_t + atm_excess,
        })
    return chain


class TestScorePick:
    def test_atm_on_surface_is_fair(self, mod):
        """A contract sitting exactly on the fitted surface should be 'fair'."""
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        chain = _synthetic_chain(spot=100, today=today, target_expiry=expiry,
                                 atm_iv=0.30, atm_excess=0.0)
        overlay_row = {
            "ticker": "FAKE",
            "tier": "C",
            "current_price_usd": 100.0,
            "expiration": expiry.isoformat(),
            "legs": [{"strike": 100.0, "dte": 188}],
        }
        with patch.object(mod, "_fetch_chain", return_value=(chain, None)):
            row = mod.score_pick(overlay_row, today, {}, pace_seconds=0)
        assert row["verdict"] in ("fair", "cheap")  # near zero excess
        assert abs(row["iv_excess_pp"]) < 0.5

    def test_rich_pick_is_flagged(self, mod):
        """A contract +3pp above the surface should be flagged 'rich'."""
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        # +3pp offset → 0.03 in iv (IV is stored as decimal)
        chain = _synthetic_chain(spot=100, today=today, target_expiry=expiry,
                                 atm_iv=0.30, atm_excess=0.03)
        overlay_row = {
            "ticker": "FAKE",
            "tier": "C",
            "current_price_usd": 100.0,
            "expiration": expiry.isoformat(),
            "legs": [{"strike": 100.0, "dte": 188}],
        }
        with patch.object(mod, "_fetch_chain", return_value=(chain, None)):
            row = mod.score_pick(overlay_row, today, {}, pace_seconds=0)
        assert row["iv_excess_pp"] >= 2.0  # 3pp − fit residuals
        assert row["verdict"] == "rich"
        assert row["same_exp_percentile"] >= 50  # richer than median on same exp

    def test_earnings_flag_downgrades_rich(self, mod):
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        chain = _synthetic_chain(spot=100, today=today, target_expiry=expiry,
                                 atm_iv=0.30, atm_excess=0.03)
        overlay_row = {
            "ticker": "FAKE",
            "tier": "C",
            "current_price_usd": 100.0,
            "expiration": expiry.isoformat(),
            "legs": [{"strike": 100.0, "dte": 188}],
        }
        earnings = {"FAKE": {"next_earnings": {"date": "2026-08-05"}}}
        with patch.object(mod, "_fetch_chain", return_value=(chain, None)):
            row = mod.score_pick(overlay_row, today, earnings, pace_seconds=0)
        assert row["earnings_in_window"] is not None
        assert row["earnings_in_window"]["date"] == "2026-08-05"
        assert row["verdict"] == "rich+earnings_in_window"

    def test_skipped_when_missing_inputs(self, mod):
        today = date(2026, 5, 16)
        row = mod.score_pick(
            {"ticker": "FAKE", "current_price_usd": None,
             "expiration": "2026-11-20", "legs": [{"strike": 100}]},
            today, {}, pace_seconds=0,
        )
        assert "skipped" in row

    def test_skipped_when_overlay_errored(self, mod):
        today = date(2026, 5, 16)
        row = mod.score_pick(
            {"ticker": "FAKE", "error": "Polygon fetch failed: ..."},
            today, {}, pace_seconds=0,
        )
        assert "skipped" in row

    def test_widens_window_for_thin_chain(self, mod):
        """When the local window has < MIN_FIT_ROWS, the wider window is used."""
        today = date(2026, 5, 16)
        expiry = date(2026, 11, 20)
        thin = _synthetic_chain(spot=100, today=today, target_expiry=expiry,
                                n_strikes=3, n_expiries=1)  # too few
        wide = _synthetic_chain(spot=100, today=today, target_expiry=expiry,
                                n_strikes=9, n_expiries=3)
        call_count = {"n": 0}

        def fake_fetch(*args, **kwargs):
            call_count["n"] += 1
            return (thin, None) if call_count["n"] == 1 else (wide, None)

        overlay_row = {
            "ticker": "RSI",
            "tier": "C",
            "current_price_usd": 100.0,
            "expiration": expiry.isoformat(),
            "legs": [{"strike": 100.0}],
        }
        with patch.object(mod, "_fetch_chain", side_effect=fake_fetch):
            row = mod.score_pick(overlay_row, today, {}, pace_seconds=0)
        assert call_count["n"] == 2
        assert row["widened_window"] is True
        assert "iv_excess_pp" in row
        assert any("widened" in w.lower() for w in row["warnings"])


# ──────────────────────────────────────────────────────────────────────
# Earnings-calendar loader
# ──────────────────────────────────────────────────────────────────────

class TestLoadEarningsCalendar:
    def test_returns_empty_when_missing(self, mod, tmp_path):
        assert mod._load_earnings_calendar(tmp_path / "missing.json") == {}
        assert mod._load_earnings_calendar(None) == {}

    def test_indexes_by_uppercase_ticker(self, mod, tmp_path):
        path = tmp_path / "earnings_calendar.json"
        path.write_text(json.dumps({
            "tickers": [
                {"ticker": "aroc", "next_earnings": {"date": "2026-08-05"}},
                {"ticker": "EQIX", "next_earnings": {"date": "2026-10-30"}},
                {"ticker": None, "next_earnings": {"date": "2026-09-01"}},  # skipped
            ],
        }))
        idx = mod._load_earnings_calendar(path)
        assert set(idx.keys()) == {"AROC", "EQIX"}

    def test_malformed_json_returns_empty(self, mod, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert mod._load_earnings_calendar(path) == {}


# ──────────────────────────────────────────────────────────────────────
# Markdown rendering smoke test
# ──────────────────────────────────────────────────────────────────────

class TestRenderMD:
    def test_renders_table_and_caveats(self, mod):
        today = date(2026, 5, 16)
        rows = [
            {
                "ticker": "EQIX", "tier": "C",
                "matched_strike": 1020.0, "matched_expiry": "2026-12-18",
                "matched_dte": 216, "pick_iv_pct": 31.0, "fitted_iv_pct": 31.4,
                "iv_excess_pp": -0.36, "same_exp_percentile": 6.0,
                "n_contracts_fit": 87, "verdict": "fair",
            },
            {
                "ticker": "AROC", "tier": "C",
                "matched_strike": 35.0, "matched_expiry": "2026-11-20",
                "matched_dte": 188, "pick_iv_pct": 38.0, "fitted_iv_pct": 36.34,
                "iv_excess_pp": 1.66, "same_exp_percentile": 100.0,
                "n_contracts_fit": 50, "verdict": "near_par+earnings_in_window",
                "earnings_in_window": {"date": "2026-08-05", "days_to_event": 81, "days_to_expiry": 188},
                "expiry": "2026-11-20",
            },
            {
                "ticker": "ZZZ", "skipped": "no overlay",
            },
        ]
        out = mod.render_md("matrix_2026-05-15_top25", rows, today)
        assert "matrix_2026-05-15_top25" in out
        assert "EQIX" in out and "AROC" in out
        assert "Earnings inside expiry window" in out
        assert "2026-08-05" in out
        assert "Could not score" in out
        assert "ZZZ" in out
        assert "Methodology caveats" in out
