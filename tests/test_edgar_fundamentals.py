"""SEC EDGAR as the source of filing-derived fundamentals.

Polygon's /vX/reference/financials endpoint is retired (sunset 2026-10-09). The
agents' statements and overview, and the screener's quarterly series, are all
built from EDGAR company facts, selected by filing date. No network: facts are
small fixtures served in place of ``sec_edgar._fetch_json``.
"""

from __future__ import annotations

import copy
from unittest import mock

import pytest
import requests

from tradingagents import default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import sec_edgar
from tradingagents.screener import fundamentals as screener

pytestmark = pytest.mark.unit

TICKER_MAP = {"0": {"cik_str": 123, "ticker": "ACME", "title": "Acme Corp"},
              "1": {"cik_str": 456, "ticker": "NOFACTS", "title": "No Facts Inc"}}

# Calendar fiscal years. A 10-Q reports the quarter (and, from Q2, the year to
# date); the 10-K reports the full year and no fourth quarter.
_FILED = ("{y}-05-01", "{y}-08-01", "{y}-11-01", "{n}-02-15")
_ENDS = ("{y}-03-31", "{y}-06-30", "{y}-09-30", "{y}-12-31")


def _fact(start, end, val, filed, form):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def _year(y, q1, q2, q3, fy, *, quarters=True, cumulative=True):
    """Facts a calendar-year filer reports: Q1-Q3 quarters, 6M/9M to date, and the year."""
    f = [d.format(y=y, n=y + 1) for d in _FILED]
    e = [d.format(y=y) for d in _ENDS]
    facts = []
    if quarters:
        facts += [_fact(f"{y}-01-01", e[0], q1, f[0], "10-Q"),
                  _fact(f"{y}-04-01", e[1], q2, f[1], "10-Q"),
                  _fact(f"{y}-07-01", e[2], q3, f[2], "10-Q")]
    if cumulative:
        facts += [_fact(f"{y}-01-01", e[1], q1 + q2, f[1], "10-Q"),
                  _fact(f"{y}-01-01", e[2], q1 + q2 + q3, f[2], "10-Q")]
    facts.append(_fact(f"{y}-01-01", e[3], fy, f[3], "10-K"))
    return facts


# Revenue by quarter. Q4 is never reported alone: 2023 -> 140, 2024 -> 170, 2025 -> 200.
REVENUE = (_year(2023, 100, 110, 120, 470) + _year(2024, 120, 130, 140, 560)
           + _year(2025, 150, 160, 170, 680))
GROSS = _year(2024, 60, 65, 70, 280) + _year(2025, 75, 80, 85, 340)
OPERATING = _year(2024, 12, 13, 14, 56) + _year(2025, 15, 16, 17, 68)
NET_INCOME = (_year(2024, 10, 11, 12, 46) + _year(2025, 13, 14, 15, 58))
# Cash flow is filed year to date only: no standalone Q2, Q3 or Q4.
CASH_FLOW = (_year(2024, 30, 30, 30, 120, quarters=False) + _year(2025, 40, 45, 50, 200, quarters=False)
             + [_fact("2024-01-01", "2024-03-31", 30, "2024-05-01", "10-Q"),
                _fact("2025-01-01", "2025-03-31", 40, "2025-05-01", "10-Q")])
EPS = [_fact(s, e, v, f, form) for (s, e, v, f, form) in
       [("2024-01-01", "2024-03-31", 0.10, "2024-05-01", "10-Q"),
        ("2024-04-01", "2024-06-30", 0.11, "2024-08-01", "10-Q"),
        ("2024-07-01", "2024-09-30", 0.12, "2024-11-01", "10-Q"),
        ("2024-01-01", "2024-12-31", 0.46, "2025-02-15", "10-K"),
        ("2025-01-01", "2025-03-31", 0.13, "2025-05-01", "10-Q"),
        ("2025-04-01", "2025-06-30", 0.14, "2025-08-01", "10-Q"),
        ("2025-07-01", "2025-09-30", 0.15, "2025-11-01", "10-Q"),
        ("2025-01-01", "2025-12-31", 0.58, "2026-02-15", "10-K")]]
