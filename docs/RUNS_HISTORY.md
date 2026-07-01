# TradingAgents Run History

Generated: 2026-06-30

This inventory documents the TradingAgents pipeline run artifacts under `runs/` to date. The operating cadence is the locked-in weekly Friday screener, on-demand matrix only for NEW tickers, and same-day options refresh before entry. Raw JSON/Markdown files in each run directory are authoritative; `runs/index.db` is a derived catalog rebuilt by `scripts/index_runs.py`.

## At-a-glance

| Metric | Value | Source |
| --- | --- | --- |
| Screener directories found | 32 | `runs/screener_*` |
| Matrix directories found | 37 | `runs/matrix_*` |
| Matrix rows analyzed | 358 | `runs/matrix_*/verdict_ledger.json` |
| Overall PICK / VETOED | 106 / 249 | `verdict_ledger.json` per run |
| Derived matrix tier counts | A: 25, B: 14, C: 67, VETO: 249, —: 3 | Derived row rule |
| Chronological span | 2026-04-29 → 2026-06-26 | Run manifests / directory names |

## Matrix timeline

| Date | Run id | Universe | Analyzed | #PICK | #VETOED | Picks (derived tier) | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-04-30 | matrix_2026-04-30_1839_top25 | unspecified | 25 | 10 | 15 | ADI (A), APD (A), APH (C), ARMK (B), AROC (A), EQIX (C), KEY (C), TJX (C), TRGP (A), VIRT (C) | `runs/matrix_2026-04-30_1839_top25/verdict_ledger.json` |
| 2026-05-01 | matrix_2026-05-01_top25 | unspecified | 25 | 11 | 14 | AROC (B), CRUS (C), CTRE (A), FHB (B), GVA (C), IDA (A), KALU (C), LGND (C), PRIM (C), VIRT (B), VNOM (A) | `runs/matrix_2026-05-01_top25/verdict_ledger.json` |
| 2026-05-05 | matrix_portfolio_2026-05-05 | single/ad hoc | 9 | 2 | 7 | METC (C), MSFT (A) | `runs/matrix_portfolio_2026-05-05/verdict_ledger.json` |
| 2026-05-06 | matrix_megacap_followup_2026-05-06 | unspecified | 6 | 1 | 5 | GOOGL (C) | `runs/matrix_megacap_followup_2026-05-06/verdict_ledger.json` |
| 2026-05-06 | matrix_weekly_2026-05-06_chain | unspecified | 5 | 4 | 1 | GS (C), KO (A), NVDA (C), TXN (A) | `runs/matrix_weekly_2026-05-06_chain/verdict_ledger.json` |
| 2026-05-08 | matrix_ablation_no_insider_2026-05-08_2228 | unspecified | 7 | 4 | 3 | ARMK (C), FHB (A), VIRT (C), VNOM (C) | `runs/matrix_ablation_no_insider_2026-05-08_2228/verdict_ledger.json` |
| 2026-05-08 | matrix_fixed_insider_2026-05-08_2303 | unspecified | 7 | 5 | 2 | ARMK (C), CTRE (A), FHB (A), VIRT (B), VNOM (C) | `runs/matrix_fixed_insider_2026-05-08_2303/verdict_ledger.json` |
| 2026-05-08 | matrix_large_weekly_2026-05-08_1754_chain | large | 16 | 3 | 13 | HAS (B), IEX (C), STT (B) | `runs/matrix_large_weekly_2026-05-08_1754_chain/verdict_ledger.json` |
| 2026-05-08 | matrix_mega_weekly_2026-05-08_1754_chain | mega | 17 | 7 | 10 | AMZN (C), BAC (C), C (C), COST (A), GE (C), MS (C), WMT (C) | `runs/matrix_mega_weekly_2026-05-08_1754_chain/verdict_ledger.json` |
| 2026-05-08 | matrix_mid_weekly_2026-05-08_1754_chain | mid | 16 | 6 | 9 | AVT (C), CARG (C), CXW (C), RSI (C), SLAB (C), YOU (C) | `runs/matrix_mid_weekly_2026-05-08_1754_chain/verdict_ledger.json` |
| 2026-05-08 | matrix_refresh_priortier_ab_2026-05-08_2053 | unspecified | 10 | 6 | 4 | APD (A), ARMK (C), AROC (B), FHB (C), IDA (A), VNOM (C) | `runs/matrix_refresh_priortier_ab_2026-05-08_2053/verdict_ledger.json` |
| 2026-05-08 | matrix_virt_20260508_1452 | single/ad hoc | 1 | 1 | 0 | VIRT (C) | `runs/matrix_virt_20260508_1452/verdict_ledger.json` |
| 2026-05-09 | matrix_ai_stack_2026-05-09 | theme: AI stack | 19 | 4 | 15 | AMZN (C), CEG (C), GOOGL (C), TSM (B) | `runs/matrix_ai_stack_2026-05-09/verdict_ledger.json` |
| 2026-05-09 | matrix_ai_stack_2026-05-09_b | theme: AI stack | 15 | 1 | 14 | ETN (C) | `runs/matrix_ai_stack_2026-05-09_b/verdict_ledger.json` |
| 2026-05-09 | matrix_ai_stack_2026-05-09_full | theme: AI stack | 19 | 4 | 15 | AMZN (B), GOOGL (C), TER (B), TSM (C) | `runs/matrix_ai_stack_2026-05-09_full/verdict_ledger.json` |
| 2026-05-14 | matrix_amzn_2026-05-14 | single/ad hoc | 1 | 1 | 0 | AMZN (C) | `runs/matrix_amzn_2026-05-14/verdict_ledger.json` |
| 2026-05-14 | matrix_ilmn_2026-05-14 | single/ad hoc | 1 | 0 | 1 | — | `runs/matrix_ilmn_2026-05-14/verdict_ledger.json` |
| 2026-05-14 | matrix_virt_2026-05-14 | single/ad hoc | 1 | 0 | 1 | — | `runs/matrix_virt_2026-05-14/verdict_ledger.json` |
| 2026-05-15 | matrix_large_weekly_2026-05-15_2251_chain | large | 24 | 7 | 17 | AFL (C), EQIX (C), ET (A), HL (C), KMI (A), STLD (A), TT (C) | `runs/matrix_large_weekly_2026-05-15_2251_chain/verdict_ledger.json` |
| 2026-05-15 | matrix_mega_weekly_2026-05-15_2236_chain | mega | 19 | 8 | 11 | ADI (C), AVGO (C), GOOG (C), GOOGL (C), KO (A), LIN (C), LLY (C), PM (B) | `runs/matrix_mega_weekly_2026-05-15_2236_chain/verdict_ledger.json` |
| 2026-05-15 | matrix_weekly_2026-05-15_1957_chain | unspecified | 5 | 1 | 4 | DAR (C) | `runs/matrix_weekly_2026-05-15_1957_chain/verdict_ledger.json` |
| 2026-05-15 | matrix_weekly_2026-05-15_extended | unspecified | 20 | 5 | 14 | AROC (C), GVA (A), RSI (C), SSRM (C), YOU (B) | `runs/matrix_weekly_2026-05-15_extended/verdict_ledger.json` |
| 2026-05-22 | matrix_large_weekly_2026-05-22_1142_chain | large | 10 | 2 | 8 | EBAY (B), TJX (C) | `runs/matrix_large_weekly_2026-05-22_1142_chain/verdict_ledger.json` |
| 2026-05-22 | matrix_mega_weekly_2026-05-22_1139_chain | mega | 10 | 4 | 6 | AMZN (C), C (C), GS (A), VZ (A) | `runs/matrix_mega_weekly_2026-05-22_1139_chain/verdict_ledger.json` |
| 2026-05-22 | matrix_mid_weekly_2026-05-22_1142_chain | mid | 10 | 1 | 9 | CRUS (C) | `runs/matrix_mid_weekly_2026-05-22_1142_chain/verdict_ledger.json` |
| 2026-05-30 | matrix_large_weekly_2026-05-30_1357_chain | large | 5 | 0 | 5 | — | `runs/matrix_large_weekly_2026-05-30_1357_chain/verdict_ledger.json` |
| 2026-05-30 | matrix_mega_weekly_2026-05-30_1335_chain | mega | 5 | 1 | 4 | ADI (C) | `runs/matrix_mega_weekly_2026-05-30_1335_chain/verdict_ledger.json` |
| 2026-05-30 | matrix_weekly_2026-05-30_1247_chain | unspecified | 5 | 0 | 5 | — | `runs/matrix_weekly_2026-05-30_1247_chain/verdict_ledger.json` |
| 2026-06-06 | matrix_large_weekly_2026-06-06_2302_chain | large | 5 | 0 | 5 | — | `runs/matrix_large_weekly_2026-06-06_2302_chain/verdict_ledger.json` |
| 2026-06-06 | matrix_mega_weekly_2026-06-06_2323_chain | mega | 5 | 1 | 4 | C (A) | `runs/matrix_mega_weekly_2026-06-06_2323_chain/verdict_ledger.json` |
| 2026-06-06 | matrix_mid_weekly_2026-06-06_2239_chain | mid | 5 | 0 | 5 | — | `runs/matrix_mid_weekly_2026-06-06_2239_chain/verdict_ledger.json` |
| 2026-06-12 | matrix_large_weekly_2026-06-12_1051_chain | large | 5 | 0 | 5 | — | `runs/matrix_large_weekly_2026-06-12_1051_chain/verdict_ledger.json` |
| 2026-06-12 | matrix_mega_weekly_2026-06-12_1115_chain | mega | 5 | 2 | 3 | ADI (C), GS (C) | `runs/matrix_mega_weekly_2026-06-12_1115_chain/verdict_ledger.json` |
| 2026-06-12 | matrix_mid_weekly_2026-06-12_1021_chain | mid | 5 | 1 | 3 | KLIC (C) | `runs/matrix_mid_weekly_2026-06-12_1021_chain/verdict_ledger.json` |
| 2026-06-26 | matrix_large_weekly_2026-06-26_2126_chain | large | 5 | 1 | 4 | NUE (A) | `runs/matrix_large_weekly_2026-06-26_2126_chain/verdict_ledger.json` |
| 2026-06-26 | matrix_mega_weekly_2026-06-26_2146_chain | mega | 5 | 1 | 4 | ADI (C) | `runs/matrix_mega_weekly_2026-06-26_2146_chain/verdict_ledger.json` |
| 2026-06-26 | matrix_mid_weekly_2026-06-26_2103_chain | mid | 5 | 1 | 4 | KLIC (C) | `runs/matrix_mid_weekly_2026-06-26_2103_chain/verdict_ledger.json` |

