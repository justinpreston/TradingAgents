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


def _load_runs(run_specs: list[str], allow_missing: bool = True) -> tuple[list[dict], dict, dict, dict, list[str]]:
    rows = []
    metadata = {}
    options_by_ticker_run: dict[tuple[str, str], dict] = {}
    chronos_by_ticker_run: dict[tuple[str, str], dict] = {}
    skipped: list[str] = []
    for spec in run_specs:
        path_str, label = spec.split(":", 1)
        run_dir = Path(path_str)
        ledger_path = run_dir / "verdict_ledger.json"
        if not ledger_path.exists():
            msg = f"{run_dir.name} (label={label}): verdict_ledger.json not found — matrix may still be in-flight or accounting not built"
            if allow_missing:
                skipped.append(msg)
                continue
            raise FileNotFoundError(msg)
        ledger = json.loads(ledger_path.read_text())
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

        # Optional: chronos overlay (price-action-only forecast cross-check)
        chronos_path = run_dir / "chronos_overlay.json"
        if chronos_path.exists():
            chronos = json.loads(chronos_path.read_text())
            metadata[label]["chronos_snapshot"] = chronos.get("snapshot_date")
            metadata[label]["chronos_model"] = chronos.get("model")
            metadata[label]["chronos_horizon_days"] = chronos.get("prediction_length_days")
            for c in chronos.get("overlays", []):
                if "error" not in c:
                    chronos_by_ticker_run[(c["ticker"], label)] = c
    return rows, metadata, options_by_ticker_run, chronos_by_ticker_run, skipped


# ──────────────────────────────────────────────────────────────────────
# Portfolio / Screener / Reconciliation
# ──────────────────────────────────────────────────────────────────────

def _load_portfolio(path: str | None) -> dict | None:
    """Load a versioned portfolio_export.json. Returns None if path is None."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"  ⚠ portfolio export not found: {path}")
        return None
    data = json.loads(p.read_text())
    if data.get("schema_version") != 1:
        print(f"  ⚠ portfolio schema_version={data.get('schema_version')} (expected 1) — rendering best-effort")
    return data


def _load_screener(path: str | None) -> dict | None:
    """Load a screener.json from a runs/screener_* dir."""
    if not path:
        return None
    p = Path(path)
    sjson = p / "screener.json" if p.is_dir() else p
    if not sjson.exists():
        print(f"  ⚠ screener output not found: {sjson}")
        return None
    data = json.loads(sjson.read_text())
    data["_screener_dir"] = p.name if p.is_dir() else p.parent.name
    data["_screener_path"] = str(p if p.is_dir() else p.parent)
    return data


def _load_news_enrichment(screener: dict | None) -> dict[str, dict] | None:
    """Load news_enrichment.json (if present) into a ticker → row dict.

    Looks for ``news_enrichment.json`` next to the screener.json. Silently
    returns ``None`` when missing — enrichment is opt-in, the dashboard
    must render fine without it.
    """
    if not screener:
        return None
    base = screener.get("_screener_path")
    if not base:
        return None
    nef = Path(base) / "news_enrichment.json"
    if not nef.exists():
        return None
    try:
        data = json.loads(nef.read_text())
    except json.JSONDecodeError as exc:
        print(f"  ⚠ news_enrichment.json malformed: {exc}")
        return None
    return {row["ticker"]: row for row in data.get("tickers", [])}


def _load_earnings_calendar(screener: dict | None,
                            extra_dirs: list[Path] | None = None
                            ) -> dict[str, dict] | None:
    """Load earnings_calendar.json from screener dir or matrix run dirs.

    Search order:
    1. ``screener_<id>/earnings_calendar.json`` (next to screener.json)
    2. each path in ``extra_dirs`` (typically the supplied --runs dirs)

    First hit wins. Returns ``{ticker: row}`` mapping. Silently returns
    ``None`` when nothing is found.
    """
    candidates: list[Path] = []
    if screener:
        base = screener.get("_screener_path")
        if base:
            candidates.append(Path(base) / "earnings_calendar.json")
    for d in extra_dirs or []:
        candidates.append(Path(d) / "earnings_calendar.json")
    for cand in candidates:
        if not cand.exists():
            continue
        try:
            data = json.loads(cand.read_text())
        except json.JSONDecodeError as exc:
            print(f"  ⚠ earnings_calendar.json malformed at {cand}: {exc}")
            continue
        return {row["ticker"]: row for row in data.get("tickers", [])
                if row.get("ticker")}
    return None


def _load_macro_snapshot(path: str | None) -> dict | None:
    """Load a macro_<DATE>.json snapshot. Returns ``None`` if missing/malformed."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"  ⚠ macro snapshot not found: {p}")
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        print(f"  ⚠ macro snapshot malformed at {p}: {exc}")
        return None


def _exposure_key(pos: dict) -> str | None:
    """Return the equity ticker key for matching against matrix verdicts.

    Stocks → symbol. Options → underlying. Mutual funds / passive ETFs → None
    (they aggregate too many names to attribute to a single matrix verdict).
    """
    at = pos.get("asset_type")
    if at == "stock":
        return pos.get("symbol")
    if at == "option":
        return pos.get("underlying")
    return None


def _build_matrix_index(rows: list[dict], metadata: dict) -> dict[str, dict]:
    """Map ticker → {row, run_label, snapshot_date, all_runs}.

    Picks the LATEST matrix verdict by snapshot_date when a ticker appears
    in multiple runs — per rubber-duck guidance, freshness wins over
    historical "best".
    """
    by_tkr: dict[str, list[dict]] = {}
    for r in rows:
        by_tkr.setdefault(r["ticker"], []).append(r)

    index: dict[str, dict] = {}
    for tkr, lst in by_tkr.items():
        # Sort by run snapshot_date desc; ties broken by run label
        def _snap(r):
            return metadata.get(r["_run"], {}).get("snapshot_date") or ""
        lst_sorted = sorted(lst, key=_snap, reverse=True)
        latest = lst_sorted[0]
        index[tkr] = {
            "row": latest,
            "run_label": latest["_run"],
            "snapshot_date": _snap(latest),
            "all_runs": [(r["_run"], _snap(r), r["classification"]) for r in lst_sorted],
        }
    return index


def _classify_friction(pos: dict, matrix_entry: dict | None) -> tuple[str, str]:
    """Return (bucket, color) for the position vs latest matrix verdict.

    Buckets:
      held_pick      — held + matrix says PICK (alignment, possibly add)
      held_vetoed    — held + matrix VETOED (friction; reduce/exit review)
      held_no_cover  — held + ticker absent from any matrix run (uncovered)
      passive        — passive fund / ETF (not subject to matrix verdict)
    """
    if _exposure_key(pos) is None:
        return ("passive", "muted")
    if matrix_entry is None:
        return ("held_no_cover", "yellow")
    cls = matrix_entry["row"].get("classification")
    if cls == "PICK":
        return ("held_pick", "green")
    if cls == "VETOED":
        return ("held_vetoed", "red")
    return ("held_no_cover", "yellow")


def _build_reconciliation(portfolio: dict | None, matrix_index: dict[str, dict]) -> dict:
    """Reconcile holdings against the latest matrix verdicts.

    Returns dict with buckets:
      held_pick:      [(position, matrix_entry), ...]
      held_vetoed:    [(position, matrix_entry), ...]  # FRICTION
      held_no_cover:  [(position, None), ...]          # FRICTION (uncoverage)
      passive:        [(position, None), ...]
      pick_not_held:  [matrix_entry, ...]              # OPPORTUNITY
      held_keys:      set of normalized exposure keys
    """
    out = {"held_pick": [], "held_vetoed": [], "held_no_cover": [],
           "passive": [], "pick_not_held": [], "held_keys": set()}
    if not portfolio:
        return out

    held_keys: set[str] = set()
    for pos in portfolio.get("positions", []):
        key = _exposure_key(pos)
        entry = matrix_index.get(key) if key else None
        bucket, _ = _classify_friction(pos, entry)
        if bucket == "passive":
            out["passive"].append((pos, None))
        elif bucket == "held_pick":
            out["held_pick"].append((pos, entry))
            held_keys.add(key)
        elif bucket == "held_vetoed":
            out["held_vetoed"].append((pos, entry))
            held_keys.add(key)
        else:  # held_no_cover
            out["held_no_cover"].append((pos, entry))
            if key:
                held_keys.add(key)

    # PICK but not held — opportunity
    for tkr, entry in matrix_index.items():
        if entry["row"].get("classification") == "PICK" and tkr not in held_keys:
            out["pick_not_held"].append(entry)
    out["held_keys"] = held_keys
    return out


