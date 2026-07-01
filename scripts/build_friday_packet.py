"""Build the Friday decision packet: a one-page go/no-go + ranked ticket table.

Reads all matrix runs sharing a given trade date (default: the most recent,
discovered the same content-based way as portfolio_load_context.py — NOT by
directory mtime) and renders a single markdown + self-contained HTML
artifact meant to be skimmed in under a minute before same-day entry:

  1. Header / go-no-go — pick counts by tier, RED banner on data-quality
     problems (missing/empty/errored options overlay), macro regime.
  2. Ranked ticket table — all tiers merged, B first (empirical best
     long-call ROI), then A, then C — with live price + gap%, OCC contract,
     limit price, sized qty, PT/exit rule, liquidity/earnings flags.
  3. Freshness footer — per-artifact generated_at, snapshot dates, and the
     same-day options refresh reminder.
  4. Portfolio deltas (only with --positions-file) — held tickers' tier
     status vs this week, via the (multi-run-union) portfolio context.
  5. Approval checklist — the exact `approve_lean_signal.py` command per
     ticket, using export_lean_signals.py's id convention.

This script is READ-ONLY over existing run artifacts. It never writes
anything outside its own output directory
(runs/friday_packet_<date>/packet.{md,html}).

Usage::

    .venv/bin/python scripts/build_friday_packet.py
    .venv/bin/python scripts/build_friday_packet.py --date 2026-06-26
    .venv/bin/python scripts/build_friday_packet.py --skip-live
    .venv/bin/python scripts/build_friday_packet.py \\
        --positions-file runs/portfolio/positions.json \\
        --policy-file runs/portfolio/policy.json
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.portfolio_load_context import (  # noqa: E402
    DEFAULT_POLICY,
    DEFAULT_POSITIONS,
    _matrix_run_trade_date,
    build_context,
)

RUNS_DIR = REPO_ROOT / "runs"
GAP_WARN_PCT = 3.0
TIER_ORDER = {"B": 0, "A": 1, "C": 2}
EXIT_RULE_BY_TIER = {
    "A": "tier_a_take_profit",
    "B": "tier_b_run",
    "C": "tier_c_trim",
}


# ---------------------------------------------------------------------------
# Run discovery (content-based, shared logic with portfolio_load_context.py)


def find_matrix_runs_for_trade_date(trade_date: str, runs_dir: Path = RUNS_DIR) -> list[Path]:
    """All matrix runs whose content-derived trade date equals `trade_date`."""
    if not runs_dir.exists():
        return []
    matches = []
    for p in runs_dir.iterdir():
        if not p.is_dir() or not (p / "verdict_ledger.json").exists():
            continue
        if _matrix_run_trade_date(p) == trade_date:
            matches.append(p)
    return sorted(matches, key=lambda p: p.name)


def find_latest_trade_date(runs_dir: Path = RUNS_DIR) -> str | None:
    if not runs_dir.exists():
        return None
    dates = []
    for p in runs_dir.iterdir():
        if not p.is_dir() or not (p / "verdict_ledger.json").exists():
            continue
        td = _matrix_run_trade_date(p)
        if td:
            dates.append(td)
    return max(dates) if dates else None


# ---------------------------------------------------------------------------
# Artifact loading


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def load_run_artifacts(run_dir: Path) -> dict[str, Any]:
    """Load every artifact this packet consumes from one matrix run dir."""
    verdict = _read_json(run_dir / "verdict_ledger.json") or {}
    overlay = _read_json(run_dir / "options_overlay.json")
    manifest = _read_json(run_dir / "manifest.json") or {}
    iv_surface = _read_json(run_dir / "iv_surface_ranking.json")
    earnings_calendar = _read_json(run_dir / "earnings_calendar.json")

    return {
        "run_dir": run_dir,
        "run_id": verdict.get("run_id", run_dir.name),
        "verdict": verdict,
        "overlay": overlay,
        "manifest": manifest,
        "iv_surface": iv_surface,
        "earnings_calendar": earnings_calendar,
    }


def overlay_health(artifacts: dict[str, Any]) -> list[str]:
    """Return data-quality problems for one run's options_overlay.json.

    Triggers the RED go/no-go banner: missing file, zero strategies built
    despite >0 picks in the ledger, or any overlay row carrying an `error`.
    """
    problems: list[str] = []
    run_id = artifacts["run_id"]
    rows = artifacts["verdict"].get("rows", [])
    n_picks = sum(1 for r in rows if r.get("classification") == "PICK")

    overlay = artifacts["overlay"]
    if overlay is None:
        if n_picks > 0:
            problems.append(f"{run_id}: options_overlay.json is MISSING ({n_picks} picks in ledger)")
        return problems

    overlays = overlay.get("overlays") or []
    n_strategies = sum(1 for ov in overlays if ov.get("legs"))
    if n_picks > 0 and n_strategies == 0:
        problems.append(
            f"{run_id}: options_overlay.json has ZERO strategies built despite {n_picks} picks"
        )
    for ov in overlays:
        if ov.get("error"):
            problems.append(f"{run_id}: {ov.get('ticker', '?')} overlay row has error: {ov['error']}")
    return problems


# ---------------------------------------------------------------------------
# Ticket assembly


def _leg(overlay_row: dict[str, Any]) -> dict[str, Any] | None:
    legs = overlay_row.get("legs") or []
    return legs[0] if legs else None


def _signal_id(ticker: str, measure_date: str) -> str:
    """Mirror export_lean_signals.py's `id` convention exactly.

    export keys the id on the overlay ``snapshot_date`` (measure date), which
    can differ from the run's content trade-date when the overlay is refreshed
    the morning after the ledger date.
    """
    return f"{ticker.upper()}-{measure_date}"


def build_tickets(
    run_artifacts: list[dict[str, Any]],
    trade_date: str,
    *,
    account_value: float | None,
    policy: dict[str, Any] | None,
    live_prices: dict[str, float],
    skip_live: bool,
) -> list[dict[str, Any]]:
    """One ticket per PICK-tier (A/B/C) ticker across all contributing runs."""
    tickets: list[dict[str, Any]] = []

    for artifacts in run_artifacts:
        run_id = artifacts["run_id"]
        rows_by_ticker = {r["ticker"]: r for r in artifacts["verdict"].get("rows", [])}
        overlay_doc = artifacts["overlay"] or {}
        overlays = overlay_doc.get("overlays") or []
        # export_lean_signals.py keys the signal id on the overlay's
        # snapshot_date (the measure date), NOT the run's content trade-date.
        # The two differ whenever the overlay is built the morning after the
        # ledger date (Fri EOD ledger, Sat overlay refresh). Mirror export
        # exactly so the approval-checklist commands resolve against signals.json.
        signal_date = overlay_doc.get("snapshot_date") or trade_date
        iv_by_ticker = {}
        if artifacts["iv_surface"]:
            iv_by_ticker = {r["ticker"]: r for r in artifacts["iv_surface"].get("rows", [])}

        for ov in overlays:
            tier = ov.get("tier")
            if tier not in ("A", "B", "C"):
                continue
            ticker = str(ov["ticker"]).upper()
            row = rows_by_ticker.get(ticker, {})
            leg = _leg(ov)

            anchor_price = row.get("current_price") or ov.get("current_price_usd")

            live_price = None
            gap_pct = None
            stale_warning = None
            if skip_live:
                stale_warning = "live price not fetched (--skip-live)"
            else:
                live_price = live_prices.get(ticker)
                if live_price is None:
                    stale_warning = "live price fetch failed; anchor price may be stale"
                elif anchor_price:
                    gap_pct = round((live_price - anchor_price) / anchor_price * 100, 2)

            ref_premium = None
            limit_price = None
            qty = None
            if leg is not None:
                ref_premium = leg.get("price", ov.get("net_debit_per_share"))
                if ref_premium is not None:
                    limit_price = round(float(ref_premium) * 1.05, 4)
                    if account_value is not None and policy is not None:
                        sizing = (policy.get("tier_sizing") or {}).get(tier) or {}
                        starter_pct = sizing.get("starter_pct")
                        if starter_pct is not None and ref_premium > 0:
                            qty = math.floor(
                                (account_value * float(starter_pct)) / (float(ref_premium) * 100)
                            )

            iv_row = iv_by_ticker.get(ticker)
            liquidity_flags = []
            if leg is not None and leg.get("open_interest") is not None:
                min_oi = (policy or {}).get("options_defaults", {}).get("min_open_interest", 100)
                if leg["open_interest"] < min_oi:
                    liquidity_flags.append(f"thin chain (OI {leg['open_interest']} < {min_oi})")
            for w in ov.get("liquidity_warnings") or []:
                liquidity_flags.append(str(w))
            earnings_flag = None
            if iv_row and iv_row.get("earnings_in_window"):
                ew = iv_row["earnings_in_window"]
                earnings_flag = ew.get("date") if isinstance(ew, dict) else str(ew)
            elif ov.get("earnings_note"):
                earnings_flag = ov["earnings_note"]

            tickets.append({
                "ticker": ticker,
                "tier": tier,
                "run_id": run_id,
                "thesis": row.get("aggressive_executive_summary") or "",
                "anchor_price": anchor_price,
                "live_price": live_price,
                "gap_pct": gap_pct,
                "gap_warn": gap_pct is not None and abs(gap_pct) > GAP_WARN_PCT,
                "stale_warning": stale_warning,
                "occ_symbol": leg.get("symbol") if leg else None,
                "strike": leg.get("strike") if leg else None,
                "expiry": leg.get("expiration", ov.get("expiration")) if leg else None,
                "ref_premium_per_share": ref_premium,
                "limit_price_per_share": limit_price,
                "qty": qty,
                "cons_pt": None if tier == "C" else ov.get("conservative_pt"),
                "aggr_pt": ov.get("aggressive_pt"),
                "exit_rule": EXIT_RULE_BY_TIER[tier],
                "liquidity_flags": liquidity_flags,
                "earnings_flag": earnings_flag,
                "signal_id": _signal_id(ticker, signal_date),
            })

    tickets.sort(key=lambda t: (TIER_ORDER.get(t["tier"], 9), t["ticker"]))
    return tickets


# ---------------------------------------------------------------------------
# Live price fetch (Polygon /prev — same idiom as build_run_accounting.py)


def fetch_live_prices(tickers: list[str], *, pace: float = 0.3, retries: int = 2) -> dict[str, float]:
    if not tickers:
        return {}
    try:
        from scripts._env import load_repo_env
        load_repo_env()
    except Exception:
        pass
    try:
        from tradingagents.dataflows.polygon_common import _make_request
    except Exception:
        return {}

    out: dict[str, float] = {}
    for t in tickers:
        for attempt in range(retries + 1):
            try:
                r = _make_request(f"/v2/aggs/ticker/{t}/prev", {"adjusted": "true"})
                res = (r.get("results") or [{}])[0]
                close = res.get("c")
                if close is not None:
                    out[t] = float(close)
                break
            except Exception:
                if attempt >= retries:
                    break
                time.sleep(1.0)
        time.sleep(pace)
    return out


# ---------------------------------------------------------------------------
# Truncation / formatting helpers


def _truncate(text: str | None, n: int = 140) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _fmt_money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _fmt_pct(v: float | None) -> str:
    return f"{v:+.2f}%" if v is not None else "—"


# ---------------------------------------------------------------------------
# Markdown rendering


def render_markdown(
    *,
    trade_date: str,
    run_artifacts: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    banner_problems: list[str],
    macro_snapshot: dict[str, Any] | None,
    skip_live: bool,
    portfolio_ctx: dict[str, Any] | None,
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# Friday decision packet — {trade_date}")
    lines.append("")
    lines.append(f"_Generated {generated_at}_")
    lines.append("")

    # 1. Header / go-no-go
    lines.append("## Go / no-go")
    lines.append("")
    if banner_problems:
        lines.append("> 🔴 **DATA QUALITY ISSUE — review before trading**")
        for p in banner_problems:
            lines.append(f"> - {p}")
        lines.append("")
    else:
        lines.append("> 🟢 All contributing runs have healthy options overlays.")
        lines.append("")

    tier_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    for t in tickets:
        tier_counts[t["tier"]] += 1
    lines.append(
        f"**Picks this week:** {len(tickets)} total — "
        f"B:{tier_counts['B']}  A:{tier_counts['A']}  C:{tier_counts['C']} "
        "(ranked B > A > C per empirical long-call ROI)"
    )
    lines.append("")
    lines.append(f"**Contributing matrix runs ({len(run_artifacts)}):** " +
                 ", ".join(f"`{a['run_id']}`" for a in run_artifacts))
    lines.append("")
    if macro_snapshot is not None:
        regime = macro_snapshot.get("regime", "unknown")
        lines.append(f"**Macro regime:** `{regime}`")
    else:
        lines.append("**Macro regime:** not built (no `runs/macro_{}.json`)".format(trade_date))
    lines.append("")

    # 2. Ranked ticket table
    lines.append("## Ranked tickets")
    lines.append("")
    if not tickets:
        lines.append("_No A/B/C picks across contributing runs._")
    else:
        lines.append(
            "| Ticker | Tier | Thesis | Anchor | Live | Gap% | Contract | Limit | Qty | "
            "Cons PT | Aggr PT | Exit rule | Flags |"
        )
        lines.append("|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---|---|")
        for t in tickets:
            gap_cell = _fmt_pct(t["gap_pct"])
            if t["gap_warn"]:
                gap_cell = f"⚠️ {gap_cell}"
            elif t["stale_warning"]:
                gap_cell = f"— _{t['stale_warning']}_"
            contract = "—"
            if t["strike"] is not None and t["expiry"]:
                contract = f"{t['strike']}C {t['expiry']}"
                if t["occ_symbol"]:
                    contract += f" (`{t['occ_symbol']}`)"
            flags = "; ".join(t["liquidity_flags"])
            if t["earnings_flag"]:
                flags = (flags + "; " if flags else "") + f"earnings {t['earnings_flag']}"
            lines.append(
                f"| {t['ticker']} | {t['tier']} | {_truncate(t['thesis'], 90)} | "
                f"{_fmt_money(t['anchor_price'])} | {_fmt_money(t['live_price'])} | {gap_cell} | "
                f"{contract} | {_fmt_money(t['limit_price_per_share'])} | "
                f"{t['qty'] if t['qty'] is not None else '—'} | "
                f"{_fmt_money(t['cons_pt'])} | {_fmt_money(t['aggr_pt'])} | "
                f"`{t['exit_rule']}` | {flags or '—'} |"
            )
    lines.append("")

    # 3. Freshness footer
    lines.append("## Freshness")
    lines.append("")
    for a in run_artifacts:
        gen_at = a["overlay"].get("generated_at") if a["overlay"] else None
        snap = a["overlay"].get("snapshot_date") if a["overlay"] else a["verdict"].get("snapshot_date")
        lines.append(
            f"- `{a['run_id']}`: overlay generated_at=`{gen_at or '(not recorded)'}`, "
            f"snapshot_date=`{snap or '(unknown)'}`"
        )
    if skip_live:
        lines.append("- Live prices: **not fetched** (`--skip-live`)")
    lines.append("")
    lines.append("> Quotes are ≥15-min delayed; refresh the options overlay same-day before entry:")
    lines.append(">")
    lines.append("> ```")
    lines.append("> .venv/bin/python scripts/build_options_overlay.py \\")
    lines.append(">     --matrix-run runs/<matrix_id> \\")
    lines.append(">     --strategy-mode long-call --long-call-delta 0.55")
    lines.append("> ```")
    lines.append("")

    # 4. Portfolio deltas
    if portfolio_ctx is not None:
        lines.append("## Portfolio deltas")
        lines.append("")
        held = portfolio_ctx.get("positions", [])
        if not held:
            lines.append("_No positions in positions.json._")
        else:
            lines.append("| Ticker | Tier @ entry → now | Δ | Flags |")
            lines.append("|---|---|---|---|")
            flags_by_ticker: dict[str, list[str]] = {}
            for f in portfolio_ctx.get("flags", []):
                flags_by_ticker.setdefault(f["ticker"], []).append(f["code"])
            for p in held:
                live = p["live"]
                tier_str = f"{live.get('matrix_tier_at_entry') or '—'} → {live.get('matrix_tier_now') or '—'}"
                delta = live.get("tier_delta", "n/a")
                pf = ", ".join(flags_by_ticker.get(p["ticker"], [])) or "—"
                lines.append(f"| {p['ticker']} | {tier_str} | {delta} | {pf} |")
        lines.append("")

    # 5. Approval checklist
    lines.append("## Approval checklist")
    lines.append("")
    if not tickets:
        lines.append("_Nothing to approve this week._")
    else:
        for t in tickets:
            lines.append(
                f"- [ ] **{t['ticker']}** ({t['tier']}): "
                f"`python scripts/approve_lean_signal.py --id {t['signal_id']}`"
            )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering (self-contained, no external assets)


_HTML_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }
h1 { font-size: 1.6rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 2px solid #eee; padding-bottom: 0.3rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; margin: 0.75rem 0; }
th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
th { background: #f5f5f5; position: sticky; top: 0; }
tr:nth-child(even) { background: #fafafa; }
.banner-red { background: #fde8e8; border: 1px solid #e53e3e; border-radius: 6px; padding: 0.75rem 1rem; }
.banner-green { background: #e8f9ee; border: 1px solid #38a169; border-radius: 6px; padding: 0.75rem 1rem; }
.tier-B { font-weight: 700; color: #2b6cb0; }
.tier-A { font-weight: 700; color: #6b46c1; }
.tier-C { font-weight: 700; color: #718096; }
.warn { color: #c05621; font-weight: 600; }
code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 0.85em; }
.checklist li { margin: 0.35rem 0; }
.small { color: #718096; font-size: 0.85rem; }
"""