## Screener run inventory

| Trading date | Run id | Tier/universe | Universe size | Candidates | Top tickers (up to 10) | Partial? | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-04-29 | screener_2026-04-30_0809 | mcap $2B–$200B | 40 | 10 | LITE, ROG, TTE, NBIS, AKAM, FLEX, ALAB, ONB, MOS, LAUR | — | `runs/screener_2026-04-30_0809/screener.json` |
| 2026-04-29 | screener_2026-04-30_0815 | mcap $2B–$200B | 164 | 20 | ADI, STLD, KEY, CTRE, COHU, MTSI, WULF, AXGN, SLAB, BIIB | — | `runs/screener_2026-04-30_0815/screener.json` |
| 2026-04-29 | screener_2026-04-30_0920 | mcap $2B–$200B | 20 | 10 | LITE, ROG, RIVN, FLEX, ONB, EXTR, MOS, PNFP, LAUR, Z | yes | `runs/screener_2026-04-30_0920/screener.json` |
| 2026-04-29 | screener_2026-04-30_0926 | mcap $2B–$200B | 27 | 10 | LITE, STLD, MTSI, ROG, PL, RIVN, PAYX, AKAM, FLEX, ONB | no | `runs/screener_2026-04-30_0926/screener.json` |
| 2026-04-29 | screener_2026-04-30_0955 | mcap $2B–$200B | 1155 | 25 | ABNB, TVTX, DAR, APD, ADI, SNDK, AROC, AAOI, ALGM, TRGP | no | `runs/screener_2026-04-30_0955/screener.json` |
| 2026-04-30 | screener_2026-04-30_0905 | unspecified | — | missing | — | — | `missing screener.json` |
| 2026-04-30 | screener_2026-04-30_0932 | unspecified | — | missing | — | — | `missing screener.json` |
| 2026-04-30 | screener_2026-05-01_0750 | mid (by mcap filter) | 543 | 25 | TVTX, DAR, SXI, KLIC, KALU, FHB, VFC, AROC, VIRT, VNOM | yes | `runs/screener_2026-05-01_0750/screener.json` |
| 2026-05-05 | screener_2026-05-06_1027 | mega (by mcap filter) | 54 | 25 | TXN, GS, KO, NVDA, AVGO, LLY, SNDK, MU, AMD, ANET | yes | `runs/screener_2026-05-06_1027/screener.json` |
| 2026-05-05 | screener_2026-05-06_1705 | mega (by mcap filter) | 54 | 25 | TXN, GS, KO, NVDA, AVGO, LLY, SNDK, MU, AMD, ANET | no | `runs/screener_2026-05-06_1705/screener.json` |
| 2026-05-07 | screener_large_2026-05-08_1734 | large | 509 | 25 | ABNB, ADI, STLD, ROK, ON, AAOI, EBAY, SNDK, LITE, APD | no | `runs/screener_large_2026-05-08_1734/screener.json` |
| 2026-05-07 | screener_mega_2026-05-08_1736 | mega | 52 | 25 | TXN, MU, GS, KO, AMD, AMZN, AVGO, LLY, BAC, LRCX | no | `runs/screener_mega_2026-05-08_1736/screener.json` |
| 2026-05-07 | screener_mid_2026-05-08_1730 | mid | 571 | 25 | RSI, AVT, KMT, THR, SLAB, COHU, LION, UUUU, VFC, CENX | no | `runs/screener_mid_2026-05-08_1730/screener.json` |
| 2026-05-08 | screener_mid_2026-05-08_1636 | mid | — | missing | — | — | `missing screener.json` |
| 2026-05-14 | screener_2026-05-15_1954 | mid (by mcap filter) | 474 | 25 | DAR, CORZ, SLAB, RDW, COHU, KLIC, LION, ONDS, AVT, RSI | no | `runs/screener_2026-05-15_1954/screener.json` |
| 2026-05-14 | screener_large_2026-05-15_2239 | large | 424 | 25 | WDC, VICR, APD, LITE, ROK, STLD, EBAY, LSCC, NUE, CRDO | no | `runs/screener_large_2026-05-15_2239/screener.json` |
| 2026-05-14 | screener_mega_2026-05-15_2235 | mega | 52 | 25 | ADI, SNDK, MU, GS, AMZN, TXN, AMD, C, AVGO, LLY | no | `runs/screener_mega_2026-05-15_2235/screener.json` |
| 2026-05-21 | screener_large_2026-05-22_1138 | large | 424 | 25 | WDC, NUE, CRDO, EBAY, ROK, TJX, LSCC, TTMI, LITE, PWR | no | `runs/screener_large_2026-05-22_1138/screener.json` |
| 2026-05-21 | screener_mega_2026-05-22_1138 | mega | 54 | 25 | TXN, SNDK, MU, AMD, GS, C, AMZN, LLY, VZ, GOOG | no | `runs/screener_mega_2026-05-22_1138/screener.json` |
| 2026-05-21 | screener_mid_2026-05-22_1138 | mid | 452 | 25 | CORZ, KLIC, MXL, SLAB, AVT, OSCR, LION, VSH, RDW, CRUS | no | `runs/screener_mid_2026-05-22_1138/screener.json` |
| 2026-05-29 | screener_2026-05-30_1243 | mid (by mcap filter) | 565 | 25 | CORZ, KLIC, OUST, KALU, SLAB, AVT, COHU, FROG, THR, RCAT | no | `runs/screener_2026-05-30_1243/screener.json` |
| 2026-05-29 | screener_large_2026-05-30_1355 | large | 563 | 25 | ARW, CRDO, CPAY, ROK, TWLO, WDC, TJX, NET, PWR, RKLB | no | `runs/screener_large_2026-05-30_1355/screener.json` |
| 2026-05-29 | screener_mega_2026-05-30_1334 | mega | 55 | 25 | ADI, SNDK, TXN, C, AMZN, MU, LLY, PLTR, GS, AMD | no | `runs/screener_mega_2026-05-30_1334/screener.json` |
| 2026-06-05 | screener_large_2026-06-06_2300 | large | 438 | 25 | FROG, NUE, CRDO, ROK, ADI, WDC, TWLO, MNST, STLD, DDOG | no | `runs/screener_large_2026-06-06_2300/screener.json` |
| 2026-06-05 | screener_mega_2026-06-06_2322 | mega | 53 | 25 | MU, SNDK, C, GS, MRVL, LLY, AMD, TXN, GOOG, DELL | no | `runs/screener_mega_2026-06-06_2322/screener.json` |
| 2026-06-05 | screener_mid_2026-06-06_2236 | mid | 475 | 25 | OSCR, RAL, SLAB, RDW, LQDA, HNGE, XMTR, OUST, PSMT, KLIC | no | `runs/screener_mid_2026-06-06_2236/screener.json` |
| 2026-06-11 | screener_large_2026-06-12_1044 | large | 443 | 25 | KEYS, CRDO, NUE, ROK, WDC, TTMI, BTSG, STLD, FTNT, LSCC | no | `runs/screener_large_2026-06-12_1044/screener.json` |
| 2026-06-11 | screener_mega_2026-06-12_1114 | mega | 56 | 25 | MU, ADI, SNDK, GS, C, TXN, MRVL, BAC, AMD, LLY | no | `runs/screener_mega_2026-06-12_1114/screener.json` |
| 2026-06-11 | screener_mid_2026-06-12_1014 | mid | 514 | 25 | KLIC, RSI, KALU, VFC, OSCR, SLAB, RDW, FROG, HNGE, VSH | no | `runs/screener_mid_2026-06-12_1014/screener.json` |
| 2026-06-25 | screener_large_2026-06-26_1829 | large | 459 | 25 | NUE, CRDO, ALGM, KEYS, ROK, SNX, STT, MTZ, FTNT, DKS | no | `runs/screener_large_2026-06-26_1829/screener.json` |
| 2026-06-25 | screener_mega_2026-06-26_1836 | mega | 58 | 25 | MU, ADI, SNDK, WDC, TXN, GS, DELL, AVGO, MRVL, AMD | no | `runs/screener_mega_2026-06-26_1836/screener.json` |
| 2026-06-25 | screener_mid_2026-06-26_1820 | mid | 519 | 25 | RSI, KALU, AVT, ESTA, KLIC, KN, FROG, VSH, COHU, OSCR | no | `runs/screener_mid_2026-06-26_1820/screener.json` |

