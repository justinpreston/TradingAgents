# TradingAgents · Quick Fundamentals

Pull a ticker's point-in-time fundamentals snapshot + recent insider
transactions. Wraps `polygon_finance.get_fundamentals()` (revenue, margins,
balance sheet, cash flow, valuation ratios) and the routed
`get_insider_transactions` call (Form-4 transactions via the yfinance →
alpha_vantage fallback chain; Polygon free tier doesn't expose insider data).

## When to invoke this skill

The user is asking about a company's underlying financials, balance sheet,
or insider activity — anything that's NOT price action, not options, not
social sentiment. Recognise phrases like:

- "Show me NVDA's fundamentals"
- "How's INTC's balance sheet?"
- "What's the financial picture for TJX?"
- "Any insider selling at NVDA?"
- "Is GS profitable?"
- "What are MU's margins?"

Do NOT invoke for technical indicators (use `tradingagents-quick-technicals`)
or for a tradeable verdict (use `tradingagents-quick-verdict`).

## How to use

```bash
.venv/bin/python scripts/quick_fundamentals.py TICKER [--date YYYY-MM-DD] [--no-insider]
```

- `TICKER`: stock symbol
- `--date`: point-in-time snapshot date (default today)
- `--no-insider`: skip the insider transactions section (faster)

## Output

Markdown with:
- Fundamentals snapshot: income statement, balance sheet, cash flow,
  valuation, growth, profitability metrics
- Insider transactions (last several quarters): CFO / CEO / director sales,
  grants, gifts with prices and share counts

All numbers are **sourced** to Polygon (fundamentals) and yfinance /
alpha_vantage (insider). Never quote a number not in the output.

## Communication style

When summarising for the user:

- **Lead with the headline**: "NVDA Q4: $81B revenue (+85% YoY), 68% gross margin, $44B FCF. Cash sales of $1.3B by insiders in Q3 — CEO sold 1.4M shares total."
- **Pick 3-5 metrics that actually matter** for the question — not the full pile.
- **Insider sales**: separate scheduled 10b5-1 dispositions from discretionary clusters. A CFO selling at 130 then again at 145 is not the same as five officers all selling in one week.
- **No Greek letters** outside option-contract specs. Plain percentages and dollar amounts.
- **Don't recommend an action** from this output alone — fundamentals is one input. Defer trade-sizing questions to `tradingagents-quick-verdict`.

## Install (once per host)

```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-quick-fundamentals" \
    ~/.copilot/skills/tradingagents-quick-fundamentals
```
