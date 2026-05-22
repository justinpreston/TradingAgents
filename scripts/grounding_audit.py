#!/usr/bin/env python3
"""Grounding audit — flag fabrication risk in a completed matrix run.

The matrix pipeline contains several agent nodes that produce content
without grounding tools (bull/bear researchers, risk debators, research
manager, portfolio manager). Their outputs carry specific numerical
claims (PTs, YoY percentages, analyst-PT references, insider counts,
quoted "Q3 beat by 12%" snippets) that the LLM picks up under persona
pressure. Some of those claims are grounded in the upstream analyst
reports. Some are confabulated from training-data memory.

This audit doesn't try to verify every claim — that would require
re-running the analysts. Instead it produces a deterministic,
per-ticker grounding risk score by combining four orthogonal signals:

  1. **PT sanity** — distance of aggressive/conservative PT from
     current price (from verdict_ledger.json `pt_quality_flags`).
     SUSPECT PTs are the highest-severity individual signal because
     they directly drive tier classification and option-sizing.

  2. **Data gaps in analyst reports** — count of ``[[TOOL_ERROR:`` or
     ``## ⚠️ Data Gaps`` blocks across the four per-cell analyst
     reports. Picks where multiple analyst reports are gap-tagged
     should not get full conviction.

  3. **Unsourced specific numerics in persona output** — regex over the
     full investment_debate_state.history + investment_plan +
     final_trade_decision counting unsourced numerical claims (specific
     percentages, "PT raised/cut to $X", "Q\\d FY\\d{4} beat/miss",
     insider-cluster mentions). These are the exact shapes of LLM
     fabrication observed in the social_media_analyst incident.

  4. **PT vs analyst consensus** — when yfinance returns a populated
     analyst-PT consensus, compute the % distance between the
     conservative_pt (the more cautious frame) and the consensus mean.
     Flag picks where the spread is large (≥20% by default).

Output:
  - <matrix-run>/grounding_audit.json — structured per-ticker scores
  - <matrix-run>/grounding_audit.md   — human-readable summary

The script is fail-soft. If a particular signal can't be computed
(state file missing, yfinance unreachable, etc.) it's marked as such
in the output rather than crashing the run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dataflows.analyst_pt_consensus import (  # noqa: E402
    AnalystPriceTargets,
    fetch_analyst_pt_consensus,
)
from tradingagents.dataflows.tool_errors import (  # noqa: E402
    extract_tool_errors,
)


# ---------------------------------------------------------------------------
# Regex catalog for "unsourced specific numeric" detection.
#
# These patterns target the *shape* of fabrication-prone claims, not the
# substance. A high count is a flag for human review, not a hard veto.
# ---------------------------------------------------------------------------
_NUMERIC_CLAIM_PATTERNS = {
    # "Q3 EPS beat by 12%" / "Q1 FY2024 missed by 8.4%"
    "earnings_beat_miss": re.compile(
        r"Q[1-4](\s+FY\d{2,4})?\s+(EPS|revenue|earnings)?\s*(beat|miss(ed)?|topped|fell short)\s+(of|by)?\s*\$?\d+(\.\d+)?%?",
        re.IGNORECASE,
    ),
    # "JPM raised PT to $200" / "Goldman cut target to $145"
    "analyst_action": re.compile(
        r"\b(JPM|Goldman|Morgan Stanley|BofA|Bank of America|Citi|Barclays|UBS|Wells Fargo|Jefferies|Wedbush|Mizuho|Bernstein)\s+(raised|cut|initiated|reiterated|upgraded|downgraded)\s+(PT|target|price target|rating)\s+(to|at)\s+\$\d+",
        re.IGNORECASE,
    ),
    # "Insiders bought 50,000 shares" / "officer sold 25k shares"
    "insider_cluster": re.compile(
        r"insider(s)?\s+(bought|sold|purchased|acquired)\s+(\d{1,3}(,\d{3})*|\d+k|\d+m)\s+shares",
        re.IGNORECASE,
    ),
    # "+23% YoY in 2024" / "down 15% year-over-year"
    "yoy_specifics": re.compile(
        r"(up|down|grew|fell|increased|decreased)\s+(of\s+)?\d+(\.\d+)?\s*%\s+(YoY|year-over-year|y/y|year on year)",
        re.IGNORECASE,
    ),
    # "raised guidance to $4.50-$4.70"
    "guidance_specifics": re.compile(
        r"(raised|cut|lowered|reaffirmed|reiterated)\s+(FY\d{2,4}\s+)?guidance\s+(to|of|at)\s+\$\d+(\.\d+)?",
        re.IGNORECASE,
    ),
}

# Anti-pattern: numerics inside a Data Gaps block don't count as fabrication
# — they're labeled as questionable. Strip those blocks before counting.
_DATA_GAPS_BLOCK_RE = re.compile(
    r"## ⚠️ Data Gaps.*?(?=\n## |\Z)",
    re.DOTALL,
)


@dataclass
class TickerGroundingScore:
    ticker: str
    tier: Optional[str] = None
    classification: Optional[str] = None
    # PT sanity (signal #1)
    pt_quality_flags: list[str] = field(default_factory=list)
    aggressive_pt: Optional[float] = None
    conservative_pt: Optional[float] = None
    current_price: Optional[float] = None
    aggressive_pt_distance_pct: Optional[float] = None
    conservative_pt_distance_pct: Optional[float] = None
    # Data gaps in analyst reports (signal #2)
    analyst_data_gaps: dict = field(default_factory=dict)  # report_key -> error_count
    n_analyst_reports_with_gaps: int = 0
    sentiment_insufficient: bool = False
    # Unsourced numerics in persona output (signal #3)
    unsourced_numeric_claims: dict = field(default_factory=dict)  # pattern_name -> count
    n_unsourced_numeric_claims: int = 0
    # Analyst PT consensus check (signal #4)
    analyst_consensus_mean: Optional[float] = None
    analyst_consensus_high: Optional[float] = None
    analyst_consensus_low: Optional[float] = None
    analyst_consensus_source: Optional[str] = None
    conservative_pt_vs_consensus_pct: Optional[float] = None
    consensus_divergence_flag: bool = False
    # Roll-up
    risk_score: float = 0.0  # 0=clean, increases with severity
    risk_level: str = "OK"   # OK | WATCH | ELEVATED | HIGH


def _load_verdict_ledger(matrix_run: Path) -> dict:
    p = matrix_run / "verdict_ledger.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _load_cell_state(matrix_run: Path, profile: str, ticker: str) -> Optional[dict]:
    p = matrix_run / "cells" / profile / ticker / f"{ticker}.state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _strip_data_gaps_blocks(text: str) -> str:
    """Remove ``## ⚠️ Data Gaps`` blocks so their numerics aren't double-counted."""
    return _DATA_GAPS_BLOCK_RE.sub("", text or "")


