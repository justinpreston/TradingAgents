"""Side-by-side comparison of two TradingAgents persona runs.

Reads two ``runs/<run-id>/summary.json`` files (or directories containing
them) and produces a markdown comparison table covering:

  * Final ratings (and the delta direction)
  * Trader's transaction proposals (the upstream divergence point)
  * Price targets and stop-loss levels (parsed from final_trade_decision)
  * Token / wall-clock cost

The Trader divergence is highlighted because the analysis of the
2026-04-30 top-5 runs surfaced that rating flips are dominated by the
Trader's BUY/SELL/HOLD proposal, not the PM's synthesis. The Trader
proposal itself is driven by which Research Manager model wrote the
investment plan (Opus = nuanced 'build over time'; GPT-5.5 = decisive
rubric). Surfacing this column makes the flip mechanism legible.

Usage::

    python compare_runs.py runs/persona_aligned_2026-04-30_top5 \\
                            runs/aggressive_riskprof_2026-04-30_top5

    # Write the table somewhere persistent:
    python compare_runs.py runs/persona_aligned_2026-04-30_top5 \\
                            runs/aggressive_riskprof_2026-04-30_top5 \\
                            --out runs/compare_2026-04-30_top5.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional


RATING_ORDER = {
    "Sell": -2,
    "Underweight": -1,
    "Hold": 0,
    "Overweight": 1,
    "Buy": 2,
}


def _delta_arrow(left: str, right: str) -> str:
    if not left or not right or left == right:
        return "—"
    li = RATING_ORDER.get(left, 0)
    ri = RATING_ORDER.get(right, 0)
    if ri > li:
        return "⬆"
    if ri < li:
        return "⬇"
    return "↔"


def _parse_field(decision_md: str, field: str) -> str:
    """Pull a `**Field**: value` line off the rendered PM markdown."""
    if not decision_md:
        return ""
    pattern = rf"\*\*{re.escape(field)}\*\*:\s*([^\n]+)"
    m = re.search(pattern, decision_md)
    return m.group(1).strip() if m else ""


def _parse_stop_from_thesis(decision_md: str) -> str:
    """Extract a stop-loss level mentioned in the Executive Summary or thesis.

    The PM prompts encourage the synthesizer to cite stop levels narratively
    rather than as a structured field. We extract them with a forgiving
    regex so the comparison row still has a stop column.
    """
    if not decision_md:
        return ""
    candidates = re.findall(
        r"(?:stop[- ]?loss|stop)\s*(?:at|near|of|around)?\s*\$?(\d{2,4}(?:\.\d+)?)",
        decision_md,
        flags=re.IGNORECASE,
    )
    if not candidates:
        return ""
    # Use the first cited stop — typically the most prominent.
    return f"${candidates[0]}"


def _load_summary(path_arg: Path) -> tuple[Path, dict]:
    """Resolve a summary.json from either a directory or a direct path."""
    if path_arg.is_dir():
        summary = path_arg / "summary.json"
        if not summary.exists():
            raise FileNotFoundError(f"summary.json not found in {path_arg}")
        return path_arg, json.loads(summary.read_text())
    summary = path_arg
    if not summary.exists():
        raise FileNotFoundError(f"file not found: {summary}")
    return summary.parent, json.loads(summary.read_text())


def _load_trader_proposal(run_dir: Path, ticker: str) -> str:
    log_path = run_dir / f"{ticker}.log"
    if not log_path.exists():
        return ""
    last = ""
    with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            if "FINAL TRANSACTION PROPOSAL" in line:
                m = re.search(r"\b(BUY|SELL|HOLD)\b", line)
                if m:
                    last = m.group(1)
    return last


def _index_results(run_data: dict) -> dict[str, dict]:
    return {r["ticker"]: r for r in run_data.get("results", [])}


def _label(run_data: dict, run_dir: Path) -> str:
    """Build a short label combining alignment and risk_profile."""
    align = run_data.get("alignment") or "persona"
    profile = run_data.get("risk_profile")
    if profile:
        return f"{align}+{profile}"
    return align


def _build_markdown(left_dir: Path, left: dict, right_dir: Path, right: dict) -> str:
    left_label = _label(left, left_dir)
    right_label = _label(right, right_dir)
    left_ix = _index_results(left)
    right_ix = _index_results(right)

    tickers = []
    seen = set()
    for r in left.get("results", []) + right.get("results", []):
        t = r.get("ticker")
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)

    lines = []
    lines.append(f"# Run comparison")
    lines.append("")
    lines.append(f"- **Left**:  `{left_dir.name}`  ({left_label})")
    lines.append(f"- **Right**: `{right_dir.name}`  ({right_label})")
    lines.append(f"- **Trade date**: {left.get('trade_date', '?')}  vs  {right.get('trade_date', '?')}")
    lines.append("")
    lines.append("## Ratings & trader proposals")
    lines.append("")
    lines.append(
        "| Ticker | Trader (L) | PM Rating (L) | Trader (R) | PM Rating (R) | Δ Rating | PT (L) | PT (R) | Stop (L) | Stop (R) |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|"
    )

    for t in tickers:
        l = left_ix.get(t, {})
        r = right_ix.get(t, {})
        l_decision = l.get("final_trade_decision", "")
        r_decision = r.get("final_trade_decision", "")
        l_rating = l.get("signal", "")
        r_rating = r.get("signal", "")
        l_trader = _load_trader_proposal(left_dir, t)
        r_trader = _load_trader_proposal(right_dir, t)
        l_pt = _parse_field(l_decision, "Price Target") or "—"
        r_pt = _parse_field(r_decision, "Price Target") or "—"
        l_stop = _parse_stop_from_thesis(l_decision) or "—"
        r_stop = _parse_stop_from_thesis(r_decision) or "—"
        delta = _delta_arrow(l_rating, r_rating)

        lines.append(
            f"| **{t}** | {l_trader or '—'} | {l_rating or '—'} | {r_trader or '—'} | "
            f"{r_rating or '—'} | {delta} | {l_pt} | {r_pt} | {l_stop} | {r_stop} |"
        )

    lines.append("")
    lines.append("## Cost & timing")
    lines.append("")
    lines.append("| Ticker | LLM calls (L→R) | Tokens in (L→R) | Tokens out (L→R) | Wall (L→R) |")
    lines.append("|---|---|---|---|---|")
    for t in tickers:
        l = left_ix.get(t, {})
        r = right_ix.get(t, {})
        lines.append(
            f"| **{t}** | "
            f"{l.get('llm_calls', '?')}→{r.get('llm_calls', '?')} | "
            f"{l.get('tokens_in', '?')}→{r.get('tokens_in', '?')} | "
            f"{l.get('tokens_out', '?')}→{r.get('tokens_out', '?')} | "
            f"{l.get('elapsed_sec', '?')}s→{r.get('elapsed_sec', '?')}s |"
        )

    lines.append("")
    lines.append("## Per-ticker thesis snippets")
    lines.append("")
    for t in tickers:
        l = left_ix.get(t, {})
        r = right_ix.get(t, {})
        lines.append(f"### {t}")
        lines.append("")
        lines.append(f"**{left_label}** — {l.get('signal', '?')}")
        l_summary = _parse_field(l.get("final_trade_decision", ""), "Executive Summary")
        if l_summary:
            lines.append(f"> {l_summary}")
        lines.append("")
        lines.append(f"**{right_label}** — {r.get('signal', '?')}")
        r_summary = _parse_field(r.get("final_trade_decision", ""), "Executive Summary")
        if r_summary:
            lines.append(f"> {r_summary}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("left", type=Path, help="Left run directory or summary.json")
    p.add_argument("right", type=Path, help="Right run directory or summary.json")
    p.add_argument("--out", type=Path, default=None, help="Write markdown to this path (default: stdout)")
    args = p.parse_args()

    try:
        left_dir, left = _load_summary(args.left)
        right_dir, right = _load_summary(args.right)
    except FileNotFoundError as exc:
        sys.stderr.write(f"❌ {exc}\n")
        return 2

    md = _build_markdown(left_dir, left, right_dir, right)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"📝 {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
