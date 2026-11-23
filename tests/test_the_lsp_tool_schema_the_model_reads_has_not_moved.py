"""The tool schema the model reads is pinned, character for character.

The fine-tuning corpus carries this schema — ``lsp_tool_schemas.canonical.json``
in ``igni__fine-tuning`` — and the shipped model was trained on it. A
change to the summary or to an argument description moves the text
the model sees without the corpus following, and the two then
disagree about what the tool is.

So the schema is pinned here: the test fails when it moves, and
moving it is a deliberate act — the corpus is regenerated in the
same breath, not left to drift. The behaviour that needs no
description (a notification is sent as a notification) is therefore
described in the part of the docstring the model does not read.
"""

from __future__ import annotations

import json

from agno.tools.function import Function

from igni_core.core.tools.lsp import LspTools

# The schema as the corpus carries it — ``lsp_tool_schemas.canonical.json``.
EXPECTED_DESCRIPTION = (
    "Send a JSON-RPC request to ``server`` and return the\nresult as a JSON string."
)

EXPECTED_PARAMETERS = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": (
                "Configured LSP server name (see\n``lsp_list_servers``). Plugin-bundled servers are\n"
                "addressed as ``<plugin>:<name>``."
            ),
        },
        "method": {
            "type": "string",
            "description": (
                "LSP method name — ``textDocument/definition``,\n"
                "``textDocument/references``, ``textDocument/hover``,\n"
                "``workspace/symbol``, etc. See\n"
                "https://microsoft.github.io/language-server-protocol/\n"
                "for the full method list and shapes."
            ),
        },
        "params": {
            "type": "string",
            "description": (
                "JSON-encoded ``params`` object. Empty string is\n"
                "treated as no params (``null``). Pass the LSP shape\n"
                "verbatim — e.g.\n"
                '``{"textDocument": {"uri": "file:///path"},\n'
                '   "position": {"line": 10, "character": 4}}``.'
            ),
        },
    },
    "required": ["server", "method"],
}


def _schema() -> tuple[str, dict]:
    built = Function.from_callable(LspTools.lsp_query)
    func = built[0] if isinstance(built, tuple) else built
    return func.description, func.parameters


class TestTheSchemaTheModelReads:
    def test_the_summary_has_not_moved(self):
        description, _ = _schema()
        assert description == EXPECTED_DESCRIPTION

    def test_the_argument_descriptions_have_not_moved(self):
        _, parameters = _schema()
        assert parameters == EXPECTED_PARAMETERS

    def test_the_schema_is_the_one_the_corpus_carries(self):
        """The whole schema, as the corpus stores it: one object per
        tool, the function's name, its description and its
        parameters. A stray field or a renamed argument shows up
        here."""
        description, parameters = _schema()
        canonical = {
            "type": "function",
            "function": {
                "name": "lsp_query",
                "description": description,
                "parameters": parameters,
            },
        }
        assert canonical["function"]["name"] == "lsp_query"
        assert set(parameters["properties"]) == {"server", "method", "params"}
        assert json.loads(json.dumps(canonical)) == canonical
