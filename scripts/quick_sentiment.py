#!/usr/bin/env python3
"""Fast social-media sentiment lookup for a single ticker — Reddit + Stocktwits
raw posts, plus optional FinBERT/keyword polarity scoring.

Wraps the same dataflows the sentiment_analyst uses, but without the LLM step
so you get raw signal in 5-10 seconds instead of 60+.

Usage:
    .venv/bin/python scripts/quick_sentiment.py NVDA
    .venv/bin/python scripts/quick_sentiment.py INTC --reddit-only
    .venv/bin/python scripts/quick_sentiment.py CRWV --score finbert
"""
from __future__ import annotations

import argparse
import pathlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--reddit-only", action="store_true")
    parser.add_argument("--stocktwits-only", action="store_true")
    parser.add_argument("--limit", type=int, default=20,
                        help="Stocktwits message limit (default 20)")
    parser.add_argument("--score", choices=["none", "keyword", "finbert"],
                        default="none",
                        help="Optionally score the combined text (default none)")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:
        pass
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

    from tradingagents.dataflows.reddit import fetch_reddit_posts
    from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages

    ticker = args.ticker.upper()
    print(f"# Social sentiment · {ticker}\n")

    blocks: list[str] = []
    if not args.stocktwits_only:
        print("## Reddit (wsb · stocks · investing)\n")
        try:
            txt = fetch_reddit_posts(ticker)
            print(txt)
            blocks.append(txt)
        except Exception as exc:  # noqa: BLE001
            print(f"_(reddit fetch failed: {exc})_")
    if not args.reddit_only:
        print("\n## Stocktwits\n")
        try:
            txt = fetch_stocktwits_messages(ticker, limit=args.limit)
            print(txt)
            blocks.append(txt)
        except Exception as exc:  # noqa: BLE001
            print(f"_(stocktwits fetch failed: {exc})_")

    if args.score != "none" and blocks:
        combined = "\n".join(blocks)
        # Treat each non-empty line as a "headline" for the scorer
        headlines = [ln.strip() for ln in combined.split("\n") if len(ln.strip()) > 20]
        if not headlines:
            print(f"\n## Polarity\n_(too few headline-shaped lines for {args.score} scorer)_")
        else:
            try:
                if args.score == "keyword":
                    from tradingagents.dataflows.news_enrichment import KeywordScorer
                    scorer = KeywordScorer()
                else:  # finbert
                    from tradingagents.dataflows.news_enrichment import FinBERTScorer
                    scorer = FinBERTScorer()
                result = scorer.score_headlines(headlines)
                print(f"\n## Polarity ({args.score})\n")
                print(f"- aggregate polarity = **{result.aggregate:+.3f}** "
                      f"(range -1 to +1, n={result.n_headlines})")
                triggers = getattr(result, "trigger_terms", None) or []
                if triggers:
                    print(f"- triggers: {', '.join(list(triggers)[:10])}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n## Polarity\n_({args.score} scorer failed: {exc})_")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
