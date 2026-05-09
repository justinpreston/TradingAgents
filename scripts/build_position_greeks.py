#!/usr/bin/env python3
"""Build live portfolio greeks from Polygon options snapshots.

Reads positions.json, pulls live greeks/IV/OI for each options position
from Polygon's /v3/snapshot/options/ endpoint, and computes aggregate
portfolio-level delta, gamma, theta, vega exposure.

Usage::

    .venv/bin/python scripts/build_position_greeks.py
    .venv/bin/python scripts/build_position_greeks.py --json
    .venv/bin/python scripts/build_position_greeks.py --ticker NVDA
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.polygon_common import _make_request, PolygonError  # noqa: E402

DEFAULT_POSITIONS = REPO_ROOT / "runs" / "portfolio" / "positions.json"


def fetch_contract_snapshot(
    underlying: str,
    strike: float,
    expiry: str,
    contract_type: str = "call",
) -> dict[str, Any] | None:
    """Fetch a single contract snapshot from Polygon options chain."""
    try:
        data = _make_request(
            f"/v3/snapshot/options/{underlying}",
            {
                "strike_price": str(strike),
                "expiration_date": expiry,
                "contract_type": contract_type,
                "limit": "5",
            },
        )
    except PolygonError:
        return None
    results = data.get("results") or []
    if not results:
        return None
    for r in results:
        d = r.get("details", {})
        if d.get("strike_price") == strike and d.get("expiration_date") == expiry:
            return r
    return results[0]


def aggregate_greeks(positions: list[dict[str, Any]]) -> dict[str, float]:
    """Compute aggregate portfolio greeks weighted by qty * 100."""
    total_delta = 0.0
    total_gamma = 0.0
    total_theta = 0.0
    total_vega = 0.0
    for p in positions:
        qty = p.get("qty", 0)
        g = p.get("greeks") or {}
        multiplier = qty * 100
        total_delta += multiplier * (g.get("delta") or 0)
        total_gamma += multiplier * (g.get("gamma") or 0)
        total_theta += multiplier * (g.get("theta") or 0)
        total_vega += multiplier * (g.get("vega") or 0)
    return {
        "total_delta": round(total_delta, 2),
        "total_gamma": round(total_gamma, 4),
        "total_theta_per_day": round(total_theta, 2),
        "total_vega": round(total_vega, 2),
    }


def build_portfolio_greeks(
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build live greeks for all options positions. Skips equity."""
    option_positions = []
    for pos in positions:
        if pos.get("instrument") == "equity" or not pos.get("option"):
            continue
        opt = pos["option"]
        contract_type = "call" if "call" in pos.get("instrument", "") else "put"
        snap = fetch_contract_snapshot(
            pos["ticker"], opt["strike"], opt["expiry"], contract_type,
        )
        entry = {
            "id": pos.get("id"),
            "ticker": pos["ticker"],
            "instrument": pos["instrument"],
            "qty": pos.get("qty", 0),
            "strike": opt["strike"],
            "expiry": opt["expiry"],
        }
        if snap:
            g = snap.get("greeks") or {}
            entry["live_greeks"] = g
            entry["greeks"] = g
            entry["implied_volatility"] = snap.get("implied_volatility")
            entry["open_interest"] = snap.get("open_interest")
            entry["underlying_price"] = (snap.get("underlying_asset") or {}).get("price")
            entry["mark"] = (snap.get("day") or {}).get("close")
            entry["break_even"] = snap.get("break_even_price")
        else:
            entry["live_greeks"] = None
            entry["greeks"] = {}
            entry["implied_volatility"] = None
            entry["open_interest"] = None
            entry["underlying_price"] = None
            entry["mark"] = None
            entry["break_even"] = None
        option_positions.append(entry)

    agg = aggregate_greeks(option_positions)
    return {"as_of": date.today().isoformat(), "positions": option_positions, "aggregate": agg}


def render_markdown(result: dict) -> str:
    lines = [f"# Portfolio greeks — {result['as_of']}", ""]
    agg = result["aggregate"]
    lines.append("## Aggregate exposure")
    lines.append(f"- **Total delta:** {agg['total_delta']:,.0f} shares equivalent")
    lines.append(f"- **Total gamma:** {agg['total_gamma']:,.4f}")
    lines.append(f"- **Total theta:** ${agg['total_theta_per_day']:,.2f}/day")
    lines.append(f"- **Total vega:** {agg['total_vega']:,.2f}")
    lines.append("")
    lines.append("## Per-position")
    lines.append("| Ticker | Strike | Expiry | Qty | Delta | Gamma | Theta | Vega | IV | OI | Underlying | Mark |")
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in result["positions"]:
        g = p.get("live_greeks") or {}
        lines.append(
            f"| {p['ticker']} | {p['strike']} | {p['expiry']} | {p['qty']} "
            f"| {_fmtf(g.get('delta'))} | {_fmtf(g.get('gamma'), 4)} "
            f"| {_fmtf(g.get('theta'))} | {_fmtf(g.get('vega'))} "
            f"| {_fmtpct(p.get('implied_volatility'))} | {_fmtint(p.get('open_interest'))} "
            f"| {_fmtprice(p.get('underlying_price'))} | {_fmtprice(p.get('mark'))} |"
        )
    return "\n".join(lines)


def _fmtf(v, prec: int = 3) -> str:
    return f"{v:.{prec}f}" if v is not None else "—"

def _fmtpct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "—"

def _fmtint(v) -> str:
    return f"{int(v):,}" if v is not None else "—"

def _fmtprice(v) -> str:
    return f"${v:.2f}" if v is not None else "—"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    p.add_argument("--ticker", type=str, default=None,
                   help="Limit to one ticker (case-insensitive)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    pos_data = json.loads(args.positions_file.read_text())
    positions = pos_data.get("positions", [])

    if args.ticker:
        positions = [p for p in positions if p["ticker"].upper() == args.ticker.upper()]

    result = build_portfolio_greeks(positions)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
