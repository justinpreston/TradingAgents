"""Weekly cadence orchestrator — locks in the recommended workflow.

For long-call / multi-month-tenor trading, the recommended rhythm is:

  • Friday EOD weekly: run the screener
  • On-demand: matrix-run only on NEW tickers (not already in the catalog)
  • Same-day before entry: refresh the options overlay on the relevant matrix
    run (Polygon snapshot data ages within hours intraday)

This script automates phases 1–2 and reports phase 3 as next-step commands.
The SQLite catalog at runs/index.db (built by scripts/index_runs.py) is the
source of truth for "have I already matrix-ran this ticker this cycle?"

────────────────────────────────────────────────────────────────────────
Phases
────────────────────────────────────────────────────────────────────────

  1. SCREEN     — run the screener (or reuse one with --use-screener-run)
  2. INDEX      — re-build runs/index.db
  3. DIFF       — compare this week's top-N against tickers already
                  matrix-ran in the catalog. Categorize:
                    NEW      → recommend matrix run (concrete command)
                    REPEAT   → matrix can be skipped; verdict still valid
                                unless earnings happened this week
                    DROPPED  → previously screener-promoted, now off the list
  4. CHAIN      — (optional, with --chain) launch matrix on NEW tickers via
                  run_screener.py's existing --chain-top flag (re-uses
                  whichever runner is configured for chained matrix runs)
  5. REPORT     — print copy-paste commands for everything that wasn't
                  auto-launched (matrix, options overlay, HTML rebuild)

────────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────────

  # Standard weekly Friday EOD run (no chaining):
  .venv/bin/python scripts/weekly_workflow.py --top 25

  # Weekly + auto-launch matrix on the top-5 NEW tickers:
  .venv/bin/python scripts/weekly_workflow.py --top 25 --chain --chain-top 5

  # Use an existing screener run (e.g. you already ran it this morning):
  .venv/bin/python scripts/weekly_workflow.py \\
      --use-screener-run runs/screener_2026-05-01_0750 --top 25

  # Show what *would* happen without launching anything:
  .venv/bin/python scripts/weekly_workflow.py --dry-run

────────────────────────────────────────────────────────────────────────
Triggers that should override the schedule (run NOW, not on cadence)
────────────────────────────────────────────────────────────────────────

  • An existing pick reports earnings    → re-matrix that ticker
  • VIX spike >25 or SPX −5% in a week   → re-screen (regime shift breaks
                                            momentum signals)
  • Quarter-end (Apr/Jul/Oct/Jan)        → full screener refresh + accept
                                            new fundamental data lands
"""
from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"
CATALOG_DB = RUNS_DIR / "index.db"

# Tier presets — each preset defines a market-cap band, an ADV floor, and a
# minimum price. The ADV floor scales with mcap so the liquidity quality bar
# stays consistent across tiers (a $50M ADV is meaningful for a $5B mid-cap
# but trivial for a $500B mega-cap).
#
# When --tier is set, these defaults are applied unless explicitly overridden
# by the corresponding flag. When --tier is omitted, behavior is unchanged
# from before (mid-cap defaults preserved on the CLI parser itself).
TIER_PRESETS: dict[str, dict[str, float]] = {
    "mid": {
        "min_mcap": 2_000_000_000.0,
        "max_mcap": 10_000_000_000.0,
        "min_dollar_adv": 50_000_000.0,
        "min_price": 5.0,
    },
    "large": {
        "min_mcap": 10_000_000_000.0,
        "max_mcap": 200_000_000_000.0,
        "min_dollar_adv": 200_000_000.0,
        "min_price": 10.0,
    },
    "mega": {
        "min_mcap": 200_000_000_000.0,
        "max_mcap": 5_000_000_000_000.0,
        "min_dollar_adv": 500_000_000.0,
        "min_price": 20.0,
    },
}


