#!/usr/bin/env python3
"""
Build an options-overlay package for a matrix run.

For each PICK in verdict_ledger.json, pulls the Polygon options chain,
selects strikes and expirations matching the agent-recommended horizon
and tier (A/B/C), and emits concrete bull-call-spread / long-call /
cash-secured-put suggestions with estimated net debit, max profit, and
breakeven.

Outputs (in the matrix-run dir):
  - options_overlay.json   (programmatic)
  - options_overlay.md     (human-readable)

Usage:
    python scripts/build_options_overlay.py \\
        --matrix-run runs/matrix_2026-05-01_top25

Strategy mapping by tier:
  - Tier A (cons PT set, comp < 5%, incl negative): bull call spread,
    long ATM, short at upper PT (cons PT if negative compression, else aggr PT).
  - Tier B (cons PT set, comp >= 5%): bull call spread, long ATM,
    short at cons PT (the conservative-blessed take-profit zone).
  - Tier C (cons did not set PT): long call at delta ~0.50 (ATM/slightly ITM).

Tenor mapping from aggressive horizon:
  - "3-6 months"   → next monthly expiration ≥ 90 days (target ~120d)
  - "6-12 months"  → expiration ≥ 180 days (target ~210d)
  - "6-18 months"  → expiration ≥ 270 days (target ~365d, LEAP if available)
  - default        → ≥ 90 days

Pricing: uses Black-Scholes with Polygon's reported implied volatility
per contract. Risk-free rate defaults to 4.5% (configurable via --risk-free).
American-vs-European distinction is ignored (BS is fine for estimating
non-dividend-paying near-the-money calls; tweak with --risk-free if
needed).
"""

from __future__ import annotations
import argparse
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.polygon_common import _make_request  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Tier classification (mirrors build_run_accounting / build_html_report)
# ──────────────────────────────────────────────────────────────────────

def _tier(row: dict) -> str:
    if row.get("classification") != "PICK":
        return "—"
    comp = row.get("pt_compression_pct")
    if row.get("conservative_pt") is None:
        return "C"
    if comp is not None and comp < 5.0:
        return "A"
    return "B"


# ──────────────────────────────────────────────────────────────────────
# Horizon → target days-to-expiration
# ──────────────────────────────────────────────────────────────────────

def _target_dte(horizon: str | None) -> tuple[int, int]:
    """Return (min_dte_floor, target_dte) for a given horizon string.

    `min_dte_floor` is the lower bound we'd ideally want; the actual fetch
    window is widened in `_build_per_ticker_overlay` to handle names without
    LEAPs. `target_dte` is the preferred expiration distance for ranking.
    """
    if not horizon:
        return (90, 120)
    h = horizon.lower()
    if "6-18" in h or "6 to 18" in h:
        return (180, 365)
    if "6-12" in h or "6 to 12" in h:
        return (180, 270)
    if "3-9" in h or "3 to 9" in h:
        return (90, 180)
    if "3-6" in h or "3 to 6" in h:
        return (90, 135)
    if "1-3" in h:
        return (30, 75)
    return (90, 120)


# ──────────────────────────────────────────────────────────────────────
# Black-Scholes for European call estimation
# ──────────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call price. Returns 0 for invalid inputs."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K)
    try:
        d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, K - S)
    try:
        d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return 0.0


# ──────────────────────────────────────────────────────────────────────
# Polygon chain fetch
# ──────────────────────────────────────────────────────────────────────

