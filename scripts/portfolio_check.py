"""Per-ticker drill-down emitting structured trade tickets.

For each held ticker (or one specified via --ticker), produces:
  - A `recommendation` enum: HOLD / EXIT / TRIM / ROLL / ADD / REVIEW
  - Specific contract details if the recommendation involves an options trade
  - Underlying-level entry/exit triggers
  - Rationale citing tier delta, P&L, DTE, and policy thresholds

Output is JSON (--json) or a markdown table of trade tickets (default).

Usage::

    .venv/bin/python scripts/portfolio_check.py
    .venv/bin/python scripts/portfolio_check.py --ticker VIRT
    .venv/bin/python scripts/portfolio_check.py --json
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


# ---------------------------------------------------------------------------
# Recommendation engine

VALID_RECOMMENDATIONS = {"HOLD", "EXIT", "TRIM", "ROLL", "ADD", "REVIEW"}


def _qty_for_action(action: str, qty: int, policy: dict[str, Any]) -> int:
    """Return suggested qty to act on, given a position size and action."""
    if action == "EXIT":
        return qty
    if action == "TRIM":
        # default: trim a third (rounded up so always >=1)
        return max(1, (qty + 2) // 3)
    return qty


def recommend_for_position(pos: dict[str, Any], policy: dict[str, Any], asof: date) -> dict[str, Any]:
    """Return a structured trade ticket for the position.

    The ticket has shape::

        {
          "id": "VIRT-...",
          "ticker": "VIRT",
          "recommendation": "TRIM",
          "qty_to_act": 2,
          "qty_to_keep": 3,
          "rationale": ["..."],
          "trigger_underlying_now": 51.31,
          "trigger_exit_below": 28.50,
          "trigger_take_profit_above": 38.00,
          "current_contract": {...},
          "alternative_contract": {...} | None,  # for ROLL
          "tags": ["take_profit_hit", "tier_stable"]
        }
    """
    live = pos.get("live", {})
    instrument = pos.get("instrument", "equity")
    qty = pos.get("qty", 0)
    ticker = pos["ticker"]

    rationale: list[str] = []
    tags: list[str] = []
    recommendation = "HOLD"

    classification = live.get("matrix_classification")
    tier_delta = live.get("tier_delta", "n/a")
    tier_now = live.get("matrix_tier_now")
    tier_entry = live.get("matrix_tier_at_entry")

    exit_defaults = policy.get("exit_defaults", {}) if isinstance(policy, dict) else {}
    veto_action = (exit_defaults.get("tier_demoted_to_veto_action") or "exit").upper()
    demote_action = (exit_defaults.get("tier_demoted_one_step_action") or "trim_third").upper()
    demote_action = "TRIM" if demote_action.startswith("TRIM") else demote_action

    # --- highest-severity rules first ---

    if live.get("underlying_below_stop_loss"):
        recommendation = "EXIT"
        rationale.append(
            f"Underlying ${live['current_underlying_price']:.2f} breached stop-loss "
            f"${pos.get('stop_loss_underlying'):.2f}."
        )
        tags.append("stop_loss_breach")

    elif classification == "VETOED":
        recommendation = veto_action if veto_action in VALID_RECOMMENDATIONS else "EXIT"
        rationale.append(
            f"{ticker} VETOED in latest matrix run "
            f"(was tier {tier_entry or 'unrated'} at entry). "
            "Cons-frame skepticism overrides aggressive thesis."
        )
        tags.append("matrix_veto")

    elif tier_delta == "demoted":
        # tier B → C, A → B, etc. → policy default trim_third
        recommendation = demote_action if demote_action in VALID_RECOMMENDATIONS else "TRIM"
        rationale.append(
            f"Tier demoted {tier_entry} → {tier_now} in latest matrix run."
        )
        tags.append("tier_demoted")

    elif live.get("roll_trigger_armed") and instrument != "equity":
        recommendation = "ROLL"
        rationale.append(
            f"Days-to-expiry {live.get('dte')} ≤ roll threshold "
            f"{pos.get('roll_trigger_dte') or policy.get('roll_defaults', {}).get('dte_threshold', 60)}. "
            "Evaluate rolling out in time."
        )
        tags.append("roll_trigger_dte")

    elif live.get("underlying_above_take_profit"):
        recommendation = "TRIM"
        rationale.append(
            f"Underlying ${live['current_underlying_price']:.2f} above take_profit "
            f"${pos.get('take_profit_underlying'):.2f}. Lock in part of the gain."
        )
        tags.append("take_profit_hit")

    elif tier_delta == "promoted":
        recommendation = "ADD"
        rationale.append(
            f"Tier promoted {tier_entry} → {tier_now} in latest matrix run. "
            "Consider scaling toward tier_sizing.max_pct."
        )
        tags.append("tier_promoted")

    else:
        recommendation = "HOLD"
        if tier_delta == "stable":
            rationale.append(f"Tier stable at {tier_now or 'unrated'}; thesis intact.")
            tags.append("tier_stable")
        elif tier_delta == "stale":
            rationale.append(
                f"{ticker} not in latest matrix run; matrix-derived signals are stale. "
                "Re-screen to refresh tier."
            )
            tags.append("tier_stale")
        elif tier_delta == "n/a":
            rationale.append("Non-matrix-driven hold; matrix tier delta does not apply.")
            tags.append("not_matrix_driven")

    # --- contract specifics (open + alternative for roll) ---

    current_contract = None
    if pos.get("option"):
        opt = pos["option"]
        current_contract = {
            "strike": opt.get("strike"),
            "expiry": opt.get("expiry"),
            "delta_at_entry": opt.get("delta_at_entry"),
            "premium_paid_per_share": opt.get("premium_paid_per_share"),
            "current_mark_per_share": opt.get("current_mark_per_share"),
        }

    alternative_contract = None
    if recommendation in {"ROLL", "ADD"} and live.get("matrix_long_call_recommendation"):
        rec = live["matrix_long_call_recommendation"]
        if rec.get("legs"):
            leg = rec["legs"][0]
            alternative_contract = {
                "strike": leg.get("strike"),
                "expiry": leg.get("expiration"),
                "delta": leg.get("delta"),
                "iv": leg.get("iv"),
                "open_interest": leg.get("open_interest"),
                "premium_per_share": rec.get("net_debit_per_share"),
                "premium_per_contract_usd": rec.get("net_debit_per_contract"),
                "breakeven_underlying": rec.get("breakeven_underlying"),
                "breakeven_pct_from_current": rec.get("breakeven_pct_from_current"),
                "matrix_dte": rec.get("dte"),
                "source": "matrix_options_overlay",
            }

    qty_to_act = _qty_for_action(recommendation, qty, policy)
    qty_to_keep = max(0, qty - qty_to_act) if recommendation in {"EXIT", "TRIM"} else qty

    # Surface the most-recent matching log entry as a follow-up nudge.
    recent_actions = live.get("recent_actions") or []
    if recent_actions:
        last = recent_actions[0]
        rationale.append(
            f"Last logged action on {ticker}: "
            f"{last.get('action')} {last.get('qty', '?')} on {last.get('ts', '?')[:10]}"
            + (f" — \"{last['notes']}\"" if last.get("notes") else "")
        )
        tags.append("has_recent_log")

    ticket = {
        "id": pos.get("id"),
        "ticker": ticker,
        "instrument": instrument,
        "recommendation": recommendation,
        "qty_total": qty,
        "qty_to_act": qty_to_act,
        "qty_to_keep": qty_to_keep,
        "rationale": rationale,
        "tags": tags,
        "trigger_underlying_now": live.get("current_underlying_price"),
        "trigger_exit_below": pos.get("stop_loss_underlying"),
        "trigger_take_profit_above": pos.get("take_profit_underlying"),
        "trigger_roll_dte": pos.get("roll_trigger_dte")
        or policy.get("roll_defaults", {}).get("dte_threshold", 60),
        "matrix_tier_at_entry": tier_entry,
        "matrix_tier_now": tier_now,
        "tier_delta": tier_delta,
        "matrix_classification": classification,
        "matrix_aggressive_pt": live.get("matrix_aggressive_pt"),
        "matrix_conservative_pt": live.get("matrix_conservative_pt"),
        "current_underlying_pnl_pct": live.get("underlying_pnl_pct"),
        "current_option_pnl_pct": live.get("option_pnl_pct"),
        "dte": live.get("dte"),
        "current_contract": current_contract,
        "alternative_contract": alternative_contract,
        "recent_log": recent_actions[:1],
    }
    ticket["spoken_summary"] = spoken_summary_for_ticket(ticket)
    return ticket


def spoken_summary_for_ticket(t: dict[str, Any]) -> str:
    """One plain-English sentence saying what to do and why.

    Designed to be the lead line of any rendering. If you only read this,
    you should know what action to take and the headline reason.
    """
    action = t["recommendation"]
    ticker = t["ticker"]
    qty_act = t.get("qty_to_act") or 0
    qty_keep = t.get("qty_to_keep") or 0
    qty_total = t.get("qty_total") or 0
    instrument = t.get("instrument", "equity")
    is_option = instrument != "equity"

    underlying = t.get("trigger_underlying_now")
    underlying_str = f"${underlying:.2f}" if underlying is not None else "current price unknown"

    cc = t.get("current_contract") or {}
    contract_short = ""
    if cc.get("strike") and cc.get("expiry"):
        contract_short = f"{cc.get('strike')}C exp {cc.get('expiry')}"

    if action == "EXIT":
        reason = "stop-loss breached" if "stop_loss_breach" in t.get("tags", []) else (
            "matrix VETOED this name" if "matrix_veto" in t.get("tags", []) else "thesis broken"
        )
        if is_option and contract_short:
            return (
                f"Close all {qty_total} {ticker} {contract_short} contracts — "
                f"{reason} ({underlying_str})."
            )
        return f"Close your full {ticker} position ({qty_total}) — {reason} ({underlying_str})."

    if action == "TRIM":
        if "take_profit_hit" in t.get("tags", []):
            tp = t.get("trigger_take_profit_above")
            why = f"{ticker} is trading {underlying_str}, past your ${tp:.2f} take-profit" if tp else "take-profit hit"
        elif "tier_demoted" in t.get("tags", []):
            why = f"tier dropped from {t.get('matrix_tier_at_entry') or '—'} to {t.get('matrix_tier_now') or '—'}"
        else:
            why = "policy trigger"
        if is_option:
            return f"Sell {qty_act} of your {qty_total} {ticker} contracts (keep {qty_keep}) — {why}."
        return f"Trim {qty_act} of {qty_total} {ticker} shares (keep {qty_keep}) — {why}."

    if action == "ROLL":
        ac = t.get("alternative_contract") or {}
        new_strike = ac.get("strike")
        new_expiry = ac.get("expiry")
        new_premium = ac.get("premium_per_share")
        dte = t.get("dte")
        if new_strike and new_expiry:
            return (
                f"Roll your {qty_total} {ticker} {contract_short} contracts → "
                f"{new_strike}C exp {new_expiry}"
                + (f" @ ${new_premium}/sh" if new_premium else "")
                + (f". Only {dte} days left on the current ones." if dte else ".")
            )
        return f"Roll your {ticker} contracts — only {dte} days left on the current strike."

    if action == "ADD":
        max_pct = None
        try:
            sizing = (t.get("policy") or {}).get("tier_sizing", {}).get(t.get("matrix_tier_now") or "B", {})
            max_pct = sizing.get("max_pct")
        except Exception:
            pass
        size_hint = f" toward {int(max_pct*100)}% of book" if max_pct else ""
        return (
            f"Add to {ticker} — promoted from "
            f"{t.get('matrix_tier_at_entry') or '—'} to {t.get('matrix_tier_now') or '—'}"
            f" in this week's matrix{size_hint}."
        )

    if action == "HOLD":
        if "tier_stable" in t.get("tags", []):
            tier = t.get("matrix_tier_now") or "—"
            return f"Hold {ticker} — tier {tier} steady, thesis intact."
        if "tier_stale" in t.get("tags", []):
            return f"Hold {ticker} for now — but it dropped out of this week's matrix, so re-screen if it comes back."
        return f"Hold {ticker} — no policy trigger fired."

    return f"Review {ticker} — manual look needed."


def render_tickets_markdown(tickets: list[dict[str, Any]]) -> str:
    if not tickets:
        return "_No positions to evaluate._"
    lines = ["# Trade tickets", ""]
    lines.append("| Ticker | ID | Action | Qty (act / keep / total) | DTE | Tier Δ | Underlying now / stop / TP | Option P&L% |")
    lines.append("|---|---|---|---|---:|---|---|---:|")
    for t in tickets:
        action = t["recommendation"]
        action_md = {
            "EXIT": "🛑 **EXIT**",
            "TRIM": "✂️ TRIM",
            "ROLL": "🔁 ROLL",
            "ADD": "➕ ADD",
            "HOLD": "⏸ HOLD",
            "REVIEW": "🔍 REVIEW",
        }.get(action, action)
        tier_delta_md = f"{t['matrix_tier_at_entry'] or '—'} → {t['matrix_tier_now'] or '—'}"
        triggers = (
            f"${t['trigger_underlying_now']:.2f}"
            if t.get("trigger_underlying_now") is not None
            else "—"
        )
        if t.get("trigger_exit_below") is not None:
            triggers += f" / SL ${t['trigger_exit_below']:.2f}"
        else:
            triggers += " / —"
        if t.get("trigger_take_profit_above") is not None:
            triggers += f" / TP ${t['trigger_take_profit_above']:.2f}"
        else:
            triggers += " / —"
        opnl = t.get("current_option_pnl_pct")
        opnl_md = f"{opnl:+.1f}%" if opnl is not None else "—"
        lines.append(
            f"| {t['ticker']} | `{t['id'] or ''}` | {action_md} | "
            f"{t['qty_to_act']} / {t['qty_to_keep']} / {t['qty_total']} | "
            f"{t.get('dte') if t.get('dte') is not None else '—'} | {tier_delta_md} | "
            f"{triggers} | {opnl_md} |"
        )
    lines.append("")
    lines.append("## Per-position rationale")
    lines.append("")
    for t in tickets:
        lines.append(f"### {t['ticker']} — `{t['id'] or ''}` — {t['recommendation']}")
        if t.get("spoken_summary"):
            lines.append("")
            lines.append(f"> **{t['spoken_summary']}**")
            lines.append("")
        for r in t["rationale"]:
            lines.append(f"- {r}")
        if t.get("current_contract") and any(t["current_contract"].values()):
            cc = t["current_contract"]
            lines.append(
                f"- **Current contract:** {cc.get('strike')}C exp {cc.get('expiry')}, "
                f"paid ${cc.get('premium_paid_per_share')}/sh → mark ${cc.get('current_mark_per_share') or '—'}/sh"
            )
        if t.get("alternative_contract"):
            ac = t["alternative_contract"]
            premium_total_str = (
                f"${ac.get('premium_per_contract_usd')}/contract"
                if ac.get("premium_per_contract_usd")
                else "—"
            )
            lines.append(
                f"- **Alternative contract (matrix-recommended):** "
                f"{ac.get('strike')}C exp {ac.get('expiry')} "
                f"(Δ {ac.get('delta'):.2f}, IV {ac.get('iv'):.2f}, OI {ac.get('open_interest')}), "
                f"premium ${ac.get('premium_per_share')}/sh = {premium_total_str}, "
                f"breakeven ${ac.get('breakeven_underlying')} ({ac.get('breakeven_pct_from_current')}% from current)"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--matrix-run", type=Path, default=None)
    parser.add_argument("--ticker", type=str, default=None,
                        help="Limit to one ticker (case-insensitive)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    ctx = build_context(args.positions_file, args.policy_file, args.matrix_run)
    asof = date.fromisoformat(ctx["as_of"])

    tickets = []
    for pos in ctx["positions"]:
        if args.ticker and pos["ticker"].upper() != args.ticker.upper():
            continue
        tickets.append(recommend_for_position(pos, ctx["policy"], asof))

    if args.json:
        print(json.dumps({
            "as_of": ctx["as_of"],
            "matrix_run_id": ctx["matrix_run_id"],
            "tickets": tickets,
        }, indent=2, default=str))
    else:
        print(render_tickets_markdown(tickets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
