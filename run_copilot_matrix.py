"""Parallel matrix runner: ticker × risk_profile cells over a thread pool.

Goal: from a screener's top-N candidate list, identify high-conviction
"ADI-pattern" picks — names that come back Overweight/Buy under the
aggressive risk profile AND don't come back Underweight/Sell under the
conservative profile. The aggressive frame surfaces opportunity; the
conservative frame stress-tests against the bear case.

Architecture
------------
* Each (ticker, profile) is a fully independent cell. Cells are dispatched
  to a ThreadPoolExecutor of subprocesses (one per cell), so the existing
  serial runner does the heavy lifting and we get OS-level isolation —
  no thread-safety concerns around the trading-agents graph internals.
* Stage A runs the *primary* profile (default: aggressive) on all tickers.
  Stage B runs the *secondary* profile (default: conservative) only on the
  Stage-A Overweight/Buy candidates — the ADI-pattern filter. This cuts
  cost roughly in half versus a full N×M matrix.
* As each cell completes, ``live_results.md`` is regenerated and an event
  is appended to ``events.jsonl`` so a watching shell can ``tail -f`` it.
* ``--stop-on-overweight N`` short-circuits Stage A once N tickers have
  cleared the bar; the in-flight cells drain and Stage B runs against the
  N picks.

Output
------
runs/<matrix_id>/
  manifest.json
  events.jsonl
  matrix.json              # full source of truth
  matrix.md                # rendered table
  live_results.md          # streaming, regenerated after every cell
  high_conviction.md       # the ADI-pattern picks, ranked
  cells/<profile>/<TICKER>/
    manifest.json          # the per-cell sub-runner's own manifest
    <TICKER>.log
    <TICKER>.state.json    # PM resynth-ready snapshot

Usage
-----
    # Use the latest screener output, top 25, default staged behavior
    python run_copilot_matrix.py --tickers-from-latest-screener --top 25

    # Explicit ticker list
    python run_copilot_matrix.py --tickers ABNB TVTX DAR APD ADI

    # Full matrix without early-stop, all three profiles
    python run_copilot_matrix.py --tickers-from-latest-screener \\
        --profiles aggressive conservative --no-stage-b \\
        --stop-on-overweight 0

    # Resume a partially-completed run
    python run_copilot_matrix.py --tickers ABNB TVTX --run-id matrix_2026-04-30_top25
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from tradingagents.dataflows.utils import resolve_trade_date

load_dotenv()


REPO_ROOT = Path(__file__).resolve().parent

PROFILES_DEFAULT = ("aggressive", "conservative")
SUB_RUNNER = "run_copilot_aggressive_aligned.py"

# ADI-pattern thresholds: aggressive verdict that counts as a green light
PROMOTE_RATINGS = {"Overweight", "Buy"}
# Conservative verdicts that DISQUALIFY a name from "high conviction"
VETO_RATINGS = {"Underweight", "Sell"}


@dataclass
class CellResult:
    ticker: str
    profile: str
    status: str  # "ok" | "error" | "skipped"
    rating: Optional[str] = None
    price_target: Optional[str] = None
    elapsed_s: Optional[float] = None
    error: Optional[str] = None
    cell_dir: Optional[str] = None
    log_size: Optional[int] = None


# ---------------------------------------------------------------------------
# Tickers + run-id resolution
# ---------------------------------------------------------------------------

def _latest_screener_dir(base: Path = Path("runs")) -> Optional[Path]:
    candidates = sorted(base.glob("screener_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        if (c / "screener.json").exists():
            return c
    return None


def _load_screener_tickers(screener_dir: Path, top: int) -> list[str]:
    data = json.loads((screener_dir / "screener.json").read_text(encoding="utf-8"))
    candidates = data.get("candidates") or []
    return [c["ticker"].upper() for c in candidates[:top]]


def _resolve_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            token = subprocess.check_output(
                ["gh", "auth", "token"], stderr=subprocess.DEVNULL, text=True
            ).strip()
            if token:
                return token
        except subprocess.CalledProcessError:
            pass
    raise SystemExit(
        "No GitHub token available. Run `gh auth login` (Copilot subscription required)."
    )


# ---------------------------------------------------------------------------
# Cell dispatch
# ---------------------------------------------------------------------------

_RATING_RE = re.compile(r"\*\*Rating\*\*:\s*([A-Za-z]+)")
_PT_RE = re.compile(r"\*\*Price Target\*\*:\s*([\d.,]+)")


def _extract_from_state(state_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Pull rating + price target out of the saved state.json's final_trade_decision."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    text = data.get("final_trade_decision") or ""
    rating = _RATING_RE.search(text)
    pt = _PT_RE.search(text)
    return (rating.group(1) if rating else None,
            pt.group(1) if pt else None)


def _run_cell(
    ticker: str,
    profile: str,
    trade_date: str,
    matrix_dir: Path,
    env: dict,
    no_dashboard: bool,
) -> CellResult:
    cell_dir = matrix_dir / "cells" / profile / ticker
    cell_dir.mkdir(parents=True, exist_ok=True)

    state_file = cell_dir / f"{ticker}.state.json"
    log_file = cell_dir / f"{ticker}.log"

    # Resume support: if state.json exists with a parseable rating, skip
    if state_file.exists():
        rating, pt = _extract_from_state(state_file)
        if rating:
            return CellResult(
                ticker=ticker, profile=profile, status="skipped",
                rating=rating, price_target=pt, elapsed_s=0.0,
                cell_dir=str(cell_dir),
                log_size=(log_file.stat().st_size if log_file.exists() else 0),
            )

    # Build the sub-runner command. We re-use run_copilot_aggressive_aligned.py
    # with --run-id pointing inside the matrix dir so its outputs land in cell_dir.
    sub_run_id = f"{matrix_dir.name}/cells/{profile}/{ticker}"
    cmd = [
        sys.executable, SUB_RUNNER,
        "--date", trade_date,
        "--run-id", sub_run_id,
    ]
    # 'neutral' is a valid sub-runner choice; it injects no addendum.
    cmd += ["--risk-profile", profile]
    if no_dashboard:
        cmd += ["--no-dashboard"]
    cmd += [ticker]

    started = time.time()
    try:
        # stdin=DEVNULL is critical: when the matrix orchestrator is launched
        # detached (nohup ... &) and its parent shell exits, the inherited
        # stdin file descriptor can become invalid. Children inheriting that
        # broken descriptor crash with "Fatal Python error: init_sys_streams:
        # can't initialize sys standard streams. OSError: [Errno 9] Bad file
        # descriptor" before any user code runs.
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path.cwd()),
        )
    except Exception as exc:  # noqa: BLE001
        return CellResult(
            ticker=ticker, profile=profile, status="error",
            error=f"subprocess failed: {exc}",
            elapsed_s=time.time() - started,
            cell_dir=str(cell_dir),
        )
    elapsed = time.time() - started

    # Persist the sub-runner stdout/stderr alongside the cell for forensics
    (cell_dir / "_subprocess.out").write_text(completed.stdout or "", encoding="utf-8")
    if completed.stderr:
        (cell_dir / "_subprocess.err").write_text(completed.stderr, encoding="utf-8")

    if completed.returncode != 0:
        # Best-effort: still try to extract a rating in case the run partially completed
        rating, pt = (None, None)
        if state_file.exists():
            rating, pt = _extract_from_state(state_file)
        return CellResult(
            ticker=ticker, profile=profile,
            status="error" if rating is None else "ok",
            rating=rating, price_target=pt,
            error=f"exit {completed.returncode}",
            elapsed_s=elapsed,
            cell_dir=str(cell_dir),
            log_size=(log_file.stat().st_size if log_file.exists() else 0),
        )

    rating, pt = _extract_from_state(state_file)
    return CellResult(
        ticker=ticker, profile=profile,
        status="ok" if rating else "error",
        rating=rating, price_target=pt,
        elapsed_s=elapsed,
        cell_dir=str(cell_dir),
        log_size=(log_file.stat().st_size if log_file.exists() else 0),
        error=None if rating else "rating not parseable from state.json",
    )


# ---------------------------------------------------------------------------
# Live writer + events stream
# ---------------------------------------------------------------------------

class _LiveWriter:
    def __init__(self, matrix_dir: Path, manifest: dict):
        self._matrix_dir = matrix_dir
        self._manifest = manifest
        self._lock = threading.Lock()
        self._results: dict[tuple[str, str], CellResult] = {}
        self._events_path = matrix_dir / "events.jsonl"

    def append(self, result: CellResult, stage: str) -> None:
        with self._lock:
            self._results[(result.profile, result.ticker)] = result
            with self._events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "stage": stage,
                    **asdict(result),
                }) + "\n")
            self._regen_live()

    def get_completed(self, profile: str) -> dict[str, CellResult]:
        with self._lock:
            return {
                t: r for (p, t), r in self._results.items()
                if p == profile and r.rating is not None
            }

    def all_results(self) -> dict[tuple[str, str], CellResult]:
        with self._lock:
            return dict(self._results)

    def _regen_live(self) -> None:
        # Group by ticker to show per-ticker ratings across profiles
        profiles = self._manifest["profiles"]
        tickers = self._manifest["tickers"]

        lines = []
        lines.append(f"# Matrix run · live · {self._manifest['run_id']}")
        lines.append("")
        lines.append(f"- Trade date: **{self._manifest['trade_date']}**  ({self._manifest['trade_date_label']})")
        lines.append(f"- Profiles: {', '.join(profiles)}")
        lines.append(f"- Started: {self._manifest['started_at']}")
        lines.append(f"- Tickers: {len(tickers)}")
        completed = sum(1 for r in self._results.values() if r.status in ("ok", "skipped"))
        errored = sum(1 for r in self._results.values() if r.status == "error")
        total = len(profiles) * len(tickers)
        lines.append(f"- Cells: {completed}/{total} ok, {errored} errored")
        lines.append("")

        # Per-ticker rollup
        header = "| Ticker | " + " | ".join(profiles) + " |"
        sep = "|---|" + "|".join(["---"] * len(profiles)) + "|"
        lines.append(header)
        lines.append(sep)
        for t in tickers:
            row = [f"**{t}**"]
            for p in profiles:
                r = self._results.get((p, t))
                if r is None:
                    row.append("—")
                elif r.status == "error":
                    row.append("❌")
                elif r.rating:
                    pt = f" / {r.price_target}" if r.price_target else ""
                    marker = ""
                    if r.rating in PROMOTE_RATINGS:
                        marker = " 🟢"
                    elif r.rating in VETO_RATINGS:
                        marker = " 🔴"
                    row.append(f"{r.rating}{pt}{marker}")
                else:
                    row.append("…")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        lines.append("Legend: 🟢 = Overweight/Buy · 🔴 = Underweight/Sell · — = pending · … = completed but no rating · ❌ = error")
        lines.append("")
        (self._matrix_dir / "live_results.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Final reports
# ---------------------------------------------------------------------------

def _write_matrix_json(matrix_dir: Path, manifest: dict, results: dict) -> None:
    payload = {
        "manifest": manifest,
        "cells": [
            {
                "profile": p, "ticker": t,
                **{k: v for k, v in asdict(r).items() if k not in ("profile", "ticker")},
            }
            for (p, t), r in sorted(results.items())
        ],
    }
    (matrix_dir / "matrix.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_matrix_md(matrix_dir: Path, manifest: dict, results: dict) -> None:
    profiles = manifest["profiles"]
    tickers = manifest["tickers"]
    lines = []
    lines.append(f"# Matrix run · {manifest['run_id']}")
    lines.append("")
    lines.append(f"- Trade date: **{manifest['trade_date']}**  ({manifest['trade_date_label']})")
    lines.append(f"- Profiles: {', '.join(profiles)}")
    lines.append(f"- Tickers: {len(tickers)}")
    if manifest.get("stop_on_overweight"):
        lines.append(f"- Stop-on-overweight: {manifest['stop_on_overweight']}")
    lines.append("")

    header = "| Ticker | " + " | ".join(f"{p} (PT)" for p in profiles) + " |"
    sep = "|---|" + "|".join(["---"] * len(profiles)) + "|"
    lines.append(header)
    lines.append(sep)
    for t in tickers:
        row = [f"**{t}**"]
        for p in profiles:
            r = results.get((p, t))
            if r is None or r.status == "skipped" and not r.rating:
                row.append("—")
            elif r.status == "error":
                row.append("❌ error")
            elif r.rating:
                pt = f" / {r.price_target}" if r.price_target else ""
                row.append(f"{r.rating}{pt}")
            else:
                row.append("…")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    (matrix_dir / "matrix.md").write_text("\n".join(lines), encoding="utf-8")


def _write_high_conviction(
    matrix_dir: Path, manifest: dict, results: dict,
    primary: str, secondary: Optional[str],
) -> list[str]:
    """Emit the ADI-pattern picks: Overweight/Buy in primary AND not Underweight/Sell in secondary."""
    high_conv = []
    for t in manifest["tickers"]:
        primary_r = results.get((primary, t))
        if not primary_r or primary_r.rating not in PROMOTE_RATINGS:
            continue
        if secondary:
            secondary_r = results.get((secondary, t))
            if secondary_r and secondary_r.rating in VETO_RATINGS:
                continue
        high_conv.append(t)

    lines = []
    lines.append(f"# High-conviction picks · {manifest['run_id']}")
    lines.append("")
    if not high_conv:
        lines.append("_No tickers passed the high-conviction filter._")
        lines.append("")
        lines.append(
            f"Filter: **Overweight/Buy in `{primary}`**" +
            (f" AND **not Underweight/Sell in `{secondary}`**" if secondary else "")
        )
    else:
        lines.append(
            f"Filter: Overweight/Buy in `{primary}`" +
            (f" AND not Underweight/Sell in `{secondary}`" if secondary else "")
        )
        lines.append("")
        lines.append(f"**{len(high_conv)} pick(s):** {', '.join(high_conv)}")
        lines.append("")
        for t in high_conv:
            primary_r = results[(primary, t)]
            lines.append(f"## {t}")
            lines.append("")
            lines.append(f"- `{primary}`: **{primary_r.rating}**" +
                         (f" · PT {primary_r.price_target}" if primary_r.price_target else ""))
            if secondary:
                sec_r = results.get((secondary, t))
                if sec_r and sec_r.rating:
                    lines.append(f"- `{secondary}`: **{sec_r.rating}**" +
                                 (f" · PT {sec_r.price_target}" if sec_r.price_target else ""))
                elif sec_r and sec_r.status == "error":
                    lines.append(f"- `{secondary}`: ❌ error")
                else:
                    lines.append(f"- `{secondary}`: not run")
            if primary_r.cell_dir:
                lines.append(f"- Full thesis: `{primary_r.cell_dir}/{t}.log`")
            lines.append("")
    (matrix_dir / "high_conviction.md").write_text("\n".join(lines), encoding="utf-8")
    return high_conv


# ---------------------------------------------------------------------------
# Stage orchestration
# ---------------------------------------------------------------------------

def _run_stage(
    profile: str,
    tickers: list[str],
    trade_date: str,
    matrix_dir: Path,
    env: dict,
    max_parallel: int,
    no_dashboard: bool,
    writer: _LiveWriter,
    stage_label: str,
    stop_on_overweight: int,
) -> dict[str, CellResult]:
    """Run all (ticker, profile) cells for one stage. Returns ticker→result."""
    results: dict[str, CellResult] = {}
    overweight_seen = 0
    sys.stdout.write(
        f"\n┌─ Stage {stage_label} · profile={profile} · "
        f"{len(tickers)} tickers · parallel={max_parallel}\n"
    )
    sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_run_cell, t, profile, trade_date, matrix_dir, env, no_dashboard): t
            for t in tickers
        }
        try:
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    result = CellResult(
                        ticker=t, profile=profile, status="error",
                        error=f"future raised: {exc}",
                    )
                results[t] = result
                writer.append(result, stage=stage_label)
                marker = "🟢" if result.rating in PROMOTE_RATINGS else (
                    "🔴" if result.rating in VETO_RATINGS else "·"
                )
                elapsed = f"{result.elapsed_s:.0f}s" if result.elapsed_s else "—"
                pt = f" PT={result.price_target}" if result.price_target else ""
                sys.stdout.write(
                    f"│ {marker} {t:<6} {profile:<13} → "
                    f"{result.rating or result.status:<13} "
                    f"({elapsed}{pt}){' ['+result.error+']' if result.error else ''}\n"
                )
                sys.stdout.flush()
                if result.rating in PROMOTE_RATINGS:
                    overweight_seen += 1
                if stop_on_overweight and overweight_seen >= stop_on_overweight:
                    sys.stdout.write(
                        f"│ ✋ stop-on-overweight={stop_on_overweight} reached. "
                        f"Cancelling pending cells.\n"
                    )
                    sys.stdout.flush()
                    for f, _ in futures.items():
                        if not f.done():
                            f.cancel()
                    break
        finally:
            sys.stdout.write("└────────────────────────────────────────────────────────────\n")
            sys.stdout.flush()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--tickers", nargs="+",
        help="Explicit ticker list. Mutually exclusive with --tickers-from-latest-screener.",
    )
    src.add_argument(
        "--tickers-from-latest-screener", action="store_true",
        help="Pull tickers from the most-recent runs/screener_*/screener.json.",
    )
    p.add_argument(
        "--screener-run", default=None,
        help=(
            "Path to a screener run dir (e.g., runs/screener_2026-05-06_1027) to record "
            "as lineage in the manifest. Auto-set when --tickers-from-latest-screener is "
            "used; pass explicitly with --tickers to attribute an ad-hoc matrix to a "
            "specific screener (used by auto-report's accounting step)."
        ),
    )
    p.add_argument("--top", type=int, default=25, help="Truncate ticker list to top N. Default: 25.")
    p.add_argument(
        "--profiles", nargs="+", default=list(PROFILES_DEFAULT),
        choices=["aggressive", "neutral", "conservative"],
        help="Risk profiles to evaluate. Default: aggressive conservative.",
    )
    p.add_argument(
        "--max-parallel", type=int, default=5,
        help="Max concurrent cells. Default 5; raise carefully (rate limits).",
    )
    p.add_argument(
        "--stop-on-overweight", type=int, default=5,
        help="Stop Stage A once N tickers come back Overweight/Buy. 0 disables.",
    )
    p.add_argument(
        "--no-stage-b", action="store_true",
        help="Skip the secondary-profile stage. Stage A runs alone.",
    )
    p.add_argument("--date", default=None, help="Trade date YYYY-MM-DD. Default: today.")
    p.add_argument("--run-id", default=None)
    p.add_argument(
        "--no-dashboard", action="store_true",
        help="Disabled dashboards in sub-runners. Default: on (more legible logs).",
    )
    p.add_argument(
        "--no-auto-report", action="store_true",
        help=(
            "Skip auto-generation of accounting + options overlay + HTML report. "
            "Default: ON — every matrix run produces a consumable HTML report."
        ),
    )
    p.add_argument(
        "--long-call-delta", type=float, default=0.55,
        help="Target delta for the auto-built long-call overlay. Default 0.55 (slightly ITM).",
    )
    p.add_argument(
        "--news-enrichment", default=None,
        help="Path to a news_enrichment.json file. When set, every cell "
             "subprocess inherits TRADINGAGENTS_NEWS_ENRICHMENT_PATH so the "
             "news analyst prepends pre-computed FinBERT polarity + theme "
             "tags to its system message.",
    )
    return p.parse_args()


def _run_auto_report(matrix_dir: Path, long_call_delta: float) -> None:
    """Run accounting → options overlay → HTML report after a successful matrix.

    Each step is best-effort: a failure logs a warning but does not abort the
    others. The matrix run output itself is already on disk; these are
    consumability layers on top of it.
    """
    sys.stdout.write("\n📦 Auto-report: building accounting + options overlay + HTML…\n")
    matrix_rel = matrix_dir.relative_to(REPO_ROOT) if matrix_dir.is_absolute() else matrix_dir
    py = sys.executable

    screener_run: Optional[str] = None
    try:
        manifest = json.loads((matrix_dir / "manifest.json").read_text(encoding="utf-8"))
        screener_run = manifest.get("screener_run")
    except (OSError, json.JSONDecodeError):
        screener_run = None

    accounting_cmd = [py, "scripts/build_run_accounting.py", "--matrix-run", str(matrix_rel)]
    if screener_run:
        accounting_cmd += ["--screener-run", screener_run]
    else:
        sys.stdout.write("    (no screener_run recorded in manifest — accounting will run without lineage)\n")

    steps = [
        ("accounting", accounting_cmd),
        (
            "options-overlay (long-call)",
            [
                py, "scripts/build_options_overlay.py",
                "--matrix-run", str(matrix_rel),
                "--strategy-mode", "long-call",
                "--long-call-delta", str(long_call_delta),
            ],
        ),
        (
            "index-runs",
            [py, "scripts/index_runs.py"],
        ),
    ]
    for label, cmd in steps:
        sys.stdout.write(f"  → {label}…\n")
        rc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL
        ).returncode
        if rc != 0:
            sys.stdout.write(f"    ⚠ {label} exited {rc} — continuing.\n")

    # HTML report — always single-run for the matrix that just completed.
    # Cross-run aggregation is a separate manual concern.
    today = datetime.now().strftime("%Y-%m-%d")
    html_dir = REPO_ROOT / "runs" / f"cross_run_{today}"
    html_path = html_dir / "report.html"
    label = matrix_dir.name.replace("matrix_", "").replace("_", "-") or "run"
    sys.stdout.write(f"  → html-report → {html_path.relative_to(REPO_ROOT)}…\n")
    rc = subprocess.run(
        [
            py, "scripts/build_html_report.py",
            "--runs", f"{matrix_rel}:{label}",
            "--output", str(html_path),
        ],
        cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
    ).returncode
    if rc == 0:
        sys.stdout.write(f"    ✅ HTML: {html_path}\n")
    else:
        sys.stdout.write(f"    ⚠ html-report exited {rc}\n")


def main() -> int:
    args = _parse_args()

    # Resolve tickers
    screener_dir: Optional[Path] = None
    if args.tickers_from_latest_screener:
        screener_dir = _latest_screener_dir()
        if not screener_dir:
            sys.stderr.write("❌ No screener output found in runs/screener_*\n")
            return 2
        tickers = _load_screener_tickers(screener_dir, args.top)
        sys.stdout.write(f"📊 Loaded {len(tickers)} tickers from {screener_dir}\n")
    elif args.tickers:
        tickers = [t.upper().strip() for t in args.tickers][: args.top]
    else:
        sys.stderr.write("❌ Provide either --tickers or --tickers-from-latest-screener\n")
        return 2

    # Manual --screener-run takes precedence; only validated when actually set so
    # ad-hoc matrix runs (like a single-ticker re-run) work without a screener.
    if args.screener_run:
        sr = Path(args.screener_run)
        if not sr.is_dir():
            sys.stderr.write(f"❌ --screener-run dir not found: {sr}\n")
            return 2
        if not (sr / "screener.json").is_file():
            sys.stderr.write(f"❌ --screener-run dir missing screener.json: {sr}\n")
            return 2
        screener_dir = sr

    if not tickers:
        sys.stderr.write("❌ No tickers resolved.\n")
        return 2

    try:
        trade_date, date_label = resolve_trade_date(args.date)
    except ValueError as exc:
        sys.stderr.write(f"❌ {exc}\n")
        return 2

    profiles = list(args.profiles)
    primary = profiles[0]
    secondary = profiles[1] if len(profiles) > 1 and not args.no_stage_b else None

    run_id = args.run_id or f"matrix_{trade_date}_top{len(tickers)}"
    matrix_dir = Path("runs") / run_id
    matrix_dir.mkdir(parents=True, exist_ok=True)
    (matrix_dir / "cells").mkdir(exist_ok=True)

    # Sub-runner dashboards from N concurrent processes would clobber the
    # terminal — always disable them; the matrix writer is the UI.
    no_dashboard = True
    _ = args.no_dashboard

    manifest = {
        "run_id": run_id,
        "trade_date": trade_date,
        "trade_date_label": date_label,
        "system_date_at_run": datetime.now().date().isoformat(),
        "tickers": tickers,
        "profiles": profiles,
        "primary_profile": primary,
        "secondary_profile": secondary,
        "max_parallel": args.max_parallel,
        "stop_on_overweight": args.stop_on_overweight,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "screener_run": (
            str(screener_dir.relative_to(REPO_ROOT))
            if screener_dir is not None and screener_dir.is_absolute() and REPO_ROOT in screener_dir.parents
            else (str(screener_dir) if screener_dir is not None else None)
        ),
    }
    (matrix_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env["GITHUB_TOKEN"] = _resolve_github_token()
    if args.news_enrichment:
        ne_path = Path(args.news_enrichment).resolve()
        if not ne_path.exists():
            sys.stdout.write(f"⚠ --news-enrichment path not found: {ne_path} (continuing without)\n")
        else:
            env["TRADINGAGENTS_NEWS_ENRICHMENT_PATH"] = str(ne_path)
            sys.stdout.write(f"📰 News enrichment active: {ne_path}\n")

    sys.stdout.write(f"\n╭─ Matrix run · {run_id}\n")
    sys.stdout.write(f"│ Trade date  : {trade_date}  ({date_label})\n")
    sys.stdout.write(f"│ Tickers     : {len(tickers)}  ({', '.join(tickers[:8])}{'…' if len(tickers) > 8 else ''})\n")
    sys.stdout.write(f"│ Profiles    : {' → '.join(profiles)}\n")
    sys.stdout.write(f"│ Concurrency : {args.max_parallel}\n")
    sys.stdout.write(f"│ Stop@OW     : {args.stop_on_overweight or 'disabled'}\n")
    sys.stdout.write(f"│ Output      : {matrix_dir.resolve()}\n")
    sys.stdout.write("╰────────────────────────────────────────────────────────────\n")
    sys.stdout.flush()

    writer = _LiveWriter(matrix_dir, manifest)
    overall_started = time.time()

    # Stage A: primary profile on all tickers
    stage_a_results = _run_stage(
        profile=primary,
        tickers=tickers,
        trade_date=trade_date,
        matrix_dir=matrix_dir,
        env=env,
        max_parallel=args.max_parallel,
        no_dashboard=no_dashboard,
        writer=writer,
        stage_label="A",
        stop_on_overweight=args.stop_on_overweight,
    )

    # Stage B: secondary profile, but only on Stage-A promotes
    stage_b_results: dict[str, CellResult] = {}
    if secondary:
        promotes = [
            t for t in tickers
            if (r := stage_a_results.get(t)) and r.rating in PROMOTE_RATINGS
        ]
        if promotes:
            sys.stdout.write(
                f"\n📈 Stage A produced {len(promotes)} promote(s): {', '.join(promotes)}\n"
            )
            stage_b_results = _run_stage(
                profile=secondary,
                tickers=promotes,
                trade_date=trade_date,
                matrix_dir=matrix_dir,
                env=env,
                max_parallel=args.max_parallel,
                no_dashboard=no_dashboard,
                writer=writer,
                stage_label="B",
                stop_on_overweight=0,  # never short-circuit Stage B
            )
        else:
            sys.stdout.write(
                "\n📭 Stage A produced no promotes; skipping Stage B.\n"
            )

    # Final reports
    all_results = writer.all_results()
    _write_matrix_json(matrix_dir, manifest, all_results)
    _write_matrix_md(matrix_dir, manifest, all_results)
    high_conv = _write_high_conviction(
        matrix_dir, manifest, all_results, primary, secondary
    )

    elapsed = time.time() - overall_started
    sys.stdout.write(f"\n✅ Matrix run complete in {elapsed/60:.1f} min.\n")
    sys.stdout.write(f"   📋 Matrix:           {matrix_dir / 'matrix.md'}\n")
    sys.stdout.write(f"   🟢 High conviction:  {matrix_dir / 'high_conviction.md'}\n")
    if high_conv:
        sys.stdout.write(f"      Picks: {', '.join(high_conv)}\n")
    else:
        sys.stdout.write(f"      No tickers passed the high-conviction filter.\n")

    if not args.no_auto_report:
        try:
            _run_auto_report(matrix_dir, args.long_call_delta)
        except Exception as exc:  # pragma: no cover — best-effort post-step
            sys.stderr.write(f"⚠ auto-report raised: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
