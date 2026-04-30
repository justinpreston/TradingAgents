"""End-to-end early-cycle screener orchestrator.

Pipeline:

  1. Build universe (mid + large US common stocks)
  2. For each universe entry, compute technical signals (one bars call)
  3. For each universe entry, compute fundamental signals (one financials call)
  4. Combine into a Candidate, score, rank
  5. Return top N

Sequential by default. Polygon's lower tiers don't permit aggressive
concurrency anyway. Total time on a ~1500-name universe: ~15-25 min.

For testing: pass ``universe_limit=N`` to cap stage 2 enrichment in
universe.build_universe. Pass ``ticker_limit=N`` to cap technicals +
fundamentals scans (useful when you've already got a universe and just
want to test the scoring tail).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable

from tradingagents.screener.fundamentals import compute_fundamental_signals
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


@dataclass
class ScreenerResult:
    trading_date: date
    universe_size: int
    candidates: list[Candidate]
    top_n: int
    config: dict


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
    progress_cb: ProgressCb | None = None,
) -> ScreenerResult:
    """Run the full screener pipeline. See module docstring."""

    def _emit(stage: str, i: int, total: int, ticker: str):
        if progress_cb:
            progress_cb(stage, i, total, ticker)

    def universe_progress(i, total, ticker, kept):
        _emit("universe", i, total, ticker)

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
        except Exception as e:  # noqa: BLE001
            log.debug("technicals failed for %s: %s", entry.ticker, e)
            continue
        if "insufficient_history" in tech.flags:
            _emit("scoring", i, total, entry.ticker)
            continue

        try:
            fund = compute_fundamental_signals(entry.ticker)
        except Exception as e:  # noqa: BLE001
            log.debug("fundamentals failed for %s: %s", entry.ticker, e)
            from tradingagents.screener.fundamentals import FundamentalSignals
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
        },
    )
