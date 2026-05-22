# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

# Portfolio advisor — per-position trade tickets + portfolio-wide queue
.venv/bin/python scripts/portfolio_health.py \
    --positions-file runs/portfolio/positions.json \
    --policy-file runs/portfolio/policy.json
# (See "Portfolio advisor" section below.)

# Tests (baseline: 535 pass, 41 subtests in ~16s)
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

scripts/score_picks_iv_surface.py  →  same dir, adds:
                            └ iv_surface_ranking.{md,json}  ← stockpile-style IV-surface scoring + earnings flag

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

Implementations that must stay in sync (all four use the same `< 5.0`
compression threshold and the same `conservative_pt is None → C` rule):
- `scripts/build_options_overlay.py::_tier()`
- `scripts/build_chronos_overlay.py::_tier()`
- `scripts/build_html_report.py::_tier()`
- `scripts/index_runs.py::_classification_to_tier()`

If the 5.0 threshold ever changes, all four files must change together.

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

## Portfolio advisor (skill + execution-follow-up loop)

A Copilot-CLI skill at `.copilot/skills/tradingagents-portfolio-advisor/`
joins the user's positions ledger to the latest matrix run and emits
**plain-spoken trade tickets**: per-position recommendations + a
portfolio-wide ranked queue. Read-only: never places trades, never edits
`positions.json`. The user manually executes and logs.

### File layout

```
.copilot/skills/tradingagents-portfolio-advisor/
├── SKILL.md                      ← invocation triggers + communication style
└── reference/
    ├── cadence.md                ← pointer to this file's weekly rhythm
    ├── tier_economics.md         ← A:-35% / B:+108% / C:+41% empirical ROI
    └── invariants.md             ← Polygon /prev rule, Δ 0.55, drift framework

scripts/                          ← lives in repo root
├── portfolio_load_context.py     ← canonical context loader (joins positions × matrix)
├── portfolio_check.py            ← per-position trade tickets
├── portfolio_allocation.py       ← sector/basket exposure + --candidate <T> simulator
├── portfolio_trade_tickets.py    ← portfolio-wide ranked queue
├── portfolio_health.py           ← weekly synthesis wrapper (calls all of the above)
└── portfolio_log_action.py       ← append-only writer for trades_log.jsonl

runs/portfolio/                   ← gitignored (under runs/), user-private
├── positions.json                ← user's live book (NEVER auto-edited)
├── positions.example.json        ← reference template
├── positions.schema.md           ← field reference + per-share semantics
├── policy.json                   ← sizing / caps / cash buffer / thematic baskets
├── trades_log.jsonl              ← append-only execution log
└── snapshots/<date>.json         ← weekly portfolio snapshots
```

### The plain-spoken contract

Every ticket carries a `spoken_summary` field — one English sentence saying
what to do and why, leading the rendered output as a `> **bold blockquote**`.
The trade-ticket queue's `## What to do` section is a numbered list of just
these summaries. Generated by `portfolio_check.py::spoken_summary_for_ticket()`.

Example output:
```
## What to do
1. ✂️ Sell 2 of your 5 VIRT contracts (keep 3) — VIRT is trading $51.31, past your $38.00 take-profit.
2. 🌱 Buy 17 × CTRE 40C exp 2026-10-16 @ $2.30/sh (~$3,910 total). Top A-tier pick this week.
   ⚠️ Thin chain — only 72 contracts of open interest. Confirm fill before sizing up.
```

**Communication style** (codified in SKILL.md): lead with the action; plain
English over jargon; specific numbers always (strike/expiry/qty/premium/price)
but explained in one short sentence each; no Greek letters outside contract
specs `(Δ 0.62, IV 0.30, OI 800)`; no walking through derivations. Tone =
experienced colleague in a Slack DM, not a research report.

### Execution follow-up loop

`scripts/portfolio_log_action.py` appends to `runs/portfolio/trades_log.jsonl`
when the user executes a trade. `build_context()` then loads the log and
attaches `recent_actions` to each position's `live` dict.
`recommend_for_position()` reads this and surfaces the most recent action in
the rationale, e.g. *"Last logged action on VIRT: TRIM 2 on 2026-05-09 —
'Took half profits'"* + tags the ticket with `has_recent_log`. This way the
skill follows up on what's been done rather than re-suggesting it.

```bash
# After executing a TRIM trade:
.venv/bin/python scripts/portfolio_log_action.py \
    --ticker VIRT --action TRIM --qty 2 --premium 1.85 \
    --underlying 51.31 --notes "Took half profits"
```

Valid actions: `OPEN`, `CLOSE`, `TRIM`, `ADD`, `ROLL`, `EXIT`, `NOTE`.

