#!/usr/bin/env python3
"""Build a complete accounting package for a matrix run.

Given a matrix run directory (and optionally the screener it sourced from),
produces:
  - verdict_ledger.csv / verdict_ledger.json (flat ledger, one row per ticker)
  - per_ticker/<T>.md (full thesis + actions per ticker)
  - trade_synthesis.md (master synthesis, sorted by aggressive upside)
  - README.md (folder index + reproduce recipes)
  - current_prices.json (Polygon snapshot for all tickers)

Usage:
    # Standard: matrix sourced from a screener
    python scripts/build_run_accounting.py \\
        --matrix-run runs/matrix_2026-04-30_1839_top25 \\
        --screener-run runs/screener_2026-04-30_0955

    # Ad-hoc: matrix run with explicit ticker list (no screener)
    python scripts/build_run_accounting.py \\
        --matrix-run runs/matrix_portfolio_2026-05-05

When --screener-run is omitted, ledger fields like rank/name/sector/composite_score
will be missing for tickers (the matrix-run state files still drive ratings, PTs,
horizons, and classifications). Re-running is safe: ledger and per_ticker/ are
regenerated from current state.json files; current_prices.json is reused if
already present and --refresh-prices is not specified.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime
from typing import Optional

PROMOTE_RATINGS = {"Overweight", "Buy"}
VETO_RATINGS = {"Underweight", "Sell"}


def _parse_state(path: pathlib.Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
    except Exception:
        return None
    ftd = d.get("final_trade_decision", "") or ""
    rating_m = re.search(r"\*\*Rating\*\*:\s*([A-Za-z]+)", ftd)
    pt_m = re.search(r"\*\*Price Target\*\*:\s*([\d.]+)", ftd)
    horizon_m = re.search(r"\*\*Time Horizon\*\*:\s*([^\n]+)", ftd)
    exec_m = re.search(r"\*\*Executive Summary\*\*:\s*([^\n]+(?:\n(?!\*\*)[^\n]+)*)", ftd)
    thesis_m = re.search(r"\*\*Investment Thesis\*\*:\s*([^\n]+(?:\n(?!\*\*)[^\n]+)*)", ftd)
    actions_m = re.search(r"\*\*Strategic Actions\*\*:\s*([^\n]+(?:\n(?!\*\*)[^\n]+)*)", ftd)
    return {
        "rating": rating_m.group(1) if rating_m else None,
        "price_target": float(pt_m.group(1)) if pt_m else None,
        "horizon": horizon_m.group(1).strip() if horizon_m else None,
        "executive_summary": exec_m.group(1).strip() if exec_m else None,
        "thesis": thesis_m.group(1).strip() if thesis_m else None,
        "strategic_actions": actions_m.group(1).strip() if actions_m else None,
        "final_trade_decision": ftd,
    }


def _classify(aggr_rating: Optional[str], cons_rating: Optional[str]) -> str:
    aggr = aggr_rating or "ERROR"
    cons = cons_rating or "ERROR"
    if aggr in PROMOTE_RATINGS and cons not in VETO_RATINGS and cons != "ERROR":
        return "PICK"
    if aggr in PROMOTE_RATINGS and cons in VETO_RATINGS:
        return "VETOED"
    if aggr in VETO_RATINGS:
        return "VETOED_AGGR"
    return "OTHER"


def _fetch_prices(tickers: list[str], pace: float = 1.4, retry_sleep: float = 70.0) -> dict[str, float]:
    """Fetch previous-day close for each ticker via Polygon REST."""
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:
        pass
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from tradingagents.dataflows.polygon_common import _make_request  # type: ignore

    out: dict[str, float] = {}
    for t in tickers:
        for attempt in range(3):
            try:
                r = _make_request(f"/v2/aggs/ticker/{t}/prev", {"adjusted": "true"})
                res = (r.get("results") or [{}])[0]
                close = res.get("c")
                if close is not None:
                    out[t] = float(close)
                print(f"  {t:6s}  close={close}", flush=True)
                break
            except Exception as e:
                msg = str(e)
                if ("rate" in msg.lower() or "429" in msg) and attempt < 2:
                    print(f"  {t:6s}  rate-limit, sleeping {retry_sleep:.0f}s (attempt {attempt+1})", flush=True)
                    time.sleep(retry_sleep)
                else:
                    print(f"  {t:6s}  ERR {e}", flush=True)
                    break
        time.sleep(pace)
    return out


def _fmt_price(p: Optional[float]) -> str:
    return f"${p:,.2f}" if p is not None else "—"


def _fmt_pct(p: Optional[float]) -> str:
    return f"{p:+.1f}%" if p is not None else "—"


def _per_ticker_md(t: str, cand: dict, cur: Optional[float], aggr: Optional[dict],
                   cons: Optional[dict], classification: str) -> str:
    aggr_rating = aggr["rating"] if aggr else "ERROR"
    cons_rating = cons["rating"] if cons else "ERROR"
    aggr_pt = aggr["price_target"] if aggr else None
    cons_pt = cons["price_target"] if cons else None
    upside = round((aggr_pt - cur) / cur * 100, 1) if (aggr_pt and cur) else None
    cons_upside = round((cons_pt - cur) / cur * 100, 1) if (cons_pt and cur) else None
    compression = round((aggr_pt - cons_pt) / aggr_pt * 100, 1) if (aggr_pt and cons_pt) else None

    lines = [f"# {t} — {cand.get('name','(no name)')}", ""]
    lines.append(f"- **Screener rank**: {cand.get('rank','—')} of 25")
    if cand.get("market_cap"):
        lines.append(f"- **Market cap**: ${cand['market_cap']/1e9:.1f}B")
    else:
        lines.append("- **Market cap**: —")
    lines.append(f"- **SIC sector**: {cand.get('sector_sic','—')}")
    lines.append(f"- **Composite screener score**: {cand.get('composite_score','—')}")
    if cur is not None:
        lines.append(f"- **Current price**: {_fmt_price(cur)} (Polygon prev-close snapshot)")
    else:
        lines.append("- **Current price**: —")
    lines.append(f"- **Classification**: **{classification}**")
    lines.append("")
    lines.append("## Verdicts")
    lines.append("")
    lines.append("| Profile | Rating | Price Target | Implied Upside | Horizon |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| Aggressive   | **{aggr_rating}** | {_fmt_price(aggr_pt)} | {_fmt_pct(upside)} | {aggr['horizon'] if aggr else '—'} |")
    lines.append(f"| Conservative | **{cons_rating}** | {_fmt_price(cons_pt)} | {_fmt_pct(cons_upside)} | {cons['horizon'] if cons else '—'} |")
    lines.append("")
    if compression is not None:
        lines.append(f"**PT compression (aggressive→conservative)**: {compression}% — *lower is stronger conviction; negative means cons PT > aggr PT (rarest signal)*")
        lines.append("")
    for label, src in [("Aggressive", aggr), ("Conservative", cons)]:
        if not src:
            continue
        if src.get("executive_summary"):
            lines += [f"## {label} — Executive Summary", "", src["executive_summary"], ""]
        if src.get("thesis"):
            lines += [f"## {label} — Investment Thesis", "", src["thesis"], ""]
        if src.get("strategic_actions"):
            lines += [f"## {label} — Strategic Actions", "", src["strategic_actions"], ""]
    lines += ["## Source artifacts", "",
              f"- Aggressive cell:   `cells/aggressive/{t}/`",
              f"- Conservative cell: `cells/conservative/{t}/`",
              f"- State files:       `{t}.state.json` in each",
              f"- Full transcripts:  `cells/<profile>/{t}/{t}.log`"]
    return "\n".join(lines)


def _trade_synthesis_md(run_id: str, screener_run: Optional[str], ledger: list[dict],
                         picks: list[dict], vetoed: list[dict], snapshot_date: str) -> str:
    source_pipeline_line = (
        f"**Source pipeline**: `runs/{screener_run}/` → top-25 candidates → dual-frame matrix (aggressive ∧ conservative)."
        if screener_run
        else "**Source pipeline**: ad-hoc matrix (no screener lineage) → dual-frame matrix (aggressive ∧ conservative)."
    )
    lines = [
        f"# Trade Synthesis · {run_id}",
        "",
        source_pipeline_line,
        "",
        f"**Snapshot date**: {snapshot_date}.  **Picks**: {len(picks)} of {len(ledger)}.  **Vetoed**: {len(vetoed)}.",
        "",
        "**Filter**: Aggressive rating ∈ {Overweight, Buy} AND Conservative rating ∉ {Underweight, Sell}.  ",
        "**Convention**: 'Hold' on conservative passes the filter (it's the strongest signal for staged entries).",
        "",
        "## Combined ranking — picks by aggressive-frame upside",
        "",
        "| Rank | Ticker | Name | Current | Aggr Rating | Aggr PT | Aggr Upside | Cons Rating | Cons PT | Cons Upside | Compression | Horizon |",
        "|---:|:--|:--|---:|:--|---:|---:|:--|---:|---:|---:|:--|",
    ]
    for r in picks:
        comp = (str(r["pt_compression_pct"]) + "%") if r["pt_compression_pct"] is not None else "—"
        lines.append(
            f"| {r['rank']} | **{r['ticker']}** | {r['name']} | {_fmt_price(r['current_price_usd'])} | "
            f"{r['aggressive_rating']} | {_fmt_price(r['aggressive_pt'])} | {_fmt_pct(r['aggressive_upside_pct'])} | "
            f"{r['conservative_rating']} | {_fmt_price(r['conservative_pt'])} | {_fmt_pct(r['conservative_upside_pct'])} | "
            f"{comp} | {r['aggressive_horizon'] or '—'} |"
        )
    lines += ["", "**Compression interpretation**: < 5% = strong dual-frame agreement on price target. **Negative compression** (cons PT > aggr PT) = rarest/strongest conviction signal.", ""]

    aggr_dist: dict[str, int] = {}
    cons_dist: dict[str, int] = {}
    cons_pick_dist: dict[str, int] = {}
    for r in ledger:
        aggr_dist[r["aggressive_rating"]] = aggr_dist.get(r["aggressive_rating"], 0) + 1
        cons_dist[r["conservative_rating"]] = cons_dist.get(r["conservative_rating"], 0) + 1
        if r.get("classification") == "PICK":
            cons_pick_dist[r["conservative_rating"]] = cons_pick_dist.get(r["conservative_rating"], 0) + 1

    def _fmt_dist(d: dict[str, int]) -> str:
        return ", ".join(f"{v} {k}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "—"

    lines += [
        "## Dual-frame asymmetry — what the ratings actually mean here",
        "",
        "**Important**: in this pipeline the conservative frame functions as a **veto gate**, not a conviction amplifier. "
        "Empirically, conservative rarely (if ever) issues Overweight or Buy on names that aggressive flagged — the strongest "
        "rating it gives on a pick is `Hold`. Differentiation among picks therefore comes from *whether* and *how* conservative "
        "engages with the thesis (PT set vs not, PT compression vs aggressive's PT) — not from the rating word itself.",
        "",
        f"- **Aggressive distribution (all {len(ledger)})**: {_fmt_dist(aggr_dist)}",
        f"- **Conservative distribution (all {len(ledger)})**: {_fmt_dist(cons_dist)}",
        f"- **Conservative on PICKS only**: {_fmt_dist(cons_pick_dist)}",
        "",
        "### Picks tiered by conservative engagement",
        "",
        "Within the `Cons=Hold` cohort that all picks share, real signal differentiation comes from the conservative PT:",
        "",
        "| Tier | Criterion | Read | Picks |",
        "|---|---|---|---|",
    ]
    tier1, tier2, tier3 = [], [], []
    for r in picks:
        comp = r["pt_compression_pct"]
        if r["conservative_pt"] is None:
            tier3.append(r["ticker"])
        elif comp is not None and comp < 5.0:
            tier1.append(r["ticker"])
        else:
            tier2.append(r["ticker"])
    lines += [
        f"| **A — Dual-frame agreement** | Cons PT set, compression < 5% (incl. negative) | Both frames model the same target — strongest size-up candidates | {', '.join(f'**{t}**' for t in tier1) or '—'} |",
        f"| **B — Cons engaged but skeptical** | Cons PT set, compression ≥ 5% | Cons sees a thesis but discounts it materially | {', '.join(tier2) or '—'} |",
        f"| **C — Aggressive-only thesis** | Cons did not set a PT | Cons won't block but declines to model — thinner conviction | {', '.join(tier3) or '—'} |",
        "",
        "Practical sizing implication: Tier A names earn full intended exposure; Tier B names use the conservative PT as the "
        "first profit-taking zone; Tier C names stay at starter size until aggressive entry triggers confirm.",
        "",
    ]

    lines += ["## Per-pick trade plans", "",
              "Each pick's verdict, full executive summary (the actionable trade plan), and link to per-ticker file.",
              ""]
    for r in picks:
        t = r["ticker"]
        cur = r["current_price_usd"]
        aggr_summary = r.get("aggressive_executive_summary")
        lines += [
            f"### {t} — {r['name']} · current **{_fmt_price(cur)}**",
            "",
            f"- **Aggressive verdict**: {r['aggressive_rating']} · PT {_fmt_price(r['aggressive_pt'])} ({_fmt_pct(r['aggressive_upside_pct'])}) · {r['aggressive_horizon'] or 'horizon n/a'}",
            f"- **Conservative verdict**: {r['conservative_rating']} · PT {_fmt_price(r['conservative_pt'])} ({_fmt_pct(r['conservative_upside_pct'])}) · {r['conservative_horizon'] or 'horizon n/a'}",
            "",
            "**Aggressive — Executive Summary** (the trade plan):",
            "",
            (aggr_summary or "_(not extracted)_"),
            "",
            f"→ Full per-ticker file: [`per_ticker/{t}.md`](per_ticker/{t}.md)",
            "",
        ]

    lines += [
        "## Vetoed names — full ledger",
        "",
        "These names cleared aggressive (Overweight/Buy) but failed conservative review with Underweight or Sell, or aggressive itself was Underweight/Sell.",
        "",
        "| Rank | Ticker | Name | Current | Aggr | Aggr PT | Cons | Cons PT |",
        "|---:|:--|:--|---:|:--|---:|:--|---:|",
    ]
    for r in vetoed:
        lines.append(
            f"| {r['rank']} | {r['ticker']} | {r['name']} | {_fmt_price(r['current_price_usd'])} | "
            f"{r['aggressive_rating']} | {_fmt_price(r['aggressive_pt'])} | "
            f"**{r['conservative_rating']}** | {_fmt_price(r['conservative_pt'])} |"
        )
    accounting_repro = (
        f"- **This accounting**: `python scripts/build_run_accounting.py --matrix-run <run_dir> --screener-run runs/{screener_run}`."
        if screener_run
        else "- **This accounting**: `python scripts/build_run_accounting.py --matrix-run <run_dir>` (no screener lineage; pass `--screener-run runs/<screener_dir>` to attribute one)."
    )
    lines += [
        "",
        "## Methodology / re-run",
        "",
        "- **Screener**: `.venv/bin/python run_screener.py --top 25` (with optional `--min-mcap`/`--max-mcap` for cap-band targeting).",
        "- **Matrix orchestrator**: `.venv/bin/python run_copilot_matrix.py --tickers <list> --parallel 5 --stop-on-overweight 0`.",
        accounting_repro,
        "- **Resume**: re-running matrix with the same `--run-id` skips cells with valid `<T>.state.json`.",
        "- **Single-cell retry**: `rm -rf <run>/cells/<profile>/<T>/` then re-launch the subrunner with the same `--run-id`.",
        "",
    ]
    return "\n".join(lines)


def _readme_md(run_id: str, screener_run: Optional[str], picks: list[dict], vetoed: list[dict],
               snapshot_date: str, n_total: int) -> str:
    pick_tickers = " · ".join(r["ticker"] for r in picks)
    vetoed_tickers = " · ".join(r["ticker"] for r in vetoed)
    sells = [r["ticker"] for r in vetoed if r["conservative_rating"] == "Sell" or r["aggressive_rating"] == "Sell"]
    sells_note = f"\n- **Outright Sell**: {', '.join(sells)}" if sells else ""
    source_blurb = (
        f"Two-stage (aggressive ∧ conservative) verdict matrix on the top-{n_total} candidates from `runs/{screener_run}/`."
        if screener_run
        else f"Two-stage (aggressive ∧ conservative) verdict matrix on a {n_total}-ticker ad-hoc list (no screener lineage)."
    )
    candidate_set_row = (
        f'| "What\'s in the source candidate set?" | `runs/{screener_run}/screener.json` |'
        if screener_run
        else '| "What\'s in the source candidate set?" | (none — ad-hoc matrix) |'
    )
    rebuild_block = (
        f"# Re-build this accounting package\npython scripts/build_run_accounting.py \\\n"
        f"  --matrix-run runs/{run_id} \\\n"
        f"  --screener-run runs/{screener_run}"
        if screener_run
        else f"# Re-build this accounting package\npython scripts/build_run_accounting.py \\\n"
             f"  --matrix-run runs/{run_id}"
    )
    return f"""# {run_id} — Run Index

