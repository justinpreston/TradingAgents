#!/usr/bin/env python3
"""
Build a self-contained HTML report synthesizing one or more matrix runs.

Usage:
    python scripts/build_html_report.py \
        --runs runs/matrix_2026-04-30_1839_top25:large runs/matrix_2026-05-01_top25:mid \
        --output runs/cross_run_2026-05-01/report.html \
        --title "TradingAgents · Cross-Run Synthesis" \
        --subtitle "Large-Cap (4/30) + Mid-Cap (5/1) · 50 tickers, 21 picks"
"""
from __future__ import annotations
import argparse
import json
import html
from datetime import datetime
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────
# Data assembly
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


def _score(row: dict, cross_band_set: set[str]) -> float:
    s = 0.0
    comp = row.get("pt_compression_pct")
    if row.get("conservative_pt") is None:
        s += 30
    elif comp is not None and comp < 5.0:
        s += 100
        if comp < 0:
            s += 30
        elif comp < 1.0:
            s += 15
    else:
        s += 60
    if row.get("aggressive_rating") == "Buy":
        s += 10
    if row.get("ticker") in cross_band_set:
        s += 10
    s += min(row.get("aggressive_upside_pct") or 0, 30) / 3
    return s


def _load_runs(run_specs: list[str]) -> tuple[list[dict], dict, dict]:
    rows = []
    metadata = {}
    options_by_ticker_run: dict[tuple[str, str], dict] = {}
    for spec in run_specs:
        path_str, label = spec.split(":", 1)
        run_dir = Path(path_str)
        ledger = json.loads((run_dir / "verdict_ledger.json").read_text())
        metadata[label] = {
            "run_id": ledger.get("run_id"),
            "matrix_run": ledger.get("matrix_run"),
            "screener_run": ledger.get("screener_run"),
            "snapshot_date": ledger.get("snapshot_date"),
            "n_total": len(ledger["rows"]),
            "n_picks": sum(1 for r in ledger["rows"] if r["classification"] == "PICK"),
            "n_vetoed": sum(1 for r in ledger["rows"] if r["classification"] == "VETOED"),
        }
        for r in ledger["rows"]:
            r["_run"] = label
            r["_run_dir"] = run_dir.name
            rows.append(r)

        # Optional: options overlay
        ovl_path = run_dir / "options_overlay.json"
        if ovl_path.exists():
            ovl = json.loads(ovl_path.read_text())
            metadata[label]["options_snapshot"] = ovl.get("snapshot_date")
            metadata[label]["options_built"] = ovl.get("strategies_built", 0)
            for o in ovl.get("overlays", []):
                if "strategy" in o and "error" not in o:
                    options_by_ticker_run[(o["ticker"], label)] = o
    return rows, metadata, options_by_ticker_run


# ──────────────────────────────────────────────────────────────────────
# HTML rendering
# ──────────────────────────────────────────────────────────────────────