def render_html(
    *,
    trade_date: str,
    run_artifacts: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    banner_problems: list[str],
    macro_snapshot: dict[str, Any] | None,
    skip_live: bool,
    portfolio_ctx: dict[str, Any] | None,
    generated_at: str,
) -> str:
    e = html.escape
    parts: list[str] = []
    parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    parts.append(f"<title>Friday packet — {e(trade_date)}</title>")
    parts.append(f"<style>{_HTML_CSS}</style></head><body>")
    parts.append(f"<h1>Friday decision packet — {e(trade_date)}</h1>")
    parts.append(f"<p class='small'>Generated {e(generated_at)}</p>")

    parts.append("<h2>Go / no-go</h2>")
    if banner_problems:
        parts.append("<div class='banner-red'><strong>🔴 DATA QUALITY ISSUE — review before trading</strong><ul>")
        for p in banner_problems:
            parts.append(f"<li>{e(p)}</li>")
        parts.append("</ul></div>")
    else:
        parts.append("<div class='banner-green'>🟢 All contributing runs have healthy options overlays.</div>")

    tier_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    for t in tickets:
        tier_counts[t["tier"]] += 1
    parts.append(
        f"<p><strong>Picks this week:</strong> {len(tickets)} total — "
        f"B:{tier_counts['B']}  A:{tier_counts['A']}  C:{tier_counts['C']}</p>"
    )
    parts.append(
        f"<p><strong>Contributing matrix runs ({len(run_artifacts)}):</strong> " +
        ", ".join(f"<code>{e(a['run_id'])}</code>" for a in run_artifacts) + "</p>"
    )
    if macro_snapshot is not None:
        parts.append(f"<p><strong>Macro regime:</strong> <code>{e(str(macro_snapshot.get('regime', 'unknown')))}</code></p>")
    else:
        parts.append(f"<p><strong>Macro regime:</strong> not built (no <code>runs/macro_{e(trade_date)}.json</code>)</p>")

    parts.append("<h2>Ranked tickets</h2>")
    if not tickets:
        parts.append("<p><em>No A/B/C picks across contributing runs.</em></p>")
    else:
        parts.append("<table><thead><tr>"
                      "<th>Ticker</th><th>Tier</th><th>Thesis</th><th>Anchor</th><th>Live</th>"
                      "<th>Gap%</th><th>Contract</th><th>Limit</th><th>Qty</th>"
                      "<th>Cons PT</th><th>Aggr PT</th><th>Exit rule</th><th>Flags</th>"
                      "</tr></thead><tbody>")
        for t in tickets:
            gap_cell = e(_fmt_pct(t["gap_pct"]))
            gap_class = " class='warn'" if t["gap_warn"] else ""
            if t["gap_warn"]:
                gap_cell = "⚠️ " + gap_cell
            elif t["stale_warning"]:
                gap_cell = f"— <span class='small'>{e(t['stale_warning'])}</span>"
            contract = "—"
            if t["strike"] is not None and t["expiry"]:
                contract = f"{e(str(t['strike']))}C {e(str(t['expiry']))}"
                if t["occ_symbol"]:
                    contract += f" <code>{e(t['occ_symbol'])}</code>"
            flags = "; ".join(t["liquidity_flags"])
            if t["earnings_flag"]:
                flags = (flags + "; " if flags else "") + f"earnings {t['earnings_flag']}"
            parts.append(
                f"<tr><td>{e(t['ticker'])}</td>"
                f"<td class='tier-{e(t['tier'])}'>{e(t['tier'])}</td>"
                f"<td>{e(_truncate(t['thesis'], 90))}</td>"
                f"<td>{e(_fmt_money(t['anchor_price']))}</td>"
                f"<td>{e(_fmt_money(t['live_price']))}</td>"
                f"<td{gap_class}>{gap_cell}</td>"
                f"<td>{contract}</td>"
                f"<td>{e(_fmt_money(t['limit_price_per_share']))}</td>"
                f"<td>{t['qty'] if t['qty'] is not None else '—'}</td>"
                f"<td>{e(_fmt_money(t['cons_pt']))}</td>"
                f"<td>{e(_fmt_money(t['aggr_pt']))}</td>"
                f"<td><code>{e(t['exit_rule'])}</code></td>"
                f"<td>{e(flags) or '—'}</td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>Freshness</h2><ul>")
    for a in run_artifacts:
        gen_at = a["overlay"].get("generated_at") if a["overlay"] else None
        snap = a["overlay"].get("snapshot_date") if a["overlay"] else a["verdict"].get("snapshot_date")
        parts.append(
            f"<li><code>{e(a['run_id'])}</code>: overlay generated_at="
            f"<code>{e(str(gen_at) if gen_at else '(not recorded)')}</code>, "
            f"snapshot_date=<code>{e(str(snap) if snap else '(unknown)')}</code></li>"
        )
    if skip_live:
        parts.append("<li>Live prices: <strong>not fetched</strong> (<code>--skip-live</code>)</li>")
    parts.append("</ul>")
    parts.append(
        "<p class='small'>Quotes are ≥15-min delayed; refresh the options overlay same-day before "
        "entry:<br><code>.venv/bin/python scripts/build_options_overlay.py --matrix-run "
        "runs/&lt;matrix_id&gt; --strategy-mode long-call --long-call-delta 0.55</code></p>"
    )

    if portfolio_ctx is not None:
        parts.append("<h2>Portfolio deltas</h2>")
        held = portfolio_ctx.get("positions", [])
        if not held:
            parts.append("<p><em>No positions in positions.json.</em></p>")
        else:
            flags_by_ticker: dict[str, list[str]] = {}
            for f in portfolio_ctx.get("flags", []):
                flags_by_ticker.setdefault(f["ticker"], []).append(f["code"])
            parts.append("<table><thead><tr><th>Ticker</th><th>Tier @ entry → now</th><th>Δ</th><th>Flags</th></tr></thead><tbody>")
            for p in held:
                live = p["live"]
                tier_str = f"{live.get('matrix_tier_at_entry') or '—'} → {live.get('matrix_tier_now') or '—'}"
                delta = live.get("tier_delta", "n/a")
                pf = ", ".join(flags_by_ticker.get(p["ticker"], [])) or "—"
                parts.append(f"<tr><td>{e(p['ticker'])}</td><td>{e(tier_str)}</td><td>{e(delta)}</td><td>{e(pf)}</td></tr>")
            parts.append("</tbody></table>")

    parts.append("<h2>Approval checklist</h2>")
    if not tickets:
        parts.append("<p><em>Nothing to approve this week.</em></p>")
    else:
        parts.append("<ul class='checklist'>")
        for t in tickets:
            parts.append(
                f"<li><input type='checkbox' disabled> <strong>{e(t['ticker'])}</strong> "
                f"({e(t['tier'])}): <code>python scripts/approve_lean_signal.py --id {e(t['signal_id'])}</code></li>"
            )
        parts.append("</ul>")

    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Orchestration