## Per-run matrix details

### matrix_2026-04-30_1839_top25

- Date/universe: **2026-04-30**, **unspecified**; source `runs/matrix_2026-04-30_1839_top25/verdict_ledger.json`.
- Verdicts: **25 analyzed**, **10 PICK**, **15 VETOED**; derived tiers: A=4, B=1, C=5, VETO=15.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ADI | A | $402.26 | $408.00 | $420.00 | -2.9% |
| APD | A | $300.05 | $330.00 | $315.00 | 4.5% |
| APH | C | $147.27 | $166.00 | — | — |
| ARMK | B | $45.69 | $52.00 | $47.00 | 9.6% |
| AROC | A | $38.75 | $42.00 | $40.50 | 3.6% |
| EQIX | C | $1,082.83 | $1,250.00 | — | — |
| KEY | C | $22.11 | $23.34 | — | — |
| TJX | C | $156.75 | $180.00 | — | — |
| TRGP | A | $260.08 | $270.00 | $262.50 | 2.8% |
| VIRT | C | $49.66 | $65.00 | — | — |

Options overlay: `runs/matrix_2026-04-30_1839_top25/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **10** of **10** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ADI | A | long_call | 2026-09-18 | long call 400 @ $45.20 (delta 0.55) | $4,520.00 | $445.20 |
| APD | A | long_call | 2026-09-18 | long call 300 @ $22.40 (delta 0.54) | $2,240.00 | $322.40 |
| APH | C | long_call | 2027-03-19 | long call 160 @ $20.38 (delta 0.51) | $2,038.00 | $180.38 |
| ARMK | B | long_call | 2026-10-16 | long call 49 @ $2.00 (delta 0.39) | $200.00 | $51.00 |
| AROC | A | long_call | 2026-11-20 | long call 35 @ $6.10 (delta 0.69) | $610.00 | $41.10 |
| EQIX | C | long_call | 2026-12-18 | long call 1020 @ $158.00 (delta 0.64) | $15,800.00 | $1,178.00 |
| KEY | C | long_call | 2026-09-18 | long call 22 @ $1.30 (delta 0.52) | $130.00 | $23.30 |
| TJX | C | long_call | 2027-01-15 | long call 160 @ $12.50 (delta 0.53) | $1,250.00 | $172.50 |
| TRGP | A | long_call | 2026-09-18 | long call 260 @ $20.60 (delta 0.51) | $2,060.00 | $280.60 |
| VIRT | C | long_call | 2026-09-18 | long call 48 @ $4.59 (delta 0.57) | $459.00 | $52.59 |

### matrix_2026-05-01_top25

- Date/universe: **2026-05-01**, **unspecified**; source `runs/matrix_2026-05-01_top25/verdict_ledger.json`.
- Verdicts: **25 analyzed**, **11 PICK**, **14 VETOED**; derived tiers: A=3, B=3, C=5, VETO=14.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AROC | B | $38.75 | $45.00 | $42.00 | 6.7% |
| CRUS | C | $163.08 | $190.00 | — | — |
| CTRE | A | $39.45 | $41.70 | $41.70 | 0.0% |
| FHB | B | $27.28 | $30.00 | $28.50 | 5.0% |
| GVA | C | $137.07 | $150.00 | — | — |
| IDA | A | $147.74 | $156.00 | $155.00 | 0.6% |
| KALU | C | $170.43 | $200.00 | — | — |
| LGND | C | $229.45 | $270.00 | — | — |
| PRIM | C | $181.15 | $220.00 | — | — |
| VIRT | B | $49.66 | $65.00 | $52.15 | 19.8% |
| VNOM | A | $49.38 | $52.00 | $52.50 | -1.0% |

Options overlay: `runs/matrix_2026-05-01_top25/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **11** of **11** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AROC | B | long_call | 2026-11-20 | long call 35 @ $6.10 (delta 0.72) | $610.00 | $41.10 |
| CRUS | C | long_call | 2026-09-18 | long call 170 @ $15.60 (delta 0.53) | $1,560.00 | $185.60 |
| CTRE | A | long_call | 2026-10-16 | long call 40 @ $2.00 (delta 0.49) | $200.00 | $42.00 |
| FHB | B | long_call | 2026-09-18 | long call 30 @ $1.73 (delta 0.44) | $173.00 | $31.73 |
| GVA | C | long_call | 2026-12-18 | long call 140 @ $7.50 (delta 0.57) | $750.00 | $147.50 |
| IDA | A | long_call | 2026-11-20 | long call 145 @ $9.43 (delta 0.57) | $943.00 | $154.43 |
| KALU | C | long_call | 2026-11-20 | long call 170 @ $16.80 (delta 0.60) | $1,680.00 | $186.80 |
| LGND | C | long_call | 2026-08-21 | long call 220 @ $27.09 (delta 0.59) | $2,709.00 | $247.09 |
| PRIM | C | long_call | 2026-12-18 | long call 190 @ $25.71 (delta 0.56) | $2,571.00 | $215.71 |
| VIRT | B | long_call | 2026-09-18 | long call 48 @ $4.59 (delta 0.55) | $459.00 | $52.59 |
| VNOM | A | long_call | 2026-09-18 | long call 50 @ $3.80 (delta 0.53) | $380.00 | $53.80 |

### matrix_portfolio_2026-05-05

- Date/universe: **2026-05-05**, **single/ad hoc**; source `runs/matrix_portfolio_2026-05-05/verdict_ledger.json`.
- Verdicts: **9 analyzed**, **2 PICK**, **7 VETOED**; derived tiers: A=1, B=0, C=1, VETO=7.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| METC | C | $14.49 | $18.00 | — | — |
| MSFT | A | $413.62 | $466.00 | $466.00 | 0.0% |

Options overlay: `runs/matrix_portfolio_2026-05-05/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **2** of **2** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| METC | C | long_call | 2026-09-18 | long call 16 @ $2.80 (delta 0.56) | $280.00 | $18.80 |
| MSFT | A | long_call | 2027-01-15 | long call 420 @ $40.00 (delta 0.54) | $4,000.00 | $460.00 |

### matrix_megacap_followup_2026-05-06

- Date/universe: **2026-05-06**, **unspecified**; source `runs/matrix_megacap_followup_2026-05-06/verdict_ledger.json`.
- Verdicts: **6 analyzed**, **1 PICK**, **5 VETOED**; derived tiers: A=0, B=0, C=1, VETO=5.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| GOOGL | C | $388.43 | $450.00 | — | — |

Options overlay: `runs/matrix_megacap_followup_2026-05-06/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| GOOGL | C | long_call | 2027-01-15 | long call 410 @ $46.00 (delta 0.55) | $4,600.00 | $456.00 |

### matrix_weekly_2026-05-06_chain

- Date/universe: **2026-05-06**, **unspecified**; source `runs/matrix_weekly_2026-05-06_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **4 PICK**, **1 VETOED**; derived tiers: A=2, B=0, C=2, VETO=1.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| GS | C | $918.89 | $981.00 | — | — |
| KO | A | $78.48 | $82.00 | $82.00 | 0.0% |
| NVDA | C | $196.50 | $265.00 | — | — |
| TXN | A | $281.00 | $304.00 | $303.00 | 0.3% |

Options overlay: `runs/matrix_weekly_2026-05-06_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **4** of **4** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| GS | C | long_call | 2026-09-18 | long call 930 @ $66.10 (delta 0.55) | $6,610.00 | $996.10 |
| KO | A | long_call | 2026-09-18 | long call 77.5 @ $4.54 (delta 0.58) | $454.00 | $82.04 |
| NVDA | C | long_call | 2027-12-17 | long call 245 @ $40.88 (delta 0.56) | $4,088.00 | $285.88 |
| TXN | A | long_call | 2026-09-18 | long call 290 @ $27.75 (delta 0.55) | $2,775.00 | $317.75 |

