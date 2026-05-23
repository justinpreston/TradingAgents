"""Specific contract-level trade tickets — open / close / roll / trim.

Where `portfolio_check.py` answers "what's the recommendation per position?",
this script produces a unified, sorted **action list** for the whole book:

  1. Per-position rebalance tickets (EXIT / TRIM / ROLL / ADD / HOLD)
  2. New-pick tickets (matrix tier-A or tier-B picks not yet in your book)
  3. Each ticket includes specific contract details (strike, expiry, qty,
     premium, breakeven) so you can copy them directly into the broker.

Sorted by `policy.json::rebalance_priority`: exits first, breaches next,
then promotions, then new picks.

Usage::

    .venv/bin/python scripts/portfolio_trade_tickets.py
    .venv/bin/python scripts/portfolio_trade_tickets.py --include-new-picks
    .venv/bin/python scripts/portfolio_trade_tickets.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.portfolio_load_context import (  # noqa: E402
    DEFAULT_POLICY,
    DEFAULT_POSITIONS,
    build_context,
)
from scripts.portfolio_check import recommend_for_position  # noqa: E402
from scripts.portfolio_allocation import (  # noqa: E402
    aggregate_exposures,
    simulate_candidate,
)


# ---------------------------------------------------------------------------
# New-pick discovery


def _matrix_picks(matrix_run_path: Path) -> list[dict[str, Any]]:
    overlay_path = matrix_run_path / "options_overlay.json"
    if not overlay_path.exists():
        return []
    doc = json.loads(overlay_path.read_text())
    return [
        ov for ov in doc.get("overlays", [])
        if ov.get("tier") in {"A", "B", "C"}
    ]


def discover_new_picks(
    ctx: dict[str, Any],
    held_tickers: set[str],
    tiers_to_consider: tuple[str, ...] = ("A", "B"),
    max_new: int = 5,
) -> list[dict[str, Any]]:
    matrix_run = ctx.get("latest_matrix_run")
    if not matrix_run:
        return []
    picks = _matrix_picks(Path(matrix_run))
    new_candidates = [
        p for p in picks
        if p["ticker"] not in held_tickers and p.get("tier") in tiers_to_consider
    ]
    # Prioritise: A before B, then by tightest compression_pct
    new_candidates.sort(
        key=lambda p: (
            {"A": 0, "B": 1, "C": 2}.get(p.get("tier", "C"), 3),
            p.get("compression_pct") if p.get("compression_pct") is not None else 999,
        )
    )
    return new_candidates[:max_new]


# ---------------------------------------------------------------------------
# Priority & rendering

# Rationale-tag → priority bucket → integer priority. Lower = act first.
_PRIORITY_BUCKETS = [
    ("EXIT", 0),
    ("ROLL", 1),
    ("TRIM", 2),
    ("ADD", 3),
    ("OPEN_NEW", 4),
    ("HOLD", 5),
    ("REVIEW", 6),
]


def _ticket_priority(action: str) -> int:
    for a, p in _PRIORITY_BUCKETS:
        if a == action:
            return p
    return 9


def render_tickets_markdown(tickets: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = []
    lines.append("# Trade ticket queue")
    lines.append("")
    lines.append(f"- **As of:** {summary['as_of']}")
    lines.append(f"- **Matrix run:** `{summary.get('matrix_run_id') or '(none)'}`")
    lines.append(f"- **Total tickets:** {len(tickets)}  ({summary['breakdown']})")
    lines.append("")

    if not tickets:
        lines.append("_No actionable tickets._")
        return "\n".join(lines)

    lines.append("## What to do")
    lines.append("")
    for i, t in enumerate(tickets, 1):
        action_emoji = {
            "EXIT": "🛑",
            "ROLL": "🔁",
            "TRIM": "✂️",
            "ADD": "➕",
            "OPEN_NEW": "🌱",
            "HOLD": "⏸",
            "REVIEW": "🔍",
        }.get(t["action"], "•")
        spoken = t.get("spoken_summary") or f"{t['action']} {t['ticker']}"
        lines.append(f"{i}. {action_emoji} **{spoken}**")
    lines.append("")

    lines.append("## At-a-glance")
    lines.append("")
    lines.append("| # | Action | Ticker | Contract | Qty | Underlying / Trigger | Rationale |")
    lines.append("|---|---|---|---|---:|---|---|")
    for i, t in enumerate(tickets, 1):
        action_md = {
            "EXIT": "🛑 **EXIT**",
            "ROLL": "🔁 ROLL",
            "TRIM": "✂️ TRIM",
            "ADD": "➕ ADD",
            "OPEN_NEW": "🌱 **OPEN**",
            "HOLD": "⏸ HOLD",
            "REVIEW": "🔍 REVIEW",
        }.get(t["action"], t["action"])

        contract_md = "—"
        if t.get("contract"):
            c = t["contract"]
            contract_md = (
                f"{c.get('strike')}C exp {c.get('expiry')}"
                + (f" @ ${c.get('premium_per_share')}/sh" if c.get("premium_per_share") else "")
            )

        trigger_md = "—"
        if t.get("underlying_now") is not None:
            trigger_md = f"${t['underlying_now']:.2f}"
            if t.get("trigger_underlying"):
                trigger_md += f" / {t['trigger_underlying']}"

        rationale = "; ".join(t.get("rationale", []))[:120]
        lines.append(
            f"| {i} | {action_md} | {t['ticker']} | {contract_md} | "
            f"{t.get('qty', '—')} | {trigger_md} | {rationale} |"
        )

    lines.append("")
    lines.append("## Detailed tickets")
    lines.append("")
    for i, t in enumerate(tickets, 1):
        lines.append(f"### {i}. {t['action']} {t['ticker']} — {t.get('id') or '(new)'}")
        if t.get("spoken_summary"):
            lines.append("")
            lines.append(f"> **{t['spoken_summary']}**")
            lines.append("")
        if t.get("contract"):
            c = t["contract"]
            lines.append(
                f"- **Contract:** {c.get('strike')}C exp {c.get('expiry')}"
                f" (Δ {c.get('delta', '—')}, IV {c.get('iv', '—')}, OI {c.get('open_interest', '—')})"
            )
            if c.get("premium_per_share"):
                lines.append(
                    f"- **Premium:** ${c['premium_per_share']}/sh"
                    + (f" (${c.get('premium_per_contract_usd')}/contract)" if c.get("premium_per_contract_usd") else "")
                )
            if c.get("breakeven_underlying"):
                lines.append(
                    f"- **Breakeven:** ${c['breakeven_underlying']} "
                    f"({c.get('breakeven_pct_from_current', '—')}% from current)"
                )
        if t.get("qty") is not None:
            lines.append(f"- **Qty:** {t['qty']}")
        if t.get("est_total_usd") is not None:
            lines.append(f"- **Est. total:** ${t['est_total_usd']:,.2f}")
        if t.get("trigger_exit_below") is not None:
            lines.append(f"- **Stop loss (underlying):** ${t['trigger_exit_below']}")
        if t.get("trigger_take_profit_above") is not None:
            lines.append(f"- **Take profit (underlying):** ${t['trigger_take_profit_above']}")
        for r in t.get("rationale", []):
            lines.append(f"- {r}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main


def build_queue(
    positions_path: Path = DEFAULT_POSITIONS,
    policy_path: Path = DEFAULT_POLICY,
    matrix_run_path: Path | None = None,
    include_new_picks: bool = True,
    max_new: int = 5,
) -> dict[str, Any]:
    ctx = build_context(positions_path, policy_path, matrix_run_path)
    asof = date.fromisoformat(ctx["as_of"])

    book_raw = json.loads(positions_path.read_text())
    account_value = book_raw.get("account_value")

    held_tickers = {p["ticker"] for p in ctx["positions"]}

    tickets: list[dict[str, Any]] = []

    # Position-level tickets
    for pos in ctx["positions"]:
        ticket_raw = recommend_for_position(pos, ctx["policy"], asof)
        action = ticket_raw["recommendation"]
        if action == "HOLD":
            continue  # don't crowd the queue with holds
        tk: dict[str, Any] = {
            "action": action,
            "ticker": ticket_raw["ticker"],
            "id": ticket_raw["id"],
            "qty": ticket_raw["qty_to_act"],
            "qty_to_keep": ticket_raw["qty_to_keep"],
            "rationale": ticket_raw["rationale"],
            "tags": ticket_raw["tags"],
            "underlying_now": ticket_raw["trigger_underlying_now"],
            "trigger_exit_below": ticket_raw["trigger_exit_below"],
            "trigger_take_profit_above": ticket_raw["trigger_take_profit_above"],
            "current_contract": ticket_raw.get("current_contract"),
            "spoken_summary": ticket_raw.get("spoken_summary"),
        }
        if action in {"ROLL", "ADD"} and ticket_raw.get("alternative_contract"):
            ac = ticket_raw["alternative_contract"]
            tk["contract"] = {
                "strike": ac.get("strike"),
                "expiry": ac.get("expiry"),
                "delta": ac.get("delta"),
                "iv": ac.get("iv"),
                "open_interest": ac.get("open_interest"),
                "premium_per_share": ac.get("premium_per_share"),
                "premium_per_contract_usd": ac.get("premium_per_contract_usd"),
                "breakeven_underlying": ac.get("breakeven_underlying"),
                "breakeven_pct_from_current": ac.get("breakeven_pct_from_current"),
            }
            if ac.get("premium_per_contract_usd"):
                tk["est_total_usd"] = round(
                    (tk["qty"] or 0) * ac["premium_per_contract_usd"], 2
                )
        elif action in {"EXIT", "TRIM"} and ticket_raw.get("current_contract"):
            cc = ticket_raw["current_contract"]
            tk["contract"] = {
                "strike": cc.get("strike"),
                "expiry": cc.get("expiry"),
                "premium_per_share": cc.get("current_mark_per_share")
                or cc.get("premium_paid_per_share"),
            }
            if cc.get("current_mark_per_share"):
                tk["est_total_usd"] = round(
                    (tk["qty"] or 0) * cc["current_mark_per_share"] * 100, 2
                )
        tickets.append(tk)

    # New-pick tickets
    if include_new_picks:
        new_picks = discover_new_picks(ctx, held_tickers, max_new=max_new)
        min_oi = ctx["policy"].get("options_defaults", {}).get("min_open_interest", 100)
        for pick in new_picks:
            sim = simulate_candidate(ctx, pick["ticker"], account_value)
            if "error" in sim:
                continue
            con = sim["contract"]
            qty = sim.get("suggested_qty_contracts")
            tier = pick.get("tier")
            agg_pt = pick.get("aggressive_pt")
            cons_pt = pick.get("conservative_pt")
            if cons_pt and agg_pt and abs(cons_pt - agg_pt) < 0.5:
                pt_phrase = f"both analysts agree on a ${agg_pt:.0f} target"
            elif cons_pt and agg_pt:
                pt_phrase = f"target ${agg_pt:.0f} (cautious view ${cons_pt:.0f})"
            elif agg_pt:
                pt_phrase = f"target ${agg_pt:.0f}"
            else:
                pt_phrase = "target not modeled"
            rationale_plain = [
                f"This week's top {tier}-tier pick — {pt_phrase}.",
                f"Sized as a starter (~{sim['starter_pct_target']*100:.0f}% of the book).",
            ]
            tags = ["new_pick", f"tier_{tier}"]
            oi = con.get("open_interest")
            if oi is not None and oi < min_oi:
                rationale_plain.append(
                    f"⚠️ Thin chain — only {oi} contracts of open interest "
                    f"(under our {min_oi} threshold). Consider a different strike or smaller size."
                )
                tags.append("low_liquidity")

            spoken = (
                f"Buy {qty} × {pick['ticker']} {con.get('strike')}C exp {con.get('expiry')} "
                f"@ ${con.get('premium_per_share')}/sh "
                f"(~${con.get('premium_total_usd'):,.0f} total). "
                f"Top {tier}-tier pick this week."
            )
            if oi is not None and oi < min_oi:
                spoken += " ⚠️ Thin liquidity — confirm fill before sizing up."

            tickets.append({
                "action": "OPEN_NEW",
                "ticker": pick["ticker"],
                "id": None,
                "qty": qty,
                "rationale": rationale_plain,
                "tags": tags,
                "underlying_now": pick.get("current_price_usd"),
                "trigger_exit_below": None,
                "trigger_take_profit_above": pick.get("aggressive_pt"),
                "contract": {
                    "strike": con.get("strike"),
                    "expiry": con.get("expiry"),
                    "delta": round(con["delta"], 3) if con.get("delta") is not None else None,
                    "iv": round(con["iv"], 3) if con.get("iv") is not None else None,
                    "open_interest": oi,
                    "premium_per_share": con.get("premium_per_share"),
                    "premium_per_contract_usd": con.get("premium_per_contract_usd"),
                    "breakeven_underlying": con.get("breakeven_underlying"),
                    "breakeven_pct_from_current": con.get("breakeven_pct_from_current"),
                },
                "est_total_usd": con.get("premium_total_usd"),
                "spoken_summary": spoken,
            })

    # Sort by priority bucket
    tickets.sort(key=lambda t: (_ticket_priority(t["action"]), t["ticker"]))

    breakdown_counts: dict[str, int] = {}
    for t in tickets:
        breakdown_counts[t["action"]] = breakdown_counts.get(t["action"], 0) + 1

    summary = {
        "as_of": ctx["as_of"],
        "matrix_run_id": ctx["matrix_run_id"],
        "n_total": len(tickets),
        "breakdown": ", ".join(f"{k}:{v}" for k, v in sorted(breakdown_counts.items())),
        "breakdown_counts": breakdown_counts,
    }

    return {"summary": summary, "tickets": tickets}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--matrix-run", type=Path, default=None)
    parser.add_argument("--include-new-picks", action="store_true",
                        help="Append fresh matrix Tier A/B picks not already held")
    parser.add_argument("--max-new", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    queue = build_queue(
        args.positions_file,
        args.policy_file,
        args.matrix_run,
        include_new_picks=args.include_new_picks,
        max_new=args.max_new,
    )

    if args.json:
        print(json.dumps(queue, indent=2, default=str))
    else:
        print(render_tickets_markdown(queue["tickets"], queue["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
