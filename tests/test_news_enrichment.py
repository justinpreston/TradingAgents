"""Tests for tradingagents.dataflows.news_enrichment.

Covers:
  - KeywordScorer polarity in [-1, 1] and aggregate math
  - KeywordScorer trigger terms surfaced for audit
  - NoOpScorer returns 0.0 for any input
  - FinBERTScorer raises ImportError cleanly when deps missing
    (or instantiates if deps are present — both paths verified)
  - classify_themes deterministic, ranked by confidence
  - classify_themes empty input returns empty list
  - Multiple themes can fire on a single headline
  - SentimentResult dataclass surfaces n_headlines + per_headline

The keyword + theme classifier is the production default (zero ML
deps), so these tests guard the production path. The FinBERT adapter
test is opportunistic: if torch+transformers are present we verify
instantiation, otherwise we verify the ImportError contract.
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.news_enrichment import (
    FinBERTScorer,
    KeywordScorer,
    NoOpScorer,
    SentimentResult,
    classify_themes,
    get_default_scorer,
)


# ---------------------------------------------------------------------------
# Sentiment scoring
# ---------------------------------------------------------------------------

def test_keyword_scorer_neutral_headline_scores_zero():
    s = KeywordScorer()
    r = s.score_headlines(["Apple announces new iPhone color"])
    assert r.scorer == "keyword"
    assert r.n_headlines == 1
    assert r.aggregate == 0.0
    assert r.per_headline == [0.0]
    assert r.trigger_terms == []


def test_keyword_scorer_negative_polarity():
    """Severe negative terms drive aggregate to a strong negative."""
    s = KeywordScorer()
    r = s.score_headlines([
        "Company facing SEC probe over accounting fraud",
        "Class-action lawsuit filed against XYZ",
    ])
    assert r.aggregate < -0.5
    assert all(score < 0 for score in r.per_headline)
    # Trigger terms surfaced for audit
    assert any("fraud" in t for t in r.trigger_terms)


def test_keyword_scorer_positive_polarity():
    s = KeywordScorer()
    r = s.score_headlines([
        "Apple beats Q4 estimates handily",
        "Analysts upgrade rating to buy",
    ])
    assert r.aggregate > 0.4
    assert all(score > 0 for score in r.per_headline)


def test_keyword_scorer_clamping():
    """Per-headline polarity is clamped to [-1, 1] even when many negatives stack."""
    s = KeywordScorer()
    r = s.score_headlines([
        "Fraud lawsuit subpoena class-action delisting bankruptcy SEC probe",
    ])
    assert r.per_headline[0] >= -1.0
    assert r.per_headline[0] <= 1.0


def test_keyword_scorer_aggregate_is_mean():
    """Aggregate must be arithmetic mean of per-headline scores."""
    s = KeywordScorer()
    r = s.score_headlines([
        "Apple announces new feature",  # 0.0
        "Company misses Q3 expectations",  # negative
        "Stock upgraded by analysts",  # positive
    ])
    assert abs(r.aggregate - sum(r.per_headline) / 3) < 1e-9


def test_keyword_scorer_empty_input():
    s = KeywordScorer()
    r = s.score_headlines([])
    assert r.aggregate == 0.0
    assert r.n_headlines == 0
    assert r.per_headline == []
    assert r.trigger_terms == []


def test_keyword_scorer_blank_strings_handled():
    s = KeywordScorer()
    r = s.score_headlines(["", "  ", "Apple beats Q4 estimates"])
    assert r.n_headlines == 3
    # Last headline should still register positive
    assert r.aggregate > 0


def test_keyword_scorer_dedups_triggers_across_batch():
    s = KeywordScorer()
    r = s.score_headlines([
        "Class-action lawsuit filed",
        "Another class-action lawsuit",
        "Third lawsuit pending",
    ])
    # "lawsuit" should appear in trigger terms only once despite three matches
    lawsuit_count = sum(1 for t in r.trigger_terms if t == "lawsuit")
    assert lawsuit_count == 1


def test_keyword_scorer_humanizes_pattern_to_readable():
    """Trigger terms should not contain raw regex metacharacters."""
    s = KeywordScorer()
    r = s.score_headlines(["Apple beats Q4 estimates"])
    for term in r.trigger_terms:
        # No backslash, no parens, no pipes
        assert "\\" not in term
        assert "(" not in term
        assert ")" not in term


def test_noop_scorer_always_zero():
    s = NoOpScorer()
    r = s.score_headlines(["Anything", "Even fraud"])
    assert r.aggregate == 0.0
    assert all(x == 0.0 for x in r.per_headline)
    assert r.scorer == "noop"


def test_get_default_scorer_returns_keyword():
    """The default scorer must require zero extra deps."""
    s = get_default_scorer()
    assert isinstance(s, KeywordScorer)
    assert s.name == "keyword"


# ---------------------------------------------------------------------------
# FinBERT adapter — opportunistic
# ---------------------------------------------------------------------------

def test_finbert_scorer_import_contract():
    """FinBERTScorer either instantiates (deps present) or raises ImportError."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        deps_present = True
    except ImportError:
        deps_present = False

    if deps_present:
        # Should not raise — model lazy-loads on first call
        scorer = FinBERTScorer()
        assert scorer.name == "finbert"
    else:
        with pytest.raises(ImportError, match="torch.*transformers"):
            FinBERTScorer()


