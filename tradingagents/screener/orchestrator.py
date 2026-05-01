"""End-to-end early-cycle screener orchestrator.

Pipeline:

  1. Build universe (mid + large US common stocks)
  2. For each universe entry, compute technical signals (one bars call)
  3. For each universe entry, compute fundamental signals (one financials call)
  4. Combine into a Candidate, score, rank
  5. Return top N

Sequential by default. Polygon's lower tiers don't permit aggressive
concurrency anyway. Total time on a ~1500-name universe: ~15-25 min.

The orchestrator enables a global :func:`min_request_interval` while it
runs so that sustained per-ticker bursts don't trip Polygon's rate limits;
the interval defaults to 0.12s (~8 req/sec) which empirically clears the
"Stocks Starter" tier's per-second window without tail-latency from 429
retries. Rate-limit failures are tracked and surfaced in the result;
``ScreenerResult.is_partial`` is True if any ticker was dropped for that
reason, so callers can avoid treating a corrupted top-N as authoritative.

For testing: pass ``universe_limit=N`` to cap stage 2 enrichment in
universe.build_universe. Pass ``ticker_limit=N`` to cap technicals +
fundamentals scans (useful when you've already got a universe and just
want to test the scoring tail).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from tradingagents.dataflows.polygon_common import (
    PolygonRateLimitError,
    min_request_interval,
)
from tradingagents.screener.fundamentals import (
    FundamentalSignals,
    compute_fundamental_signals,
)
from tradingagents.screener.score import (
    Candidate,
    build_candidate,
    rank_candidates,
)
from tradingagents.screener.technicals import compute_technical_signals
from tradingagents.screener.universe import (
    UniverseEntry,
    build_universe,
)

log = logging.getLogger(__name__)


DEFAULT_MIN_REQUEST_INTERVAL_S = 0.25


@dataclass
class ScreenerResult:
    trading_date: date
    universe_size: int
    candidates: list[Candidate]
    top_n: int
    config: dict
    rate_limited_failures: list[str] = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        """True if any ticker was dropped due to a Polygon rate-limit error.

        When True the top-N may be missing legitimately high-scoring names
        and should not be treated as authoritative — re-run with a longer
        ``min_request_interval`` or a smaller universe.
        """
        return bool(self.rate_limited_failures)


ProgressCb = Callable[[str, int, int, str], None]


def run_screener(
    *,
    target_date: date | None = None,
    min_price: float = 5.0,
    min_dollar_adv: float = 50_000_000.0,
    min_mcap: float = 2_000_000_000.0,
    max_mcap: float = 200_000_000_000.0,
    universe_limit: int | None = None,
    ticker_limit: int | None = None,
    top_n: int = 25,
    technical_weight: float = 0.55,
    fundamental_weight: float = 0.45,
    min_request_interval_s: float = DEFAULT_MIN_REQUEST_INTERVAL_S,
    progress_cb: ProgressCb | None = None,
) -> ScreenerResult:
    """Run the full screener pipeline. See module docstring."""

    def _emit(stage: str, i: int, total: int, ticker: str):
        if progress_cb:
            progress_cb(stage, i, total, ticker)

    def universe_progress(i, total, ticker, kept):
        _emit("universe", i, total, ticker)

    rate_limited_failures: list[str] = []

    with min_request_interval(min_request_interval_s, adaptive=True):
        used_date, universe = build_universe(
            target_date=target_date,
            min_price=min_price,
            min_dollar_adv=min_dollar_adv,
            min_mcap=min_mcap,
            max_mcap=max_mcap,
            enrich_limit=universe_limit,
            progress_callback=universe_progress,
        )
        log.info("universe built: %d entries on %s", len(universe), used_date)

        if ticker_limit is not None:
            universe = universe[:ticker_limit]

        candidates: list[Candidate] = []
        total = len(universe)
        for i, entry in enumerate(universe, start=1):
            try:
                tech = compute_technical_signals(entry.ticker, end_date=used_date)
            except PolygonRateLimitError as e:
                rate_limited_failures.append(f"{entry.ticker}:technicals")
                log.warning("rate limit on technicals for %s — dropped: %s", entry.ticker, e)
                continue
            except Exception as e:  # noqa: BLE001
                log.debug("technicals failed for %s: %s", entry.ticker, e)
                continue
            if "insufficient_history" in tech.flags:
                _emit("scoring", i, total, entry.ticker)
                continue

            try:
                fund = compute_fundamental_signals(entry.ticker)
            except PolygonRateLimitError as e:
                rate_limited_failures.append(f"{entry.ticker}:fundamentals")
                log.warning(
                    "rate limit on fundamentals for %s — scoring on tech only: %s",
                    entry.ticker,
                    e,
                )
                fund = FundamentalSignals(ticker=entry.ticker, flags=["rate_limited"])
            except Exception as e:  # noqa: BLE001
                log.debug("fundamentals failed for %s: %s", entry.ticker, e)
                fund = FundamentalSignals(ticker=entry.ticker, flags=["insufficient_data"])

            candidate = build_candidate(
                entry,
                tech,
                fund,
                technical_weight=technical_weight,
                fundamental_weight=fundamental_weight,
            )
            candidates.append(candidate)
            _emit("scoring", i, total, entry.ticker)

    ranked = rank_candidates(candidates)

    if rate_limited_failures:
        log.error(
            "screener result is PARTIAL — %d ticker stages dropped to rate limits "
            "(top-N may be missing legitimate names): %s%s",
            len(rate_limited_failures),
            ", ".join(rate_limited_failures[:10]),
            "..." if len(rate_limited_failures) > 10 else "",
        )

    return ScreenerResult(
        trading_date=used_date,
        universe_size=len(universe),
        candidates=ranked[:top_n],
        top_n=top_n,
        config={
            "min_price": min_price,
            "min_dollar_adv": min_dollar_adv,
            "min_mcap": min_mcap,
            "max_mcap": max_mcap,
            "universe_limit": universe_limit,
            "ticker_limit": ticker_limit,
            "technical_weight": technical_weight,
            "fundamental_weight": fundamental_weight,
            "min_request_interval_s": min_request_interval_s,
        },
        rate_limited_failures=rate_limited_failures,
    )