### matrix_ablation_no_insider_2026-05-08_2228

- Date/universe: **2026-05-08**, **unspecified**; source `runs/matrix_ablation_no_insider_2026-05-08_2228/verdict_ledger.json`.
- Verdicts: **7 analyzed**, **4 PICK**, **3 VETOED**; derived tiers: A=1, B=0, C=3, VETO=3.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ARMK | C | $45.08 | $52.00 | — | — |
| FHB | A | $27.34 | $30.00 | $30.00 | 0.0% |
| VIRT | C | $51.31 | $65.00 | — | — |
| VNOM | C | $46.79 | $55.00 | — | — |

Options overlay: `runs/matrix_ablation_no_insider_2026-05-08_2228/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **4** of **4** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ARMK | C | long_call | 2026-10-16 | long call 49 @ $2.00 (delta 0.39) | $200.00 | $51.00 |
| FHB | A | long_call | 2026-09-18 | long call 25 @ $2.22 (delta 0.66) | $222.00 | $27.22 |
| VIRT | C | long_call | 2026-09-18 | long call 50 @ $4.30 (delta 0.60) | $430.00 | $54.30 |
| VNOM | C | long_call | 2026-09-18 | long call 47 @ $3.60 (delta 0.53) | $360.00 | $50.60 |

### matrix_fixed_insider_2026-05-08_2303

- Date/universe: **2026-05-08**, **unspecified**; source `runs/matrix_fixed_insider_2026-05-08_2303/verdict_ledger.json`.
- Verdicts: **7 analyzed**, **5 PICK**, **2 VETOED**; derived tiers: A=2, B=1, C=2, VETO=2.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ARMK | C | $45.08 | $52.00 | — | — |
| CTRE | A | $41.60 | $45.00 | $45.00 | 0.0% |
| FHB | A | $27.34 | $30.00 | $30.00 | 0.0% |
| VIRT | B | $51.31 | $58.00 | $55.00 | 5.2% |
| VNOM | C | $46.79 | $51.50 | — | — |

Options overlay: `runs/matrix_fixed_insider_2026-05-08_2303/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **5** of **5** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ARMK | C | long_call | 2026-10-16 | long call 49 @ $2.00 (delta 0.39) | $200.00 | $51.00 |
| CTRE | A | long_call | 2026-10-16 | long call 40 @ $2.30 (delta 0.63) | $230.00 | $42.30 |
| FHB | A | long_call | 2026-09-18 | long call 25 @ $2.22 (delta 0.66) | $222.00 | $27.22 |
| VIRT | B | long_call | 2026-09-18 | long call 50 @ $4.30 (delta 0.60) | $430.00 | $54.30 |
| VNOM | C | long_call | 2026-09-18 | long call 47 @ $3.60 (delta 0.53) | $360.00 | $50.60 |

### matrix_large_weekly_2026-05-08_1754_chain

- Date/universe: **2026-05-08**, **large**; source `runs/matrix_large_weekly_2026-05-08_1754_chain/verdict_ledger.json`.
- Verdicts: **16 analyzed**, **3 PICK**, **13 VETOED**; derived tiers: A=0, B=2, C=1, VETO=13.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| HAS | B | $97.39 | $112.00 | $102.50 | 8.5% |
| IEX | C | $214.87 | $235.00 | — | — |
| STT | B | $148.78 | $172.00 | $156.18 | 9.2% |

Options overlay: `runs/matrix_large_weekly_2026-05-08_1754_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **3** of **3** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| HAS | B | long_call | 2026-09-18 | long call 95 @ $9.40 (delta 0.60) | $940.00 | $104.40 |
| IEX | C | long_call | 2026-10-16 | long call 220 @ $16.92 (delta 0.52) | $1,692.00 | $236.92 |
| STT | B | long_call | 2026-09-18 | long call 145 @ $12.00 (delta 0.61) | $1,200.00 | $157.00 |

### matrix_mega_weekly_2026-05-08_1754_chain

- Date/universe: **2026-05-08**, **mega**; source `runs/matrix_mega_weekly_2026-05-08_1754_chain/verdict_ledger.json`.
- Verdicts: **17 analyzed**, **7 PICK**, **10 VETOED**; derived tiers: A=1, B=0, C=6, VETO=10.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AMZN | C | $271.17 | $290.00 | — | — |
| BAC | C | $52.75 | $56.50 | — | — |
| C | C | $129.09 | $150.00 | — | — |
| COST | A | $1,012.06 | $1,070.00 | $1,035.00 | 3.3% |
| GE | C | $302.63 | $345.00 | — | — |
| MS | C | $190.17 | $208.00 | — | — |
| WMT | C | $130.20 | $138.00 | — | — |

Options overlay: `runs/matrix_mega_weekly_2026-05-08_1754_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **7** of **7** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AMZN | C | long_call | 2027-01-15 | long call 280 @ $30.76 (delta 0.55) | $3,076.00 | $310.76 |
| BAC | C | long_call | 2026-09-18 | long call 50 @ $4.25 (delta 0.60) | $425.00 | $54.25 |
| C | C | long_call | 2027-01-15 | long call 125 @ $14.64 (delta 0.57) | $1,464.00 | $139.64 |
| COST | A | long_call | 2026-09-18 | long call 1020 @ $57.20 (delta 0.52) | $5,720.00 | $1,077.20 |
| GE | C | long_call | 2026-09-18 | long call 300 @ $27.29 (delta 0.55) | $2,729.00 | $327.29 |
| MS | C | long_call | 2026-09-18 | long call 190 @ $15.34 (delta 0.57) | $1,534.00 | $205.34 |
| WMT | C | long_call | 2026-09-18 | long call 130 @ $10.07 (delta 0.56) | $1,007.00 | $140.07 |

### matrix_mid_weekly_2026-05-08_1754_chain

- Date/universe: **2026-05-08**, **mid**; source `runs/matrix_mid_weekly_2026-05-08_1754_chain/verdict_ledger.json`.
- Verdicts: **16 analyzed**, **6 PICK**, **9 VETOED**; derived tiers: A=0, B=0, C=6, VETO=9.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AVT | C | $80.86 | $90.00 | — | — |
| CARG | C | $38.16 | $39.00 | — | — |
| CXW | C | $21.82 | $23.50 | — | — |
| RSI | C | $27.90 | $35.00 | — | — |
| SLAB | C | $217.64 | $240.00 | — | — |
| YOU | C | $58.17 | $62.00 | — | — |

Options overlay: `runs/matrix_mid_weekly_2026-05-08_1754_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **6** of **6** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AVT | C | long_call | 2026-08-21 | long call 75 @ $9.61 (delta 0.71) | $961.00 | $84.61 |
| CARG | C | long_call | 2026-08-21 | long call 35 @ $2.20 (delta 0.56) | $220.00 | $37.20 |
| CXW | C | long_call | 2026-09-18 | long call 21 @ $2.35 (delta 0.52) | $235.00 | $23.35 |
| RSI | C | long_call | 2026-10-16 | long call 30 @ $3.25 (delta 0.50) | $325.00 | $33.25 |
| SLAB | C | long_call | 2026-10-16 | long call 210 @ $10.80 (delta 0.77) | $1,080.00 | $220.80 |
| YOU | C | long_call | 2026-08-21 | long call 59.8 @ $7.20 (delta 0.50) | $720.00 | $67.00 |

### matrix_refresh_priortier_ab_2026-05-08_2053

- Date/universe: **2026-05-08**, **unspecified**; source `runs/matrix_refresh_priortier_ab_2026-05-08_2053/verdict_ledger.json`.
- Verdicts: **10 analyzed**, **6 PICK**, **4 VETOED**; derived tiers: A=2, B=1, C=3, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| APD | A | $295.41 | $330.00 | $315.00 | 4.5% |
| ARMK | C | $45.08 | $52.00 | — | — |
| AROC | B | $36.96 | $45.00 | $40.00 | 11.1% |
| FHB | C | $27.34 | $32.00 | — | — |
| IDA | A | $144.00 | $156.00 | $150.00 | 3.8% |
| VNOM | C | $46.79 | $52.00 | — | — |

Options overlay: `runs/matrix_refresh_priortier_ab_2026-05-08_2053/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **6** of **6** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| APD | A | long_call | 2026-09-18 | long call 290 @ $21.20 (delta 0.57) | $2,120.00 | $311.20 |
| ARMK | C | long_call | 2026-10-16 | long call 49 @ $2.00 (delta 0.39) | $200.00 | $51.00 |
| AROC | B | long_call | 2026-11-20 | long call 35 @ $5.61 (delta 0.61) | $561.00 | $40.61 |
| FHB | C | long_call | 2026-12-18 | long call 25 @ $5.91 (delta 0.65) | $591.00 | $30.91 |
| IDA | A | long_call | 2026-11-20 | long call 145 @ $9.43 (delta 0.51) | $943.00 | $154.43 |
| VNOM | C | long_call | 2026-09-18 | long call 47 @ $3.60 (delta 0.53) | $360.00 | $50.60 |

