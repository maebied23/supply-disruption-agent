"""Company values live in the packs, never in agent code.

Collects every company-specific token from every pack (record ids, role names, currencies,
names, time zone, rule ids) and fails if one appears in code under src/sdra: string
literals or identifiers, not comments or docstrings (where examples are fine).
"""

import ast
import re
from pathlib import Path

import pytest

from sdra.pack import load_pack

ROOT = Path(__file__).parents[1]
PACKS = [p.parent for p in sorted((ROOT / "packs").glob("*/policy.yaml"))]


def pack_tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for directory in PACKS:
        pack = load_pack(directory)
        policy, seed = pack.policy, pack.seed
        values = [policy.company.name, policy.company.timezone, *policy.roles, *policy.fx_rates_to_base,
                  *(r.id for r in policy.autonomy.rules)]
        for table in type(seed).model_fields:
            for row in getattr(seed, table):
                values += [v for k, v in row.model_dump().items() if k in ("id", "name") and isinstance(v, str)]
        for v in values:
            out.setdefault(v, directory.name)
    return out


def code_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            out.append(n.value)
        elif isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
    return out


# Schema words that a pack also happens to use as a value (here, as a rule id).
ALLOWED = {
    "reallocation",  # the policy key options.reallocation
}


@pytest.mark.parametrize("path", sorted((ROOT / "src" / "sdra").rglob("*.py")), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_pack_values_in_agent_code(path):
    tokens = {t: pack for t, pack in pack_tokens().items() if t not in ALLOWED and len(t) >= 3}
    hits = []
    for s in code_strings(path):
        for token, pack in tokens.items():
            if re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", s):
                hits.append(f"{token!r} ({pack}) in {s[:60]!r}")
    assert not hits, hits


def test_packs_differ_where_it_matters():
    """The second pack is a real test of generality, not a copy."""
    a, b = (load_pack(p) for p in PACKS[:2])
    assert a.policy.company.base_currency != b.policy.company.base_currency
    assert set(a.policy.roles).isdisjoint(b.policy.roles)
    assert {i.id for i in a.seed.items}.isdisjoint({i.id for i in b.seed.items})
