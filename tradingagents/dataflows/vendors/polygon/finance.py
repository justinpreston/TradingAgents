"""Polygon REST implementations for the trading-agents data layer.

Market data (bars, indicators) and the overview's company reference data come
from Polygon. Polygon's ``/vX/reference/financials`` endpoint is retired
(sunset 2026-10-09) and its successors are not on this plan, so every
filing-derived figure now comes from SEC EDGAR (:mod:`..sec_edgar`), selected by
filing date: statements are served by the ``sec_edgar`` vendor directly, and the
overview below takes its TTM and balance-sheet fields from
:func:`sec_edgar.ttm_snapshot`.

Public surface:

* :func:`get_stock_data` — daily OHLCV bars over a date range (CSV string)
* :func:`get_fundamentals` — overview/snapshot fundamentals at ``curr_date``
* :func:`get_indicators` — technical indicators computed off Polygon bars
  via ``stockstats``

Forward-looking analyst projections (Forward EPS / PE / PEG) have no
PIT-correct source either; we omit them rather than fabricate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

import pandas as pd

from tradingagents.dataflows.vendors import sec_edgar

from .common import (
    PolygonError,
    PolygonNotFoundError,
    _make_request,
)

# --- helpers ----------------------------------------------------------------

def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _format_money(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 1e12:
        return f"${v / 1e12:.2f}T"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.2f}M"
    return f"${v:,.2f}"


def _close_at(ticker: str, curr_date: str) -> float | None:
    """Daily close at or before ``curr_date`` (split-adjusted)."""
    curr_dt = _parse_date(curr_date)
    if curr_dt is None:
        return None
    start = (curr_dt - timedelta(days=14)).strftime("%Y-%m-%d")
    end = curr_date
    try:
        payload = _make_request(
            f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start}/{end}",
            {"adjusted": "true", "sort": "desc", "limit": 30},
        )
    except PolygonError:
        return None
    bars = payload.get("results") or []
    if not bars:
        return None
    return float(bars[0].get("c"))


def _high_low_window(ticker: str, curr_date: str, days: int) -> tuple[float | None, float | None]:
    curr_dt = _parse_date(curr_date)
    if curr_dt is None:
        return None, None
    start = (curr_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    end = curr_date
    try:
        payload = _make_request(
            f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start}/{end}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
    except PolygonError:
        return None, None
    bars = payload.get("results") or []
    if not bars:
        return None, None
    highs = [float(b.get("h")) for b in bars if b.get("h") is not None]
    lows = [float(b.get("l")) for b in bars if b.get("l") is not None]
    return (max(highs) if highs else None, min(lows) if lows else None)


def _moving_average(ticker: str, curr_date: str, window: int) -> float | None:
    curr_dt = _parse_date(curr_date)
    if curr_dt is None:
        return None
    # Pad backwards to cover non-trading days
    start = (curr_dt - timedelta(days=int(window * 1.6) + 14)).strftime("%Y-%m-%d")
    end = curr_date
    try:
        payload = _make_request(
            f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start}/{end}",
            {"adjusted": "true", "sort": "desc", "limit": int(window * 2 + 60)},
        )
    except PolygonError:
        return None
    bars = payload.get("results") or []
    closes = [float(b.get("c")) for b in bars if b.get("c") is not None]
    if len(closes) < window:
        return None
    return sum(closes[:window]) / window


# --- public API -------------------------------------------------------------


def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch daily OHLCV bars from Polygon and return as CSV (yfinance-shape).

    Bars are split-adjusted (``adjusted=true``) so historical highs/lows align
    with current share counts — necessary for moving-average and 52-week
    range calculations to be meaningful across split events.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    try:
        payload = _make_request(
            f"/v2/aggs/ticker/{symbol.upper()}/range/1/day/{start_date}/{end_date}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
    except PolygonNotFoundError:
        return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"
    except PolygonError as exc:
        from tradingagents.dataflows.tool_errors import format_tool_error
        return format_tool_error("get_stock_data (polygon)", symbol, exc)

    bars = payload.get("results") or []
    if not bars:
        return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"

    rows = []
    for bar in bars:
        ts = bar.get("t")
        if ts is None:
            continue
        date = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        rows.append({
            "Date": date,
            "Open": round(float(bar.get("o", 0)), 2),
            "High": round(float(bar.get("h", 0)), 2),
            "Low": round(float(bar.get("l", 0)), 2),
            "Close": round(float(bar.get("c", 0)), 2),
            "Adj Close": round(float(bar.get("c", 0)), 2),
            "Volume": int(bar.get("v", 0)),
        })

    df = pd.DataFrame(rows)
    df.set_index("Date", inplace=True)

    header = (
        f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
        f"# Total records: {len(df)}\n"
        f"# Source: Polygon (split-adjusted)\n\n"
    )
    return header + df.to_csv()


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[
        str,
        "current date in YYYY-MM-DD format. Snapshot fields (Market Cap, P/E, "
        "TTM ratios, 52-week ranges, moving averages) are computed point-in-time "
        "from filings and bars available on or before this date.",
    ] = None,
) -> str:
    """Build an overview fundamentals report for ``ticker`` at ``curr_date``.

    Combines Polygon's ``/v3/reference/tickers`` (company metadata, shares,
    SIC code), SEC EDGAR facts filed on or before ``curr_date`` (TTM revenue,
    margins, EPS, latest balance sheet), daily bar history (52w range + moving
    averages), and derived ratios. The market cap shown
    is the as-of value from Polygon's reference data, which uses the share
    count and price valid on ``curr_date`` — eliminating the split-adjustment
    pitfalls the yfinance-derived path had on tickers like NVDA.
    """
    if not curr_date:
        curr_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Filing-derived fields come from SEC EDGAR, fetched first: when EDGAR has no
    # coverage or cannot be reached this raises, and the router serves the
    # overview from the next vendor (yfinance) instead of a half-empty one.
    edgar_snapshot = sec_edgar.ttm_snapshot(ticker, curr_date)

    out_lines: list[str] = []
    derived_count = 0

    # Reference data (name, sector, market cap, shares at date)
    try:
        ref = _make_request(
            f"/v3/reference/tickers/{ticker.upper()}",
            {"date": curr_date},
        )
        results = ref.get("results") or {}
    except PolygonNotFoundError:
        return f"No fundamentals data found for symbol '{ticker}'"
    except PolygonError as exc:
        from tradingagents.dataflows.tool_errors import format_tool_error
        return format_tool_error("get_fundamentals (polygon)", ticker, exc)

    name = results.get("name")
    if name:
        out_lines.append(f"Name: {name}")
    sic_desc = results.get("sic_description")
    if sic_desc:
        out_lines.append(f"Sector / SIC: {sic_desc}")
    primary_exchange = results.get("primary_exchange")
    if primary_exchange:
        out_lines.append(f"Primary Exchange: {primary_exchange}")

    market_cap = results.get("market_cap")
    if market_cap:
        out_lines.append(f"Market Cap: {_format_money(market_cap)}")
        derived_count += 1
    shares = results.get("share_class_shares_outstanding") or results.get("weighted_shares_outstanding")
    if shares:
        out_lines.append(f"Shares Outstanding: {int(shares):,}")
        derived_count += 1

    # Price + 52-week range
    last_close = _close_at(ticker, curr_date)
    if last_close is not None:
        out_lines.append(f"Last Close (≤ {curr_date}): ${last_close:.2f}")
        derived_count += 1
    high52, low52 = _high_low_window(ticker, curr_date, 365)
    if high52 is not None and low52 is not None:
        out_lines.append(f"52-Week High: ${high52:.2f}")
        out_lines.append(f"52-Week Low: ${low52:.2f}")
        derived_count += 2
    sma50 = _moving_average(ticker, curr_date, 50)
    sma200 = _moving_average(ticker, curr_date, 200)
    if sma50 is not None:
        out_lines.append(f"50-Day Moving Average: ${sma50:.2f}")
        derived_count += 1
    if sma200 is not None:
        out_lines.append(f"200-Day Moving Average: ${sma200:.2f}")
        derived_count += 1

    # Filing-derived figures (TTM sums, latest balance sheet) from SEC EDGAR
    snap = edgar_snapshot
    revenue_ttm = snap.get("revenue")
    gross_profit_ttm = snap.get("gross_profit")
    op_income_ttm = snap.get("operating_income")
    net_income_ttm = snap.get("net_income")
    eps_ttm = snap.get("eps")
    op_cash_ttm = snap.get("operating_cash_flow")

    if revenue_ttm is not None:
        out_lines.append(f"Revenue (TTM): {_format_money(revenue_ttm)}")
        derived_count += 1
    if gross_profit_ttm is not None:
        out_lines.append(f"Gross Profit (TTM): {_format_money(gross_profit_ttm)}")
        if revenue_ttm:
            out_lines.append(f"Gross Margin (TTM): {gross_profit_ttm / revenue_ttm * 100:.1f}%")
        derived_count += 1
    if op_income_ttm is not None:
        out_lines.append(f"Operating Income (TTM): {_format_money(op_income_ttm)}")
        if revenue_ttm:
            out_lines.append(f"Operating Margin (TTM): {op_income_ttm / revenue_ttm * 100:.1f}%")
        derived_count += 1
    if net_income_ttm is not None:
        out_lines.append(f"Net Income (TTM): {_format_money(net_income_ttm)}")
        if revenue_ttm:
            out_lines.append(f"Profit Margin (TTM): {net_income_ttm / revenue_ttm * 100:.1f}%")
        derived_count += 1

    # Diluted EPS TTM and PE
    if eps_ttm is not None:
        out_lines.append(f"EPS (TTM): {eps_ttm:.2f}")
        derived_count += 1
        if last_close and eps_ttm > 0:
            out_lines.append(f"P/E (TTM): {last_close / eps_ttm:.2f}")
            derived_count += 1

    # Cash flow detail
    if op_cash_ttm is not None:
        out_lines.append(f"Operating Cash Flow (TTM): {_format_money(op_cash_ttm)}")
        derived_count += 1

    # Latest balance sheet snapshot
    cash = snap.get("cash")
    debt = snap.get("long_term_debt")
    total_equity = snap.get("equity")
    total_assets = snap.get("total_assets")

    if cash is not None:
        out_lines.append(f"Cash & Equivalents (latest): {_format_money(cash)}")
        derived_count += 1
    if debt is not None:
        out_lines.append(f"Long-Term Debt (latest): {_format_money(debt)}")
        derived_count += 1
    if total_assets is not None:
        out_lines.append(f"Total Assets (latest): {_format_money(total_assets)}")
        derived_count += 1
    if total_equity is not None:
        out_lines.append(f"Stockholders' Equity (latest): {_format_money(total_equity)}")
        derived_count += 1
        if net_income_ttm is not None and total_equity > 0:
            out_lines.append(f"Return on Equity (TTM/avg equity ≈ latest): {net_income_ttm / total_equity * 100:.1f}%")
            derived_count += 1

    # Reporting period reference
    if snap.get("latest_period_end"):
        out_lines.append(
            f"Most recent fiscal period: {snap['latest_period_end']} (filed {snap['latest_filed']})"
        )

    header = (
        f"# Company Fundamentals for {ticker.upper()}\n"
        f"# Source: Polygon reference data and bars + SEC EDGAR filings (point-in-time as of {curr_date})\n"
        f"# {derived_count} fields derived from filings & bars available on or before {curr_date}\n"
        f"# Forward-looking analyst projections (Forward EPS / PE / PEG) intentionally omitted —\n"
        f"#   no PIT-correct source.\n\n"
    )
    return header + "\n".join(out_lines)


def get_indicators(
    symbol: Annotated[str, "ticker symbol"],
    indicator: Annotated[str, "technical indicator key"],
    curr_date: Annotated[str, "current trading date in YYYY-MM-DD"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Compute indicators by reusing the stockstats path against Polygon bars.

    The stockstats indicator window report itself is vendor-agnostic — it
    delegates to :func:`stockstats_utils.load_ohlcv`, which we patched to
    dispatch on the configured vendor. So we just call the existing
    ``get_stock_stats_indicators_window`` helper; bars come from Polygon
    automatically when ``core_stock_apis = polygon``.
    """
    from tradingagents.dataflows.vendors.yahoo.market import get_stock_stats_indicators_window

    return get_stock_stats_indicators_window(
        symbol=symbol,
        indicator=indicator,
        as_of_date=curr_date,
        look_back_days=look_back_days,
    )
