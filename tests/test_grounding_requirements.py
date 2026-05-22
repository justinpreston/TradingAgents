"""Enforce the declarative grounding contract.

Every agent module under ``tradingagents/agents/`` must either:

  (a) Bind tools to its LLM (detected by an ``llm.bind_tools(...)`` Call
      anywhere in the module's AST), OR
  (b) Be explicitly listed in
      ``tradingagents.grounding.UNGROUNDED_BY_DESIGN`` with a documented
      rationale, risk note, and mitigation.

This test exists because the worst fabrication failure in the pipeline
(the old social_media_analyst Reddit/X invention) was caused by an
agent that was deployed without tool binding and without a documented
acknowledgment of that fact. The fix was added — but nothing prevented
a future agent from repeating the mistake. This test does.

When adding a new agent module:

* If it has data tools — bind them. The test passes.
* If it's a synthesis/debate/decision node operating on upstream text —
  add an entry to ``UNGROUNDED_BY_DESIGN`` documenting why grounding is
  not possible and which guardrail catches the fabrication risk.

If you genuinely cannot satisfy either of those, the design needs
review — talk to the maintainer before silencing this test.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "tradingagents" / "agents"


# Modules that are not agents themselves — schemas, shared utilities,
# deprecation shims. These are out of scope for the grounding contract.
_NOT_AGENTS = {
    "tradingagents.agents.schemas",
    "tradingagents.agents.managers._risk_profile",
    # Backwards-compat re-export shim — actual implementation is in
    # sentiment_analyst.py which is covered separately.
    "tradingagents.agents.analysts.social_media_analyst",
}


def _agent_modules() -> Iterator[tuple[str, Path]]:
    """Yield (module_dotted_name, path) for every candidate agent module."""
    for path in AGENTS_DIR.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        if "/utils/" in str(path) or path.parent.name == "utils":
            continue
        # tradingagents/agents/foo/bar.py → tradingagents.agents.foo.bar
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        module_name = ".".join(rel.parts)
        if module_name in _NOT_AGENTS:
            continue
        yield module_name, path


def _has_bind_tools_call(tree: ast.AST) -> bool:
    """Return True if the AST contains a ``.bind_tools(...)`` Call.

    We look for ``Attribute(attr='bind_tools')`` as the call target —
    this matches ``llm.bind_tools(tools)`` regardless of the receiver
    name. Comments and string literals containing 'bind_tools' are
    correctly excluded (they aren't Call nodes).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "bind_tools":
            return True
    return False


def test_grounding_contract_covers_all_agents():
    """Every agent must bind tools OR be in UNGROUNDED_BY_DESIGN."""
    from tradingagents.grounding import UNGROUNDED_BY_DESIGN

    violations: list[str] = []
    for module_name, path in _agent_modules():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - shouldn't happen
            pytest.fail(f"Failed to parse {module_name}: {exc}")

        binds_tools = _has_bind_tools_call(tree)
        is_documented_exempt = module_name in UNGROUNDED_BY_DESIGN

        if not binds_tools and not is_documented_exempt:
            violations.append(module_name)
        if binds_tools and is_documented_exempt:
            # Not a violation per se, but worth surfacing: an agent that
            # both binds tools and is in the allowlist suggests the
            # rationale may be stale.
            violations.append(
                f"{module_name} (binds tools AND is listed in "
                "UNGROUNDED_BY_DESIGN — remove from allowlist)"
            )

    if violations:
        pretty = "\n  - ".join(violations)
        pytest.fail(
            "Grounding contract violations:\n  - "
            + pretty
            + "\n\nFix: either bind tools inside the module body "
            "(via ``llm.bind_tools(tools)``) or add an entry to "
            "``tradingagents/grounding/__init__.py::UNGROUNDED_BY_DESIGN`` "
            "with a documented rationale, risk note, and mitigation."
        )


def test_ungrounded_rationale_fields_non_empty():
    """Every allowlist entry must have a non-empty rationale, risk, mitigation."""
    from tradingagents.grounding import UNGROUNDED_BY_DESIGN

    for module_name, rat in UNGROUNDED_BY_DESIGN.items():
        assert rat.module == module_name, (
            f"UNGROUNDED_BY_DESIGN['{module_name}'].module mismatch: "
            f"got {rat.module!r}"
        )
        for field in ("rationale", "risk_note", "mitigation"):
            value = getattr(rat, field)
            assert value and len(value.strip()) >= 20, (
                f"{module_name}.{field} is empty or too short "
                f"({len(value.strip())} chars; need ≥20)"
            )


def test_ungrounded_modules_are_importable():
    """Every module in the allowlist must actually exist + import cleanly."""
    from tradingagents.grounding import UNGROUNDED_BY_DESIGN

    for module_name in UNGROUNDED_BY_DESIGN:
        # We don't need the return value — we're just verifying import.
        importlib.import_module(module_name)