BALANCE = [{"end": "2025-09-30", "val": 900, "filed": "2025-11-01", "form": "10-Q"},
           {"end": "2025-12-31", "val": 1000, "filed": "2026-02-15", "form": "10-K"}]
CASH = [{"end": "2025-09-30", "val": 80, "filed": "2025-11-01", "form": "10-Q"},
        {"end": "2025-12-31", "val": 95, "filed": "2026-02-15", "form": "10-K"}]


def _facts(**tags):
    return {"cik": 123, "facts": {"us-gaap": {tag: {"units": {unit: rows}} for tag, (unit, rows) in tags.items()}}}


FACTS = _facts(
    RevenueFromContractWithCustomerExcludingAssessedTax=("USD", REVENUE),
    GrossProfit=("USD", GROSS),
    OperatingIncomeLoss=("USD", OPERATING),
    NetIncomeLoss=("USD", NET_INCOME),
    NetCashProvidedByUsedInOperatingActivities=("USD", CASH_FLOW),
    EarningsPerShareDiluted=("USD/shares", EPS),
    Assets=("USD", BALANCE),
    CashAndCashEquivalentsAtCarryingValue=("USD", CASH),
)


def _serve(monkeypatch, tmp_path, facts_by_cik=None, *, ticker_map=TICKER_MAP):
    facts_by_cik = {"0000000123": FACTS} if facts_by_cik is None else facts_by_cik
    monkeypatch.setattr(sec_edgar, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    calls = []

    def fetch(url):
        calls.append(url)
        if "company_tickers" in url:
            return ticker_map
        cik = url.rsplit("CIK", 1)[1].split(".")[0]
        if cik not in facts_by_cik:
            raise sec_edgar.EdgarNotFoundError("SEC EDGAR request failed (404)")
        return facts_by_cik[cik]

    monkeypatch.setattr(sec_edgar, "_fetch_json", fetch)
    return calls


@pytest.fixture
def edgar(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path)


# --- quarterly series ----------------------------------------------------------


def test_a_fourth_quarter_reported_only_in_the_annual_filing_is_derived(edgar):
    rows = sec_edgar.quarterly_income_series("ACME", "2026-03-01")
    by_end = {r["end"]: r for r in rows}
    assert len(rows) == 8
    assert [r["end"] for r in rows][0] == "2024-03-31" and rows[-1]["end"] == "2025-12-31"
    # FY2025 680 less Q1-Q3 (150+160+170) = 200; FY2024 560 - 390 = 170.
    assert by_end["2025-12-31"]["revenue"] == 200
    assert by_end["2024-12-31"]["revenue"] == 170
    # Derived from the annual filing: it carries that filing's date.
    assert by_end["2025-12-31"]["filed"] == "2026-02-15"
    # A quarter filed as such is used as filed, not re-derived.
    assert by_end["2025-06-30"]["revenue"] == 160 and by_end["2025-06-30"]["filed"] == "2025-08-01"
    # Gross profit and operating income get the same treatment.
    assert by_end["2025-12-31"]["gross_profit"] == 340 - (75 + 80 + 85)
    assert by_end["2025-12-31"]["operating_income"] == 68 - (15 + 16 + 17)


def test_the_series_is_oldest_first_and_capped(edgar):
    rows = sec_edgar.quarterly_income_series("ACME", "2026-03-01", max_quarters=5)
    assert [r["end"] for r in rows] == ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]


def test_facts_filed_after_the_as_of_date_are_excluded(edgar):
    # The FY2025 10-K (Q4 2025) is filed 2026-02-15.
    before = sec_edgar.quarterly_income_series("ACME", "2026-02-14")
    after = sec_edgar.quarterly_income_series("ACME", "2026-02-15")
    assert before[-1]["end"] == "2025-09-30"
    assert "2025-12-31" not in [r["end"] for r in before]
    assert after[-1]["end"] == "2025-12-31"


def test_a_restatement_counts_from_its_filing_date(monkeypatch, tmp_path):
    revenue = REVENUE + [_fact("2025-07-01", "2025-09-30", 175, "2026-03-10", "10-Q/A")]
    _serve(monkeypatch, tmp_path, {"0000000123": _facts(
        RevenueFromContractWithCustomerExcludingAssessedTax=("USD", revenue))})
    old = {r["end"]: r["revenue"] for r in sec_edgar.quarterly_income_series("ACME", "2026-03-09")}
    new = {r["end"]: r["revenue"] for r in sec_edgar.quarterly_income_series("ACME", "2026-03-10")}
    assert old["2025-09-30"] == 170 and new["2025-09-30"] == 175


