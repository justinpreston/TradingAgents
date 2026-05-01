"""Output writers for the screener — JSON manifest + Markdown report."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from tradingagents.screener.orchestrator import ScreenerResult


def write_json(result: ScreenerResult, path: Path) -> None:
    payload = {
        "trading_date": result.trading_date.isoformat(),
        "universe_size": result.universe_size,
        "top_n": result.top_n,
        "config": result.config,
        "is_partial": result.is_partial,
        "rate_limited_failures": result.rate_limited_failures,
        "candidates": [
            {
                "rank": c.rank,
                "ticker": c.ticker,
                "name": c.name,
                "market_cap": c.market_cap,
                "sector_sic": c.sector_sic,
                "sector_desc": c.sector_desc,
                "composite_score": c.composite_score,
                "technical_score": c.technical.technical_score,
                "fundamental_score": c.fundamental.fundamental_score,
                "summary": c.summary,
                "notable_flags": c.notable_flags,
                "technical": {
                    k: v for k, v in asdict(c.technical).items()
                    if k != "flags"
                },
                "fundamental": {
                    k: v for k, v in asdict(c.fundamental).items()
                    if k != "flags"
                },
                "all_flags": c.technical.flags + c.fundamental.flags,
            }
            for c in result.candidates
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))


def write_markdown(result: ScreenerResult, path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# Early-cycle screener — {result.trading_date.isoformat()}")
    lines.append("")
    if result.is_partial:
        lines.append(
            f"> ⚠️ **PARTIAL RESULT** — {len(result.rate_limited_failures)} "
            f"ticker stage(s) dropped due to Polygon rate limits. "
            f"Top-N may be missing legitimate names."
        )
        lines.append("")
    lines.append(
        f"Universe: **{result.universe_size}** mid/large-cap names · "
        f"showing top **{len(result.candidates)}** by composite score"
    )
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    for k, v in result.config.items():
        lines.append(f"- `{k}` = `{v}`")
    lines.append("")
    lines.append("## Ranked candidates")
    lines.append("")
    lines.append("| # | Ticker | Score | Tech | Fund | Mcap | Summary |")
    lines.append("|---|--------|-------|------|------|------|---------|")
    for c in result.candidates:
        mcap_b = c.market_cap / 1e9
        lines.append(
            f"| {c.rank} | **{c.ticker}** | {c.composite_score:.0f} | "
            f"{c.technical.technical_score:.0f} | {c.fundamental.fundamental_score:.0f} | "
            f"${mcap_b:.1f}B | {c.summary} |"
        )
    lines.append("")
    lines.append("## Detail")
    lines.append("")
    for c in result.candidates:
        lines.append(f"### {c.rank}. {c.ticker} — {c.name}")
        lines.append("")
        lines.append(f"- Sector: {c.sector_desc} (SIC {c.sector_sic})")
        lines.append(f"- Market cap: ${c.market_cap/1e9:.2f}B")
        lines.append(f"- Composite: **{c.composite_score:.1f}** (tech {c.technical.technical_score:.0f} · fund {c.fundamental.fundamental_score:.0f})")
        if c.technical.last_close:
            lines.append(
                f"- Price: ${c.technical.last_close:.2f} · "
                f"50d MA ${c.technical.ma_50:.2f} · 200d MA ${c.technical.ma_200:.2f} · "
                f"RSI {c.technical.rsi_14:.0f} · vol {c.technical.vol_expansion:.2f}x"
            )
            lines.append(
                f"- 52w high: ${c.technical.high_252:.2f} ({c.technical.pct_off_high*100:+.1f}%) · "
                f"52w low: ${c.technical.low_252:.2f} ({c.technical.pct_above_low*100:+.1f}%) · "
                f"base consolidation: {c.technical.base_consolidation_days}d"
            )
        if c.fundamental.revenue_yoy:
            yoy_strs = [f"{y*100:+.1f}%" for y in c.fundamental.revenue_yoy[-4:]]
            lines.append(f"- Revenue YoY (last {len(yoy_strs)}Q): {' → '.join(yoy_strs)}")
        if c.fundamental.gross_margin_quarterly:
            gm_strs = [f"{g*100:.1f}%" for g in c.fundamental.gross_margin_quarterly[-4:]]
            lines.append(f"- Gross margin (last {len(gm_strs)}Q): {' → '.join(gm_strs)}")
        if c.notable_flags:
            lines.append(f"- Flags: {', '.join(c.notable_flags)}")
        lines.append("")

    path.write_text("\n".join(lines))


def write_top_tickers(result: ScreenerResult, path: Path, *, top: int) -> None:
    """Plain-text list of top-N tickers, one per line — for piping to runners."""
    tickers = [c.ticker for c in result.candidates[:top]]
    path.write_text("\n".join(tickers) + ("\n" if tickers else ""))
