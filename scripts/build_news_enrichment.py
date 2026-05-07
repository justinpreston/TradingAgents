#!/usr/bin/env python3
"""Build a news_enrichment.json artifact for a screener or matrix run.

Fetches recent headlines per ticker from Polygon, runs a sentiment
scorer (keyword by default, FinBERT optional, or "both" for A/B
testing), and tags each ticker with deterministic theme labels (M&A,
earnings, regulatory, government action, leadership, litigation, etc).

Output: ``<run-dir>/news_enrichment.json`` — consumed downstream by the
matrix runner (when --inject-news-enrichment is set, future work) and
by ``build_html_report.py`` for surface-level theme/sentiment tags in
the cross-run dashboard.

────────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────────

  # Default: keyword scorer + theme classifier on the most recent screener
  .venv/bin/python scripts/build_news_enrichment.py \\
      --screener-run runs/screener_2026-05-06_1705

  # Run on an explicit ticker list (no run dir required)
  .venv/bin/python scripts/build_news_enrichment.py \\
      --tickers AAPL MSFT NVDA --output /tmp/news_enrichment.json

  # A/B test: run BOTH scorers side-by-side. Requires torch+transformers.
  .venv/bin/python scripts/build_news_enrichment.py \\
      --screener-run runs/screener_<id> --scorer both

  # FinBERT only (after `pip install torch transformers`)
  .venv/bin/python scripts/build_news_enrichment.py \\
      --screener-run runs/screener_<id> --scorer finbert

────────────────────────────────────────────────────────────────────────
A/B testing
────────────────────────────────────────────────────────────────────────

When ``--scorer both`` is set, the output JSON has TWO sentiment blocks
per ticker — one ``keyword``, one ``finbert``. Comparing them across a
run gives you a side-by-side delta of:

    keyword aggregate vs. finbert aggregate
    keyword trigger terms (rule audit)
    finbert per-headline scores (probabilistic)

The decision criteria for replacing keyword with FinBERT as the default:
    1. FinBERT's aggregates are more discriminating (variance across
       tickers is meaningfully larger than keyword's)
    2. FinBERT's verdicts on a hand-labelled subset are >= keyword's
       (precision on negative-news names where you knew the truth)
    3. The +1.5-2GB env weight is acceptable for the production cadence

If those don't hold, keep keyword as the production default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.news_enrichment import (  # noqa: E402
    FinBERTScorer,
    KeywordScorer,
    SentimentResult,
    SentimentScorer,
    classify_themes,
)
from tradingagents.dataflows.polygon_common import (  # noqa: E402
    PolygonError,
    paginated_results,
)


def _fetch_headlines(
    ticker: str, *, lookback_days: int = 30, limit: int = 50,
) -> list[dict[str, Any]]:
    """Pull raw news items for ``ticker`` over the past ``lookback_days``.

    Returns the Polygon /v2/reference/news payload (list of dicts).
    Empty list on error or empty result.
    """
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    params = {
        "ticker": ticker.upper(),
        "published_utc.gte": start_dt.strftime("%Y-%m-%d"),
        "published_utc.lt": end_dt.strftime("%Y-%m-%d"),
        "order": "desc",
        "limit": limit,
    }
    try:
        return paginated_results("/v2/reference/news", params, max_pages=2)
    except PolygonError:
        return []


def _build_scorers(choice: str) -> dict[str, SentimentScorer]:
    """Resolve --scorer argv to one or more scorer instances.

    Returns a dict keyed by scorer name so output JSON shows which
    scorer produced which result. ``both`` resolves to keyword+finbert.
    ``finbert`` falls back to ``keyword`` with a warning if torch+
    transformers aren't installed (the default scorer should always
    produce *some* signal rather than crashing).
    """
    if choice == "keyword":
        return {"keyword": KeywordScorer()}
    if choice == "finbert":
        try:
            return {"finbert": FinBERTScorer()}
        except ImportError as exc:
            print(
                "Warning: FinBERT deps missing — falling back to keyword scorer.\n"
                f"  ({exc})\n"
                "  Run `pip install torch transformers` to enable FinBERT.",
                file=sys.stderr,
            )
            return {"keyword": KeywordScorer()}
    if choice == "both":
        scorers: dict[str, SentimentScorer] = {"keyword": KeywordScorer()}
        try:
            scorers["finbert"] = FinBERTScorer()
        except ImportError as exc:
            print(
                "Warning: --scorer both requested but FinBERT deps are missing — "
                f"running keyword only.\n  ({exc})",
                file=sys.stderr,
            )
        return scorers
    raise ValueError(f"Unknown --scorer: {choice}")


def _serialize_sentiment(result: SentimentResult) -> dict[str, Any]:
    return {
        "scorer": result.scorer,
        "aggregate": round(result.aggregate, 4),
        "n_headlines": result.n_headlines,
        "trigger_terms": result.trigger_terms,
        "per_headline": [round(s, 4) for s in result.per_headline],
    }


def _enrich_ticker(
    ticker: str,
    scorers: dict[str, SentimentScorer],
    lookback_days: int,
) -> dict[str, Any]:
    items = _fetch_headlines(ticker, lookback_days=lookback_days)
    headlines = [(it.get("title") or "").strip() for it in items if it.get("title")]

    sentiments: dict[str, Any] = {}
    for name, scorer in scorers.items():
        sentiments[name] = _serialize_sentiment(scorer.score_headlines(headlines))

    themes = classify_themes(headlines)
    themes_serialized = [
        {
            "label": t.label,
            "confidence": round(t.confidence, 4),
            "n_matched_headlines": len(t.matched_headlines),
        }
        for t in themes
    ]

    return {
        "ticker": ticker.upper(),
        "lookback_days": lookback_days,
        "headline_count": len(headlines),
        "sentiment": sentiments,
        "themes": themes_serialized,
        "headlines_sample": headlines[:10],
    }


def _resolve_tickers(args: argparse.Namespace) -> tuple[list[str], Path | None]:
    """Return (tickers, run_dir). run_dir is None if --tickers was used."""
    if args.tickers:
        return [t.upper() for t in args.tickers], None

    if args.screener_run:
        run_dir = Path(args.screener_run).resolve()
        top_file = run_dir / "top_tickers.txt"
        if not top_file.exists():
            raise FileNotFoundError(f"Missing {top_file} — is this a valid screener run?")
        tickers = [line.strip().upper() for line in top_file.read_text().splitlines()
                   if line.strip()]
        return tickers, run_dir

    if args.matrix_run:
        run_dir = Path(args.matrix_run).resolve()
        ledger = run_dir / "verdict_ledger.json"
        if not ledger.exists():
            raise FileNotFoundError(f"Missing {ledger} — is this a valid matrix run?")
        rows = json.loads(ledger.read_text())
        tickers = [r["ticker"].upper() for r in rows if r.get("ticker")]
        return tickers, run_dir

    raise SystemExit("Pass exactly one of --screener-run, --matrix-run, or --tickers")


def _sentiment_polarity_for(row: dict[str, Any]) -> float:
    """Pick FinBERT polarity if present, else keyword. Returns 0 on missing.

    Used by --rerank-screener so the re-rank prefers the more
    discriminating signal (FinBERT) when both are available.
    """
    sent = row.get("sentiment") or {}
    chosen = sent.get("finbert") or sent.get("keyword") or {}
    if not chosen.get("n_headlines"):
        return 0.0
    return float(chosen.get("aggregate") or 0.0)


def _rerank_screener(run_dir: Path, enriched: list[dict[str, Any]],
                     alpha: float) -> Path | None:
    """Re-rank screener candidates by sentiment-tilted multiplier.

    Reads screener.json, multiplies each candidate's composite_score by
    ``clamp(1 + alpha * polarity, 0.5, 1.5)``, sorts descending, writes
    new ``screener_sentiment_reranked.json`` and
    ``top_tickers_sentiment_reranked.txt`` next to the original.
    Original files are immutable — this is purely additive.

    Returns the path to the reranked JSON, or None if the original
    screener.json is missing.
    """
    src = run_dir / "screener.json"
    if not src.exists():
        print(f"⚠ Cannot rerank — {src} missing", file=sys.stderr)
        return None

    data = json.loads(src.read_text())
    candidates = data.get("candidates") or []
    polarity_by_ticker = {
        row["ticker"].upper(): _sentiment_polarity_for(row) for row in enriched
    }

    rescored = []
    for c in candidates:
        polarity = polarity_by_ticker.get(c["ticker"].upper(), 0.0)
        multiplier = max(0.5, min(1.5, 1.0 + alpha * polarity))
        new_score = (c.get("composite_score") or 0.0) * multiplier
        rescored.append({
            **c,
            "original_composite_score": c.get("composite_score"),
            "sentiment_polarity": round(polarity, 4),
            "sentiment_multiplier": round(multiplier, 4),
            "composite_score": round(new_score, 4),
        })

    rescored.sort(key=lambda r: r.get("composite_score") or 0.0, reverse=True)
    for rank, r in enumerate(rescored, 1):
        r["rank"] = rank

    out_data = {
        **data,
        "candidates": rescored,
        "rerank_metadata": {
            "alpha": alpha,
            "applied_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "n_with_sentiment": sum(1 for r in rescored if r["sentiment_polarity"] != 0.0),
        },
    }

    out_json = run_dir / "screener_sentiment_reranked.json"
    out_json.write_text(json.dumps(out_data, indent=2))

    out_txt = run_dir / "top_tickers_sentiment_reranked.txt"
    out_txt.write_text("\n".join(r["ticker"] for r in rescored) + "\n")

    return out_json


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--screener-run", type=str,
                     help="Path to a runs/screener_* directory")
    src.add_argument("--matrix-run", type=str,
                     help="Path to a runs/matrix_* directory")
    src.add_argument("--tickers", nargs="+",
                     help="Explicit ticker list (no run-dir context)")

    p.add_argument(
        "--scorer", choices=["keyword", "finbert", "both"], default="finbert",
        help="Sentiment scorer to apply. 'finbert' (default, ~3× more "
             "discriminating than keyword on real headlines) requires "
             "`pip install torch transformers` — falls back to keyword "
             "with a warning if those deps are missing. 'keyword' forces "
             "the zero-dep regex scorer. 'both' runs them side-by-side "
             "for A/B comparison.",
    )
    p.add_argument(
        "--lookback-days", type=int, default=30,
        help="How far back to pull headlines (default 30).",
    )
    p.add_argument(
        "--output", type=str, default=None,
        help="Output path for news_enrichment.json. Defaults to "
             "<run-dir>/news_enrichment.json when --screener-run/--matrix-run is set.",
    )
    p.add_argument(
        "--ticker-limit", type=int, default=None,
        help="Cap on tickers processed (useful for smoke tests).",
    )
    p.add_argument(
        "--rerank-screener", action="store_true",
        help="After enrichment, re-rank the screener candidates by applying "
             "a sentiment-tilted multiplier to each composite_score, and "
             "write screener_sentiment_reranked.json + "
             "top_tickers_sentiment_reranked.txt next to the original "
             "screener.json. Original files are NEVER modified. Requires "
             "--screener-run.",
    )
    p.add_argument(
        "--rerank-alpha", type=float, default=0.10,
        help="Sentiment penalty/boost magnitude for --rerank-screener. "
             "Each candidate's score is multiplied by (1 + alpha * "
             "sentiment_polarity), clamped to [0.5, 1.5]. Default 0.10 "
             "(±10%% effect at sentiment ±1.0).",
    )
    args = p.parse_args()

    if not os.getenv("POLYGON_API_KEY"):
        print("⚠ POLYGON_API_KEY not set — headlines will be empty.", file=sys.stderr)

    try:
        tickers, run_dir = _resolve_tickers(args)
    except (FileNotFoundError, SystemExit) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.ticker_limit:
        tickers = tickers[: args.ticker_limit]

    output_path: Path
    if args.output:
        output_path = Path(args.output).resolve()
    elif run_dir is not None:
        output_path = run_dir / "news_enrichment.json"
    else:
        output_path = REPO_ROOT / "news_enrichment.json"

    try:
        scorers = _build_scorers(args.scorer)
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Enriching {len(tickers)} tickers · scorer={args.scorer} · "
          f"lookback={args.lookback_days}d", flush=True)

    enriched: list[dict[str, Any]] = []
    for i, ticker in enumerate(tickers, 1):
        try:
            row = _enrich_ticker(ticker, scorers, args.lookback_days)
        except Exception as exc:
            print(f"  [{i}/{len(tickers)}] {ticker}: ERROR {exc}", flush=True)
            continue

        sentiment_summary = " · ".join(
            f"{name}={s['aggregate']:+.2f}"
            for name, s in row["sentiment"].items()
        )
        themes_summary = ", ".join(t["label"] for t in row["themes"][:3]) or "—"
        print(f"  [{i}/{len(tickers)}] {ticker}: "
              f"n={row['headline_count']:>2} · {sentiment_summary} · themes: {themes_summary}",
              flush=True)
        enriched.append(row)

    output: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scorer_choice": args.scorer,
        "lookback_days": args.lookback_days,
        "n_tickers": len(enriched),
        "source_run": str(run_dir) if run_dir else None,
        "tickers": enriched,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {output_path}", flush=True)

    if args.scorer == "both":
        print("\nA/B summary (keyword vs finbert):")
        kw_avg = [t["sentiment"]["keyword"]["aggregate"] for t in enriched]
        fb_avg = [t["sentiment"]["finbert"]["aggregate"] for t in enriched]
        if kw_avg and fb_avg:
            kw_var = (max(kw_avg) - min(kw_avg))
            fb_var = (max(fb_avg) - min(fb_avg))
            print(f"  keyword: mean={sum(kw_avg) / len(kw_avg):+.3f} · "
                  f"range={kw_var:.3f}")
            print(f"  finbert: mean={sum(fb_avg) / len(fb_avg):+.3f} · "
                  f"range={fb_var:.3f}")
            print(f"  ratio (finbert range / keyword range) = "
                  f"{(fb_var / kw_var) if kw_var > 0 else float('inf'):.2f}× "
                  f"(higher = finbert more discriminating)")

    if args.rerank_screener:
        if not args.screener_run or run_dir is None:
            print("Error: --rerank-screener requires --screener-run",
                  file=sys.stderr)
            return 2
        rerank_path = _rerank_screener(run_dir, enriched, args.rerank_alpha)
        if rerank_path is not None:
            print(f"\nRe-ranked screener written to {rerank_path}", flush=True)
            print(f"  alpha={args.rerank_alpha} · multiplier ∈ [0.5, 1.5]")
            print(f"  top_tickers_sentiment_reranked.txt also updated.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