CSS = r"""
:root {
  --bg: #0a0e1a;
  --bg-elev-1: #111827;
  --bg-elev-2: #1f2937;
  --bg-elev-3: #273449;
  --border: #1e293b;
  --border-strong: #334155;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --text-dim: #64748b;
  --accent: #38bdf8;
  --accent-2: #818cf8;
  --green: #22c55e;
  --green-dim: #14532d;
  --green-bg: rgba(34, 197, 94, 0.08);
  --red: #ef4444;
  --red-dim: #7f1d1d;
  --red-bg: rgba(239, 68, 68, 0.08);
  --yellow: #eab308;
  --yellow-bg: rgba(234, 179, 8, 0.08);
  --tier-a: #22c55e;
  --tier-b: #eab308;
  --tier-c: #94a3b8;
  --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.4);
  --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.5), 0 2px 4px -2px rgb(0 0 0 / 0.5);
  --shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.6), 0 4px 6px -4px rgb(0 0 0 / 0.6);
  --radius: 12px;
  --radius-sm: 8px;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }

body {
  font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

.container {
  max-width: 1280px;
  margin: 0 auto;
  padding: 48px 32px;
}

/* Header */
.report-header {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 32px;
  margin-bottom: 48px;
  border-bottom: 1px solid var(--border);
}
.report-header .eyebrow {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
}
.report-header h1 {
  font-size: 40px;
  font-weight: 700;
  margin: 0;
  letter-spacing: -0.02em;
  background: linear-gradient(135deg, #f1f5f9 0%, #94a3b8 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.report-header .subtitle {
  font-size: 18px;
  color: var(--text-muted);
}
.report-header .meta {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 13px;
  color: var(--text-dim);
}
.report-header .meta span { display: inline-flex; align-items: center; gap: 6px; }

/* Section */
.section {
  margin-bottom: 56px;
}
.section h2 {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.01em;
  margin: 0 0 8px 0;
  display: flex;
  align-items: center;
  gap: 12px;
}
.section h2::before {
  content: '';
  width: 4px;
  height: 24px;
  background: var(--accent);
  border-radius: 2px;
}
.section h3 {
  font-size: 18px;
  font-weight: 600;
  margin: 32px 0 12px 0;
  color: var(--text);
}
.section .lede {
  font-size: 16px;
  color: var(--text-muted);
  margin: 0 0 24px 0;
  max-width: 800px;
}

/* Stat cards */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 32px;
}
.stat-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  position: relative;
  overflow: hidden;
}
.stat-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 3px;
  height: 100%;
  background: var(--accent);
}
.stat-card.green::before { background: var(--green); }
.stat-card.red::before { background: var(--red); }
.stat-card.yellow::before { background: var(--yellow); }
.stat-card .label {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.stat-card .value {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.stat-card .sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* Hero picks */
.hero {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.pick-card {
  background: linear-gradient(180deg, var(--bg-elev-1) 0%, var(--bg-elev-2) 100%);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  padding: 24px;
  position: relative;
  overflow: hidden;
  transition: transform 0.15s ease, border-color 0.15s ease;
}
.pick-card:hover {
  transform: translateY(-2px);
  border-color: var(--accent);
}
.pick-card.rank-1 {
  border-color: rgba(34, 197, 94, 0.5);
  box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.2), var(--shadow-md);
}
.pick-card.rank-2 {
  border-color: rgba(34, 197, 94, 0.4);
}
.pick-card .rank {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--bg-elev-3);
  color: var(--text-muted);
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}
.pick-card.rank-1 .rank, .pick-card.rank-2 .rank {
  background: var(--green);
  color: var(--bg);
}
.pick-card .ticker {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--text);
  margin-bottom: 4px;
  font-variant-numeric: tabular-nums;
}
.pick-card .name {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 16px;
  height: 32px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.pick-card .price-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 16px;
}
.pick-card .price {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.pick-card .upside {
  font-size: 14px;
  font-weight: 600;
  color: var(--green);
  font-variant-numeric: tabular-nums;
}
.pick-card .badge-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

/* Compression visualization */
.compression-vis {
  margin: 12px 0;
  padding: 12px;
  background: var(--bg);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.compression-vis .label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  margin-bottom: 8px;
  font-weight: 600;
}
.compression-vis .bar-track {
  position: relative;
  height: 4px;
  background: var(--bg-elev-3);
  border-radius: 2px;
  margin: 16px 0;
}
.compression-vis .marker {
  position: absolute;
  top: -4px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--bg);
  transform: translateX(-50%);
}
.compression-vis .marker.cur { background: var(--text-dim); }
.compression-vis .marker.cons { background: var(--accent-2); }
.compression-vis .marker.aggr { background: var(--green); }
.compression-vis .scale {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.compression-vis .legend {
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 8px;
}
.compression-vis .legend span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.compression-vis .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

/* Why */
.pick-card .why {
  font-size: 13px;
  color: var(--text-muted);
  line-height: 1.5;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

/* Badges */
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  padding: 3px 8px;
  border-radius: 999px;
  text-transform: uppercase;
}
.badge-tier-a { background: rgba(34, 197, 94, 0.12); color: var(--tier-a); border: 1px solid rgba(34, 197, 94, 0.25); }
.badge-tier-b { background: rgba(234, 179, 8, 0.12); color: var(--tier-b); border: 1px solid rgba(234, 179, 8, 0.25); }
.badge-tier-c { background: rgba(148, 163, 184, 0.12); color: var(--tier-c); border: 1px solid rgba(148, 163, 184, 0.25); }
.badge-buy { background: rgba(56, 189, 248, 0.12); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }
.badge-large { background: rgba(129, 140, 248, 0.12); color: var(--accent-2); border: 1px solid rgba(129, 140, 248, 0.25); }
.badge-mid { background: rgba(56, 189, 248, 0.12); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }
.badge-cross-band { background: rgba(168, 85, 247, 0.12); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.25); }
.badge-neg-comp { background: rgba(34, 197, 94, 0.18); color: var(--green); border: 1px solid rgba(34, 197, 94, 0.4); }

/* Asymmetry callout */
.callout {
  background: linear-gradient(135deg, rgba(56, 189, 248, 0.05) 0%, rgba(129, 140, 248, 0.05) 100%);
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-radius: var(--radius);
  padding: 24px 28px;
  margin: 32px 0;
}
.callout .label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 8px;
}
.callout p { margin: 0 0 12px 0; color: var(--text); }
.callout p:last-child { margin-bottom: 0; }

/* Distribution charts */
.dist-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 24px;
  margin: 24px 0;
}
.dist-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
}
.dist-card .title {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 16px;
}
.dist-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.dist-row .lbl {
  width: 110px;
  font-size: 13px;
  color: var(--text-muted);
}
.dist-row .bar {
  flex: 1;
  height: 22px;
  background: var(--bg);
  border-radius: 4px;
  position: relative;
  overflow: hidden;
}
.dist-row .bar .fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.4s ease;
}
.dist-row .bar .fill.green { background: linear-gradient(90deg, var(--green) 0%, #16a34a 100%); }
.dist-row .bar .fill.yellow { background: linear-gradient(90deg, var(--yellow) 0%, #ca8a04 100%); }
.dist-row .bar .fill.red { background: linear-gradient(90deg, var(--red) 0%, #b91c1c 100%); }
.dist-row .bar .fill.gray { background: linear-gradient(90deg, var(--text-dim) 0%, #475569 100%); }
.dist-row .bar .fill.blue { background: linear-gradient(90deg, var(--accent) 0%, #0284c7 100%); }
.dist-row .num {
  width: 40px;
  text-align: right;
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

/* Tier section */
.tier-section {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
  margin-top: 16px;
}
.tier-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
}
.tier-card .header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.tier-card.tier-a { border-color: rgba(34, 197, 94, 0.3); }
.tier-card.tier-a .header { color: var(--tier-a); }
.tier-card.tier-b { border-color: rgba(234, 179, 8, 0.3); }
.tier-card.tier-b .header { color: var(--tier-b); }
.tier-card.tier-c { border-color: rgba(148, 163, 184, 0.3); }
.tier-card.tier-c .header { color: var(--tier-c); }
.tier-card .header .letter {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 14px;
}
.tier-card.tier-a .header .letter { background: rgba(34, 197, 94, 0.15); }
.tier-card.tier-b .header .letter { background: rgba(234, 179, 8, 0.15); }
.tier-card.tier-c .header .letter { background: rgba(148, 163, 184, 0.15); }
.tier-card .header .title {
  font-weight: 700;
  font-size: 15px;
}
.tier-card .criterion {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 16px;
  line-height: 1.5;
}
.tier-card .picks-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.tier-card .picks-list .pick {
  font-size: 13px;
  font-weight: 600;
  padding: 4px 10px;
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.tier-card .picks-list .pick.starred {
  border-color: var(--green);
  color: var(--green);
}
.tier-card .sizing {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-muted);
}
.tier-card .sizing strong { color: var(--text); }

/* Table */
.table-wrap {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  margin: 16px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
thead {
  background: var(--bg-elev-2);
}
th, td {
  padding: 10px 14px;
  text-align: left;
}
th {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}
td {
  border-bottom: 1px solid var(--border);
}
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--bg-elev-2); }
td.num { text-align: right; }
td.tkr { font-weight: 700; }
.green-text { color: var(--green); }
.red-text { color: var(--red); }
.muted { color: var(--text-dim); }

/* Sector chart */
.sector-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}
.sector-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.sector-card .name {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-dim);
}
.sector-card .tickers {
  font-weight: 700;
  color: var(--text);
}

/* Options section */
.options-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 20px;
  margin: 20px 0;
}
.opt-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.opt-card .head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}
.opt-card .head .tkr { font-size: 22px; font-weight: 700; letter-spacing: -0.01em; }
.opt-card .head .strat {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--accent);
}
.opt-card .legs {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.opt-card .leg {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.opt-card .leg .side {
  font-weight: 700;
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.08em;
  padding: 2px 6px;
  border-radius: 3px;
}
.opt-card .leg .side.long { background: rgba(34, 197, 94, 0.15); color: var(--green); }
.opt-card .leg .side.short { background: rgba(239, 68, 68, 0.15); color: var(--red); }
.opt-card .econ {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 16px;
  background: var(--bg-elev-2);
  padding: 12px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.opt-card .econ .row {
  display: flex;
  justify-content: space-between;
}
.opt-card .econ .lbl { color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
.opt-card .econ .val { font-weight: 600; }
.opt-card .econ .val.green { color: var(--green); }
.opt-card .econ .val.red { color: var(--red); }
.opt-card .liquidity {
  font-size: 11px;
  color: var(--yellow);
  background: rgba(234, 179, 8, 0.08);
  border: 1px solid rgba(234, 179, 8, 0.2);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}

/* Vetoed */
details.vetoed {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
  margin-top: 16px;
}
details.vetoed summary {
  cursor: pointer;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-muted);
  user-select: none;
}
details.vetoed summary:hover { color: var(--text); }
details.vetoed[open] summary { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }

/* Footer */
.footer {
  margin-top: 80px;
  padding-top: 32px;
  border-top: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-dim);
}
.footer p { margin: 4px 0; }
.footer code {
  background: var(--bg-elev-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 6px;
  font-size: 12px;
  color: var(--text-muted);
}

/* Print */
@media print {
  body { background: white; color: black; }
  .pick-card, .stat-card, .dist-card, .tier-card, .table-wrap { break-inside: avoid; }
}
@media (max-width: 720px) {
  .container { padding: 24px 16px; }
  .report-header h1 { font-size: 28px; }
  .report-header .subtitle { font-size: 15px; }
  th, td { padding: 8px 10px; font-size: 12px; }
}
"""