{source_blurb}

## TL;DR — read these first

1. **[`trade_synthesis.md`](trade_synthesis.md)** — combined trade-plan synthesis for all {len(picks)} picks with current prices, full executive summaries, and links into per-ticker files.
2. **[`high_conviction.md`](high_conviction.md)** — picks list (orchestrator-native).
3. **[`verdict_ledger.csv`](verdict_ledger.csv)** / **[`verdict_ledger.json`](verdict_ledger.json)** — flat {n_total}-row ledger with classification, ratings, PTs, current price, upside, PT compression.

## Result snapshot ({snapshot_date})

- **{len(picks)} PICKs**: {pick_tickers}
- **{len(vetoed)} VETOED**: {vetoed_tickers}{sells_note}

## Folder layout

```
{run_id}/
├── README.md                   ← this file
├── trade_synthesis.md          ← master synthesis with current prices
├── high_conviction.md          ← orchestrator picks list
├── matrix.md / matrix.json     ← raw orchestrator grid
├── verdict_ledger.csv          ← flat ledger
├── verdict_ledger.json         ← same, structured
├── current_prices.json         ← Polygon snapshot
├── per_ticker/<T>.md           ← full per-ticker thesis
├── cells/aggressive/<T>/       ← raw cell artifacts (state.json, log, ...)
├── cells/conservative/<T>/     ← raw cell artifacts
├── events.jsonl, launcher.log  ← orchestrator activity
└── manifest.json               ← run config
```