**The skill must NOT auto-update `positions.json` after a log entry.** The
log captures intent + execution context; positions.json captures current
state. They intentionally diverge until the user reconciles, and the skill
detects+surfaces that divergence.

### Key invariants

- **Per-share vs per-contract** (the big gotcha): positions schema uses
  `premium_paid_per_share` and `current_mark_per_share` (per-share, what the
  broker shows). Total cost = `qty × 100 × premium_paid_per_share`. Matrix
  overlay's `net_debit_per_share` is per-share; `net_debit_per_contract` is
  total $ for one contract (= per_share × 100). Mixing them double-counts
  or under-counts P&L by 100×. The validator catches the legacy
  `_per_contract` field name with an actionable rename error.
- **Stale ≠ demoted**: a held ticker absent from the latest matrix run is
  "stale" (informational), not "demoted" (high signal). Distinction matters
  for HOLD vs EXIT recommendations.
- **Tier rank ladder** (mirrors `compare_insider_ablation.py`): VETO=0,
  "—"/None=1, C=2, B=3, A=4. Lower rank now → demoted.
- **Priority chain** in `recommend_for_position()`:
  `stop_loss > VETO > demoted > roll > take_profit > promoted > HOLD`.
- **`runs/portfolio/` is gitignored** (because all of `runs/` is). Templates
  in repo are placeholders; user data is local-only.

### Discoverability

Repo-local skills at `.copilot/skills/<name>` are not auto-loaded by the CLI.
Symlink once to make discoverable across sessions:
```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-portfolio-advisor" \
    ~/.copilot/skills/tradingagents-portfolio-advisor
```

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
# → 535 passed, 41 subtests passed in ~16s
# (one timing-sensitive flake: deselect tests/test_polygon_pacer.py::test_retry_after_seconds_is_honored
#  if a parallel run perturbs sleep budgets)
```

`runs/` is fully gitignored (so `runs/index.db` and all per-run artifacts
stay local). `.env` is also gitignored.

---

## Testing

```bash
# Full suite (baseline: 535 pass, 41 subtests, ~16s)
.venv/bin/python -m pytest tests/ -x -q

# Single test file
.venv/bin/python -m pytest tests/test_news_enrichment.py -x -q

# Single test function
.venv/bin/python -m pytest tests/test_polygon_tier.py::test_tier_a_threshold -x -q

# By marker (defined in pyproject.toml)
.venv/bin/python -m pytest -m unit -x -q      # fast isolated tests
.venv/bin/python -m pytest -m smoke -x -q      # quick sanity checks
.venv/bin/python -m pytest -m integration -x -q # needs external services
```

Known flake: `tests/test_polygon_pacer.py::test_retry_after_seconds_is_honored`
is timing-sensitive — deselect if a parallel run perturbs sleep budgets.

---

## Core package architecture (`tradingagents/`)

The framework is a LangGraph state machine. `TradingAgentsGraph.propagate(ticker, date)`
is the entry point — it builds the graph, creates initial `AgentState`, and
runs it to a terminal node that returns a portfolio decision.

```
tradingagents/
├── graph/                        ← LangGraph wiring
│   ├── trading_graph.py          ← TradingAgentsGraph (main class, builds the graph)
│   ├── setup.py                  ← GraphSetup: node/edge registration
│   ├── propagation.py            ← Propagator: initial state + graph invoke
│   ├── conditional_logic.py      ← Edge predicates (debate rounds, risk discussion)
│   ├── signal_processing.py      ← Post-run signal extraction
│   ├── reflection.py             ← Cross-run memory reflection (decision log)
│   └── checkpointer.py           ← LangGraph checkpoint resume (opt-in)
│
├── agents/                       ← Node implementations (each is a LangGraph node)
│   ├── analysts/                 ← Four analyst nodes (fundamentals, market, news, social_media)
│   ├── researchers/              ← Bull/bear researchers (structured debate)
│   ├── managers/                 ← Research manager + portfolio manager (structured output)
│   ├── trader/                   ← Trader node (structured output: Buy/Sell/Hold)
│   ├── risk_mgmt/                ← Risk discussion nodes
│   ├── schemas.py                ← Pydantic schemas for structured-output agents
│   └── utils/
│       ├── agent_states.py       ← AgentState, InvestDebateState, RiskDebateState TypedDicts
│       ├── agent_utils.py        ← Abstract tool functions (get_stock_data, get_news, etc.)
│       └── memory.py             ← TradingMemoryLog (decision log persistence)
│
├── dataflows/                    ← Data vendor abstraction layer
│   ├── interface.py              ← Vendor-agnostic tool interface
│   ├── config.py                 ← set_config() wires DEFAULT_CONFIG → vendor routing
│   ├── polygon_*.py              ← Polygon.io implementations
│   ├── alpha_vantage*.py         ← Alpha Vantage implementations
│   ├── y_finance.py              ← yfinance implementations
│   └── news_enrichment_loader.py ← Loads pre-computed news enrichment from env var
│
├── llm_clients/                  ← Provider abstraction
│   ├── factory.py                ← create_llm_client(provider, model) → LangChain LLM
│   ├── model_catalog.py          ← Canonical model names per provider
│   ├── openai_client.py, anthropic_client.py, google_client.py, azure_client.py
│   └── base_client.py            ← Base class for provider clients
│
├── default_config.py             ← DEFAULT_CONFIG dict (LLM, vendors, debate rounds)
├── screener/                     ← Universe screening (technical + fundamental scoring)
└── ui/                           ← Rich terminal UI components