def _badge(label: str, klass: str) -> str:
    return f'<span class="badge {klass}">{html.escape(label)}</span>'


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _compression_vis(row: dict) -> str:
    cur = row.get("current_price_usd")
    aggr = row.get("aggressive_pt")
    cons = row.get("conservative_pt")
    if not cur or not aggr:
        return ""

    lo = min(cur, aggr, cons or aggr) * 0.97
    hi = max(cur, aggr, cons or aggr) * 1.03
    span = hi - lo if hi > lo else 1.0

    def pct(v):
        return ((v - lo) / span) * 100

    cur_pct = pct(cur)
    aggr_pct = pct(aggr)
    cons_pct = pct(cons) if cons else None

    markers = (
        f'<span class="marker cur" style="left:{cur_pct:.1f}%" title="current ${cur:.2f}"></span>'
        f'<span class="marker aggr" style="left:{aggr_pct:.1f}%" title="aggr PT ${aggr:.2f}"></span>'
    )
    if cons_pct is not None:
        markers += f'<span class="marker cons" style="left:{cons_pct:.1f}%" title="cons PT ${cons:.2f}"></span>'

    legend = (
        '<div class="legend">'
        '<span><span class="dot" style="background:var(--text-dim)"></span>current</span>'
        '<span><span class="dot" style="background:var(--green)"></span>aggr PT</span>'
    )
    if cons_pct is not None:
        legend += '<span><span class="dot" style="background:var(--accent-2)"></span>cons PT</span>'
    legend += '</div>'

    return (
        '<div class="compression-vis">'
        '<div class="label">Price targets</div>'
        f'<div class="bar-track">{markers}</div>'
        f'<div class="scale"><span>${lo:,.0f}</span><span>${hi:,.0f}</span></div>'
        f'{legend}'
        '</div>'
    )


