"""Composite scoring + ranking for the early-cycle screener."""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagents.screener.fundamentals import FundamentalSignals
from tradingagents.screener.technicals import TechnicalSignals
from tradingagents.screener.universe import UniverseEntry


@dataclass
class Candidate:
    ticker: str
    name: str
    market_cap: float
    sector_sic: str
    sector_desc: str

    technical: TechnicalSignals
    fundamental: FundamentalSignals

    composite_score: float = 0.0
    rank: int = 0
    summary: str = ""
    notable_flags: list[str] = field(default_factory=list)


def composite_score(
    tech: TechnicalSignals,
    fund: FundamentalSignals,
    *,
    technical_weight: float = 0.55,
    fundamental_weight: float = 0.45,
) -> float:
    """Weighted composite of technical and fundamental scores.

    Default weighting tilts modestly toward technicals — early-cycle entries
    are validated by price action *first* (institutions front-run the
    fundamentals). But fundamental backing is what separates BE-style
    durable parabolics from GME-style meme spikes, so it carries 45%.

    If fundamentals data is missing (insufficient_data flag), we don't
    zero out — we use technical alone with a small cap penalty.
    """
    if "insufficient_data" in fund.flags:
        return tech.technical_score * 0.85  # 15% penalty for unverifiable fundamentals
    return technical_weight * tech.technical_score + fundamental_weight * fund.fundamental_score


def build_candidate(
    universe_entry: UniverseEntry,
    tech: TechnicalSignals,
    fund: FundamentalSignals,
    *,
    technical_weight: float = 0.55,
    fundamental_weight: float = 0.45,
) -> Candidate:
    score = composite_score(
        tech,
        fund,
        technical_weight=technical_weight,
        fundamental_weight=fundamental_weight,
    )
    notable = []
    high_value_flags = {
        "ma_stack_recent",
        "base_breakout",
        "rev_re_accelerating",
        "gross_margin_expanding",
        "vol_expansion_strong",
        "rsi_sweet_spot",
    }
    cautionary_flags = {
        "rsi_overheated",
        "rev_declining",
        "far_below_52w_high",
    }
    for f in tech.flags + fund.flags:
        if f in high_value_flags or f in cautionary_flags:
            notable.append(f)

    parts = []
    if tech.last_close > 0:
        parts.append(f"${tech.last_close:.2f}")
    if tech.pct_off_high:
        parts.append(f"{tech.pct_off_high*100:+.1f}% from 52w high")
    if fund.revenue_yoy:
        parts.append(f"rev YoY {fund.revenue_yoy[-1]*100:+.1f}%")
    if tech.rsi_14:
        parts.append(f"RSI {tech.rsi_14:.0f}")
    if tech.vol_expansion:
        parts.append(f"vol {tech.vol_expansion:.2f}x")

    return Candidate(
        ticker=universe_entry.ticker,
        name=universe_entry.name,
        market_cap=universe_entry.market_cap,
        sector_sic=universe_entry.sic_code,
        sector_desc=universe_entry.sic_description,
        technical=tech,
        fundamental=fund,
        composite_score=score,
        summary=" · ".join(parts),
        notable_flags=notable,
    )


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Sort descending by composite score and assign ``rank`` 1..N."""
    sorted_c = sorted(candidates, key=lambda c: -c.composite_score)
    for i, c in enumerate(sorted_c, start=1):
        c.rank = i
    return sorted_c
