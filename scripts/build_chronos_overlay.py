#!/usr/bin/env python3
"""Build a Chronos forecast overlay for a matrix run.

For each PICK (and optionally VETOED) row in ``verdict_ledger.json``,
pulls the trailing daily-bar history from Polygon, runs Amazon's
Chronos-Bolt time-series foundation model to produce a probabilistic
forecast at a configurable horizon, and emits a side-by-side comparison
with the persona-derived price target.

Output (in the matrix-run dir):
  - chronos_overlay.json   (programmatic)
  - chronos_overlay.md     (human-readable)

The point of the overlay
========================

The matrix's persona debate ends with an aggressive/conservative price
target derived from qualitative reasoning (Bull-vs-Bear, sector context,
catalyst calendar). Chronos comes at the same question from a purely
numerical, distribution-free angle: given two years of daily closes,
what's the calibrated probabilistic forecast at horizon H?

When the persona PT lands inside the Chronos cone (p10-p90), the two
methods agree. When it lands outside, that's a flag worth a second look:
either the personas saw something the model can't (catalyst, regime
shift) or they're stretching.

Usage
=====

    python scripts/build_chronos_overlay.py \\
        --matrix-run runs/matrix_weekly_2026-05-06_chain

    # Longer horizon, include vetoed rows for cross-check signal
    python scripts/build_chronos_overlay.py \\
        --matrix-run runs/matrix_weekly_2026-05-06_chain \\
        --prediction-length 126 \\
        --include-vetoed

    # Pin to a specific Chronos size (tiny/mini/small/base)
    python scripts/build_chronos_overlay.py \\
        --matrix-run runs/matrix_weekly_2026-05-06_chain \\
        --model amazon/chronos-bolt-small

Defaults
========

  * Model:               amazon/chronos-bolt-base   (~200 MB, CPU-friendly)
  * Context length:      504 trading days (~2 years)
  * Prediction length:   90 trading days (~4 months, comfortably in the
                         model's optimized ≤64 zone — note 90 nudges into
                         best-effort territory; use --prediction-length 63
                         for strict-quality forecasts)
  * Quantile levels:     0.1 / 0.5 / 0.9
  * Device:              auto (mps > cuda > cpu)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.volatility_context import _fetch_daily_aggs  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Tier classification (mirrors build_options_overlay / build_run_accounting)
# ──────────────────────────────────────────────────────────────────────

def _tier(row: dict) -> str:
    if row.get("classification") == "VETOED":
        return "VETO"
    if row.get("classification") != "PICK":
        return "—"
    comp = row.get("pt_compression_pct")
    if row.get("conservative_pt") is None:
        return "C"
    if comp is not None and comp < 5.0:
        return "A"
    return "B"


# ──────────────────────────────────────────────────────────────────────
# Device selection
# ──────────────────────────────────────────────────────────────────────

def _select_device(preference: str) -> str:
    """Resolve 'auto' to the best available device on this machine."""
    if preference != "auto":
        return preference
    try:
        import torch  # local import so --help works without torch
    except ImportError:
        return "cpu"
    if torch.backends.mps.is_available() and torch.backends.mps.is_built():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


# ──────────────────────────────────────────────────────────────────────
# Quantile interpolation: where does an arbitrary value fall in the
# Chronos forecast distribution at a given horizon?
# ──────────────────────────────────────────────────────────────────────

def _value_to_quantile(value: float,
                       quantile_levels: list[float],
                       quantile_values: list[float]) -> float:
    """Linearly interpolate the quantile position of ``value`` within the
    forecast distribution defined by (quantile_levels, quantile_values).

    Returns a number in roughly [0, 1]. Below the minimum quantile maps
    to <0; above the maximum maps to >1. Useful for asking "where does
    the persona PT sit in the model's view at horizon H?"
    """
    pairs = sorted(zip(quantile_values, quantile_levels))
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if value <= xs[0]:
        # Below the lowest reported quantile — extrapolate proportionally
        if len(xs) >= 2 and xs[1] > xs[0]:
            slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
            return ys[0] + slope * (value - xs[0])
        return ys[0]
    if value >= xs[-1]:
        if len(xs) >= 2 and xs[-1] > xs[-2]:
            slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
            return ys[-1] + slope * (value - xs[-1])
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= value <= xs[i + 1]:
            if xs[i + 1] == xs[i]:
                return ys[i]
            t = (value - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]  # unreachable


def _agreement_label(quantile_pos: float | None) -> str:
    """Human-readable bucket for where the persona PT falls in the model cone."""
    if quantile_pos is None:
        return "—"
    if quantile_pos < 0.10:
        return "below cone"
    if quantile_pos < 0.25:
        return "low"
    if quantile_pos < 0.75:
        return "inside"
    if quantile_pos < 0.90:
        return "high"
    return "above cone"


# ──────────────────────────────────────────────────────────────────────
# Per-ticker forecast
# ──────────────────────────────────────────────────────────────────────

def _build_per_ticker_forecast(
    row: dict,
    pipe: Any,
    today: date,
    context_days: int,
    prediction_length: int,
    quantile_levels: list[float],
    fetch_lookback_calendar_days: int,
    min_context_bars: int,
) -> dict:
    """Run Chronos on one ticker. Returns a dict ready for JSON serialization."""
    import torch  # local import keeps --help fast

    ticker = row["ticker"]
    out: dict[str, Any] = {
        "ticker": ticker,
        "tier": _tier(row),
        "classification": row.get("classification"),
        "current_price": row.get("current_price_usd"),
        "agent_aggressive_pt": row.get("aggressive_pt"),
        "agent_conservative_pt": row.get("conservative_pt"),
        "agent_horizon": row.get("aggressive_horizon"),
    }

    bars, err = _fetch_daily_aggs(
        ticker=ticker,
        today=today,
        lookback_days=fetch_lookback_calendar_days,
    )
    if err:
        out["error"] = f"polygon: {err}"
        return out
    if not bars:
        out["error"] = "polygon: no bars returned"
        return out

    closes = [b["c"] for b in bars if isinstance(b.get("c"), (int, float))]
    if len(closes) < min_context_bars:
        out["error"] = f"insufficient history: {len(closes)} bars (<{min_context_bars})"
        return out

    closes = closes[-context_days:]  # cap at context window
    out["context_bars_used"] = len(closes)
    out["context_first_close"] = round(float(closes[0]), 4)
    out["context_last_close"] = round(float(closes[-1]), 4)

    ctx = torch.tensor(closes, dtype=torch.float32)
    try:
        import warnings
        with warnings.catch_warnings():
            # Chronos warns "We recommend keeping prediction length <= 64"
            # for every call when prediction_length > 64. We've documented this
            # tradeoff in the CLI help and surface it once at startup; suppress
            # the per-call repetition so the run log stays readable.
            warnings.filterwarnings(
                "ignore",
                message=r".*We recommend keeping prediction length.*",
            )
            qs, mean = pipe.predict_quantiles(
                inputs=ctx,
                prediction_length=prediction_length,
                quantile_levels=quantile_levels,
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"chronos inference: {exc}"
        return out

    # qs shape: (batch=1, prediction_length, num_quantiles)
    # mean shape: (batch=1, prediction_length)
    final_quantiles = qs[0, -1, :].cpu().tolist()
    final_mean = float(mean[0, -1].cpu().item())
    last_close = float(closes[-1])

    forecast_quantiles = {
        f"p{int(q*100)}": round(float(v), 4)
        for q, v in zip(quantile_levels, final_quantiles)
    }
    forecast_pct = {
        f"p{int(q*100)}_pct": round((float(v) / last_close - 1.0) * 100.0, 2)
        for q, v in zip(quantile_levels, final_quantiles)
    }

    out["forecast_at_horizon"] = forecast_quantiles
    out["forecast_pct_change_at_horizon"] = forecast_pct
    out["forecast_mean_at_horizon"] = round(final_mean, 4)
    out["prediction_length_days"] = prediction_length

    # Where does the agent's PT sit in the model distribution?
    agent_pt = row.get("aggressive_pt")
    if agent_pt is not None and final_quantiles:
        q_pos = _value_to_quantile(float(agent_pt), quantile_levels, final_quantiles)
        median_idx = quantile_levels.index(0.5) if 0.5 in quantile_levels else len(quantile_levels) // 2
        median_forecast = float(final_quantiles[median_idx])
        out["agent_pt_quantile"] = round(q_pos, 3)
        out["agent_pt_vs_median_pct"] = (
            round((float(agent_pt) / median_forecast - 1.0) * 100.0, 2)
            if median_forecast > 0 else None
        )
        out["model_view"] = _agreement_label(q_pos)
        out["agent_inside_cone"] = bool(0.1 <= q_pos <= 0.9)

    return out


# ──────────────────────────────────────────────────────────────────────
# Markdown rendering
# ──────────────────────────────────────────────────────────────────────

def _render_md(matrix_run_name: str, overlays: list[dict],
               today: date, model: str, context_days: int,
               prediction_length: int, quantile_levels: list[float],
               include_vetoed: bool) -> str:
    lines: list[str] = []
    lines.append(f"# Chronos Forecast Overlay — `{matrix_run_name}`")
    lines.append("")
    lines.append(f"- **Snapshot:** {today.isoformat()}")
    lines.append(f"- **Model:** `{model}`")
    lines.append(f"- **Context window:** {context_days} trading days (~{context_days/252:.1f} years)")
    lines.append(f"- **Forecast horizon:** {prediction_length} trading days (~{prediction_length/21:.0f} months)")
    lines.append(f"- **Quantiles:** {', '.join(f'p{int(q*100)}' for q in quantile_levels)}")
    lines.append(f"- **Tickers in scope:** {len(overlays)} ({'PICK + VETO' if include_vetoed else 'PICK only'})")
    lines.append("")
    lines.append("## How to read this")
    lines.append("")
    lines.append(
        "Chronos produces a probabilistic forecast — a distribution, not a point. "
        "The persona-derived PT (from the matrix Bull/Bear debate) is mapped onto "
        "that distribution. **Inside cone** (p10-p90) = the two methods agree the "
        "PT is plausible. **Above cone** = personas are more bullish than the model "
        "thinks the price history justifies. **Below cone** = the personas are leaving "
        "upside on the table that the model sees in the price action."
    )
    lines.append("")
    lines.append("## Forecast vs. agent PT")
    lines.append("")
    lines.append("| Ticker | Tier | Last $ | Agent PT | Model p10 | Model p50 | Model p90 | PT in cone? | View |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---|")

    for o in overlays:
        if "error" in o:
            lines.append(
                f"| {o['ticker']} | {o.get('tier','—')} | "
                f"{(o.get('current_price') or 0):.2f} | "
                f"{(o.get('agent_aggressive_pt') or 0):.2f} | — | — | — | — | "
                f"⚠ {o['error']} |"
            )
            continue
        fq = o.get("forecast_at_horizon", {})
        view = o.get("model_view", "—")
        in_cone = o.get("agent_inside_cone")
        in_cone_str = "✓ yes" if in_cone else ("✗ no" if in_cone is False else "—")
        lines.append(
            f"| **{o['ticker']}** | {o.get('tier','—')} | "
            f"${o.get('current_price') or 0:.2f} | "
            f"${(o.get('agent_aggressive_pt') or 0):.2f} | "
            f"${fq.get('p10', 0):.2f} | "
            f"${fq.get('p50', 0):.2f} | "
            f"${fq.get('p90', 0):.2f} | "
            f"{in_cone_str} | {view} |"
        )
    lines.append("")

    # Per-ticker narrative
    lines.append("## Per-ticker reads")
    lines.append("")
    for o in overlays:
        if "error" in o:
            lines.append(f"### {o['ticker']} — error")
            lines.append(f"- {o['error']}")
            lines.append("")
            continue
        fq = o.get("forecast_at_horizon", {})
        fp = o.get("forecast_pct_change_at_horizon", {})
        agent_pt = o.get("agent_aggressive_pt")
        last = o.get("current_price") or 0
        agent_upside = (float(agent_pt) / last - 1.0) * 100.0 if agent_pt and last else None

        lines.append(f"### {o['ticker']} — Tier {o.get('tier','—')}")
        lines.append(f"- **Now:** ${last:.2f}")
        if agent_pt is not None:
            up_str = f" ({agent_upside:+.1f}%)" if agent_upside is not None else ""
            lines.append(f"- **Agent PT:** ${float(agent_pt):.2f}{up_str}")
        lines.append(
            f"- **Chronos at horizon:** p10 ${fq.get('p10',0):.2f} "
            f"({fp.get('p10_pct',0):+.1f}%) · "
            f"p50 ${fq.get('p50',0):.2f} ({fp.get('p50_pct',0):+.1f}%) · "
            f"p90 ${fq.get('p90',0):.2f} ({fp.get('p90_pct',0):+.1f}%)"
        )
        if "agent_pt_quantile" in o:
            qpos = o["agent_pt_quantile"]
            view = o.get("model_view", "—")
            vs_med = o.get("agent_pt_vs_median_pct")
            vs_str = f", {vs_med:+.1f}% vs model median" if vs_med is not None else ""
            lines.append(
                f"- **Agent PT in model distribution:** p{qpos*100:.0f} → "
                f"**{view}**{vs_str}"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Generated by `scripts/build_chronos_overlay.py`. "
                 "Forecasts are unconditional on news/earnings/macro — they "
                 "extrapolate from price history alone. Treat as a model-vs-narrative "
                 "cross-check, not a trade signal.*")
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--matrix-run", required=True,
                   help="Path to matrix run dir (must contain verdict_ledger.json)")
    p.add_argument("--model", default="amazon/chronos-bolt-base",
                   help="HuggingFace model id "
                        "(default amazon/chronos-bolt-base, ~200 MB; "
                        "swap to chronos-bolt-{tiny,mini,small} for less RAM)")
    p.add_argument("--prediction-length", type=int, default=90,
                   help="Forecast horizon in trading days "
                        "(default 90 ≈ 4 months; ≤64 keeps the model in its "
                        "optimized zone but 90-126 is reasonable best-effort)")
    p.add_argument("--context-length", type=int, default=504,
                   help="History window in trading days (default 504 ≈ 2 years)")
    p.add_argument("--quantiles", default="0.1,0.5,0.9",
                   help="Comma-separated quantile levels (default 0.1,0.5,0.9)")
    p.add_argument("--device", default="auto", choices=["auto", "mps", "cuda", "cpu"],
                   help="Inference device (default auto: mps > cuda > cpu)")
    p.add_argument("--include-vetoed", action="store_true",
                   help="Also forecast VETOED rows (useful as a cross-check signal)")
    p.add_argument("--snapshot-date", default=None,
                   help="Override 'today' for bar-fetch + filename "
                        "(YYYY-MM-DD); defaults to system date")
    p.add_argument("--polygon-pace-seconds", type=float, default=1.5,
                   help="Sleep between Polygon calls (default 1.5s)")
    args = p.parse_args()

    matrix_run = Path(args.matrix_run)
    ledger_path = matrix_run / "verdict_ledger.json"
    if not ledger_path.exists():
        print(f"❌ {ledger_path} not found", file=sys.stderr)
        return 2

    quantile_levels = [float(q) for q in args.quantiles.split(",") if q.strip()]
    if not quantile_levels or any(not 0 < q < 1 for q in quantile_levels):
        print(f"❌ invalid --quantiles: {args.quantiles}", file=sys.stderr)
        return 2

    ledger = json.loads(ledger_path.read_text())
    rows = ledger["rows"]
    if args.include_vetoed:
        scope_rows = [r for r in rows if r["classification"] in ("PICK", "VETOED")]
    else:
        scope_rows = [r for r in rows if r["classification"] == "PICK"]

    today = date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()
    device = _select_device(args.device)

    # Pull a calendar window slightly larger than context_days to absorb weekends/holidays
    fetch_calendar_days = int(args.context_length * 1.5) + 60
    min_context_bars = max(64, int(args.context_length * 0.4))

    print(f"[chronos] matrix: {matrix_run.name} · scope: {len(scope_rows)} "
          f"({'PICK+VETO' if args.include_vetoed else 'PICK'}) · today: {today.isoformat()}")
    print(f"[chronos] model: {args.model} · device: {device} · "
          f"context: {args.context_length}d · prediction: {args.prediction_length}d · "
          f"quantiles: {quantile_levels}")
    if args.prediction_length > 64:
        print(f"[chronos] note: prediction_length={args.prediction_length} > 64; "
              f"the model warns longer horizons are best-effort. Forecasts beyond "
              f"~3 months should be treated as directional rather than calibrated.")

    # Lazy-import torch + chronos so --help and arg validation are fast
    print(f"[chronos] loading model (first run downloads ~200 MB to ~/.cache/huggingface)...")
    t0 = time.time()
    try:
        import torch
        from chronos import BaseChronosPipeline
    except ImportError as exc:
        print(f"❌ chronos-forecasting not installed: {exc}", file=sys.stderr)
        print("   Install with: pip install chronos-forecasting", file=sys.stderr)
        return 3

    try:
        pipe = BaseChronosPipeline.from_pretrained(
            args.model,
            device_map=device,
            dtype=torch.float32,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ failed to load {args.model}: {exc}", file=sys.stderr)
        return 4
    print(f"[chronos] loaded in {time.time() - t0:.1f}s")

    overlays: list[dict] = []
    for i, row in enumerate(scope_rows, 1):
        t = row["ticker"]
        print(f"  [{i}/{len(scope_rows)}] {t} ...", end=" ", flush=True)
        ovl = _build_per_ticker_forecast(
            row=row,
            pipe=pipe,
            today=today,
            context_days=args.context_length,
            prediction_length=args.prediction_length,
            quantile_levels=quantile_levels,
            fetch_lookback_calendar_days=fetch_calendar_days,
            min_context_bars=min_context_bars,
        )
        if "error" in ovl:
            print(f"error ({ovl['error']})")
        else:
            fq = ovl.get("forecast_at_horizon", {})
            view = ovl.get("model_view", "—")
            qpos = ovl.get("agent_pt_quantile")
            qpos_str = f"p{qpos*100:.0f}" if qpos is not None else "—"
            print(
                f"p50 ${fq.get('p50', 0):.2f} · "
                f"agent PT {qpos_str} → {view}"
            )
        overlays.append(ovl)
        if i < len(scope_rows):
            time.sleep(args.polygon_pace_seconds)

    # Persist
    out_json = matrix_run / "chronos_overlay.json"
    out_md = matrix_run / "chronos_overlay.md"

    payload = {
        "matrix_run": matrix_run.name,
        "snapshot_date": today.isoformat(),
        "model": args.model,
        "device": device,
        "context_length_days": args.context_length,
        "prediction_length_days": args.prediction_length,
        "quantile_levels": quantile_levels,
        "include_vetoed": args.include_vetoed,
        "tickers_analyzed": len(overlays),
        "forecasts_built": sum(1 for o in overlays if "error" not in o),
        "overlays": overlays,
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str))
    out_md.write_text(_render_md(
        matrix_run.name, overlays, today, args.model,
        args.context_length, args.prediction_length, quantile_levels,
        args.include_vetoed,
    ))

    print()
    print(f"  ✅ {out_json}")
    print(f"  ✅ {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
