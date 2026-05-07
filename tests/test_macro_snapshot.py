"""Tests for tradingagents.dataflows.macro_snapshot + the CLI builder."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tradingagents.dataflows import macro_snapshot as ms


class _StubTicker:
    """Minimal yfinance.Ticker stand-in: returns whatever .history() is set to."""

    def __init__(self, closes: list[float] | None):
        self._closes = closes

    def history(self, period: str | None = None, auto_adjust: bool = False):  # noqa: D401, ARG002
        if self._closes is None:
            raise RuntimeError("vendor failure")

        class _DF:
            def __init__(self, closes):
                self._closes = list(closes)
                self.empty = len(closes) == 0

            def __getitem__(self, key):
                if key == "Close":
                    return self
                raise KeyError(key)

            def tolist(self):
                return list(self._closes)

        return _DF(self._closes)


def _yf_with(map_: dict[str, list[float] | None]):
    """Return a fake ``yf`` module with an injected Ticker map."""
    class _FakeYf:
        @staticmethod
        def Ticker(symbol: str):  # noqa: N802 - matches yfinance API
            return _StubTicker(map_.get(symbol))

    return _FakeYf


# ── Pure helpers ──────────────────────────────────────────────────────


def test_spx_5d_return_pct_basic():
    closes = [100.0, 101.0, 102.0, 99.0, 98.0, 95.0, 95.0]
    # last = 95, 5 ago = 101 → (95-101)/101 ≈ -5.94%
    out = ms._spx_5d_return_pct(closes)
    assert out is not None
    assert -6.5 < out < -5.5


def test_spx_5d_return_pct_short_history_returns_none():
    assert ms._spx_5d_return_pct([100.0, 101.0]) is None
    assert ms._spx_5d_return_pct([]) is None


def test_classify_normal():
    regime, triggers = ms._classify(
        vix=15.0, spx_5d_pct=1.0, yield_curve_bps=120.0,
        thresholds=ms.DEFAULTS,
    )
    assert regime == "normal"
    assert triggers == []


def test_classify_defensive_on_vix_alone():
    regime, triggers = ms._classify(
        vix=27.0, spx_5d_pct=0.0, yield_curve_bps=80.0,
        thresholds=ms.DEFAULTS,
    )
    assert regime == "defensive"
    assert any("VIX" in t for t in triggers)


def test_classify_defensive_on_yield_curve_inversion():
    regime, triggers = ms._classify(
        vix=15.0, spx_5d_pct=0.0, yield_curve_bps=-25.0,
        thresholds=ms.DEFAULTS,
    )
    assert regime == "defensive"
    assert any("yield curve" in t for t in triggers)


def test_classify_halt_requires_both_vix_and_spx():
    regime, _ = ms._classify(
        vix=40.0, spx_5d_pct=-10.0, yield_curve_bps=0.0,
        thresholds=ms.DEFAULTS,
    )
    assert regime == "halt"


def test_classify_single_halt_signal_is_only_defensive():
    # Only VIX past halt; SPX still normal → defensive, not halt
    regime, _ = ms._classify(
        vix=40.0, spx_5d_pct=0.0, yield_curve_bps=0.0,
        thresholds=ms.DEFAULTS,
    )
    assert regime == "defensive"


def test_classify_custom_thresholds():
    custom = {**ms.DEFAULTS, "defensive_vix": 18.0}
    regime, triggers = ms._classify(
        vix=20.0, spx_5d_pct=0.0, yield_curve_bps=80.0,
        thresholds=custom,
    )
    assert regime == "defensive"


def test_recommended_action_mapping():
    assert ms._recommended_action("normal") == "proceed"
    assert ms._recommended_action("defensive") == "review_only"
    assert ms._recommended_action("halt") == "block"
    assert ms._recommended_action("???") == "proceed"


# ── End-to-end build_snapshot ─────────────────────────────────────────


def test_build_snapshot_normal_when_all_clean(monkeypatch):
    # 14 days of flat ^GSPC = 100, VIX=15, yields making positive curve
    monkeypatch.setattr(
        ms, "yf",
        _yf_with({
            "^VIX": [15.0] * 14,
            "^GSPC": [100.0] * 14,
            "^TNX": [4.5] * 14,
            "^IRX": [3.5] * 14,
        }),
    )
    snap = ms.build_snapshot(today=date(2026, 5, 8))
    assert snap["status"] == "ok"
    assert snap["regime"] == "normal"
    assert snap["recommended_action"] == "proceed"
    assert snap["signals"]["vix"] == 15.0
    assert abs(snap["signals"]["yield_curve_10y_3m_bps"] - 100.0) < 0.5


def test_build_snapshot_defensive_on_high_vix(monkeypatch):
    monkeypatch.setattr(
        ms, "yf",
        _yf_with({
            "^VIX": [27.0] * 14,
            "^GSPC": [100.0] * 14,
            "^TNX": [4.5] * 14,
            "^IRX": [3.5] * 14,
        }),
    )
    snap = ms.build_snapshot(today=date(2026, 5, 8))
    assert snap["regime"] == "defensive"
    assert snap["recommended_action"] == "review_only"
    assert snap["triggers"]


def test_build_snapshot_partial_when_vix_unavailable(monkeypatch):
    monkeypatch.setattr(
        ms, "yf",
        _yf_with({
            "^VIX": None,  # vendor error
            "^GSPC": [100.0] * 14,
            "^TNX": [4.5] * 14,
            "^IRX": [3.5] * 14,
        }),
    )
    snap = ms.build_snapshot(today=date(2026, 5, 8))
    assert snap["status"] == "partial"
    assert snap["signals"]["vix"] is None
    assert any("VIX unavailable" in w for w in snap["warnings"])


def test_build_snapshot_unavailable_when_all_fail(monkeypatch):
    monkeypatch.setattr(
        ms, "yf",
        _yf_with({"^VIX": None, "^GSPC": None, "SPY": None,
                  "^TNX": None, "^IRX": None}),
    )
    snap = ms.build_snapshot(today=date(2026, 5, 8))
    assert snap["status"] == "unavailable"
    assert snap["regime"] == "normal"  # fallback when no data


def test_build_snapshot_falls_back_to_spy(monkeypatch):
    monkeypatch.setattr(
        ms, "yf",
        _yf_with({
            "^VIX": [15.0] * 14,
            "^GSPC": None,
            "SPY": [100.0, 101.0, 102.0, 99.0, 98.0, 90.0, 90.0],
            "^TNX": [4.5] * 14,
            "^IRX": [3.5] * 14,
        }),
    )
    snap = ms.build_snapshot(today=date(2026, 5, 8))
    assert snap["signals"]["spx_5d_return_pct"] is not None
    # 90 vs 101 is -10.9%, well into halt territory for SPX
    assert snap["signals"]["spx_5d_return_pct"] < -10.0


def test_render_banner_text_includes_regime_and_signals():
    snap = {
        "regime": "defensive",
        "signals": {
            "vix": 27.4,
            "spx_5d_return_pct": -3.8,
            "yield_curve_10y_3m_bps": -72.0,
        },
        "triggers": ["VIX 27.4 ≥ defensive 25"],
    }
    out = ms.render_banner_text(snap)
    assert "DEFENSIVE" in out
    assert "VIX 27.4" in out
    assert "10y/3m" in out
    assert "VIX 27.4 ≥ defensive 25" in out


# ── CLI builder ───────────────────────────────────────────────────────


def test_cli_writes_json(tmp_path: Path, monkeypatch):
    from scripts import build_macro_snapshot as bms

    fake_snap = {
        "schema_version": 1,
        "regime": "normal",
        "signals": {"vix": 15.0},
        "triggers": [],
        "recommended_action": "proceed",
    }
    monkeypatch.setattr(bms, "build_snapshot",
                        lambda **kw: dict(fake_snap))

    out = tmp_path / "macro.json"
    rc = bms._run(type("A", (), {
        "output": str(out),
        "vix_defensive": 25.0,
        "vix_halt": 35.0,
        "spx_defensive_pct": -5.0,
        "spx_halt_pct": -8.0,
        "yc_inversion_bps": 0.0,
        "quiet": True,
    })())
    assert rc == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["regime"] == "normal"
