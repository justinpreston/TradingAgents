"""Tests for news_enrichment_loader and the screener re-rank helper.

These exercise the two opt-in surfaces that were wired this session:

1. ``tradingagents.dataflows.news_enrichment_loader.build_enrichment_prefix``
   reads the ``TRADINGAGENTS_NEWS_ENRICHMENT_PATH`` env var, looks up a
   ticker in the JSON, and returns a system-message prefix for the
   news_analyst (or ``""`` when no enrichment is wired).

2. ``scripts.build_news_enrichment._rerank_screener`` produces
   ``screener_sentiment_reranked.json`` and
   ``top_tickers_sentiment_reranked.txt`` next to the original screener
   files without modifying the originals.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tradingagents.dataflows import news_enrichment_loader as loader


@pytest.fixture(autouse=True)
def _reset_loader_cache():
    loader.reset_cache()
    yield
    loader.reset_cache()


def _write_enrichment(tmp_path: Path, ticker: str = "AVGO",
                      polarity: float = 0.18, n: int = 64,
                      themes: list[dict] | None = None,
                      triggers: list[str] | None = None,
                      lookback_days: int = 30) -> Path:
    payload = {
        "schema_version": 1,
        "scorer_choice": "finbert",
        "lookback_days": lookback_days,
        "tickers": [
            {
                "ticker": ticker,
                "lookback_days": lookback_days,
                "sentiment": {
                    "finbert": {
                        "scorer": "finbert",
                        "n_headlines": n,
                        "aggregate": polarity,
                        "trigger_terms": triggers or [],
                    }
                },
                "themes": themes or [],
            }
        ],
    }
    p = tmp_path / "news_enrichment.json"
    p.write_text(json.dumps(payload))
    return p


def test_build_enrichment_prefix_returns_empty_without_env_var(monkeypatch):
    monkeypatch.delenv(loader.ENRICHMENT_ENV_VAR, raising=False)
    assert loader.build_enrichment_prefix("AAPL") == ""


def test_build_enrichment_prefix_returns_empty_when_path_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(tmp_path / "missing.json"))
    assert loader.build_enrichment_prefix("AAPL") == ""


def test_build_enrichment_prefix_returns_empty_for_unknown_ticker(
    monkeypatch, tmp_path
):
    p = _write_enrichment(tmp_path, ticker="AVGO")
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(p))
    assert loader.build_enrichment_prefix("NVDA") == ""


def test_build_enrichment_prefix_includes_polarity_and_themes(
    monkeypatch, tmp_path
):
    p = _write_enrichment(
        tmp_path, ticker="AVGO", polarity=0.12, n=68,
        themes=[
            {"label": "earnings", "confidence": 0.45},
            {"label": "product_launch", "confidence": 0.20},
            {"label": "analyst_action", "confidence": 0.10},
        ],
        triggers=["raised guidance", "beat"],
        lookback_days=30,
    )
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(p))
    prefix = loader.build_enrichment_prefix("avgo")
    assert "AVGO" in prefix
    assert "+0.12" in prefix
    assert "n=68" in prefix
    assert "earnings" in prefix
    assert "product_launch" in prefix
    assert "analyst_action" in prefix
    assert "raised guidance" in prefix
    assert "verify with your own tool" in prefix


def test_build_enrichment_prefix_handles_zero_headlines(
    monkeypatch, tmp_path
):
    p = _write_enrichment(tmp_path, ticker="AVGO", polarity=0.0, n=0)
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(p))
    assert loader.build_enrichment_prefix("AVGO") == ""


def test_build_enrichment_prefix_falls_back_to_keyword(monkeypatch, tmp_path):
    payload = {
        "tickers": [
            {
                "ticker": "AVGO",
                "lookback_days": 30,
                "sentiment": {
                    "keyword": {
                        "scorer": "keyword",
                        "n_headlines": 5,
                        "aggregate": -0.25,
                        "trigger_terms": ["downgrade"],
                    }
                },
                "themes": [],
            }
        ]
    }
    p = tmp_path / "news_enrichment.json"
    p.write_text(json.dumps(payload))
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(p))
    prefix = loader.build_enrichment_prefix("AVGO")
    assert "keyword sentiment -0.25" in prefix


def test_build_enrichment_prefix_handles_malformed_json(
    monkeypatch, tmp_path
):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(bad))
    assert loader.build_enrichment_prefix("AVGO") == ""


def test_get_enrichment_for_returns_none_for_unknown(monkeypatch, tmp_path):
    p = _write_enrichment(tmp_path, ticker="AVGO")
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(p))
    assert loader.get_enrichment_for("MSFT") is None


def test_get_enrichment_for_is_case_insensitive(monkeypatch, tmp_path):
    p = _write_enrichment(tmp_path, ticker="AVGO")
    monkeypatch.setenv(loader.ENRICHMENT_ENV_VAR, str(p))
    row = loader.get_enrichment_for("avgo")
    assert row is not None
    assert row["ticker"] == "AVGO"


# ---------------------------------------------------------------------------
# screener re-rank
# ---------------------------------------------------------------------------


@pytest.fixture
def rerank_module():
    return importlib.import_module("scripts.build_news_enrichment")


def _make_screener_dir(tmp_path: Path,
                       candidates: list[dict]) -> Path:
    run_dir = tmp_path / "screener_test"
    run_dir.mkdir()
    (run_dir / "screener.json").write_text(json.dumps({
        "schema_version": 1,
        "candidates": candidates,
    }))
    (run_dir / "top_tickers.txt").write_text(
        "\n".join(c["ticker"] for c in candidates) + "\n"
    )
    return run_dir


def test_rerank_screener_does_not_modify_originals(rerank_module, tmp_path):
    cands = [
        {"rank": 1, "ticker": "AAA", "composite_score": 50.0},
        {"rank": 2, "ticker": "BBB", "composite_score": 40.0},
    ]
    run_dir = _make_screener_dir(tmp_path, cands)
    enriched = [
        {"ticker": "AAA", "sentiment": {"finbert": {"n_headlines": 10, "aggregate": -0.5}}},
        {"ticker": "BBB", "sentiment": {"finbert": {"n_headlines": 10, "aggregate": +0.8}}},
    ]
    out = rerank_module._rerank_screener(run_dir, enriched, alpha=0.20)
    assert out is not None
    original = json.loads((run_dir / "screener.json").read_text())
    assert original["candidates"] == cands  # unchanged
    assert (run_dir / "top_tickers.txt").read_text() == "AAA\nBBB\n"


def test_rerank_screener_promotes_positive_sentiment(rerank_module, tmp_path):
    cands = [
        {"rank": 1, "ticker": "AAA", "composite_score": 50.0},
        {"rank": 2, "ticker": "BBB", "composite_score": 40.0},
    ]
    run_dir = _make_screener_dir(tmp_path, cands)
    enriched = [
        {"ticker": "AAA", "sentiment": {"finbert": {"n_headlines": 10, "aggregate": -0.5}}},
        {"ticker": "BBB", "sentiment": {"finbert": {"n_headlines": 10, "aggregate": +0.8}}},
    ]
    rerank_module._rerank_screener(run_dir, enriched, alpha=0.50)
    rerank = json.loads((run_dir / "screener_sentiment_reranked.json").read_text())
    tickers_in_order = [c["ticker"] for c in rerank["candidates"]]
    # AAA: 50 * (1 - 0.25) = 37.5
    # BBB: 40 * (1 + 0.40) = 56.0  → BBB now ranks above AAA
    assert tickers_in_order == ["BBB", "AAA"]
    txt = (run_dir / "top_tickers_sentiment_reranked.txt").read_text()
    assert txt.strip().splitlines() == ["BBB", "AAA"]


def test_rerank_screener_clamps_multiplier(rerank_module, tmp_path):
    cands = [{"rank": 1, "ticker": "ZZZ", "composite_score": 100.0}]
    run_dir = _make_screener_dir(tmp_path, cands)
    enriched = [
        {"ticker": "ZZZ", "sentiment": {"finbert": {"n_headlines": 10, "aggregate": +1.0}}},
    ]
    # With alpha=10 the raw multiplier would be 11, but it must be clamped to 1.5.
    rerank_module._rerank_screener(run_dir, enriched, alpha=10.0)
    rerank = json.loads((run_dir / "screener_sentiment_reranked.json").read_text())
    row = rerank["candidates"][0]
    assert row["sentiment_multiplier"] == 1.5
    assert row["composite_score"] == 150.0


def test_rerank_screener_zero_polarity_for_unknown_ticker(rerank_module, tmp_path):
    cands = [
        {"rank": 1, "ticker": "AAA", "composite_score": 50.0},
        {"rank": 2, "ticker": "BBB", "composite_score": 40.0},
    ]
    run_dir = _make_screener_dir(tmp_path, cands)
    enriched = [
        {"ticker": "AAA", "sentiment": {"finbert": {"n_headlines": 10, "aggregate": +0.5}}},
        # BBB intentionally absent from enrichment
    ]
    rerank_module._rerank_screener(run_dir, enriched, alpha=0.20)
    rerank = json.loads((run_dir / "screener_sentiment_reranked.json").read_text())
    bbb = next(c for c in rerank["candidates"] if c["ticker"] == "BBB")
    assert bbb["sentiment_polarity"] == 0.0
    assert bbb["sentiment_multiplier"] == 1.0
    assert bbb["composite_score"] == 40.0


def test_rerank_screener_preserves_metadata(rerank_module, tmp_path):
    cands = [{"rank": 1, "ticker": "AAA", "composite_score": 50.0,
              "name": "Acme", "sector_sic": "1234"}]
    run_dir = _make_screener_dir(tmp_path, cands)
    enriched = [
        {"ticker": "AAA", "sentiment": {"finbert": {"n_headlines": 1, "aggregate": 0.1}}},
    ]
    rerank_module._rerank_screener(run_dir, enriched, alpha=0.10)
    rerank = json.loads((run_dir / "screener_sentiment_reranked.json").read_text())
    row = rerank["candidates"][0]
    assert row["name"] == "Acme"
    assert row["sector_sic"] == "1234"
    assert row["original_composite_score"] == 50.0
    assert "rerank_metadata" in rerank
    assert rerank["rerank_metadata"]["alpha"] == 0.10