### matrix_virt_20260508_1452

- Date/universe: **2026-05-08**, **single/ad hoc**; source `runs/matrix_virt_20260508_1452/verdict_ledger.json`.
- Verdicts: **1 analyzed**, **1 PICK**, **0 VETOED**; derived tiers: A=0, B=0, C=1.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| VIRT | C | $49.54 | $65.00 | — | — |

Options overlay: `runs/matrix_virt_20260508_1452/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| VIRT | C | long_call | 2026-09-18 | long call 50 @ $4.30 (delta 0.56) | $430.00 | $54.30 |

### matrix_ai_stack_2026-05-09

- Date/universe: **2026-05-09**, **theme: AI stack**; source `runs/matrix_ai_stack_2026-05-09/verdict_ledger.json`.
- Verdicts: **19 analyzed**, **4 PICK**, **15 VETOED**; derived tiers: A=0, B=1, C=3, VETO=15.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AMZN | C | $272.68 | $305.00 | — | — |
| CEG | C | $303.63 | $330.00 | — | — |
| GOOGL | C | $400.80 | $450.00 | — | — |
| TSM | B | $411.68 | $445.00 | $420.00 | 5.6% |

Options overlay: `runs/matrix_ai_stack_2026-05-09/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **4** of **4** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AMZN | C | long_call | 2026-09-18 | long call 275 @ $22.50 (delta 0.55) | $2,250.00 | $297.50 |
| CEG | C | long_call | 2026-09-18 | long call 310 @ $34.40 (delta 0.56) | $3,440.00 | $344.40 |
| GOOGL | C | long_call | 2027-01-15 | long call 410 @ $47.57 (delta 0.56) | $4,757.00 | $457.57 |
| TSM | B | long_call | 2026-09-18 | long call 420 @ $42.00 (delta 0.54) | $4,200.00 | $462.00 |

### matrix_ai_stack_2026-05-09_b

- Date/universe: **2026-05-09**, **theme: AI stack**; source `runs/matrix_ai_stack_2026-05-09_b/verdict_ledger.json`.
- Verdicts: **15 analyzed**, **1 PICK**, **14 VETOED**; derived tiers: A=0, B=0, C=1, VETO=14.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ETN | C | $401.51 | $450.00 | — | — |

Options overlay: `runs/matrix_ai_stack_2026-05-09_b/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ETN | C | long_call | 2026-09-18 | long call 400 @ $38.08 (delta 0.57) | $3,808.00 | $438.08 |

### matrix_ai_stack_2026-05-09_full

- Date/universe: **2026-05-09**, **theme: AI stack**; source `runs/matrix_ai_stack_2026-05-09_full/verdict_ledger.json`.
- Verdicts: **19 analyzed**, **4 PICK**, **15 VETOED**; derived tiers: A=0, B=2, C=2, VETO=15.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AMZN | B | $272.68 | $337.50 | $312.00 | 7.6% |
| GOOGL | C | $400.80 | $450.00 | — | — |
| TER | B | $359.77 | $500.00 | $383.00 | 23.4% |
| TSM | C | $411.68 | $480.00 | — | — |

Options overlay: `runs/matrix_ai_stack_2026-05-09_full/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **4** of **4** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AMZN | B | long_call | 2027-12-17 | long call 300 @ $45.30 (delta 0.56) | $4,530.00 | $345.30 |
| GOOGL | C | long_call | 2027-01-15 | long call 410 @ $47.57 (delta 0.56) | $4,757.00 | $457.57 |
| TER | B | long_call | 2027-01-15 | long call 400 @ $72.60 (delta 0.56) | $7,260.00 | $472.60 |
| TSM | C | long_call | 2027-06-17 | long call 450 @ $69.20 (delta 0.54) | $6,920.00 | $519.20 |

### matrix_amzn_2026-05-14

- Date/universe: **2026-05-14**, **single/ad hoc**; source `runs/matrix_amzn_2026-05-14/verdict_ledger.json`.
- Verdicts: **1 analyzed**, **1 PICK**, **0 VETOED**; derived tiers: A=0, B=0, C=1.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AMZN | C | $270.13 | $312.00 | — | — |

Options overlay: `runs/matrix_amzn_2026-05-14/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AMZN | C | long_call | 2027-01-15 | long call 275 @ $31.94 (delta 0.55) | $3,194.00 | $306.94 |

### matrix_ilmn_2026-05-14

- Date/universe: **2026-05-14**, **single/ad hoc**; source `runs/matrix_ilmn_2026-05-14/verdict_ledger.json`.
- Verdicts: **1 analyzed**, **0 PICK**, **1 VETOED**; derived tiers: A=0, B=0, C=0, VETO=1.
No PICK rows in the ledger.

Options overlay: `runs/matrix_ilmn_2026-05-14/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_virt_2026-05-14

- Date/universe: **2026-05-14**, **single/ad hoc**; source `runs/matrix_virt_2026-05-14/verdict_ledger.json`.
- Verdicts: **1 analyzed**, **0 PICK**, **1 VETOED**; derived tiers: A=0, B=0, C=0, VETO=1.
No PICK rows in the ledger.

Options overlay: `runs/matrix_virt_2026-05-14/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_large_weekly_2026-05-15_2251_chain

- Date/universe: **2026-05-15**, **large**; source `runs/matrix_large_weekly_2026-05-15_2251_chain/verdict_ledger.json`.
- Verdicts: **24 analyzed**, **7 PICK**, **17 VETOED**; derived tiers: A=3, B=0, C=4, VETO=17.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AFL | C | $116.81 | $123.00 | — | — |
| EQIX | C | $1,059.44 | $1,250.00 | — | — |
| ET | A | $20.15 | $22.50 | $21.50 | 4.4% |
| HL | C | $17.64 | $21.30 | — | — |
| KMI | A | $33.63 | $36.00 | $34.75 | 3.5% |
| STLD | A | $229.34 | $250.00 | $240.00 | 4.0% |
| TT | C | $466.60 | $503.00 | — | — |

Options overlay: `runs/matrix_large_weekly_2026-05-15_2251_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **7** of **7** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AFL | C | long_call | 2026-09-18 | long call 115 @ $7.56 (delta 0.58) | $756.00 | $122.56 |
| EQIX | C | long_call | 2026-12-18 | long call 1020 @ $134.08 (delta 0.61) | $13,408.00 | $1,154.08 |
| ET | A | long_call | 2026-10-16 | long call 20 @ $1.55 (delta 0.52) | $155.00 | $21.55 |
| HL | C | long_call | 2026-12-18 | long call 20 @ $3.19 (delta 0.53) | $319.00 | $23.19 |
| KMI | A | long_call | 2026-09-18 | long call 33 @ $2.59 (delta 0.57) | $259.00 | $35.59 |
| STLD | A | long_call | 2026-09-18 | long call 220 @ $31.40 (delta 0.60) | $3,140.00 | $251.40 |
| TT | C | long_call | 2027-01-15 | long call 480 @ $49.66 (delta 0.52) | $4,966.00 | $529.66 |

### matrix_mega_weekly_2026-05-15_2236_chain

- Date/universe: **2026-05-15**, **mega**; source `runs/matrix_mega_weekly_2026-05-15_2236_chain/verdict_ledger.json`.
- Verdicts: **19 analyzed**, **8 PICK**, **11 VETOED**; derived tiers: A=1, B=1, C=6, VETO=11.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ADI | C | $417.49 | $450.00 | — | — |
| AVGO | C | $425.19 | $475.00 | — | — |
| GOOG | C | $393.32 | $500.00 | — | — |
| GOOGL | C | $396.78 | $450.00 | — | — |
| KO | A | $80.82 | $84.00 | $82.00 | 2.4% |
| LIN | C | $506.11 | $575.00 | — | — |
| LLY | C | $1,004.92 | $1,215.00 | — | — |
| PM | B | $189.61 | $210.00 | $195.00 | 7.1% |

