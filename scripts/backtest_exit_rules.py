#!/usr/bin/env python3
"""Exit-discipline backtest: does taking profit at the called PTs beat hold?

Builds directly on the realized-outcome idea in ``backtest_picks.py`` but adds
profit-taking simulation. For every historical long-call PICK it replays the
trade on the ACTUAL option contract (Polygon daily bars) under four exit
strategies and compares realized ROI:

  HOLD       Buy at first option close, mark at last option close in window
             (mark-to-measurement-date; identical to backtest_picks.py).
  EXIT_CONS  Sell 100% on the first day the underlying's daily HIGH touches the
             CONSERVATIVE price target. (Only A/B picks have a cons PT.)
  EXIT_AGGR  Sell 100% on the first day the underlying's daily HIGH touches the
             AGGRESSIVE price target.
  EXIT_HALF  Sell 50% at the cons-PT trigger and 50% at the aggr-PT trigger
             (the policy's actual scale-out). Untriggered fractions are marked
             to the last close. Falls back to all-at-aggr when no cons PT.

Exit price on a trigger day = that option contract's CLOSE on the first day the
target is touched; if the option didn't print that day, the next available
option bar is used (forward fill); if the target is never touched, the leg is
held and marked at the last close.

CAVEATS (same family as backtest_picks.py):
  * Marked to --measure-date, NOT held to expiry (most contracts still open).
  * Daily closes only: no bid/ask, slippage, or commissions. Thin contracts
    give noisy entry/exit marks.
  * Exit uses the option CLOSE on the trigger day, not the intraday print when
    the underlying actually crossed — a conservative approximation.

Usage:
    source ./.env
    .venv/bin/python scripts/backtest_exit_rules.py --dedupe \
        --measure-date 2026-06-26 --json runs/backtest_exits_2026-06-26.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, date
from collections import defaultdict
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


def _bars(ticker: str, dfrom: str, dto: str, key: str, pace: float):
    """Fetch daily bars via the shared polygon_bars helper, preserving this
    script's (bars-or-None) return contract. ``key`` is accepted for
    backward compatibility but unused; ``pace`` is applied via the
    process-wide pacer (see main()) rather than an unconditional sleep here."""
    bars, _err = fetch_daily_bars(ticker, dfrom, dto)
    return bars


def _d(ms: int) -> str:
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _run_date(run_id: str):
    import re
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", run_id)
    return m.group(1) if m else None


def load_picks(db: str, dedupe: bool):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT mp.run_id, mp.ticker, mp.tier,
                  mp.current_price AS rec_px,
                  mp.aggressive_pt AS aggr_pt,
                  mp.conservative_pt AS cons_pt,
                  ol.leg_strike AS strike, ol.expiration AS expiration
           FROM matrix_picks mp
           JOIN options_legs ol
             ON mp.run_id = ol.run_id AND mp.ticker = ol.ticker
           WHERE mp.classification = 'PICK'
             AND ol.strategy_mode = 'long-call' AND ol.leg_index = 0
           ORDER BY mp.run_id, mp.ticker"""
    ).fetchall()
    conn.close()
    picks = []
    for r in rows:
        d = _run_date(r["run_id"])
        if not d:
            continue
        picks.append(dict(run_id=r["run_id"], ticker=r["ticker"], tier=r["tier"],
                          rec_date=d, rec_px=r["rec_px"], aggr_pt=r["aggr_pt"],
                          cons_pt=r["cons_pt"], strike=r["strike"],
                          expiration=r["expiration"]))
    if dedupe:
        seen = {}
        for p in picks:
            k = (p["ticker"], p["rec_date"], p["strike"], p["expiration"])
            seen.setdefault(k, p)
        picks = list(seen.values())
    return picks


def _trigger_date(ubars, pt):
    """First date underlying daily HIGH >= pt, else None."""
    if pt is None:
        return None
    for b in ubars:
        if b["h"] >= pt:
            return _d(b["t"])
    return None


def _opt_close_on_or_after(obars_by_date, sorted_dates, trig_date):
    """Option close on trig_date, else next available option bar (fwd fill)."""
    if trig_date in obars_by_date:
        return obars_by_date[trig_date]
    for d in sorted_dates:
        if d >= trig_date:
            return obars_by_date[d]
    return None