def test_a_missing_quarter_ends_the_run_and_is_never_bridged(monkeypatch, tmp_path):
    """Q2 2025 is neither reported alone nor inside a six-month figure: the series
    starts after the gap rather than subtracting across it."""
    revenue = [f for f in REVENUE if f["end"] != "2025-06-30"]        # no Q2 and no 6M
    _serve(monkeypatch, tmp_path, {"0000000123": _facts(
        RevenueFromContractWithCustomerExcludingAssessedTax=("USD", revenue))})
    ends = [r["end"] for r in sec_edgar.quarterly_income_series("ACME", "2026-03-01")]
    # Q3 stays (reported alone) and Q4 is annual less nine months; Q1 sits across the gap.
    assert ends == ["2025-09-30", "2025-12-31"]

    # No Q2, no standalone Q3: the nine-month figure cannot be split without Q2,
    # so Q3 is unknown rather than guessed. Q4 (annual - nine months) still stands.
    revenue = [f for f in REVENUE if f["end"] not in ("2025-06-30",)
               and not (f["end"] == "2025-09-30" and f["start"] == "2025-07-01")]
    _serve(monkeypatch, tmp_path / "b", {"0000000123": _facts(
        RevenueFromContractWithCustomerExcludingAssessedTax=("USD", revenue))})
    assert [r["end"] for r in sec_edgar.quarterly_income_series("ACME", "2026-03-01")] == ["2025-12-31"]


def test_cash_flow_filed_only_year_to_date_yields_quarters(edgar):
    us_gaap = FACTS["facts"]["us-gaap"]
    series = sec_edgar._quarter_series(us_gaap, ("NetCashProvidedByUsedInOperatingActivities",), "2026-03-01")
    # 2025: 3M=40, 6M=85, 9M=135, FY=200.
    assert {e: v for e, (v, _) in series.items() if e.startswith("2025")} == {
        "2025-03-31": 40, "2025-06-30": 45, "2025-09-30": 50, "2025-12-31": 65}


def test_gross_profit_falls_back_to_revenue_less_cost(monkeypatch, tmp_path):
    cost = _year(2024, 40, 45, 50, 200) + _year(2025, 75, 80, 85, 300)
    facts = _facts(
        RevenueFromContractWithCustomerExcludingAssessedTax=("USD", REVENUE),
        CostOfRevenue=("USD", cost))
    _serve(monkeypatch, tmp_path, {"0000000123": facts})
    rows = {r["end"]: r for r in sec_edgar.quarterly_income_series("ACME", "2026-03-01")}
    assert rows["2025-03-31"]["gross_profit"] == 150 - 75
    assert rows["2025-03-31"]["operating_income"] is None


def test_the_revenue_tag_may_change_over_the_history(monkeypatch, tmp_path):
    old = _year(2023, 100, 110, 120, 470) + _year(2024, 120, 130, 140, 560)
    new = _year(2025, 150, 160, 170, 680)
    facts = _facts(SalesRevenueNet=("USD", old),
                   RevenueFromContractWithCustomerExcludingAssessedTax=("USD", new))
    _serve(monkeypatch, tmp_path, {"0000000123": facts})
    rows = sec_edgar.quarterly_income_series("ACME", "2026-03-01")
    assert len(rows) == 8 and rows[0]["revenue"] == 120 and rows[-1]["revenue"] == 200


def test_a_bank_reads_revenue_net_of_interest_expense(monkeypatch, tmp_path):
    """JPM, WFC, GS and MS tag their top line only as RevenuesNetOfInterestExpense;
    without it every large bank screened as insufficient_data."""
    facts = _facts(RevenuesNetOfInterestExpense=("USD", REVENUE))
    _serve(monkeypatch, tmp_path, {"0000000123": facts})
    rows = sec_edgar.quarterly_income_series("ACME", "2026-03-01")
    assert len(rows) == 8 and rows[-1]["revenue"] == 200