def _apply_tier_preset(args: argparse.Namespace, *, tier: str | None,
                       user_set: set[str]) -> None:
    """Apply tier preset to ``args`` for any flag the user did NOT set.

    ``user_set`` is the set of dest names the user passed on the CLI (used
    to distinguish "I explicitly set this" from "argparse fed me the
    default"). Tier presets only override unspecified flags so the user can
    still tweak any individual knob (e.g. ``--tier mega --min-mcap 500B``).
    """
    if not tier:
        return
    preset = TIER_PRESETS.get(tier)
    if preset is None:
        raise ValueError(f"Unknown tier {tier!r}; expected one of {sorted(TIER_PRESETS)}")
    for dest, value in preset.items():
        if dest not in user_set:
            setattr(args, dest, value)


def _user_supplied_dests(argv: list[str]) -> set[str]:
    """Return the set of argparse `dest` names the user passed on argv.

    Used to honor user overrides through tier presets — if the user passed
    ``--min-mcap 5e9 --tier mega`` we keep their 5e9 instead of clobbering
    it with the mega preset's 200e9.
    """
    flag_to_dest = {
        "--min-mcap": "min_mcap",
        "--max-mcap": "max_mcap",
        "--min-dollar-adv": "min_dollar_adv",
        "--min-price": "min_price",
    }
    return {dest for flag, dest in flag_to_dest.items() if flag in argv}


def _hr(label: str = "") -> None:
    width = 72
    if label:
        print(f"\n── {label} " + "─" * (width - len(label) - 4))
    else:
        print("─" * width)


def _run(cmd: list[str], dry_run: bool = False) -> int:
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")
    if dry_run:
        print("    (skipped: --dry-run)")
        return 0
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def phase_screen(args: argparse.Namespace) -> Path:
    """Run the screener (or return an existing run path)."""
    _hr("Phase 1 · SCREEN")

    if args.use_screener_run:
        run_path = (REPO_ROOT / args.use_screener_run).resolve()
        if not run_path.is_dir() or not (run_path / "screener.json").exists():
            print(f"  ❌ Not a valid screener run: {run_path}", file=sys.stderr)
            sys.exit(1)
        print(f"  Reusing existing screener run: {run_path.relative_to(REPO_ROOT)}")
        return run_path

    cmd = [".venv/bin/python", "run_screener.py", "--top", str(args.top)]
    if args.target_date:
        cmd += ["--target-date", args.target_date]
    if args.min_mcap is not None:
        cmd += ["--min-mcap", str(args.min_mcap)]
    if args.max_mcap is not None:
        cmd += ["--max-mcap", str(args.max_mcap)]
    if args.min_dollar_adv is not None:
        cmd += ["--min-dollar-adv", str(args.min_dollar_adv)]
    if args.min_price is not None:
        cmd += ["--min-price", str(args.min_price)]
    if args.universe_limit is not None:
        cmd += ["--universe-limit", str(args.universe_limit)]
    if args.min_request_interval is not None:
        cmd += ["--min-request-interval", str(args.min_request_interval)]

    # When --tier is explicit, force the screener's output dir to embed the
    # tier label so that this run is traceable to the tier and so that
    # downstream filters (phase_diff, cross-tier HTML) can pattern-match.
    expected_screener_dir: Path | None = None
    if args.tier:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        expected_screener_dir = RUNS_DIR / f"screener_{args.tier}_{timestamp}"
        cmd += ["--output-dir", str(expected_screener_dir)]

    rc = _run(cmd, args.dry_run)
    if rc != 0:
        print(f"  ❌ Screener exited with code {rc}", file=sys.stderr)
        sys.exit(rc)

    if args.dry_run:
        return expected_screener_dir or RUNS_DIR / "<dry-run-screener>"

    # When tier-tagged, the screener wrote to the path we specified.
    if expected_screener_dir is not None and expected_screener_dir.is_dir():
        print(f"  → Screener output: {expected_screener_dir.relative_to(REPO_ROOT)}")
        return expected_screener_dir

    # Find the screener run that was just created (newest screener_* dir)
    screener_runs = sorted(RUNS_DIR.glob("screener_*"), key=lambda p: p.stat().st_mtime)
    if not screener_runs:
        print("  ❌ Could not locate the screener output directory", file=sys.stderr)
        sys.exit(1)
    latest = screener_runs[-1]
    print(f"  → Screener output: {latest.relative_to(REPO_ROOT)}")
    return latest


