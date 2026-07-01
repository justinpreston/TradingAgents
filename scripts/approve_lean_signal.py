"""Flip `approved` on one or more rows in lean/signals.json.

This is the ONLY tool that may flip `approved`. export_lean_signals.py
always defaults new signals to `approved:false` (the human-in-the-loop
gate documented in CLAUDE.md's LEAN section) — nothing else in the
pipeline is allowed to auto-flip it. This script exists so the user has
one explicit, auditable command to say "yes, trade this."

Every flip appends an audit record to lean/approvals_log.jsonl so there's
a durable trail of what was approved/unapproved and when, independent of
signals.json's current state (which export_lean_signals.py regenerates
weekly, carrying forward `approved:true` for ids that persist).

Examples::

    # Approve a couple of picks from this week's export
    .venv/bin/python scripts/approve_lean_signal.py --id NUE-2026-06-27 --id ADI-2026-06-27

    # Revoke an approval
    .venv/bin/python scripts/approve_lean_signal.py --id NUE-2026-06-27 --unapprove

    # See what's in the file and its current approval state
    .venv/bin/python scripts/approve_lean_signal.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SIGNALS_FILE = REPO_ROOT / "lean" / "signals.json"
DEFAULT_APPROVALS_LOG = REPO_ROOT / "lean" / "approvals_log.jsonl"


class SignalsFileError(Exception):
    """Raised for a missing or malformed signals.json."""


def load_signals_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SignalsFileError(
            f"signals file not found: {path}. Run scripts/export_lean_signals.py first."
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SignalsFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("signals"), list):
        raise SignalsFileError(f"{path} does not look like a signals manifest (missing `signals` list)")
    return doc


def _available_ids(doc: dict[str, Any]) -> list[str]:
    return [s.get("id") for s in doc.get("signals", []) if isinstance(s, dict) and s.get("id")]


def set_approval(
    doc: dict[str, Any],
    ids: list[str],
    approved: bool,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Flip `approved` for the given ids in-place on `doc`.

    Returns (doc, applied_ids, unknown_ids). Raises SignalsFileError (with
    the list of available ids in the message) if ANY requested id is
    unknown — a partial apply on a typo'd id would be worse than refusing
    outright, since the caller might assume the whole batch went through.
    """
    by_id = {s["id"]: s for s in doc.get("signals", []) if isinstance(s, dict) and s.get("id")}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        available = ", ".join(sorted(by_id)) or "(none)"
        raise SignalsFileError(
            f"unknown signal id(s): {', '.join(unknown)}. Available ids: {available}"
        )
    applied: list[str] = []
    for signal_id in ids:
        by_id[signal_id]["approved"] = approved
        applied.append(signal_id)
    return doc, applied, unknown


def write_signals_file(path: Path, doc: dict[str, Any]) -> None:
    """Write back preserving key order and the repo's 2-space indent convention."""
    text = json.dumps(doc, indent=2, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def append_audit_record(
    log_path: Path,
    *,
    ids: list[str],
    action: str,
    ts: str | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record_ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as f:
        for signal_id in ids:
            record = {
                "ts": record_ts,
                "id": signal_id,
                "action": action,
                "source": "approve_lean_signal",
            }
            f.write(json.dumps(record) + "\n")


def render_list(doc: dict[str, Any]) -> str:
    lines = []
    lines.append(f"# LEAN signals — {doc.get('measure_date', '(no measure_date)')}")
    lines.append("")
    signals = doc.get("signals", [])
    if not signals:
        lines.append("_No signals in file._")
        return "\n".join(lines)
    lines.append("| ID | Ticker | Tier | Approved |")
    lines.append("|---|---|---|---|")
    for s in signals:
        mark = "✅" if s.get("approved") else "—"
        lines.append(f"| `{s.get('id')}` | {s.get('ticker')} | {s.get('tier')} | {mark} |")
    n_approved = sum(1 for s in signals if s.get("approved"))
    lines.append("")
    lines.append(f"{n_approved}/{len(signals)} approved.")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", action="append", default=None, dest="ids",
                        help="Signal id to approve (repeatable). e.g. NUE-2026-06-27")
    parser.add_argument("--unapprove", action="store_true",
                        help="Flip the given --id(s) to approved:false instead of true")
    parser.add_argument("--list", action="store_true",
                        help="Print all signals with their current approval state and exit")
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--approvals-log", type=Path, default=DEFAULT_APPROVALS_LOG,
                        help="Audit log path (default: lean/approvals_log.jsonl)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        doc = load_signals_file(args.signals_file)
    except SignalsFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list:
        print(render_list(doc))
        return 0

    if not args.ids:
        print("ERROR: provide at least one --id (repeatable), or use --list.", file=sys.stderr)
        return 2

    approved = not args.unapprove
    try:
        doc, applied, _unknown = set_approval(doc, args.ids, approved)
    except SignalsFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    write_signals_file(args.signals_file, doc)
    action = "approve" if approved else "unapprove"
    append_audit_record(args.approvals_log, ids=applied, action=action)

    verb = "Approved" if approved else "Unapproved"
    print(f"✅ {verb} {len(applied)} signal(s): {', '.join(applied)}")
    print(f"   Wrote {args.signals_file}")
    print(f"   Logged to {args.approvals_log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