def _fetch_chain(ticker: str, contract_type: str,
                 strike_min: float, strike_max: float,
                 exp_min: str, exp_max: str,
                 limit: int = 250, max_retries: int = 3) -> tuple[list[dict], str | None]:
    """Returns (contracts, error_msg). Empty list with error_msg means real failure;
    empty list with None means simply no contracts in window."""
    params = {
        "contract_type": contract_type,
        "strike_price.gte": strike_min,
        "strike_price.lte": strike_max,
        "expiration_date.gte": exp_min,
        "expiration_date.lte": exp_max,
        "limit": limit,
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            r = _make_request(f"/v3/snapshot/options/{ticker}", params)
            return (r.get("results", []) or []), None
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            msg = last_err.lower()
            if ("rate" in msg or "429" in msg) and attempt < max_retries - 1:
                time.sleep(70)
                continue
            return [], last_err
    return [], last_err


# ──────────────────────────────────────────────────────────────────────
# Strike selection helpers
# ──────────────────────────────────────────────────────────────────────

def _round_to_listed_strike(target: float, available: list[float]) -> float:
    if not available:
        return target
    return min(available, key=lambda s: abs(s - target))


def _pick_expiration(contracts: list[dict], target_dte: int, today: date) -> str | None:
    """Pick the listed expiration closest to target_dte that's still >= min_dte."""
    exps: dict[str, int] = {}
    for c in contracts:
        exp = c["details"]["expiration_date"]
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte >= 30:
            exps[exp] = dte
    if not exps:
        return None
    return min(exps.keys(), key=lambda e: abs(exps[e] - target_dte))


def _contracts_at_exp(contracts: list[dict], exp: str) -> list[dict]:
    return [c for c in contracts if c["details"]["expiration_date"] == exp]


def _contract_summary(c: dict, S: float, r: float, today: date) -> dict:
    d = c["details"]
    K = d["strike_price"]
    exp = d["expiration_date"]
    dte = max(1, (datetime.strptime(exp, "%Y-%m-%d").date() - today).days)
    T = dte / 365.0
    iv = c.get("implied_volatility") or 0.0
    last_trade = (c.get("last_trade") or {}).get("price")
    quote_bid = (c.get("last_quote") or {}).get("bid")
    quote_ask = (c.get("last_quote") or {}).get("ask")

    if quote_bid is not None and quote_ask is not None and quote_bid > 0 and quote_ask > 0:
        mid = (quote_bid + quote_ask) / 2.0
        price_source = "mid"
    elif last_trade and last_trade > 0:
        mid = last_trade
        price_source = "last"
    else:
        mid = _bs_call(S, K, T, r, iv) if d.get("contract_type") == "call" else _bs_put(S, K, T, r, iv)
        price_source = "bs"

    return {
        "symbol": d["ticker"],
        "type": d.get("contract_type"),
        "strike": K,
        "expiration": exp,
        "dte": dte,
        "iv": iv,
        "open_interest": c.get("open_interest") or 0,
        "delta": (c.get("greeks") or {}).get("delta"),
        "theta": (c.get("greeks") or {}).get("theta"),
        "vega": (c.get("greeks") or {}).get("vega"),
        "price": round(mid, 2),
        "price_source": price_source,
        "bid": quote_bid,
        "ask": quote_ask,
        "last_trade": last_trade,
    }


# ──────────────────────────────────────────────────────────────────────
# Strategy builders per tier
# ──────────────────────────────────────────────────────────────────────

def _pick_strike_by_delta(contracts_at_exp: list[dict], spot: float,
                          target_delta: float, min_oi: int) -> dict | None:
    """Pick the call contract whose delta is closest to target_delta.
    Falls back to ATM-by-strike if no contract reports a delta. Prefers
    contracts with OI ≥ min_oi when available."""
    candidates = [c for c in contracts_at_exp if c["details"].get("contract_type") == "call"]
    if not candidates:
        return None

    def _d(c: dict):
        return (c.get("greeks") or {}).get("delta")

    has_delta = [c for c in candidates if _d(c) is not None]
    if has_delta:
        liquid = [c for c in has_delta if (c.get("open_interest") or 0) >= min_oi]
        pool = liquid or has_delta
        return min(pool, key=lambda c: abs(_d(c) - target_delta))

    liquid = [c for c in candidates if (c.get("open_interest") or 0) >= min_oi]
    pool = liquid or candidates
    return min(pool, key=lambda c: abs(c["details"]["strike_price"] - spot))


def _build_strategy(row: dict, contracts: list[dict], today: date,
                    risk_free: float, min_oi: int,
                    strategy_mode: str = "tier-driven",
                    long_call_delta: float = 0.55) -> dict | None:
    tier = _tier(row)
    S = row.get("current_price_usd")
    aggr_pt = row.get("aggressive_pt")
    cons_pt = row.get("conservative_pt")
    horizon = row.get("aggressive_horizon")
    if not S or not aggr_pt:
        return None

    min_dte, target_dte = _target_dte(horizon)

    # Pick the expiration where the long leg can clear our OI threshold.
    # If no expiration has a sufficiently liquid ATM-ish long leg, fall back
    # to the closest-to-target expiration with any contracts.
    candidate_exps: dict[str, int] = {}
    for c in contracts:
        exp = c["details"]["expiration_date"]
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte >= 30:
            candidate_exps[exp] = dte
    if not candidate_exps:
        return {"error": f"No expiration ≥ 30 DTE in chain (window targeted {min_dte}-{target_dte} DTE)"}

    def _has_liquid_long_leg(exp: str) -> bool:
        same = _contracts_at_exp(contracts, exp)
        liquid_strikes = [c["details"]["strike_price"] for c in same
                          if (c.get("open_interest") or 0) >= min_oi]
        if not liquid_strikes:
            return False
        # Long leg target is current price S; require a liquid strike within ±10% of S
        return any(abs(k - S) / S <= 0.10 for k in liquid_strikes)

    liquid_exps = {e: d for e, d in candidate_exps.items() if _has_liquid_long_leg(e)}
    if liquid_exps:
        exp = min(liquid_exps.keys(), key=lambda e: abs(liquid_exps[e] - target_dte))
        liquidity_note = None
    else:
        exp = min(candidate_exps.keys(), key=lambda e: abs(candidate_exps[e] - target_dte))
        liquidity_note = f"No expiration has a liquid long leg (OI ≥ {min_oi} within ±10% of spot); using closest-to-target exp anyway."

    same_exp_all = _contracts_at_exp(contracts, exp)
    if not same_exp_all:
        return {"error": f"No contracts at exp {exp}"}

    available_strikes = sorted({c["details"]["strike_price"] for c in same_exp_all})

    # ── Strategy selection
    if strategy_mode == "long-call":
        # Always long call. Strike is delta-targeted (slightly ITM by default
        # for less time decay and more stock-like price action).
        strategy = "long_call"
        upper_target = None
        long_leg_contract = _pick_strike_by_delta(
            same_exp_all, S, long_call_delta, min_oi
        )
        if not long_leg_contract:
            return {"error": f"No call contract for delta target {long_call_delta} at exp {exp}"}
    else:
        # Tier-driven (original behavior)
        if tier == "A":
            comp = row.get("pt_compression_pct")
            upper_target = max(aggr_pt, cons_pt) if (cons_pt and comp is not None and comp < 0) else aggr_pt
            lower_target = S
            strategy = "bull_call_spread"
        elif tier == "B":
            upper_target = cons_pt or aggr_pt
            lower_target = S
            strategy = "bull_call_spread"
        else:  # Tier C
            upper_target = None
            lower_target = S
            strategy = "long_call"

        lower_K = _round_to_listed_strike(lower_target, available_strikes)
        long_leg_contract = next((c for c in same_exp_all if c["details"]["strike_price"] == lower_K), None)
        if not long_leg_contract:
            return {"error": f"Could not find listed contract at strike {lower_K}"}

    legs = [{"side": "long", **_contract_summary(long_leg_contract, S, risk_free, today)}]

    if strategy == "bull_call_spread" and upper_target:
        lower_K = legs[0]["strike"]
        upper_K = _round_to_listed_strike(upper_target, available_strikes)
        if upper_K <= lower_K:
            higher_strikes = [s for s in available_strikes if s > lower_K]
            if higher_strikes:
                upper_K = higher_strikes[0]
            else:
                strategy = "long_call"  # No higher listed strike → fall back to long call

        if strategy == "bull_call_spread":
            short_leg_contract = next(
                (c for c in same_exp_all if c["details"]["strike_price"] == upper_K), None
            )
            if short_leg_contract:
                legs.append({"side": "short", **_contract_summary(short_leg_contract, S, risk_free, today)})
            else:
                strategy = "long_call"

    # Compute economics
    if strategy == "bull_call_spread" and len(legs) == 2:
        long_p = legs[0]["price"]
        short_p = legs[1]["price"]
        net_debit = max(0.01, long_p - short_p)  # Floor at $0.01 to avoid div-by-zero
        spread_width = legs[1]["strike"] - legs[0]["strike"]
        max_profit = max(0.0, spread_width - net_debit)
        max_loss = net_debit
        breakeven = legs[0]["strike"] + net_debit
        rr = (max_profit / net_debit) if net_debit > 0 else 0
        upside_to_short_strike = ((legs[1]["strike"] - S) / S * 100) if S else 0
    else:
        strategy = "long_call"
        long_p = legs[0]["price"]
        net_debit = long_p
        spread_width = None
        max_profit = None
        max_loss = net_debit
        breakeven = legs[0]["strike"] + net_debit
        rr = None
        upside_to_short_strike = None

    long_oi = legs[0]["open_interest"]
    short_oi = legs[1]["open_interest"] if len(legs) > 1 else None
    liquidity_warnings = []
    if long_oi < min_oi:
        liquidity_warnings.append(f"long leg OI {long_oi} < {min_oi}")
    if short_oi is not None and short_oi < min_oi:
        liquidity_warnings.append(f"short leg OI {short_oi} < {min_oi}")
    if liquidity_note:
        liquidity_warnings.append(liquidity_note)
    chosen_dte = legs[0]["dte"]
    if abs(chosen_dte - target_dte) > 90:
        liquidity_warnings.append(
            f"tenor mismatch: chose {chosen_dte} DTE vs target {target_dte} DTE for horizon '{horizon}' (longest listed expiration)"
        )

    # Long-call-specific extras
    long_call_extras = {}
    if strategy == "long_call":
        long_strike = legs[0]["strike"]
        long_delta = legs[0].get("delta")
        if long_strike < S * 0.98:
            moneyness = "ITM"
        elif long_strike > S * 1.02:
            moneyness = "OTM"
        else:
            moneyness = "ATM"
        gain_aggr = max(0.0, aggr_pt - long_strike) * 100 - net_debit * 100
        gain_cons = (max(0.0, cons_pt - long_strike) * 100 - net_debit * 100) if cons_pt else None
        long_call_extras = {
            "long_call_delta": long_delta,
            "long_call_moneyness": moneyness,
            "approx_gain_at_aggr_pt_per_contract": round(gain_aggr, 2),
            "approx_gain_at_cons_pt_per_contract": round(gain_cons, 2) if gain_cons is not None else None,
        }

    return {
        "tier": tier,
        "strategy": strategy,
        "strategy_mode": strategy_mode,
        "expiration": exp,
        "dte": legs[0]["dte"],
        "horizon": horizon,
        "legs": legs,
        "net_debit_per_share": round(net_debit, 2),
        "net_debit_per_contract": round(net_debit * 100, 2),
        "spread_width": spread_width,
        "max_profit_per_contract": round(max_profit * 100, 2) if max_profit is not None else None,
        "max_loss_per_contract": round(max_loss * 100, 2),
        "breakeven_underlying": round(breakeven, 2),
        "breakeven_pct_from_current": round((breakeven - S) / S * 100, 2),
        "risk_reward": round(rr, 2) if rr else None,
        "upside_to_short_strike_pct": round(upside_to_short_strike, 2) if upside_to_short_strike is not None else None,
        "liquidity_warnings": liquidity_warnings or None,
        **long_call_extras,
    }


# ──────────────────────────────────────────────────────────────────────
# Per-ticker overlay
# ──────────────────────────────────────────────────────────────────────

def _build_per_ticker_overlay(row: dict, today: date, risk_free: float,
                              min_oi: int,
                              strategy_mode: str = "tier-driven",
                              long_call_delta: float = 0.55) -> dict:
    t = row["ticker"]
    S = row.get("current_price_usd")
    aggr_pt = row.get("aggressive_pt")
    cons_pt = row.get("conservative_pt")
    horizon = row.get("aggressive_horizon")
    if not S or not aggr_pt:
        return {"ticker": t, "skipped": "missing current price or aggressive PT"}

    # Strike search range: ~10% ITM to ~10% above max(aggr, cons)
    upper = max(aggr_pt, cons_pt or aggr_pt) * 1.10
    lower = S * 0.90
    strike_min = max(0.5, lower)
    strike_max = upper

    # Expiration window. Use a generous lower bound (90 days) so names without
    # LEAPs still get a recommendation at the longest-available expiration.
    # The strategy builder will pick the closest-to-target inside whatever's listed.
    min_dte, target_dte = _target_dte(horizon)
    exp_min_date = today + timedelta(days=90)
    exp_max_date = today + timedelta(days=target_dte + 180)

    # Tier C → long calls only; Tier A/B → bull call spreads (calls only)
    chain, fetch_err = _fetch_chain(
        t, "call",
        strike_min, strike_max,
        exp_min_date.isoformat(), exp_max_date.isoformat(),
    )

    if fetch_err:
        return {"ticker": t, "name": row.get("name"), "error": f"Polygon fetch failed: {fetch_err}"}
    if not chain:
        return {"ticker": t, "name": row.get("name"),
                "skipped": f"no listed call chain in window {exp_min_date.isoformat()}..{exp_max_date.isoformat()} K {strike_min:.2f}-{strike_max:.2f}"}

    strat = _build_strategy(row, chain, today, risk_free, min_oi,
                            strategy_mode=strategy_mode,
                            long_call_delta=long_call_delta)
    return {
        "ticker": t,
        "name": row.get("name"),
        "current_price_usd": S,
        "aggressive_pt": aggr_pt,
        "conservative_pt": cons_pt,
        "aggressive_rating": row.get("aggressive_rating"),
        "conservative_rating": row.get("conservative_rating"),
        "horizon": horizon,
        "tier": _tier(row),
        "compression_pct": row.get("pt_compression_pct"),
        "n_contracts_in_window": len(chain),
        **(strat or {}),
    }


# ──────────────────────────────────────────────────────────────────────
# Markdown rendering
# ──────────────────────────────────────────────────────────────────────

def _fmt_money(v) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _strategy_label(s: str | None) -> str:
    return {
        "bull_call_spread": "Bull call spread",
        "long_call": "Long call",
    }.get(s or "", s or "—")


def _render_md(run_id: str, overlays: list[dict], today: date,
               risk_free: float, min_oi: int,
               strategy_mode: str = "tier-driven",
               long_call_delta: float = 0.55) -> str:
    valid = [o for o in overlays if "strategy" in o and "error" not in o]
    skipped = [o for o in overlays if "skipped" in o or "error" in o]

    mode_label = (
        f"Long-call (target Δ {long_call_delta})"
        if strategy_mode == "long-call" else "Tier-driven (A/B → spreads, C → long calls)"
    )

    lines = [
        f"# Options overlay · {run_id}",
        "",
        f"**Generated**: {today.isoformat()} · **Risk-free rate**: {risk_free:.1%} · **Min OI filter**: {min_oi} · **Mode**: {mode_label}",
        "",
        f"**Picks analyzed**: {len(overlays)} · **Strategies built**: {len(valid)} · **Skipped**: {len(skipped)}",
        "",
        "## Strategy mapping",
        "",
    ]

    if strategy_mode == "long-call":
        lines += [
            f"All picks → **single long call** at delta ≈ {long_call_delta} (slightly ITM by default for less time decay and more stock-like price action). Tier label is preserved for sizing guidance:",
            "",
            "| Tier | Sizing guidance |",
            "|---|---|",
            "| **A** (cons PT set, comp < 5%) | Full intended exposure. Both frames target the same zone. |",
            "| **B** (cons PT set, comp ≥ 5%) | 75% of intended exposure. Cons skeptical → take profits at cons PT. |",
            "| **C** (cons declined to set PT) | Starter size only. Add only on confirmed entry triggers. |",
            "",
            f"Long-call mode chooses the listed call whose Polygon-reported delta is closest to {long_call_delta}, with a fallback to ATM-by-strike if no contract reports a delta. **Long calls have unbounded profit, simpler P&L, no leg-management — at the cost of higher premium and full directional time-decay exposure.**",
            "",
        ]
    else:
        lines += [
            "Per the dual-frame asymmetry convention (see `trade_synthesis.md`), each pick maps to an options structure based on its tier:",
            "",
            "| Tier | Strategy | Rationale |",
            "|---|---|---|",
            "| **A** (cons PT set, comp < 5%) | Bull call spread | Both frames model the same target — defined-risk capture of the agreed move. Negative-compression names use cons PT as the upper strike. |",
            "| **B** (cons PT set, comp ≥ 5%) | Bull call spread | Cons engaged but skeptical — use cons PT as upper strike (conservative-blessed take-profit). |",
            "| **C** (cons did not set PT) | Long call | Aggressive-only thesis — no credible upper bound from cons; pay for full upside, smaller premium spend. |",
            "",
        ]

    lines += [
        "Tenor selection follows the aggressive horizon: `3-6m → ~120 DTE`, `6-12m → ~270 DTE`, `6-18m → ~365 DTE`. Longest available expiration is used when LEAPs are not listed.",
        "",
        "Pricing uses Polygon's reported implied volatility per contract via Black-Scholes when bid/ask is unavailable. **Always validate strikes, IV, and net debit on a real broker chain before trading** — Polygon snapshot data may lag intraday quotes.",
        "",
        "## Picks · ranked by tier × upside",
        "",
    ]

    def sort_key(o: dict) -> tuple:
        tier_order = {"A": 0, "B": 1, "C": 2}.get(o.get("tier", "—"), 3)
        if strategy_mode == "long-call":
            gain = o.get("approx_gain_at_aggr_pt_per_contract") or 0
            return (tier_order, -gain)
        upside = o.get("upside_to_short_strike_pct") or 0
        return (tier_order, -upside)

    valid_sorted = sorted(valid, key=sort_key)

    if strategy_mode == "long-call":
        lines += [
            "| Tkr | Tier | Exp | DTE | Strike | Δ | M/ness | Net Debit | Breakeven | Gain @ Aggr PT | Gain @ Cons PT | Liq |",
            "|---|:--:|---|--:|--:|--:|:--:|--:|--:|--:|--:|:--:|",
        ]
    else:
        lines += [
            "| Tkr | Tier | Strategy | Exp | DTE | Long K | Short K | Net Debit | Max Profit | Breakeven | RR | Upside to short K | Liq |",
            "|---|:--:|---|---|--:|--:|--:|--:|--:|--:|--:|--:|:--:|",
        ]
    for o in valid_sorted:
        legs = o.get("legs") or []
        long_k = legs[0]["strike"] if legs else None
        short_k = legs[1]["strike"] if len(legs) > 1 else None
        liq = "⚠" if o.get("liquidity_warnings") else "✓"
        if strategy_mode == "long-call":
            d = o.get("long_call_delta")
            d_str = f"{d:.2f}" if d is not None else "—"
            m = o.get("long_call_moneyness", "—")
            gain_a = o.get("approx_gain_at_aggr_pt_per_contract")
            gain_c = o.get("approx_gain_at_cons_pt_per_contract")
            gain_a_str = f"${gain_a:,.0f}" if gain_a is not None else "—"
            gain_c_str = f"${gain_c:,.0f}" if gain_c is not None else "—"
            lines.append(
                f"| **{o['ticker']}** | {o['tier']} | {o['expiration']} | {o['dte']} | "
                f"{_fmt_money(long_k)} | {d_str} | {m} | "
                f"{_fmt_money(o['net_debit_per_share'])} | {_fmt_money(o['breakeven_underlying'])} | "
                f"{gain_a_str} | {gain_c_str} | {liq} |"
            )
        else:
            rr = o.get("risk_reward")
            rr_str = f"{rr:.2f}×" if rr else "—"
            lines.append(
                f"| **{o['ticker']}** | {o['tier']} | {_strategy_label(o['strategy'])} | "
                f"{o['expiration']} | {o['dte']} | {_fmt_money(long_k)} | {_fmt_money(short_k)} | "
                f"{_fmt_money(o['net_debit_per_share'])} | "
                f"{_fmt_money((o['max_profit_per_contract'] / 100) if o.get('max_profit_per_contract') else None)} | "
                f"{_fmt_money(o['breakeven_underlying'])} | {rr_str} | "
                f"{_fmt_pct(o.get('upside_to_short_strike_pct'))} | {liq} |"
            )

    lines += ["", "## Per-pick details", ""]

    for o in valid_sorted:
        legs = o.get("legs") or []
        lines += [
            f"### {o['ticker']} — {o['name']} · current **{_fmt_money(o['current_price_usd'])}**",
            "",
            f"- **Tier**: {o['tier']} · **Strategy**: {_strategy_label(o['strategy'])} · **Horizon**: {o.get('horizon') or '—'}",
            f"- **Aggressive PT**: {_fmt_money(o['aggressive_pt'])} ({_fmt_pct(((o['aggressive_pt'] - o['current_price_usd']) / o['current_price_usd'] * 100) if o.get('current_price_usd') else None)})",
            f"- **Conservative PT**: {_fmt_money(o.get('conservative_pt'))}"
            + (f" ({_fmt_pct(((o['conservative_pt'] - o['current_price_usd']) / o['current_price_usd'] * 100) if o.get('current_price_usd') and o.get('conservative_pt') else None)})" if o.get('conservative_pt') else ""),
            f"- **Compression**: {_fmt_pct(o.get('compression_pct'))}",
            "",
            "**Legs**:",
            "",
            "| Side | Strike | Expiration | DTE | IV | OI | Δ | Price | Source |",
            "|---|--:|---|--:|--:|--:|--:|--:|---|",
        ]
        for leg in legs:
            iv_str = f"{leg['iv']:.1%}" if leg.get('iv') else "—"
            delta = leg.get("delta")
            delta_str = f"{delta:.2f}" if delta is not None else "—"
            lines.append(
                f"| {leg['side'].upper()} {leg['type']} | {_fmt_money(leg['strike'])} | "
                f"{leg['expiration']} | {leg['dte']} | {iv_str} | {leg['open_interest']} | "
                f"{delta_str} | {_fmt_money(leg['price'])} | {leg['price_source']} |"
            )

        lines += [
            "",
            "**Economics** (per 1 contract = 100 shares):",
            "",
            f"- Net debit: **{_fmt_money(o['net_debit_per_share'])}/share** = ${o['net_debit_per_contract']:,.0f}/contract",
        ]
        if o["strategy"] == "bull_call_spread":
            lines += [
                f"- Max profit: ${o['max_profit_per_contract']:,.0f}/contract  "
                f"(spread width ${o.get('spread_width'):.2f})",
                f"- Max loss: ${o['max_loss_per_contract']:,.0f}/contract (premium paid)",
                f"- Risk/reward: {o['risk_reward']}× max profit per $1 risked" if o.get('risk_reward') else "- Risk/reward: —",
            ]
        else:
            lines += [
                f"- Max loss: ${o['max_loss_per_contract']:,.0f}/contract (premium paid)",
                "- Max profit: unbounded (long call)",
            ]
            d = o.get("long_call_delta")
            m = o.get("long_call_moneyness")
            if d is not None or m:
                d_str = f"Δ {d:.2f}" if d is not None else "Δ —"
                lines.append(f"- Strike profile: {d_str} · {m or '—'}")
            gain_a = o.get("approx_gain_at_aggr_pt_per_contract")
            gain_c = o.get("approx_gain_at_cons_pt_per_contract")
            if gain_a is not None:
                pct_a = (gain_a / (o['net_debit_per_contract'] or 1)) * 100 if o.get('net_debit_per_contract') else None
                pct_str = f" ({_fmt_pct(pct_a)} on premium)" if pct_a is not None else ""
                lines.append(f"- Approx P&L if aggressive PT hits at expiry: **${gain_a:,.0f}/contract**{pct_str}")
            if gain_c is not None:
                pct_c = (gain_c / (o['net_debit_per_contract'] or 1)) * 100 if o.get('net_debit_per_contract') else None
                pct_str = f" ({_fmt_pct(pct_c)} on premium)" if pct_c is not None else ""
                lines.append(f"- Approx P&L if conservative PT hits at expiry: ${gain_c:,.0f}/contract{pct_str}")
        lines += [
            f"- Breakeven at expiration: {_fmt_money(o['breakeven_underlying'])} "
            f"({_fmt_pct(o['breakeven_pct_from_current'])} from current)",
            "",
        ]
        warns = o.get("liquidity_warnings")
        if warns:
            lines += ["**⚠ Liquidity warnings**:", ""]
            for w in warns:
                lines.append(f"- {w}")
            lines.append("")

    if skipped:
        lines += ["## Skipped picks", ""]
        for o in skipped:
            reason = o.get("skipped") or o.get("error") or "unknown"
            lines.append(f"- **{o['ticker']}**: {reason}")
        lines.append("")

    if strategy_mode == "long-call":
        lines += [
            "## Caveats",
            "",
            "- **Pricing model**: Black-Scholes with Polygon's reported per-contract IV. Bid/ask is often unavailable in the snapshot endpoint outside US market hours; when missing, prices are BS-estimates marked `bs` in the source column. Validate on a live broker chain before trading.",
            "- **Long-call exposure**: full directional time-decay risk — the position bleeds theta if the underlying chops sideways. A move in the right direction by the right time is essential.",
            f"- **Strike target Δ {long_call_delta}**: chosen for less time decay than ATM and more leverage than deep ITM. Lower delta (e.g. 0.40) for cheaper OTM speculation; higher delta (e.g. 0.70) for more stock-like behavior.",
            "- **Approx-gain at PT**: assumes the underlying hits the agent's price target *at expiration*. Mid-life moves are worth more (extrinsic value still in the contract); leaving room for early exits is prudent.",
            "- **Min OI filter is loose**: defaults to OI ≥ 50, fine for typical mid- and large-cap chains but may surface illiquid strikes. Tighten to ≥ 100 or ≥ 250 for production sizing.",
            "",
        ]
    else:
        lines += [
            "## Caveats",
            "",
            "- **Pricing model**: Black-Scholes with Polygon's reported per-contract IV. Bid/ask is often unavailable in the snapshot endpoint outside US market hours; when missing, prices are BS-estimates marked `bs` in the source column. Validate on a live broker chain before trading.",
            "- **American-style assumption**: BS undervalues American calls slightly when there's an upcoming dividend; for non-dividend names this is negligible.",
            "- **Min OI filter is loose**: this script defaults to OI ≥ 50, which is fine for typical mid- and large-cap chains but may surface illiquid strikes. Tighten to ≥ 100 or ≥ 250 for production sizing.",
            "- **No multi-leg validation**: spreads are picked from listed contracts at the *same expiration*, but the script doesn't validate that both legs are simultaneously executable (broker may show wider markets on one leg).",
            "- **Tier C uses ATM long calls**: this is the highest-premium structure. Consider ITM (delta 0.65–0.75) for less time decay or OTM (delta 0.35) for cheaper directional speculation, depending on conviction.",
            "",
        ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--matrix-run", required=True,
                   help="Path to matrix run dir (must contain verdict_ledger.json)")
    p.add_argument("--risk-free", type=float, default=0.045,
                   help="Annualized risk-free rate (default 4.5%%)")
    p.add_argument("--min-oi", type=int, default=50,
                   help="Minimum open interest for strike eligibility (default 50)")
    p.add_argument("--strategy-mode", choices=["tier-driven", "long-call"],
                   default="tier-driven",
                   help="'tier-driven' (default): A/B → bull call spreads, C → long calls. "
                        "'long-call': always single long call regardless of tier.")
    p.add_argument("--long-call-delta", type=float, default=0.55,
                   help="Target delta for long-call mode strike selection (default 0.55, slightly ITM)")
    p.add_argument("--snapshot-date", default=None,
                   help="Override 'today' for DTE calc (YYYY-MM-DD); defaults to system date")
    args = p.parse_args()

    matrix_run = Path(args.matrix_run)
    ledger_path = matrix_run / "verdict_ledger.json"
    if not ledger_path.exists():
        print(f"❌ {ledger_path} not found", file=sys.stderr)
        sys.exit(2)

    ledger = json.loads(ledger_path.read_text())
    rows = ledger["rows"]
    picks = [r for r in rows if r["classification"] == "PICK"]
    today = date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()

    print(f"[options] matrix: {matrix_run.name} · picks: {len(picks)} · "
          f"today: {today.isoformat()} · rf: {args.risk_free:.1%} · min OI: {args.min_oi} · "
          f"mode: {args.strategy_mode}"
          + (f" (target Δ {args.long_call_delta})" if args.strategy_mode == "long-call" else ""))

    overlays = []
    for i, row in enumerate(picks, 1):
        t = row["ticker"]
        print(f"  [{i}/{len(picks)}] {t} ...", end=" ", flush=True)
        ovl = _build_per_ticker_overlay(
            row, today, args.risk_free, args.min_oi,
            strategy_mode=args.strategy_mode,
            long_call_delta=args.long_call_delta,
        )
        if "skipped" in ovl:
            print(f"skipped ({ovl['skipped']})")
        elif ovl.get("error"):
            print(f"error ({ovl['error']})")
        else:
            tier = ovl["tier"]
            strat = _strategy_label(ovl["strategy"])
            nd = ovl["net_debit_per_share"]
            be = ovl["breakeven_underlying"]
            warn = " ⚠liquidity" if ovl.get("liquidity_warnings") else ""
            extra = ""
            if ovl["strategy"] == "long_call":
                d = ovl.get("long_call_delta")
                m = ovl.get("long_call_moneyness", "")
                if d is not None:
                    extra = f" · Δ{d:.2f} {m}"
            print(f"tier {tier} · {strat} · debit ${nd:.2f}/sh · BE ${be:.2f}{extra}{warn}")
        overlays.append(ovl)
        time.sleep(1.5)

    # Persist
    out_json = matrix_run / "options_overlay.json"
    out_md = matrix_run / "options_overlay.md"

    out_json.write_text(json.dumps({
        "matrix_run": matrix_run.name,
        "snapshot_date": today.isoformat(),
        "risk_free_rate": args.risk_free,
        "min_open_interest": args.min_oi,
        "strategy_mode": args.strategy_mode,
        "long_call_delta_target": args.long_call_delta if args.strategy_mode == "long-call" else None,
        "picks_analyzed": len(overlays),
        "strategies_built": sum(1 for o in overlays if "strategy" in o and "error" not in o),
        "overlays": overlays,
    }, indent=2, default=str))

    out_md.write_text(_render_md(matrix_run.name, overlays, today,
                                 args.risk_free, args.min_oi,
                                 args.strategy_mode, args.long_call_delta))

    print()
    print(f"  ✅ {out_json}")
    print(f"  ✅ {out_md}")


if __name__ == "__main__":
    main()
