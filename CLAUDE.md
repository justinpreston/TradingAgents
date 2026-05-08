# Coding-agent runbook — TradingAgents

> **Read this first.** This file is the canonical operational guide for Claude
> Code, GitHub Copilot, Cursor, or any other agent working in this repo. It
> describes the locked-in pipeline cadence, command surface, and the invariants
> that have historically broken when ignored.
>
> If you update this file, also update `.github/copilot-instructions.md`
> (which is a pointer plus the most critical must-knows).

---

## TL;DR — running the pipeline

```bash
# Weekly Friday tick — auto-detects what's new vs catalog and prints next-step commands
.venv/bin/python scripts/weekly_workflow.py --top 25

# Reuse an existing screener output (e.g. you already ran it earlier today)
.venv/bin/python scripts/weekly_workflow.py \
    --use-screener-run runs/screener_<id> --top 25

# Auto-launch matrix on the NEW tickers only (skips wasteful re-screening)
.venv/bin/python scripts/weekly_workflow.py --top 25 --chain --chain-top 5

# Same-day options refresh before any entry
.venv/bin/python scripts/build_options_overlay.py \
    --matrix-run runs/<matrix_id> --strategy-mode long-call

# Optional: news enrichment (sentiment + theme tags) on the screener output
# Default scorer is now FinBERT (~3× more discriminating than keyword on real
# headlines). Auto-falls-back to keyword if torch+transformers not installed.
.venv/bin/python scripts/build_news_enrichment.py \
    --screener-run runs/<screener_id>
# Force the zero-dep keyword scorer:
.venv/bin/python scripts/build_news_enrichment.py \
    --screener-run runs/<screener_id> --scorer keyword
# A/B test keyword vs FinBERT side-by-side:
.venv/bin/python scripts/build_news_enrichment.py \
    --screener-run runs/<screener_id> --scorer both

# Tests (baseline: 331 pass, 41 subtests)
.venv/bin/python -m pytest tests/ -x -q
```

---

## The locked-in cadence

User's explicit instruction (do not re-litigate without confirmation):

> *"For your current workflow (long calls, multi-month tenors), a weekly
> Friday screener + on-demand matrix on new names + same-day options refresh
> before entry is the right rhythm."*

| When | What | How |
|---|---|---|
| **Weekly, Friday EOD** | Run the screener | `scripts/weekly_workflow.py --top 25` |
| **On-demand** | Matrix-run only NEW tickers (diff vs catalog) | `--chain --chain-top N` (or copy-paste from Phase 5 output) |
| **Same-day, before entry** | Refresh options overlay | `scripts/build_options_overlay.py --matrix-run runs/<id> --strategy-mode long-call` |

**Override triggers** (re-run outside cadence):
- Earnings on an active pick → re-matrix that single ticker.
- VIX > 25 or SPX −5%/week → full re-screen (regime shift).
- Quarter-end → full refresh (screen + matrix + options).

A launchd plist for Friday 17:00 local exists at
`scripts/launchd/com.tradingagents.weekly.plist`. Install with:
```bash
cp scripts/launchd/com.tradingagents.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tradingagents.weekly.plist
```
The launchd job intentionally does **not** auto-chain matrix runs — review the
NEW ticker list before spending LLM cycles on it.

---

## Pipeline architecture

```
run_screener.py  →  runs/screener_<TS>/{screener.json, top_tickers.txt}
                         │
                         ▼
run_copilot_matrix.py  →  runs/matrix_<TS>_top25/
                            ├ cells/aggressive/<T>/<T>.state.json   (Stage A)
                            └ cells/conservative/<T>/<T>.state.json (Stage B, on promotes)
                            
scripts/build_run_accounting.py  →  same dir, adds:
                            ├ verdict_ledger.{csv,json}     ← AUTHORITATIVE per-ticker results
                            ├ trade_synthesis.md            ← human-readable narrative
                            ├ per_ticker/<T>.md
                            └ current_prices.json

scripts/build_options_overlay.py  →  same dir, adds:
                            └ options_overlay.{md,json}     ← Polygon-pulled options structures

scripts/build_chronos_overlay.py  →  same dir, adds:
                            └ chronos_overlay.{md,json}     ← Amazon Chronos forecast vs persona PT

scripts/build_html_report.py  →  runs/cross_run_<DATE>/report.html  (cross-run dashboard)

scripts/index_runs.py  →  runs/index.db                     ← SQLite catalog (DERIVED, regenerable)

scripts/weekly_workflow.py  →  orchestrates SCREEN → INDEX → DIFF → CHAIN → REPORT
```

