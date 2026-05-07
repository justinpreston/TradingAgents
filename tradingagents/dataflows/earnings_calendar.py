"""Earnings calendar data — next earnings date + 8Q surprise history.

Used by:
- ``scripts/build_earnings_calendar.py`` to write
  ``earnings_calendar.json`` next to a screener or matrix run.
- ``scripts/build_options_overlay.py`` to push expiry past an upcoming
  earnings print when horizon allows (avoids long-call IV crush).
- ``tradingagents/dataflows/news_enrichment_loader.py`` to add a
  ``Next earnings in N days`` line to the news_analyst prefix.

Design:
- yfinance is the primary (free) source. The Polygon paid-tier endpoint
  is structured here as an optional second source — when callers pass a
  ``polygon_client`` we'll prefer Polygon's confirmed dates.
- All public functions are **fail-soft**: any vendor error returns
  ``None`` rather than raising. Earnings data is optional context — a
  missing date should never break a matrix run.
- Module-level cache keyed by ``(ticker, function_name)`` so a single
  ``build_earnings_calendar.py`` invocation hits each ticker at most
  once even if both ``get_next_earnings`` and ``get_earnings_history``
  are called.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

try:  # yfinance is already a transitive dep via stockstats_utils
    import yfinance as yf
except ImportError:  # pragma: no cover - bootstrap safety only
    yf = None  # type: ignore[assignment]

_CACHE: dict[tuple[str, str], Any] = {}


def _cache_get(ticker: str, kind: str) -> Any | None:
    return _CACHE.get((ticker.upper(), kind))


def _cache_put(ticker: str, kind: str, value: Any) -> None:
    _CACHE[(ticker.upper(), kind)] = value


def reset_cache() -> None:
    """Clear the module-level cache (test helper)."""
    _CACHE.clear()


def _coerce_date(value: Any) -> date | None:
    """Convert a yfinance / Polygon datetime field to a plain ``date``.

    yfinance returns various shapes (``Timestamp``, ``datetime``, ``date``,
    ``str``). Polygon returns ISO strings. Anything we can't parse becomes
    ``None`` so callers can fail-soft.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def _trading_days_between(start: date, end: date) -> int:
    """Approximate trading-day count via ``pd.bdate_range`` (excludes
    weekends, but not US holidays — close enough for a "is the expiry
    near earnings?" check).
    """
    if start > end:
        start, end = end, start
    return max(0, len(pd.bdate_range(start, end)) - 1)


def get_next_earnings(ticker: str, *, today: date | None = None) -> dict | None:
    """Return ``{date, days_away, trading_days_away, source, confirmed}`` or
    None if unavailable / past."""
    cached = _cache_get(ticker, "next_earnings")
    if cached is not None:
        return cached if cached else None

    if yf is None:
        _cache_put(ticker, "next_earnings", False)
        return None

    today = today or date.today()
    nxt: date | None = None
    confirmed = False

    try:
        t = yf.Ticker(ticker.upper())
        cal = getattr(t, "calendar", None)
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, list) and ed:
                nxt = _coerce_date(ed[0])
                confirmed = len(ed) == 1
        elif isinstance(cal, pd.DataFrame) and not cal.empty:
            row = cal.loc["Earnings Date"] if "Earnings Date" in cal.index else None
            if row is not None and len(row):
                nxt = _coerce_date(row.iloc[0])
                confirmed = len(row) == 1

        if nxt is None:
            ed_df = getattr(t, "earnings_dates", None)
            if isinstance(ed_df, pd.DataFrame) and not ed_df.empty:
                future = ed_df.index[ed_df.index.date >= today]
                if len(future):
                    nxt = future.min().date()
                    confirmed = False
    except Exception:  # noqa: BLE001 - vendor errors are expected
        _cache_put(ticker, "next_earnings", False)
        return None

    if not nxt or nxt < today:
        _cache_put(ticker, "next_earnings", False)
        return None

    payload = {
        "date": nxt.isoformat(),
        "days_away": (nxt - today).days,
        "trading_days_away": _trading_days_between(today, nxt),
        "source": "yfinance",
        "confirmed": bool(confirmed),
    }
    _cache_put(ticker, "next_earnings", payload)
    return payload


