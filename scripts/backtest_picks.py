#!/usr/bin/env python3
"""Realized-outcome backtest of past long-call PICKs.

This is the first *realized* (not modeled) look-back at the matrix
recommendations. For every historical PICK with a long-call leg recorded in
``runs/index.db``, it pulls ACTUAL subsequent market data from Polygon:

  * Underlying daily bars from the recommendation date to a measurement date
    (today, capped at the option expiration). → realized underlying return,
    and whether the aggressive price target was ever touched (max daily high).
  * The ACTUAL option contract's daily bars over the same window (OCC ticker
    reconstructed from strike + expiration). → market-based entry premium
    (first available close), exit value (last available close), peak value,
    realized option ROI, and a profitable yes/no.

It then aggregates by tier (A/B/C) so we can finally answer: did we hit the
price targets we called, and did the long calls actually make money?

IMPORTANT: this measures REALIZED outcomes from real prices. It is distinct
from the "ROI by tier" line in ``index_runs.py``, which is a *modeled*
gain-if-PT-hits-at-expiry figure and assumes a 100% PT-hit rate. The two
should not be conflated.

Usage:
    source ./.env
    .venv/bin/python scripts/backtest_picks.py
    .venv/bin/python scripts/backtest_picks.py --measure-date 2026-06-26 \
        --dedupe --pace 0.2 --json runs/backtest_2026-06-26.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dataflows.polygon_bars import fetch_daily_bars, occ_ticker  # noqa: E402
from tradingagents.dataflows.polygon_common import min_request_interval  # noqa: E402


def _api_key() -> str:
    k = os.environ.get("POLYGON_API_KEY")
    if not k:
        sys.exit("POLYGON_API_KEY not set. Run `source ./.env` first.")
    return k


def _occ_ticker(under: str, expiration: str, strike: float, opt_type: str = "C") -> str:
    return occ_ticker(under, expiration, strike, opt_type)


def _daily_bars(ticker: str, dfrom: str, dto: str, key: str, pace: float):
    """Fetch daily bars via the shared polygon_bars helper, preserving this
    script's (bars, auth_error_code) return contract. ``key`` is accepted
    for backward compatibility but unused — polygon_common resolves
    POLYGON_API_KEY from the environment itself. ``pace`` is applied via the
    process-wide pacer (see main()) rather than an unconditional sleep here."""
    bars, err = fetch_daily_bars(ticker, dfrom, dto)
    if bars is None:
        auth_code = int(err) if err in ("401", "403") else None
        return None, auth_code
    return bars, None


def _run_date(run_id: str) -> str | None:
    """Extract a YYYY-MM-DD date from a run_id."""
    import re
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", run_id)
    return m.group(1) if m else None


def load_picks(db: str, dedupe: bool):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT mp.run_id, mp.ticker, mp.tier, mp.current_price AS rec_px,
                  mp.aggressive_pt AS pt, mp.aggressive_horizon AS horizon,
                  ol.leg_strike AS strike, ol.expiration AS expiration,
                  ol.net_debit_per_share AS model_prem
           FROM matrix_picks mp
           JOIN options_legs ol
             ON mp.run_id = ol.run_id AND mp.ticker = ol.ticker
           WHERE mp.classification = 'PICK'
             AND ol.strategy_mode = 'long-call'
             AND ol.leg_index = 0
           ORDER BY mp.run_id, mp.ticker"""
    ).fetchall()
    conn.close()

    picks = []
    for r in rows:
        d = _run_date(r["run_id"])
        if not d:
            continue
        picks.append({
            "run_id": r["run_id"], "ticker": r["ticker"], "tier": r["tier"],
            "rec_date": d, "rec_px": r["rec_px"], "pt": r["pt"],
            "horizon": r["horizon"], "strike": r["strike"],
            "expiration": r["expiration"], "model_prem": r["model_prem"],
        })

    if dedupe:
        seen = {}
        for p in picks:
            key = (p["ticker"], p["rec_date"], p["strike"], p["expiration"])
            if key not in seen:
                seen[key] = p
        picks = list(seen.values())
    return picks


