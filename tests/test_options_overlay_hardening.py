"""Reliability-hardening tests for scripts/build_options_overlay.py.

Covers the fixes made after the 2026-06-26 Friday run silently produced a
total-failure options overlay (every pick's chain fetch failed on a
transient Polygon 403, and the script still exited 0):

1. main() exits nonzero when picks_analyzed > 0 but strategies_built == 0.
2. _fetch_chain retries on 403/NOT_AUTHORIZED (transient entitlement blips)
   in addition to 429 rate limiting, with a short backoff.
3. options_overlay.json carries a top-level `generated_at` ISO-8601 (UTC,
   seconds precision) timestamp.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from tradingagents.dataflows import polygon_options as polygon_options_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_options_overlay.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_options_overlay_hardening", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def overlay_mod():
    return _load_module()


# ---------------------------------------------------------------------------
# 403 / NOT_AUTHORIZED retry classification
# ---------------------------------------------------------------------------

class TestAuthBlipRetryClassification:

    @pytest.mark.parametrize("msg", [
        "403 Client Error",
        "NOT_AUTHORIZED: you are not entitled to this data",
        "Forbidden (403)",
        "not authorized for this endpoint",
    ])
    def test_classifies_auth_blip(self, overlay_mod, msg):
        assert overlay_mod._is_auth_blip_error(msg.lower()) is True

    @pytest.mark.parametrize("msg", [
        "connection timed out",
        "500 internal server error",
        "invalid ticker",
    ])
    def test_does_not_misclassify_other_errors(self, overlay_mod, msg):
        assert overlay_mod._is_auth_blip_error(msg.lower()) is False

    def test_rate_limit_classification_unchanged(self, overlay_mod):
        assert overlay_mod._is_rate_limit_error("429 too many requests") is True
        assert overlay_mod._is_rate_limit_error("rate limit exceeded") is True
        assert overlay_mod._is_rate_limit_error("403 forbidden") is False

    # _fetch_chain itself now lives in tradingagents.dataflows.polygon_options
    # (build_options_overlay._fetch_chain is an imported alias for it), so
    # these three tests patch _make_request/time.sleep on the real owning
    # module rather than on overlay_mod's copy of the name.

    def test_fetch_chain_retries_on_403_then_succeeds(self, overlay_mod, monkeypatch):
        calls = {"n": 0}

        def fake_request(path, params):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("403 NOT_AUTHORIZED")
            return {"results": [{"details": {"ticker": "FOO"}}]}

        sleeps = []
        monkeypatch.setattr(polygon_options_mod, "_make_request", fake_request)
        monkeypatch.setattr(polygon_options_mod.time, "sleep", lambda s: sleeps.append(s))

        contracts, err = overlay_mod._fetch_chain(
            "FOO", "call", 10.0, 20.0, "2026-01-01", "2026-06-01",
        )
        assert err is None
        assert len(contracts) == 1
        assert calls["n"] == 2
        # Short backoff (not the 70s rate-limit wait)
        assert sleeps == [10.0]

    def test_fetch_chain_gives_up_after_max_auth_retries(self, overlay_mod, monkeypatch):
        calls = {"n": 0}

        def fake_request(path, params):
            calls["n"] += 1
            raise RuntimeError("403 NOT_AUTHORIZED")

        monkeypatch.setattr(polygon_options_mod, "_make_request", fake_request)
        monkeypatch.setattr(polygon_options_mod.time, "sleep", lambda s: None)

        contracts, err = overlay_mod._fetch_chain(
            "FOO", "call", 10.0, 20.0, "2026-01-01", "2026-06-01",
            max_retries=5,
        )
        assert contracts == []
        assert err is not None
        assert "403" in err
        # 1 initial + 2 auth retries = 3 total attempts, then gives up
        # (even though max_retries=5 would otherwise allow more)
        assert calls["n"] == 3

    def test_fetch_chain_still_retries_429(self, overlay_mod, monkeypatch):
        calls = {"n": 0}

        def fake_request(path, params):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("429 rate limited")
            return {"results": []}

        sleeps = []
        monkeypatch.setattr(polygon_options_mod, "_make_request", fake_request)
        monkeypatch.setattr(polygon_options_mod.time, "sleep", lambda s: sleeps.append(s))

        contracts, err = overlay_mod._fetch_chain(
            "FOO", "call", 10.0, 20.0, "2026-01-01", "2026-06-01",
        )
        assert err is None
        assert sleeps == [70]


# ---------------------------------------------------------------------------
# main() exit-code behavior
# ---------------------------------------------------------------------------

def _write_ledger(matrix_run: Path, rows: list[dict]) -> None:
    matrix_run.mkdir(parents=True, exist_ok=True)
    (matrix_run / "verdict_ledger.json").write_text(json.dumps({
        "run_id": matrix_run.name,
        "rows": rows,
    }), encoding="utf-8")


def _pick_row(ticker: str = "FOO") -> dict:
    return {
        "ticker": ticker,
        "name": f"{ticker} Corp",
        "classification": "PICK",
        "current_price_usd": 100.0,
        "aggressive_pt": 130.0,
        "conservative_pt": 125.0,
        "aggressive_horizon": "6-12 months",
        "pt_compression_pct": 3.0,
    }


class TestMainExitCode:

    def test_exits_1_when_zero_strategies_built(self, tmp_path, monkeypatch):
        matrix_run = tmp_path / "matrix_test"
        _write_ledger(matrix_run, [_pick_row("FOO"), _pick_row("BAR")])

        mod = _load_module()
        monkeypatch.setattr(
            mod, "_fetch_chain",
            lambda *a, **k: ([], "Polygon fetch failed: 403 NOT_AUTHORIZED"),
        )
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            sys, "argv",
            ["build_options_overlay.py", "--matrix-run", str(matrix_run)],
        )
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1

        out_json = json.loads((matrix_run / "options_overlay.json").read_text())
        assert out_json["picks_analyzed"] == 2
        assert out_json["strategies_built"] == 0

    def test_exits_0_when_at_least_one_strategy_built(self, tmp_path, monkeypatch):
        matrix_run = tmp_path / "matrix_test2"
        _write_ledger(matrix_run, [_pick_row("FOO")])

        mod = _load_module()

        def fake_build_overlay(row, today, risk_free, min_oi, **kwargs):
            return {
                "ticker": row["ticker"],
                "name": row.get("name"),
                "current_price_usd": row["current_price_usd"],
                "aggressive_pt": row["aggressive_pt"],
                "conservative_pt": row.get("conservative_pt"),
                "aggressive_rating": "Overweight",
                "conservative_rating": "Hold",
                "horizon": row.get("aggressive_horizon"),
                "tier": "A",
                "compression_pct": row.get("pt_compression_pct"),
                "n_contracts_in_window": 5,
                "strategy": "long_call",
                "strategy_mode": "long-call",
                "expiration": "2026-12-18",
                "dte": 200,
                "horizon_": None,
                "legs": [{
                    "side": "long", "symbol": "FOO261218C00100000", "type": "call",
                    "strike": 100.0, "expiration": "2026-12-18", "dte": 200,
                    "iv": 0.4, "open_interest": 500, "delta": 0.55, "theta": -0.02,
                    "vega": 0.1, "price": 12.0, "price_source": "mid",
                    "bid": 11.8, "ask": 12.2, "last_trade": 12.0,
                }],
                "net_debit_per_share": 12.0,
                "net_debit_per_contract": 1200.0,
                "spread_width": None,
                "max_profit_per_contract": None,
                "max_loss_per_contract": 1200.0,
                "breakeven_underlying": 112.0,
                "breakeven_pct_from_current": 12.0,
                "risk_reward": None,
                "upside_to_short_strike_pct": None,
                "liquidity_warnings": None,
                "earnings_note": None,
                "vol_context": {},
                "long_call_delta": 0.55,
                "long_call_moneyness": "ATM",
                "approx_gain_at_aggr_pt_per_contract": 1800.0,
                "approx_gain_at_cons_pt_per_contract": 1300.0,
            }

        monkeypatch.setattr(mod, "_build_per_ticker_overlay",
                             lambda row, today, rf, min_oi, **kw: fake_build_overlay(row, today, rf, min_oi, **kw))
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            sys, "argv",
            ["build_options_overlay.py", "--matrix-run", str(matrix_run)],
        )
        # main() should return normally (no SystemExit) when >=1 strategy built
        mod.main()

        out_json = json.loads((matrix_run / "options_overlay.json").read_text())
        assert out_json["picks_analyzed"] == 1
        assert out_json["strategies_built"] == 1

    def test_missing_ledger_still_exits_2(self, tmp_path, monkeypatch):
        """Regression guard: the pre-existing missing-ledger exit(2) must be untouched."""
        mod = _load_module()
        monkeypatch.setattr(
            sys, "argv",
            ["build_options_overlay.py", "--matrix-run", str(tmp_path / "does_not_exist")],
        )
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# generated_at timestamp
# ---------------------------------------------------------------------------

class TestGeneratedAtTimestamp:

    def test_generated_at_present_and_iso8601_utc(self, tmp_path, monkeypatch):
        matrix_run = tmp_path / "matrix_gen_at"
        _write_ledger(matrix_run, [_pick_row("FOO")])

        mod = _load_module()
        monkeypatch.setattr(mod, "_fetch_chain", lambda *a, **k: ([], "boom"))
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            sys, "argv",
            ["build_options_overlay.py", "--matrix-run", str(matrix_run)],
        )
        with pytest.raises(SystemExit):
            mod.main()

        out_json = json.loads((matrix_run / "options_overlay.json").read_text())
        assert "generated_at" in out_json
        # Must parse as an ISO-8601 UTC timestamp with seconds precision
        parsed = datetime.fromisoformat(out_json["generated_at"])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert parsed.microsecond == 0
        # snapshot_date remains the date-granular field (unchanged contract)
        assert out_json["snapshot_date"] == date.today().isoformat()
