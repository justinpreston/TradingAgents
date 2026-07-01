"""Shared daily-bar fetch + OCC option-symbol helpers for the backtest scripts.

Extracted from scripts/backtest_picks.py and scripts/backtest_exit_rules.py,
which both hand-rolled a raw-urllib GET with duplicated 429/403 backoff and
duplicated OCC-ticker construction. Both scripts now route through
:func:`tradingagents.dataflows.polygon_common._make_request` (shared
exponential backoff + Retry-After handling) via :func:`fetch_daily_bars`
here, keeping their own ``--pace`` CLI behavior via the pacer context
manager rather than an explicit ``time.sleep`` per call.
"""

from __future__ import annotations

from datetime import datetime

from tradingagents.dataflows.polygon_common import (
    PolygonAuthError,
    PolygonError,
    PolygonRateLimitError,
    _make_request,
)


def occ_ticker(underlying: str, expiration: str, strike: float, opt_type: str = "C") -> str:
    """Build a Polygon OCC-style option ticker, e.g. ``O:AAPL260116C00150000``.

    ``strike`` is the dollar strike price; Polygon encodes it as
    strike * 1000, zero-padded to 8 digits.
    """
    yymmdd = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    strike_int = int(round(float(strike) * 1000))
    return f"O:{underlying}{yymmdd}{opt_type}{strike_int:08d}"


def fetch_daily_bars(ticker: str, date_from: str, date_to: str) -> tuple[list[dict] | None, str | None]:
    """Fetch daily aggregate bars for ``ticker`` between ``date_from`` and
    ``date_to`` (inclusive, YYYY-MM-DD).

    Returns ``(bars, error)``. ``bars`` is ``None`` on any failure (rate
    limit exhaustion, auth failure, transient error after retries);
    ``error`` carries a short code: ``"401"``/``"403"`` for auth failures,
    or a free-text message for other failures. On success, ``error`` is
    ``None`` and ``bars`` is the (possibly empty) list of Polygon bar dicts.

    Uses :func:`tradingagents.dataflows.polygon_common._make_request`, which
    already retries transient 429/5xx with exponential backoff and honors
    ``Retry-After`` — callers no longer need their own retry loop. Pacing
    between calls (the old scripts' ``--pace`` flag) should be applied by
    the caller via ``polygon_common.set_min_request_interval`` /
    ``polygon_common.min_request_interval`` rather than here, so a batch of
    calls shares one pacer instead of sleeping unconditionally after every
    single request (including the last).
    """
    try:
        payload = _make_request(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{date_from}/{date_to}",
            {"adjusted": "true", "sort": "asc", "limit": 5000},
        )
    except PolygonAuthError as exc:
        code = "403" if "403" in str(exc) else "401"
        return None, code
    except PolygonRateLimitError as exc:
        return None, f"rate_limit: {exc}"
    except PolygonError as exc:
        return None, str(exc)
    return (payload.get("results") or []), None
