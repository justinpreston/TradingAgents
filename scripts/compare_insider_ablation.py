"""Compare verdict ledgers across the broken/ablated/fixed insider-tool trio.

Usage:
    python scripts/compare_insider_ablation.py \\
        --broken  runs/matrix_refresh_priortier_ab_2026-05-08_2053 \\
        --ablated runs/matrix_ablation_no_insider_<ts> \\
        --fixed   runs/matrix_fixed_insider_<ts>

Output: a markdown table to stdout showing per-ticker classification,
conservative PT, PT compression %, and derived tier in each of the three
states, plus a summary count of "demotions reversed" under each
condition. The point is to attribute the 70% demotion rate empirically
between (a) the broken-tool retry-and-hedge artifact (ablate fixes it),
(b) actual insider-data signal (only fix benefits), and (c) data drift /
unexplained residual (neither fixes it).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradingagents.tiers import tier_for_row, tier_rank


def _tier(row: dict) -> str:
    """Delegates to tradingagents.tiers.tier_for_row (canonical source of
    truth)."""
    return tier_for_row(row, suspect_caps_a=False)


def _load_rows(run_dir: Path) -> dict[str, dict]:
    p = run_dir / "verdict_ledger.json"
    if not p.exists():
        raise SystemExit(f"verdict_ledger.json missing in {run_dir}")
    payload = json.loads(p.read_text())
    rows = payload["rows"] if isinstance(payload, dict) else payload
    return {r["ticker"]: r for r in rows}


def _tier_rank(tier: str) -> int:
    """Numeric rank where larger = better verdict.

    VETO < — < C < B < A. Used to quantify "did this ticker move *up*
    the conviction ladder" (any upgrade counts as a revert, not just
    pick-status flips). Delegates to tradingagents.tiers.tier_rank.
    """
    return tier_rank(tier)


def _is_pick(tier: str) -> bool:
    """Tier C is still a PICK (long-call recommendation at starter size);
    only VETO and the no-verdict sentinel (—) are *not* picks."""
    return tier in {"A", "B", "C"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broken", type=Path, required=True,
                    help="Run with the broken insider tool (today's baseline).")
    ap.add_argument("--ablated", type=Path, required=True,
                    help="Run with TRADINGAGENTS_DISABLE_INSIDER_TXNS=1.")
    ap.add_argument("--fixed", type=Path, required=True,
                    help="Run with the ToolNode fix and AV/yfinance vendor live.")
    args = ap.parse_args()

    broken = _load_rows(args.broken)
    ablated = _load_rows(args.ablated)
    fixed = _load_rows(args.fixed)

    tickers = sorted(set(broken) & set(ablated) & set(fixed))

    print("# Insider-tool ablation comparison\n")
    print(f"* broken : `{args.broken.name}` ({len(broken)} tickers)")
    print(f"* ablated: `{args.ablated.name}` ({len(ablated)} tickers)")
    print(f"* fixed  : `{args.fixed.name}` ({len(fixed)} tickers)\n")
    print(f"Ablation cohort: {tickers}\n")
    print("| ticker | broken | ablated | fixed | abl Δ | fix Δ | revert by ablation | revert by fix |")
    print("|---|---|---|---|---|---|---|---|")
    abl_revert = 0
    fix_revert = 0
    fix_only = 0
    abl_only = 0
    both_revert = 0
    no_revert = 0
    for t in tickers:
        b_tier = _tier(broken[t])
        a_tier = _tier(ablated[t])
        f_tier = _tier(fixed[t])
        b_rank = _tier_rank(b_tier)
        a_rank = _tier_rank(a_tier)
        f_rank = _tier_rank(f_tier)
        # Was the ticker demoted in the broken baseline (only such cases
        # are eligible for reverting). The "broken cohort" = the set of
        # tickers we picked precisely because they were demoted.
        was_demoted = b_tier in {"VETO", "C"}
        # An upgrade in the tier ladder counts as a revert. VETO→C, C→B,
        # C→A, etc. Same tier or downgrade is not a revert.
        a_revert = was_demoted and a_rank > b_rank
        f_revert = was_demoted and f_rank > b_rank
        if a_revert:
            abl_revert += 1
        if f_revert:
            fix_revert += 1
        if a_revert and f_revert:
            both_revert += 1
        elif a_revert:
            abl_only += 1
        elif f_revert:
            fix_only += 1
        elif was_demoted:
            no_revert += 1
        a_delta = (
            f"+{a_rank - b_rank}" if a_rank > b_rank
            else (f"{a_rank - b_rank}" if a_rank < b_rank else "·")
        )
        f_delta = (
            f"+{f_rank - b_rank}" if f_rank > b_rank
            else (f"{f_rank - b_rank}" if f_rank < b_rank else "·")
        )
        print(
            f"| {t} | {b_tier} | {a_tier} | {f_tier} | {a_delta} | {f_delta} "
            f"| {'✅' if a_revert else '—'} | {'✅' if f_revert else '—'} |"
        )

    n_demoted = sum(
        1 for t in tickers if _tier(broken[t]) in {"VETO", "C"}
    )
    print(f"\nDemoted in broken baseline: {n_demoted}")
    print(f"  reverted by ablation only       : {abl_only}")
    print(f"  reverted by fix only            : {fix_only}")
    print(f"  reverted by both                : {both_revert}")
    print(f"  not reverted (residual / drift) : {no_revert}")

    print("\n## Interpretation guide\n")
    print(
        "* Reverts under **ablation but not fix** → broken-tool retry-and-hedge "
        "was widening cons PT bands. yfinance/AV insider data isn't strong "
        "enough to compensate for the caveats it triggers."
    )
    print(
        "* Reverts under **fix but not ablation** → real insider data "
        "(yfinance Form-4 / AV) is providing usable signal that the agent "
        "synthesizes into a tighter cons PT. Tool was net-positive when "
        "actually working."
    )
    print(
        "* Reverts under **both** → tool's mere presence (broken or working) "
        "doesn't drive the demotion. Either the prompt's mention of the tool "
        "matters, or there's run-to-run noise. Inspect the cons frame's "
        "report text directly."
    )
    print(
        "* Not reverted → divergence is data drift (Polygon Basic vendor swap, "
        "1-week price/IV change) rather than agent code drift. Compare the "
        "fundamentals tool outputs week-over-week."
    )


if __name__ == "__main__":
    main()
