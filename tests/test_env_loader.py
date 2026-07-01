"""Tests for scripts/_env.py::load_repo_env.

``load_dotenv(".env")`` (used historically by several scripts) silently
loads nothing when invoked from a cwd other than the repo root — e.g. via
a symlinked ~/.copilot/skills/... invocation, which was flagged as the
motivating bug. load_repo_env() must resolve the repo root from __file__
instead of the process cwd, so it works regardless of invocation cwd.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore_cwd():
    original = Path.cwd()
    yield
    os.chdir(original)


def test_load_repo_env_finds_dotenv_from_foreign_cwd(tmp_path, monkeypatch):
    """Simulate invocation from an unrelated directory (e.g. a symlinked
    skill's cwd) and confirm the repo-root .env still loads."""
    sentinel_key = "TRADINGAGENTS_TEST_SENTINEL_VALUE"
    dotenv_path = REPO_ROOT / ".env"
    original_contents = dotenv_path.read_text(encoding="utf-8") if dotenv_path.exists() else None
    try:
        with dotenv_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{sentinel_key}=loaded_from_repo_root\n")

        monkeypatch.delenv(sentinel_key, raising=False)
        foreign_cwd = tmp_path / "some" / "unrelated" / "dir"
        foreign_cwd.mkdir(parents=True)
        os.chdir(foreign_cwd)

        # Sanity check: a naive cwd-relative load_dotenv(".env") would find nothing here.
        from dotenv import load_dotenv
        assert load_dotenv(".env") is False

        import importlib
        import scripts._env as env_mod
        importlib.reload(env_mod)
        env_mod.load_repo_env()

        assert os.environ.get(sentinel_key) == "loaded_from_repo_root"
    finally:
        if original_contents is not None:
            dotenv_path.write_text(original_contents, encoding="utf-8")
        os.environ.pop(sentinel_key, None)


def test_load_repo_env_resolves_repo_root_from_file_not_cwd():
    import scripts._env as env_mod
    assert env_mod.REPO_ROOT == REPO_ROOT
    assert (env_mod.REPO_ROOT / "CLAUDE.md").exists()


def test_load_repo_env_is_a_thin_wrapper(monkeypatch):
    """load_repo_env must call dotenv.load_dotenv with the resolved repo-root
    .env path (not a bare relative ".env" string)."""
    import scripts._env as env_mod

    captured = {}

    def fake_load_dotenv(path, *args, **kwargs):
        captured["path"] = path
        return True

    monkeypatch.setattr(env_mod, "load_dotenv", fake_load_dotenv)
    env_mod.load_repo_env()

    assert captured["path"] == REPO_ROOT / ".env"


# Root runners that must use load_repo_env() instead of a bare cwd-relative
# load_dotenv(). run_copilot_aggressive_aligned.py is intentionally absent —
# it's a thin re-exec shim with no env loading of its own; run_screener.py
# predates _env.py but already passes an explicit repo-root path.
_ADOPTED_ROOT_RUNNERS = [
    "run_copilot_matrix.py",
    "run_copilot_persona_aligned.py",
    "run_copilot_multi.py",
    "run_copilot_opus.py",
    "run_copilot_opus_multi.py",
    "run_opus.py",
    "run_github_models.py",
    "resynthesize_pm.py",
]


@pytest.mark.parametrize("runner", _ADOPTED_ROOT_RUNNERS)
def test_root_runner_uses_load_repo_env(runner):
    """Each root runner must import scripts._env.load_repo_env and must not
    call a bare cwd-relative load_dotenv()."""
    src = (REPO_ROOT / runner).read_text(encoding="utf-8")
    assert "load_repo_env" in src, f"{runner} has not adopted scripts._env"
    assert "load_dotenv()" not in src, f"{runner} still calls bare load_dotenv()"
