"""CodeIndex left igni, and took its graph database and its embedder with it.

The ratchet that used to live here counted the modules outside
``core/code_index/`` that imported it — 30, then 19, then 15 — so the
extraction could not quietly lose ground. It is finished: the local
index, its Neo4j processes, the JDK they run on, the sentence-
transformer and ``codeindex_cypher`` are the CodeIndex extension's.
igni provides no databases or models to extensions; each runs what it
needs from its own data directory.

So the rule is now absolute: nothing in igni imports any of it — nor
Artifacts', which left first. (``test_a_capability_can_be_removed``
measured the edges while both were leaving; there are none to measure.)
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "igni_core"

#: Modules igni no longer has, and libraries it no longer uses.
GONE = (
    "igni_core.core.code_index",
    "igni_core.core.tools.codeindex",
    "igni_core.core.graph",
    "igni_core.core.artifacts",
    "igni_core.core.tools.artifacts",
    "igni_core.core.embeddings",
    "igni_core.backend.neo4j_runtime",
    "igni_core.backend.jdk_bootstrap",
    "igni_core.jvm",
    "igni_core.extensions.capabilities",
    "neo4j",
    "sentence_transformers",
)


def _imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_nothing_in_igni_imports_codeindex_neo4j_or_an_embedder():
    offenders = [
        f"{path.relative_to(_SRC)}: {module}"
        for path in sorted(_SRC.rglob("*.py"))
        for module in sorted(_imports_of(path))
        if any(module == gone or module.startswith(f"{gone}.") for gone in GONE)
    ]
    assert not offenders, "\n".join(offenders)


def test_the_packages_are_gone():
    for package in ("core/code_index", "core/tools/codeindex", "core/graph", "jvm"):
        assert not (_SRC / package).exists(), f"{package} is back"
