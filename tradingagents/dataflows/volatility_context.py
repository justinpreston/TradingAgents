"""Volatility context for the options overlay.

Computes historical-volatility (HV) rank and IV/RV premium for a ticker
using free-tier Polygon daily underlying aggregates. We deliberately do
NOT call this "IV rank" — true IV rank requires per-day historical
implied vol from the option chain, which is cost-prohibitive on free
tier. What we ship is:

- `hv_30d`              — 30-trading-day annualized realized vol.
- `hv_rank_252d`        — 0..100 rank of `hv_30d` against trailing 252
                          observations of the same metric. (current −
                          min) / (max − min) × 100.
- `hv_percentile_252d`  — 0..100 percentile (fraction strictly below) of
                          `hv_30d` in the trailing 252 observations.
- `current_iv`          — caller-supplied implied vol from the overlay's
                          chosen contract (annualized decimal).
- `iv_rv_ratio`         — `current_iv / hv_30d` if HV present.
- `iv_minus_rv`         — `current_iv − hv_30d` if HV present.
- `long_call_backdrop`  — qualitative: favorable / mixed / unfavorable.
- `status`              — ok / insufficient_history / fetch_error.

Going forward, `index_runs.py` (or a follow-up) can persist
`current_iv` per run and synthesize a true local IV-rank from the
accumulated archive without burning extra Polygon calls.

Design notes:
- We need ≈ 282 trading observations to compute 252 rolling-RV samples
  with a 30-day window, which is ~400 calendar days of bars.
- Fail-soft: any fetch error returns `{"status": "fetch_error", ...}`
  so the overlay still emits a strategy.
- Module-level cache keyed on (ticker, today, lookback_days) so repeat
  calls within one process don't refetch.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from typing import Any

from tradingagents.dataflows.polygon_common import _make_request

_TRADING_DAYS_PER_YEAR = 252.0
_RV_WINDOW_DAYS = 30
_RANK_LOOKBACK_OBSERVATIONS = 252
_DEFAULT_LOOKBACK_CALENDAR_DAYS = 400

_HV_CACHE: dict[tuple[str, str, int], dict[str, Any]] = {}


def reset_cache() -> None:
    """Clear the module-level HV cache (test seam)."""
    _HV_CACHE.clear()


def _fetch_daily_aggs(ticker: str, today: date, lookback_days: int,
                      max_retries: int = 3) -> tuple[list[dict], str | None]:
    """Pull daily aggregates from Polygon for the trailing ``lookback_days``.

    Returns ``(bars, error)``. ``bars`` is a list of dicts with at least
    ``c`` (close) and ``t`` (timestamp ms). ``error`` is ``None`` on
    success or a string on failure.
    """
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()
    path = f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}
    last_err: str | None = None
    for attempt in range(max_retries):
        try:
            r = _make_request(path, params)
            results = r.get("results", []) or []
            return results, None
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            msg = last_err.lower()
            if ("rate" in msg or "429" in msg) and attempt < max_retries - 1:
                time.sleep(70)
                continue
            return [], last_err
    return [], last_err


def _compute_log_returns(closes: list[float]) -> list[float]:
    if len(closes) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            continue
        out.append(math.log(curr / prev))
    return out


def _rolling_realized_vol(log_returns: list[float],
                          window: int = _RV_WINDOW_DAYS) -> list[float]:
    """Return list of annualized realized-vol values, one per window-end."""
    if len(log_returns) < window:
        return []
    out: list[float] = []
    for i in range(window, len(log_returns) + 1):
        sample = log_returns[i - window:i]
        n = len(sample)
        if n < 2:
            continue
        mean = sum(sample) / n
        var = sum((r - mean) ** 2 for r in sample) / (n - 1)
        std = math.sqrt(var)
        out.append(std * math.sqrt(_TRADING_DAYS_PER_YEAR))
    return out


def _rank_pct(value: float, history: list[float]) -> tuple[float, float]:
    """Return ``(rank_pct, percentile_pct)`` of ``value`` vs ``history``.

    rank_pct is the standard volatility-rank formula:
      (current − min) / (max − min) × 100, clamped to [0, 100].
    percentile_pct is the fraction of history strictly below value, ×100.
    """
    if not history:
        return (0.0, 0.0)
    lo = min(history)
    hi = max(history)
    if hi <= lo:
        rank = 50.0
    else:
        rank = max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))
    below = sum(1 for v in history if v < value)
    pct = below / len(history) * 100.0
    return (rank, pct)


def _backdrop_label(hv_rank: float | None, iv_rv_ratio: float | None,
                    earnings_window_warning: bool) -> str:
    """Map HV rank + IV/RV premium into a long-call backdrop verdict.

    - favorable:    HV rank low (<35) AND IV/RV ratio modest (<1.30) AND
                    no earnings warning.
    - unfavorable:  IV/RV ratio elevated (>1.50) OR HV rank > 75.
    - mixed:        anything else (including missing inputs).
    """
    if earnings_window_warning:
        # Earnings overrides — IV crush risk dominates
        return "mixed"
    if iv_rv_ratio is not None and iv_rv_ratio > 1.50:
        return "unfavorable"
    if hv_rank is not None and hv_rank > 75:
        return "unfavorable"
    if (hv_rank is not None and hv_rank < 35
            and iv_rv_ratio is not None and iv_rv_ratio < 1.30):
        return "favorable"
    return "mixed"


def compute_vol_context(ticker: str, *, today: date | None = None,
                        current_iv: float | None = None,
                        earnings_window_warning: bool = False,
                        lookback_days: int = _DEFAULT_LOOKBACK_CALENDAR_DAYS,
                        ) -> dict[str, Any]:
    """Compute HV rank, IV/RV ratio, and a long-call backdrop label.

    Args:
      ticker: underlying symbol.
      today: anchor date. Defaults to ``date.today()``.
      current_iv: implied vol of the chosen long-call contract (decimal,
        annualized). When ``None``, IV/RV fields are omitted.
      earnings_window_warning: True when the chosen expiry sits within
        ±7 days of next earnings — feeds the backdrop label.
      lookback_days: calendar days of underlying history to fetch.

    Returns a dict with at minimum ``status`` and (when status=="ok")
    the full set of vol_context fields. Always returns a dict; never
    raises on data issues.
    """
    today = today or date.today()
    cache_key = (ticker.upper(), today.isoformat(), lookback_days)
    cached = _HV_CACHE.get(cache_key)
    if cached is None:
        bars, err = _fetch_daily_aggs(ticker, today, lookback_days)
        if err:
            cached = {"status": "fetch_error", "error": err}
        else:
            closes = [b.get("c") for b in bars if b.get("c") is not None]
            log_returns = _compute_log_returns(closes)
            rv_series = _rolling_realized_vol(log_returns)
            if not rv_series:
                cached = {
                    "status": "insufficient_history",
                    "n_bars": len(bars),
                    "n_log_returns": len(log_returns),
                }
            else:
                hv_30d = rv_series[-1]
                history = rv_series[-(_RANK_LOOKBACK_OBSERVATIONS + 1):-1]
                if len(history) < 30:
                    cached = {
                        "status": "insufficient_history",
                        "n_bars": len(bars),
                        "n_rv_observations": len(rv_series),
                        "hv_30d": round(hv_30d, 4),
                    }
                else:
                    hv_rank, hv_pct = _rank_pct(hv_30d, history)
                    cached = {
                        "status": "ok",
                        "hv_30d": round(hv_30d, 4),
                        "hv_rank_252d": round(hv_rank, 1),
                        "hv_percentile_252d": round(hv_pct, 1),
                        "n_rv_observations": len(history),
                        "rv_history_window_days": _RV_WINDOW_DAYS,
                    }
        _HV_CACHE[cache_key] = cached

    out = dict(cached)
    if out.get("status") == "ok" and current_iv is not None and current_iv > 0:
        hv = out["hv_30d"]
        if hv > 0:
            out["current_iv"] = round(current_iv, 4)
            out["iv_rv_ratio"] = round(current_iv / hv, 3)
            out["iv_minus_rv"] = round(current_iv - hv, 4)
        else:
            out["current_iv"] = round(current_iv, 4)
    elif current_iv is not None and current_iv > 0:
        out["current_iv"] = round(current_iv, 4)

    out["long_call_backdrop"] = _backdrop_label(
        out.get("hv_rank_252d"),
        out.get("iv_rv_ratio"),
        earnings_window_warning,
    )
    return out


def render_vol_advisory(vol: dict[str, Any]) -> str | None:
    """Compose a one-line advisory string for trade_synthesis / HTML.

    Returns ``None`` when no useful vol data is available.
    """
    status = vol.get("status")
    if status == "fetch_error":
        return None
    if status == "insufficient_history":
        return None
    backdrop = vol.get("long_call_backdrop", "mixed")
    parts: list[str] = []
    parts.append(f"Long-call backdrop: {backdrop}")
    if vol.get("hv_rank_252d") is not None:
        parts.append(f"HV rank {vol['hv_rank_252d']:.0f}")
    if vol.get("iv_rv_ratio") is not None:
        parts.append(f"IV/RV {vol['iv_rv_ratio']:.2f}×")
    elif vol.get("hv_30d") is not None:
        parts.append(f"30d HV {vol['hv_30d'] * 100:.0f}%")
    return " · ".join(parts)
