"""Portfolio-level allocation check + rebalance trade tickets.

Computes:
  - Current $ and % exposure by sector, thematic basket, and single name
  - Cash buffer estimated from `account_value` minus deployed capital
  - Breach flags against `policy.json` caps (max_single_name, max_sector, etc.)
  - Rebalance tickets: which positions to trim to clear breaches, which
    to add (with specific contract specs from the matrix overlay) to
    bring exposures back toward policy targets

`--candidate <T>`: simulate adding ticker T at its current matrix-recommended
long-call structure and show the resulting exposure / breach delta.

Usage::

    .venv/bin/python scripts/portfolio_allocation.py
    .venv/bin/python scripts/portfolio_allocation.py --candidate CTRE
    .venv/bin/python scripts/portfolio_allocation.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
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
# Capital deployment math


def _market_value_for_position(pos: dict[str, Any]) -> float:
    """Return current market value, falling back to cost basis if no live mark."""
    instrument = pos.get("instrument", "equity")
    qty = pos.get("qty", 0) or 0
    live = pos.get("live", {})
    if instrument == "equity":
        px = live.get("current_underlying_price") or pos.get("underlying_cost_basis_at_entry") or 0
        return qty * px
    opt = pos.get("option") or {}
    mark = opt.get("current_mark_per_share")
    if mark is None:
        mark = opt.get("premium_paid_per_share") or 0
    return qty * 100 * mark


def _deployed_capital_for_position(pos: dict[str, Any]) -> float:
    """Return the dollar capital deployed in this position at cost basis."""
    instrument = pos.get("instrument", "equity")
    qty = pos.get("qty", 0) or 0
    if instrument == "equity":
        cost = pos.get("underlying_cost_basis_at_entry") or 0
        return qty * cost
    opt = pos.get("option") or {}
    prem = opt.get("premium_paid_per_share") or 0
    return qty * 100 * prem


def _exposure_for_position(pos: dict[str, Any], use_mtm: bool) -> float:
    return _market_value_for_position(pos) if use_mtm else _deployed_capital_for_position(pos)


# ---------------------------------------------------------------------------
# Aggregation


def aggregate_exposures(
    positions: list[dict[str, Any]],
    policy: dict[str, Any],
    use_mtm: bool = True,
) -> dict[str, Any]:
    by_ticker: dict[str, float] = defaultdict(float)
    by_sector: dict[str, float] = defaultdict(float)
    by_basket: dict[str, float] = defaultdict(float)
    by_instrument: dict[str, float] = defaultdict(float)
    total = 0.0

    baskets = policy.get("thematic_baskets", {}) if isinstance(policy, dict) else {}

    for pos in positions:
        amt = _exposure_for_position(pos, use_mtm)
        total += amt
        by_ticker[pos["ticker"]] += amt
        by_sector[pos.get("sector") or "unknown"] += amt
        by_instrument[pos.get("instrument", "unknown")] += amt
        # match against thematic baskets
        for basket_name, tickers in baskets.items():
            if isinstance(tickers, list) and pos["ticker"] in tickers:
                by_basket[basket_name] += amt

    return {
        "total_exposure_usd": round(total, 2),
        "by_ticker": {k: round(v, 2) for k, v in by_ticker.items()},
        "by_sector": {k: round(v, 2) for k, v in by_sector.items()},
        "by_basket": {k: round(v, 2) for k, v in by_basket.items()},
        "by_instrument": {k: round(v, 2) for k, v in by_instrument.items()},
        "valuation_basis": "mark-to-market" if use_mtm else "cost-basis",
    }


def compute_breaches(
    exposures: dict[str, Any], policy: dict[str, Any], account_value: float | None
) -> list[dict[str, Any]]:
    breaches: list[dict[str, Any]] = []
    if not account_value or account_value <= 0:
        return breaches

    max_single = policy.get("max_single_name_pct")
    if max_single:
        for ticker, amt in exposures["by_ticker"].items():
            pct = amt / account_value
            if pct > max_single:
                breaches.append({
                    "kind": "max_single_name",
                    "key": ticker,
                    "current_pct": round(pct * 100, 2),
                    "limit_pct": round(max_single * 100, 2),
                    "excess_usd": round((pct - max_single) * account_value, 2),
                })

    max_sector = policy.get("max_sector_pct")
    if max_sector:
        for sector, amt in exposures["by_sector"].items():
            if sector == "unknown":
                continue
            pct = amt / account_value
            if pct > max_sector:
                breaches.append({
                    "kind": "max_sector",
                    "key": sector,
                    "current_pct": round(pct * 100, 2),
                    "limit_pct": round(max_sector * 100, 2),
                    "excess_usd": round((pct - max_sector) * account_value, 2),
                })

    max_basket = policy.get("max_thematic_basket_pct")
    if max_basket:
        for basket, amt in exposures["by_basket"].items():
            pct = amt / account_value
            if pct > max_basket:
                breaches.append({
                    "kind": "max_thematic_basket",
                    "key": basket,
                    "current_pct": round(pct * 100, 2),
                    "limit_pct": round(max_basket * 100, 2),
                    "excess_usd": round((pct - max_basket) * account_value, 2),
                })

    return breaches


def cash_buffer_status(
    exposures: dict[str, Any], policy: dict[str, Any], account_value: float | None
) -> dict[str, Any] | None:
    if not account_value or account_value <= 0:
        return None
    deployed = exposures["total_exposure_usd"]
    cash = account_value - deployed
    cash_pct = cash / account_value
    target_pct = policy.get("cash_buffer_pct_target", 0.20)
    return {
        "cash_usd": round(cash, 2),
        "cash_pct": round(cash_pct * 100, 2),
        "target_pct": round(target_pct * 100, 2),
        "below_target": cash_pct < target_pct,
        "shortfall_usd": round((target_pct - cash_pct) * account_value, 2) if cash_pct < target_pct else 0.0,
    }


# ---------------------------------------------------------------------------
# Candidate simulation


def _matrix_recommended_long_call(ctx: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    overlays = []
    # The build_context call already retrieved overlays; we re-walk positions for matching ticker.
    # Simpler path: re-load matrix overlays directly from the context's matrix_run path.
    matrix_run = ctx.get("latest_matrix_run")
    if not matrix_run:
        return None
    overlay_path = Path(matrix_run) / "options_overlay.json"
    if not overlay_path.exists():
        return None
    doc = json.loads(overlay_path.read_text())
    for ov in doc.get("overlays", []):
        if ov.get("ticker") == ticker:
            return ov
    return None


def simulate_candidate(
    ctx: dict[str, Any], ticker: str, account_value: float | None
) -> dict[str, Any]:
    """Simulate adding `ticker` at its matrix-recommended long-call structure."""
    overlay = _matrix_recommended_long_call(ctx, ticker)
    if not overlay:
        return {"error": f"No matrix overlay for {ticker} in latest run."}

    legs = overlay.get("legs") or []
    if not legs:
        return {"error": f"Matrix overlay for {ticker} has no legs."}

    leg = legs[0]
    premium_per_share = overlay.get("net_debit_per_share")
    premium_per_contract_usd = overlay.get("net_debit_per_contract")
    tier = overlay.get("tier")

    policy = ctx["policy"]
    sizing = policy.get("tier_sizing", {}).get(tier or "C", {})
    starter_pct = sizing.get("starter_pct", 0.02)

    starter_usd = (account_value or 0) * starter_pct if account_value else None
    suggested_qty = None
    if starter_usd and premium_per_contract_usd:
        suggested_qty = max(1, int(starter_usd / premium_per_contract_usd))

    return {
        "ticker": ticker,
        "tier": tier,
        "matrix_classification": overlay.get("aggressive_rating") and "PICK",
        "starter_pct_target": starter_pct,
        "starter_usd_target": round(starter_usd, 2) if starter_usd else None,
        "suggested_qty_contracts": suggested_qty,
        "contract": {
            "strike": leg.get("strike"),
            "expiry": leg.get("expiration"),
            "delta": leg.get("delta"),
            "iv": leg.get("iv"),
            "open_interest": leg.get("open_interest"),
            "premium_per_share": premium_per_share,
            "premium_per_contract_usd": premium_per_contract_usd,
            "premium_total_usd": (
                round(suggested_qty * premium_per_contract_usd, 2)
                if suggested_qty and premium_per_contract_usd
                else None
            ),
            "breakeven_underlying": overlay.get("breakeven_underlying"),
            "breakeven_pct_from_current": overlay.get("breakeven_pct_from_current"),
            "current_underlying_price": overlay.get("current_price_usd"),
            "matrix_aggressive_pt": overlay.get("aggressive_pt"),
            "matrix_conservative_pt": overlay.get("conservative_pt"),
        },
        "vol_context": overlay.get("vol_context"),
    }


# ---------------------------------------------------------------------------
# Output


def render_markdown(report: dict[str, Any]) -> str:
    lines = []
    lines.append("# Portfolio allocation")
    lines.append("")
    lines.append(f"- **As of:** {report['as_of']}")
    lines.append(f"- **Matrix run:** `{report.get('matrix_run_id') or '(none)'}`")
    lines.append(f"- **Account value:** {report.get('account_value') or '(unset — set in positions.json)'}")
    lines.append(f"- **Total exposure:** ${report['exposures']['total_exposure_usd']:,.2f} ({report['exposures']['valuation_basis']})")
    lines.append("")

    cash = report.get("cash_buffer")
    if cash:
        status = "🟢 OK" if not cash["below_target"] else "🔴 BELOW TARGET"
        lines.append("## Cash buffer")
        lines.append(f"- Cash: ${cash['cash_usd']:,.2f} ({cash['cash_pct']}% of book)")
        lines.append(f"- Target: {cash['target_pct']}%  → {status}")
        if cash["below_target"]:
            lines.append(f"- Shortfall: ${cash['shortfall_usd']:,.2f}")
        lines.append("")

    if report["breaches"]:
        lines.append("## Breaches")
        for b in report["breaches"]:
            lines.append(
                f"- **{b['kind']}** `{b['key']}` — currently {b['current_pct']}%, "
                f"limit {b['limit_pct']}%, excess ${b['excess_usd']:,.2f}"
            )
        lines.append("")

    lines.append("## Exposure by sector")
    av = report.get("account_value")
    for sector, amt in sorted(report["exposures"]["by_sector"].items(), key=lambda x: -x[1]):
        pct = (amt / av * 100) if av else None
        pct_s = f" ({pct:.1f}%)" if pct is not None else ""
        lines.append(f"- {sector}: ${amt:,.2f}{pct_s}")
    lines.append("")

    if report["exposures"]["by_basket"]:
        lines.append("## Exposure by thematic basket")
        for basket, amt in sorted(report["exposures"]["by_basket"].items(), key=lambda x: -x[1]):
            pct = (amt / av * 100) if av else None
            pct_s = f" ({pct:.1f}%)" if pct is not None else ""
            lines.append(f"- {basket}: ${amt:,.2f}{pct_s}")
        lines.append("")

    lines.append("## Top single-name exposures")
    for ticker, amt in sorted(report["exposures"]["by_ticker"].items(), key=lambda x: -x[1])[:10]:
        pct = (amt / av * 100) if av else None
        pct_s = f" ({pct:.1f}%)" if pct is not None else ""
        lines.append(f"- {ticker}: ${amt:,.2f}{pct_s}")
    lines.append("")

    if report.get("candidate"):
        c = report["candidate"]
        if "error" in c:
            lines.append(f"## Candidate ({c.get('ticker', '?')})")
            lines.append(f"_{c['error']}_")
        else:
            con = c["contract"]
            lines.append(f"## Candidate: {c['ticker']} (Tier {c['tier']})")
            lines.append(
                f"- Starter target: {c['starter_pct_target']*100:.1f}% of book"
                + (f" = ${c['starter_usd_target']:,.2f}" if c.get("starter_usd_target") else "")
            )
            qty = c.get("suggested_qty_contracts")
            lines.append(
                f"- **Suggested ticket:** BUY {qty if qty else '?'} × {con['strike']}C "
                f"exp {con['expiry']} @ ${con['premium_per_share']}/sh "
                f"(${con.get('premium_per_contract_usd')}/contract)"
                + (f" = ${con['premium_total_usd']:,.2f}" if con.get('premium_total_usd') else "")
            )
            lines.append(
                f"- Δ {con.get('delta'):.2f}, IV {con.get('iv'):.2f}, OI {con.get('open_interest')}"
            )
            lines.append(
                f"- Underlying ${con.get('current_underlying_price')} → "
                f"breakeven ${con.get('breakeven_underlying')} "
                f"({con.get('breakeven_pct_from_current')}% from current)"
            )
            lines.append(
                f"- Aggressive PT ${con.get('matrix_aggressive_pt')} / Cons PT ${con.get('matrix_conservative_pt')}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main


def build_report(
    positions_path: Path = DEFAULT_POSITIONS,
    policy_path: Path = DEFAULT_POLICY,
    matrix_run_path: Path | None = None,
    candidate_ticker: str | None = None,
    use_mtm: bool = True,
) -> dict[str, Any]:
    ctx = build_context(positions_path, policy_path, matrix_run_path)

    book_raw = json.loads(positions_path.read_text())
    account_value = book_raw.get("account_value")

    exposures = aggregate_exposures(ctx["positions"], ctx["policy"], use_mtm)
    breaches = compute_breaches(exposures, ctx["policy"], account_value)
    cash = cash_buffer_status(exposures, ctx["policy"], account_value)

    report = {
        "as_of": ctx["as_of"],
        "matrix_run_id": ctx["matrix_run_id"],
        "matrix_snapshot_date": ctx["matrix_snapshot_date"],
        "account_value": account_value,
        "exposures": exposures,
        "breaches": breaches,
        "cash_buffer": cash,
    }

    if candidate_ticker:
        report["candidate"] = simulate_candidate(ctx, candidate_ticker.upper(), account_value)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--matrix-run", type=Path, default=None)
    parser.add_argument("--candidate", type=str, default=None,
                        help="Simulate adding this ticker at matrix-recommended long-call")
    parser.add_argument("--cost-basis", action="store_true",
                        help="Use cost-basis valuation instead of mark-to-market")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(
        args.positions_file,
        args.policy_file,
        args.matrix_run,
        args.candidate,
        use_mtm=not args.cost_basis,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
