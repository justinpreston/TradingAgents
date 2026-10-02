import logging
from typing import Annotated

import yfinance as yf

from tradingagents.dataflows.date_window import (
    withhold_live_profile,
    withhold_undated_statements,
    withhold_undisclosed_trades,
)
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import (
    YAHOO_HOST,
    raise_for_empty,
    yf_retry,
)
from tradingagents.dataflows.vendors.yahoo.pit_derivations import derive_pit_fundamentals

logger = logging.getLogger(__name__)

# Fork: the ``Ticker.info`` snapshot fields that a past ``curr_date`` rebuilds
# from dated statements and price bars (see pit_derivations). Forward-looking
# fields (Forward EPS, Forward PE, PEG Ratio) have no point-in-time source.
_PIT_SNAPSHOT_LABELS = (
    "Market Cap", "PE Ratio (TTM)", "Price to Book", "EPS (TTM)", "Dividend Yield",
    "52 Week High", "52 Week Low", "50 Day Average", "200 Day Average",
    "Revenue (TTM)", "Gross Profit", "EBITDA", "Net Income", "Profit Margin",
    "Operating Margin", "Return on Equity", "Return on Assets", "Debt to Equity",
    "Current Ratio", "Book Value", "Free Cash Flow",
)


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None
):
    """Get company fundamentals overview from yfinance.

    ``Ticker.info`` is a present-day snapshot with no historical vintage, so a
    past ``as_of_date`` withholds it through the shared point-in-time guard
    (``date_window.withhold_live_profile``, #1300). Fork: the snapshot fields
    are then rebuilt from data dated on or before ``as_of_date`` and appended
    under the withheld notice (:func:`_pit_reconstruction_section`).
    """
    canonical = normalize_symbol(ticker)

    # Guard before the request: the response would only be discarded, and the
    # answer does not depend on it.
    withheld = withhold_live_profile(as_of_date, canonical)
    if withheld:
        return withheld + _pit_reconstruction_section(canonical, as_of_date)

    info = yf_retry(lambda: yf.Ticker(canonical).info)
    if not info:
        raise_for_empty(ticker, canonical, "fundamentals")

    # Yahoo gives these two in percent (dividendYield 0.41 is 0.41%; debtToEquity
    # 78.4 is 78.4%, a ratio of 0.78) but the margins and returns as fractions,
    # so each carries its unit.
    dividend_yield, debt_to_equity = info.get("dividendYield"), info.get("debtToEquity")
    fields = [
        ("Name", info.get("longName")),
        ("Sector", info.get("sector")),
        ("Industry", info.get("industry")),
        ("Market Cap", info.get("marketCap")),
        ("PE Ratio (TTM)", info.get("trailingPE")),
        ("Forward PE", info.get("forwardPE")),
        ("PEG Ratio", info.get("pegRatio")),
        ("Price to Book", info.get("priceToBook")),
        ("EPS (TTM)", info.get("trailingEps")),
        ("Forward EPS", info.get("forwardEps")),
        ("Dividend Yield", None if dividend_yield is None else f"{dividend_yield}%"),
        ("Beta", info.get("beta")),
        ("52 Week High", info.get("fiftyTwoWeekHigh")),
        ("52 Week Low", info.get("fiftyTwoWeekLow")),
        ("50 Day Average", info.get("fiftyDayAverage")),
        ("200 Day Average", info.get("twoHundredDayAverage")),
        ("Revenue (TTM)", info.get("totalRevenue")),
        ("Gross Profit", info.get("grossProfits")),
        ("EBITDA", info.get("ebitda")),
        ("Net Income", info.get("netIncomeToCommon")),
        ("Profit Margin", info.get("profitMargins")),
        ("Operating Margin", info.get("operatingMargins")),
        ("Return on Equity", info.get("returnOnEquity")),
        ("Return on Assets", info.get("returnOnAssets")),
        ("Debt to Equity", None if debt_to_equity is None
         else f"{debt_to_equity}% ({debt_to_equity / 100:.2f}x)"),
        ("Current Ratio", info.get("currentRatio")),
        ("Book Value", info.get("bookValue")),
        ("Free Cash Flow", info.get("freeCashflow")),
    ]

    lines = [f"{label}: {v}" for label, v in fields if v is not None]

    # yfinance returns a stub dict (e.g. {"trailingPegRatio": None}) for
    # unknown symbols, so `info` is truthy but every field is empty. Treat
    # "no usable fields" as no data rather than emitting a bare header the
    # agent might fabricate around.
    if not lines:
        raise_for_empty(ticker, canonical, "fundamental fields")

    return f"# Company Fundamentals for {canonical}\n\n" + "\n".join(lines)


def _pit_reconstruction_section(canonical: str, curr_date: str) -> str:
    """Snapshot fields rebuilt from data dated on or before ``curr_date``.

    Uses only ``Ticker.history``, the quarterly statements and the share-count
    series (see :mod:`pit_derivations`), never ``Ticker.info``. Returns "" when
    nothing could be derived.
    """
    try:
        derived = derive_pit_fundamentals(yf.Ticker(canonical), curr_date) or {}
    except Exception as e:
        logger.warning("PIT fundamentals reconstruction failed for %s: %s", canonical, e)
        return ""
    lines = [f"{label}: {derived[label]}" for label in _PIT_SNAPSHOT_LABELS if label in derived]
    if not lines:
        return ""
    return (
        f"\n\n## Reconstructed point-in-time figures (as of {curr_date})\n"
        f"# Derived from price bars, quarterly statements and share counts dated "
        f"on or before {curr_date} ({len(lines)} of {len(_PIT_SNAPSHOT_LABELS)} "
        f"fields; missing entries mean sparse data). Forward-looking analyst "
        f"projections are omitted — no point-in-time source.\n\n"
        + "\n".join(lines)
    )


def _statement(ticker, freq, as_of_date, title, quarterly_attr, annual_attr) -> str:
    """One financial statement as CSV, for a run dated today."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undated_statements(as_of_date, canonical, title)
    if withheld:
        return withheld
    what = title.lower()
    attr = quarterly_attr if freq.lower() == "quarterly" else annual_attr
    data = yf_retry(lambda: getattr(yf.Ticker(canonical), attr))
    if data is None or data.empty:
        raise_for_empty(ticker, canonical, f"{what} data")
    return f"# {title} data for {canonical} ({freq})\n" + data.to_csv()


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get balance sheet data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Balance Sheet", "quarterly_balance_sheet", "balance_sheet")


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get cash flow data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Cash Flow", "quarterly_cashflow", "cashflow")


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get income statement data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Income Statement", "quarterly_income_stmt", "income_stmt")


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date, yyyy-mm-dd"] = None,
):
    """Get insider transactions data from yfinance, for a run dated today."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undisclosed_trades(as_of_date, canonical)
    if withheld:
        return withheld
    data = yf_retry(lambda: yf.Ticker(canonical).insider_transactions)

    # Empty is normal here (many valid symbols have no insider filings),
    # so report it plainly rather than treating the symbol as invalid.
    if data is None or data.empty:
        if not vendor_reachable(YAHOO_HOST):
            raise VendorUnavailableError("Yahoo Finance is unreachable; insider filings were not retrieved")
        return f"No insider transactions reported for symbol '{canonical}'"

    return f"# Insider Transactions data for {canonical}\n" + data.to_csv()


def get_company_profile(ticker: str) -> dict:
    """Yahoo's current profile for ``ticker``: name, sector, industry and the like."""
    return yf_retry(lambda: yf.Ticker(normalize_symbol(ticker)).info) or {}