`run_copilot_persona_aligned.py` is the per-ticker subrunner each matrix cell
shells out to — also runnable directly for a quick single-stage analysis on
one or more tickers (produces flat `<T>.state.json` files; **does not**
produce the `verdict_ledger.json` + `cells/` structure that
`build_options_overlay.py`/`build_run_accounting.py` consume — for that you
need `run_copilot_matrix.py`). Other runners (`run_copilot_aggressive_aligned.py`,
`run_copilot_opus*.py`) exist for ad-hoc / experimental work.

`scripts/weekly_workflow.py --chain` defaults to `--chain-runner run_copilot_matrix.py`
for exactly this reason — feed it any other runner and Phase 4 will warn that
the rest of the cadence can't ingest the output.

`scripts/index_runs.py` detects matrix vs screener runs by the presence of
`verdict_ledger.json` or `screener.json` (not by directory name), so
custom run-ids like `matrix_pipeline_test_<id>` or `matrix_weekly_<ts>_chain`
are indexed correctly.

---

## User preferences (drive defaults)

1. **Long calls over multi-leg structures** — *"for ease of trading"*. Single
   fill, no leg management, unbounded upside.
   - Default mode: `--strategy-mode long-call --long-call-delta 0.55` (slightly
     ITM — less time decay than ATM, retains leverage).
   - The other mode (`--strategy-mode tier-driven`: A/B → bull call spreads,
     C → ATM long calls) is still available for comparison/back-test.
2. **Trade recommendations always include current price.** Without it,
   risk/reward and upside are meaningless. Polygon
   `/v2/aggs/ticker/{T}/prev` is the canonical source.
3. **Hybrid storage** — raw JSON/markdown files in `runs/<id>/` are the
   authoritative source of truth (immutable, easy to inspect). The SQLite
   catalog `runs/index.db` is purely derived/regenerable for cross-run
   queries. **Never write authoritative data to `index.db`. Never commit it.**
4. **Cross-run reasoning matters.** When the user asks about a name, check
   `runs/index.db` first via `scripts/index_runs.py --query` to see how often
   it has appeared and at what tier.

---

## Tier A/B/C classification (canonical, must keep in sync)

Tier is **derived per-row**, NOT a stored field. The `classification` column
in `verdict_ledger.json` only takes values `PICK` or `VETOED`.

```python
def _tier(row):
    if row['classification'] != 'PICK':
        return 'VETO' if row['classification'] == 'VETOED' else '—'
    if row.get('conservative_pt') is None:
        return 'C'   # cons declined to model
    if row.get('pt_compression_pct') is not None and row['pt_compression_pct'] < 5.0:
        return 'A'   # tight dual-frame agreement
    return 'B'       # cons engaged but skeptical
```

Implementations that must stay in sync:
- `scripts/build_options_overlay.py::_tier()` (lines 65–73)
- `scripts/index_runs.py::_classification_to_tier()` (lines 145–161)

If the 5.0 threshold ever changes, both files must change together.

**Empirical context** (cross-run, indexed in `runs/index.db`):

| Tier | Long-call avg ROI | Notes |
|---|---:|---|
| A | **−35%** | "Equity only" — 5–7% modeled upside doesn't clear long-call premium. |
| B | **+108%** ⭐ | Cons engaged but skeptical → wider modeled aggressive PT, premium clears it. |
| C | **+41%** | Aggressive-only thesis. Stay at starter size. |

---

## News enrichment (optional, off by default)

`scripts/build_news_enrichment.py` writes `news_enrichment.json` next to a
screener or matrix run with two derived signals per ticker:

1. **Sentiment polarity** in [-1, 1] from a pluggable scorer.
2. **Theme labels** (deterministic regex over 10 catalysts: m_and_a,
   earnings, guidance, regulatory, government_action, leadership,
   litigation, product_launch, analyst_action, capital_action).

**Two scorers, A/B-testable:**
- `--scorer finbert` (**default**, ~3× more discriminating on real headlines):
  ProsusAI/finbert transformer. **Auto-falls-back to keyword with a warning**
  if torch+transformers aren't installed — the default should always
  produce some signal rather than crash. Install: `pip install torch transformers`
  (~1.5-2GB + 440MB model on first call).
- `--scorer keyword` (zero deps): weighted regex over a curated
  positive/negative term list. Fast, deterministic, fully auditable
  (`trigger_terms` lists the rules that fired). Best as a cheap negative
  prefilter (warn / miss / downgrade triggers).
- `--scorer both`: runs both side-by-side, prints discrimination ratio
  (`finbert range / keyword range`). On the 2026-05-06 mega-cap smoke
  test (TXN/GS/KO/NVDA/AVGO), this came out to **3.03×**.