def _count_unsourced_numerics(text: str) -> dict[str, int]:
    """Return per-pattern hit counts for unsourced numeric claim shapes."""
    if not text:
        return {}
    cleaned = _strip_data_gaps_blocks(text)
    counts: dict[str, int] = {}
    for name, pattern in _NUMERIC_CLAIM_PATTERNS.items():
        hits = pattern.findall(cleaned)
        if hits:
            counts[name] = len(hits)
    return counts


def _scan_analyst_reports(cell_state: dict) -> tuple[dict[str, int], bool]:
    """Count tool errors per analyst report; detect INSUFFICIENT_SENTIMENT_DATA."""
    if not cell_state:
        return {}, False
    gap_counts: dict[str, int] = {}
    sentiment_insufficient = False
    for key in ("market_report", "news_report", "fundamentals_report", "sentiment_report"):
        body = cell_state.get(key)
        if not body:
            continue
        errs = extract_tool_errors(body)
        if errs:
            gap_counts[key] = len(errs)
        if key == "sentiment_report" and "INSUFFICIENT SENTIMENT DATA" in body:
            sentiment_insufficient = True
    return gap_counts, sentiment_insufficient


def _persona_text(cell_state: Optional[dict]) -> str:
    """Concatenate the persona-generated text streams into one searchable blob."""
    if not cell_state:
        return ""
    parts: list[str] = []
    debate = cell_state.get("investment_debate_state") or {}
    for k in ("history", "bull_history", "bear_history", "judge_decision", "current_response"):
        v = debate.get(k)
        if isinstance(v, str):
            parts.append(v)
    for k in ("investment_plan", "trader_investment_plan", "final_trade_decision"):
        v = cell_state.get(k)
        if isinstance(v, str):
            parts.append(v)
    risk = cell_state.get("risk_debate_state") or {}
    for k in (
        "history",
        "current_response",
        "risky_history",
        "safe_history",
        "neutral_history",
        "judge_decision",
    ):
        v = risk.get(k)
        if isinstance(v, str):
            parts.append(v)
    return "\n\n".join(parts)


