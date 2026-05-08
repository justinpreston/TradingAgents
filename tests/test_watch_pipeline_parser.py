"""Tests for the watch_pipeline.py log parser.

These cover the regressions fixed in the all-tiers replay:
  - tier transition resets per-tier state (no Phase carryover)
  - "Done in Xs." sets authoritative elapsed (catch-up safe)
  - "All tiers complete" marks pipeline complete (no recent_lines window)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from watch_pipeline import (  # noqa: E402
    WatchState,
    fmt_dur,
    process_line,
)
from datetime import timedelta  # noqa: E402


@pytest.fixture
def state(tmp_path: Path) -> WatchState:
    fake_log = tmp_path / "fake.log"
    fake_log.write_text("")
    return WatchState(log_path=fake_log)


def _feed(state: WatchState, lines: list[str]) -> None:
    for line in lines:
        process_line(line + "\n", state)


def test_tier_transition_resets_phase(state: WatchState) -> None:
    """When a new tier banner appears, prior tier's Phase must not carry over."""
    _feed(state, [
        "│ Tier · LARGE                                                              │",
        "── Phase 5 · NEXT STEPS ──",
        "│ Tier · MEGA                                                              │",
    ])
    mega = state.tier("mega")
    assert mega.status == "running"
    # The old "Phase 5 · Next Steps" must not have leaked into mega
    assert mega.current_phase == "", f"phase carryover bug: {mega.current_phase!r}"


def test_tier_transition_marks_previous_done(state: WatchState) -> None:
    _feed(state, [
        "│ Tier · MID                                                              │",
        "│ Tier · LARGE                                                              │",
    ])
    assert state.tier("mid").status == "done"
    assert state.tier("large").status == "running"


def test_done_in_seconds_sets_elapsed_override(state: WatchState) -> None:
    """The per-tier 'Done in Xs.' footer is the authoritative elapsed source."""
    _feed(state, [
        "│ Tier · MID                                                              │",
        "  Done in 189s.",
    ])
    mid = state.tier("mid")
    assert mid.status == "done"
    assert mid.elapsed_override_s == 189.0
    # Renderer uses fmt_dur on the override → "3:09"
    assert fmt_dur(timedelta(seconds=mid.elapsed_override_s)) == "3:09"


def test_done_in_seconds_does_not_match_total_elapsed_line(state: WatchState) -> None:
    """The all-tiers footer 'Total elapsed: 341s (5.7 min)' must NOT trigger
    the per-tier RE_TIER_DONE regex — different format."""
    _feed(state, [
        "│ Tier · MID                                                              │",
        "  Total elapsed: 341s (5.7 min)",
    ])
    mid = state.tier("mid")
    assert mid.elapsed_override_s is None
    assert mid.status == "running"


def test_all_tiers_complete_marks_pipeline_done(state: WatchState) -> None:
    _feed(state, [
        "│ Tier · MEGA                                                              │",
        "  scoring 52/52 (100%) — last: MRK",
        "│ All tiers complete                                                       │",
    ])
    assert state.completed_at is not None
    assert state.tier("mega").status == "done"
    assert state.current_tier is None


def test_full_three_tier_replay() -> None:
    """End-to-end: replay a synthetic three-tier transcript and verify all
    three tiers end up done with correct elapsed values and the pipeline
    is marked complete — exactly the scenario that broke for the user."""
    log = REPO_ROOT / "scripts" / "watch_pipeline.py"  # any existing path
    state = WatchState(log_path=log)
    _feed(state, [
        "│ Tier · MID                                                              │",
        "[universe] 2041 tickers",
        "  universe 2041/2041 (100%) — last: ZTS",
        "[scoring] 571 tickers",
        "  scoring 571/571 (100%) — last: ZIM",
        "  Done in 189s.",
        "│ Tier · LARGE                                                              │",
        "[universe] 1500 tickers",
        "  universe 1500/1500 (100%) — last: ZTS",
        "[scoring] 509 tickers",
        "  scoring 509/509 (100%) — last: CW",
        "  Done in 124s.",
        "│ Tier · MEGA                                                              │",
        "[universe] 372 tickers",
        "  universe 372/372 (100%) — last: MRK",
        "[scoring] 52 tickers",
        "  scoring 52/52 (100%) — last: MRK",
        "  Done in 27s.",
        "│ All tiers complete                                                       │",
        "  Total elapsed: 341s (5.7 min)",
    ])
    assert state.tier("mid").status == "done"
    assert state.tier("mid").elapsed_override_s == 189.0
    assert state.tier("large").status == "done"
    assert state.tier("large").elapsed_override_s == 124.0
    assert state.tier("mega").status == "done"
    assert state.tier("mega").elapsed_override_s == 27.0
    assert state.completed_at is not None