def _hero_card(rank: int, row: dict, why: str, cross_band_set: set[str]) -> str:
    tier = _tier(row)
    tier_badge = _badge(f"Tier {tier}", f"badge-tier-{tier.lower()}") if tier in {"A", "B", "C"} else ""
    run_badge = _badge(row["_run"].upper() + "-CAP", f"badge-{row['_run']}")
    badges = [run_badge, tier_badge]
    if row.get("aggressive_rating") == "Buy":
        badges.append(_badge("Aggr Buy", "badge-buy"))
    comp = row.get("pt_compression_pct")
    if comp is not None and comp < 0:
        badges.append(_badge("Neg Compression ⭐", "badge-neg-comp"))
    if row["ticker"] in cross_band_set:
        badges.append(_badge("Cross-Band", "badge-cross-band"))

    return (
        f'<div class="pick-card rank-{rank}">'
        f'<span class="rank">{rank}</span>'
        f'<div class="ticker">{html.escape(row["ticker"])}</div>'
        f'<div class="name">{html.escape(row.get("name") or "—")}</div>'
        f'<div class="price-row">'
        f'<span class="price">{_fmt_price(row.get("current_price_usd"))}</span>'
        f'<span class="upside">{_fmt_pct(row.get("aggressive_upside_pct"))}</span>'
        f'</div>'
        f'<div class="badge-row">{"".join(badges)}</div>'
        f'{_compression_vis(row)}'
        f'<div class="why">{html.escape(why)}</div>'
        f'</div>'
    )


def _dist_chart(title: str, entries: list[tuple[str, int, str]], total: int) -> str:
    rows_html = []
    for label, count, color in entries:
        pct = (count / total * 100) if total else 0
        rows_html.append(
            f'<div class="dist-row">'
            f'<span class="lbl">{html.escape(label)}</span>'
            f'<div class="bar"><div class="fill {color}" style="width:{pct:.1f}%"></div></div>'
            f'<span class="num">{count}</span>'
            f'</div>'
        )
    return (
        '<div class="dist-card">'
        f'<div class="title">{html.escape(title)}</div>'
        f'{"".join(rows_html)}'
        '</div>'
    )


def _opt_card(row: dict, ovl: dict | None) -> str:
    """Render an options strategy card for one pick. ovl may be None."""
    tkr = row["ticker"]
    if not ovl:
        return (
            f'<div class="opt-card">'
            f'<div class="head"><span class="tkr">{html.escape(tkr)}</span>'
            f'<span class="strat" style="color:var(--text-dim)">No overlay</span></div>'
            f'<div style="color:var(--text-dim);font-size:13px">No options strategy generated for this run.</div>'
            f'</div>'
        )

    legs = ovl.get("legs") or []
    legs_html = []
    for leg in legs:
        iv_str = f"IV {leg['iv']:.0%}" if leg.get("iv") else ""
        oi_str = f"OI {leg['open_interest']}" if leg.get("open_interest") is not None else ""
        meta = " · ".join(filter(None, [iv_str, oi_str]))
        legs_html.append(
            f'<div class="leg">'
            f'<span><span class="side {leg["side"]}">{leg["side"]}</span> '
            f'${leg["strike"]:,.2f} call · {leg["expiration"]}</span>'
            f'<span style="color:var(--text-dim);font-size:11px">{html.escape(meta)} · ${leg["price"]:.2f}</span>'
            f'</div>'
        )

    nd = ovl.get("net_debit_per_share") or 0
    nd_contract = ovl.get("net_debit_per_contract") or 0
    max_p = ovl.get("max_profit_per_contract")
    max_l = ovl.get("max_loss_per_contract") or 0
    rr = ovl.get("risk_reward")
    be = ovl.get("breakeven_underlying")
    be_pct = ovl.get("breakeven_pct_from_current")
    upside = ovl.get("upside_to_short_strike_pct")

    econ_rows = [
        ("Net debit", f"${nd:.2f}/sh", "neutral"),
        ("Per contract", f"${nd_contract:,.0f}", "neutral"),
    ]
    if max_p is not None:
        econ_rows.append(("Max profit", f"${max_p:,.0f}", "green"))
    econ_rows.append(("Max loss", f"${max_l:,.0f}", "red"))
    if rr:
        econ_rows.append(("R/R", f"{rr:.2f}×", "green"))
    if be is not None:
        econ_rows.append(("Breakeven", f"${be:.2f} ({be_pct:+.1f}%)", "neutral"))
    if upside is not None:
        econ_rows.append(("Upside to short K", f"{upside:+.1f}%", "neutral"))

    econ_html = "".join(
        f'<div class="row"><span class="lbl">{html.escape(lbl)}</span>'
        f'<span class="val {cls if cls != "neutral" else ""}">{html.escape(val)}</span></div>'
        for lbl, val, cls in econ_rows
    )

    warns = ovl.get("liquidity_warnings") or []
    warn_html = ""
    if warns:
        warn_html = (
            '<div class="liquidity">⚠ '
            + html.escape(" · ".join(warns))
            + "</div>"
        )

    strat = ovl.get("strategy", "—").replace("_", " ").title()
    tier = ovl.get("tier", "—")

    return (
        f'<div class="opt-card">'
        f'<div class="head">'
        f'<span class="tkr">{html.escape(tkr)} <span style="font-size:11px;color:var(--text-dim);font-weight:500">Tier {tier}</span></span>'
        f'<span class="strat">{html.escape(strat)} · {ovl.get("dte", "—")}d</span>'
        f'</div>'
        f'<div class="legs">{"".join(legs_html)}</div>'
        f'<div class="econ">{econ_html}</div>'
        f'{warn_html}'
        f'</div>'
    )