Options overlay: `runs/matrix_mega_weekly_2026-05-15_2236_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **8** of **8** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ADI | C | long_call | 2026-09-18 | long call 420 @ $47.57 (delta 0.55) | $4,757.00 | $467.57 |
| AVGO | C | long_call | 2027-01-15 | long call 450 @ $66.46 (delta 0.54) | $6,646.00 | $516.46 |
| GOOG | C | long_call | 2027-01-15 | long call 405 @ $46.91 (delta 0.54) | $4,691.00 | $451.91 |
| GOOGL | C | long_call | 2027-01-15 | long call 405 @ $50.56 (delta 0.56) | $5,056.00 | $455.56 |
| KO | A | long_call | 2026-09-18 | long call 80 @ $4.91 (delta 0.56) | $491.00 | $84.91 |
| LIN | C | long_call | 2026-09-18 | long call 500 @ $35.77 (delta 0.57) | $3,577.00 | $535.77 |
| LLY | C | long_call | 2027-01-15 | long call 1030 @ $124.81 (delta 0.55) | $12,481.00 | $1,154.81 |
| PM | B | long_call | 2026-09-18 | long call 190 @ $14.52 (delta 0.53) | $1,452.00 | $204.52 |

### matrix_weekly_2026-05-15_1957_chain

- Date/universe: **2026-05-15**, **unspecified**; source `runs/matrix_weekly_2026-05-15_1957_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **4 VETOED**; derived tiers: A=0, B=0, C=1, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| DAR | C | $62.37 | $74.00 | — | — |

Options overlay: `runs/matrix_weekly_2026-05-15_1957_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| DAR | C | long_call | 2026-10-16 | long call 65 @ $5.99 (delta 0.51) | $599.00 | $70.99 |

### matrix_weekly_2026-05-15_extended

- Date/universe: **2026-05-15**, **unspecified**; source `runs/matrix_weekly_2026-05-15_extended/verdict_ledger.json`.
- Verdicts: **20 analyzed**, **5 PICK**, **14 VETOED**; derived tiers: A=1, B=1, C=3, VETO=14.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AROC | C | $37.43 | $45.00 | — | — |
| GVA | A | $138.55 | $150.00 | $150.00 | 0.0% |
| RSI | C | $26.77 | $35.00 | — | — |
| SSRM | C | $31.39 | $36.28 | — | — |
| YOU | B | $58.89 | $75.00 | $62.00 | 17.3% |

Options overlay: `runs/matrix_weekly_2026-05-15_extended/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **5** of **5** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AROC | C | long_call | 2026-11-20 | long call 35 @ $5.72 (delta 0.65) | $572.00 | $40.72 |
| GVA | A | long_call | 2026-12-18 | long call 140 @ $15.29 (delta 0.56) | $1,529.00 | $155.29 |
| RSI | C | long_call | 2026-10-16 | long call 30 @ $2.42 (delta 0.46) | $242.00 | $32.42 |
| SSRM | C | long_call | 2026-09-18 | long call 33 @ $4.35 (delta 0.55) | $435.00 | $37.35 |
| YOU | B | long_call | 2026-08-21 | long call 59.8 @ $6.80 (delta 0.57) | $680.00 | $66.60 |

### matrix_large_weekly_2026-05-22_1142_chain

- Date/universe: **2026-05-22**, **large**; source `runs/matrix_large_weekly_2026-05-22_1142_chain/verdict_ledger.json`.
- Verdicts: **10 analyzed**, **2 PICK**, **8 VETOED**; derived tiers: A=0, B=1, C=1, VETO=8.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| EBAY | B | $117.13 | $130.00 | $120.00 | 7.7% |
| TJX | C | $157.46 | $190.00 | — | — |

Options overlay: `runs/matrix_large_weekly_2026-05-22_1142_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **2** of **2** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| EBAY | B | long_call | 2026-10-16 | long call 120 @ $10.93 (delta 0.52) | $1,093.00 | $130.93 |
| TJX | C | long_call | 2027-03-19 | long call 160 @ $15.95 (delta 0.53) | $1,595.00 | $175.95 |

### matrix_mega_weekly_2026-05-22_1139_chain

- Date/universe: **2026-05-22**, **mega**; source `runs/matrix_mega_weekly_2026-05-22_1139_chain/verdict_ledger.json`.
- Verdicts: **10 analyzed**, **4 PICK**, **6 VETOED**; derived tiers: A=2, B=0, C=2, VETO=6.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| AMZN | C | $268.46 | $312.00 | — | — |
| C | C | $125.22 | $150.00 | — | — |
| GS | A | $988.17 | $1,050.00 | $1,050.00 | 0.0% |
| VZ | A | $48.27 | $52.50 | $51.70 | 1.5% |

Options overlay: `runs/matrix_mega_weekly_2026-05-22_1139_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **4** of **4** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| AMZN | C | long_call | 2027-03-19 | long call 280 @ $33.27 (delta 0.54) | $3,327.00 | $313.27 |
| C | C | long_call | 2027-03-19 | long call 125 @ $17.73 (delta 0.57) | $1,773.00 | $142.73 |
| GS | A | long_call | 2026-10-16 | long call 1000 @ $83.46 (delta 0.55) | $8,346.00 | $1,083.46 |
| VZ | A | long_call | 2026-10-16 | long call 48 @ $3.52 (delta 0.53) | $352.00 | $51.52 |

### matrix_mid_weekly_2026-05-22_1142_chain

- Date/universe: **2026-05-22**, **mid**; source `runs/matrix_mid_weekly_2026-05-22_1142_chain/verdict_ledger.json`.
- Verdicts: **10 analyzed**, **1 PICK**, **9 VETOED**; derived tiers: A=0, B=0, C=1, VETO=9.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| CRUS | C | $166.62 | $190.00 | — | — |

Options overlay: `runs/matrix_mid_weekly_2026-05-22_1142_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| CRUS | C | long_call | 2026-09-18 | long call 170 @ $16.56 (delta 0.59) | $1,656.00 | $186.56 |

### matrix_large_weekly_2026-05-30_1357_chain

- Date/universe: **2026-05-30**, **large**; source `runs/matrix_large_weekly_2026-05-30_1357_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **0 PICK**, **5 VETOED**; derived tiers: A=0, B=0, C=0, VETO=5.
No PICK rows in the ledger.

Options overlay: `runs/matrix_large_weekly_2026-05-30_1357_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_mega_weekly_2026-05-30_1335_chain

- Date/universe: **2026-05-30**, **mega**; source `runs/matrix_mega_weekly_2026-05-30_1335_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **4 VETOED**; derived tiers: A=0, B=0, C=1, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ADI | C | $413.85 | $460.00 | — | — |

Options overlay: `runs/matrix_mega_weekly_2026-05-30_1335_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ADI | C | long_call | 2026-09-18 | long call 420 @ $42.34 (delta 0.53) | $4,234.00 | $462.34 |

### matrix_weekly_2026-05-30_1247_chain

- Date/universe: **2026-05-30**, **unspecified**; source `runs/matrix_weekly_2026-05-30_1247_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **0 PICK**, **5 VETOED**; derived tiers: A=0, B=0, C=0, VETO=5.
No PICK rows in the ledger.

Options overlay: `runs/matrix_weekly_2026-05-30_1247_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_large_weekly_2026-06-06_2302_chain

- Date/universe: **2026-06-06**, **large**; source `runs/matrix_large_weekly_2026-06-06_2302_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **0 PICK**, **5 VETOED**; derived tiers: A=0, B=0, C=0, VETO=5.
No PICK rows in the ledger.

Options overlay: `runs/matrix_large_weekly_2026-06-06_2302_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_mega_weekly_2026-06-06_2323_chain

- Date/universe: **2026-06-06**, **mega**; source `runs/matrix_mega_weekly_2026-06-06_2323_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **4 VETOED**; derived tiers: A=1, B=0, C=0, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| C | A | $132.47 | $150.00 | $145.00 | 3.3% |

Options overlay: `runs/matrix_mega_weekly_2026-06-06_2323_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| C | A | long_call | 2027-03-19 | long call 135 @ $17.19 (delta 0.54) | $1,719.00 | $152.19 |

### matrix_mid_weekly_2026-06-06_2239_chain

- Date/universe: **2026-06-06**, **mid**; source `runs/matrix_mid_weekly_2026-06-06_2239_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **0 PICK**, **5 VETOED**; derived tiers: A=0, B=0, C=0, VETO=5.
No PICK rows in the ledger.

Options overlay: `runs/matrix_mid_weekly_2026-06-06_2239_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_large_weekly_2026-06-12_1051_chain

- Date/universe: **2026-06-12**, **large**; source `runs/matrix_large_weekly_2026-06-12_1051_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **0 PICK**, **5 VETOED**; derived tiers: A=0, B=0, C=0, VETO=5.
No PICK rows in the ledger.

Options overlay: `runs/matrix_large_weekly_2026-06-12_1051_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **0** of **0** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |

### matrix_mega_weekly_2026-06-12_1115_chain

- Date/universe: **2026-06-12**, **mega**; source `runs/matrix_mega_weekly_2026-06-12_1115_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **2 PICK**, **3 VETOED**; derived tiers: A=0, B=0, C=2, VETO=3.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ADI | C | $412.13 | $470.00 | — | — |
| GS | C | $1,035.64 | $1,200.00 | — | — |

