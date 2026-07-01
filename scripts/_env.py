"""Shared .env loader — resolves the repo root from this file's location
instead of the process cwd.

``load_dotenv(".env")`` (used historically across several scripts) silently
loads nothing when the script is invoked from a different working directory
(e.g. via a symlinked ``~/.copilot/skills/...`` invocation). Import this
module's ``load_repo_env()`` instead so env vars load regardless of cwd.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_repo_env() -> None:
    """Load the repo-root .env file regardless of the current working directory."""
    load_dotenv(REPO_ROOT / ".env")