def simulate(picks, measure_date, key, pace):
    results = []
    for i, p in enumerate(picks, 1):
        dto = min(measure_date, p["expiration"])
        dfrom = p["rec_date"]
        rec = dict(p)
        if dfrom >= dto:
            rec["status"] = "window_invalid"
            results.append(rec)
            continue

        ubars = _bars(p["ticker"], dfrom, dto, key, pace)
        occ = _occ_ticker(p["ticker"], p["expiration"], p["strike"])
        obars = _bars(occ, dfrom, dto, key, pace)
        rec["occ"] = occ
        rec["measure_date"] = dto

        if not obars or not ubars:
            rec["status"] = "no_data"
            results.append(rec)
            continue

        obars_by_date = {_d(b["t"]): b["c"] for b in obars}
        sorted_odates = sorted(obars_by_date)
        entry = obars[0]["c"]
        last_close = obars[-1]["c"]
        rec["entry"] = round(entry, 2)
        rec["hold_exit"] = round(last_close, 2)

        # PT triggers (underlying-based)
        cons_trig = _trigger_date(ubars, p["cons_pt"])
        aggr_trig = _trigger_date(ubars, p["aggr_pt"])
        rec["cons_pt_hit"] = cons_trig is not None if p["cons_pt"] is not None else None
        rec["aggr_pt_hit"] = aggr_trig is not None

        def roi(exit_px):
            return round((exit_px / entry - 1) * 100, 1) if entry else None

        # HOLD
        rec["roi_hold"] = roi(last_close)

        # EXIT_AGGR
        if aggr_trig:
            px = _opt_close_on_or_after(obars_by_date, sorted_odates, aggr_trig) or last_close
            rec["roi_exit_aggr"] = roi(px)
            rec["exit_aggr_px"] = round(px, 2)
        else:
            rec["roi_exit_aggr"] = roi(last_close)  # never triggered → held
            rec["exit_aggr_px"] = round(last_close, 2)

        # EXIT_CONS (only meaningful where cons PT exists)
        if p["cons_pt"] is not None:
            if cons_trig:
                px = _opt_close_on_or_after(obars_by_date, sorted_odates, cons_trig) or last_close
                rec["roi_exit_cons"] = roi(px)
                rec["exit_cons_px"] = round(px, 2)
            else:
                rec["roi_exit_cons"] = roi(last_close)
                rec["exit_cons_px"] = round(last_close, 2)
        else:
            rec["roi_exit_cons"] = None

        # EXIT_HALF: 50% at cons trigger, 50% at aggr trigger; untriggered
        # halves marked at last close. No cons PT → all-at-aggr fallback.
        if p["cons_pt"] is not None:
            half_cons = (_opt_close_on_or_after(obars_by_date, sorted_odates, cons_trig)
                         if cons_trig else last_close) or last_close
            half_aggr = (_opt_close_on_or_after(obars_by_date, sorted_odates, aggr_trig)
                         if aggr_trig else last_close) or last_close
            blended = 0.5 * half_cons + 0.5 * half_aggr
            rec["roi_exit_half"] = roi(blended)
        else:
            rec["roi_exit_half"] = rec["roi_exit_aggr"]

        rec["status"] = "ok"
        results.append(rec)
        print(f"  [{i}/{len(picks)}] {p['ticker']:6} {p['rec_date']} {p['tier'] or '—'}"
              f"  hold={rec['roi_hold']}%  cons={rec.get('roi_exit_cons')}%"
              f"  aggr={rec['roi_exit_aggr']}%  half={rec['roi_exit_half']}%", flush=True)
    return results


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, None
    s = sorted(vals)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    avg = sum(s) / n
    prof = sum(1 for v in s if v > 0) / n * 100
    return round(avg, 1), round(med, 1), round(prof, 0)


def report(results):
    ok = [r for r in results if r.get("status") == "ok"]
    strategies = [("HOLD", "roi_hold"), ("EXIT_CONS", "roi_exit_cons"),
                  ("EXIT_AGGR", "roi_exit_aggr"), ("EXIT_HALF", "roi_exit_half")]

    def block(title, rows):
        print(f"\n{title}  (n={len(rows)})")
        print(f"  {'strategy':<10} {'avg ROI':>9} {'median':>8} {'profit%':>8}")
        for name, key in strategies:
            avg, med, prof = _stats([r.get(key) for r in rows])
            if avg is None:
                continue
            print(f"  {name:<10} {avg:>+8.1f}% {med:>+7.1f}% "
                  f"{(f'{prof:.0f}%' if prof is not None else '—'):>8}")

    print("\n" + "=" * 70)
    print("EXIT-DISCIPLINE COMPARISON — realized ROI on the actual contracts")
    print("=" * 70)
    block("ALL PICKS", ok)
    for tier in ["A", "B", "C"]:
        rs = [r for r in ok if r["tier"] == tier]
        if rs:
            block(f"TIER {tier}", rs)

    # PT-hit rates
    print("\n--- Price-target hit rates (underlying touched the target) ---")
    for tier in ["A", "B", "C"]:
        rs = [r for r in ok if r["tier"] == tier]
        if not rs:
            continue
        cons_known = [r for r in rs if r.get("cons_pt_hit") is not None]
        cons_hit = [r for r in cons_known if r["cons_pt_hit"]]
        aggr_hit = [r for r in rs if r.get("aggr_pt_hit")]
        cons_str = (f"{len(cons_hit)}/{len(cons_known)} "
                    f"({len(cons_hit)/len(cons_known)*100:.0f}%)") if cons_known else "n/a"
        print(f"  Tier {tier}: cons PT {cons_str:<14} "
              f"aggr PT {len(aggr_hit)}/{len(rs)} ({len(aggr_hit)/len(rs)*100:.0f}%)")

    statuses = defaultdict(int)
    for r in results:
        statuses[r.get("status")] += 1
    print(f"\nCoverage: {dict(statuses)}")

    summary = {}
    for name, key in strategies:
        avg, med, prof = _stats([r.get(key) for r in ok])
        summary[name] = dict(avg_roi=avg, median_roi=med, profit_rate=prof)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="runs/index.db")
    ap.add_argument("--measure-date", default=date.today().isoformat())
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--pace", type=float, default=0.15)
    ap.add_argument("--json")
    args = ap.parse_args()

    key = _api_key()
    picks = load_picks(args.db, args.dedupe)
    print(f"Loaded {len(picks)} long-call PICK rows"
          f"{' (deduped)' if args.dedupe else ''}. Measure: {args.measure_date}\n")
    with min_request_interval(args.pace):
        results = simulate(picks, args.measure_date, key, args.pace)
    summary = report(results)

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(dict(measure_date=args.measure_date, deduped=args.dedupe,
                           results=results, summary=summary), f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