Options overlay: `runs/matrix_mega_weekly_2026-06-12_1115_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **2** of **2** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ADI | C | long_call | 2026-09-18 | long call 420 @ $41.61 (delta 0.56) | $4,161.00 | $461.61 |
| GS | C | long_call | 2026-10-16 | long call 1080 @ $77.77 (delta 0.53) | $7,777.00 | $1,157.77 |

### matrix_mid_weekly_2026-06-12_1021_chain

- Date/universe: **2026-06-12**, **mid**; source `runs/matrix_mid_weekly_2026-06-12_1021_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **3 VETOED**; derived tiers: A=0, B=0, C=1, VETO=3.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| KLIC | C | $111.82 | $130.00 | — | — |

Options overlay: `runs/matrix_mid_weekly_2026-06-12_1021_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| KLIC | C | long_call | 2027-01-15 | long call 130 @ $17.39 (delta 0.51) | $1,739.00 | $147.39 |

### matrix_large_weekly_2026-06-26_2126_chain

- Date/universe: **2026-06-26**, **large**; source `runs/matrix_large_weekly_2026-06-26_2126_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **4 VETOED**; derived tiers: A=1, B=0, C=0, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| NUE | A | $239.78 | $274.00 | $274.00 | 0.0% |

Options overlay: `runs/matrix_large_weekly_2026-06-26_2126_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| NUE | A | long_call | 2026-12-18 | long call 240 @ $26.37 (delta 0.57) | $2,637.00 | $266.37 |

### matrix_mega_weekly_2026-06-26_2146_chain

- Date/universe: **2026-06-26**, **mega**; source `runs/matrix_mega_weekly_2026-06-26_2146_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **4 VETOED**; derived tiers: A=0, B=0, C=1, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| ADI | C | $386.91 | $470.00 | — | — |

Options overlay: `runs/matrix_mega_weekly_2026-06-26_2146_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| ADI | C | long_call | 2026-12-18 | long call 400 @ $55.36 (delta 0.54) | $5,536.00 | $455.36 |

### matrix_mid_weekly_2026-06-26_2103_chain

- Date/universe: **2026-06-26**, **mid**; source `runs/matrix_mid_weekly_2026-06-26_2103_chain/verdict_ledger.json`.
- Verdicts: **5 analyzed**, **1 PICK**, **4 VETOED**; derived tiers: A=0, B=0, C=1, VETO=4.
| Ticker | Tier | Current price | Aggressive PT | Conservative PT | PT compression |
| --- | --- | --- | --- | --- | --- |
| KLIC | C | $125.22 | $140.00 | — | — |

Options overlay: `runs/matrix_mid_weekly_2026-06-26_2103_chain/options_overlay.json`; mode **long-call**, delta target **0.55**, structures built **1** of **1** picks.
| Ticker | Tier | Strategy | Expiration | Legs | Debit/contract | Breakeven |
| --- | --- | --- | --- | --- | --- | --- |
| KLIC | C | long_call | 2027-01-15 | long call 130 @ $27.13 (delta 0.58) | $2,713.00 | $157.13 |

## SQLite catalog cross-run views

`runs/index.db` is a derived catalog. `.venv/bin/python scripts/index_runs.py --force --query` was run for this document; it reported 29 indexed screener runs, 40 indexed matrix runs, 675 screener rows, 373 matrix rows, 106 option legs, and 73 Chronos forecasts. The filesystem inventory found 32 screener directories (3 empty/malformed with no `screener.json`) and 37 matrix directories; the catalog still contains 3 matrix run ids with no matching directory: `matrix_large_weekly_2026-06-26_1830_chain`, `matrix_mega_weekly_2026-06-26_1836_chain`, `matrix_mid_weekly_2026-06-26_1823_chain`. Recurrence tables below are filtered to filesystem-backed matrix run ids. ROI/gain figures in `index.db` are **modeled gain-if-price-target-hits-at-expiry**, not realized P&L.

| Run type | Indexed count |
| --- | --- |
| matrix | 40 |
| screener | 29 |

### Most-recurring matrix tickers

| Ticker | Distinct matrix runs | Tiers observed | PICK rows | VETOED rows |
| --- | --- | --- | --- | --- |
| ADI | 9 | A,VETO,C | 5 | 4 |
| SNDK | 8 | VETO | 0 | 8 |
| VIRT | 7 | C,B,VETO | 5 | 2 |
| CRDO | 7 | VETO | 0 | 7 |
| MU | 7 | VETO | 0 | 7 |
| ROK | 7 | VETO | 0 | 7 |
| AMZN | 6 | C,VETO,B | 5 | 1 |
| KLIC | 6 | VETO,C | 2 | 4 |
| TXN | 6 | VETO,A | 1 | 5 |
| VNOM | 5 | VETO,A,C | 4 | 1 |
| GS | 5 | VETO,A,C | 3 | 2 |
| AVGO | 5 | VETO,C | 1 | 4 |
| NUE | 5 | VETO,A | 1 | 4 |
| MRVL | 5 | VETO | 0 | 5 |
| WDC | 5 | VETO | 0 | 5 |
| ARMK | 4 | B,C | 4 | 0 |
| AROC | 4 | A,B,C | 4 | 0 |
| FHB | 4 | B,A,C | 4 | 0 |
| GOOGL | 4 | C | 4 | 0 |
| CTRE | 4 | A,VETO | 2 | 2 |
| RSI | 4 | C,VETO | 2 | 1 |
| APH | 4 | C,VETO | 1 | 3 |
| KALU | 4 | C,VETO | 1 | 3 |
| SLAB | 4 | C,VETO | 1 | 3 |
| TRGP | 4 | A,VETO | 1 | 3 |

### Per-tier count from `ticker_history` (filesystem-backed matrix ids)

| Tier | Rows | PICK rows | VETOED rows |
| --- | --- | --- | --- |
| A | 25 | 25 | 0 |
| B | 14 | 14 | 0 |
| C | 67 | 67 | 0 |
| VETO | 249 | 0 | 249 |
| None | 3 | 0 | 0 |

## Realized backtest findings (measured 2026-06-26)

These are **realized** option/backtest measurements, distinct from older modeled ROI fields in `index.db`. Sources: `runs/backtest_2026-06-26.json`, `runs/backtest_exits_2026-06-26.json`, and visual packet `runs/exit_discipline_packet_2026-06-26.html`.

- Selection alone was approximately a coin flip: **n=92**, **50% win rate**, **+0.7% median option ROI** (`runs/backtest_2026-06-26.json`).
- Exit discipline was the edge: exit at aggressive PT produced **+13.8% median ROI** and **57% win rate** vs hold median **+0.7%** (`runs/backtest_exits_2026-06-26.json`).
| Tier | n | Hold median ROI | Exit @ aggr PT median | Exit @ cons PT median | Half/discipline median |
| --- | --- | --- | --- | --- | --- |
| A | 22 | -5.2% | 19.0% | 21.6% | 20.2% |
| B | 13 | 15.2% | 15.2% | 0.0% | 7.1% |
| C | 57 | 0.0% | 12.5% | — | 12.5% |

Interpretation: Tier A needed conservative-target selling (hold median −5.2% vs exit-at-cons +21.6%); Tier B was the group where letting winners run held up best (hold median +15.2%); Tier C benefited from trimming at aggressive PT (exit-at-aggr +12.5%).

## Empirical signal correlations (realized, as of 2026-06-26)

Generated by `scripts/backtest_signal_report.py` from
`runs/backtest_exits_2026-06-26.json` joined to `runs/index.db` on
`(run_id, ticker)`. Regenerate after every backtest cycle:

```bash
.venv/bin/python scripts/backtest_signal_report.py \
    --backtest runs/backtest_exits_<DATE>.json
