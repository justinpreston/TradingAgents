"""Macro regime snapshot — VIX, SPX, yield curve.

Provides a lightweight, fail-soft snapshot of three high-signal macro
inputs that the cadence docs already name as override triggers:

- VIX level (volatility regime)
- SPX 5-day return (drawdown regime)
- 10y/3m yield-curve spread (recession proxy; optional)

Output classifies into one of three regimes:

- **normal**: no triggers fired.
- **defensive**: any single trigger fired (VIX > defensive_vix or
  SPX 5d <= defensive_spx_5d_pct or yield curve inverted).
- **halt**: both VIX and SPX triggers exceed their *halt* thresholds.

The snapshot is **advisory by default**. ``recommended_action`` is a
soft suggestion (``proceed``, ``review_only``, ``block``) — the
calling pipeline decides whether to honor it.

Data sources
------------
yfinance is the primary source for free-tier compatibility:
- ``^VIX`` — CBOE Volatility Index
- ``^GSPC`` — S&P 500 (with ``SPY`` fallback if unavailable)
- ``^TNX`` and ``^IRX`` — 10y and 3m treasury yields (yields are quoted
  in % already; ``^TNX - ^IRX`` = 10y/3m spread)

Each series is fetched independently and the snapshot is composed from
whatever succeeds. Status flags ``ok`` / ``partial`` / ``unavailable``
indicate completeness.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - bootstrap safety
    yf = None  # type: ignore[assignment]


DEFAULTS = {
    "defensive_vix": 25.0,
    "halt_vix": 35.0,
    "defensive_spx_5d_pct": -5.0,
    "halt_spx_5d_pct": -8.0,
    "yield_curve_inversion_threshold_bps": 0.0,  # negative spread = inverted
}


def _fetch_series(ticker: str, lookback_days: int = 14) -> tuple[list[float], str | None]:
    """Pull recent daily closes for ``ticker`` via yfinance.

    Returns ``(closes_chronological, error)``. ``closes_chronological``
    is at most ``lookback_days+1`` entries with the most recent close
    last. Error is None on success.
    """
    if yf is None:
        return ([], "yfinance not installed")
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{lookback_days}d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        return ([], f"yfinance error: {exc}")
    if hist is None or hist.empty:
        return ([], "no history returned")
    try:
        closes = [float(v) for v in hist["Close"].tolist() if v is not None]
    except Exception as exc:  # noqa: BLE001
        return ([], f"close column malformed: {exc}")
    closes = [c for c in closes if c == c]  # drop NaN
    if not closes:
        return ([], "no usable closes")
    return (closes, None)


def _spx_5d_return_pct(spx_closes: list[float]) -> float | None:
    """Return last 5-trading-day percentage change (most recent vs 5 ago)."""
    if len(spx_closes) < 6:
        return None
    end = spx_closes[-1]
    start = spx_closes[-6]
    if start <= 0:
        return None
    return (end - start) / start * 100.0


def _classify(vix: float | None, spx_5d_pct: float | None,
              yield_curve_bps: float | None,
              thresholds: dict[str, float]) -> tuple[str, list[str]]:
    """Return (regime, triggers).

    Halt requires BOTH VIX > halt_vix AND SPX 5d <= halt_spx_5d_pct (a
    real crash signal, not just one elevated reading). Otherwise a single
    trigger flips to "defensive".
    """
    triggers: list[str] = []
    halt_vix_hit = False
    halt_spx_hit = False
    if vix is not None:
        if vix >= thresholds["halt_vix"]:
            triggers.append(f"VIX {vix:.1f} ≥ halt {thresholds['halt_vix']:.0f}")
            halt_vix_hit = True
        elif vix >= thresholds["defensive_vix"]:
            triggers.append(f"VIX {vix:.1f} ≥ defensive {thresholds['defensive_vix']:.0f}")
    if spx_5d_pct is not None:
        if spx_5d_pct <= thresholds["halt_spx_5d_pct"]:
            triggers.append(
                f"SPX 5d {spx_5d_pct:.1f}% ≤ halt {thresholds['halt_spx_5d_pct']:.0f}%"
            )
            halt_spx_hit = True
        elif spx_5d_pct <= thresholds["defensive_spx_5d_pct"]:
            triggers.append(
                f"SPX 5d {spx_5d_pct:.1f}% ≤ defensive {thresholds['defensive_spx_5d_pct']:.0f}%"
            )
    if (yield_curve_bps is not None
            and yield_curve_bps < thresholds["yield_curve_inversion_threshold_bps"]):
        triggers.append(f"yield curve inverted ({yield_curve_bps:.0f} bps)")

    if halt_vix_hit and halt_spx_hit:
        return ("halt", triggers)
    if triggers:
        return ("defensive", triggers)
    return ("normal", triggers)


def _recommended_action(regime: str) -> str:
    return {
        "normal": "proceed",
        "defensive": "review_only",
        "halt": "block",
    }.get(regime, "proceed")


def build_snapshot(*, today: date | None = None,
                   thresholds: dict[str, float] | None = None,
                   ) -> dict[str, Any]:
    """Build a macro regime snapshot. Always returns a dict; never raises.

    Schema:
    {
      "schema_version": 1,
      "generated_at": "<iso>",
      "as_of_date": "YYYY-MM-DD",
      "status": "ok" | "partial" | "unavailable",
      "warnings": [...],
      "signals": {
         "vix": float | None,
         "spx_5d_return_pct": float | None,
         "spx_5d_close": float | None,
         "yield_curve_10y_3m_bps": float | None,
         "treasury_10y_pct": float | None,
         "treasury_3m_pct": float | None,
      },
      "thresholds": {...},
      "regime": "normal" | "defensive" | "halt",
      "triggers": [...],
      "recommended_action": "proceed" | "review_only" | "block",
    }
    """
    today = today or date.today()
    thr = {**DEFAULTS, **(thresholds or {})}
    warnings: list[str] = []

    vix_closes, err = _fetch_series("^VIX")
    if err:
        warnings.append(f"VIX unavailable: {err}")
    vix = vix_closes[-1] if vix_closes else None

    spx_closes, err = _fetch_series("^GSPC")
    if err:
        warnings.append(f"SPX (^GSPC) unavailable: {err}; trying SPY fallback")
        spx_closes, err2 = _fetch_series("SPY")
        if err2:
            warnings.append(f"SPY also unavailable: {err2}")
    spx_5d = _spx_5d_return_pct(spx_closes) if spx_closes else None
    spx_close = spx_closes[-1] if spx_closes else None

    t10_closes, err = _fetch_series("^TNX")
    t3m_closes, err2 = _fetch_series("^IRX")
    if err:
        warnings.append(f"10y yield (^TNX) unavailable: {err}")
    if err2:
        warnings.append(f"3m yield (^IRX) unavailable: {err2}")
    t10 = t10_closes[-1] if t10_closes else None
    t3m = t3m_closes[-1] if t3m_closes else None
    yc_bps = (t10 - t3m) * 100.0 if (t10 is not None and t3m is not None) else None

    n_signals = sum(x is not None for x in (vix, spx_5d, yc_bps))
    if n_signals == 0:
        status = "unavailable"
    elif n_signals < 3:
        status = "partial"
    else:
        status = "ok"

    regime, triggers = _classify(vix, spx_5d, yc_bps, thr)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of_date": today.isoformat(),
        "status": status,
        "warnings": warnings,
        "signals": {
            "vix": round(vix, 2) if vix is not None else None,
            "spx_5d_return_pct": round(spx_5d, 2) if spx_5d is not None else None,
            "spx_5d_close": round(spx_close, 2) if spx_close is not None else None,
            "yield_curve_10y_3m_bps": round(yc_bps, 1) if yc_bps is not None else None,
            "treasury_10y_pct": round(t10, 3) if t10 is not None else None,
            "treasury_3m_pct": round(t3m, 3) if t3m is not None else None,
        },
        "thresholds": thr,
        "regime": regime,
        "triggers": triggers,
        "recommended_action": _recommended_action(regime),
    }


def render_banner_text(snapshot: dict[str, Any]) -> str:
    """One-line text banner suitable for stdout / launchd logs."""
    regime = snapshot.get("regime", "normal").upper()
    sig = snapshot.get("signals") or {}
    bits = []
    if sig.get("vix") is not None:
        bits.append(f"VIX {sig['vix']:.1f}")
    if sig.get("spx_5d_return_pct") is not None:
        bits.append(f"SPX 5d {sig['spx_5d_return_pct']:+.1f}%")
    if sig.get("yield_curve_10y_3m_bps") is not None:
        bits.append(f"10y/3m {sig['yield_curve_10y_3m_bps']:+.0f}bps")
    body = " · ".join(bits) if bits else "no signals"
    triggers = snapshot.get("triggers") or []
    suffix = ""
    if triggers:
        suffix = f"   ⚠ {'; '.join(triggers)}"
    return f"[macro] regime={regime}   {body}{suffix}"