**Wired into weekly_workflow.py** as opt-in Phase 2.5:
```bash
.venv/bin/python scripts/weekly_workflow.py --tier mega --enrich-news
.venv/bin/python scripts/weekly_workflow.py --tier mega --enrich-news --news-scorer both
```

**Three downstream consumers** (all opt-in, no behavioral change without flags):

1. **HTML report chips** — `scripts/build_html_report.py` auto-detects
   `news_enrichment.json` next to the supplied `--screener-run` and renders
   a `News` column on the screener watchlist with sentiment polarity and
   the top-2 themes per ticker (purple chip for `government_action` to
   tag INTC-style theses).

2. **Screener re-rank** — `scripts/build_news_enrichment.py --rerank-screener
   --rerank-alpha 0.10` writes `screener_sentiment_reranked.json` and
   `top_tickers_sentiment_reranked.txt` next to the original screener (which
   is **never** modified). Each candidate's score is multiplied by
   `clamp(1 + alpha * polarity, 0.5, 1.5)`. Default alpha 0.10 is gentle —
   on the 2026-05-06 mega-cap watchlist it preserves the top-5 order while
   tilting scores by ±1-3% per ticker.

3. **Matrix cell injection** — `run_copilot_matrix.py --news-enrichment
   <path>` (auto-passed by `weekly_workflow.py --chain --enrich-news`) sets
   `TRADINGAGENTS_NEWS_ENRICHMENT_PATH` for every cell subprocess. The
   `news_analyst` then prepends a compact "Pre-computed news context for
   <T>: …" prefix to its system message before tool calls. **No
   `AgentState` schema change** — the env-var route keeps the langgraph
   checkpointer schema stable. `run_copilot_persona_aligned.py
   --news-enrichment <path>` does the same for one-off runs.

**Insider transactions** are now bound to `fundamentals_analyst.py`
(Form-4 signals via the yfinance → alpha_vantage vendor fallback chain).
Polygon free-tier doesn't expose insider transactions; the vendor router
in `default_config.py` falls through automatically.

---

## Subprocess invariants (these have all broken matrix runs historically)

1. **`stdin=subprocess.DEVNULL`** on every child process call. Without it,
   headless / CI invocations hang waiting for input. Fixed in commit
   `50b41e4`. Applies to: `run_copilot_matrix.py`, `weekly_workflow.py::_run()`,
   anything that shells out to a per-ticker runner.
2. **Flag is `--max-parallel`, NOT `--parallel`** on `run_copilot_matrix.py`.
3. **`--stop-on-overweight 0`** for full-coverage matrix runs (otherwise the
   matrix terminates on the first OVERWEIGHT verdict).
4. **`load_dotenv('.env')` with explicit path** (Python 3.13 has a heredoc
   invocation quirk where the implicit search path misses).

---

## Run output anatomy

```
runs/matrix_2026-05-01_top25/
├── verdict_ledger.csv             ← spreadsheet-friendly per-ticker verdicts
├── verdict_ledger.json            ← AUTHORITATIVE, programmatic source
├── trade_synthesis.md             ← human narrative (top picks, asymmetry, tier breakdown)
├── README.md                      ← run metadata
├── current_prices.json
├── options_overlay.md             ← human-readable options structures
├── options_overlay.json           ← programmatic options data (consumed by HTML report + indexer)
├── per_ticker/
│   └── <T>.md                     ← per-ticker drilldown
├── cells/
│   ├── aggressive/<T>/<T>.state.json
│   └── conservative/<T>/<T>.state.json
└── manifest.json
```

`verdict_ledger.json` is the cross-script contract. Fields consumed downstream:
`ticker`, `name`, `sector_sic`, `market_cap`, `current_price`,
`aggressive_*`, `conservative_*`, `pt_compression_pct`, `classification`,
`aggressive_executive_summary`.

---

## Setup / first-run

```bash
# Python 3.10+ required. The repo's existing venv lives at .venv/
.venv/bin/python -m pip install -e .

# Required env vars (in .env at repo root)
POLYGON_API_KEY=...        # mandatory for screener + options overlay
OPENAI_API_KEY=...         # OR any other supported LLM provider:
ANTHROPIC_API_KEY=...      # (GOOGLE/XAI/DEEPSEEK/DASHSCOPE/ZHIPU/OPENROUTER)

# Tests baseline
.venv/bin/python -m pytest tests/ -x -q
# → 267 passed, 41 subtests passed in ~4s
```

`runs/` is fully gitignored (so `runs/index.db` and all per-run artifacts
stay local). `.env` is also gitignored.

---

## Common workflows

