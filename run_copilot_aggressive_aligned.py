"""Back-compat shim — folded into run_copilot_persona_aligned.py.

This script used to be a standalone persona/model-routing variant. Its
``AGGRESSIVE_PERSONA_MODELS`` table now lives in
``run_copilot_persona_aligned.py`` (selected via ``--persona-routing
aggressive-aligned``), which ``run_copilot_matrix.py`` invokes directly.

This shim re-execs run_copilot_persona_aligned.py with the same argv,
defaulting --persona-routing to aggressive-aligned when the caller didn't
specify one, so any existing script/alias invoking this file keeps working.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TARGET = REPO_ROOT / "run_copilot_persona_aligned.py"


def main() -> int:
    argv = sys.argv[1:]
    sys.stderr.write(
        "⚠ run_copilot_aggressive_aligned.py is deprecated — use "
        "run_copilot_persona_aligned.py --persona-routing aggressive-aligned. "
        "Re-execing now.\n"
    )
    if "--persona-routing" not in argv:
        argv = ["--persona-routing", "aggressive-aligned", *argv]
    os.execv(sys.executable, [sys.executable, str(TARGET), *argv])


if __name__ == "__main__":
    sys.exit(main())