def get_earnings_history(ticker: str, *, max_quarters: int = 8) -> list[dict] | None:
    """Return last ``max_quarters`` of {quarter, eps_estimate, eps_actual,
    surprise_pct} or None on failure."""
    cached = _cache_get(ticker, "earnings_history")
    if cached is not None:
        return cached if cached else None

    if yf is None:
        _cache_put(ticker, "earnings_history", False)
        return None

    try:
        t = yf.Ticker(ticker.upper())
        df = getattr(t, "earnings_history", None)
        if not isinstance(df, pd.DataFrame) or df.empty:
            _cache_put(ticker, "earnings_history", False)
            return None
    except Exception:  # noqa: BLE001
        _cache_put(ticker, "earnings_history", False)
        return None

    rows: list[dict] = []
    df_sorted = df.tail(max_quarters)
    for idx, row in df_sorted.iterrows():
        est = row.get("epsEstimate") or row.get("EPS Estimate")
        act = row.get("epsActual") or row.get("Reported EPS")
        try:
            est_f = float(est) if est is not None and not (isinstance(est, float) and math.isnan(est)) else None
            act_f = float(act) if act is not None and not (isinstance(act, float) and math.isnan(act)) else None
        except (TypeError, ValueError):
            est_f, act_f = None, None
        surprise_pct = None
        if est_f is not None and act_f is not None and est_f != 0:
            surprise_pct = round((act_f - est_f) / abs(est_f) * 100.0, 2)
        rows.append({
            "quarter": str(idx.date() if hasattr(idx, "date") else idx),
            "eps_estimate": est_f,
            "eps_actual": act_f,
            "surprise_pct": surprise_pct,
        })

    _cache_put(ticker, "earnings_history", rows)
    return rows


def beat_rate(history: list[dict] | None) -> float | None:
    """% of quarters where eps_actual > eps_estimate.

    Returns None when fewer than 4 quarters with both fields are
    available (too small to be meaningful).
    """
    if not history:
        return None
    matched = [
        h for h in history
        if h.get("eps_actual") is not None and h.get("eps_estimate") is not None
    ]
    if len(matched) < 4:
        return None
    beats = sum(1 for h in matched if h["eps_actual"] > h["eps_estimate"])
    return round(beats / len(matched) * 100.0, 1)


def is_earnings_window(ticker: str, expiry_date: date,
                      *, window_trading_days: int = 5,
                      today: date | None = None) -> tuple[bool, dict | None]:
    """Used by build_options_overlay tenor selection.

    Returns (in_window, earnings_info). ``in_window`` is True when the
    chosen expiry sits within ±``window_trading_days`` trading days of
    the next earnings date. The returned ``earnings_info`` is the same
    shape as ``get_next_earnings`` so callers can log the rationale.
    """
    info = get_next_earnings(ticker, today=today)
    if not info:
        return False, None
    earn = _coerce_date(info["date"])
    if not earn:
        return False, info
    gap = _trading_days_between(earn, expiry_date)
    return gap <= window_trading_days, info


def build_summary(ticker: str, *, today: date | None = None) -> dict | None:
    """One-shot helper that returns the full enrichment row for a ticker
    in a single dict. None if neither next-earnings nor history is
    available."""
    today = today or date.today()
    nxt = get_next_earnings(ticker, today=today)
    hist = get_earnings_history(ticker)
    if not nxt and not hist:
        return None
    payload: dict[str, Any] = {
        "ticker": ticker.upper(),
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if nxt:
        payload["next_earnings"] = nxt
    if hist:
        payload["history"] = hist
        rate = beat_rate(hist)
        if rate is not None:
            payload["beat_rate_pct"] = rate
    return payload