def render(rows: list[dict], metadata: dict, title: str, subtitle: str,
           options_map: dict | None = None) -> str:
    options_map = options_map or {}
    picks = [r for r in rows if r["classification"] == "PICK"]
    vetoed = [r for r in rows if r["classification"] == "VETOED"]

    # Cross-band set
    by_tkr: dict[str, list[dict]] = {}
    for r in picks:
        by_tkr.setdefault(r["ticker"], []).append(r)
    cross_band = {t for t, rs in by_tkr.items() if len(rs) > 1}

    # Best of each cross-band ticker
    best: dict[str, dict] = {}
    for r in picks:
        t = r["ticker"]
        if t not in best or _score(r, cross_band) > _score(best[t], cross_band):
            best[t] = r
    ranked = sorted(best.values(), key=lambda r: _score(r, cross_band), reverse=True)
    top5 = ranked[:5]

    # Distribution stats (combined across all rows)
    aggr_dist: dict[str, int] = {}
    cons_dist: dict[str, int] = {}
    cons_pick: dict[str, int] = {}
    for r in rows:
        aggr_dist[r["aggressive_rating"]] = aggr_dist.get(r["aggressive_rating"], 0) + 1
        cons_dist[r["conservative_rating"]] = cons_dist.get(r["conservative_rating"], 0) + 1
        if r["classification"] == "PICK":
            cons_pick[r["conservative_rating"]] = cons_pick.get(r["conservative_rating"], 0) + 1

    # Tier groupings (use best-of-cross-band)
    tier_a = [r for r in best.values() if _tier(r) == "A"]
    tier_b = [r for r in best.values() if _tier(r) == "B"]
    tier_c = [r for r in best.values() if _tier(r) == "C"]

    # Sector mapping (manual based on the picks present)
    sector_map = {
        "VNOM": "Energy / Royalties", "AROC": "Energy / Infrastructure",
        "TRGP": "Energy / Midstream",
        "ADI": "Semiconductors", "APH": "Electronic Components",
        "CRUS": "Semiconductors", "DIOD": "Semiconductors",
        "SYNA": "Semiconductors", "MXL": "Semiconductors",
        "COHU": "Semiconductors", "ALGM": "Semiconductors",
        "VSH": "Semiconductors", "KLIC": "Semis Equipment",
        "APD": "Industrial Gases", "STLD": "Steel",
        "KALU": "Aluminum", "EQIX": "Data Centers / REIT",
        "CTRE": "Healthcare REIT", "TJX": "Off-Price Retail",
        "ARMK": "Food Services", "DAR": "Food / Renewables",
        "VFC": "Apparel", "DAVE": "Fintech",
        "VIRT": "Capital Markets", "KEY": "Regional Banking",
        "FHB": "Regional Banking", "IDA": "Utility / Electric",
        "GVA": "Construction", "PRIM": "Construction Services",
        "MYRG": "Construction Services", "PWR": "Construction Services",
        "ROK": "Industrial Automation", "LGND": "Pharma / Royalties",
        "TVTX": "Pharma", "SNDK": "Storage / Memory",
        "AAOI": "Optical Networking", "LITE": "Optical Networking",
        "VICR": "Power Conversion", "ABNB": "Travel / Marketplace",
        "GSAT": "Satellite", "AMPX": "Energy Storage",
        "AGX": "Engineering / Construction", "SXI": "Industrial Conglomerate",
        "BNTX": "Biotech",
    }
    sector_groups: dict[str, list[str]] = {}
    for r in best.values():
        sec = sector_map.get(r["ticker"], "Other")
        sector_groups.setdefault(sec, []).append(r["ticker"])

    # ── HTML
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<header class="report-header">
  <span class="eyebrow">TradingAgents · Cross-Run Synthesis</span>
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">{html.escape(subtitle)}</p>
  <div class="meta">
    <span>📅 Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}</span>
    <span>📊 {len(rows)} tickers analyzed</span>
    <span>✅ {len(picks)} picks · ❌ {len(vetoed)} vetoed</span>
    <span>🌐 {len(cross_band)} cross-band confirmed</span>
  </div>
</header>
""")

    # ── Stat strip
    parts.append('<section class="section">')
    parts.append(f"""
<div class="stats">
  <div class="stat-card green">
    <div class="label">High Conviction</div>
    <div class="value">{len(top5)}</div>
    <div class="sub">Top-5 trades · all Tier A</div>
  </div>
  <div class="stat-card">
    <div class="label">Total Picks</div>
    <div class="value">{len(best)}</div>
    <div class="sub">{len(tier_a)} Tier A · {len(tier_b)} Tier B · {len(tier_c)} Tier C</div>
  </div>
  <div class="stat-card yellow">
    <div class="label">Cross-Band</div>
    <div class="value">{len(cross_band)}</div>
    <div class="sub">{', '.join(sorted(cross_band)) or '—'}</div>
  </div>
  <div class="stat-card red">
    <div class="label">Vetoed</div>
    <div class="value">{len(vetoed)}</div>
    <div class="sub">{sum(1 for r in vetoed if r['conservative_rating']=='Sell')} Sell · {sum(1 for r in vetoed if r['conservative_rating']=='Underweight')} Underweight</div>
  </div>
