"""quant/ must stay a leaf: no other package may import it, so its numpy / scipy /
cvxpy dependencies never load on their import path."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_OTHER_PACKAGES = ("cycle", "pricing_agent", "fundamental_agent", "entity_resolution", "api")


def _imports_quant(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            n.name == "quant" or n.name.startswith("quant.") for n in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (
            node.module == "quant" or (node.module or "").startswith("quant.")
        ):
            return True
    return False


def test_no_other_package_imports_quant() -> None:
    offenders = [
        str(py.relative_to(_SRC))
        for pkg in _OTHER_PACKAGES
        for py in (_SRC / pkg).rglob("*.py")
        if _imports_quant(py)
    ]
    assert not offenders, f"quant/ imported by: {offenders}"