def build_packet(
    *,
    trade_date: str | None,
    runs_dir: Path,
    positions_path: Path | None,
    policy_path: Path,
    skip_live: bool,
) -> dict[str, Any]:
    resolved_date = trade_date or find_latest_trade_date(runs_dir)
    if resolved_date is None:
        raise ValueError(f"no matrix runs with a resolvable trade date found under {runs_dir}")

    run_paths = find_matrix_runs_for_trade_date(resolved_date, runs_dir)
    if not run_paths:
        raise ValueError(f"no matrix runs found for trade date {resolved_date} under {runs_dir}")

    run_artifacts = [load_run_artifacts(p) for p in run_paths]

    banner_problems: list[str] = []
    for a in run_artifacts:
        banner_problems.extend(overlay_health(a))

    macro_path = runs_dir / f"macro_{resolved_date}.json"
    macro_snapshot = _read_json(macro_path)

    account_value = None
    policy = None
    if policy_path.exists():
        policy = _read_json(policy_path)
    if positions_path is not None and positions_path.exists():
        pos_doc = _read_json(positions_path)
        if pos_doc:
            account_value = pos_doc.get("account_value")

    # Gather tickers for live pricing before assembling tickets.
    all_tickers: list[str] = []
    for a in run_artifacts:
        for ov in (a["overlay"].get("overlays") if a["overlay"] else []) or []:
            if ov.get("tier") in ("A", "B", "C"):
                all_tickers.append(str(ov["ticker"]).upper())
    live_prices: dict[str, float] = {}
    if not skip_live and all_tickers:
        live_prices = fetch_live_prices(sorted(set(all_tickers)))

    tickets = build_tickets(
        run_artifacts,
        resolved_date,
        account_value=account_value,
        policy=policy,
        live_prices=live_prices,
        skip_live=skip_live,
    )

    portfolio_ctx = None
    if positions_path is not None:
        try:
            portfolio_ctx = build_context(positions_path, policy_path, run_paths)
        except FileNotFoundError:
            portfolio_ctx = None

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    return {
        "trade_date": resolved_date,
        "run_artifacts": run_artifacts,
        "tickets": tickets,
        "banner_problems": banner_problems,
        "macro_snapshot": macro_snapshot,
        "skip_live": skip_live,
        "portfolio_ctx": portfolio_ctx,
        "generated_at": generated_at,
    }


