# Polygon financials -> SEC EDGAR: parity check (2026-10-02)

Polygon's `/vX/reference/financials` is retired (sunset 2026-10-09, intermittent
`410 GONE` brownout now). Fundamentals now come from SEC EDGAR company facts
(`tradingagents/dataflows/vendors/sec_edgar.py`). This note compares the old
Polygon-backed numbers with the new EDGAR-backed ones for 10 tickers
(BOX HPE TEVA ABBV AMAT RBRK TMO TXN FSLY ATRC), run live on 2026-10-02.

**Method.** "Polygon" figures were produced by HEAD's *unmodified*
`tradingagents/screener/fundamentals.py` (`fetch_quarterly_financials` +
`_build_signals`, run from a verbatim copy) against the live endpoint, retrying
through the 410 brownout. "EDGAR" figures come from the new code with
`compute_fundamental_signals(use_cache=False)`. All 10 Polygon runs succeeded
on retry. Nothing here is committed data; re-run the same way to refresh.

## Headline

* **Reported values agree exactly.** On every period both sources return,
  quarterly revenue differs by **0.000%** for 9 of 10 tickers (the exception,
  ATRC, is a Polygon data error, below). Gross margin (latest) is identical for
  all 10. Latest-quarter revenue, operating income and total assets are
  identical for all 10.
