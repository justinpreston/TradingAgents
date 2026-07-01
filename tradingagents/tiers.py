"""Single source of truth for A/B/C/VETO tier derivation.

Tier is derived per-row from a ``verdict_ledger.json`` row, NOT a stored
field. The canonical rule (see ``CLAUDE.md``):

    - classification != 'PICK'        -> 'VETO' if VETOED else '—'
    - conservative_pt is None         -> 'C'   (cons declined to model)
    - pt_compression_pct < 5.0        -> 'A'   (tight dual-frame agreement)
    - else                            -> 'B'   (cons engaged but skeptical)

A second, stricter variant additionally caps a row at 'B' when
``pt_quality_flags`` is non-empty — a SUSPECT price target (e.g. >50% from
current price, likely hallucinated) cannot drive a Tier A allocation even
if compression is tight. Both variants are implemented here so every call
site can delegate without changing its existing behavior; see
``tier_for_row(..., suspect_caps_a=True)``.

Sites that must stay in sync with this module (see CLAUDE.md's "Tier A/B/C
classification" section):
  - scripts/build_options_overlay.py::_tier()          (suspect_caps_a=True)
  - scripts/build_chronos_overlay.py::_tier()           (suspect_caps_a=True)
  - scripts/index_runs.py::_classification_to_tier()    (suspect_caps_a=True)
  - scripts/grounding_audit.py::_tier_for_row()         (suspect_caps_a=True)
  - scripts/build_html_report.py::_tier()               (suspect_caps_a=False)
  - scripts/quick_verdict.py::_tier()                   (suspect_caps_a=False)
  - scripts/compare_insider_ablation.py::_tier()        (suspect_caps_a=False)
  - scripts/portfolio_load_context.py::_row_tier()      (suspect_caps_a=False)

If the 5.0 compression threshold ever changes, update it here only — every
wrapper delegates to this module.
"""

from __future__ import annotations

VETO = "VETO"
NONE_TIER = "—"

# Rank ladder: higher = better verdict. Mirrors compare_insider_ablation.py
# and portfolio_load_context.py's historical hand-rolled ladders.
TIER_RANK: dict[str | None, int] = {
    "VETO": 0,
    "—": 1,
    None: 1,
    "C": 2,
    "B": 3,
    "A": 4,
}


def tier_for_row(row: dict, *, suspect_caps_a: bool = False) -> str:
    """Derive the canonical tier label for a verdict_ledger row.

    Parameters
    ----------
    row:
        A verdict_ledger.json row (or equivalent dict) with at least
        ``classification``, ``conservative_pt``, and ``pt_compression_pct``.
    suspect_caps_a:
        When True, a non-empty ``pt_quality_flags`` list on the row caps
        the result at 'B' even when compression < 5.0. Several sites
        (options overlay, chronos overlay, index_runs, grounding audit,
        html report) adopted this stricter rule as a grounded-pipeline
        guardrail; other sites (quick_verdict, portfolio context, ablation
        comparison) do not check it. Both are preserved here so callers
        can delegate without behavior change.

    Returns
    -------
    One of ``'VETO'``, ``'—'``, ``'C'``, ``'A'``, ``'B'``.
    """
    cls = row.get("classification")
    if cls != "PICK":
        return VETO if cls == "VETOED" else NONE_TIER
    if row.get("conservative_pt") is None:
        return "C"
    comp = row.get("pt_compression_pct")
    if comp is not None and comp < 5.0:
        if suspect_caps_a and (row.get("pt_quality_flags") or []):
            return "B"
        return "A"
    return "B"


def tier_rank(tier: str | None) -> int:
    """Numeric rank where larger = better verdict: VETO < — < C < B < A."""
    return TIER_RANK.get(tier, 1)
