"""Company statements as they were filed, from SEC EDGAR.

Every other fundamentals vendor serves a period's current value and cuts the
statement at the fiscal period end. That is two claims a run should not make: a
period that has ended is not public until the company files, weeks later, and a
figure that was later restated is not what investors saw at the time.

EDGAR reports every fact with the date it was filed, so a run dated ``as_of_date``
serves exactly what was on file by then, restatements included at the vintage
that was current: Apple's 2008 total assets read 39.6B until the 2010 amendment
restated them to 36.2B.

Access needs no key or account, only a User-Agent identifying the caller, which
SEC requires and refuses requests without. US filers only: anything absent from
EDGAR's ticker map falls through to the next configured vendor.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from tradingagents import __version__
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file

logger = logging.getLogger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# A filing history only changes when something new is filed, so one fetch per
# company per day serves every date a run asks about.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Line items, each with the tags filers use for it, best first. First match wins
# and values are never summed across tags: a company reporting revenue under two
# tags would otherwise be counted twice.
_STATEMENTS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "balance_sheet": [
        ("Total Assets", ("Assets",)),
        ("Current Assets", ("AssetsCurrent",)),
        ("Cash and Equivalents", ("CashAndCashEquivalentsAtCarryingValue",)),
        ("Total Liabilities", ("Liabilities",)),
        ("Current Liabilities", ("LiabilitiesCurrent",)),
        ("Stockholders Equity", ("StockholdersEquity",
                                 "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    ],
    "income_statement": [
        ("Revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax",
                     "RevenueFromContractWithCustomerIncludingAssessedTax", "Revenues",
                     "SalesRevenueNet", "RevenuesNetOfInterestExpense")),
        ("Cost of Revenue", ("CostOfRevenue", "CostOfGoodsAndServicesSold")),
        ("Gross Profit", ("GrossProfit",)),
        ("Operating Income", ("OperatingIncomeLoss",)),
        ("Net Income", ("NetIncomeLoss",)),
        ("Diluted EPS", ("EarningsPerShareDiluted",)),
    ],
    "cashflow": [
        ("Operating Cash Flow", ("NetCashProvidedByUsedInOperatingActivities",
                                 "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")),
        ("Investing Cash Flow", ("NetCashProvidedByUsedInInvestingActivities",)),
        ("Financing Cash Flow", ("NetCashProvidedByUsedInFinancingActivities",)),
        ("Capital Expenditure", ("PaymentsToAcquirePropertyPlantAndEquipment",
                                 "PaymentsToAcquireProductiveAssets")),
    ],
}

# A statement's figures cover a span: a quarter is about 90 days, a year about
# 365. One filing reports both the quarter and the year to date under the same
# end date, so a match on the end date alone can report half a year as a quarter.
_SPANS = {"quarterly": (60, 115), "annual": (300, 400)}

# A 10-Q's cash flows are often filed only year to date. A quarterly table takes
# the quarter where filed, else the span to date, named in the column; it never
# subtracts one filing from another, which would give a figure no filing states.
_YEAR_TO_DATE = ((150, 200, 6), (240, 290, 9))

# A fiscal year is a period an annual report covers. A 10-Q balance has no span
# to reject, and some filers' 10-Qs report twelve-month totals that pass the span
# check, so either would read as a fiscal year. The value is still the latest
# filing of any form: a recast after a split or spin-off counts from its filing.
_ANNUAL_FORMS = ("10-K", "20-F", "40-F")


def _user_agent() -> str:
    """Who SEC sees. No account or key exists; callers identify themselves.

    www.sec.gov, which serves the ticker map, refuses a User-Agent carrying no
    contact address: a client name alone or with a project URL gets 403, one
    with an address gets 200. So the default carries a placeholder address and
    the package version. Set SEC_EDGAR_USER_AGENT to your own name and address
    so SEC can reach you about your traffic rather than the project.
    """
    configured = os.getenv("SEC_EDGAR_USER_AGENT", "").strip()
    return configured or f"TradingAgents/{__version__} (contact@example.com)"




class EdgarNotFoundError(VendorUnavailableError):
    """EDGAR answered 404: the document does not exist (no XBRL facts for this CIK).

    Still a ``VendorUnavailableError`` so the router skips to the next vendor,
    but distinguishable by callers that must tell "no coverage" from "outage".
    """


# SEC's fair-access limit is 10 requests/second per source. The pacer spaces
# every request this process makes at least ``_MIN_INTERVAL`` apart (8 req/s
# leaves headroom for clock jitter) so a screener walking hundreds of tickers
# cannot get the caller's address throttled. It is process-wide; separate
# processes (matrix cells) each make only a handful of requests.
_MIN_INTERVAL = 0.125
_pace_lock = threading.Lock()
_next_slot = 0.0
_monotonic = time.monotonic
_sleep = time.sleep


def _pace() -> None:
    """Block until this request's slot, then reserve the next one."""
    global _next_slot
    with _pace_lock:
        now = _monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + _MIN_INTERVAL
    if slot > now:
        _sleep(slot - now)


