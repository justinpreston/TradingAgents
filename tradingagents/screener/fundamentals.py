"""Fundamental-filter signals for the early-cycle screener.

We're looking for fundamentally-justified momentum, not pure narrative
bubbles. The signals that matter for early-cycle catches:

  * Revenue YoY *re-accelerating* — last quarter > prior quarter > quarter
    before (two consecutive quarters of accelerating growth)
  * Gross margin expanding — margin improvement signals pricing power and
    operating leverage
  * Top-line growth rate >= 15% YoY (filters out dying-business turnarounds
    that are only "cheap")

Polygon's ``/vX/reference/financials`` endpoint returns SEC filings with
breakdowns under ``financials.income_statement``, ``cash_flow_statement``,
etc. We use ``timeframe=quarterly`` to get the last 4–8 quarters.

For tickers with insufficient financial history (recent IPOs, ADRs that
don't file 10-Q, etc.) we return an ``insufficient_data`` flag and zero
score — those names can still pass on technicals alone if the user wants.

Caching: by default :func:`compute_fundamental_signals` consults a
disk cache keyed by ticker with a 7-day TTL — financials don't change
daily, and the weekly Friday cadence means the cache absorbs every
mid-week re-run. Pass ``use_cache=False`` to bypass.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields

from tradingagents.dataflows._disk_cache import DiskCache
from tradingagents.dataflows.polygon_common import (
    PolygonNotFoundError,
    paginated_results,
)

log = logging.getLogger(__name__)

# 7 days matches the weekly Friday cadence: each weekly run repopulates the
# cache; mid-week re-screens (e.g. VIX spike, override-trigger reruns) hit
# cache for the prior week's tickers.
_FUNDAMENTALS_CACHE_TTL_S = 7 * 24 * 3600
_FUNDAMENTALS_CACHE = DiskCache("fundamentals", ttl_seconds=_FUNDAMENTALS_CACHE_TTL_S)


@dataclass
class FundamentalSignals:
    ticker: str
    quarters_available: int = 0

    revenue_quarterly: list[float] = field(default_factory=list)
    revenue_yoy: list[float] = field(default_factory=list)  # last 4Q YoY %
    revenue_yoy_accelerating: bool = False
    revenue_growth_strong: bool = False  # most recent YoY >= 15%

    gross_margin_quarterly: list[float] = field(default_factory=list)
    gross_margin_expanding: bool = False
    gross_margin_latest: float = 0.0

    operating_margin_quarterly: list[float] = field(default_factory=list)
    operating_margin_expanding: bool = False

    fundamental_score: float = 0.0
    flags: list[str] = field(default_factory=list)


def fetch_quarterly_financials(ticker: str, *, max_quarters: int = 8) -> list[dict]:
    """Pull last ``max_quarters`` of quarterly SEC filings from Polygon.

    Only swallows :class:`PolygonNotFoundError` (ticker not covered by SEC
    filings — typical for ADRs, recent IPOs, foreign issuers). Auth and
    rate-limit errors propagate so the orchestrator can flag the run as
    partial rather than silently labelling tickers ``insufficient_data``.
    """
    try:
        results = paginated_results(
            "/vX/reference/financials",
            initial_params={
                "ticker": ticker,
                "timeframe": "quarterly",
                "order": "desc",
                "sort": "filing_date",
                "limit": max_quarters,
            },
            max_pages=2,
        )
    except PolygonNotFoundError:
        log.debug("financials 404 for %s — no SEC filings indexed", ticker)
        return []
    return results[:max_quarters]


def _income_value(report: dict, key: str) -> float:
    """Pull a numeric value from financials.income_statement.{key}.value."""
    try:
        return float(
            (((report.get("financials") or {}).get("income_statement") or {})
             .get(key) or {}).get("value") or 0.0
        )
    except (TypeError, ValueError):
        return 0.0


def _signals_from_dict(d: dict) -> "FundamentalSignals":
    """Reconstruct :class:`FundamentalSignals` from a JSON-cached payload.

    Tolerant of schema additions: unknown keys are ignored, missing keys fall
    back to dataclass defaults. This keeps a stale cache from poisoning a run
    after we add a new field — old entries are silently treated as
    incomplete-but-usable.
    """
    valid = {f.name for f in fields(FundamentalSignals)}
    return FundamentalSignals(**{k: v for k, v in d.items() if k in valid})


def compute_fundamental_signals(
    ticker: str,
    *,
    reports: list[dict] | None = None,
    use_cache: bool = True,
) -> FundamentalSignals:
    """Compute fundamental signals + composite score in [0, 100].

    When ``reports`` is None and ``use_cache`` is True (default), result is
    served from a 7-day disk cache keyed by ticker. Set ``use_cache=False``
    to bypass the cache (used by tests and by callers that need fresh data
    after an earnings beat).
    """
    # We persist to cache only when we did the fetch ourselves — caller-
    # supplied reports may be mocked/custom and shouldn't poison the cache.
    persist_to_cache = use_cache and reports is None

    if persist_to_cache:
        cached = _FUNDAMENTALS_CACHE.get(ticker)
        if cached is not None:
            try:
                return _signals_from_dict(cached)
            except (TypeError, KeyError) as e:
                log.debug("fundamentals cache: malformed entry for %s (%s) — refetching", ticker, e)

    if reports is None:
        reports = fetch_quarterly_financials(ticker)

    sig = _build_signals(ticker, reports)

    if persist_to_cache:
        try:
            _FUNDAMENTALS_CACHE.set(ticker, asdict(sig))
        except Exception as e:  # noqa: BLE001
            log.debug("fundamentals cache: failed to persist %s: %s", ticker, e)
    return sig


def _build_signals(ticker: str, reports: list[dict]) -> "FundamentalSignals":
    """Compute :class:`FundamentalSignals` from a report list (no I/O)."""
    sig = FundamentalSignals(ticker=ticker, quarters_available=len(reports))

    if len(reports) < 4:
        sig.flags.append("insufficient_data")
        return sig

    # Reports come back desc by filing_date — reorder asc by fiscal period_of_report_date
    sorted_reports = sorted(
        reports,
        key=lambda r: r.get("period_of_report_date") or r.get("end_date") or "",
    )

    revenues: list[float] = []
    gross_margins: list[float] = []
    op_margins: list[float] = []
    for r in sorted_reports:
        rev = _income_value(r, "revenues")
        cogs = _income_value(r, "cost_of_revenue") or _income_value(r, "cost_of_goods_and_services_sold")
        op_inc = _income_value(r, "operating_income_loss")
        if rev <= 0:
            continue
        revenues.append(rev)
        gross = rev - cogs if cogs > 0 else 0.0
        gross_margins.append((gross / rev) if (cogs > 0 and gross > 0) else 0.0)
        op_margins.append((op_inc / rev) if op_inc != 0 else 0.0)

    sig.revenue_quarterly = revenues
    sig.gross_margin_quarterly = gross_margins
    sig.operating_margin_quarterly = op_margins
    sig.gross_margin_latest = gross_margins[-1] if gross_margins else 0.0

    # YoY: need at least 5 quarters to get 1 YoY; 8 quarters gets 4 YoYs
    yoys: list[float] = []
    if len(revenues) >= 5:
        for i in range(4, len(revenues)):
            prior = revenues[i - 4]
            if prior > 0:
                yoys.append((revenues[i] - prior) / prior)
    sig.revenue_yoy = yoys

    if len(yoys) >= 3:
        # Last 3 YoYs strictly increasing → re-accelerating
        sig.revenue_yoy_accelerating = yoys[-1] > yoys[-2] > yoys[-3]
    if yoys:
        sig.revenue_growth_strong = yoys[-1] >= 0.15

    if len(gross_margins) >= 4:
        recent = sum(gross_margins[-2:]) / 2
        prior = sum(gross_margins[-4:-2]) / 2
        sig.gross_margin_expanding = recent > prior + 0.01  # +1pp threshold

    if len(op_margins) >= 4:
        recent = sum(op_margins[-2:]) / 2
        prior = sum(op_margins[-4:-2]) / 2
        sig.operating_margin_expanding = recent > prior + 0.005

    score = 0.0
    if sig.revenue_yoy_accelerating:
        score += 35
        sig.flags.append("rev_re_accelerating")
    if sig.revenue_growth_strong:
        score += 20
        sig.flags.append("rev_growth_strong")
    if sig.gross_margin_expanding:
        score += 25
        sig.flags.append("gross_margin_expanding")
    if sig.operating_margin_expanding:
        score += 20
        sig.flags.append("op_margin_expanding")
    if yoys and yoys[-1] < 0:
        score -= 30
        sig.flags.append("rev_declining")

    sig.fundamental_score = max(0.0, min(100.0, score))
    return sig