* **Fundamental scores differ materially for 6 of 10 tickers (more than
  10 points), and the new numbers are the correct ones.** The cause is not a
  data-source disagreement. **Polygon's quarterly list never contains a fourth
  quarter** (Q4 exists only in the 10-K annual figure), so its "8 quarters" were
  Q1-Q3 of ~3 fiscal years with 1-3 holes. `_build_signals` computes YoY as
  `revenues[i] - revenues[i-4]`, which assumes consecutive quarters, so with the
  holes it compared non-adjacent periods (e.g. BOX's "+16.2%" was the
  2026-07-31 quarter ($321M) against the 2025-04-30 quarter ($276M), 15 months
  earlier, not against 2025-07-31 ($294M, which gives +9.2%).
  The EDGAR series derives Q4 = FY - (Q1+Q2+Q3) and refuses to build a window
  with a missing quarter, so YoY is genuinely year over year.
* **Proof it is the gaps and nothing else:** feeding `_build_signals` the
  *EDGAR* values restricted to the exact period ends Polygon returned
  reproduces the Polygon score for all 10 tickers (table B).

## A. Screener `compute_fundamental_signals` (use_cache=False)

| Ticker | Qtrs old/new | Last-4 rev YoY (Polygon) | Last-4 rev YoY (EDGAR) | GM latest old/new | Score old/new | Flags (Polygon) | Flags (EDGAR) |
|---|---|---|---|---|---|---|---|
| BOX | 8/8 | +12.5%, +15.1%, +10.9%, +16.2% | +9.1%, +9.4%, +10.7%, +9.2% | 79.1%/79.1% | 40/0 | rev_growth_strong, op_margin_expanding | - |
| HPE | 8/8 | +26.8%, +20.6%, +36.0%, +60.1% | +14.4%, +18.4%, +40.0%, +33.7% | 0.0%/0.0% | 75/40 | rev_re_accelerating, rev_growth_strong, op_margin_expanding | rev_growth_strong, op_margin_expanding |
| TEVA | 8/8 | +9.3%, +7.6%, -8.1%, +6.5% | +3.4%, +11.4%, +2.3%, -0.8% | 52.0%/52.0% | 0/0 | - | rev_declining |
| ABBV | 8/8 | +25.3%, +9.1%, +3.7%, +27.3% | +9.1%, +10.0%, +12.4%, +10.2% | 74.7%/74.7% | 65/45 | rev_growth_strong, gross_margin_expanding, op_margin_expanding | gross_margin_expanding, op_margin_expanding |
| AMAT | 8/8 | +9.9%, +3.5%, +10.4%, +28.4% | -3.5%, -2.1%, +11.4%, +24.8% | 50.3%/50.3% | 100/100 | rev_re_accelerating, rev_growth_strong, gross_margin_expanding, op_margin_expanding | (same four) |
| RBRK | 8/8 | +86.9%, +84.3%, +39.0%, +37.9% | +48.3%, +46.3%, +39.0%, +37.9% | 78.4%/78.4% | 40/40 | rev_growth_strong, op_margin_expanding | (same) |
| TMO | 8/8 | +4.9%, +5.5%, +3.8%, +15.7% | +4.9%, +7.2%, +6.2%, +10.5% | 0.0%/0.0% | 20/0 | rev_growth_strong | - |
| TXN | 8/8 | -10.2%, +21.5%, +26.2%, +31.6% | +14.2%, +10.4%, +18.6%, +22.8% | 61.4%/61.4% | 100/100 | rev_re_accelerating, rev_growth_strong, gross_margin_expanding, op_margin_expanding | (same four) |
| FSLY | 8/8 | +11.4%, +19.5%, +26.1%, +26.9% | +15.3%, +22.8%, +19.8%, +23.3% | 63.3%/63.3% | 100/65 | rev_re_accelerating, rev_growth_strong, gross_margin_expanding, op_margin_expanding | rev_growth_strong, gross_margin_expanding, op_margin_expanding |
| ATRC | 8/8 | +28203.3%, +15.5%, +21.9%, +24.3% | +15.8%, +13.1%, +14.3%, +12.8% | 77.2%/77.2% | 100/45 | rev_re_accelerating, rev_growth_strong, gross_margin_expanding, op_margin_expanding | gross_margin_expanding, op_margin_expanding |

Score deltas beyond 10 points: BOX -40, HPE -35, ABBV -20, TMO -20, FSLY -35,
ATRC -55. All are explained by the Q4 gaps (and, for ATRC, a bad Polygon value).
Revenue deltas beyond 2% appear only in the YoY columns, never in the underlying
per-period revenue (table B).

### Operational consequence (read this)

Old screener scores were computed on gapped windows, so rankings built on them
were partly noise: BOX scored 40 on a "+16%" that is really +9%, and TEVA
(-0.8% latest YoY) now correctly carries `rev_declining`. After this change the
weekly screener ranking will shift, and `fundamental_score`-based ordering will
not be comparable to historical runs in `runs/index.db`. (The docs already
record the composite score as a weak candidate filter, not a ranking signal.)

## B. Like-for-like check (EDGAR values, Polygon's period set)

| Ticker | Polygon period ends missing a Q4 (gaps) | Common periods | Max revenue diff on common periods | Like-for-like score (EDGAR values, Polygon periods) vs Polygon | Like-for-like YoY equal? |
|---|---|---|---|---|---|
| BOX | 3 | 8/8 | 0.000% | 40 vs 40 | True |
| HPE | 2 | 8/8 | 0.000% | 75 vs 75 | True |
| TEVA | 2 | 8/8 | 0.000% | 0 vs 0 | True |
| ABBV | 2 | 8/8 | 0.000% | 65 vs 65 | True |
| AMAT | 2 | 8/8 | 0.000% | 100 vs 100 | True |
| RBRK | 1 | 8/8 | 0.000% | 40 vs 40 | True |
| TMO | 2 | 8/8 | 0.000% | 20 vs 20 | True |
| TXN | 3 | 8/8 | 0.000% | 100 vs 100 | True |
| FSLY | 2 | 8/8 | 0.000% | 100 vs 100 | True |
| ATRC | 2 | 8/8 | 22530% (see below) | 100 vs 100 | False |

**ATRC is a Polygon data error.** Polygon returned revenue of **$481,000** for
the 2024-03-31 quarter; EDGAR (and the company) report **$108.85M**. That bogus
value is what produced Polygon's "+28203%" YoY and its `rev_re_accelerating`
flag, and the false 100 score. EDGAR gives +15.8/+13.1/+14.3/+12.8%. (The
like-for-like score still lands on 100 only because the other windowed YoYs
accelerate on the gapped series; the YoY lists differ for exactly this reason.)

## C. Agent statements: latest-quarter key figures (Polygon raw entry vs EDGAR statement column)

Polygon column = latest entry of `/vX/reference/financials?timeframe=quarterly`
(the data the old `get_income_statement` / `get_balance_sheet` printed). EDGAR
column = last populated column of `route_to_vendor("get_income_statement" /
"get_balance_sheet", T, "quarterly", None)`.

| Ticker | Latest qtr (Polygon / EDGAR) | Revenue $M P/E | Op income $M P/E | Net income $M P/E | Total assets $M P/E |
|---|---|---|---|---|---|
| BOX | 2026-07-31 / 2026-07-31 | 321 / 321 | 33 / 33 | 19 / 19 | 1,404 / 1,404 |
| HPE | 2026-07-31 / 2026-07-31 | 12,213 / 12,213 | 1,393 / 1,393 | 1,540 / 1,540 | 83,596 / 83,596 |
| TEVA | 2026-06-30 / 2026-06-30 | 4,142 / 4,142 | -231 / -231 | -575 / -576 | 39,857 / 39,857 |
| ABBV | 2026-06-30 / 2026-06-30 | 16,990 / 16,990 | 6,432 / 6,432 | 3,616 / 3,613 | 135,115 / 135,115 |
| AMAT | 2026-07-26 / 2026-07-26 | 9,115 / 9,115 | 3,075 / 3,075 | 2,538 / 2,538 | 43,522 / 43,522 |
| RBRK | 2026-07-31 / 2026-07-31 | 427 / 427 | -72 / -72 | -62 / -62 | 2,846 / 2,846 |
| TMO | 2026-06-27 / 2026-06-27 | 11,994 / 11,994 | 2,087 / 2,087 | 1,741 / 1,736 | 113,174 / 113,174 |
| TXN | 2026-06-30 / 2026-06-30 | 5,463 / 5,463 | 2,310 / 2,310 | 1,980 / 1,980 | 35,882 / 35,882 |
| FSLY | 2026-06-30 / 2026-06-30 | 183 / 183 | -14 / -14 | -16 / -16 | 1,504 / 1,504 |
| ATRC | 2026-06-30 / 2026-06-30 | 154 / 154 | 10 / 10 | 9 / 9 | 677 / 677 |

(AMAT's pre-formatted Polygon statement text 410'd on every retry, so its
Polygon column is from the raw entry like the others.)

Differences:

* **Net income (TEVA, ABBV, TMO; up to 0.3%).** Polygon's "Net Income/Loss" is
  `ProfitLoss` (including non-controlling interests); EDGAR's `NetIncomeLoss`
  is attributable to the parent. Verified in the facts: ABBV ProfitLoss
  3,616 / NetIncomeLoss 3,613; TMO 1,741 / 1,736; TEVA -575 / -576. The EDGAR
  figure is the right one for EPS and P/E.
* **FSLY revenue tag (fixed during this work).** The first EDGAR run printed
  FSLY revenue as "unavailable (not tagged by this filer)": Fastly tags revenue
  `RevenueFromContractWithCustomerIncludingAssessedTax`, which upstream's
  statement tag list did not include. That tag is now in the statement revenue
  tags (and in the screener's). Revenue now matches (183 / 183).
* **Shape.** Polygon returned 16 quarterly columns of many Polygon-specific
  concept rows (e.g. "Wages", "Depreciation and Amortization") with blank cells
  in some periods, and no Q4. The EDGAR
  statements (unchanged upstream code) print 6 core lines per statement over the
  filer's *entire* history (BOX: ~38 quarterly columns), never derive a Q4 in the
  income statement, and show cash flows year to date where filed that way.
  Fewer lines, more columns; every figure is as filed with a filing date.
  "Long-term debt" appears only in the overview, not in the statement.

## Other behavioural differences worth knowing

* **Overview (`get_fundamentals`) TTM** is now a true four-consecutive-quarter
  sum (Q4 derived). The old Polygon TTM summed "the latest four entries" in the
  gapped list, i.e. non-adjacent quarters, so it was wrong whenever a Q4 fell in
  the window. BOX now reads Revenue (TTM) $1.23B, Net income (TTM) $130.69M.
* **Per-share TTM** (EPS, hence P/E) sums quarterly diluted EPS; a derived Q4 is
  annual EPS less three quarters, which absorbs share-count drift (small).
* **Coverage.** ADRs / foreign filers (no us-gaap facts), recent IPOs and tickers
  absent from SEC's ticker map return `insufficient_data` in the screener, and in
  the overview raise `NoMarketDataError` so the router falls through to
  yfinance. Filers that tag no revenue concept in
  `_REVENUE_TAGS` (some banks/insurers) also read as insufficient data; the
  10 tickers checked here did not include one, so the size of this gap is not
  measured.
* **Point in time.** EDGAR gives each fact's filing date, so a past `as_of_date`
  only sees what was on file (inclusive of the as-of day, matching the
  existing `sec_edgar` statements). Polygon's rule was strictly before.