def _fetch_json(url: str) -> dict:
    """Read a public EDGAR document, respecting SEC's identification rule."""
    _pace()
    try:
        response = requests.get(url, headers={"User-Agent": _user_agent()}, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        # Every failure here is "this vendor cannot serve it now", so the router
        # moves on instead of seeing a transport exception it has no rule for.
        error = EdgarNotFoundError if status == 404 else VendorUnavailableError
        raise error(f"SEC EDGAR request failed ({status or type(exc).__name__})") from exc
    except ValueError as exc:
        raise VendorUnavailableError("SEC EDGAR returned an unreadable response") from exc


def _cached_json(url: str, name: str, *, persist: bool = True) -> dict:
    """A document from the 24h disk cache, else fetched.

    ``persist=False`` still reads a fresh cache file but never writes one: a
    company's facts run to several MB, and a screener walking the whole universe
    would otherwise fill the cache directory with files it does not need (it
    keeps its own small derived-signals cache).
    """
    path = Path(get_config()["data_cache_dir"]) / "sec_edgar" / name
    if path.exists() and time.time() - path.stat().st_mtime < _CACHE_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass  # a truncated file is a miss, not a failure
    data = _fetch_json(url)
    if persist:
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, lambda temp: Path(temp).write_text(json.dumps(data), encoding="utf-8"))
    return data


def cik_for(ticker: str) -> str | None:
    """The filer's CIK, or None when the ticker is not a US filer."""
    table = _cached_json(_TICKERS_URL, "company_tickers.json")
    wanted = ticker.strip().upper()
    for entry in table.values():
        if entry.get("ticker", "").upper() == wanted:
            return f"{int(entry['cik_str']):010d}"
    return None


def _span_index(fact: dict, spans: tuple[tuple[int, int], ...]) -> int | None:
    """Which of ``spans`` a duration fact covers (0 for an instant fact), or None."""
    if "start" not in fact:
        return 0
    days = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
    return next((i for i, (low, high) in enumerate(spans) if low <= days <= high), None)


