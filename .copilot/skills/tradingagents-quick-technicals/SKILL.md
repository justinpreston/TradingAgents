# TradingAgents · Quick Technicals

Pull OHLCV + a standard technical indicator panel (RSI, MACD, 50/200 SMA,
Bollinger Bands) on any ticker. Wraps `polygon_finance.get_stock_data()`
and the per-indicator `get_indicators()` calls — same stockstats pipeline
the market_analyst agent uses, minus the LLM step.

## When to invoke this skill

The user is asking about price action, trend, momentum, support/resistance,
or chart setup. Recognise phrases like:

- "What's NVDA's RSI?"
- "Is INTC oversold?"
- "Show me the technicals on TJX"
- "Is GS above its 200-day?"
- "Bollinger Band setup on CRWV"
- "Where's MU support?"

Do NOT invoke for fundamentals (use `tradingagents-quick-fundamentals`)
or news (use `tradingagents-quick-news`) or a verdict
(use `tradingagents-quick-verdict`).

## How to use

```bash
.venv/bin/python scripts/quick_technicals.py TICKER [--days N] [--no-indicators]
```

- `TICKER`: stock symbol
- `--days N`: lookback window (default 30)
- `--end-date YYYY-MM-DD`: override end date (default today)
- `--no-indicators`: OHLCV only (faster)

## Output

Markdown with:
- OHLCV daily bars over the window
- Per-indicator series: RSI · MACD · 50-day SMA · 200-day SMA · Bollinger Middle
- Each indicator includes a short usage note inline

All values are **computed** from Polygon bars — same vendor the matrix
uses. No fabricated levels.

## Communication style

When summarising for the user:

- **Lead with the setup**: "NVDA above 50-SMA ($192) and 200-SMA ($168), RSI 58 — uptrend intact but not yet overbought. Bollinger upper at $214 is the next ceiling."
- **Specific levels always**: support, resistance, MAs, BB upper/lower. No vague "looks bullish."
- **Plain English over Greek**: "momentum cooling" not "MACD histogram contracting."
- **Don't conflate technicals with conviction**: oversold ≠ buy; uptrend ≠ chase. Defer trade decisions to `tradingagents-quick-verdict`.
- One paragraph + a small price levels list is usually enough.

## Install (once per host)

```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-quick-technicals" \
    ~/.copilot/skills/tradingagents-quick-technicals
```