cli/                              ← Interactive CLI (`tradingagents` entry point)
    └── main.py                   ← Typer app; `python -m cli.main` or `tradingagents`
```

**Graph flow**: Analysts → Research Manager → Bull/Bear Debate (N rounds) →
Trader → Risk Discussion (N rounds) → Portfolio Manager → Decision.

**Data vendor routing**: `default_config.py::data_vendors` sets category-level
defaults (polygon, alpha_vantage, yfinance). `tool_vendors` overrides at the
individual tool level (e.g. `get_insider_transactions` routes to
`yfinance,alpha_vantage` because Polygon free tier lacks insider data).
`dataflows/config.py::set_config()` wires this at graph construction time.

**Structured output**: Research Manager, Trader, and Portfolio Manager use
Pydantic schemas (`agents/schemas.py`) with provider-native structured output
(OpenAI json_schema, Gemini response_schema, Anthropic tool-use). A render
helper converts parsed models back to markdown for reports and memory log.

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

### Portfolio health check (after each weekly matrix tick)
```bash
.venv/bin/python scripts/portfolio_health.py \
    --positions-file runs/portfolio/positions.json \
    --policy-file runs/portfolio/policy.json \
    --output runs/portfolio/snapshots/$(date +%Y-%m-%d).md
```

### Log an executed trade
```bash
.venv/bin/python scripts/portfolio_log_action.py \
    --ticker <T> --action {OPEN|CLOSE|TRIM|ADD|ROLL|EXIT|NOTE} \
    --qty <N> --premium <PER_SHARE> --underlying <PRICE> \
    --strike <K> --expiry <YYYY-MM-DD> --notes "<freeform>"
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

`scripts/score_picks_iv_surface.py`: `--matrix-run` (required, reads
`options_overlay.json`), `--earnings-calendar` (auto-discovers
`<matrix-run>/earnings_calendar.json`), `--snapshot-date`, `--output`,
`--strike-window-pct` (default 25), `--expiry-window-days` (default 60),
`--pace-seconds`. Implements medloh/stockpile's 5-param IV-surface fit and
flags picks with earnings inside the option's expiry window.

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

`scripts/portfolio_load_context.py`: `--positions-file`, `--policy-file`,
`--matrix-run` (auto-discovered if omitted), `--trades-log`, `--output`,
`--validate`.

`scripts/portfolio_check.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--trades-log`, `--ticker <T>` (limit to one position),
`--json`.

`scripts/portfolio_allocation.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--candidate <T>` (simulator mode: emits BUY ticket with
starter sizing from policy), `--json`.

`scripts/portfolio_trade_tickets.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--trades-log`, `--max-new-picks` (default 5),
`--json`.

`scripts/portfolio_health.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--trades-log`, `--output` (write markdown to file).

`scripts/portfolio_log_action.py`: `--ticker`, `--action`, `--id`, `--qty`,
`--premium`, `--underlying`, `--strike`, `--expiry`, `--notes`, `--source`,
`--ts`, `--log-file`.

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
- ❌ Don't auto-edit `runs/portfolio/positions.json` from any portfolio
  script. `portfolio_log_action.py` appends to the log; reconciling the log
  to positions is the user's job. The two are designed to diverge until
  reconciled — that divergence is signal the skill surfaces.
- ❌ Don't mix per-share and per-contract option fields. `premium_paid_per_share`
  is the broker-quoted per-share price; `qty × 100 × per_share = total cost`.
  See `runs/portfolio/positions.schema.md`.

---

## Persisted session notes

For GitHub Copilot CLI:
`~/.copilot/session-state/<id>/files/codebase_conventions.md` carries
session-survival-grade notes. A shell wrapper at `~/.zshrc` routes the
memory store to the correct identity for `justinpreston/*` repos.

For Claude Code: project memory lives at
`~/.claude/projects/-Users-jpp5q-Documents-GitHub-TradingAgents/memory/`.