def _score_ticker(
    row: dict,
    aggressive_cell: Optional[dict],
    conservative_cell: Optional[dict],
    consensus: AnalystPriceTargets,
    consensus_divergence_pct: float,
) -> TickerGroundingScore:
    score = TickerGroundingScore(
        ticker=row["ticker"],
        classification=row.get("classification"),
        pt_quality_flags=list(row.get("pt_quality_flags") or []),
        aggressive_pt=row.get("aggressive_pt"),
        conservative_pt=row.get("conservative_pt"),
        current_price=row.get("current_price_usd"),
        aggressive_pt_distance_pct=row.get("aggressive_pt_distance_pct"),
        conservative_pt_distance_pct=row.get("conservative_pt_distance_pct"),
    )

    # Signal #2 — data gaps across analyst reports (use whichever cell is
    # populated; aggressive runs first and is required for every PICK).
    primary_cell = aggressive_cell or conservative_cell
    gap_counts, sentiment_insufficient = _scan_analyst_reports(primary_cell or {})
    score.analyst_data_gaps = gap_counts
    score.n_analyst_reports_with_gaps = len(gap_counts)
    score.sentiment_insufficient = sentiment_insufficient

    # Signal #3 — unsourced numerics in persona text.
    persona_text = _persona_text(aggressive_cell) + "\n\n" + _persona_text(conservative_cell)
    numeric_counts = _count_unsourced_numerics(persona_text)
    score.unsourced_numeric_claims = numeric_counts
    score.n_unsourced_numeric_claims = sum(numeric_counts.values())

    # Signal #4 — PT consensus check.
    if consensus.is_populated() and consensus.mean and row.get("conservative_pt"):
        score.analyst_consensus_mean = consensus.mean
        score.analyst_consensus_high = consensus.high
        score.analyst_consensus_low = consensus.low
        score.analyst_consensus_source = consensus.source
        divergence = (row["conservative_pt"] - consensus.mean) / consensus.mean * 100
        score.conservative_pt_vs_consensus_pct = round(divergence, 1)
        if abs(divergence) >= consensus_divergence_pct:
            score.consensus_divergence_flag = True

    # Risk roll-up. Weights tuned so a single SUSPECT PT puts a ticker in
    # ELEVATED, a SUSPECT PT + multiple gaps + many numeric claims → HIGH.
    risk = 0.0
    risk += 8.0 * len(score.pt_quality_flags)           # PT sanity (most severe)
    risk += 4.0 * score.n_analyst_reports_with_gaps     # data gaps
    risk += 5.0 if score.sentiment_insufficient else 0  # sentiment short-circuit
    risk += 0.5 * score.n_unsourced_numeric_claims      # specific-numeric shapes
    risk += 3.0 if score.consensus_divergence_flag else 0  # PT vs street

    score.risk_score = round(risk, 1)
    if risk == 0:
        score.risk_level = "OK"
    elif risk < 5:
        score.risk_level = "WATCH"
    elif risk < 12:
        score.risk_level = "ELEVATED"
    else:
        score.risk_level = "HIGH"

    return score


def _tier_for_row(row: dict) -> Optional[str]:
    cls = row.get("classification")
    if cls == "VETOED":
        return "VETO"
    if cls != "PICK":
        return None
    if row.get("conservative_pt") is None:
        return "C"
    suspect = row.get("pt_quality_flags") or []
    comp = row.get("pt_compression_pct")
    if comp is not None and comp < 5.0 and not suspect:
        return "A"
    return "B"