## How to consume

| Question | Where to look |
|---|---|
| "What should I buy and at what entry?" | `trade_synthesis.md` |
| "Why was X vetoed?" | `per_ticker/X.md` (Conservative — Investment Thesis section) |
| "What was the full LLM transcript?" | `cells/<profile>/<T>/<T>.log` |
| "What rating + PT did each frame return?" | `verdict_ledger.csv` |
| "What's the current price for X?" | `current_prices.json` |
{candidate_set_row}

## Reproduce

```bash
# Single-ticker retry (force regeneration)
rm -rf {run_id}/cells/conservative/<TICKER>
.venv/bin/python run_copilot_aggressive_aligned.py \\
  --risk-profile conservative \\
  --run-id {run_id}/cells/conservative/<TICKER> \\
  --no-dashboard <TICKER>

# Full matrix re-run (same run-id resumes from cached state.json)
.venv/bin/python run_copilot_matrix.py \\
  --run-id {run_id} \\
  --tickers <space-separated list> \\
  --parallel 5 --stop-on-overweight 0

{rebuild_block}
```
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--matrix-run", required=True, type=pathlib.Path,
                   help="Path to matrix run directory (e.g., runs/matrix_2026-04-30_1839_top25)")
    p.add_argument("--screener-run", required=False, default=None, type=pathlib.Path,
                   help=(
                       "Path to source screener directory (e.g., runs/screener_2026-04-30_0955). "
                       "Optional: when omitted, the matrix run is treated as ad-hoc with no "
                       "screener lineage; ledger fields like rank/name/sector/composite_score "
                       "will be missing for tickers not in any screener."
                   ))
    p.add_argument("--refresh-prices", action="store_true",
                   help="Re-fetch all current prices via Polygon (otherwise reuse current_prices.json if present)")
    p.add_argument("--snapshot-date", default=None,
                   help="Override the snapshot date label (default: today's date)")
    args = p.parse_args()

    matrix_run = args.matrix_run.resolve()
    screener_run: Optional[pathlib.Path] = (
        args.screener_run.resolve() if args.screener_run is not None else None
    )
    if not matrix_run.is_dir():
        print(f"ERROR: matrix run dir not found: {matrix_run}", file=sys.stderr)
        return 2
    if screener_run is not None and not screener_run.is_dir():
        print(f"ERROR: screener run dir not found: {screener_run}", file=sys.stderr)
        return 2

    if screener_run is not None:
        screener_json_path = screener_run / "screener.json"
        screener_doc = json.loads(screener_json_path.read_text())
        candidates = {c["ticker"]: c for c in screener_doc["candidates"]}
        screener_label = screener_run.name
    else:
        candidates = {}
        screener_label = None

    aggressive_dir = matrix_run / "cells" / "aggressive"
    if not aggressive_dir.is_dir():
        print(f"ERROR: no cells/aggressive/ in {matrix_run}", file=sys.stderr)
        return 2
    tickers = sorted(p.name for p in aggressive_dir.iterdir() if p.is_dir())
    print(f"[accounting] matrix: {matrix_run.name} · screener: {screener_label or '(none — ad-hoc matrix)'} · tickers: {len(tickers)}")

    # --- Prices ---
    prices_path = matrix_run / "current_prices.json"
    prices: dict[str, float] = {}
    if prices_path.exists() and not args.refresh_prices:
        existing = json.loads(prices_path.read_text())
        prices = existing.get("prices_usd", {}) or {}
    missing = [t for t in tickers if t not in prices]
    if missing:
        print(f"[accounting] fetching prices for {len(missing)} tickers from Polygon...")
        fetched = _fetch_prices(missing)
        prices.update(fetched)
        prices_path.write_text(json.dumps({
            "snapshot_taken": datetime.now().isoformat(),
            "source": "polygon /v2/aggs/ticker/{T}/prev",
            "prices_usd": dict(sorted(prices.items())),
        }, indent=2))

    snapshot_date = args.snapshot_date or datetime.now().strftime("%Y-%m-%d")

    # --- Ledger + per_ticker ---
    per_ticker_dir = matrix_run / "per_ticker"
    per_ticker_dir.mkdir(exist_ok=True)
    ledger: list[dict] = []
    for t in tickers:
        aggr = _parse_state(matrix_run / "cells" / "aggressive" / t / f"{t}.state.json")
        cons = _parse_state(matrix_run / "cells" / "conservative" / t / f"{t}.state.json")
        cand = candidates.get(t, {})
        cur = prices.get(t)
        aggr_rating = aggr["rating"] if aggr else "ERROR"
        cons_rating = cons["rating"] if cons else "ERROR"
        aggr_pt = aggr["price_target"] if aggr else None
        cons_pt = cons["price_target"] if cons else None
        classification = _classify(aggr_rating, cons_rating)
        upside = round((aggr_pt - cur) / cur * 100, 1) if (aggr_pt and cur) else None
        cons_upside = round((cons_pt - cur) / cur * 100, 1) if (cons_pt and cur) else None
        compression = round((aggr_pt - cons_pt) / aggr_pt * 100, 1) if (aggr_pt and cons_pt) else None
        row = {
            "rank": cand.get("rank"), "ticker": t, "name": cand.get("name"),
            "sector_sic": cand.get("sector_sic"),
            "market_cap_usd": cand.get("market_cap"),
            "composite_score": cand.get("composite_score"),
            "current_price_usd": cur,
            "aggressive_rating": aggr_rating, "aggressive_pt": aggr_pt, "aggressive_upside_pct": upside,
            "conservative_rating": cons_rating, "conservative_pt": cons_pt, "conservative_upside_pct": cons_upside,
            "pt_compression_pct": compression,
            "classification": classification,
            "aggressive_horizon": aggr["horizon"] if aggr else None,
            "conservative_horizon": cons["horizon"] if cons else None,
            "aggressive_executive_summary": aggr["executive_summary"] if aggr else None,
        }
        ledger.append(row)
        (per_ticker_dir / f"{t}.md").write_text(_per_ticker_md(t, cand, cur, aggr, cons, classification))

    # --- CSV (slim) + JSON (full) ---
    slim_keys = [k for k in ledger[0].keys() if k != "aggressive_executive_summary"]
    with open(matrix_run / "verdict_ledger.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=slim_keys)
        w.writeheader()
        w.writerows([{k: r.get(k) for k in slim_keys} for r in ledger])
    (matrix_run / "verdict_ledger.json").write_text(json.dumps({
        "run_id": matrix_run.name,
        "screener_run": screener_label,
        "generated_at": datetime.now().isoformat(),
        "snapshot_date": snapshot_date,
        "n_tickers": len(ledger),
        "n_picks": sum(1 for r in ledger if r["classification"] == "PICK"),
        "n_vetoed": sum(1 for r in ledger if r["classification"].startswith("VETOED")),
        "rows": ledger,
    }, indent=2))

    picks = sorted(
        [r for r in ledger if r["classification"] == "PICK"],
        key=lambda r: -(r["aggressive_upside_pct"] if r["aggressive_upside_pct"] is not None else -1e9),
    )
    vetoed = sorted([r for r in ledger if r["classification"].startswith("VETOED")],
                    key=lambda r: r["rank"] or 999)

    (matrix_run / "trade_synthesis.md").write_text(
        _trade_synthesis_md(matrix_run.name, screener_label, ledger, picks, vetoed, snapshot_date)
    )
    (matrix_run / "README.md").write_text(
        _readme_md(matrix_run.name, screener_label, picks, vetoed, snapshot_date, len(ledger))
    )

    print()
    print(f"  ✅ verdict_ledger.csv  ({len(ledger)} rows)")
    print(f"  ✅ verdict_ledger.json")
    print(f"  ✅ per_ticker/         ({len(tickers)} files)")
    print(f"  ✅ trade_synthesis.md")
    print(f"  ✅ README.md")
    print(f"  ✅ current_prices.json ({len(prices)}/{len(tickers)} tickers priced)")
    print()
    print(f"  PICKs ({len(picks)}): {[r['ticker'] for r in picks]}")
    print(f"  VETOED ({len(vetoed)}): {[r['ticker'] for r in vetoed]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