def backtest(picks, measure_date: str, key: str, pace: float):
    results = []
    today = measure_date
    for i, p in enumerate(picks, 1):
        exp = p["expiration"]
        # measurement window ends at min(today, expiration)
        dto = min(today, exp)
        dfrom = p["rec_date"]
        if dfrom >= dto:
            # rec on/after measurement or already expired before rec — skip
            results.append({**p, "status": "window_invalid"})
            continue

        # --- underlying ---
        ubars, uerr = _daily_bars(p["ticker"], dfrom, dto, key, pace)
        # --- option ---
        occ = _occ_ticker(p["ticker"], exp, p["strike"])
        obars, oerr = _daily_bars(occ, dfrom, dto, key, pace)

        rec = dict(p)
        rec["occ"] = occ
        rec["measure_date"] = dto

        if ubars:
            u_close_last = ubars[-1]["c"]
            u_high_max = max(b["h"] for b in ubars)
            rec["under_last"] = round(u_close_last, 2)
            rec["under_return_pct"] = round((u_close_last / p["rec_px"] - 1) * 100, 1)
            rec["pt_hit"] = bool(p["pt"] and u_high_max >= p["pt"])
            rec["under_peak_return_pct"] = round((u_high_max / p["rec_px"] - 1) * 100, 1)
        else:
            rec["under_last"] = None
            rec["pt_hit"] = None

        if obars and len(obars) >= 1:
            o_entry = obars[0]["c"]
            o_exit = obars[-1]["c"]
            o_peak = max(b["h"] for b in obars)
            rec["opt_entry"] = round(o_entry, 2)
            rec["opt_exit"] = round(o_exit, 2)
            rec["opt_peak"] = round(o_peak, 2)
            rec["opt_roi_pct"] = round((o_exit / o_entry - 1) * 100, 1) if o_entry else None
            rec["opt_peak_roi_pct"] = round((o_peak / o_entry - 1) * 100, 1) if o_entry else None
            rec["profitable"] = (o_exit > o_entry) if o_entry else None
            rec["opt_bars"] = len(obars)
            rec["status"] = "ok"
        else:
            rec["opt_entry"] = None
            rec["status"] = "no_option_data" if not oerr else f"opt_auth_{oerr}"

        results.append(rec)
        print(f"  [{i}/{len(picks)}] {p['ticker']:6} {p['rec_date']} tier {p['tier'] or '—'}"
              f"  under={rec.get('under_return_pct')}%  pt_hit={rec.get('pt_hit')}"
              f"  optROI={rec.get('opt_roi_pct')}%  ({rec['status']})", flush=True)
    return results