def audit_matrix_run(
    matrix_run: Path,
    *,
    consensus_divergence_pct: float = 20.0,
    skip_consensus: bool = False,
) -> dict:
    ledger = _load_verdict_ledger(matrix_run)
    rows = ledger.get("rows", [])
    audits: list[TickerGroundingScore] = []
    for row in rows:
        ticker = row["ticker"]
        aggr = _load_cell_state(matrix_run, "aggressive", ticker)
        cons = _load_cell_state(matrix_run, "conservative", ticker)

        consensus = AnalystPriceTargets(
            ticker=ticker,
            current=None, high=None, low=None, mean=None, median=None,
            number_of_analysts=None, source="unavailable",
        )
        if not skip_consensus:
            try:
                consensus = fetch_analyst_pt_consensus(ticker)
            except Exception:
                pass

        s = _score_ticker(row, aggr, cons, consensus, consensus_divergence_pct)
        s.tier = _tier_for_row(row)
        audits.append(s)

    # Sort: HIGH/ELEVATED first, then by risk score desc, then by ticker.
    severity_order = {"HIGH": 0, "ELEVATED": 1, "WATCH": 2, "OK": 3}
    audits.sort(key=lambda a: (severity_order.get(a.risk_level, 9), -a.risk_score, a.ticker))

    summary = {
        "matrix_run": matrix_run.name,
        "n_tickers": len(audits),
        "n_high": sum(1 for a in audits if a.risk_level == "HIGH"),
        "n_elevated": sum(1 for a in audits if a.risk_level == "ELEVATED"),
        "n_watch": sum(1 for a in audits if a.risk_level == "WATCH"),
        "n_ok": sum(1 for a in audits if a.risk_level == "OK"),
        "n_suspect_pt": sum(1 for a in audits if a.pt_quality_flags),
        "n_with_data_gaps": sum(1 for a in audits if a.n_analyst_reports_with_gaps > 0),
        "n_consensus_diverged": sum(1 for a in audits if a.consensus_divergence_flag),
        "n_sentiment_insufficient": sum(1 for a in audits if a.sentiment_insufficient),
        "rules": {
            "pt_suspect_threshold_pct": 50.0,
            "consensus_divergence_threshold_pct": consensus_divergence_pct,
            "risk_levels": {
                "OK": "0",
                "WATCH": "0 < risk < 5",
                "ELEVATED": "5 ≤ risk < 12",
                "HIGH": "≥ 12",
            },
        },
        "rows": [asdict(a) for a in audits],
    }
    return summary


