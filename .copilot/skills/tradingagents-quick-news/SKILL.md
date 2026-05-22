# TradingAgents · Quick News

Get the last week's catalysts and sourced headlines on any ticker without
running the full pipeline. Thin wrapper around `polygon_news.get_news()` —
the same Polygon source the matrix runs use.

## When to invoke this skill

The user is asking what's been happening with a specific ticker — catalysts,
news flow, earnings reactions, regulatory items. Recognise phrases like:

- "What's the news on NVDA?"
- "Any catalysts for INTC this week?"
- "Why is TJX moving?"
- "Anything happening with CRWV?"
- "What did the analysts say about MU?"
- "Headlines on GS"

Do NOT invoke for sentiment-on-social (use `tradingagents-quick-sentiment`)
or for a tradeable verdict (use `tradingagents-quick-verdict`).

## How to use

```bash
.venv/bin/python scripts/quick_news.py TICKER [--days N]
```

- `TICKER`: stock symbol (case insensitive)
- `--days N`: lookback window (default 7)
- `--end-date YYYY-MM-DD`: override the end date (default today)

## Output

Markdown with:
- Total items count
- Per-item: title · publisher · timestamp · keywords · URL · summary

Each item is **sourced** to a real Polygon news record (with URL). Never
quote items not in this output — that's exactly the fabrication risk the
grounded-pipeline mitigations exist to prevent.

## Communication style

When summarising for the user:

- **Lead with the action**: "NVDA reported earnings May 21, beat on revenue +85%, stock down 3% pre-market on margin commentary."
- **Cluster by theme** if there are many items: earnings, guidance, M&A, regulatory, analyst actions.
- **Always include source publisher + date** for any specific number quoted.
- **Don't editorialise sentiment** — that's the sentiment scorer's job, not ours.
- One paragraph + a 3-5 line bulleted catalyst list is usually enough. Skip the news firehose.

## Install (once per host)

```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-quick-news" \
    ~/.copilot/skills/tradingagents-quick-news
```
