"""Shared risk-profile addenda for synthesizer agents (Research Manager, Portfolio Manager).

Both the Research Manager and Portfolio Manager are *synthesizer* seats: each
takes a structured debate as input (bull vs. bear; aggressive vs. neutral vs.
conservative) and produces a final stance on the same five-rating scale
(Buy / Overweight / Hold / Underweight / Sell). The investor's risk mandate
applies equally to both — the disposition text below is the same one used at
the PM seat, also injected at the RM seat so the upstream investment plan
(and therefore the Trader's BUY/SELL/HOLD proposal) carries the same frame.
"""

from __future__ import annotations


RISK_PROFILE_ADDENDA: dict = {
    "aggressive": (
        "**Investor Risk Profile: Aggressive Growth.**\n"
        "The portfolio's mandate is aggressive capital appreciation, not capital preservation. "
        "Apply this disposition when synthesising:\n"
        "- When the bull case identifies a coherent fundamental or technical growth signal "
        "AND the bear case has provided a well-defined stop or invalidation level, prefer "
        "**Buy** or **Overweight** over Hold.\n"
        "- Treat 'extended valuation', 'near resistance', or 'overbought RSI' alone as "
        "**insufficient** to downgrade — these are entry-timing concerns, not exit triggers, "
        "and are addressed via staged sizing and stops rather than rating downgrades.\n"
        "- Reserve **Underweight** or **Sell** for cases where the bear identifies concrete "
        "deterioration in the underlying operating engine (e.g., margin compression, demand "
        "destruction, deteriorating cash conversion, accounting concerns, broken trend on volume).\n"
        "- Position sizing should express conviction in the upside, not absence of risk: "
        "use stops, staged entry, and core/trade splits to manage risk while remaining exposed.\n"
        "- The investor's precept: 'It's not how much you make, it's how much you keep' — "
        "translates here to disciplined stops, NOT to defaulting to Hold/Underweight when "
        "growth signals are intact."
    ),
    "conservative": (
        "**Investor Risk Profile: Conservative.**\n"
        "The portfolio's mandate is capital preservation with steady income. "
        "Apply this disposition when synthesising:\n"
        "- When the bear case identifies any risk to the underlying operating engine, "
        "valuation, or technical structure, prefer **Hold** or **Underweight** over Buy/Overweight.\n"
        "- Require multiple corroborating bull signals (earnings momentum AND margin expansion "
        "AND favorable technicals AND reasonable valuation) before upgrading to Overweight or Buy.\n"
        "- Treat ambiguous or 'mixed' setups as **Hold** by default.\n"
        "- Prefer larger margins of safety; require confirmed catalysts before adding."
    ),
    "neutral": None,
}


def format_risk_profile_block(risk_profile) -> str:
    """Return the prompt-ready risk-profile section, or empty string for no-op profiles."""
    if risk_profile in (None, "", "neutral"):
        return ""
    addendum = RISK_PROFILE_ADDENDA.get(risk_profile)
    if not addendum:
        return ""
    return f"\n---\n\n{addendum}\n"