```

Headline reads (details in the tables below):

- **Market-cap tier is the strongest pre-trade signal** (Spearman −0.30,
  monotonic): mid-cap picks median +3.5% hold / +23.0% exit / 59% win;
  mega-cap −35.6% hold / 42% win. The weekly workflow's mid-cap default is
  empirically supported; mega picks warrant a size haircut.
- **Tenor: 90–180 DTE decisively beat >180 DTE** (+21.2% vs +1.3% exit
  median; 40% vs 26% PT-hit).
- **Chronos `agent_pt_quantile` predicts PT-hit rates monotonically**
  (q<0.6 → 42% hit; q>0.8 → 24%) but is non-monotonic on ROI — the
  q0.6–0.8 band was the worst bucket (32% win, −35.7% hold median).
- **Recurrence is a caution flag**: tickers PICKed 3+ times ran
  −19.6% hold median / 43% win vs seen-once +0.9% / 50%.
- **Modeled upside carries no signal** (Pearson +0.04) and the **screener
  composite score does not rank outcomes** (Spearman −0.19 to −0.29,
  non-monotonic terciles) — it is a candidate filter, not an alpha rank.
- **Veto rate** climbed from ~55–60% (late Apr) to 83–93% (late May–June);
  the 2026-06-26 row (40%) is inflated by the 3 phantom catalog run-ids noted
  in the provenance section. Track as a gate-tightness regime metric.

### Signal correlations

Pearson / Spearman correlation of each pre-trade signal vs
realized ROI. Cells with n < 8 or zero variance are skipped.

| Signal | Outcome | n | Pearson | Spearman |
|---|---|---:|---:|---:|
| aggressive_upside_pct | roi_hold | 92 | +0.04 | -0.01 |
| aggressive_upside_pct | roi_exit_aggr | 92 | +0.02 | -0.03 |
| pt_compression_pct | roi_hold | 35 | +0.16 | +0.05 |
| pt_compression_pct | roi_exit_aggr | 35 | +0.05 | -0.09 |
| composite_score | roi_hold | 37 | -0.21 | -0.19 |
| composite_score | roi_exit_aggr | 37 | -0.21 | -0.29 |
| agent_pt_quantile | roi_hold | 64 | +0.05 | +0.02 |
| agent_pt_quantile | roi_exit_aggr | 64 | +0.08 | +0.03 |
| forecast_pct_p50 | roi_hold | 64 | +0.07 | +0.09 |
| forecast_pct_p50 | roi_exit_aggr | 64 | +0.00 | +0.04 |
| market_cap | roi_hold | 73 | -0.30 | -0.30 |
| market_cap | roi_exit_aggr | 73 | -0.30 | -0.23 |

### Market cap

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| mid (<10B) | 27 | +3.5% | +23.0% | 59% | 33% |
| large (10-200B) | 20 | +1.6% | +9.1% | 50% | 40% |
| mega (>200B) | 26 | -35.6% | -4.6% | 42% | 31% |

### Screener composite score terciles

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| composite_score tercile: low | 13 | +70.6% | +64.4% | 77% | 62% |
| composite_score tercile: mid | 12 | -2.3% | +9.8% | 42% | 25% |
| composite_score tercile: high | 12 | +7.2% | +21.4% | 58% | 50% |

### Chronos agent PT quantile

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| q<0.6 | 24 | +12.6% | +18.9% | 62% | 42% |
| q0.6-0.8 | 19 | -35.7% | -27.1% | 32% | 26% |
| q>0.8 | 21 | +15.2% | +17.6% | 57% | 24% |

### Chronos model view

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| above cone | 11 | +15.2% | +15.2% | 64% | 18% |
| high | 14 | -27.0% | +9.5% | 43% | 29% |
| inside | 39 | +1.3% | +12.5% | 51% | 36% |

### Sector (sector_sic)

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| SEMICONDUCTORS & RELATED DEVICES | 6 | -35.8% | -31.3% | 17% | 33% |

### Tenor (DTE at rec_date)

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| 90-180d | 58 | +5.3% | +21.2% | 52% | 40% |
| >180d | 34 | +0.0% | +1.3% | 47% | 26% |

### Recurrence (distinct PICK appearances)

| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |
|---|---:|---:|---:|---:|---:|
| seen once | 40 | +0.9% | +18.9% | 50% | 45% |
| seen 2x | 22 | +2.2% | +13.1% | 59% | 36% |
| seen 3+ | 30 | -19.6% | -18.5% | 43% | 20% |

### Veto rate by week

| Week | Picks | Vetoed | Total | Veto % |
|---|---:|---:|---:|---:|
| 2026-04-30 (post-close) | 10 | 15 | 25 | 60% |
| 2026-05-01 | 11 | 14 | 25 | 56% |
| 2026-05-05 | 2 | 7 | 9 | 78% |
| 2026-05-06 | 1 | 5 | 6 | 83% |
| 2026-05-08 | 37 | 42 | 79 | 53% |
| 2026-05-09 | 9 | 44 | 53 | 83% |
| 2026-05-14 | 1 | 2 | 3 | 67% |
| 2026-05-15 | 14 | 30 | 44 | 68% |
| 2026-05-16 | 7 | 17 | 24 | 71% |
| 2026-05-22 | 7 | 23 | 30 | 77% |
| 2026-05-30 | 1 | 14 | 15 | 93% |
| 2026-06-06 | 1 | 14 | 15 | 93% |
| 2026-06-12 | 4 | 11 | 15 | 73% |
| 2026-06-26 | 18 | 12 | 30 | 40% |
| 2026-06-30 | 0 | 1 | 1 | 100% |

### Caveats

- Single measurement date — all realized ROI figures are snapshotted as of the backtest's `measure_date`, not tracked to each option's actual expiration or a rolling exit.
- Small bucket sizes — several cross-tabs rest on n well under 30; treat splits as directional, not statistically confirmed.
- Options ROI medians are noisy — single-name option premiums swing on IV crush/expansion independent of the underlying thesis playing out.
- index.db signal columns (aggressive_upside_pct, pt_compression_pct, agent_pt_quantile, forecast_pct_p50, composite_score) are modeled estimates from the matrix/Chronos run, not realized outcomes.

## Latest week spotlight — 2026-06-26

Across the 2026-06-26 mid/large/mega weekly matrix runs, the ledgers show **3 picks / 12 vetoed** over **15 analyzed**. Sources: `runs/matrix_large_weekly_2026-06-26_2126_chain/verdict_ledger.json`, `runs/matrix_mega_weekly_2026-06-26_2146_chain/verdict_ledger.json`, `runs/matrix_mid_weekly_2026-06-26_2103_chain/verdict_ledger.json`.
| Universe | Run id | Pick | Tier | Current price | Aggressive PT | Conservative PT |
| --- | --- | --- | --- | --- | --- | --- |
| large | matrix_large_weekly_2026-06-26_2126_chain | NUE | A | $239.78 | $274.00 | $274.00 |
| mega | matrix_mega_weekly_2026-06-26_2146_chain | ADI | C | $386.91 | $470.00 | — |
| mid | matrix_mid_weekly_2026-06-26_2103_chain | KLIC | C | $125.22 | $140.00 | — |

### Rebuilt Δ0.55 long-call structures

| Universe | Ticker | Tier | Mode | Expiration | Legs | Debit/contract | Breakeven | Source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| large | NUE | A | long-call | 2026-12-18 | long call 240 @ $26.37 (delta 0.57) | $2,637.00 | $266.37 | `runs/matrix_large_weekly_2026-06-26_2126_chain/options_overlay.json` |
| mega | ADI | C | long-call | 2026-12-18 | long call 400 @ $55.36 (delta 0.54) | $5,536.00 | $455.36 | `runs/matrix_mega_weekly_2026-06-26_2146_chain/options_overlay.json` |
| mid | KLIC | C | long-call | 2027-01-15 | long call 130 @ $27.13 (delta 0.58) | $2,713.00 | $157.13 | `runs/matrix_mid_weekly_2026-06-26_2103_chain/options_overlay.json` |

## Cross-run HTML reports

| Report date | Directory | Path |
| --- | --- | --- |
| 2026-05-01 | cross_run_2026-05-01 | `runs/cross_run_2026-05-01/report.html` |
| 2026-05-05 | cross_run_2026-05-05 | `runs/cross_run_2026-05-05/report.html` |
| 2026-05-06 | cross_run_2026-05-06 | `runs/cross_run_2026-05-06/report.html` |
| 2026-05-08 | cross_run_2026-05-08 | `runs/cross_run_2026-05-08/report.html` |
| 2026-05-09 | cross_run_2026-05-09 | `runs/cross_run_2026-05-09/report.html` |
| 2026-05-14 | cross_run_2026-05-14 | `runs/cross_run_2026-05-14/report.html` |
| 2026-05-15 | cross_run_2026-05-15 | `runs/cross_run_2026-05-15/report.html` |
| 2026-05-16 | cross_run_2026-05-16 | `runs/cross_run_2026-05-16/report.html` |
| 2026-05-22 | cross_run_2026-05-22 | `runs/cross_run_2026-05-22/report.html` |
| 2026-05-30 | cross_run_2026-05-30 | `runs/cross_run_2026-05-30/report.html` |
| 2026-06-06 | cross_run_2026-06-06 | `runs/cross_run_2026-06-06/report.html` |
| 2026-06-12 | cross_run_2026-06-12 | `runs/cross_run_2026-06-12/report.html` |
| 2026-06-26 | cross_run_2026-06-26 | `runs/cross_run_2026-06-26/report.html` |

## Caveats and data provenance

- Authoritative run facts come from `runs/<id>/screener.json`, `runs/<id>/top_tickers.txt`, `runs/<id>/verdict_ledger.json`, `runs/<id>/current_prices.json`, and `runs/<id>/options_overlay.json`.
- Tier A/B/C is derived per row: non-PICK → VETO; PICK with no conservative PT → C; PICK with PT compression < 5.0% → A; otherwise B.
- `runs/index.db` is a derived catalog; it can be rebuilt and should not be treated as the authoritative artifact. Its ROI columns are modeled, not realized.
- The realized backtest and exit-discipline numbers are from the 2026-06-26 backtest JSON files. All runs relied on real Polygon/market-data artifacts captured under `runs/`.
