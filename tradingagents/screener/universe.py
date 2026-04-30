"""Pull and filter the US mid/large-cap equity universe via Polygon.

Two-stage construction:
  1. Pull the most recent trading day's grouped daily aggregates
     (``/v2/aggs/grouped/locale/us/market/stocks/{date}``) — one call returns
     OHLCV for ~12k US-listed tickers.
  2. Apply cheap in-memory filters (price, dollar volume, ticker shape) to
     drop ~80% of tickers immediately.
  3. For survivors, hit ``/v3/reference/tickers/{T}`` for canonical metadata
     including ``market_cap``, ``primary_exchange``, ``type``, ``locale``,
     ``sic_code``. Filter to mid + large US common stocks.

Filter criteria (defaults):

  * Primary US listing on a major exchange (NYSE, NASDAQ, ARCA, BATS, AMEX)
  * Common stock (``type == 'CS'``)
  * Price >= $5 (penny stock filter)
  * Last-day dollar volume >= $50M (liquidity proxy)
  * Market cap in [$2B, $200B] (mid + large, excludes mega and micro)

The bulk snapshot endpoint
(``/v2/snapshot/locale/us/markets/stocks/tickers``) is faster but is gated
behind Polygon's higher tiers; grouped aggs are available on Stocks Starter
and above.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from tradingagents.dataflows.polygon_common import _make_request

log = logging.getLogger(__name__)


PRIMARY_US_EXCHANGES = {
    "XNAS",  # NASDAQ
    "XNYS",  # NYSE
    "ARCX",  # NYSE Arca
    "BATS",  # CBOE BZX
    "XASE",  # NYSE American
    "BATY",  # CBOE BYX
    "EDGA",
    "EDGX",
    "IEXG",
}

DEFAULT_MIN_PRICE = 5.0
DEFAULT_MIN_DOLLAR_ADV = 50_000_000.0
DEFAULT_MIN_MCAP = 2_000_000_000.0
DEFAULT_MAX_MCAP = 200_000_000_000.0


@dataclass
class UniverseEntry:
    ticker: str
    name: str = ""
    primary_exchange: str = ""
    last_price: float = 0.0
    day_volume: float = 0.0
    market_cap: float = 0.0
    shares_outstanding: float = 0.0
    sic_code: str = ""
    sic_description: str = ""
    description: str = ""
    list_date: str = ""
    raw_grouped: dict = field(default_factory=dict, repr=False)
    raw_reference: dict = field(default_factory=dict, repr=False)

    @property
    def dollar_adv_proxy(self) -> float:
        """Last-day dollar volume; cheap proxy used for the universe filter.

        A proper ADV would average 20–60 trading days. For a coarse first
        cut this is sufficient — names that fail will fall out before we
        spend per-ticker calls in the technical stage.
        """
        return self.last_price * self.day_volume


def _is_common_stock_shape(ticker: str) -> bool:
    """Heuristic on ticker shape to drop obvious non-CS securities.

    The grouped-aggs endpoint returns warrants, units, rights, and preferred
    shares alongside common stock. We exclude obvious patterns:
      * Tickers >5 chars (units/warrants frequently have 5+ letter suffixes)
      * Tickers containing '.' or '-' (preferred-share class indicators
        like BRK.B, BF.B — these are still CS but we exclude in v1 for
        simplicity; can revisit)
      * Tickers ending in W (warrant), R (rights), U (unit) when 5 chars
    """
    if not ticker or len(ticker) > 5:
        return False
    if "." in ticker or "-" in ticker:
        return False
    if len(ticker) == 5 and ticker[-1] in {"W", "R", "U"}:
        return False
    return True


def latest_trading_day(reference: date | None = None) -> date:
    """Return the most recent *completed* trading day on/before ``reference``.

    Defaults to yesterday (US Eastern), since Polygon's lower tiers don't
    authorize today's grouped-aggs data before market close. Doesn't account
    for US market holidays; the grouped-aggs endpoint will return
    ``status: NOT_FOUND`` on a holiday and we step back one more day.
    """
    d = reference or (date.today() - timedelta(days=1))
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch_grouped_aggs(target_date: date | None = None, *, max_lookback: int = 7) -> tuple[date, list[dict]]:
    """Pull the grouped-daily aggregates for the most recent trading day.

    Returns ``(actual_date, results)`` where ``actual_date`` is the trading
    day actually loaded (in case the requested date was a holiday or
    Polygon rejected today's intraday data with 403 NOT_AUTHORIZED).
    """
    d = latest_trading_day(target_date)
    last_err: Exception | None = None
    for _ in range(max_lookback):
        try:
            payload = _make_request(
                f"/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}",
                params={"adjusted": "true"},
            )
            results = payload.get("results") or []
            if results:
                return d, results
        except Exception as e:  # noqa: BLE001 — auth + rate errors handled by stepping back
            last_err = e
            log.debug("grouped-aggs %s failed: %s — stepping back", d, e)
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    raise RuntimeError(
        f"No grouped-aggs data found in {max_lookback} day lookback ending {d}"
        + (f" (last error: {last_err})" if last_err else "")
    )


def _enrich_with_reference(ticker: str) -> dict:
    """Pull /v3/reference/tickers/{T} for canonical metadata + market_cap."""
    try:
        payload = _make_request(f"/v3/reference/tickers/{ticker}")
    except Exception as e:
        log.debug("reference fetch failed for %s: %s", ticker, e)
        return {}
    return payload.get("results") or {}


def build_universe(
    *,
    target_date: date | None = None,
    min_price: float = DEFAULT_MIN_PRICE,
    min_dollar_adv: float = DEFAULT_MIN_DOLLAR_ADV,
    min_mcap: float = DEFAULT_MIN_MCAP,
    max_mcap: float = DEFAULT_MAX_MCAP,
    enrich_limit: int | None = None,
    progress_callback=None,
) -> tuple[date, list[UniverseEntry]]:
    """Return mid/large-cap US common stocks meeting price + liquidity bars.

    Three-stage filter:
      1. Cheap in-memory filters on grouped-aggs (price, dollar volume,
         ticker shape). Drops most tickers in one pass.
      2. Per-ticker reference enrichment for ``market_cap``, ``type``,
         ``primary_exchange``. Filters to CS/US/major-exchange.
      3. Final mcap window filter.

    ``enrich_limit`` caps stage 2 for testing — None means enrich all stage-1
    survivors. Returns the trading date used and the surviving entries.
    """
    used_date, raw = fetch_grouped_aggs(target_date)
    log.info("grouped aggs for %s: %d entries", used_date, len(raw))

    stage1: list[dict] = []
    for entry in raw:
        ticker = entry.get("T", "")
        if not _is_common_stock_shape(ticker):
            continue

        last_price = entry.get("c") or 0.0
        if last_price < min_price:
            continue

        day_volume = entry.get("v") or 0.0
        if last_price * day_volume < min_dollar_adv:
            continue

        stage1.append(entry)

    log.info("stage1 (price + liquidity): %d", len(stage1))

    if enrich_limit is not None:
        stage1 = stage1[:enrich_limit]

    out: list[UniverseEntry] = []
    for i, entry in enumerate(stage1):
        ticker = entry["T"]
        ref = _enrich_with_reference(ticker)
        if not ref:
            continue
        if ref.get("primary_exchange") not in PRIMARY_US_EXCHANGES:
            continue
        if ref.get("type") != "CS":
            continue
        if ref.get("locale") != "us":
            continue

        mcap = ref.get("market_cap") or 0.0
        if mcap < min_mcap or mcap > max_mcap:
            continue

        # Foreign issuers (ADRs etc.) listed in the US frequently come back
        # as type=CS, locale=us, but with empty SIC and missing financials.
        # We require an SIC code so the fundamental filter has something to
        # work with downstream.
        sic_code = ref.get("sic_code") or ""
        if not sic_code:
            continue

        shares = (
            ref.get("share_class_shares_outstanding")
            or ref.get("weighted_shares_outstanding")
            or 0
        )

        out.append(
            UniverseEntry(
                ticker=ticker,
                name=ref.get("name", ""),
                primary_exchange=ref.get("primary_exchange", ""),
                last_price=entry.get("c") or 0.0,
                day_volume=entry.get("v") or 0.0,
                market_cap=mcap,
                shares_outstanding=shares,
                sic_code=ref.get("sic_code", ""),
                sic_description=ref.get("sic_description", ""),
                description=ref.get("description", ""),
                list_date=ref.get("list_date", ""),
                raw_grouped=entry,
                raw_reference=ref,
            )
        )

        if progress_callback:
            progress_callback(i + 1, len(stage1), ticker, len(out))

    log.info("stage2 (mcap window): %d", len(out))
    return used_date, out


def filter_by_sector(
    entries: Iterable[UniverseEntry],
    *,
    exclude_sics: Iterable[str] = (),
    include_sics_prefix: Iterable[str] = (),
) -> list[UniverseEntry]:
    """Apply SIC-based exclusion / inclusion.

    SIC codes are 4-digit. ``exclude_sics`` is exact match; ``include_sics_prefix``
    matches any prefix (e.g. '6' for financials, '28' for chemicals).
    """
    excl = set(exclude_sics)
    incl_prefixes = tuple(include_sics_prefix)
    out = []
    for e in entries:
        if e.sic_code in excl:
            continue
        if incl_prefixes and not any(e.sic_code.startswith(p) for p in incl_prefixes):
            continue
        out.append(e)
    return out
