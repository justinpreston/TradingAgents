#!/usr/bin/env python3
"""Score the picks in an options_overlay.json against an IV-surface fit.

Implements the methodology popularised by the `medloh/stockpile` YouTube
project (https://github.com/medloh/stockpile, file
``options-scanner/src/iv_surface.py``): a 5-parameter polynomial OLS fit of
the chain's implied-volatility surface, then a per-pick deviation score in
**IV percentage points**.

    IV(K, T) ≈ a + b·m + c·m² + d·√T + e·m·√T
    where m = ln(K/S), T = DTE/365

Negative ``iv_excess_pp`` = cheap relative to the fitted surface
(strong long-option entry). Positive = rich (avoid as a buyer).

This is **complementary** to the per-pick EV math the options overlay
already produces. The overlay tells you *whether the trade pays at PT*;
this script tells you *whether you're paying fair premium to get there*.
Both must agree for an A-grade entry.

Earnings awareness
------------------
A known weakness of the stockpile methodology is the lack of an earnings
filter — sustained post-earnings IV creep silently shows up as "rich" in
the surface fit. When ``earnings_calendar.json`` exists next to the
matrix run (auto-discovered, or pass ``--earnings-calendar``), picks whose
``next_earnings.date`` falls inside the option's expiry window are
flagged with ``earnings_in_window`` and the verdict is downgraded
accordingly.

Inputs
------
- ``--matrix-run runs/<id>/`` (required)
  Reads ``options_overlay.json`` from this directory.
- ``--earnings-calendar <path>`` (optional; auto-discovers
  ``<matrix-run>/earnings_calendar.json``)

Outputs (next to the matrix run by default)
-------------------------------------------
- ``iv_surface_ranking.json`` — programmatic
- ``iv_surface_ranking.md``   — human-readable table

Usage
-----
    python scripts/score_picks_iv_surface.py \\
        --matrix-run runs/matrix_2026-05-15_top25

Or override the output location:
    python scripts/score_picks_iv_surface.py \\
        --matrix-run runs/<id> \\
        --output runs/cross_run_2026-05-15/iv_surface_ranking.json

Caveats (all inherited from the methodology)
--------------------------------------------
1. The 5-param polynomial misses wing flattening at extreme moneyness.
   Don't trust the fit for strikes >25% OTM/ITM.
2. Polygon ``implied_volatility`` is a model-derived field for many
   contracts, not exchange-quoted. Validate the actual mark on the
   broker chain before entry.
3. No vol-of-vol normalization — a +1pp deviation on a quiet name and
   on a high-IV name are NOT directly comparable.
4. Small-cap chains can be too thin for a stable fit. We auto-widen
   the strike window once if the initial fit has <5 valid rows; if
   still too thin we mark the row ``fit_failed``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import numpy as np  # noqa: E402

from scripts.build_options_overlay import _fetch_chain  # noqa: E402


SCHEMA_VERSION = 1
DEFAULT_STRIKE_WINDOW_PCT = 25.0
DEFAULT_EXPIRY_WINDOW_DAYS = 60
WIDE_STRIKE_WINDOW_PCT = 150.0
WIDE_EXPIRY_WINDOW_DAYS = 365
MIN_FIT_ROWS = 5

# Verdict thresholds — same as the deliverable summary
_CHEAP_PP = -1.0
_FAIR_PP = 0.5
_RICH_PP = 2.0


# ──────────────────────────────────────────────────────────────────────
# IV-surface fit
# ──────────────────────────────────────────────────────────────────────

def fit_surface(
    moneyness: Iterable[float],
    sqrt_t: Iterable[float],
    iv: Iterable[float],
) -> np.ndarray | None:
    """Fit IV ≈ a + b·m + c·m² + d·√T + e·m·√T via OLS.

    Returns the 5-vector of coefficients or ``None`` if the fit is
    degenerate (fewer than 5 unique design rows).
    """
    m = np.asarray(list(moneyness), dtype=float)
    s = np.asarray(list(sqrt_t), dtype=float)
    y = np.asarray(list(iv), dtype=float)
    if m.size < MIN_FIT_ROWS:
        return None
    X = np.column_stack([np.ones_like(m), m, m * m, s, m * s])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coef)):
        return None
    return coef


def predict_iv(coef: np.ndarray, m: float, sqrt_t: float) -> float:
    return float(
        coef[0]
        + coef[1] * m
        + coef[2] * m * m
        + coef[3] * sqrt_t
        + coef[4] * m * sqrt_t
    )


# ──────────────────────────────────────────────────────────────────────
# Chain extraction
# ──────────────────────────────────────────────────────────────────────

def _chain_iv_rows(chain: list[dict], spot: float, today: date
                   ) -> list[dict]:
    """Extract clean per-contract rows from a Polygon snapshot chain.

    Filters: iv > 0.02, DTE > 0, strike > 0.
    """
    out: list[dict] = []
    for c in chain:
        det = c.get("details") or {}
        strike = det.get("strike_price")
        exp = det.get("expiration_date")
        iv = c.get("implied_volatility")
        if not strike or strike <= 0 or not exp or iv is None:
            continue
        try:
            iv_f = float(iv)
        except (TypeError, ValueError):
            continue
        if iv_f <= 0.02:
            continue
        try:
            exp_d = date.fromisoformat(exp)
        except ValueError:
            continue
        dte = (exp_d - today).days
        if dte <= 0:
            continue
        out.append({
            "strike": float(strike),
            "expiry": exp,
            "dte": dte,
            "iv": iv_f,
            "moneyness": float(np.log(strike / spot)),
            "sqrt_t": float(np.sqrt(dte / 365.0)),
        })
    return out


# ──────────────────────────────────────────────────────────────────────
# Per-pick scoring
# ──────────────────────────────────────────────────────────────────────

def _pick_strike_and_expiry(overlay_row: dict) -> tuple[float | None, str | None]:
    """Return (strike, expiry) of the long leg of the recommended structure.

    Long-call mode: legs[0] is the single long call.
    Tier-driven spread mode: legs[0] is the long leg (lower strike).
    Both share leg index 0 = long.
    """
    legs = overlay_row.get("legs") or []
    if not legs:
        return None, None
    long_leg = legs[0]
    return long_leg.get("strike"), overlay_row.get("expiration") or long_leg.get("expiration")


def _earnings_in_window(
    earnings_info: dict | None,
    today: date,
    expiry: date,
) -> dict | None:
    """Return earnings flag if next_earnings.date falls inside (today, expiry]."""
    if not earnings_info:
        return None
    next_e = earnings_info.get("next_earnings") if "next_earnings" in earnings_info else earnings_info
    if not next_e:
        return None
    d_str = next_e.get("date")
    if not d_str:
        return None
    try:
        e_date = date.fromisoformat(d_str)
    except ValueError:
        return None
    if not (today < e_date <= expiry):
        return None
    return {
        "date": d_str,
        "days_to_event": (e_date - today).days,
        "days_to_expiry": (expiry - today).days,
    }


def _verdict(iv_excess_pp: float | None, earnings_flag: dict | None) -> str:
    if iv_excess_pp is None:
        return "fit_failed"
    if iv_excess_pp < _CHEAP_PP:
        base = "cheap"
    elif iv_excess_pp < _FAIR_PP:
        base = "fair"
    elif iv_excess_pp < _RICH_PP:
        base = "near_par"
    else:
        base = "rich"
    if earnings_flag and base in ("near_par", "rich"):
        return f"{base}+earnings_in_window"
    return base


def score_pick(
    overlay_row: dict,
    today: date,
    earnings_by_ticker: dict[str, dict],
    strike_window_pct: float = DEFAULT_STRIKE_WINDOW_PCT,
    expiry_window_days: int = DEFAULT_EXPIRY_WINDOW_DAYS,
    pace_seconds: float = 1.5,
) -> dict:
    """Score a single pick against its locally-fit IV surface."""
    t = overlay_row.get("ticker")
    spot = overlay_row.get("current_price_usd")
    tier = overlay_row.get("tier")
    strike, exp_str = _pick_strike_and_expiry(overlay_row)
    base = {
        "ticker": t,
        "tier": tier,
        "current_price_usd": spot,
        "strike": strike,
        "expiry": exp_str,
    }
    if "error" in overlay_row or "skipped" in overlay_row:
        return {**base, "skipped": overlay_row.get("skipped") or overlay_row.get("error") or "no overlay"}
    if not (t and spot and strike and exp_str):
        return {**base, "skipped": "missing ticker/spot/strike/expiry"}
    try:
        exp_d = date.fromisoformat(exp_str)
    except ValueError:
        return {**base, "skipped": f"bad expiry: {exp_str}"}
    pick_dte = (exp_d - today).days
    if pick_dte <= 0:
        return {**base, "skipped": f"expiry in past: {exp_str}"}

    earnings_flag = _earnings_in_window(
        earnings_by_ticker.get(t.upper()), today, exp_d,
    )

    # Window the chain around the pick to dodge Polygon's 250-contract page cap.
    def _fetch_with_window(strike_pct: float, exp_days: int) -> list[dict]:
        strike_lo = max(0.5, spot * (1 - strike_pct / 100.0))
        strike_hi = spot * (1 + strike_pct / 100.0)
        exp_lo = today + timedelta(days=max(7, pick_dte - exp_days))
        exp_hi = exp_d + timedelta(days=exp_days)
        chain, _err = _fetch_chain(
            t, "call",
            strike_lo, strike_hi,
            exp_lo.isoformat(), exp_hi.isoformat(),
        )
        return chain

    chain = _fetch_with_window(strike_window_pct, expiry_window_days)
    rows = _chain_iv_rows(chain, spot, today)
    widened = False
    if len(rows) < MIN_FIT_ROWS:
        widened = True
        chain = _fetch_with_window(WIDE_STRIKE_WINDOW_PCT, WIDE_EXPIRY_WINDOW_DAYS)
        rows = _chain_iv_rows(chain, spot, today)

    if pace_seconds > 0:
        time.sleep(pace_seconds)

    if len(rows) < MIN_FIT_ROWS:
        return {
            **base,
            "verdict": "fit_failed",
            "warnings": [f"only {len(rows)} valid chain rows after widening"],
            "earnings_in_window": earnings_flag,
            "widened_window": widened,
        }

    coef = fit_surface(
        [r["moneyness"] for r in rows],
        [r["sqrt_t"] for r in rows],
        [r["iv"] for r in rows],
    )
    if coef is None:
        return {
            **base,
            "verdict": "fit_failed",
            "warnings": [f"OLS fit degenerate with n={len(rows)}"],
            "earnings_in_window": earnings_flag,
            "widened_window": widened,
        }

    # Find the matching contract in the chain (closest strike on the exact expiry).
    same_exp = [r for r in rows if r["expiry"] == exp_str]
    if same_exp:
        matched = min(same_exp, key=lambda r: abs(r["strike"] - strike))
    else:
        # Fallback: closest strike+expiry combination
        matched = min(
            rows,
            key=lambda r: (abs(r["strike"] - strike), abs(r["dte"] - pick_dte)),
        )

    pick_iv = matched["iv"]
    fitted_iv = predict_iv(coef, matched["moneyness"], matched["sqrt_t"])
    excess_pp = (pick_iv - fitted_iv) * 100.0

    # Percentiles: how cheap/rich is this contract vs the rest of the chain?
    all_excess = []
    for r in rows:
        f = predict_iv(coef, r["moneyness"], r["sqrt_t"])
        all_excess.append((r["iv"] - f) * 100.0)
    rank = sum(1 for e in all_excess if e <= excess_pp)
    cross_fit_pctl = round(rank / len(all_excess) * 100.0, 1)

    same_exp_excess = []
    for r in same_exp:
        f = predict_iv(coef, r["moneyness"], r["sqrt_t"])
        same_exp_excess.append((r["iv"] - f) * 100.0)
    if same_exp_excess:
        rank_se = sum(1 for e in same_exp_excess if e <= excess_pp)
        same_exp_pctl = round(rank_se / len(same_exp_excess) * 100.0, 1)
    else:
        same_exp_pctl = None

    warnings: list[str] = []
    if widened:
        warnings.append("chain thin: widened window used (small-cap data risk)")
    if same_exp_pctl is None:
        warnings.append("no same-expiration peers in window")
    if earnings_flag:
        warnings.append(
            f"earnings inside expiry window: {earnings_flag['date']} "
            f"({earnings_flag['days_to_event']}d away, {earnings_flag['days_to_expiry']}d to expiry)"
        )

    return {
        **base,
        "matched_strike": round(matched["strike"], 2),
        "matched_expiry": matched["expiry"],
        "matched_dte": matched["dte"],
        "pick_iv_pct": round(pick_iv * 100.0, 2),
        "fitted_iv_pct": round(fitted_iv * 100.0, 2),
        "iv_excess_pp": round(excess_pp, 2),
        "cross_fit_percentile": cross_fit_pctl,
        "same_exp_percentile": same_exp_pctl,
        "n_contracts_fit": len(rows),
        "n_same_exp": len(same_exp),
        "widened_window": widened,
        "earnings_in_window": earnings_flag,
        "verdict": _verdict(excess_pp, earnings_flag),
        "warnings": warnings,
    }


# ──────────────────────────────────────────────────────────────────────
# Markdown rendering
# ──────────────────────────────────────────────────────────────────────

_VERDICT_BADGE = {
    "cheap": "💚 CHEAP",
    "fair": "✓ fair",
    "near_par": "≈ par",
    "rich": "⚠ RICH",
    "near_par+earnings_in_window": "⚠ par+earnings",
    "rich+earnings_in_window": "🚫 RICH+earnings",
    "fit_failed": "— fit failed",
}


def render_md(matrix_run_name: str, rows: list[dict], today: date) -> str:
    valid = [r for r in rows if "iv_excess_pp" in r]
    failed = [r for r in rows if r.get("verdict") == "fit_failed" or "skipped" in r]
    valid.sort(key=lambda r: r["iv_excess_pp"])

    lines: list[str] = []
    lines.append(f"# IV-Surface Ranking · {matrix_run_name}")
    lines.append("")
    lines.append(f"_Generated {today.isoformat()}._  "
                 f"Methodology: medloh/stockpile (`options-scanner/src/iv_surface.py`) — "
                 f"5-param OLS fit IV(K,T) ≈ a + b·m + c·m² + d·√T + e·m·√T.")
    lines.append("")
    lines.append("Negative `iv_excess_pp` = cheap relative to fitted surface = "
                 "good long-call entry. `same_exp_pctl` = position among same-expiry peers (lower = cheaper).")
    lines.append("")
    lines.append("## Ranking — coldest to hottest")
    lines.append("")
    lines.append("| # | Tkr | Tier | Strike | Exp | DTE | IV % | Fitted % | **Excess pp** | Same-exp pctl | n_fit | Verdict |")
    lines.append("|:--:|--|:--:|--:|--|--:|--:|--:|--:|--:|--:|--|")
    for i, r in enumerate(valid, 1):
        se = f"{r['same_exp_percentile']:.0f}%" if r.get("same_exp_percentile") is not None else "—"
        badge = _VERDICT_BADGE.get(r["verdict"], r["verdict"])
        lines.append(
            f"| {i} | **{r['ticker']}** | {r.get('tier') or '—'} | "
            f"${r['matched_strike']:.2f} | {r['matched_expiry']} | {r['matched_dte']} | "
            f"{r['pick_iv_pct']:.1f}% | {r['fitted_iv_pct']:.1f}% | "
            f"**{r['iv_excess_pp']:+.2f}** | {se} | {r['n_contracts_fit']} | {badge} |"
        )
    lines.append("")

    if failed:
        lines.append("## Could not score")
        lines.append("")
        for r in failed:
            why = r.get("skipped") or (r.get("warnings") or ["unknown"])[0]
            lines.append(f"- **{r.get('ticker')}** — {why}")
        lines.append("")

    flagged_earnings = [r for r in valid if r.get("earnings_in_window")]
    if flagged_earnings:
        lines.append("## Earnings inside expiry window")
        lines.append("")
        for r in flagged_earnings:
            ef = r["earnings_in_window"]
            lines.append(
                f"- **{r['ticker']}** — earnings on `{ef['date']}` "
                f"({ef['days_to_event']}d away). Expiry `{r['expiry']}` is "
                f"{ef['days_to_expiry']}d out. IV excess **{r['iv_excess_pp']:+.2f}pp** "
                f"likely reflects earnings premium."
            )
        lines.append("")

    lines.append("## Methodology caveats (per the methodology's known weaknesses)")
    lines.append("")
    lines.append("1. **5-param polynomial misses wing flattening** — don't trust the fit at strikes >25% OTM/ITM.")
    lines.append("2. **Polygon IV is partially model-derived** — validate against the broker chain before entry.")
    lines.append("3. **No vol-of-vol normalization** — a +1pp deviation on a quiet name and on a high-IV name are NOT directly comparable.")
    lines.append("4. **Small-cap chain starvation** — rows with `widened_window=true` had <5 valid contracts in the local window. Treat with caution.")
    lines.append("5. **Earnings flag is opt-in** — depends on `earnings_calendar.json` being present next to the matrix run.")
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def _load_earnings_calendar(path: Path | None) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[iv-surface] ⚠ failed to read {path}: {exc} (continuing without)", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for row in data.get("tickers", []) or []:
        t = (row.get("ticker") or "").upper()
        if t:
            out[t] = row
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Score picks against an IV-surface fit (stockpile methodology)")
    p.add_argument("--matrix-run", required=True,
                   help="Path to runs/matrix_<id>/ — reads options_overlay.json from here")
    p.add_argument("--earnings-calendar", default=None,
                   help="Optional path to earnings_calendar.json (auto-discovers <matrix-run>/earnings_calendar.json)")
    p.add_argument("--snapshot-date", default=None,
                   help="Override 'today' for DTE calc (YYYY-MM-DD); defaults to system date")
    p.add_argument("--output", default=None,
                   help="Output JSON path; defaults to <matrix-run>/iv_surface_ranking.json")
    p.add_argument("--strike-window-pct", type=float, default=DEFAULT_STRIKE_WINDOW_PCT,
                   help=f"Strike window % around spot (default {DEFAULT_STRIKE_WINDOW_PCT})")
    p.add_argument("--expiry-window-days", type=int, default=DEFAULT_EXPIRY_WINDOW_DAYS,
                   help=f"Expiry window days around pick expiry (default {DEFAULT_EXPIRY_WINDOW_DAYS})")
    p.add_argument("--pace-seconds", type=float, default=1.5,
                   help="Sleep between Polygon calls (default 1.5)")
    args = p.parse_args()

    matrix_run = Path(args.matrix_run).resolve()
    overlay_path = matrix_run / "options_overlay.json"
    if not overlay_path.exists():
        print(f"❌ {overlay_path} not found — run build_options_overlay.py first", file=sys.stderr)
        return 2

    overlay = json.loads(overlay_path.read_text())
    overlays = overlay.get("overlays") or []
    today = date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()

    # Earnings calendar
    ec_path: Path | None = None
    if args.earnings_calendar:
        ec_path = Path(args.earnings_calendar)
    else:
        sibling = matrix_run / "earnings_calendar.json"
        if sibling.exists():
            ec_path = sibling
    earnings_by_ticker = _load_earnings_calendar(ec_path)

    print(f"[iv-surface] matrix: {matrix_run.name} · "
          f"picks: {len(overlays)} · "
          f"earnings rows: {len(earnings_by_ticker)} · "
          f"today: {today.isoformat()}")

    scored: list[dict] = []
    for i, ovl in enumerate(overlays, 1):
        t = ovl.get("ticker") or "?"
        print(f"  [{i}/{len(overlays)}] {t} ...", end=" ", flush=True)
        row = score_pick(
            ovl, today, earnings_by_ticker,
            strike_window_pct=args.strike_window_pct,
            expiry_window_days=args.expiry_window_days,
            pace_seconds=args.pace_seconds,
        )
        if "skipped" in row:
            print(f"skipped ({row['skipped']})")
        elif row.get("verdict") == "fit_failed":
            print(f"fit failed ({(row.get('warnings') or ['?'])[0]})")
        else:
            badge = _VERDICT_BADGE.get(row["verdict"], row["verdict"])
            print(f"{row['iv_excess_pp']:+.2f}pp · {badge}")
        scored.append(row)

    n_fitted = sum(1 for r in scored if "iv_excess_pp" in r)
    n_cheap = sum(1 for r in scored if r.get("verdict") == "cheap")
    n_rich = sum(1 for r in scored if r.get("verdict", "").startswith("rich"))
    n_earnings = sum(1 for r in scored if r.get("earnings_in_window"))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "matrix_run": matrix_run.name,
        "snapshot_date": today.isoformat(),
        "earnings_calendar": ec_path.name if ec_path else None,
        "n_picks": len(scored),
        "n_fitted": n_fitted,
        "n_cheap": n_cheap,
        "n_rich": n_rich,
        "n_earnings_flagged": n_earnings,
        "rows": scored,
    }

    out_json = Path(args.output) if args.output else matrix_run / "iv_surface_ranking.json"
    out_md = out_json.with_suffix(".md")
    out_json.write_text(json.dumps(payload, indent=2, default=str))
    out_md.write_text(render_md(matrix_run.name, scored, today))

    print()
    print(f"  ✅ {out_json}")
    print(f"  ✅ {out_md}")
    print(f"  Summary: {n_fitted}/{len(scored)} fitted · "
          f"{n_cheap} cheap · {n_rich} rich · {n_earnings} earnings-flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
