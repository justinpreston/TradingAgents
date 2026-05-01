"""Tests for the Polygon REST pacer + Retry-After + retry logic.

No live network calls — everything is mocked at the ``requests.Session.get``
level so we can exercise the 429/Retry-After/backoff paths deterministically.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from unittest.mock import patch, MagicMock

import pytest

from tradingagents.dataflows import polygon_common as pc
from tradingagents.dataflows.polygon_common import (
    PolygonRateLimitError,
    _parse_retry_after,
    _wait_for_slot,
    min_request_interval,
    set_min_request_interval,
)


@pytest.fixture(autouse=True)
def _reset_pacer():
    """Force pacer state back to defaults around each test."""
    pc._min_interval_s = 0.0
    pc._last_request_time = 0.0
    yield
    pc._min_interval_s = 0.0
    pc._last_request_time = 0.0


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")


def _make_response(status: int, *, headers=None, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text
    if json_body is not None:
        r.json.return_value = json_body
    else:
        r.json.side_effect = ValueError("no body")
    return r


# ---------------------------------------------------------------------------
# _parse_retry_after
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_none_or_empty(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("   ") is None

    def test_integer_seconds(self):
        assert _parse_retry_after("30") == 30.0
        assert _parse_retry_after("0") == 0.0

    def test_float_seconds(self):
        assert _parse_retry_after("12.5") == 12.5

    def test_negative_clamped_to_zero(self):
        assert _parse_retry_after("-5") == 0.0

    def test_http_date_in_future(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=45)
        header = format_datetime(future, usegmt=True)
        parsed = _parse_retry_after(header)
        assert parsed is not None
        assert 35.0 < parsed <= 50.0  # rough — wall-clock drift tolerance

    def test_http_date_in_past_clamped(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        header = format_datetime(past, usegmt=True)
        assert _parse_retry_after(header) == 0.0

    def test_garbage_returns_none(self):
        assert _parse_retry_after("not a date or number") is None


# ---------------------------------------------------------------------------
# _wait_for_slot pacer
# ---------------------------------------------------------------------------


class TestWaitForSlot:
    def test_no_pacing_returns_immediately(self):
        set_min_request_interval(0.0)
        t0 = time.monotonic()
        _wait_for_slot()
        _wait_for_slot()
        assert time.monotonic() - t0 < 0.05

    def test_pacing_enforces_interval(self):
        set_min_request_interval(0.05)
        t0 = time.monotonic()
        _wait_for_slot()
        _wait_for_slot()
        _wait_for_slot()
        # First call passes through (pacer state was just initialised), then
        # two paced waits → at least 2*0.05 = 0.10s elapsed total.
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.09, f"expected >=0.09s, got {elapsed:.3f}s"

    def test_context_manager_restores_prior_value(self):
        set_min_request_interval(0.2)
        with min_request_interval(0.5):
            assert pc._min_interval_s == 0.5
        assert pc._min_interval_s == 0.2

    def test_adaptive_mode_ratchets_on_rate_limit(self):
        # Adaptive on, base 0.1s. Each 429 should multiply interval by 1.5x
        # up to the cap.
        with min_request_interval(0.1, adaptive=True):
            assert pc._min_interval_s == 0.1
            new1 = pc._adapt_on_rate_limit()
            assert new1 is not None
            assert pc._min_interval_s == pytest.approx(0.15)
            new2 = pc._adapt_on_rate_limit()
            assert new2 is not None
            assert pc._min_interval_s == pytest.approx(0.225)
        # On exit, restored to whatever was before (0 by fixture).
        assert pc._min_interval_s == 0.0

    def test_adaptive_mode_off_does_not_ratchet(self):
        with min_request_interval(0.1, adaptive=False):
            assert pc._adapt_on_rate_limit() is None
            assert pc._min_interval_s == 0.1

    def test_adaptive_caps_at_max(self):
        with min_request_interval(1.5, adaptive=True):
            for _ in range(20):
                pc._adapt_on_rate_limit()
            assert pc._min_interval_s == pytest.approx(pc._PACER_MAX_INTERVAL_S)


# ---------------------------------------------------------------------------
# _make_request retry + Retry-After integration
# ---------------------------------------------------------------------------


class TestMakeRequestRetry:
    def test_429_retries_then_succeeds(self):
        responses = [
            _make_response(429, headers={"Retry-After": "0"}, text="rate limited"),
            _make_response(429, headers={"Retry-After": "0"}, text="rate limited"),
            _make_response(200, json_body={"results": [{"x": 1}]}),
        ]
        session = MagicMock()
        session.get.side_effect = responses
        with patch.object(pc, "_get_session", return_value=session):
            payload = pc._make_request("/v3/x", max_attempts=4)
        assert payload == {"results": [{"x": 1}]}
        assert session.get.call_count == 3

    def test_429_exhausted_raises_rate_limit_error(self):
        session = MagicMock()
        session.get.return_value = _make_response(
            429, headers={"Retry-After": "0"}, text="still rate limited"
        )
        with patch.object(pc, "_get_session", return_value=session):
            with pytest.raises(PolygonRateLimitError):
                pc._make_request("/v3/x", max_attempts=3)
        assert session.get.call_count == 3

    def test_retry_after_seconds_is_honored(self):
        # 0.05s Retry-After should be slept (not the default backoff schedule
        # which would start at 1s).
        session = MagicMock()
        session.get.side_effect = [
            _make_response(429, headers={"Retry-After": "0.05"}),
            _make_response(200, json_body={"ok": True}),
        ]
        with patch.object(pc, "_get_session", return_value=session):
            t0 = time.monotonic()
            payload = pc._make_request("/v3/x", max_attempts=3)
            elapsed = time.monotonic() - t0
        assert payload == {"ok": True}
        # Should have slept ~0.05s + jitter (<1s), well under the 1s default
        # backoff that would have applied without Retry-After parsing.
        assert elapsed < 0.9, f"expected <0.9s, got {elapsed:.3f}s"

    def test_retry_after_caps_at_120s(self):
        # An absurd Retry-After value gets capped — verify by patching sleep
        # and checking the requested duration.
        session = MagicMock()
        session.get.side_effect = [
            _make_response(429, headers={"Retry-After": "9999"}),
            _make_response(200, json_body={"ok": True}),
        ]
        with patch.object(pc, "_get_session", return_value=session):
            with patch.object(pc.time, "sleep") as mock_sleep:
                pc._make_request("/v3/x", max_attempts=3)
        # Find the sleep call after the 429 — should be capped at 120 (+ jitter)
        sleep_durations = [c.args[0] for c in mock_sleep.call_args_list]
        assert any(120.0 <= d <= 121.5 for d in sleep_durations), (
            f"expected a ~120s capped sleep, got {sleep_durations}"
        )

    def test_404_is_not_retried(self):
        from tradingagents.dataflows.polygon_common import PolygonNotFoundError
        session = MagicMock()
        session.get.return_value = _make_response(404, text="not found")
        with patch.object(pc, "_get_session", return_value=session):
            with pytest.raises(PolygonNotFoundError):
                pc._make_request("/v3/x")
        assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# Screener-side propagation of rate-limit errors (no more silent corruption)
# ---------------------------------------------------------------------------


class TestRateLimitPropagation:
    def test_universe_enrich_propagates_rate_limit(self):
        from tradingagents.screener.universe import _enrich_with_reference

        with patch(
            "tradingagents.screener.universe._make_request",
            side_effect=PolygonRateLimitError("test"),
        ):
            with pytest.raises(PolygonRateLimitError):
                _enrich_with_reference("AAPL")

    def test_universe_enrich_swallows_404(self):
        from tradingagents.screener.universe import _enrich_with_reference
        from tradingagents.dataflows.polygon_common import PolygonNotFoundError

        with patch(
            "tradingagents.screener.universe._make_request",
            side_effect=PolygonNotFoundError("test"),
        ):
            assert _enrich_with_reference("DELISTED") == {}

    def test_fundamentals_propagates_rate_limit(self):
        from tradingagents.screener.fundamentals import fetch_quarterly_financials

        with patch(
            "tradingagents.screener.fundamentals.paginated_results",
            side_effect=PolygonRateLimitError("test"),
        ):
            with pytest.raises(PolygonRateLimitError):
                fetch_quarterly_financials("AAPL")

    def test_fundamentals_swallows_404(self):
        from tradingagents.screener.fundamentals import fetch_quarterly_financials
        from tradingagents.dataflows.polygon_common import PolygonNotFoundError

        with patch(
            "tradingagents.screener.fundamentals.paginated_results",
            side_effect=PolygonNotFoundError("test"),
        ):
            assert fetch_quarterly_financials("ADRX") == []


# ---------------------------------------------------------------------------
# ScreenerResult partial flag
# ---------------------------------------------------------------------------


class TestScreenerResultPartial:
    def test_is_partial_false_when_no_failures(self):
        from datetime import date as _date
        from tradingagents.screener.orchestrator import ScreenerResult
        r = ScreenerResult(
            trading_date=_date(2026, 4, 29),
            universe_size=10,
            candidates=[],
            top_n=25,
            config={},
        )
        assert r.is_partial is False

    def test_is_partial_true_when_failures_present(self):
        from datetime import date as _date
        from tradingagents.screener.orchestrator import ScreenerResult
        r = ScreenerResult(
            trading_date=_date(2026, 4, 29),
            universe_size=10,
            candidates=[],
            top_n=25,
            config={},
            rate_limited_failures=["AAPL:technicals"],
        )
        assert r.is_partial is True