def _as_of(facts: dict, tags: tuple[str, ...], as_of_date: str, spans: tuple[tuple[int, int], ...],
           forms: tuple[str, ...] = ()) -> tuple[dict, str]:
    """({(period end, span index): value}, unit) for the tags the filer reports, as known then.

    A period reported more than once takes its latest filing on or before the
    date, so an amendment counts from the day it was filed and not before. The
    unit comes from the filing: most lines are USD, earnings per share are
    USD/shares, and scaling those alike would print a real figure as zero. A
    duration fact (revenue, cash flow) must cover one of ``spans``; an instant
    fact (a balance) has no span and serves any.
    """
    values: dict[tuple[str, int], float] = {}
    chosen_unit = "USD"
    # Tags are tried in order and a period keeps the first one that reports it:
    # filers renamed lines over the years, so one tag covers only part of the
    # history. Values are never added across tags, which would double count.
    for tag in tags:
        for unit, unit_values in ((facts.get(tag) or {}).get("units", {})).items():
            latest: dict[tuple[str, int], dict] = {}
            covered: set[tuple[str, int]] = set()   # periods a filing of ``forms`` reports
            for fact in unit_values:
                index = _span_index(fact, spans)
                key = (fact["end"], index)
                if fact["filed"] > as_of_date or index is None or key in values:
                    continue
                if not forms or fact.get("form", "").startswith(forms):
                    covered.add(key)
                seen = latest.get(key)
                if seen is None or fact["filed"] >= seen["filed"]:
                    latest[key] = fact
            latest = {key: fact for key, fact in latest.items() if key in covered}
            if latest:
                chosen_unit = unit
                values.update({key: fact["val"] for key, fact in latest.items()})
    return dict(sorted(values.items())), chosen_unit