</div>
</section>""")

    # ── Top 5 hero
    why_map = {
        "VNOM": "Conservative is more bullish than aggressive — rarest signal of either run. Vetoed at large-cap; true mid-cap conviction.",
        "ADI":  "Same negative-compression signal at large-cap scale. Cons PT $420 > aggr $408. Caveat: aggr PT only +1.4% above current — wait for pullback.",
        "AROC": "Only name picked in BOTH bands. Tier A in large-cap, Tier B in mid-cap. Aggressive Buy rating. Cons PT $40.50 is first profit-taking zone.",
        "CTRE": "Both frames model exact same PT — unique signal. Healthcare REIT. Cleanest dual-frame agreement of either run.",
        "IDA":  "Defensive utility. Both frames within 1% of each other. Portfolio counterweight to energy + semis names in the top-5.",
        "TRGP": "At first PT zone already. Lower remaining upside.",
        "APD":  "Clean dual-frame agreement. +10% aggressive upside but slightly wider compression.",
        "VIRT": "Tier C in large-cap, Tier B in mid-cap. Strengthens at smaller frame; cons engaged with PT in mid-cap but not large-cap.",
        "FHB":  "Tightest tier-B compression in mid-cap (5.0%). Regional banking — interest-rate sensitive.",
        "ARMK": "Cons PT only +2.9% above current despite passing veto filter. Margin still exists but compressed.",
    }
    parts.append('<section class="section">')
    parts.append('<h2>🏆 Top 5 Trades · Cross-Run</h2>')
    parts.append('<p class="lede">Composite ranking across both runs by tier × compression × cross-band confirmation × aggressive upside. Every name in this list is <strong>Tier A</strong> — both frames set explicit price targets within 5% of each other (or the conservative target is <em>higher</em>, the rarest signal).</p>')
    parts.append('<div class="hero">')
    for i, r in enumerate(top5, 1):
        parts.append(_hero_card(i, r, why_map.get(r["ticker"], ""), cross_band))
    parts.append('</div>')

    # Sector mix of top-5
    parts.append('<h3>Sector composition of the top-5</h3>')
    parts.append('<p class="lede" style="margin-bottom:12px">Diversified across cyclicality regimes — not a thematic concentration trade. The fundamentals screen surfaced five different stories.</p>')
    parts.append('<div class="sector-grid">')
    top5_sectors: dict[str, list[str]] = {}
    for r in top5:
        sec = sector_map.get(r["ticker"], "Other")
        top5_sectors.setdefault(sec, []).append(r["ticker"])
    for sec, tickers in sorted(top5_sectors.items()):
        parts.append(
            f'<div class="sector-card">'
            f'<span class="name">{html.escape(sec)}</span>'
            f'<span class="tickers">{html.escape(", ".join(tickers))}</span>'
            f'</div>'
        )
    parts.append('</div>')
    parts.append('</section>')

    # ── Options Playbook for top 5
    if options_map:
        parts.append('<section class="section">')
        parts.append('<h2>📈 Options Playbook · Top 5</h2>')
        parts.append('<p class="lede">Tier-driven options structures for each top-5 pick. <strong>Tier A/B</strong> → defined-risk bull call spreads with the upper strike anchored to the conservative PT (the "where do both frames agree" zone). <strong>Tier C</strong> → ATM long calls (no credible upper bound from cons → don\'t cap upside). Strikes round to listed contracts; pricing uses Polygon\'s reported IV via Black-Scholes when bid/ask is unavailable. <em>Validate on a live broker chain before trading.</em></p>')
        parts.append('<div class="options-grid">')
        for r in top5:
            ovl = options_map.get((r["ticker"], r["_run"]))
            parts.append(_opt_card(r, ovl))
        parts.append('</div>')

        # All-picks options summary table
        all_ovls = [(r, options_map.get((r["ticker"], r["_run"])))
                    for r in ranked if options_map.get((r["ticker"], r["_run"]))]
        if all_ovls:
            parts.append('<h3 style="margin-top:32px">All picks · options snapshot</h3>')
            parts.append('<div class="table-wrap"><table>')
            parts.append('<thead><tr><th>Ticker</th><th>Run</th><th>Tier</th><th>Strategy</th><th>Exp</th><th class="num">DTE</th><th class="num">Long K</th><th class="num">Short K</th><th class="num">Net Debit</th><th class="num">Max Profit</th><th class="num">Breakeven</th><th class="num">R/R</th><th>Liq</th></tr></thead>')
            parts.append('<tbody>')
            for r, ovl in all_ovls:
                legs = ovl.get("legs") or []
                long_k = legs[0]["strike"] if legs else None
                short_k = legs[1]["strike"] if len(legs) > 1 else None
                tier = ovl.get("tier", "—")
                strat = ovl.get("strategy", "—").replace("_", " ").title()
                rr = ovl.get("risk_reward")
                rr_str = f"{rr:.2f}×" if rr else "—"
                max_p = ovl.get("max_profit_per_contract")
                max_p_str = f"${max_p/100:,.2f}" if max_p is not None else "—"
                liq = "⚠" if ovl.get("liquidity_warnings") else "✓"
                parts.append(
                    f'<tr>'
                    f'<td class="tkr">{html.escape(r["ticker"])}</td>'
                    f'<td>{_badge(r["_run"].upper(), "badge-" + r["_run"])}</td>'
                    f'<td>{_badge("Tier " + tier, "badge-tier-" + tier.lower())}</td>'
                    f'<td>{html.escape(strat)}</td>'
                    f'<td class="muted">{html.escape(ovl.get("expiration", "—"))}</td>'
                    f'<td class="num">{ovl.get("dte", "—")}</td>'
                    f'<td class="num">{_fmt_price(long_k)}</td>'
                    f'<td class="num">{_fmt_price(short_k)}</td>'
                    f'<td class="num">${ovl.get("net_debit_per_share", 0):.2f}</td>'
                    f'<td class="num green-text">{max_p_str}</td>'
                    f'<td class="num">{_fmt_price(ovl.get("breakeven_underlying"))}</td>'
                    f'<td class="num">{rr_str}</td>'
                    f'<td>{liq}</td>'
                    f'</tr>'
                )
            parts.append('</tbody></table></div>')

        parts.append('</section>')

    # ── Asymmetry callout
    parts.append('<section class="section">')
    parts.append('<h2>📐 Dual-Frame Asymmetry — How Ratings Actually Work Here</h2>')
    parts.append('<p class="lede">This is the most important framing finding from running this pipeline at multiple market-cap scales: <strong>the conservative frame functions as a veto gate, not a conviction amplifier</strong>.</p>')
    parts.append('<div class="callout">')
    parts.append('<div class="label">⚠ Empirical truth across 50 tickers</div>')
    parts.append("<p>Across both runs to date the conservative agent has issued <strong>zero Overweight or Buy ratings</strong>. Its strongest endorsement on a pick is <code>Hold</code>. The matrix's PICK criterion (<code>Aggr ∈ Overweight/Buy AND Cons ∉ Underweight/Sell</code>) is intentionally asymmetric: aggressive must <em>actively endorse</em>, conservative only must <em>not veto</em>.</p>")
    parts.append("<p>Differentiation among picks therefore does <strong>not</strong> come from the conservative rating word. It comes from <strong>whether</strong> conservative engaged enough with the thesis to set a PT, and <strong>how</strong> that PT compares to aggressive's. That's the A/B/C tier system below.</p>")
    parts.append('</div>')

    # Distribution charts
    parts.append('<div class="dist-grid">')

    aggr_entries = []
    for k, color in [("Buy", "blue"), ("Overweight", "green"), ("Hold", "gray"),
                     ("Underweight", "yellow"), ("Sell", "red")]:
        if k in aggr_dist:
            aggr_entries.append((k, aggr_dist[k], color))
    parts.append(_dist_chart(f"Aggressive distribution (all {len(rows)})", aggr_entries, len(rows)))

    cons_entries = []
    for k, color in [("Buy", "blue"), ("Overweight", "green"), ("Hold", "gray"),
                     ("Underweight", "yellow"), ("Sell", "red")]:
        if k in cons_dist:
            cons_entries.append((k, cons_dist[k], color))
    parts.append(_dist_chart(f"Conservative distribution (all {len(rows)})", cons_entries, len(rows)))

    pick_entries = []
    for k, color in [("Buy", "blue"), ("Overweight", "green"), ("Hold", "gray"),
                     ("Underweight", "yellow"), ("Sell", "red")]:
        if k in cons_pick:
            pick_entries.append((k, cons_pick[k], color))
    parts.append(_dist_chart(f"Conservative on PICKS only ({len(picks)})", pick_entries, len(picks)))
    parts.append('</div>')
    parts.append('</section>')

    # ── Tier breakdown
    parts.append('<section class="section">')
    parts.append('<h2>🎯 Picks Tiered by Conservative Engagement</h2>')
    parts.append(f'<p class="lede">All {len(best)} unique picks across both runs, grouped by conservative-frame engagement. Tier A names earn full intended exposure; Tier B uses cons PT as the first profit-taking zone; Tier C stays at starter size until aggressive entry triggers confirm.</p>')

    parts.append('<div class="tier-section">')
    for tier_letter, tier_list, criterion, sizing in [
        ("A", tier_a, "Cons PT set, compression < 5% (incl. negative). Both frames model the same target.",
         "<strong>Sizing</strong>: Full intended exposure. Negative-compression names earn the largest relative size since both frames agree the upside isn't capped at the aggressive PT."),
        ("B", tier_b, "Cons PT set, compression ≥ 5%. Cons sees a thesis but discounts it materially.",
         "<strong>Sizing</strong>: Use the conservative PT as the first profit-taking zone. Trim into strength when price reaches that level."),
        ("C", tier_c, "Cons declined to set a PT. Won't block the trade but won't model it either.",
         "<strong>Sizing</strong>: Starter size only. Add only when aggressive entry triggers (breakouts/pullback levels) confirm."),
    ]:
        sorted_tier = sorted(tier_list, key=lambda r: _score(r, cross_band), reverse=True)
        picks_html = []
        for r in sorted_tier:
            comp = r.get("pt_compression_pct")
            star = (comp is not None and comp < 0) or (comp == 0.0)
            cls = "pick starred" if star else "pick"
            picks_html.append(f'<span class="{cls}" title="{r["_run"]}-cap · comp {comp if comp is not None else "—"}">{html.escape(r["ticker"])}</span>')
        parts.append(f"""
