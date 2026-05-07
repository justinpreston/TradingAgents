"""News enrichment primitives — sentiment scoring and theme classification.

Provides pluggable sentiment scoring (keyword-based default, FinBERT-ready
adapter) and a deterministic keyword-based theme classifier. Used by
``scripts/build_news_enrichment.py`` to produce
``news_enrichment.json`` artifacts that screener and matrix runs can
consume as additional signal.

Design goals:
    1. Zero new runtime deps — the default scorer/classifier work on
       string ops alone. No torch, no transformers, no heavy ML stack.
    2. Pluggable — drop in FinBERT (or any other HF model) via the
       ``SentimentScorer`` protocol when the dep weight is acceptable.
    3. Deterministic by default — keyword rules give reproducible
       outputs for backtest replay. LLM-based classification is opt-in.
    4. Trigger-term transparency — every score includes the matched
       terms so the operator can audit why a ticker was flagged.

----------------------------------------------------------------------
Enabling FinBERT
----------------------------------------------------------------------
``FinBERTScorer`` is included as a documented adapter. To use it::

    pip install torch transformers   # ~1.5-2GB

    from tradingagents.dataflows.news_enrichment import FinBERTScorer
    scorer = FinBERTScorer()  # downloads ProsusAI/finbert on first use
    result = scorer.score_headlines(["Apple beats Q4 estimates", ...])

Until those packages are installed, ``FinBERTScorer()`` raises a clean
``ImportError`` with installation guidance — no surprise crashes in CI.

The pattern mirrors the FinBERT sentiment gate from inference-capital
but stays self-contained inside the TradingAgents tree (no cross-repo
import dependency).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


# ---------------------------------------------------------------------------
# Sentiment scoring
# ---------------------------------------------------------------------------

# Polarity-bearing terms with weights in [-1, 1]. Weights tuned on the
# heuristic that earnings-miss/lawsuit/recall language is more reliably
# negative than a mere "underperform" downgrade, and that "beat/raise"
# language is a stronger positive than a mere upgrade.
_NEGATIVE_TERMS: dict[str, float] = {
    # Severe — almost always bearish for the named ticker
    r"\bfraud\b": -1.0,
    r"\bsec\s+(probe|investigat)": -0.9,
    r"\bclass[- ]action\b": -0.8,
    r"\blawsuit\b": -0.7,
    r"\bsubpoena\b": -0.8,
    r"\brestate(ment|d)?\b": -0.9,
    r"\bdelist": -1.0,
    r"\bbankrupt": -1.0,
    r"\bgoing\s+concern\b": -1.0,
    r"\bfda\s+reject": -0.9,
    r"\brecall\b": -0.7,
    # Earnings/guidance (allow up to 30 chars between key terms — "miss Q4 estimates")
    r"\bmiss(es|ed)?\b.{0,30}\b(estimates|expectations|consensus)\b": -0.7,
    r"\bcuts?\s+guidance\b": -0.7,
    r"\blowered?\s+(forecast|outlook|guidance)\b": -0.6,
    r"\bwarn(s|ed|ing)?\b": -0.5,
    r"\bdisappointing\b": -0.5,
    # Analyst actions
    r"\bdowngrade(s|d)?\b": -0.5,
    r"\bunderperform\b": -0.4,
    # Operational
    r"\bdata\s+breach\b": -0.7,
    r"\bcyber\s*attack\b": -0.6,
    r"\blayoffs?\b": -0.3,
    r"\bplant\s+shutdown\b": -0.4,
    r"\bstrike\b": -0.3,
}

_POSITIVE_TERMS: dict[str, float] = {
    # Earnings/guidance (allow up to 30 chars — "beats Q4 estimates")
    r"\bbeats?\b.{0,30}\b(estimates|expectations|consensus)\b": 0.7,
    r"\braises?\s+(guidance|forecast|outlook)\b": 0.7,
    r"\brecord\s+(revenue|earnings|profit)\b": 0.7,
    r"\bblowout\b": 0.7,
    # Analyst
    r"\bupgrade(s|d)?\b": 0.5,
    r"\bovertaking\b": 0.3,
    r"\bprice\s+target\s+raised\b": 0.5,
    # Corporate actions
    r"\bbuyback\b": 0.5,
    r"\bdividend\s+(increase|raised|hike)\b": 0.5,
    r"\bspin[- ]off\b": 0.3,
    r"\bacquir(es|ing)\b": 0.4,
    # Product/regulatory wins
    r"\bfda\s+approv": 0.8,
    r"\bphase\s+(2|3|ii|iii)\s+success": 0.7,
    r"\blaunches?\b": 0.2,
    r"\bpartnership\b": 0.3,
    r"\bcontract\s+win\b": 0.4,
}


@dataclass
class SentimentResult:
    """Result of scoring a batch of headlines for a ticker.

    Attributes:
        aggregate: Mean polarity in [-1, 1]. 0 = neutral.
        scorer: Name of the scorer that produced this result.
        n_headlines: Number of headlines scored.
        trigger_terms: List of distinct matched terms (deduplicated)
            that contributed non-zero polarity. Useful for audit.
        per_headline: Per-headline polarity scores in input order.
    """

    aggregate: float
    scorer: str
    n_headlines: int
    trigger_terms: list[str] = field(default_factory=list)
    per_headline: list[float] = field(default_factory=list)


class SentimentScorer(Protocol):
    """Pluggable sentiment scorer interface."""

    name: str

    def score_headlines(self, headlines: list[str]) -> SentimentResult:
        ...


class NoOpScorer:
    """Always returns 0.0 — used when sentiment should be inert."""

    name = "noop"

    def score_headlines(self, headlines: list[str]) -> SentimentResult:
        return SentimentResult(
            aggregate=0.0,
            scorer=self.name,
            n_headlines=len(headlines),
            per_headline=[0.0] * len(headlines),
        )


class KeywordScorer:
    """Regex-based polarity scorer.

    Per headline: sum of all matched negative + positive term weights,
    clamped to [-1, 1]. Aggregate: mean across headlines. Trigger terms
    are deduplicated across the batch and returned as readable strings
    (without the regex word-boundary markers).

    This is the default scorer because it has zero ML deps, is fully
    deterministic, fast (~microseconds per headline), and the
    weighted-keyword approach matches the resolution we actually need
    for a screener prefilter (does the news lean clearly negative?
    yes/no/maybe).
    """

    name = "keyword"

    def __init__(
        self,
        negative_terms: dict[str, float] | None = None,
        positive_terms: dict[str, float] | None = None,
    ) -> None:
        self._negatives = {re.compile(p, re.IGNORECASE): w
                           for p, w in (negative_terms or _NEGATIVE_TERMS).items()}
        self._positives = {re.compile(p, re.IGNORECASE): w
                           for p, w in (positive_terms or _POSITIVE_TERMS).items()}

    @staticmethod
    def _humanize(pattern: re.Pattern[str]) -> str:
        # Strip regex meta and collapse whitespace for human-readable triggers
        text = re.sub(r"\\b|\\s\+|\\s\*|\.\{0,\d+\}|[()|?+*\[\]]|\\S\*", " ", pattern.pattern)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _score_one(self, headline: str) -> tuple[float, list[str]]:
        if not headline:
            return 0.0, []
        polarity = 0.0
        triggers: list[str] = []
        for pat, weight in self._negatives.items():
            if pat.search(headline):
                polarity += weight
                triggers.append(self._humanize(pat))
        for pat, weight in self._positives.items():
            if pat.search(headline):
                polarity += weight
                triggers.append(self._humanize(pat))
        polarity = max(-1.0, min(1.0, polarity))
        return polarity, triggers

    def score_headlines(self, headlines: list[str]) -> SentimentResult:
        if not headlines:
            return SentimentResult(
                aggregate=0.0,
                scorer=self.name,
                n_headlines=0,
            )
        per_headline: list[float] = []
        all_triggers: list[str] = []
        for h in headlines:
            score, trig = self._score_one(h)
            per_headline.append(score)
            all_triggers.extend(trig)
        # Dedup while preserving order.
        seen: set[str] = set()
        triggers_dedup: list[str] = []
        for t in all_triggers:
            if t not in seen:
                seen.add(t)
                triggers_dedup.append(t)
        aggregate = sum(per_headline) / len(per_headline)
        return SentimentResult(
            aggregate=aggregate,
            scorer=self.name,
            n_headlines=len(headlines),
            trigger_terms=triggers_dedup,
            per_headline=per_headline,
        )


class FinBERTScorer:
    """ProsusAI/finbert-backed scorer (optional, requires extra deps).

    Mirrors the inference-capital sentiment gate but stays self-contained
    inside TradingAgents. Lazy-loads the model on first call so import
    of this module stays cheap. Caching is intentionally NOT included —
    operators should cache at the scorer or call-site level if needed.

    Raises:
        ImportError: If ``torch`` or ``transformers`` is not installed.
            Both are intentionally NOT in TradingAgents' base deps to
            keep the runtime minimal. Run::

                pip install torch transformers

            then reinstantiate.
    """

    name = "finbert"

    def __init__(self, model_name: str = "ProsusAI/finbert", device: str = "cpu") -> None:
        try:
            import torch  # noqa: F401
            from transformers import (  # noqa: F401
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise ImportError(
                "FinBERTScorer requires torch + transformers. Install with:\n"
                "    pip install torch transformers\n"
                f"(import error: {exc})"
            ) from exc
        self._model_name = model_name
        self._device = device
        self._model: object | None = None
        self._tokenizer: object | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(self._model_name)
        self._model.to(self._device)  # type: ignore[union-attr]
        self._model.eval()  # type: ignore[union-attr]

    def _score_one(self, headline: str) -> float:
        import torch
        self._load()
        assert self._tokenizer is not None and self._model is not None
        inputs = self._tokenizer(  # type: ignore[operator]
            headline, return_tensors="pt", truncation=True, max_length=512
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self._model(**inputs)  # type: ignore[operator]
            probs = torch.softmax(outputs.logits, dim=-1)[0]
        # FinBERT label order: positive=0, negative=1, neutral=2
        return float(probs[0].item() - probs[1].item())

    def score_headlines(self, headlines: list[str]) -> SentimentResult:
        if not headlines:
            return SentimentResult(aggregate=0.0, scorer=self.name, n_headlines=0)
        per_headline = [self._score_one(h) for h in headlines]
        aggregate = sum(per_headline) / len(per_headline)
        return SentimentResult(
            aggregate=aggregate,
            scorer=self.name,
            n_headlines=len(headlines),
            per_headline=per_headline,
        )


# ---------------------------------------------------------------------------
# Theme / catalyst classification
# ---------------------------------------------------------------------------

# Each theme maps to a list of regex patterns. A headline is tagged with
# a theme if it matches ANY of that theme's patterns. Multiple themes can
# fire on the same headline (e.g. "FDA approves Pfizer's drug after
# court rules in their favor" → regulatory + litigation).
_THEME_PATTERNS: dict[str, list[str]] = {
    "m_and_a": [
        r"\bacquir(?:e|es|ed|ing|ition)\b",
        r"\bmerger\b",
        r"\bbuyout\b",
        r"\btender\s+offer\b",
        r"\bleveraged\s+buyout\b|\blbo\b",
        r"\bgoing\s+private\b",
        r"\btake[- ]private\b",
    ],
    "earnings": [
        r"\bearnings\b",
        r"\bq[1-4]\b",
        r"\b(beats?|miss(?:es|ed)?)\s+(?:estimates|expectations|consensus)\b",
        r"\beps\b",
        r"\brevenue\s+(?:beat|miss|growth)\b",
    ],
    "guidance": [
        r"\b(?:raises?|cuts?|lowers?|reaffirms?)\s+(?:guidance|forecast|outlook)\b",
        r"\b(?:fy|full[- ]year)\s+guidance\b",
        r"\bwithdraws?\s+guidance\b",
    ],
    "regulatory": [
        r"\bfda\b",
        r"\bsec\b",
        r"\bftc\b",
        r"\bdoj\b",
        r"\bantitrust\b",
        r"\bregulator(y|s)?\b",
        r"\bcompliance\s+(?:probe|investigation)\b",
        r"\beuropean\s+commission\b",
    ],
    "government_action": [
        r"\bgovernment\b.{0,30}\b(?:stake|equity|invest)",
        r"\btreasury\b.{0,30}\b(?:stake|equity|invest)",
        r"\bnationalization\b",
        r"\btariff(s)?\b",
        r"\bsanction(s|ed|ing)?\b",
        r"\bexport\s+control\b",
        r"\bwhite\s+house\b",
        r"\bcongress\b",
        r"\bdefense\s+contract\b",
    ],
    "leadership": [
        r"\b(?:ceo|cfo|coo|cto|chair(?:man|person)?)\s+(?:resign|step|appoint|named|succeed)",
        r"\b(?:resigns?|steps?\s+down|retires?)\b",
        r"\bnew\s+(?:ceo|cfo|coo|cto)\b",
        r"\bsuccessor\b",
        r"\bappoint(s|ed|ing)?\b",
    ],
    "litigation": [
        r"\blawsuit\b",
        r"\bclass[- ]action\b",
        r"\bsettle(s|d|ment)?\b",
        r"\bcourt\s+(?:rules|ruling)\b",
        r"\btrial\b",
        r"\bpatent\s+infring",
        r"\bcounterclaim\b",
    ],
    "product_launch": [
        r"\b(?:launch(?:es|ed|ing)?|unveil(?:s|ed|ing)?|introduces?|debuts?)\b",
        r"\bnew\s+product\b",
        r"\brelease(s|d)?\s+(?:new|next[- ]gen)\b",
    ],
    "analyst_action": [
        r"\bupgrade(s|d)?\b",
        r"\bdowngrade(s|d)?\b",
        r"\bprice\s+target\b",
        r"\b(?:buy|sell|hold|overweight|underweight|outperform)\s+rating\b",
        r"\binitiate(s|d)?\s+coverage\b",
    ],
    "capital_action": [
        r"\bstock\s+split\b",
        r"\bdividend\s+(?:increase|raised?|cut|suspend)",
        r"\bbuyback\b|\brepurchase\b",
        r"\bsecondary\s+offering\b",
        r"\bspin[- ]off\b",
    ],
}


@dataclass
class ThemeMatch:
    """A theme tag with confidence and matched headlines."""

    label: str
    confidence: float
    matched_headlines: list[int] = field(default_factory=list)  # indices


def classify_themes(headlines: list[str]) -> list[ThemeMatch]:
    """Tag a batch of headlines with theme labels.

    Confidence is the fraction of headlines that match the theme — a
    cluster of multiple matching headlines is more confident than a
    single one. Returned in descending confidence order. Themes with
    zero matches are omitted.

    Deterministic and zero-dep — uses regex pattern matching only. For
    finer-grained classification (e.g. distinguishing "stock split" from
    "split-up of business unit"), drop in an LLM-based classifier as a
    second pass.
    """
    if not headlines:
        return []

    compiled = {label: [re.compile(p, re.IGNORECASE) for p in patterns]
                for label, patterns in _THEME_PATTERNS.items()}

    matches: list[ThemeMatch] = []
    n = len(headlines)
    for label, patterns in compiled.items():
        matched_idx: list[int] = []
        for i, h in enumerate(headlines):
            if any(pat.search(h) for pat in patterns):
                matched_idx.append(i)
        if matched_idx:
            matches.append(ThemeMatch(
                label=label,
                confidence=len(matched_idx) / n,
                matched_headlines=matched_idx,
            ))

    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches


def get_default_scorer() -> SentimentScorer:
    """Return the default sentiment scorer.

    Tries FinBERT first (requires torch + transformers); falls back to the
    zero-dep ``KeywordScorer`` if those packages are not installed. This
    auto-selection keeps the call-site one-liner: each environment gets
    the best signal available without having to special-case install state.

    To force a specific scorer, instantiate it directly instead.
    """
    try:
        return FinBERTScorer()
    except ImportError:
        return KeywordScorer()