def aggregate(results):
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in results:
        if r.get("status") == "ok":
            buckets[r["tier"] or "—"].append(r)

    print("\n" + "=" * 78)
    print("REALIZED LONG-CALL OUTCOMES BY TIER (actual market prices)")
    print("=" * 78)
    header = (f"{'Tier':<5} {'n':>3} {'PT-hit':>7} {'avg under%':>11} "
              f"{'avg optROI%':>12} {'median optROI%':>15} {'profit%':>8} {'avg peak%':>10}")
    print(header)
    print("-" * len(header))

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    summary = {}
    for tier in ["A", "B", "C", "—"]:
        rs = buckets.get(tier)
        if not rs:
            continue
        n = len(rs)
        pt_hits = [r for r in rs if r.get("pt_hit")]
        pt_known = [r for r in rs if r.get("pt_hit") is not None]
        rois = [r["opt_roi_pct"] for r in rs if r.get("opt_roi_pct") is not None]
        unders = [r["under_return_pct"] for r in rs if r.get("under_return_pct") is not None]
        peaks = [r["opt_peak_roi_pct"] for r in rs if r.get("opt_peak_roi_pct") is not None]
        prof = [r for r in rs if r.get("profitable")]
        prof_known = [r for r in rs if r.get("profitable") is not None]
        pt_rate = (len(pt_hits) / len(pt_known) * 100) if pt_known else None
        prof_rate = (len(prof) / len(prof_known) * 100) if prof_known else None
        avg_under = sum(unders) / len(unders) if unders else None
        avg_roi = sum(rois) / len(rois) if rois else None
        avg_peak = sum(peaks) / len(peaks) if peaks else None
        print(f"{tier:<5} {n:>3} "
              f"{(f'{pt_rate:.0f}%' if pt_rate is not None else '—'):>7} "
              f"{(f'{avg_under:+.1f}' if avg_under is not None else '—'):>11} "
              f"{(f'{avg_roi:+.1f}' if avg_roi is not None else '—'):>12} "
              f"{(f'{med(rois):+.1f}' if med(rois) is not None else '—'):>15} "
              f"{(f'{prof_rate:.0f}%' if prof_rate is not None else '—'):>8} "
              f"{(f'{avg_peak:+.1f}' if avg_peak is not None else '—'):>10}")
        summary[tier] = {
            "n": n, "pt_hit_rate_pct": pt_rate, "avg_underlying_return_pct": avg_under,
            "avg_option_roi_pct": avg_roi, "median_option_roi_pct": med(rois),
            "profitable_rate_pct": prof_rate, "avg_peak_option_roi_pct": avg_peak,
        }

    # overall
    allok = [r for r in results if r.get("status") == "ok"]
    rois = [r["opt_roi_pct"] for r in allok if r.get("opt_roi_pct") is not None]
    prof_known = [r for r in allok if r.get("profitable") is not None]
    prof = [r for r in prof_known if r.get("profitable")]
    pt_known = [r for r in allok if r.get("pt_hit") is not None]
    pt_hits = [r for r in pt_known if r.get("pt_hit")]
    print("-" * len(header))
    overall = {
        "n": len(allok),
        "pt_hit_rate_pct": (len(pt_hits) / len(pt_known) * 100) if pt_known else None,
        "avg_option_roi_pct": (sum(rois) / len(rois)) if rois else None,
        "median_option_roi_pct": med(rois),
        "profitable_rate_pct": (len(prof) / len(prof_known) * 100) if prof_known else None,
    }
    print(f"{'ALL':<5} {overall['n']:>3} "
          f"{(f'{overall['pt_hit_rate_pct']:.0f}%' if overall['pt_hit_rate_pct'] is not None else '—'):>7} "
          f"{'':>11} "
          f"{(f'{overall['avg_option_roi_pct']:+.1f}' if overall['avg_option_roi_pct'] is not None else '—'):>12} "
          f"{(f'{overall['median_option_roi_pct']:+.1f}' if overall['median_option_roi_pct'] is not None else '—'):>15} "
          f"{(f'{overall['profitable_rate_pct']:.0f}%' if overall['profitable_rate_pct'] is not None else '—'):>8}")

    # coverage
    statuses = {}
    for r in results:
        statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
    print(f"\nCoverage: {statuses}")
    return {"by_tier": summary, "overall": overall, "coverage": statuses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="runs/index.db")
    ap.add_argument("--measure-date", default=date.today().isoformat(),
                    help="Outcome measurement date (default: today)")
    ap.add_argument("--dedupe", action="store_true",
                    help="Dedupe by (ticker, rec_date, strike, expiration) to drop "
                         "experimental re-runs")
    ap.add_argument("--pace", type=float, default=0.15,
                    help="Seconds to sleep between Polygon calls")
    ap.add_argument("--json", help="Write full per-pick results + summary to this path")
    args = ap.parse_args()

    key = _api_key()
    picks = load_picks(args.db, args.dedupe)
    print(f"Loaded {len(picks)} long-call PICK rows"
          f"{' (deduped)' if args.dedupe else ''} from {args.db}")
    print(f"Measurement date: {args.measure_date}\n")

    with min_request_interval(args.pace):
        results = backtest(picks, args.measure_date, key, args.pace)
    summary = aggregate(results)

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({"measure_date": args.measure_date,
                       "deduped": args.dedupe,
                       "results": results, "summary": summary}, f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