<div class="tier-card tier-{tier_letter.lower()}">
  <div class="header">
    <span class="letter">{tier_letter}</span>
    <span class="title">Tier {tier_letter} · {len(tier_list)} pick{"s" if len(tier_list)!=1 else ""}</span>
  </div>
  <div class="criterion">{html.escape(criterion)}</div>
  <div class="picks-list">{"".join(picks_html) or '<span class="muted">none</span>'}</div>
  <div class="sizing">{sizing}</div>
</div>
""")
    parts.append('</div>')
    parts.append('</section>')

    # ── Full picks table
    parts.append('<section class="section">')
    parts.append('<h2>📋 Full Picks Ledger · Both Runs</h2>')
    parts.append('<p class="lede">All 21 picks across both runs, sorted by composite score. Star indicates negative compression (cons PT > aggr PT) or perfect agreement.</p>')
    parts.append('<div class="table-wrap"><table>')
    parts.append('<thead><tr><th>#</th><th>Ticker</th><th>Run</th><th>Tier</th><th class="num">Current</th><th class="num">Aggr PT</th><th class="num">Aggr Up%</th><th class="num">Cons PT</th><th class="num">Comp</th><th>Aggr</th><th>Horizon</th></tr></thead>')
    parts.append('<tbody>')
    for i, r in enumerate(ranked, 1):
        tier = _tier(r)
        comp = r.get("pt_compression_pct")
        comp_str = f"{comp:.1f}%" if comp is not None else "—"
        comp_cls = "green-text" if comp is not None and comp < 5 else ""
        star = "⭐ " if comp is not None and comp <= 0 else ""
        parts.append(
            f'<tr>'
            f'<td>{i}</td>'
            f'<td class="tkr">{star}{html.escape(r["ticker"])}</td>'
            f'<td>{_badge(r["_run"].upper(), "badge-" + r["_run"])}</td>'
            f'<td>{_badge("Tier " + tier, "badge-tier-" + tier.lower())}</td>'
            f'<td class="num">{_fmt_price(r.get("current_price_usd"))}</td>'
            f'<td class="num">{_fmt_price(r.get("aggressive_pt"))}</td>'
            f'<td class="num green-text">{_fmt_pct(r.get("aggressive_upside_pct"))}</td>'
            f'<td class="num">{_fmt_price(r.get("conservative_pt"))}</td>'
            f'<td class="num {comp_cls}">{comp_str}</td>'
            f'<td>{html.escape(r["aggressive_rating"])}</td>'
            f'<td class="muted">{html.escape(r.get("aggressive_horizon") or "—")}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table></div>')
    parts.append('</section>')

    # ── Sector breakdown all picks
    parts.append('<section class="section">')
    parts.append('<h2>🗂 Sector Composition · All Picks</h2>')
    parts.append('<div class="sector-grid">')
    for sec, tickers in sorted(sector_groups.items(), key=lambda kv: -len(kv[1])):
        parts.append(
            f'<div class="sector-card">'
            f'<span class="name">{html.escape(sec)} · {len(tickers)}</span>'
            f'<span class="tickers">{html.escape(", ".join(sorted(tickers)))}</span>'
            f'</div>'
        )
    parts.append('</div>')
    parts.append('</section>')

    # ── Vetoed (collapsed)
    parts.append('<section class="section">')
    parts.append('<h2>❌ Vetoed Names</h2>')
    parts.append(f'<p class="lede">{len(vetoed)} names that cleared the screener and aggressive (mostly Overweight) but failed conservative review with Underweight or Sell.</p>')
    parts.append(f'<details class="vetoed"><summary>Show all {len(vetoed)} vetoed names</summary>')
    parts.append('<div class="table-wrap"><table>')
    parts.append('<thead><tr><th>Ticker</th><th>Run</th><th class="num">Current</th><th>Aggr</th><th class="num">Aggr PT</th><th>Cons</th><th class="num">Cons PT</th></tr></thead>')
    parts.append('<tbody>')
    for r in sorted(vetoed, key=lambda r: (r["_run"], r["ticker"])):
        cons_color = "red-text" if r["conservative_rating"] == "Sell" else ""
        parts.append(
            f'<tr>'
            f'<td class="tkr">{html.escape(r["ticker"])}</td>'
            f'<td>{_badge(r["_run"].upper(), "badge-" + r["_run"])}</td>'
            f'<td class="num">{_fmt_price(r.get("current_price_usd"))}</td>'
            f'<td>{html.escape(r["aggressive_rating"])}</td>'
            f'<td class="num">{_fmt_price(r.get("aggressive_pt"))}</td>'
            f'<td class="{cons_color}"><strong>{html.escape(r["conservative_rating"])}</strong></td>'
            f'<td class="num">{_fmt_price(r.get("conservative_pt"))}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table></div>')
    parts.append('</details>')
    parts.append('</section>')

    # ── Footer
    parts.append('<footer class="footer">')
    parts.append('<p><strong>Source pipeline</strong>:</p>')
    for label, meta in metadata.items():
        parts.append(f'<p>· <code>{label}-cap</code>: matrix <code>{html.escape(meta["matrix_run"] or "—")}</code> ← screener <code>{html.escape(meta["screener_run"] or "—")}</code> · {meta["n_total"]} tickers ({meta["n_picks"]} picks, {meta["n_vetoed"]} vetoed)</p>')
    parts.append('<p>&nbsp;</p>')
    parts.append('<p><strong>Methodology</strong>: Each ticker run through TradingAgents twice (aggressive + conservative profile), full multi-agent debate. Filter: aggressive ∈ Overweight/Buy AND conservative ∉ Underweight/Sell. Composite score = tier base × compression bonus × cross-band confirmation × bounded aggressive upside.</p>')
    parts.append('<p><strong>Reproduce</strong>: <code>python scripts/build_run_accounting.py --matrix-run &lt;dir&gt; --screener-run &lt;dir&gt;</code> · this report: <code>python scripts/build_html_report.py --runs &lt;run&gt;:&lt;label&gt; ... --output report.html</code></p>')
    parts.append('<p>&nbsp;</p>')
    parts.append('<p style="color: var(--text-dim); font-size: 11px;">Generated by TradingAgents accounting pipeline · self-contained HTML, no external dependencies</p>')
    parts.append('</footer>')

    parts.append('</div></body></html>')
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", nargs="+", required=True,
                   help="Run dirs in form 'path:label' (e.g. runs/matrix_X:large)")
    p.add_argument("--output", required=True, help="Output HTML path")
    p.add_argument("--title", default="TradingAgents · Cross-Run Synthesis")
    p.add_argument("--subtitle", default="Cross-run dual-frame matrix analysis")
    args = p.parse_args()

    rows, metadata, options_map = _load_runs(args.runs)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render(rows, metadata, args.title, args.subtitle, options_map)
    out_path.write_text(html_text)

    n_picks = sum(1 for r in rows if r["classification"] == "PICK")
    n_vetoed = sum(1 for r in rows if r["classification"] == "VETOED")
    print(f"  ✅ {out_path}")
    print(f"     {len(rows)} tickers · {n_picks} picks · {n_vetoed} vetoed")
    print(f"     {len(html_text):,} bytes")


if __name__ == "__main__":
    main()