def _build_markdown(summary: dict) -> str:
    lines = [
        f"# Grounding Audit — {summary['matrix_run']}",
        "",
        "_Per-ticker fabrication-risk scoring based on PT sanity, analyst-report data gaps, unsourced specific numerics in persona output, and PT divergence from sell-side consensus._",
        "",
        "## Roll-up",
        "",
        f"- Tickers audited: **{summary['n_tickers']}**",
        f"- 🔴 HIGH risk: **{summary['n_high']}**",
        f"- 🟠 ELEVATED risk: **{summary['n_elevated']}**",
        f"- 🟡 WATCH: **{summary['n_watch']}**",
        f"- 🟢 OK: **{summary['n_ok']}**",
        "",
        "### Signal counts",
        "",
        f"- Suspect PTs (>50% from current): **{summary['n_suspect_pt']}**",
        f"- Picks with analyst-report data gaps: **{summary['n_with_data_gaps']}**",
        f"- PTs diverging >20% from analyst consensus: **{summary['n_consensus_diverged']}**",
        f"- Sentiment cells short-circuited (INSUFFICIENT): **{summary['n_sentiment_insufficient']}**",
        "",
        "## Per-ticker (ordered by risk)",
        "",
        "| Ticker | Tier | Risk | Score | Flags |",
        "|---|---|---|---:|---|",
    ]
    for r in summary["rows"]:
        if r["risk_level"] == "OK":
            continue
        emoji = {"HIGH": "🔴", "ELEVATED": "🟠", "WATCH": "🟡", "OK": "🟢"}[r["risk_level"]]
        flags: list[str] = []
        if r["pt_quality_flags"]:
            flags.append(f"PT-suspect ({', '.join(r['pt_quality_flags'])})")
        if r["n_analyst_reports_with_gaps"]:
            flags.append(f"{r['n_analyst_reports_with_gaps']} report(s) with gaps")
        if r["sentiment_insufficient"]:
            flags.append("sentiment insufficient")
        if r["consensus_divergence_flag"]:
            div = r["conservative_pt_vs_consensus_pct"]
            mean = r["analyst_consensus_mean"]
            flags.append(f"cons_pt {div:+.1f}% vs street mean ${mean:.2f}")
        if r["n_unsourced_numeric_claims"]:
            top = sorted(r["unsourced_numeric_claims"].items(), key=lambda x: -x[1])
            top_str = ", ".join(f"{k}:{v}" for k, v in top[:3])
            flags.append(f"{r['n_unsourced_numeric_claims']} numeric claims ({top_str})")
        flag_text = "; ".join(flags) if flags else "—"
        lines.append(f"| {r['ticker']} | {r['tier'] or '—'} | {emoji} {r['risk_level']} | {r['risk_score']} | {flag_text} |")

    if summary["n_high"] == 0 and summary["n_elevated"] == 0 and summary["n_watch"] == 0:
        lines.append("| _all clear_ | | | | |")

    lines.extend([
        "",
        "## How to read this report",
        "",
        "**Signals (per ticker):**",
        "",
        "1. **PT sanity** — flagged when persona PT is more than ±50% from current price. The matrix has historically produced these (e.g. an $140 PT on a $108 stock) when the LLM hallucinates from a stale point-in-time prior. Tier A is automatically blocked when any PT is suspect.",
        "2. **Analyst-report data gaps** — count of tool errors detected in the per-cell analyst reports. A `## ⚠️ Data Gaps` block in any report means part of the report is unsupported.",
        "3. **Unsourced specific numerics in persona output** — regex hits for fabrication-prone shapes: earnings-beat/miss specifics, named-bank PT changes, insider transaction counts, YoY specifics, guidance ranges. High counts mean human review before sizing.",
        "4. **PT vs analyst consensus** — when yfinance returns a populated street consensus, flag when conservative_pt diverges by ≥20% from the consensus mean. Either the persona or the street is wrong; either way, conviction should be tempered.",
        "",
        "**Mitigation guidance:**",
        "",
        "- 🔴 **HIGH**: do not size up to full conviction. Manual review required. Consider exiting or skipping.",
        "- 🟠 **ELEVATED**: cut starter size by half. Verify suspect signals manually before entry.",
        "- 🟡 **WATCH**: track the signal but starter-size entry is acceptable.",
        "- 🟢 **OK**: standard sizing per the tier rules and policy.",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--matrix-run", required=True, help="Path to a matrix run directory.")
    parser.add_argument(
        "--consensus-divergence-pct",
        type=float,
        default=20.0,
        help="Percent distance from analyst consensus mean above which to flag (default: 20).",
    )
    parser.add_argument(
        "--skip-consensus",
        action="store_true",
        help="Skip the yfinance analyst PT consensus fetch (faster; loses signal #4).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional explicit path for grounding_audit.json (default: <matrix-run>/grounding_audit.json).",
    )
    args = parser.parse_args()

    matrix_run = Path(args.matrix_run).resolve()
    if not matrix_run.exists():
        print(f"[grounding-audit] ERROR: matrix-run not found: {matrix_run}", file=sys.stderr)
        return 1
    if not (matrix_run / "verdict_ledger.json").exists():
        print(
            f"[grounding-audit] ERROR: no verdict_ledger.json under {matrix_run}. "
            f"Run scripts/build_run_accounting.py first.",
            file=sys.stderr,
        )
        return 1

    print(f"[grounding-audit] auditing {matrix_run.name} ...")
    summary = audit_matrix_run(
        matrix_run,
        consensus_divergence_pct=args.consensus_divergence_pct,
        skip_consensus=args.skip_consensus,
    )

    out_json = Path(args.output) if args.output else (matrix_run / "grounding_audit.json")
    out_json.write_text(json.dumps(summary, indent=2))
    out_md = matrix_run / "grounding_audit.md"
    out_md.write_text(_build_markdown(summary))

    print(
        f"[grounding-audit] {summary['matrix_run']} · "
        f"HIGH={summary['n_high']} ELEVATED={summary['n_elevated']} "
        f"WATCH={summary['n_watch']} OK={summary['n_ok']} "
        f"(suspect_pt={summary['n_suspect_pt']}, "
        f"data_gaps={summary['n_with_data_gaps']}, "
        f"sentiment_insufficient={summary['n_sentiment_insufficient']})"
    )
    print(f"[grounding-audit] wrote {out_json.relative_to(matrix_run.parent)} + grounding_audit.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