def _statement(kind: str, ticker: str, freq: str, as_of_date: str, title: str) -> str:
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    cik = cik_for(ticker)
    if cik is None:
        raise NoMarketDataError(ticker, ticker, "not a US SEC filer")

    facts = _cached_json(_FACTS_URL.format(cik=cik), f"CIK{cik}.json")
    us_gaap = (facts.get("facts") or {}).get("us-gaap")
    if not us_gaap:
        raise NoMarketDataError(ticker, ticker, "US filer with no us-gaap facts")

    quarterly = freq.lower() == "quarterly"
    if quarterly:
        spans = (_SPANS["quarterly"], *((low, high) for low, high, _ in _YEAR_TO_DATE))
        names = ["", *(f" ({months} months)" for _, _, months in _YEAR_TO_DATE)]
    else:
        spans, names = (_SPANS["annual"],), [""]
    forms = () if quarterly else _ANNUAL_FORMS
    lines = {label: _as_of(us_gaap, tags, as_of_date, spans, forms) for label, tags in _STATEMENTS[kind]}
    # Each row takes the shortest span it reports for a period, and a column
    # holds one span of one period, so a row filed only to date keeps its figure
    # beside a row filed by quarter.
    chosen = {label: {} for label in lines}
    for label, (values, _) in lines.items():
        for end, index in values:
            chosen[label][end] = min(index, chosen[label].get(end, index))
    periods = sorted({(end, index) for spans_of in chosen.values() for end, index in spans_of.items()})
    if not periods:
        raise NoMarketDataError(ticker, ticker, f"no {freq} {title.lower()} filed by {as_of_date}")

    header = (
        f"# {title} for {ticker.upper()} ({freq}), USD in millions unless the row says otherwise\n"
        f"# SEC EDGAR facts filed on or before {as_of_date}, at the values filed then\n\n"
    )
    rows = [",".join([""] + [end + names[index] for end, index in periods])]
    for label, (values, unit) in lines.items():
        # Every row spans the same columns, or a reader lines the table up wrong.
        if not values:
            rows.append(",".join([label] + ["unavailable (not tagged by this filer)"] * len(periods)))
            continue
        name = label if unit == "USD" else f"{label} ({unit})"
        # Plain numbers: a thousands separator would split the CSV field.
        cells = []
        for end, index in periods:
            value = values.get((end, index)) if chosen[label].get(end) == index else None
            cells.append("" if value is None else f"{value / 1e6:.0f}" if unit == "USD" else f"{value:.2f}")
        rows.append(",".join([name] + cells))
    return header + "\n".join(rows) + "\n"


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Balance sheet as filed on or before ``as_of_date``."""
    return _statement("balance_sheet", ticker, freq, as_of_date, "Balance Sheet")


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Income statement as filed on or before ``as_of_date``.

    A fourth quarter is never derived: filers report it only inside the annual
    figure, and subtracting three separately filed quarters would invent a number
    with no filing date behind it.
    """
    return _statement("income_statement", ticker, freq, as_of_date, "Income Statement")


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Cash flow statement as filed on or before ``as_of_date``."""
    return _statement("cashflow", ticker, freq, as_of_date, "Cash Flow Statement")


# --- quarterly series and trailing-twelve-month figures -----------------------
#
# The statements above refuse to derive a fourth quarter, because the table they
# print is a record of what was filed. The screener and the fundamentals overview
# need the opposite: an unbroken run of quarters and four-quarter sums, and
# filers report a fourth quarter only inside the annual figure. Those callers
# take the derived quarter (annual less the first three, or year-to-date less
# the year to date before it), and it carries the later of its two filing dates.

# Spans of a fiscal quarter and of the cumulative (year-to-date) figures a 10-Q or
# 10-K reports. A 52/53-week year runs a few days past 365.
_QUARTER_SPAN = (55, 120)
_CUMULATIVE_SPANS = ((150, 200), (240, 290), (300, 400))

# Two quarter ends further apart than this have a missing quarter between them.
_MAX_QUARTER_GAP = 120

# A series whose latest quarter ended longer ago than this has stopped reporting.
_STALE_AFTER_DAYS = 420

# Banks and brokers (JPM, WFC, GS, MS) report their top line only as revenue net
# of interest expense; a filer that tags it uses it as the total, while its
# contract-revenue tag (AXP's discount revenue) is just one component. So it
# comes first.
_REVENUE_TAGS = (
    "RevenuesNetOfInterestExpense",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
_COST_OF_REVENUE_TAGS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")

Quarters = dict[str, tuple[float, str]]     # period end -> (value, filing date)


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _duration_periods(us_gaap: dict, tag: str, as_of_date: str, unit: str) -> dict[tuple[str, str], tuple[float, str]]:
    """{(start, end): (value, filed)} for a duration tag as known on ``as_of_date``.

    A period reported more than once keeps its latest filing, so a restatement
    counts from the day it was filed.
    """
    periods: dict[tuple[str, str], tuple[float, str]] = {}
    for fact in ((us_gaap.get(tag) or {}).get("units") or {}).get(unit, []):
        if "start" not in fact or fact["filed"] > as_of_date:
            continue
        key = (fact["start"], fact["end"])
        seen = periods.get(key)
        if seen is None or fact["filed"] >= seen[1]:
            periods[key] = (fact["val"], fact["filed"])
    return periods


def _quarters(periods: dict[tuple[str, str], tuple[float, str]]) -> Quarters:
    """Fiscal quarters from reported periods, deriving the ones never reported alone.

    A quarter filed as such is used as filed. One that exists only inside a
    cumulative figure (a fourth quarter inside the annual figure, a cash-flow
    second quarter inside the six-month figure) is that figure less the one
    before it in the same fiscal year, which is only done when every quarter in
    between is known: it never subtracts across a gap.
    """
    quarters: Quarters = {}
    for (start, end), value in periods.items():
        if _QUARTER_SPAN[0] <= _days(start, end) <= _QUARTER_SPAN[1]:
            quarters[end] = value

    year_starts = {start for (start, end) in periods
                   if any(low <= _days(start, end) <= high for low, high in _CUMULATIVE_SPANS)}
    for year_start in sorted(year_starts):
        # The first quarter shares the year's start; later ones are known by
        # their own span, and the cumulative figures by start == year_start.
        cumulative = {end: value for (start, end), value in periods.items()
                      if start == year_start
                      and _QUARTER_SPAN[0] <= _days(start, end) <= _CUMULATIVE_SPANS[-1][1]}
        in_year = {end: value for (start, end), value in periods.items()
                   if _QUARTER_SPAN[0] <= _days(start, end) <= _QUARTER_SPAN[1]
                   and 0 <= _days(year_start, start) < 360}
        previous_end = (date.fromisoformat(year_start) - timedelta(days=1)).isoformat()
        running: float | None = 0.0
        running_filed = ""
        for end in sorted(set(cumulative) | set(in_year)):
            contiguous = (running is not None
                          and _QUARTER_SPAN[0] <= _days(previous_end, end) <= _MAX_QUARTER_GAP)
            if end in in_year:
                value, filed = in_year[end]
                if end in cumulative:
                    running, running_filed = cumulative[end]
                elif contiguous:
                    running, running_filed = running + value, max(running_filed, filed)
                else:
                    running = None
            else:
                value, filed = cumulative[end]
                if contiguous:
                    quarters.setdefault(end, (value - running, max(filed, running_filed)))
                running, running_filed = value, filed
            previous_end = end
    return quarters


def _quarter_series(us_gaap: dict, tags: tuple[str, ...], as_of_date: str, unit: str = "USD") -> Quarters:
    """Quarters across ``tags``: a period takes the first tag that yields it.

    Derivation happens per tag, never across tags, so a year-to-date figure is
    never reduced by a quarter another tag reported. Filers renamed lines over
    the years, which is why one tag rarely covers the whole history.
    """
    merged: Quarters = {}
    for tag in tags:
        for end, value in _quarters(_duration_periods(us_gaap, tag, as_of_date, unit)).items():
            merged.setdefault(end, value)
    return merged


def _recent_run(series: Quarters, count: int) -> list[str]:
    """Period ends, newest first, of up to ``count`` quarters with none missing between."""
    run: list[str] = []
    for end in sorted(series, reverse=True):
        if run and not _QUARTER_SPAN[0] <= _days(end, run[-1]) <= _MAX_QUARTER_GAP:
            break
        run.append(end)
        if len(run) == count:
            break
    return run


def _ttm(series: Quarters) -> float | None:
    """Sum of the latest four consecutive quarters, or None when fewer are known."""
    run = _recent_run(series, 4)
    return sum(series[end][0] for end in run) if len(run) == 4 else None


def _us_gaap_facts(ticker: str, *, persist: bool = True) -> dict | None:
    """The filer's us-gaap facts, or None when EDGAR has none (not a US filer, ADR, new IPO).

    A failed request is not "no facts": it raises, so a caller cannot mistake an
    outage for a company EDGAR does not cover.
    """
    cik = cik_for(ticker)
    if cik is None:
        return None
    try:
        facts = _cached_json(_FACTS_URL.format(cik=cik), f"CIK{cik}.json", persist=persist)
    except EdgarNotFoundError:
        return None
    return (facts.get("facts") or {}).get("us-gaap") or None


def quarterly_income_series(
    ticker: str,
    as_of_date: str | None = None,
    max_quarters: int = 8,
    *,
    persist: bool = True,
) -> list[dict]:
    """The latest ``max_quarters`` consecutive quarters of revenue, gross profit and operating income.

    Oldest first, as {"end", "filed", "revenue", "gross_profit", "operating_income"}
    (a line the filer does not tag is None). Returns [] where EDGAR has no
    coverage (not a US filer, no us-gaap facts, no quarterly revenue, or a
    filer that stopped reporting); a failed request raises
    ``VendorUnavailableError``.
    """
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    us_gaap = _us_gaap_facts(ticker, persist=persist)
    if us_gaap is None:
        return []

    revenue = _quarter_series(us_gaap, _REVENUE_TAGS, as_of_date)
    run = _recent_run(revenue, max_quarters)
    if not run or _days(run[0], as_of_date) > _STALE_AFTER_DAYS:
        return []
    gross = _quarter_series(us_gaap, ("GrossProfit",), as_of_date)
    cost = _quarter_series(us_gaap, _COST_OF_REVENUE_TAGS, as_of_date)
    operating = _quarter_series(us_gaap, ("OperatingIncomeLoss",), as_of_date)

    rows = []
    for end in reversed(run):
        value, filed = revenue[end]
        if end in gross:
            gross_profit = gross[end][0]
        else:
            gross_profit = value - cost[end][0] if end in cost else None
        rows.append({
            "end": end,
            "filed": filed,
            "revenue": value,
            "gross_profit": gross_profit,
            "operating_income": operating[end][0] if end in operating else None,
        })
    return rows


def _latest_instant(us_gaap: dict, tags: tuple[str, ...], as_of_date: str) -> float | None:
    """The newest balance-sheet figure filed by ``as_of_date``; earlier tags win a tie on date."""
    best: tuple[str, int, str, float] | None = None
    for priority, tag in enumerate(tags):
        for fact in ((us_gaap.get(tag) or {}).get("units") or {}).get("USD", []):
            if "start" in fact or fact["filed"] > as_of_date:
                continue
            candidate = (fact["end"], -priority, fact["filed"], fact["val"])
            if best is None or candidate[:3] > best[:3]:
                best = candidate
    return best[3] if best else None


def ttm_snapshot(ticker: str, as_of_date: str | None = None) -> dict:
    """Trailing-twelve-month and latest balance figures from facts filed by ``as_of_date``.

    A figure the filer does not tag, or whose last four quarters are not all
    known, is absent from the result rather than estimated. Keys: revenue,
    gross_profit, operating_income, net_income, eps (diluted, else basic),
    operating_cash_flow (TTM); cash, long_term_debt, total_assets, equity
    (latest); latest_period_end and latest_filed for the newest quarter.
    Raises ``NoMarketDataError`` where EDGAR has no coverage and
    ``VendorUnavailableError`` when it could not be reached.
    """
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    us_gaap = _us_gaap_facts(ticker)
    if us_gaap is None:
        raise NoMarketDataError(ticker, ticker, "no SEC EDGAR us-gaap facts (not a US filer, or too new)")

    revenue = _quarter_series(us_gaap, _REVENUE_TAGS, as_of_date)
    flows = {
        "revenue": revenue,
        "gross_profit": _quarter_series(us_gaap, ("GrossProfit",), as_of_date),
        "operating_income": _quarter_series(us_gaap, ("OperatingIncomeLoss",), as_of_date),
        "net_income": _quarter_series(us_gaap, ("NetIncomeLoss",), as_of_date),
        "operating_cash_flow": _quarter_series(
            us_gaap, ("NetCashProvidedByUsedInOperatingActivities",
                      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"), as_of_date),
    }
    snapshot: dict = {}
    for name, series in flows.items():
        total = _ttm(series)
        if total is not None:
            snapshot[name] = total
    # Per-share lines are summed the same way. A derived fourth quarter is the
    # annual figure less three quarters, so it inherits any change in share count.
    for tags in (("EarningsPerShareDiluted",), ("EarningsPerShareBasic",)):
        eps = _ttm(_quarter_series(us_gaap, tags, as_of_date, unit="USD/shares"))
        if eps is not None:
            snapshot["eps"] = eps
            break

    for name, tags in (
        ("cash", ("CashAndCashEquivalentsAtCarryingValue",)),
        ("long_term_debt", ("LongTermDebtNoncurrent", "LongTermDebt")),
        ("total_assets", ("Assets",)),
        ("equity", ("StockholdersEquity",
                    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    ):
        value = _latest_instant(us_gaap, tags, as_of_date)
        if value is not None:
            snapshot[name] = value

    run = _recent_run(revenue, 1) or _recent_run(flows["net_income"], 1)
    if run:
        newest = revenue if run[0] in revenue else flows["net_income"]
        snapshot["latest_period_end"] = run[0]
        snapshot["latest_filed"] = newest[run[0]][1]
    return snapshot