def write_packet(packet: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "packet.md"
    html_path = output_dir / "packet.html"

    md = render_markdown(
        trade_date=packet["trade_date"],
        run_artifacts=packet["run_artifacts"],
        tickets=packet["tickets"],
        banner_problems=packet["banner_problems"],
        macro_snapshot=packet["macro_snapshot"],
        skip_live=packet["skip_live"],
        portfolio_ctx=packet["portfolio_ctx"],
        generated_at=packet["generated_at"],
    )
    html_doc = render_html(
        trade_date=packet["trade_date"],
        run_artifacts=packet["run_artifacts"],
        tickets=packet["tickets"],
        banner_problems=packet["banner_problems"],
        macro_snapshot=packet["macro_snapshot"],
        skip_live=packet["skip_live"],
        portfolio_ctx=packet["portfolio_ctx"],
        generated_at=packet["generated_at"],
    )
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_doc, encoding="utf-8")
    return md_path, html_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="Trade date YYYY-MM-DD (default: most recent found)")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--no-portfolio", action="store_true",
                        help="Skip the portfolio-deltas section even if positions-file exists")
    parser.add_argument("--skip-live", action="store_true",
                        help="Offline mode — no network calls for live prices")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Override output dir (default: runs/friday_packet_<date>/)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    positions_path = None if args.no_portfolio else args.positions_file

    packet = build_packet(
        trade_date=args.date,
        runs_dir=args.runs_dir,
        positions_path=positions_path,
        policy_path=args.policy_file,
        skip_live=args.skip_live,
    )

    output_dir = args.output_dir or (args.runs_dir / f"friday_packet_{packet['trade_date']}")
    md_path, html_path = write_packet(packet, output_dir)

    n_tickets = len(packet["tickets"])
    status = "🔴 ISSUES" if packet["banner_problems"] else "🟢 clean"
    print(f"✅ Wrote Friday packet for {packet['trade_date']} ({status}, {n_tickets} tickets)")
    print(f"   {md_path}")
    print(f"   {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