def phase_index(dry_run: bool) -> None:
    _hr("Phase 2 · INDEX")
    rc = _run([".venv/bin/python", "scripts/index_runs.py"], dry_run)
    if rc != 0:
        print(f"  ⚠ Indexer exited {rc}; continuing anyway", file=sys.stderr)


def phase_enrich_news(screener_run: Path, scorer: str, lookback_days: int,
                      ticker_limit: int | None, dry_run: bool) -> None:
    """Optional Phase 2.5 — write news_enrichment.json next to the screener.

    Default scorer is ``keyword`` (zero extra deps). ``finbert`` requires
    torch+transformers (~1.5-2GB); ``both`` runs them side-by-side for A/B.
    Output JSON is consumed by build_html_report.py for theme/sentiment
    surface tags.
    """
    _hr("Phase 2.5 · NEWS ENRICHMENT")
    cmd = [
        ".venv/bin/python", "scripts/build_news_enrichment.py",
        "--screener-run", str(screener_run),
        "--scorer", scorer,
        "--lookback-days", str(lookback_days),
    ]
    if ticker_limit:
        cmd += ["--ticker-limit", str(ticker_limit)]
    rc = _run(cmd, dry_run)
    if rc != 0:
        print(f"  ⚠ News enrichment exited {rc}; continuing without it",
              file=sys.stderr)


def phase_enrich_earnings(screener_run: Path, ticker_limit: int | None,
                          max_history_quarters: int, dry_run: bool) -> None:
    """Optional Phase 2.6 — write earnings_calendar.json next to the screener.

    yfinance-backed (free, fail-soft). The artifact is consumed by:
    - build_options_overlay.py (push expiry past earnings when horizon
      is long, to avoid IV crush)
    - build_html_report.py (📅 chip on screener watchlist)
    - news_enrichment_loader.py (extra "Next earnings in N days" line
      in the news analyst's pre-computed prefix)
    """
    _hr("Phase 2.6 · EARNINGS CALENDAR")
    cmd = [
        ".venv/bin/python", "scripts/build_earnings_calendar.py",
        "--screener-run", str(screener_run),
        "--max-history-quarters", str(max_history_quarters),
    ]
    if ticker_limit:
        cmd += ["--ticker-limit", str(ticker_limit)]
    rc = _run(cmd, dry_run)
    if rc != 0:
        print(f"  ⚠ Earnings calendar exited {rc}; continuing without it",
              file=sys.stderr)