def _summarize_realized(trades: list[dict]) -> dict:
    """Roll up realized P/L from trades that have realized_pl populated."""
    closes = [t for t in trades if t.get("realized_pl") is not None]
    by_underlying: dict[str, dict] = {}
    for t in closes:
        u = t.get("underlying") or t.get("symbol")
        bucket = by_underlying.setdefault(u, {"pl": 0, "trades": []})
        bucket["pl"] += t["realized_pl"]
        bucket["trades"].append(t)
    return {
        "n_closes": len(closes),
        "total_pl": sum(t["realized_pl"] for t in closes),
        "by_underlying": by_underlying,
        "closes": sorted(closes, key=lambda t: (t["trade_date"], t.get("symbol", "")), reverse=True),
    }


# Coarse SIC-prefix → sector rollup. Replaces the ticker-by-ticker dict.
_SIC_SECTORS = [
    ("01", "Agriculture"), ("10", "Mining / Metals"), ("12", "Coal Mining"),
    ("13", "Energy / Oil & Gas"), ("14", "Mining"), ("15", "Construction"),
    ("16", "Heavy Construction"), ("17", "Construction Specialty"),
    ("20", "Food & Beverage"), ("22", "Textiles"), ("23", "Apparel"),
    ("24", "Wood / Lumber"), ("25", "Furniture"), ("26", "Paper / Pulp"),
    ("27", "Printing / Publishing"), ("28", "Chemicals / Pharma"),
    ("29", "Petroleum Refining"), ("30", "Rubber / Plastics"),
    ("32", "Glass / Cement"), ("33", "Primary Metals / Steel"),
    ("34", "Fabricated Metals"), ("35", "Industrial Machinery"),
    ("36", "Electronics / Semiconductors"), ("37", "Transportation Equipment"),
    ("38", "Instruments / Measurement"), ("39", "Misc Manufacturing"),
    ("40", "Railroads"), ("41", "Local Transit"), ("42", "Trucking"),
    ("44", "Water Transportation"), ("45", "Air Transportation"),
    ("47", "Transportation Services"), ("48", "Telecommunications"),
    ("49", "Utilities"), ("50", "Wholesale Durable"), ("51", "Wholesale Nondurable"),
    ("52", "Retail / Building"), ("53", "Retail / General"),
    ("54", "Retail / Food"), ("55", "Retail / Auto"),
    ("56", "Retail / Apparel"), ("57", "Retail / Furniture"),
    ("58", "Retail / Restaurants"), ("59", "Retail / Misc"),
    ("60", "Banks"), ("61", "Credit Institutions"), ("62", "Securities / Brokers"),
    ("63", "Insurance Carriers"), ("64", "Insurance Agents"),
    ("65", "Real Estate / REIT"), ("67", "Holding / Investment"),
    ("70", "Hotels / Lodging"), ("72", "Personal Services"),
    ("73", "Business Services / Software"), ("75", "Auto Services"),
    ("78", "Motion Pictures"), ("79", "Recreation Services"),
    ("80", "Health Services"), ("82", "Educational Services"),
    ("83", "Social Services"), ("87", "Engineering / Accounting"),
    ("99", "Nonclassifiable"),
]


def _sector_from_sic(sic: str | None) -> str:
    if not sic:
        return "Unknown"
    s = str(sic)
    for prefix, name in _SIC_SECTORS:
        if s.startswith(prefix):
            return name
    return "Unknown"


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

/* ── Private banner ──────────────────────────────────────────────── */
.private-banner {
  background: linear-gradient(135deg, rgba(234, 179, 8, 0.12) 0%, rgba(239, 68, 68, 0.08) 100%);
  border: 1px solid rgba(234, 179, 8, 0.35);
  border-radius: var(--radius);
  padding: 14px 20px;
  margin-bottom: 24px;
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: #fde68a;
}
.private-banner .icon { font-size: 18px; }
.private-banner strong { color: #fef3c7; }

/* ── Portfolio dashboard ─────────────────────────────────────────── */
.pf-snapshot {
  font-size: 11px;
  color: var(--text-dim);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.holdings-table-wrap { margin-top: 16px; }
.pos-row {
  border-left: 3px solid transparent;
}
.pos-row.status-open-watch { border-left-color: var(--yellow); }
.pos-row.status-open-hold { border-left-color: var(--accent); }
.pos-row.status-open { border-left-color: var(--green); }
.pos-row td.tkr {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.pos-row .opt-meta {
  font-size: 10px;
  color: var(--text-dim);
  letter-spacing: 0.03em;
  text-transform: uppercase;
  display: block;
  margin-top: 2px;
  font-weight: 500;
}
.pos-row td.notes {
  font-size: 11px;
  color: var(--text-muted);
  max-width: 280px;
  line-height: 1.4;
}
.status-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 6px;
  border-radius: 3px;
}
.status-badge.open { background: rgba(34, 197, 94, 0.15); color: var(--green); }
.status-badge.watch { background: rgba(234, 179, 8, 0.15); color: var(--yellow); }
.status-badge.hold { background: rgba(56, 189, 248, 0.15); color: var(--accent); }
.status-badge.closed { background: rgba(148, 163, 184, 0.15); color: var(--text-dim); }

/* ── Reconciliation / friction cards ─────────────────────────────── */
.reconcile-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
  margin-top: 16px;
}
.recon-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  border-left: 4px solid var(--text-dim);
}
.recon-card.bucket-friction { border-left-color: var(--red); }
.recon-card.bucket-uncovered { border-left-color: var(--yellow); }
.recon-card.bucket-aligned { border-left-color: var(--green); }
.recon-card.bucket-opportunity { border-left-color: var(--accent); }
.recon-card .header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
}
.recon-card .title {
  font-size: 15px;
  font-weight: 700;
}
.recon-card .count {
  font-size: 12px;
  color: var(--text-dim);
  font-weight: 600;
}
.recon-card .desc {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 16px;
  line-height: 1.5;
}
.recon-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 12px;
  padding: 14px;
  background: var(--bg);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.recon-pair:last-child { margin-bottom: 0; }
@media (max-width: 720px) {
  .recon-pair { grid-template-columns: 1fr; }
}
.recon-pair .col {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.recon-pair .col .label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
}
.recon-pair .col .ticker {
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.recon-pair .col .meta {
  font-size: 12px;
  color: var(--text-muted);
}
.recon-pair .col .pl {
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.recon-pair .col .pl.green { color: var(--green); }
.recon-pair .col .pl.red { color: var(--red); }
.recon-pair .col .verdict {
  font-size: 13px;
  color: var(--text);
  line-height: 1.4;
}

/* ── Action matrix ──────────────────────────────────────────────── */
.action-matrix {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin: 16px 0 24px 0;
}
.action-bucket {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 14px;
  border-top: 3px solid var(--text-dim);
}
.action-bucket.green { border-top-color: var(--green); }
.action-bucket.red { border-top-color: var(--red); }
.action-bucket.yellow { border-top-color: var(--yellow); }
.action-bucket.blue { border-top-color: var(--accent); }
.action-bucket .label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.action-bucket .count {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.action-bucket .names {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
}

/* ── Screener watchlist ─────────────────────────────────────────── */
.screener-table-wrap { margin: 16px 0; }
.scr-row td.score {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
.badge-screener-pick { background: rgba(34, 197, 94, 0.18); color: var(--green); border: 1px solid rgba(34, 197, 94, 0.3); }
.badge-screener-vetoed { background: rgba(239, 68, 68, 0.15); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.3); }
.badge-screener-pending { background: rgba(234, 179, 8, 0.15); color: var(--yellow); border: 1px solid rgba(234, 179, 8, 0.3); }
.badge-held { background: rgba(56, 189, 248, 0.15); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3); }
/* News enrichment chips: sentiment polarity + theme tags */
.chip-sent { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 8px; margin-right: 4px; font-weight: 600; }
.chip-sent-pos { background: rgba(34, 197, 94, 0.15); color: var(--green); border: 1px solid rgba(34, 197, 94, 0.25); }
.chip-sent-neg { background: rgba(239, 68, 68, 0.15); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.25); }
.chip-sent-neu { background: rgba(148, 163, 184, 0.12); color: var(--muted); border: 1px solid rgba(148, 163, 184, 0.25); }
.chip-theme { display: inline-block; font-size: 10px; padding: 1px 5px; border-radius: 6px; margin-right: 3px; background: rgba(56, 189, 248, 0.10); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.20); }
.chip-theme-gov { background: rgba(168, 85, 247, 0.15); color: #c084fc; border-color: rgba(168, 85, 247, 0.30); }
.chip-earn { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 6px; margin-right: 3px; font-weight: 600; }
.chip-earn-near { background: rgba(239, 68, 68, 0.18); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.35); }
.chip-earn-mid { background: rgba(234, 179, 8, 0.15); color: var(--yellow); border: 1px solid rgba(234, 179, 8, 0.30); }
.chip-earn-far { background: rgba(148, 163, 184, 0.12); color: var(--muted); border: 1px solid rgba(148, 163, 184, 0.25); }

