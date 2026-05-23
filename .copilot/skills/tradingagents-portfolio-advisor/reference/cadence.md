# Cadence

The TradingAgents pipeline runs on an explicit, locked-in rhythm. **Do not
re-litigate this cadence inside a portfolio conversation** unless the user
asks an override-trigger-shaped question.

| When | What | Command |
|---|---|---|
| **Weekly, Friday EOD** | Run the screener | `.venv/bin/python scripts/weekly_workflow.py --top 25` |
| **On-demand** | Matrix-run only NEW tickers | `--chain --chain-top N` (or copy-paste from Phase 5) |
| **Same-day, before entry** | Refresh options overlay | `.venv/bin/python scripts/build_options_overlay.py --matrix-run runs/<id> --strategy-mode long-call` |

## Override triggers

Re-run outside cadence if:

- **Earnings on a held position** → re-matrix that single ticker.
- **VIX > 25** or **SPX −5%/week** → full re-screen (regime shift).
- **Quarter-end** → full refresh (screen + matrix + options).

## In ongoing conversations

- If the user asks "should I run the screener?" — answer **only** if today
  is Friday EOD or an override trigger has fired. Otherwise reference the
  most recent run.
- If the user mentions an earnings event for a held position — that's an
  override trigger; suggest a single-ticker matrix re-run.
- If the user asks about a name that's not in the latest matrix run, the
  matrix-derived signals are **stale** for that name; do not invent a tier.

For the full cadence reference, refer to `/CLAUDE.md` at the repo root.