### Weekly tick (most common)
```bash
.venv/bin/python scripts/weekly_workflow.py --top 25
```
Phase 3 (DIFF) prints NEW vs REPEAT vs DROPPED against the last matrix run.
Phase 5 prints the exact next-step commands.

### Just refresh options on an existing matrix
```bash
.venv/bin/python scripts/build_options_overlay.py \
    --matrix-run runs/<matrix_id> \
    --strategy-mode long-call \
    --long-call-delta 0.55
```

### Build the cross-run HTML dashboard
```bash
.venv/bin/python scripts/build_html_report.py \
    --runs runs/<matrix_id_1>:large runs/<matrix_id_2>:mid \
    --output runs/cross_run_$(date +%Y-%m-%d)/report.html
```

### Query the catalog
```bash
# Summary
.venv/bin/python scripts/index_runs.py --query

# Or raw SQL
sqlite3 runs/index.db "SELECT ticker, COUNT(*) AS n FROM ticker_history GROUP BY ticker ORDER BY n DESC LIMIT 10"
```

### Force re-index (e.g. after editing an indexer field)
```bash
.venv/bin/python scripts/index_runs.py --force
```

---

## CLI flag reference (all scripts)

`run_screener.py`: `--top`, `--chain-top`, `--chain-runner`, `--target-date`,
`--output-dir`, `--technical-weight`, `--fundamental-weight`,
`--min-mcap`, `--max-mcap`, `--min-dollar-adv`, `--min-price`,
`--universe-limit`, `--min-request-interval`.

`run_copilot_matrix.py`: `--tickers ...` **xor** `--tickers-from-latest-screener`,
`--top N`, `--profiles aggressive conservative`, `--max-parallel`,
`--stop-on-overweight`, `--no-stage-b`, `--date`, `--run-id`,
`--no-dashboard`.

`run_copilot_persona_aligned.py`: `<tickers>...`, `--run-id`, `--date`.

`scripts/build_options_overlay.py`: `--matrix-run`, `--strategy-mode {tier-driven,long-call}`,
`--long-call-delta` (default 0.55), `--min-oi`, `--risk-free`,
`--ticker-limit`, `--snapshot-date`, `--verbose`.

`scripts/build_chronos_overlay.py`: `--matrix-run`, `--model` (default
`amazon/chronos-bolt-base` ~200 MB), `--prediction-length` (default 90 trading
days), `--context-length` (default 504 ≈ 2y), `--quantiles` (default
`0.1,0.5,0.9`), `--device {auto,mps,cuda,cpu}`, `--include-vetoed`,
`--snapshot-date`, `--polygon-pace-seconds`. Requires `pip install chronos-forecasting`.

`scripts/build_run_accounting.py`: `--matrix-run`, `--snapshot-date`.

`scripts/build_html_report.py`: `--runs`, `--output`, `--title`, `--subtitle`.

`scripts/index_runs.py`: `--runs-dir`, `--db`, `--force`, `--query`.

`scripts/weekly_workflow.py`: `--top`, `--target-date`, `--use-screener-run`,
`--chain`, `--chain-top`, `--chain-runner` (default `run_copilot_matrix.py`),
`--chain-max-parallel`, `--dry-run`, plus universe-shaping pass-throughs:
`--min-mcap` (default 2B), `--max-mcap` (default 10B = mid-cap focus),
`--min-dollar-adv` (default 50M), `--min-price` (default 5.0),
`--universe-limit`, `--min-request-interval`.

---

## Don'ts

- ❌ Don't add new dependencies casually — `requirements.txt` is intentionally
  minimal (`.` for editable). Heavy deps go in `pyproject.toml::dependencies`.
- ❌ Don't commit anything under `runs/` — it's all gitignored for a reason.
- ❌ Don't write to `runs/index.db` from anywhere except `scripts/index_runs.py`.
- ❌ Don't add `--parallel` thinking it's an alias for `--max-parallel`. It isn't.
- ❌ Don't drop the `stdin=subprocess.DEVNULL` from any subprocess invocation.
- ❌ Don't rebuild the cadence around an arbitrary daily/intraday loop. The
  user explicitly chose weekly + on-demand. If you think a different cadence
  is better, raise it as a question, don't unilaterally implement it.

---

## Persisted session notes

`~/.copilot/session-state/<id>/files/codebase_conventions.md` carries
session-survival-grade notes. If `store_memory` is unavailable to your agent
(e.g. the GitHub Copilot CLI memory store rejects writes for personal
forks), persist new conventions there instead.

A shell wrapper at `~/.zshrc` routes the GitHub Copilot CLI memory store to
the correct identity for `justinpreston/*` repos. See
`files/codebase_conventions.md` "store_memory limitation" for details.