def test_net_revenue_outranks_a_contract_revenue_component(monkeypatch, tmp_path):
    """A filer tagging both (AXP) reports contract revenue as one component of
    its net revenue, so the net figure is the top line."""
    component = [dict(f, val=f["val"] // 2) for f in REVENUE]
    facts = _facts(RevenuesNetOfInterestExpense=("USD", REVENUE),
                   RevenueFromContractWithCustomerExcludingAssessedTax=("USD", component))
    _serve(monkeypatch, tmp_path, {"0000000123": facts})
    rows = sec_edgar.quarterly_income_series("ACME", "2026-03-01")
    assert rows[-1]["revenue"] == 200


def test_a_filer_that_stopped_reporting_has_no_coverage(edgar):
    assert sec_edgar.quarterly_income_series("ACME", "2028-01-01") == []


# --- coverage and failure contract --------------------------------------------


def test_a_ticker_edgar_does_not_list_has_no_coverage(edgar):
    assert sec_edgar.quarterly_income_series("TEVAF") == []


def test_a_filer_without_us_gaap_facts_has_no_coverage(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, {"0000000456": {"cik": 456, "facts": {"ifrs-full": {}}}})
    assert sec_edgar.quarterly_income_series("NOFACTS") == []


def test_a_404_for_the_companys_facts_is_no_coverage_not_an_outage(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, {})
    assert sec_edgar.quarterly_income_series("ACME") == []


@pytest.mark.parametrize("raised", [VendorUnavailableError("SEC EDGAR request failed (429)"),
                                    VendorUnavailableError("SEC EDGAR request failed (ConnectionError)")])
def test_http_failures_propagate_from_the_series(monkeypatch, tmp_path, raised):
    monkeypatch.setattr(sec_edgar, "get_config", lambda: {"data_cache_dir": str(tmp_path)})

    def fetch(url):
        if "company_tickers" in url:
            return TICKER_MAP
        raise raised

    monkeypatch.setattr(sec_edgar, "_fetch_json", fetch)
    with pytest.raises(VendorUnavailableError):
        sec_edgar.quarterly_income_series("ACME")


def test_a_failed_ticker_map_request_propagates(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_edgar, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(sec_edgar, "_fetch_json", mock.Mock(side_effect=VendorUnavailableError("503")))
    with pytest.raises(VendorUnavailableError):
        sec_edgar.quarterly_income_series("ACME")


def test_an_http_404_is_distinguishable_from_other_failures(monkeypatch):
    monkeypatch.setattr(sec_edgar, "_pace", lambda: None)
    for status, expected in ((404, sec_edgar.EdgarNotFoundError), (429, VendorUnavailableError),
                             (500, VendorUnavailableError)):
        response = requests.Response()
        response.status_code = status
        with mock.patch.object(sec_edgar.requests, "get", return_value=response), \
                pytest.raises(expected) as caught:
            sec_edgar._fetch_json("https://data.sec.gov/x")
        assert type(caught.value) is expected


def test_the_screener_does_not_write_company_facts_to_the_cache(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path)
    sec_edgar.quarterly_income_series("ACME", "2026-03-01", persist=False)
    assert not (tmp_path / "sec_edgar" / "CIK0000000123.json").exists()
    sec_edgar.quarterly_income_series("ACME", "2026-03-01")
    assert (tmp_path / "sec_edgar" / "CIK0000000123.json").exists()


# --- screener signals ----------------------------------------------------------


def test_screener_signals_from_edgar_quarters(edgar):
    sig = screener.compute_fundamental_signals("ACME", use_cache=False)
    assert sig.quarters_available == 8
    assert sig.revenue_quarterly[-4:] == [150, 160, 170, 200]
    # Q1'25 150 vs Q1'24 120; Q4'25 200 vs Q4'24 170
    assert sig.revenue_yoy[0] == pytest.approx(0.25)
    assert sig.revenue_yoy[-1] == pytest.approx(200 / 170 - 1)
    assert sig.revenue_growth_strong and "rev_growth_strong" in sig.flags
    assert sig.gross_margin_latest == pytest.approx((340 - 240) / 200)
    assert "insufficient_data" not in sig.flags


def test_screener_without_coverage_is_insufficient_data(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path)
    sig = screener.compute_fundamental_signals("TEVAF", use_cache=False)
    assert "insufficient_data" in sig.flags and sig.fundamental_score == 0.0


def test_screener_propagates_an_edgar_outage(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_edgar, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(sec_edgar, "_fetch_json", mock.Mock(side_effect=VendorUnavailableError("429")))
    with pytest.raises(VendorUnavailableError):
        screener.compute_fundamental_signals("ACME", use_cache=False)


def test_orchestrator_marks_an_edgar_outage_partial_not_insufficient(monkeypatch):
    from datetime import date

    from tradingagents.screener import orchestrator
    from tradingagents.screener.technicals import TechnicalSignals
    from tradingagents.screener.universe import UniverseEntry

    entry = UniverseEntry(ticker="ACME", market_cap=5e9)
    monkeypatch.setattr(orchestrator, "build_universe", lambda **k: (date(2026, 3, 1), [entry]))
    monkeypatch.setattr(orchestrator, "compute_technical_signals",
                        lambda *a, **k: TechnicalSignals(ticker="ACME", technical_score=50.0))
    monkeypatch.setattr(orchestrator, "compute_fundamental_signals",
                        mock.Mock(side_effect=VendorUnavailableError("SEC EDGAR request failed (429)")))
    result = orchestrator.run_screener(top_n=5)
    assert result.is_partial and result.rate_limited_failures == ["ACME:fundamentals"]
    assert "rate_limited" in result.candidates[0].fundamental.flags


# --- overview snapshot ----------------------------------------------------------


def test_snapshot_ttm_figures_and_latest_balance(edgar):
    snap = sec_edgar.ttm_snapshot("ACME", "2026-03-01")
    assert snap["revenue"] == 150 + 160 + 170 + 200
    assert snap["gross_profit"] == 340
    assert snap["operating_income"] == 68
    assert snap["net_income"] == 58
    assert snap["eps"] == pytest.approx(0.13 + 0.14 + 0.15 + (0.58 - 0.42))
    assert snap["operating_cash_flow"] == 200
    assert snap["total_assets"] == 1000 and snap["cash"] == 95
    assert snap["latest_period_end"] == "2025-12-31" and snap["latest_filed"] == "2026-02-15"


def test_snapshot_is_point_in_time(edgar):
    """Before the FY2025 10-K was filed, TTM ends at Q3 2025 and the balance is the 10-Q's."""
    snap = sec_edgar.ttm_snapshot("ACME", "2026-02-01")
    assert snap["revenue"] == 170 + 150 + 160 + 170        # Q4'24 .. Q3'25
    assert snap["net_income"] == (46 - 33) + 13 + 14 + 15
    assert snap["total_assets"] == 900 and snap["cash"] == 80
    assert snap["latest_period_end"] == "2025-09-30" and snap["latest_filed"] == "2025-11-01"
    # A fact filed after the as-of date must not leak in (the 2025 10-K total).
    assert 1000 not in snap.values() and 200 not in snap.values()


def test_snapshot_omits_what_cannot_be_derived(monkeypatch, tmp_path):
    facts = _facts(RevenueFromContractWithCustomerExcludingAssessedTax=("USD", REVENUE[:5]))  # only 2023 Q1-Q3 + 6M/9M
    _serve(monkeypatch, tmp_path, {"0000000123": facts})
    snap = sec_edgar.ttm_snapshot("ACME", "2026-03-01")
    # Three quarters of 2023, no annual filing: strict TTM needs four consecutive quarters.
    assert "revenue" not in snap
    assert not {"gross_profit", "operating_income", "net_income", "eps", "cash", "total_assets"} & set(snap)


def test_snapshot_without_coverage_raises_no_data(edgar):
    with pytest.raises(NoMarketDataError):
        sec_edgar.ttm_snapshot("TEVAF", "2026-03-01")


# --- statement routing ---------------------------------------------------------


@pytest.mark.parametrize("method", ["get_balance_sheet", "get_cashflow", "get_income_statement"])
def test_statements_come_from_sec_edgar_first_then_yfinance(method):
    set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))
    served = []

    def edgar_down(*a, **k):
        served.append("sec_edgar")
        raise NoMarketDataError("TEVA", "TEVA", "not a US SEC filer")

    chain = {"sec_edgar": edgar_down,
             "yfinance": lambda *a, **k: served.append("yfinance") or "yahoo statement"}
    with mock.patch.dict(router.VENDOR_METHODS, {method: chain}):
        assert router.route_to_vendor(method, "TEVA", "quarterly", "2025-01-01") == "yahoo statement"
    assert served == ["sec_edgar", "yfinance"]


@pytest.mark.parametrize("method", ["get_balance_sheet", "get_cashflow", "get_income_statement"])
def test_statements_no_longer_register_polygon(method):
    assert "polygon" not in router.VENDOR_METHODS[method]
    assert "sec_edgar" in router.VENDOR_METHODS[method]


def test_default_vendor_chains():
    config = default_config.DEFAULT_CONFIG
    assert config["data_vendors"]["fundamental_data"] == "sec_edgar,yfinance"
    # The overview is Polygon reference data + EDGAR facts, ending in yfinance.
    assert config["tool_vendors"]["get_fundamentals"] == "polygon,yfinance"
    set_config(copy.deepcopy(config))
    assert router.get_vendor("fundamental_data", "get_income_statement") == "sec_edgar,yfinance"
    assert router.get_vendor("fundamental_data", "get_fundamentals") == "polygon,yfinance"


# --- SEC etiquette --------------------------------------------------------------


def test_requests_are_spaced_under_the_ten_per_second_limit(monkeypatch):
    clock = [1000.0]
    slept = []
    monkeypatch.setattr(sec_edgar, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(sec_edgar, "_sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(sec_edgar, "_next_slot", 0.0)

    stamps = []
    for _ in range(5):
        sec_edgar._pace()
        stamps.append(clock[0])
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert gaps and all(g >= sec_edgar._MIN_INTERVAL - 1e-9 for g in gaps)
    assert 1 / sec_edgar._MIN_INTERVAL < 10          # strictly under SEC's limit
    assert len(slept) == 4                           # the first request does not wait


def test_a_request_after_a_quiet_spell_does_not_wait(monkeypatch):
    clock = [50.0]
    slept = []
    monkeypatch.setattr(sec_edgar, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(sec_edgar, "_sleep", slept.append)
    monkeypatch.setattr(sec_edgar, "_next_slot", 0.0)
    sec_edgar._pace()
    clock[0] += 5
    sec_edgar._pace()
    assert slept == []


def test_threads_share_one_pacer(monkeypatch):
    import threading

    slots = []
    lock = threading.Lock()
    clock = [0.0]
    monkeypatch.setattr(sec_edgar, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(sec_edgar, "_next_slot", 0.0)

    def sleeper(seconds):
        with lock:
            slots.append(clock[0] + seconds)

    monkeypatch.setattr(sec_edgar, "_sleep", sleeper)
    threads = [threading.Thread(target=sec_edgar._pace) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    slots.sort()
    assert len(slots) == 7
    assert all(b - a >= sec_edgar._MIN_INTERVAL - 1e-9 for a, b in zip(slots, slots[1:], strict=False))


def test_every_fetch_is_paced_and_identifies_the_caller(monkeypatch):
    paced = []
    monkeypatch.setattr(sec_edgar, "_pace", lambda: paced.append(1))
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    response = mock.Mock()
    response.json.return_value = {"ok": True}
    with mock.patch.object(sec_edgar.requests, "get", return_value=response) as get:
        assert sec_edgar._fetch_json("https://data.sec.gov/x") == {"ok": True}
    assert paced == [1]
    assert get.call_args.kwargs["headers"]["User-Agent"].startswith("TradingAgents/")


def test_statement_revenue_reads_the_including_assessed_tax_tag(monkeypatch, tmp_path):
    """Fastly tags revenue RevenueFromContractWithCustomerIncludingAssessedTax; the
    statement printed "unavailable (not tagged by this filer)" for it."""
    facts = _facts(RevenueFromContractWithCustomerIncludingAssessedTax=("USD", REVENUE))
    _serve(monkeypatch, tmp_path, {"0000000123": facts})
    out = sec_edgar.get_income_statement("ACME", "quarterly", "2026-03-01")
    revenue_row = next(line for line in out.splitlines() if line.startswith("Revenue,"))
    assert "unavailable" not in revenue_row           # values print in $M: 150 -> "0"
