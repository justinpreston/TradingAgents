# TradingAgents · Quick Sentiment

Pull real Reddit + Stocktwits posts on a ticker, optionally scored with the
keyword or FinBERT polarity scorer the news_enrichment pipeline uses.

**Critical**: this skill **only** returns posts that actually exist in the
Reddit / Stocktwits APIs at the moment of the call. It cannot — and must not
— fabricate posts. Under no circumstances paraphrase a "typical" Reddit
sentiment or invent a representative comment. If the output is thin
(common for small-caps), say so explicitly.

## When to invoke this skill

The user is asking what retail / social media thinks about a ticker.
Recognise phrases like:

- "What's reddit saying about NVDA?"
- "Stocktwits sentiment on INTC"
- "Any chatter on CRWV?"
- "Is the retail crowd long or short TJX?"
- "Sentiment check on MU"

Do NOT invoke for institutional news flow (use `tradingagents-quick-news`).
The scorer here is **secondary signal** — don't size off it alone.

## How to use

```bash
.venv/bin/python scripts/quick_sentiment.py TICKER \
    [--reddit-only | --stocktwits-only] \
    [--limit N] \
    [--score none|keyword|finbert]
```

- `TICKER`: stock symbol
- `--reddit-only` / `--stocktwits-only`: limit to one source
- `--limit N`: stocktwits max messages (default 20)
- `--score keyword` (default scorer in news_enrichment): zero-deps regex
- `--score finbert`: ProsusAI/finbert transformer (~3× more discriminating);
  requires `pip install torch transformers`

## Output

Markdown with:
- Reddit: top posts across wallstreetbets / stocks / investing with title +
  excerpt + score + URL
- Stocktwits: most-recent messages with author + Bullish/Bearish/no-label
- Polarity (if `--score` set): aggregate in [-1, +1] + trigger terms

All posts are **real** API records. If empty: "No recent chatter" — never invent.

## Communication style

When summarising for the user:

- **Lead with volume + tilt**: "NVDA: 47 reddit posts in last day, bullish 32 / bearish 8 / neutral 7. Stocktwits 20 most-recent: 4 bullish, 3 bearish, 13 unlabeled. Aggregate polarity +0.12 (mildly positive)."
- **Quote 1-2 specific posts** when they illustrate the tilt — use the actual text from the output, never paraphrased.
- **Call out fabrication risk**: if posts look fake or all-positive in a way that doesn't match price action, surface that as a warning.
- **Never quote a post that isn't in the script output** — that's the exact failure mode the grounded mitigations were built to stop.
- **No hard "buy" or "sell" conclusions** — sentiment is one input among many. Defer to `tradingagents-quick-verdict`.

## Install (once per host)

```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-quick-sentiment" \
    ~/.copilot/skills/tradingagents-quick-sentiment
```