/* Vol-context chips for the options table */
.vol-chip { display: inline-block; font-size: 12px; padding: 1px 6px; border-radius: 6px; cursor: help; }
.vol-favorable { background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.30); }
.vol-mixed { background: rgba(234, 179, 8, 0.12); border: 1px solid rgba(234, 179, 8, 0.25); }
.vol-unfavorable { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.30); }

/* Chronos forecast chips — agents-vs-model agreement on the persona PT */
.model-chip { display: inline-block; font-size: 12px; padding: 1px 6px; border-radius: 6px; cursor: help; }
.model-agree    { background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.30); }
.model-edge     { background: rgba(234, 179, 8, 0.12); border: 1px solid rgba(234, 179, 8, 0.25); }
.model-disagree { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.30); }

/* ── Realized P/L ───────────────────────────────────────────────── */
.realized-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.real-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 14px;
  border-left: 3px solid var(--text-dim);
}
.real-card.green { border-left-color: var(--green); }
.real-card.red { border-left-color: var(--red); }
.real-card .ticker {
  font-size: 16px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.real-card .pl {
  font-size: 18px;
  font-weight: 700;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
}
.real-card .pl.green { color: var(--green); }
.real-card .pl.red { color: var(--red); }
.real-card .meta {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 2px;
}

.warn-banner {
  background: rgba(234, 179, 8, 0.08);
  border: 1px solid rgba(234, 179, 8, 0.25);
  border-radius: var(--radius-sm);
  padding: 12px 16px;
  margin: 16px 0;
  font-size: 12px;
  color: var(--yellow);
}
.warn-banner code {
  background: var(--bg-elev-2);
  padding: 2px 5px;
  border-radius: 3px;
  color: var(--text-muted);
  font-size: 11px;
}

.status-badge.green { background: rgba(34, 197, 94, 0.15); color: var(--green); }
.status-badge.red { background: rgba(239, 68, 68, 0.15); color: var(--red); }
.status-badge.muted { background: rgba(148, 163, 184, 0.15); color: var(--text-dim); }

.badge-screener-pick {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
}
.badge-screener-vetoed {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
}
.badge-screener-pending {
  background: rgba(148, 163, 184, 0.15);
  color: var(--text-dim);
}
.badge-held {
  background: rgba(56, 189, 248, 0.15);
  color: var(--accent);
  font-weight: 700;
}

.skipped-banner {
  background: rgba(56, 189, 248, 0.05);
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-radius: var(--radius-sm);
  padding: 12px 16px;
  margin: 16px 0;
  font-size: 12px;
  color: var(--text-muted);
}
.skipped-banner ul { margin: 6px 0 0 20px; padding: 0; }
.skipped-banner li { margin: 2px 0; }
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


def _chronos_chip(c: dict | None) -> str:
    """Render a colored chip for the Chronos agent-PT-vs-model agreement.

    Buckets:
      🟢 inside        — agent PT lands in p25-p75 (model & personas agree)
      🟡 high / low    — agent PT lands in p75-p90 / p10-p25 (one-edge inside cone)
      🔴 above / below — agent PT outside the p10-p90 cone (notable disagreement)

    Returns an empty string when ``c`` is None or has no model_view.
    """
    if not c or "error" in c:
        return ""
    view = c.get("model_view")
    if not view:
        return ""
    if view == "inside":
        klass, emoji = "model-agree", "🟢"
    elif view in ("high", "low"):
        klass, emoji = "model-edge", "🟡"
    elif view in ("above cone", "below cone"):
        klass, emoji = "model-disagree", "🔴"
    else:
        klass, emoji = "model-edge", "🟡"

    quantile = c.get("agent_pt_quantile")
    vs_median = c.get("agent_pt_vs_median_pct")
    horizon = c.get("prediction_length_days")
    p50 = (c.get("forecast_at_horizon") or {}).get("p50")
    bits = [f"Chronos: agent PT {view}"]
    if quantile is not None:
        bits.append(f"@ p{int(round(quantile*100))}")
    if vs_median is not None and p50 is not None:
        bits.append(f"({vs_median:+.1f}% vs model p50 ${p50:,.0f})")
    if horizon:
        bits.append(f"horizon {horizon}d")
    title_attr = html.escape(" · ".join(bits))
    return f'<span class="model-chip {klass}" title="{title_attr}">{emoji}</span>'


def _opt_card(row: dict, ovl: dict | None, chronos: dict | None = None) -> str:
    """Render an options strategy card for one pick. ovl may be None.

    If ``chronos`` (a chronos_overlay row dict) is provided, a small Chronos
    agreement chip is appended to the head row so the reader can see at a
    glance whether the persona PT is inside the model's forecast cone.
    """
    tkr = row["ticker"]
    chronos_chip = _chronos_chip(chronos)
    chronos_inline = f' {chronos_chip}' if chronos_chip else ''
    if not ovl:
        return (
            f'<div class="opt-card">'
            f'<div class="head"><span class="tkr">{html.escape(tkr)}{chronos_inline}</span>'
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
        f'<span class="tkr">{html.escape(tkr)} <span style="font-size:11px;color:var(--text-dim);font-weight:500">Tier {tier}</span>{chronos_inline}</span>'
        f'<span class="strat">{html.escape(strat)} · {ovl.get("dte", "—")}d</span>'
        f'</div>'
        f'<div class="legs">{"".join(legs_html)}</div>'
        f'<div class="econ">{econ_html}</div>'
        f'{warn_html}'
        f'</div>'
    )


# ──────────────────────────────────────────────────────────────────────
# Portfolio / reconciliation / screener / realized rendering
# ──────────────────────────────────────────────────────────────────────

def _fmt_money(v, sign: bool = False) -> str:
    if v is None:
        return "—"
    s = f"${abs(v):,.2f}"
    if v < 0:
        return f"-{s}"
    return f"+{s}" if sign else s


def _status_badge(status: str | None) -> str:
    if not status:
        return ""
    s = status.lower()
    if "watch" in s:
        return '<span class="status-badge watch">watch</span>'
    if "hold" in s and s != "open":
        return '<span class="status-badge hold">hold</span>'
    if s == "closed":
        return '<span class="status-badge closed">closed</span>'
    return '<span class="status-badge open">open</span>'


def _option_dte(expiration: str | None, today: str | None) -> int | None:
    if not expiration:
        return None
    try:
        exp = datetime.strptime(expiration, "%Y-%m-%d")
        ref = datetime.strptime(today, "%Y-%m-%dT%H:%M-04:00") if today else datetime.now()
        return (exp - ref).days
    except (ValueError, TypeError):
        return None


def _render_portfolio_dashboard(portfolio: dict) -> str:
    if not portfolio:
        return ""
    acct = portfolio.get("account", {})
    positions = portfolio.get("positions", [])
    snap = acct.get("snapshot_at", "—")
    pos_value = acct.get("positions_value") or 0
    today = acct.get("today_change_dollars") or 0
    today_pct = (today / pos_value * 100) if pos_value else 0
    unreal = acct.get("total_unrealized_dollars") or 0
    cost = acct.get("total_cost_basis") or 0
    unreal_pct = (unreal / cost * 100) if cost else 0
    realized = acct.get("realized_pl_window_dollars") or 0
    n_closes = acct.get("realized_pl_window_n_closes") or 0
    rstart = acct.get("realized_pl_window_start") or "—"
    rend = acct.get("realized_pl_window_end") or "—"

    parts = []
    parts.append('<section class="section">')
    parts.append('<h2>💼 Portfolio · Live Account State</h2>')
    parts.append(f'<p class="lede">Real positions and trades from the connected Fidelity brokerage account. All numbers below are <strong>your actual exposure</strong>, not recommendations. Snapshot: <code>{html.escape(snap)}</code>.</p>')

    # Stat strip
    today_klass = "green" if today >= 0 else "red"
    unreal_klass = "green" if unreal >= 0 else "red"
    realized_klass = "green" if realized >= 0 else "red"
    parts.append(f"""
<div class="stats">
  <div class="stat-card">
    <div class="label">Positions Subtotal</div>
    <div class="value">${pos_value:,.0f}</div>
    <div class="sub">{len(positions)} positions · cash not included</div>
  </div>
  <div class="stat-card {today_klass}">
    <div class="label">Today's Change</div>
    <div class="value">{_fmt_money(today, sign=True)}</div>
    <div class="sub">{today_pct:+.2f}% intraday</div>
  </div>
  <div class="stat-card {unreal_klass}">
    <div class="label">Unrealized P/L</div>
    <div class="value">{_fmt_money(unreal, sign=True)}</div>
    <div class="sub">{unreal_pct:+.1f}% on ${cost:,.0f} cost basis</div>
  </div>
  <div class="stat-card {realized_klass}">
    <div class="label">Realized P/L (window)</div>
    <div class="value">{_fmt_money(realized, sign=True)}</div>
    <div class="sub">{n_closes} closes · {rstart} → {rend}</div>
  </div>
</div>
""")

    # Cash note if applicable
    cash_note = acct.get("cash_note")
    if cash_note:
        parts.append(f'<div class="warn-banner">⚠ {html.escape(cash_note)}</div>')

    # Active management table (stocks + options, NON-passive)
    active = [p for p in positions if p.get("asset_type") in ("stock", "option")]
    passive = [p for p in positions if p.get("asset_type") in ("etf", "mutual_fund")]
    active_value = sum(p.get("current_value", 0) for p in active)
    passive_value = sum(p.get("current_value", 0) for p in passive)

    parts.append(f'<h3>Active management · {len(active)} positions · ${active_value:,.0f} ({active_value/pos_value*100:.1f}% of subtotal)</h3>')
    parts.append('<div class="holdings-table-wrap table-wrap"><table>')
    parts.append('<thead><tr><th>Position</th><th>Status</th><th class="num">Qty</th><th class="num">Cost</th><th class="num">Last</th><th class="num">Value</th><th class="num">Unrealized</th><th class="num">Today</th><th>Notes</th></tr></thead><tbody>')
    for p in sorted(active, key=lambda x: -x.get("current_value", 0)):
        is_opt = p.get("asset_type") == "option"
        ticker_disp = html.escape(p.get("underlying") or p.get("symbol"))
        opt_meta = ""
        if is_opt:
            dte = _option_dte(p.get("expiration"), snap)
            dte_str = f" · {dte}d" if dte is not None else ""
            opt_meta = f'<span class="opt-meta">${p.get("strike",0):.0f} {p.get("option_type","").upper()} {p.get("expiration","")}{dte_str} · ×{p.get("contracts",0)}</span>'
        status = (p.get("status") or "open").lower()
        gain = p.get("total_gain_dollars") or 0
        gain_pct = p.get("total_gain_pct") or 0
        today_g = p.get("today_gain_dollars") or 0
        today_pct = p.get("today_gain_pct") or 0
        gain_cls = "green-text" if gain >= 0 else "red-text"
        today_cls = "green-text" if today_g >= 0 else "red-text"
        notes_text = p.get("notes") or p.get("thesis") or ""
        parts.append(
            f'<tr class="pos-row status-{html.escape(status)}">'
            f'<td class="tkr">{ticker_disp}{opt_meta}</td>'
            f'<td>{_status_badge(status)}</td>'
            f'<td class="num">{p.get("quantity",0):g}</td>'
            f'<td class="num">${p.get("avg_cost",0):,.2f}</td>'
            f'<td class="num">${p.get("last_price",0):,.2f}</td>'
            f'<td class="num">${p.get("current_value",0):,.0f}</td>'
            f'<td class="num {gain_cls}">{_fmt_money(gain, sign=True)}<br><span style="font-size:10px;font-weight:500">{gain_pct:+.1f}%</span></td>'
            f'<td class="num {today_cls}">{_fmt_money(today_g, sign=True)}<br><span style="font-size:10px;font-weight:500">{today_pct:+.1f}%</span></td>'
            f'<td class="notes">{html.escape(notes_text)}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table></div>')

    # Passive index (collapsed)
    if passive:
        parts.append(f'<details style="margin-top:16px"><summary style="cursor:pointer;color:var(--text-muted);font-size:13px"><strong>Passive index · {len(passive)} positions · ${passive_value:,.0f} ({passive_value/pos_value*100:.1f}% of subtotal)</strong> — click to expand</summary>')
        parts.append('<div class="holdings-table-wrap table-wrap"><table>')
        parts.append('<thead><tr><th>Symbol</th><th>Description</th><th class="num">Qty</th><th class="num">Last</th><th class="num">Value</th><th class="num">Unrealized</th></tr></thead><tbody>')
        for p in sorted(passive, key=lambda x: -x.get("current_value", 0)):
            gain = p.get("total_gain_dollars") or 0
            gain_cls = "green-text" if gain >= 0 else "red-text"
            parts.append(
                f'<tr>'
                f'<td class="tkr">{html.escape(p.get("symbol",""))}</td>'
                f'<td class="muted">{html.escape(p.get("description",""))}</td>'
                f'<td class="num">{p.get("quantity",0):g}</td>'
                f'<td class="num">${p.get("last_price",0):,.2f}</td>'
                f'<td class="num">${p.get("current_value",0):,.0f}</td>'
                f'<td class="num {gain_cls}">{_fmt_money(gain, sign=True)} ({(p.get("total_gain_pct") or 0):+.1f}%)</td>'
                f'</tr>'
            )
        parts.append('</tbody></table></div>')
        parts.append('</details>')

    parts.append('</section>')
    return "".join(parts)


def _render_action_matrix(reconciliation: dict) -> str:
    h_pick = reconciliation["held_pick"]
    h_veto = reconciliation["held_vetoed"]
    h_uncov = reconciliation["held_no_cover"]
    pick_nh = reconciliation["pick_not_held"]

    def names(seq, get):
        return ", ".join(sorted({get(x) for x in seq if get(x)}))

    pick_held_names = names(h_pick, lambda x: x[0].get("underlying") or x[0].get("symbol"))
    veto_held_names = names(h_veto, lambda x: x[0].get("underlying") or x[0].get("symbol"))
    uncov_names = names([(p, e) for p, e in h_uncov if _exposure_key(p)], lambda x: x[0].get("underlying") or x[0].get("symbol"))
    opp_names = ", ".join(sorted(e["row"]["ticker"] for e in pick_nh))

    return (
        '<div class="action-matrix">'
        f'<div class="action-bucket green">'
        f'<div class="label">✓ Aligned</div>'
        f'<div class="count">{len(h_pick)}</div>'
        f'<div class="names">{html.escape(pick_held_names) or "—"}</div>'
        f'</div>'
        f'<div class="action-bucket red">'
        f'<div class="label">⚠ Friction · Held + Vetoed</div>'
        f'<div class="count">{len(h_veto)}</div>'
        f'<div class="names">{html.escape(veto_held_names) or "—"}</div>'
        f'</div>'
        f'<div class="action-bucket yellow">'
        f'<div class="label">? Uncovered</div>'
        f'<div class="count">{len(h_uncov)}</div>'
        f'<div class="names">{html.escape(uncov_names) or "—"}</div>'
        f'</div>'
        f'<div class="action-bucket blue">'
        f'<div class="label">⚡ PICK + Not Held</div>'
        f'<div class="count">{len(pick_nh)}</div>'
        f'<div class="names">{html.escape(opp_names) or "—"}</div>'
        f'</div>'
        '</div>'
    )


def _recon_pair_position(pos: dict, snap: str | None) -> str:
    """Render the LEFT side: a held position summary."""
    is_opt = pos.get("asset_type") == "option"
    ticker = pos.get("underlying") or pos.get("symbol")
    gain = pos.get("total_gain_dollars") or 0
    gain_pct = pos.get("total_gain_pct") or 0
    pl_cls = "green" if gain >= 0 else "red"
    detail = ""
    if is_opt:
        dte = _option_dte(pos.get("expiration"), snap)
        dte_str = f" · {dte}d to expiry" if dte is not None else ""
        moneyness = ""
        if pos.get("last_price") and pos.get("strike"):
            # For long-call: ITM if underlying > strike. We don't have underlying spot here,
            # so just show structure.
            moneyness = ""
        detail = f'<span class="meta">${pos.get("strike",0):.0f} {pos.get("option_type","").upper()} {pos.get("expiration","")}{dte_str} · ×{pos.get("contracts",0)} · cost ${pos.get("avg_cost",0):.2f}</span>'
    else:
        detail = f'<span class="meta">{pos.get("quantity",0):g} sh · cost ${pos.get("avg_cost",0):,.2f} · last ${pos.get("last_price",0):,.2f}</span>'
    notes = pos.get("notes") or pos.get("thesis") or ""
    return (
        '<div class="col">'
        '<span class="label">Held</span>'
        f'<span class="ticker">{html.escape(ticker)} {_status_badge(pos.get("status"))}</span>'
        f'{detail}'
        f'<span class="meta">value: <strong>${pos.get("current_value",0):,.0f}</strong> · {pos.get("pct_of_account",0):.1f}% of acct</span>'
        f'<span class="pl {pl_cls}">{_fmt_money(gain, sign=True)} ({gain_pct:+.1f}%)</span>'
        f'<span class="meta" style="margin-top:4px">{html.escape(notes)}</span>'
        '</div>'
    )


def _recon_pair_verdict(matrix_entry: dict | None) -> str:
    """Render the RIGHT side: matrix verdict on the same name."""
    if not matrix_entry:
        return (
            '<div class="col">'
            '<span class="label">Matrix verdict</span>'
            '<span class="ticker" style="color:var(--text-dim)">—</span>'
            '<span class="meta">No matrix coverage in supplied runs.</span>'
            '<span class="verdict">Run the matrix on this name before adding or sizing up.</span>'
            '</div>'
        )
    row = matrix_entry["row"]
    cls = row.get("classification") or "—"
    cls_color = "green" if cls == "PICK" else "red" if cls == "VETOED" else "muted"
    tier = _tier(row)
    cur = row.get("current_price_usd")
    aggr = row.get("aggressive_pt")
    cons = row.get("conservative_pt")
    upside = row.get("aggressive_upside_pct")
    summary = row.get("aggressive_executive_summary") or ""
    summary_short = summary[:280] + ("…" if len(summary) > 280 else "")
    snap = matrix_entry.get("snapshot_date") or "—"
    targets = []
    if cur is not None:
        targets.append(f"current ${cur:,.2f}")
    if aggr is not None:
        targets.append(f"agg PT ${aggr:,.2f}")
    if cons is not None:
        targets.append(f"cons PT ${cons:,.2f}")
    targets_str = " · ".join(targets) if targets else "—"
    upside_str = f"{upside:+.1f}% to agg PT" if upside is not None else ""
    return (
        '<div class="col">'
        '<span class="label">Matrix verdict</span>'
        f'<span class="ticker">{html.escape(row.get("ticker"))} '
        f'<span class="status-badge {cls_color}">{html.escape(cls)}</span> '
        f'{(_badge("Tier " + tier, "badge-tier-" + tier.lower()) if tier in {"A","B","C"} else "")}</span>'
        f'<span class="meta">{html.escape(targets_str)} · {html.escape(upside_str)}</span>'
        f'<span class="meta">snapshot: <code>{html.escape(snap)}</code> · run: <code>{html.escape(matrix_entry["run_label"])}</code></span>'
        f'<span class="verdict">{html.escape(summary_short)}</span>'
        '</div>'
    )


def _render_reconciliation(reconciliation: dict, portfolio: dict) -> str:
    snap = portfolio.get("account", {}).get("snapshot_at") if portfolio else None
    parts = []
    parts.append('<section class="section">')
    parts.append('<h2>🔀 Recommendations vs Reality</h2>')
    parts.append('<p class="lede">For each held name, what does the latest matrix verdict say? <strong>Friction cases first</strong> (held + vetoed, held + uncovered), then opportunities (PICK + not held), then aligned positions. Latest verdict by snapshot date wins when a ticker appears in multiple runs.</p>')

    parts.append(_render_action_matrix(reconciliation))

    bucket_specs = [
        ("held_vetoed", "bucket-friction", "⚠ Friction · Held but Matrix VETOED",
         "Conservative frame failed these in the latest run. Either reduce/exit, or document explicitly why you're overriding the model."),
        ("held_no_cover", "bucket-uncovered", "? Uncovered · Held but Not Matrix-Run",
         "These names aren't in any supplied matrix run. Run the matrix before adding; consider whether to keep them at all."),
        ("pick_not_held", "bucket-opportunity", "⚡ Opportunity · Matrix PICK · Not Held",
         "These names cleared the matrix but aren't in the portfolio. Highest-priority review candidates — validate options chain + sizing before entry."),
        ("held_pick", "bucket-aligned", "✓ Aligned · Held + Matrix PICK",
         "Latest matrix confirms the thesis. Hold; consider adding only if conviction increases and current sizing is below target."),
    ]

    parts.append('<div class="reconcile-grid">')
    for bucket_key, bucket_class, title, desc in bucket_specs:
        items = reconciliation.get(bucket_key, [])
        if not items:
            continue
        parts.append(f'<div class="recon-card {bucket_class}">')
        parts.append(f'<div class="header"><span class="title">{html.escape(title)}</span><span class="count">{len(items)} {"name" if len(items)==1 else "names"}</span></div>')
        parts.append(f'<div class="desc">{html.escape(desc)}</div>')
        if bucket_key == "pick_not_held":
            for entry in items:
                parts.append('<div class="recon-pair">')
                # LEFT: empty/placeholder
                parts.append(
                    '<div class="col">'
                    '<span class="label">Current exposure</span>'
                    '<span class="ticker" style="color:var(--text-dim)">—</span>'
                    '<span class="meta">Not in portfolio.</span>'
                    '<span class="meta">Consider entry sizing before adding.</span>'
                    '</div>'
                )
                # RIGHT: matrix verdict
                parts.append(_recon_pair_verdict(entry))
                parts.append('</div>')
        else:
            for pos, entry in items:
                parts.append('<div class="recon-pair">')
                parts.append(_recon_pair_position(pos, snap))
                parts.append(_recon_pair_verdict(entry))
                parts.append('</div>')
        parts.append('</div>')
    parts.append('</div>')

    # Passive separately - acknowledged but not reconciled
    if reconciliation.get("passive"):
        n = len(reconciliation["passive"])
        parts.append(f'<p class="lede" style="margin-top:24px;font-size:13px"><em>Plus {n} passive holdings (ETFs/mutual funds) — not subject to per-ticker matrix verdicts.</em></p>')

    parts.append('</section>')
    return "".join(parts)


def _render_news_chips(enrichment_row: dict | None,
                       earnings_row: dict | None = None) -> str:
    """Render a compact catalyst chip cluster for a screener row.

    Combines (when available):
    - sentiment polarity in [-1, 1] color-coded by sign
    - top 2 themes by confidence (purple chip for ``government_action``)
    - next earnings date with days-away color coding (red <7d / yellow
      7-30d / gray >30d)
    """
    parts = []
    if enrichment_row:
        sent = (enrichment_row.get("sentiment") or {})
        chosen = sent.get("finbert") or sent.get("keyword")
        if chosen and chosen.get("n_headlines", 0) > 0:
            agg = chosen.get("aggregate", 0.0) or 0.0
            cls = (
                "chip-sent-pos" if agg > 0.05
                else "chip-sent-neg" if agg < -0.05
                else "chip-sent-neu"
            )
            scorer = chosen.get("scorer", "?")
            n = chosen.get("n_headlines", 0)
            title_attr = (
                f"{scorer} sentiment from n={n} headlines (range [-1, 1])"
            )
            parts.append(
                f'<span class="chip-sent {cls}" title="{html.escape(title_attr)}">'
                f'{agg:+.2f}</span>'
            )
        themes = enrichment_row.get("themes") or []
        for t in themes[:2]:
            label = t.get("label", "")
            conf = t.get("confidence", 0.0) or 0.0
            cls = "chip-theme chip-theme-gov" if label == "government_action" else "chip-theme"
            title_attr = f"{label}: {conf:.0%} of headlines matched"
            parts.append(
                f'<span class="{cls}" title="{html.escape(title_attr)}">'
                f'{html.escape(label.replace("_", " "))}</span>'
            )

    if earnings_row and earnings_row.get("next_earnings"):
        ne = earnings_row["next_earnings"]
        days = ne.get("days_away")
        date_str = ne.get("date") or "?"
        if days is None:
            cls = "chip-earn-far"
            label = f"📅 {date_str}"
        elif days < 7:
            cls = "chip-earn-near"
            label = f"📅 {days}d"
        elif days <= 30:
            cls = "chip-earn-mid"
            label = f"📅 {days}d"
        else:
            cls = "chip-earn-far"
            label = f"📅 {days}d"
        beat = earnings_row.get("beat_rate_pct")
        title_bits = [f"Next earnings: {date_str}"]
        if days is not None:
            title_bits.append(f"in {days} days")
        if beat is not None:
            title_bits.append(f"8Q beat rate {beat:.0f}%")
        title_attr = " · ".join(title_bits)
        parts.append(
            f'<span class="chip-earn {cls}" title="{html.escape(title_attr)}">'
            f'{html.escape(label)}</span>'
        )

    return "".join(parts) if parts else '<span class="muted" style="font-size:11px">—</span>'


def _render_screener_watchlist(screener: dict, matrix_index: dict, held_keys: set,
                               enrichment: dict[str, dict] | None = None,
                               earnings: dict[str, dict] | None = None,
                               top_n: int = 25) -> str:
    candidates = screener.get("candidates", [])[:top_n]
    if not candidates:
        return ""
    snap = screener.get("trading_date", "—")
    parts = []
    parts.append('<section class="section">')
    parts.append(f'<h2>🔭 Screener Watchlist · Top {len(candidates)} Candidates</h2>')
    parts.append(f'<p class="lede">Pre-matrix candidates from the latest screener run. <strong>High screener score is a funnel, not a buy signal</strong> — these need matrix validation + same-day options chain before any entry. Snapshot: <code>{html.escape(snap)}</code>. Universe size: {screener.get("universe_size", "—")}.</p>')

    if screener.get("is_partial"):
        rate_limited = ", ".join(screener.get("rate_limited_failures", []))
        parts.append(f'<div class="warn-banner">⚠ Partial screener result — rate limited on: <code>{html.escape(rate_limited)}</code>. Top-N may be missing legitimate names.</div>')

    parts.append('<div class="screener-table-wrap table-wrap"><table>')
    parts.append('<thead><tr><th>#</th><th>Ticker</th><th>Name</th><th class="num">Score</th><th class="num">Mcap</th><th>Sector</th><th>Catalysts</th><th>Matrix</th><th>Ownership</th><th>Summary</th></tr></thead><tbody>')
    for c in candidates:
        tkr = c["ticker"]
        # Matrix status
        m_entry = matrix_index.get(tkr)
        if m_entry is None:
            matrix_badge = '<span class="badge badge-screener-pending">Needs matrix</span>'
        else:
            cls = m_entry["row"].get("classification")
            if cls == "PICK":
                matrix_badge = '<span class="badge badge-screener-pick">PICK</span>'
            elif cls == "VETOED":
                matrix_badge = '<span class="badge badge-screener-vetoed">VETOED</span>'
            else:
                matrix_badge = '<span class="badge badge-screener-pending">—</span>'
        # Ownership status
        owned_badge = '<span class="badge badge-held">HELD</span>' if tkr in held_keys else '<span class="muted" style="font-size:11px">—</span>'
        mcap = c.get("market_cap", 0)
        mcap_str = f"${mcap/1e9:.0f}B" if mcap else "—"
        sector = c.get("sector_desc", "—")
        if sector and len(sector) > 28:
            sector = sector[:28] + "…"
        summary = c.get("summary", "")[:80]
        news_cell = _render_news_chips(
            enrichment.get(tkr) if enrichment else None,
            earnings.get(tkr) if earnings else None,
        )
        parts.append(
            f'<tr class="scr-row">'
            f'<td>{c.get("rank","—")}</td>'
            f'<td class="tkr">{html.escape(tkr)}</td>'
            f'<td class="muted" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{html.escape(c.get("name","—"))}</td>'
            f'<td class="num score">{c.get("composite_score",0):.1f}</td>'
            f'<td class="num">{mcap_str}</td>'
            f'<td class="muted" style="font-size:11px">{html.escape(sector)}</td>'
            f'<td>{news_cell}</td>'
            f'<td>{matrix_badge}</td>'
            f'<td>{owned_badge}</td>'
            f'<td class="muted" style="font-size:11px;max-width:280px">{html.escape(summary)}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table></div>')
    parts.append('</section>')
    return "".join(parts)


def _render_realized_pl(portfolio: dict) -> str:
    if not portfolio:
        return ""
    trades = portfolio.get("trades", [])
    summary = _summarize_realized(trades)
    if summary["n_closes"] == 0:
        return ""

    parts = []
    parts.append('<section class="section">')
    parts.append(f'<h2>💰 Realized P/L · {summary["n_closes"]} Closes</h2>')
    parts.append(f'<p class="lede">Closed positions with computed realized P/L. Net: <strong>{_fmt_money(summary["total_pl"], sign=True)}</strong> across {len(summary["by_underlying"])} underlyings. Equity sells without cost basis are listed below as proceeds-only.</p>')

    parts.append('<div class="realized-summary">')
    for u, bucket in sorted(summary["by_underlying"].items(), key=lambda kv: -kv[1]["pl"]):
        cls = "green" if bucket["pl"] >= 0 else "red"
        n = len(bucket["trades"])
        parts.append(
            f'<div class="real-card {cls}">'
            f'<div class="ticker">{html.escape(u)}</div>'
            f'<div class="pl {cls}">{_fmt_money(bucket["pl"], sign=True)}</div>'
            f'<div class="meta">{n} close{"s" if n!=1 else ""}</div>'
            f'</div>'
        )
    parts.append('</div>')

    parts.append('<h3>Trade ledger · all closes (newest first)</h3>')
    parts.append('<div class="table-wrap"><table>')
    parts.append('<thead><tr><th>Date</th><th>Action</th><th>Symbol</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Proceeds</th><th class="num">Realized P/L</th><th>Rationale</th></tr></thead><tbody>')
    for t in summary["closes"]:
        pl = t.get("realized_pl") or 0
        pl_cls = "green-text" if pl >= 0 else "red-text"
        sym = t.get("symbol") or t.get("underlying") or "—"
        parts.append(
            f'<tr>'
            f'<td class="muted">{html.escape(t.get("trade_date","—"))}</td>'
            f'<td><span class="status-badge {"open" if t.get("action") in ("STC","SELL") else "closed"}">{html.escape(t.get("action","—"))}</span></td>'
            f'<td class="tkr">{html.escape(sym)}</td>'
            f'<td class="num">{t.get("quantity",0):g}</td>'
            f'<td class="num">${t.get("price",0):,.2f}</td>'
            f'<td class="num">{_fmt_money(t.get("proceeds"), sign=True)}</td>'
            f'<td class="num {pl_cls}">{_fmt_money(pl, sign=True)}</td>'
            f'<td class="muted" style="font-size:11px">{html.escape(t.get("rationale") or "")}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table></div>')

    # Equity sells without cost basis
    other_sells = [t for t in trades if t.get("realized_pl") is None and t.get("action") in ("SELL",)]
    if other_sells:
        parts.append('<details style="margin-top:16px"><summary style="cursor:pointer;color:var(--text-muted);font-size:13px"><strong>Equity sells without cost basis</strong> · {n} trades — proceeds only</summary>'.format(n=len(other_sells)))
        parts.append('<div class="table-wrap"><table>')
        parts.append('<thead><tr><th>Date</th><th>Symbol</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Proceeds</th><th>Notes</th></tr></thead><tbody>')
        for t in sorted(other_sells, key=lambda x: x.get("trade_date",""), reverse=True):
            parts.append(
                f'<tr>'
                f'<td class="muted">{html.escape(t.get("trade_date","—"))}</td>'
                f'<td class="tkr">{html.escape(t.get("symbol","—"))}</td>'
                f'<td class="num">{t.get("quantity",0):g}</td>'
                f'<td class="num">${t.get("price",0):,.2f}</td>'
                f'<td class="num green-text">{_fmt_money(t.get("proceeds"), sign=True)}</td>'
                f'<td class="muted" style="font-size:11px">{html.escape(t.get("rationale") or t.get("notes") or "")}</td>'
                f'</tr>'
            )
        parts.append('</tbody></table></div></details>')

    parts.append('</section>')
    return "".join(parts)


def _render_private_banner(portfolio: dict | None) -> str:
    if not portfolio:
        return ""
    return (
        '<div class="private-banner">'
        '<span class="icon">🔒</span>'
        '<span><strong>Private — contains live brokerage data.</strong> Real positions, real cost basis, real P/L. '
        'Do not share, screenshot, or commit. This file lives outside <code>runs/</code> for a reason.</span>'
        '</div>'
    )


def _render_skipped(skipped: list[str] | None) -> str:
    if not skipped:
        return ""
    items = "".join(f"<li>{html.escape(s)}</li>" for s in skipped)
    return (
        '<div class="skipped-banner">'
        '<strong>ℹ Some runs were skipped (not yet ready):</strong>'
        f'<ul>{items}</ul>'
        '</div>'
    )


def _render_macro_banner(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    regime = snapshot.get("regime", "normal")
    sig = snapshot.get("signals") or {}
    triggers = snapshot.get("triggers") or []
    bg = {
        "normal": "rgba(34, 197, 94, 0.08)",
        "defensive": "rgba(234, 179, 8, 0.10)",
        "halt": "rgba(239, 68, 68, 0.12)",
    }.get(regime, "rgba(148, 163, 184, 0.08)")
    border = {
        "normal": "rgba(34, 197, 94, 0.25)",
        "defensive": "rgba(234, 179, 8, 0.35)",
        "halt": "rgba(239, 68, 68, 0.40)",
    }.get(regime, "rgba(148, 163, 184, 0.25)")
    accent = {
        "normal": "var(--green)",
        "defensive": "var(--yellow)",
        "halt": "var(--red)",
    }.get(regime, "var(--muted)")
    bits = []
    if sig.get("vix") is not None:
        bits.append(f"VIX <strong>{sig['vix']:.1f}</strong>")
    if sig.get("spx_5d_return_pct") is not None:
        v = sig["spx_5d_return_pct"]
        bits.append(f"SPX 5d <strong>{v:+.1f}%</strong>")
    if sig.get("yield_curve_10y_3m_bps") is not None:
        bits.append(f"10y/3m <strong>{sig['yield_curve_10y_3m_bps']:+.0f}bps</strong>")
    body = " · ".join(bits) if bits else "no signals"
    triggers_html = ""
    if triggers:
        items = "".join(f"<li>{html.escape(t)}</li>" for t in triggers)
        triggers_html = f'<ul style="margin: 6px 0 0 20px; padding: 0; font-size: 11px;">{items}</ul>'
    rec = snapshot.get("recommended_action", "proceed")
    return (
        f'<div style="background: {bg}; border: 1px solid {border}; '
        f'border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; '
        f'color: var(--text); font-size: 12px;">'
        f'<span style="color: {accent}; font-weight: 700;">'
        f'Macro · {regime.upper()}</span>'
        f' &nbsp;·&nbsp; {body}'
        f' &nbsp;·&nbsp; <span class="muted">recommended: {html.escape(rec)}'
        f' (as of {html.escape(snapshot.get("as_of_date", "—"))})</span>'
        f'{triggers_html}'
        f'</div>'
    )


def render(rows: list[dict], metadata: dict, title: str, subtitle: str,
           options_map: dict | None = None,
           chronos_map: dict | None = None,
           portfolio: dict | None = None,
           screener: dict | None = None,
           news_enrichment: dict[str, dict] | None = None,
           earnings_calendar: dict[str, dict] | None = None,
           macro_snapshot: dict | None = None,
           skipped_runs: list[str] | None = None) -> str:
    options_map = options_map or {}
    chronos_map = chronos_map or {}
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

    # Sector grouping driven by SIC prefix on each row.
    sector_groups: dict[str, list[str]] = {}
    for r in best.values():
        sec = _sector_from_sic(r.get("sector_sic"))
        sector_groups.setdefault(sec, []).append(r["ticker"])

    # Reconciliation against portfolio (if provided)
    matrix_index = _build_matrix_index(rows, metadata)
    reconciliation = _build_reconciliation(portfolio, matrix_index) if portfolio else None
    held_keys = reconciliation["held_keys"] if reconciliation else set()

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

    # Private banner (only if portfolio supplied)
    parts.append(_render_private_banner(portfolio))

    # Macro regime banner (only if snapshot supplied)
    parts.append(_render_macro_banner(macro_snapshot))

    # Skipped runs warning
    parts.append(_render_skipped(skipped_runs))

    # Portfolio dashboard (only if portfolio supplied)
    if portfolio:
        parts.append(_render_portfolio_dashboard(portfolio))

    # Reconciliation: held-vs-matrix
    if reconciliation:
        parts.append(_render_reconciliation(reconciliation, portfolio))

    # ── Stat strip
    parts.append('<section class="section">')
    parts.append('<h2>📊 Matrix Coverage Across Supplied Runs</h2>')
    n_top5_a = sum(1 for r in top5 if _tier(r) == "A")
    n_top5_b = sum(1 for r in top5 if _tier(r) == "B")
    n_top5_c = sum(1 for r in top5 if _tier(r) == "C")
    top5_breakdown = " · ".join(
        f"{n} Tier {letter}" for letter, n in [("A", n_top5_a), ("B", n_top5_b), ("C", n_top5_c)] if n
    ) or "no picks"
    parts.append(f"""
<div class="stats">
  <div class="stat-card green">
    <div class="label">High Conviction</div>
    <div class="value">{len(top5)}</div>
    <div class="sub">Top-{len(top5)} trades · {top5_breakdown}</div>
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
    parts.append('<section class="section">')
    parts.append('<h2>🏆 Top 5 Trades · Cross-Run</h2>')
    if n_top5_a == len(top5) and len(top5) > 0:
        lede_extra = "Every name in this list is <strong>Tier A</strong> — both frames set explicit price targets within 5% of each other (or the conservative target is <em>higher</em>, the rarest signal)."
    elif n_top5_a:
        lede_extra = f"<strong>{n_top5_a} of {len(top5)} are Tier A</strong> (tight dual-frame agreement); the rest are Tier B/C — see per-pick caveats."
    else:
        lede_extra = "No Tier A names in the top-5 — these are all Tier B/C picks where conservative either set a wider PT or declined to model. Size accordingly."
    parts.append(f'<p class="lede">Composite ranking across all supplied runs by tier × compression × cross-band confirmation × aggressive upside. {lede_extra}</p>')
    parts.append('<div class="hero">')
    for i, r in enumerate(top5, 1):
        # Use the agent's own executive summary, truncated, instead of curated copy
        summary = (r.get("aggressive_executive_summary") or "").strip()
        # Take first 2 sentences max, and cap at ~280 chars
        first = summary.split(". ")
        why = ". ".join(first[:2]).rstrip(".") + ("." if first[:2] else "")
        if len(why) > 280:
            why = why[:280].rstrip() + "…"
        parts.append(_hero_card(i, r, why, cross_band))
    parts.append('</div>')

    # Sector mix of top-5
    parts.append('<h3>Sector composition of the top-5</h3>')
    parts.append('<p class="lede" style="margin-bottom:12px">SIC-derived sector mapping. Diversified composition is a good sign that the screener isn\'t over-fitting a single thematic pocket.</p>')
    parts.append('<div class="sector-grid">')
    top5_sectors: dict[str, list[str]] = {}
    for r in top5:
        sec = _sector_from_sic(r.get("sector_sic"))
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
        if chronos_map:
            parts.append('<p class="lede" style="margin-top:8px"><strong>Model agreement chip</strong> — 🟢 inside the Chronos p25-p75 cone (model & personas agree), 🟡 inside the cone but at one edge (p10-p25 low or p75-p90 high), 🔴 outside the p10-p90 cone (notable disagreement). Hover the chip for the model\'s p50 vs the persona PT.</p>')
        parts.append('<div class="options-grid">')
        for r in top5:
            ovl = options_map.get((r["ticker"], r["_run"]))
            chronos = chronos_map.get((r["ticker"], r["_run"]))
            parts.append(_opt_card(r, ovl, chronos))
        parts.append('</div>')

        # All-picks options summary table
        all_ovls = [(r, options_map.get((r["ticker"], r["_run"])))
                    for r in ranked if options_map.get((r["ticker"], r["_run"]))]
        if all_ovls:
            parts.append('<h3 style="margin-top:32px">All picks · options snapshot</h3>')
            parts.append('<div class="table-wrap"><table>')
            model_header = '<th>Model</th>' if chronos_map else ''
            parts.append(f'<thead><tr><th>Ticker</th><th>Run</th><th>Tier</th><th>Strategy</th><th>Exp</th><th class="num">DTE</th><th class="num">Long K</th><th class="num">Short K</th><th class="num">Net Debit</th><th class="num">Max Profit</th><th class="num">Breakeven</th><th class="num">R/R</th><th>Vol</th>{model_header}<th>Liq</th></tr></thead>')
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
                vc = ovl.get("vol_context") or {}
                backdrop = vc.get("long_call_backdrop")
                vol_chip = ""
                if backdrop in ("favorable", "mixed", "unfavorable"):
                    vol_label = {
                        "favorable": "🟢",
                        "mixed": "🟡",
                        "unfavorable": "🔴",
                    }[backdrop]
                    hv_rank = vc.get("hv_rank_252d")
                    iv_rv = vc.get("iv_rv_ratio")
                    title_bits = [f"Long-call backdrop: {backdrop}"]
                    if hv_rank is not None:
                        title_bits.append(f"HV rank {hv_rank:.0f}")
                    if iv_rv is not None:
                        title_bits.append(f"IV/RV {iv_rv:.2f}×")
                    title_attr = html.escape(" · ".join(title_bits))
                    vol_chip = f'<span class="vol-chip vol-{backdrop}" title="{title_attr}">{vol_label}</span>'
                else:
                    vol_chip = '<span class="muted">—</span>'
                model_cell = ''
                if chronos_map:
                    chronos_data = chronos_map.get((r["ticker"], r["_run"]))
                    chip = _chronos_chip(chronos_data)
                    model_cell = f'<td>{chip if chip else "<span class=\"muted\">—</span>"}</td>'
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
                    f'<td>{vol_chip}</td>'
                    f'{model_cell}'
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
    parts.append('<h2>📋 Full Picks Ledger · All Supplied Runs</h2>')
    n_run_word = "run" if len(metadata) == 1 else f"{len(metadata)} runs"
    parts.append(f'<p class="lede">All {len(ranked)} picks across the supplied {n_run_word}, sorted by composite score. Star indicates negative compression (cons PT > aggr PT) or perfect agreement.</p>')
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

    # ── Screener watchlist (only if screener provided)
    if screener:
        parts.append(_render_screener_watchlist(
            screener, matrix_index, held_keys,
            enrichment=news_enrichment,
            earnings=earnings_calendar,
        ))

    # ── Realized P/L (only if portfolio provided)
    if portfolio:
        parts.append(_render_realized_pl(portfolio))

    # ── Footer
    parts.append('<footer class="footer">')
    parts.append('<p><strong>Source pipeline</strong>:</p>')
    for label, meta in metadata.items():
        parts.append(f'<p>· <code>{label}</code>: matrix <code>{html.escape(meta["matrix_run"] or "—")}</code> ← screener <code>{html.escape(meta["screener_run"] or "—")}</code> · {meta["n_total"]} tickers ({meta["n_picks"]} picks, {meta["n_vetoed"]} vetoed)</p>')
    if screener:
        parts.append(f'<p>· screener watchlist: <code>{html.escape(screener.get("_screener_dir","—"))}</code> · {screener.get("trading_date","—")} · {len(screener.get("candidates", []))} candidates from {screener.get("universe_size","—")}-name universe</p>')
    if portfolio:
        n_pos = len(portfolio.get("positions", []))
        n_trd = len(portfolio.get("trades", []))
        snap = portfolio.get("account", {}).get("snapshot_at", "—")
        parts.append(f'<p>· portfolio: {n_pos} positions · {n_trd} trades · snapshot <code>{html.escape(snap)}</code></p>')
    if skipped_runs:
        parts.append(f'<p>· skipped {len(skipped_runs)} run(s) (matrix not yet ready) — re-run when available</p>')
    parts.append('<p>&nbsp;</p>')
    parts.append('<p><strong>Methodology</strong>: Each ticker run through TradingAgents twice (aggressive + conservative profile), full multi-agent debate. Filter: aggressive ∈ Overweight/Buy AND conservative ∉ Underweight/Sell. Composite score = tier base × compression bonus × cross-band confirmation × bounded aggressive upside.</p>')
    parts.append('<p><strong>Reproduce</strong>: <code>python scripts/build_run_accounting.py --matrix-run &lt;dir&gt; --screener-run &lt;dir&gt;</code> · this report: <code>python scripts/build_html_report.py --runs &lt;run&gt;:&lt;label&gt; ... --output report.html [--portfolio &lt;json&gt;] [--screener-run &lt;dir&gt;]</code></p>')
    parts.append('<p>&nbsp;</p>')
    if portfolio:
        parts.append('<p style="color: var(--yellow); font-size: 11px;"><strong>🔒 Reminder:</strong> This report contains live brokerage data. Treat as private — do not share, screenshot, or commit.</p>')
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
    p.add_argument("--portfolio", default=None,
                   help="Path to portfolio_export.json (versioned positions + trades)")
    p.add_argument("--screener-run", default=None,
                   help="Path to a screener_* run dir to render as a watchlist")
    p.add_argument("--macro", default=None,
                   help="Path to a macro_<DATE>.json snapshot. When supplied, "
                        "renders a regime banner at the top of the report.")
    p.add_argument("--allow-missing-runs", action="store_true", default=True,
                   help="Skip runs whose verdict_ledger.json is missing (matrix may be in flight). Default true.")
    p.add_argument("--strict-runs", action="store_true",
                   help="Fail if any run is missing its verdict_ledger.json. Overrides --allow-missing-runs.")
    args = p.parse_args()

    allow_missing = not args.strict_runs
    rows, metadata, options_map, chronos_map, skipped = _load_runs(args.runs, allow_missing=allow_missing)
    portfolio = _load_portfolio(args.portfolio)
    screener = _load_screener(args.screener_run)
    news_enrichment = _load_news_enrichment(screener)

    extra_dirs = []
    for spec in args.runs:
        path_str, _, _ = spec.partition(":")
        extra_dirs.append(Path(path_str))
    earnings_calendar = _load_earnings_calendar(screener, extra_dirs=extra_dirs)
    macro_snapshot = _load_macro_snapshot(args.macro)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render(rows, metadata, args.title, args.subtitle,
                       options_map=options_map,
                       chronos_map=chronos_map,
                       portfolio=portfolio,
                       screener=screener, news_enrichment=news_enrichment,
                       earnings_calendar=earnings_calendar,
                       macro_snapshot=macro_snapshot,
                       skipped_runs=skipped)
    out_path.write_text(html_text)

    n_picks = sum(1 for r in rows if r["classification"] == "PICK")
    n_vetoed = sum(1 for r in rows if r["classification"] == "VETOED")
    print(f"  ✅ {out_path}")
    print(f"     {len(rows)} tickers · {n_picks} picks · {n_vetoed} vetoed")
    if portfolio:
        n_pos = len(portfolio.get("positions", []))
        n_trd = len(portfolio.get("trades", []))
        print(f"     portfolio: {n_pos} positions · {n_trd} trades")
    if screener:
        print(f"     screener: {len(screener.get('candidates', []))} candidates from {screener.get('_screener_dir', '—')}")
    if news_enrichment:
        print(f"     news_enrichment: {len(news_enrichment)} tickers tagged")
    if earnings_calendar:
        print(f"     earnings_calendar: {len(earnings_calendar)} tickers with next-earnings dates")
    if chronos_map:
        print(f"     chronos_overlay: {len(chronos_map)} ticker-run forecasts loaded")
    if macro_snapshot:
        print(f"     macro_snapshot: regime={macro_snapshot.get('regime', '—')} status={macro_snapshot.get('status', '—')}")
    if skipped:
        print(f"     ⚠ skipped {len(skipped)} run(s):")
        for s in skipped:
            print(f"        · {s}")
    print(f"     {len(html_text):,} bytes")


if __name__ == "__main__":
    main()