# ---------------------------------------------------------------------------
# Theme classifier
# ---------------------------------------------------------------------------

def test_classify_themes_empty_input():
    assert classify_themes([]) == []


def test_classify_themes_no_match_returns_empty():
    """Headlines with no theme keywords return empty list (not noise)."""
    out = classify_themes([
        "Generic announcement about the weather",
        "Another irrelevant headline",
    ])
    assert out == []


def test_classify_themes_government_action_caught():
    """Specifically test that the INTC-style government-stake thesis is detected."""
    out = classify_themes([
        "US government takes equity stake in Intel",
        "Treasury invests in struggling chipmaker",
    ])
    labels = [t.label for t in out]
    assert "government_action" in labels


def test_classify_themes_sorted_by_confidence_desc():
    """Higher-confidence themes come first."""
    headlines = [
        "Apple beats Q3 earnings estimates",
        "Apple beats Q4 earnings estimates",
        "Apple sees stronger Q1 earnings",
        "CEO Tim Cook resigns",  # leadership: 1/4 = 0.25
    ]
    out = classify_themes(headlines)
    confidences = [t.confidence for t in out]
    assert confidences == sorted(confidences, reverse=True)


def test_classify_themes_multiple_themes_per_headline():
    """A single headline can fire multiple themes."""
    out = classify_themes([
        "FDA approves new drug after court rules in favor of Pfizer",
    ])
    labels = {t.label for t in out}
    # Both regulatory (FDA) and litigation (court rules) should match
    assert "regulatory" in labels
    assert "litigation" in labels


def test_classify_themes_confidence_is_match_fraction():
    """confidence == n_matched / n_total."""
    headlines = [
        "Apple Q4 earnings beat",  # earnings
        "Apple announces new Mac",  # neutral / product
        "Apple Q3 earnings call",  # earnings
        "Random headline",  # neutral
    ]
    out = classify_themes(headlines)
    earnings = next((t for t in out if t.label == "earnings"), None)
    assert earnings is not None
    assert earnings.confidence == pytest.approx(2 / 4)
    assert len(earnings.matched_headlines) == 2


# ---------------------------------------------------------------------------
# SentimentResult dataclass
# ---------------------------------------------------------------------------

def test_sentiment_result_default_fields():
    r = SentimentResult(aggregate=0.0, scorer="keyword", n_headlines=0)
    assert r.trigger_terms == []
    assert r.per_headline == []