def phase_macro(args: argparse.Namespace) -> tuple[dict | None, Path | None]:
    """Optional Phase 0 — write runs/macro_<DATE>.json and surface a banner.

    Advisory by default. ``--macro-gate halt`` can hard-block when the
    snapshot reports ``regime == "halt"``. ``--macro-gate defensive``
    halves chain-max-parallel when ``regime`` is ``defensive`` or
    ``halt`` (the user's --chain-max-parallel value, if explicitly set,
    is preserved with a warning).

    Returns ``(snapshot, path)``. Both are ``None`` when --enrich-macro
    is off.
    """
    if not getattr(args, "enrich_macro", False):
        return (None, None)
    _hr("Phase 0 · MACRO REGIME")
    today_iso = datetime.now().date().isoformat()
    out_path = REPO_ROOT / "runs" / f"macro_{today_iso}.json"
    cmd = [
        ".venv/bin/python", "scripts/build_macro_snapshot.py",
        "--output", str(out_path),
        "--quiet",
        "--vix-defensive", str(args.vix_defensive),
        "--vix-halt", str(args.vix_halt),
        "--spx-defensive-pct", str(args.spx_defensive_pct),
        "--spx-halt-pct", str(args.spx_halt_pct),
    ]
    rc = _run(cmd, args.dry_run)
    if rc != 0 or args.dry_run:
        print(f"  ⚠ Macro snapshot exited {rc}; continuing in advisory mode",
              file=sys.stderr)
        return (None, out_path if out_path.exists() else None)
    if not out_path.exists():
        print("  ⚠ Macro snapshot produced no artifact; continuing",
              file=sys.stderr)
        return (None, None)
    try:
        snap = json.loads(out_path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ Failed to read macro snapshot: {exc}", file=sys.stderr)
        return (None, out_path)

    from tradingagents.dataflows.macro_snapshot import render_banner_text
    print(f"  {render_banner_text(snap)}")
    print(f"  artifact: {out_path}")
    regime = snap.get("regime", "normal")
    gate = getattr(args, "macro_gate", "advisory")
    if regime == "halt" and gate == "halt":
        triggers = "; ".join(snap.get("triggers") or ["no triggers"])
        print(f"  ⛔ HALT regime + --macro-gate halt → exiting non-zero. "
              f"Triggers: {triggers}", file=sys.stderr)
        raise SystemExit(2)
    if regime in ("defensive", "halt") and gate in ("defensive", "halt"):
        if args.chain_max_parallel > 1:
            new_max = max(1, args.chain_max_parallel // 2)
            print(f"  ⚠ {regime.upper()} regime + --macro-gate {gate}: "
                  f"halving chain-max-parallel {args.chain_max_parallel} → "
                  f"{new_max}", file=sys.stderr)
            args.chain_max_parallel = new_max
    elif regime != "normal":
        print(f"  ⚠ {regime.upper()} regime detected (--macro-gate "
              f"{gate}); cadence proceeding unmodified.", file=sys.stderr)
    return (snap, out_path)


def _read_top_tickers(screener_run: Path, top_n: int) -> list[str]:
    """Read top-N tickers from a screener run."""
    top_path = screener_run / "top_tickers.txt"
    if top_path.exists():
        tickers = [t.strip() for t in top_path.read_text().splitlines() if t.strip()]
        return tickers[:top_n]

    json_path = screener_run / "screener.json"
    if json_path.exists():
        data = json.loads(json_path.read_text())
        candidates = data.get("candidates") or []
        return [c["ticker"] for c in candidates[:top_n]]

    return []


def phase_diff(screener_run: Path, top_n: int, dry_run: bool,
               *, tier: str | None = None) -> dict[str, list[str]]:
    """Compare current top-N against tickers already matrix-ran in the catalog.

    Returns dict with NEW / REPEAT / DROPPED ticker lists.

    When ``tier`` is set, the comparison is scoped to matrix runs whose
    ``run_id`` contains the tier label (e.g. ``matrix_mid_*``). This keeps
    a mid-cap diff from being polluted by yesterday's mega-cap matrix.
    """
    _hr("Phase 3 · DIFF (this week vs catalog)")

    if dry_run and not screener_run.exists():
        print("  (dry-run: would diff against runs/index.db)")
        return {"NEW": [], "REPEAT": [], "DROPPED": []}

    top_now = _read_top_tickers(screener_run, top_n)
    if not top_now:
        print("  ⚠ No tickers found in screener run; nothing to diff", file=sys.stderr)
        return {"NEW": [], "REPEAT": [], "DROPPED": []}

    last_matrix_picks: set[str] = set()
    last_run_id: str | None = None
    if CATALOG_DB.exists():
        conn = sqlite3.connect(CATALOG_DB)
        try:
            # Pick the most recent matrix run that actually produced PICKs.
            # An empty-pick run (e.g. a single-ticker smoke test) shouldn't
            # reset the cadence's view of the catalog.
            #
            # When tier is set, restrict to matrix runs whose run_id contains
            # the tier label. Pattern: run_ids like ``matrix_mid_weekly_*``
            # or ``matrix_<tier>_*`` are matched.
            if tier:
                cur = conn.execute(
                    """SELECT run_id FROM runs
                       WHERE run_type='matrix'
                         AND COALESCE(n_picks, 0) > 0
                         AND run_id LIKE ?
                       ORDER BY snapshot_date DESC, run_id DESC LIMIT 1""",
                    (f"matrix_{tier}_%",),
                )
            else:
                cur = conn.execute(
                    """SELECT run_id FROM runs
                       WHERE run_type='matrix' AND COALESCE(n_picks, 0) > 0
                       ORDER BY snapshot_date DESC, run_id DESC LIMIT 1"""
                )
            row = cur.fetchone()
            if row:
                last_run_id = row[0]
                last_matrix_picks = {
                    r[0]
                    for r in conn.execute(
                        "SELECT ticker FROM matrix_picks WHERE run_id=? AND classification='PICK'",
                        (last_run_id,),
                    )
                }
        finally:
            conn.close()

    top_set = set(top_now)
    new = [t for t in top_now if t not in last_matrix_picks]
    repeat = [t for t in top_now if t in last_matrix_picks]
    dropped = sorted(last_matrix_picks - top_set)

    print(f"  This week's top-{top_n}: {len(top_now)} tickers"
          + (f" · tier={tier}" if tier else ""))
    if last_run_id:
        print(f"  Compared against: {last_run_id} ({len(last_matrix_picks)} prior PICKs)")
    elif tier:
        print(f"  No prior {tier}-tier matrix runs in catalog → all tickers are NEW")
    else:
        print("  No prior matrix runs in catalog → all tickers are NEW")

    print()
    print(f"  🆕 NEW     ({len(new):>2}): {', '.join(new) if new else '—'}")
    print(f"  ♻  REPEAT  ({len(repeat):>2}): {', '.join(repeat) if repeat else '—'}")
    print(f"  ⊘  DROPPED ({len(dropped):>2}): {', '.join(dropped) if dropped else '—'}")

    return {"NEW": new, "REPEAT": repeat, "DROPPED": dropped, "last_run_id": last_run_id or ""}


def phase_chain(screener_run: Path, args: argparse.Namespace, diff: dict) -> None:
    """Optionally launch the matrix runner directly on NEW tickers.

    The chain runner must be ``run_copilot_matrix.py`` for the rest of the
    cadence to work — it is the only runner that produces the dual-frame
    verdict_ledger.json + cells/ structure that
    ``scripts/build_options_overlay.py`` and ``scripts/build_run_accounting.py``
    consume. Other runners (e.g. ``run_copilot_persona_aligned.py``) produce
    flat per-ticker state.json files that downstream cadence scripts can't
    digest.
    """
    _hr("Phase 4 · CHAIN")

    new_tickers = diff.get("NEW") or []
    if not args.chain:
        print("  (skipped: --chain not set; commands printed in Phase 5)")
        return

    if not new_tickers:
        print("  No NEW tickers — nothing to matrix-run.")
        return

    chain_n = min(args.chain_top, len(new_tickers))
    chain_tickers = new_tickers[:chain_n]
    runner_path = REPO_ROOT / args.chain_runner
    if not runner_path.exists():
        print(f"  ❌ Runner not found: {runner_path}")
        print(f"     Pass --chain-runner with a valid runner script.")
        return

    is_matrix_runner = runner_path.name == "run_copilot_matrix.py"
    if not is_matrix_runner:
        print(f"  ⚠ {runner_path.name} produces flat output that the rest of the")
        print(f"    cadence (options_overlay, accounting) cannot consume. Use")
        print(f"    run_copilot_matrix.py for cadence chaining.")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    tier_label = f"_{args.tier}" if args.tier else ""
    run_id = f"matrix{tier_label}_weekly_{timestamp}_chain"

    print(f"  Matrix-running top-{chain_n} NEW tickers via {runner_path.name}:")
    print(f"    {', '.join(chain_tickers)}")
    print(f"    → run_id = {run_id}")

    cmd = [".venv/bin/python", str(runner_path.relative_to(REPO_ROOT))]
    if is_matrix_runner:
        cmd += ["--tickers", *chain_tickers,
                "--run-id", run_id,
                "--stop-on-overweight", "0",
                "--max-parallel", str(args.chain_max_parallel)]
    else:
        # Persona-aligned runner takes positional tickers and only --run-id
        cmd += [*chain_tickers, "--run-id", run_id]
    if args.target_date:
        cmd += ["--date", args.target_date]

    # Auto-pass news enrichment when this run produced one earlier in Phase 2.5.
    # Both matrix and persona-aligned runners accept --news-enrichment; the
    # matrix runner sets the env var on every cell subprocess, while the
    # persona-aligned runner sets it for its own process.
    if args.enrich_news:
        ne_path = screener_run / "news_enrichment.json"
        if ne_path.exists():
            cmd += ["--news-enrichment", str(ne_path.relative_to(REPO_ROOT))]
            print(f"  📰 News enrichment will be injected into matrix cells: {ne_path.name}")

    # Auto-pass earnings calendar when Phase 2.6 produced one. Same plumbing
    # path as news enrichment: matrix runner accepts --earnings-calendar and
    # threads it via TRADINGAGENTS_EARNINGS_CALENDAR_PATH; the options
    # overlay step (auto-run by run_copilot_matrix.py post-run) will pick
    # up the file from the matrix-run dir directly.
    if args.enrich_earnings:
        ec_path = screener_run / "earnings_calendar.json"
        if ec_path.exists() and is_matrix_runner:
            cmd += ["--earnings-calendar", str(ec_path.relative_to(REPO_ROOT))]
            print(f"  📅 Earnings calendar will be injected into matrix cells: {ec_path.name}")

    rc = _run(cmd, args.dry_run)
    if rc == 0 and not args.dry_run:
        print(f"  ✅ Matrix complete. Re-index with: .venv/bin/python scripts/index_runs.py")
    elif rc != 0:
        print(f"  ⚠ Runner exited {rc}", file=sys.stderr)


def phase_report(screener_run: Path, args: argparse.Namespace, diff: dict) -> None:
    _hr("Phase 5 · NEXT STEPS")

    new_tickers = diff.get("NEW") or []
    chain_n = min(args.chain_top, len(new_tickers)) if args.chain_top else 5

    rel_screener = screener_run.relative_to(REPO_ROOT) if screener_run.exists() else Path("runs/<screener>")

    if new_tickers and not args.chain:
        target_list = " ".join(new_tickers[:chain_n])
        ts = datetime.now().strftime("%Y-%m-%d_%H%M")
        tier_label = f"_{args.tier}" if args.tier else ""
        print(f"\n  🎯 MATRIX (run on {chain_n} NEW tickers — bypasses re-screening):")
        runner_name = Path(args.chain_runner).name
        if runner_name == "run_copilot_matrix.py":
            print(
                f"     .venv/bin/python {args.chain_runner} \\\n"
                f"         --tickers {target_list} \\\n"
                f"         --run-id matrix{tier_label}_weekly_{ts}_chain \\\n"
                f"         --stop-on-overweight 0 --max-parallel {args.chain_max_parallel}"
            )
        else:
            print(
                f"     .venv/bin/python {args.chain_runner} \\\n"
                f"         {target_list} \\\n"
                f"         --run-id matrix{tier_label}_weekly_{ts}_chain"
            )
    elif new_tickers and args.chain:
        print("\n  🎯 MATRIX: launched in Phase 4 (above).")
    else:
        print("\n  🎯 MATRIX: nothing new to run — last week's verdicts still apply.")

    print("\n  📈 OPTIONS REFRESH (same-day before entry, on whichever matrix run):")
    print("     Note: run_copilot_matrix.py now auto-builds accounting + long-call")
    print("           options overlay + Chronos forecast overlay + HTML report")
    print("           after every matrix run. Use these only to refresh on an")
    print("           existing run intraday:")
    print(
        "     .venv/bin/python scripts/build_options_overlay.py \\\n"
        "         --matrix-run runs/<matrix_id> --strategy-mode long-call"
    )
    print(
        "     .venv/bin/python scripts/build_chronos_overlay.py \\\n"
        "         --matrix-run runs/<matrix_id>"
    )

    print("\n  📊 RE-INDEX after any new matrix or options overlay (auto-run by matrix):")
    print("     .venv/bin/python scripts/index_runs.py --query")

    print("\n  🌐 CROSS-RUN HTML (auto-built single-run report after each matrix; this is")
    print("      the multi-run aggregation for tracking trends across runs):")
    print(
        "     .venv/bin/python scripts/build_html_report.py \\\n"
        "         --runs runs/<matrix_id_1>:large runs/<matrix_id_2>:mid \\\n"
        "         --output runs/cross_run_$(date +%Y-%m-%d)/report.html"
    )

    print(f"\n  Screener output for this cycle: {rel_screener}")

    if diff.get("DROPPED"):
        print(f"\n  ⊘  Note: previously promoted tickers no longer in screener: "
              f"{', '.join(diff['DROPPED'])}")
        print("     Consider whether to close existing positions in those names.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", type=str, default=None,
                   choices=sorted(TIER_PRESETS.keys()),
                   help="Apply a market-cap-tier preset (mid|large|mega) for "
                        "mcap band, ADV floor, and min price. Tier-tagged runs "
                        "produce screener_<tier>_<ts>/ and matrix_<tier>_*_chain "
                        "run_ids; the diff phase is filtered to the same tier so "
                        "mid/large/mega cycles stay independent. Individual flags "
                        "(--min-mcap, --min-dollar-adv, etc.) override the preset.")
    p.add_argument("--top", type=int, default=25,
                   help="Top-N from screener to consider (default 25)")
    p.add_argument("--target-date", type=str, default=None,
                   help="Trading date for screener (YYYY-MM-DD); defaults to today")
    p.add_argument("--use-screener-run", type=str, default=None,
                   help="Skip phase 1 and reuse this screener run (e.g. runs/screener_2026-05-01_0750)")
    p.add_argument("--chain", action="store_true",
                   help="Auto-launch matrix on NEW tickers using --chain-runner")
    p.add_argument("--chain-top", type=int, default=5,
                   help="When chaining matrix, how many NEW tickers to feed (default 5)")
    p.add_argument("--chain-runner", type=str, default="run_copilot_matrix.py",
                   help="Runner script to invoke for chained matrix runs "
                        "(default run_copilot_matrix.py — produces verdict_ledger "
                        "+ cells/ that the rest of the cadence consumes)")
    p.add_argument("--chain-max-parallel", type=int, default=5,
                   help="Max parallel cells when chaining matrix runs (default 5)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without executing")
    # Universe-shaping pass-throughs to run_screener.py. Defaults match the
    # locked-in cadence that produced runs/screener_2026-05-01_0750
    # (mid-cap focus: $2B–$10B mcap, $50M ADV, $5 min price). Override here
    # if you want a different universe.
    p.add_argument("--min-mcap", type=float, default=2_000_000_000.0,
                   help="Min market cap in $ (default 2B)")
    p.add_argument("--max-mcap", type=float, default=10_000_000_000.0,
                   help="Max market cap in $ (default 10B = mid-cap focus)")
    p.add_argument("--min-dollar-adv", type=float, default=50_000_000.0,
                   help="Min 20d dollar ADV in $ (default 50M)")
    p.add_argument("--min-price", type=float, default=5.0,
                   help="Min last close in $ (default 5.0)")
    p.add_argument("--universe-limit", type=int, default=None,
                   help="Cap stage-2 enrichment for testing (default: no cap)")
    p.add_argument("--min-request-interval", type=float, default=None,
                   help="Min seconds between Polygon REST calls (default: screener decides)")
    # Optional Phase 2.5 — news enrichment artifact
    p.add_argument("--enrich-news", action="store_true",
                   help="Run Phase 2.5 to write news_enrichment.json next to the "
                        "screener run (sentiment + theme tags). Default off; "
                        "consumed by build_html_report.py for tag display when "
                        "the file exists.")
    p.add_argument("--news-scorer", choices=["keyword", "finbert", "both"],
                   default="finbert",
                   help="Sentiment scorer for --enrich-news. 'finbert' "
                        "(default) is ~3× more discriminating; requires "
                        "`pip install torch transformers` — auto-falls-back "
                        "to keyword with a warning if missing. 'keyword' "
                        "forces zero-dep mode. 'both' runs them side-by-side.")
    p.add_argument("--news-lookback-days", type=int, default=30,
                   help="How far back to pull headlines for --enrich-news (default 30)")
    # Optional Phase 2.6 — earnings calendar artifact
    p.add_argument("--enrich-earnings", action="store_true",
                   help="Run Phase 2.6 to write earnings_calendar.json next to "
                        "the screener run. yfinance-backed (free, fail-soft). "
                        "Used by the options overlay to push expiry past "
                        "earnings when horizon is long, by build_html_report.py "
                        "for the 📅 chip, and as a one-line prefix to the news "
                        "analyst's pre-computed context.")
    p.add_argument("--earnings-history-quarters", type=int, default=8,
                   help="How many past quarters of EPS surprises to capture "
                        "for beat-rate calculation (default 8)")
    # Optional Phase 0 — macro regime snapshot
    p.add_argument("--enrich-macro", action="store_true",
                   help="Run Phase 0 to write runs/macro_<DATE>.json with "
                        "VIX, SPX 5d return, and 10y/3m yield curve. "
                        "Advisory by default; see --macro-gate to enforce.")
    p.add_argument("--macro-gate",
                   choices=["advisory", "defensive", "halt"],
                   default="advisory",
                   help="Action mode for --enrich-macro. 'advisory' (default) "
                        "only logs and writes the file. 'defensive' halves "
                        "--chain-max-parallel when regime is defensive/halt. "
                        "'halt' additionally exits non-zero on halt regime "
                        "(forces manual review).")
    p.add_argument("--vix-defensive", type=float, default=25.0,
                   help="VIX threshold for defensive regime (default 25).")
    p.add_argument("--vix-halt", type=float, default=35.0,
                   help="VIX threshold for halt regime (default 35).")
    p.add_argument("--spx-defensive-pct", type=float, default=-5.0,
                   help="SPX 5d return %% for defensive regime (default -5).")
    p.add_argument("--spx-halt-pct", type=float, default=-8.0,
                   help="SPX 5d return %% for halt regime (default -8).")
    args = p.parse_args()

    # Apply tier preset BEFORE running phases — only for flags the user did
    # not explicitly set on the CLI.
    user_set = _user_supplied_dests(sys.argv[1:])
    _apply_tier_preset(args, tier=args.tier, user_set=user_set)

    started = datetime.now()
    print(f"╔══════════════════════════════════════════════════════════════════════╗")
    tier_suffix = f" · tier={args.tier}" if args.tier else ""
    print(f"║ Weekly TradingAgents cadence · "
          f"{started.strftime('%Y-%m-%d %H:%M %a')}{tier_suffix}".ljust(72) + "║")
    print(f"╚══════════════════════════════════════════════════════════════════════╝")
    if args.tier:
        print(f"  tier preset → mcap=${args.min_mcap/1e9:.1f}B-${args.max_mcap/1e9:.1f}B "
              f"· ADV ≥ ${args.min_dollar_adv/1e6:.0f}M · price ≥ ${args.min_price:.0f}")

    macro_snap, macro_path = phase_macro(args)

    screener_run = phase_screen(args)
    phase_index(args.dry_run)
    if args.enrich_news:
        phase_enrich_news(
            screener_run,
            scorer=args.news_scorer,
            lookback_days=args.news_lookback_days,
            ticker_limit=args.top,
            dry_run=args.dry_run,
        )
    if args.enrich_earnings:
        phase_enrich_earnings(
            screener_run,
            ticker_limit=args.top,
            max_history_quarters=args.earnings_history_quarters,
            dry_run=args.dry_run,
        )
    diff = phase_diff(screener_run, args.top, args.dry_run, tier=args.tier)
    phase_chain(screener_run, args, diff)
    phase_report(screener_run, args, diff)

    elapsed = datetime.now() - started
    _hr()
    print(f"  Done in {elapsed.total_seconds():.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
