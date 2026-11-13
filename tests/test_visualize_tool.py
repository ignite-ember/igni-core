"""Tests for ``VisualizeTools`` — the visualizer sub-agent's
render tool.

Note on architecture: the tool's ``visualize`` method itself is a
confirmation. The visualization payload has ALREADY been streamed to
the FE by the time this method runs — see
``_LoggingModel.process_response_stream`` +
``orchestrate.py``'s ``CustomEvent`` handler for the pipeline that
turns each streaming tool_call arg fragment into a
``visualization_delta`` event, and the ``ToolCallStartedEvent``
branch that emits the final ``final=True`` delta once args are
complete. This tool exists so the sub-agent has an actual tool to
CALL (the whole point of switching from content-stream to tool
call), and so ``VisualizeTools`` shows up in ``ToolRegistry``.

Covers:
- Tool is discoverable via ``ToolRegistry`` under the ``Visualize`` name.
- ``visualize()`` returns a friendly confirmation string for any spec
  shape; no exceptions on unusual input.
- The spec parameter is typed (the contract is a model, not a dict).
- The malformed-input cases run through the *entrypoint Agno
  installs*, not the bare method: Agno validates the tool call's
  arguments before the body runs, so that boundary is where a spec
  the FE would have drawn could still fail the call.
- The toolkit owns no card id — the FE's dedup id rides the orchestrate
  stream, not the tool.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.tools.registry import ToolRegistry
from igni_core.core.tools.visualize import VisualizeTools
from igni_core.core.tools.visualize_schemas import JsonRenderSpec

#: A spec as the visualizer sub-agent writes it — the mapping the
#: model produced, which is what Agno hands the tool at runtime.
RAW_SPEC = {
    "root": "r",
    "elements": {
        "r": {"type": "Text", "props": {"text": "hello"}, "children": []},
    },
}


async def call_as_agno_does(**arguments: object) -> str:
    """Call the tool the way Agno calls it.

    Agno builds the tool call's arguments from the model's JSON and
    runs them through the entrypoint it derives from the method's
    annotations — a pydantic-validated wrapper that runs *before* the
    body. Calling the bare method would skip that boundary and hide
    exactly the failures that reach the user in production.
    """
    tool = VisualizeTools()
    function = tool.async_functions["visualize"]
    function.process_entrypoint(strict=False)
    return await function.entrypoint(**arguments)


class TestVisualizeTool:
    @pytest.mark.asyncio
    async def test_returns_confirmation_string(self):
        # The tool's runtime job is to give the model something to
        # recap; the wire-side rendering is already done by the
        # ``CustomEvent`` interceptor.
        tool = VisualizeTools()
        result = await tool.visualize(RAW_SPEC, title="Hi")
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_handles_empty_title(self):
        tool = VisualizeTools()
        # Empty title should not raise; the FE renders without a
        # heading in that case.
        result = await tool.visualize(RAW_SPEC, title="")
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_handles_minimal_spec(self):
        # A degenerate spec (just root + one leaf) still parses at
        # the tool level. Any deeper validation lives on the FE via
        # ``@json-render/core`` — we don't reinvent it here.
        tool = VisualizeTools()
        result = await tool.visualize(
            {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        )
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_accepts_a_spec_model(self):
        # The typed contract: the same call made with a model works.
        tool = VisualizeTools()
        result = await tool.visualize(JsonRenderSpec.model_validate(RAW_SPEC))
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_a_malformed_spec_does_not_fail_the_call(self):
        # The FE has already drawn (or placeholdered) the card by the
        # time the tool runs, so a payload the BE cannot model must
        # still confirm rather than raise — through the boundary Agno
        # actually calls the tool across.
        result = await call_as_agno_does(spec="not a spec at all")
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_a_boolean_visibility_does_not_fail_the_call(self):
        # ``visible`` is a boolean in the FE's own contract — the
        # visualizer's prompt says ``true``/``false`` mean always
        # visible/hidden. A payload the FE would have drawn must not
        # be rejected here.
        result = await call_as_agno_does(
            spec={"root": "r", "elements": {"r": {"type": "Text", "visible": True}}}
        )
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_a_missing_title_does_not_fail_the_call(self):
        # The title is optional, and a model that leaves it out often
        # writes ``null``.
        result = await call_as_agno_does(spec=RAW_SPEC, title=None)
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_a_spec_without_its_envelope_does_not_fail_the_call(self):
        # A spec missing the root id is one the FE renders as an
        # empty card, not one that may fail the call.
        result = await call_as_agno_does(spec={"elements": {}})
        assert "Emitted visualization" in result

    def test_the_spec_parameter_is_typed(self):
        # The contract is a model, not a bare dict: Agno derives the
        # tool schema the model sees from this annotation.
        tool = VisualizeTools()
        (function,) = tool.async_functions.values()
        function.process_entrypoint(strict=False)
        spec_schema = function.to_dict()["parameters"]["properties"]["spec"]
        assert spec_schema["properties"]["elements"]["type"] == "object"
        assert spec_schema["properties"]["root"]["type"] == "string"
        # The envelope the visualizer's prompt asks for: a root id and
        # an element map, with the state bag optional.
        assert sorted(spec_schema["required"]) == ["elements", "root"]

    def test_the_toolkit_owns_no_card_id(self):
        # The FE dedupes cards on the ``spec_id`` the orchestrate
        # stream assigns (``SubAgentStreamState.vis_spec_id``). The
        # toolkit used to mint a second, never-read one — dead state
        # that claimed an ownership it did not have.
        tool = VisualizeTools()
        assert not hasattr(tool, "_spec_id")
        assert not hasattr(tool, "wire")


class TestRegistryWiring:
    def test_visualize_appears_in_available_tools(self):
        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
        )
        assert "Visualize" in reg.available_tools

    def test_resolve_returns_visualize_tools_instance(self):
        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
        )
        tools = reg.resolve(["Visualize"])
        assert len(tools) == 1
        assert isinstance(tools[0], VisualizeTools)

    @pytest.mark.asyncio
    async def test_registry_wired_tool_still_executes(self):
        # A registry with a broadcast still builds the tool and the
        # tool still runs: the broadcast is not the visualize path.
        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
            broadcast=lambda _event: None,
        )
        (tool,) = reg.resolve(["Visualize"])
        result = await tool.visualize(
            {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        )
        assert "Emitted visualization" in result


class TestTheTitleIsAlwaysText:
    """Whatever the model writes as the title reaches the body as a string."""

    @pytest.mark.parametrize(
        ("written", "recorded"),
        [
            ("AAPL", "AAPL"),
            (None, ""),
            (True, ""),
            (False, ""),
            (7, "7"),
            (2.5, "2.5"),
            ({"a": 1}, ""),
            (["a"], ""),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_body_receives_text(self, written, recorded, caplog):
        caplog.set_level("DEBUG", logger="igni_core.core.tools.visualize")
        result = await call_as_agno_does(spec=RAW_SPEC, title=written)
        assert result == "Emitted visualization."
        assert f"title={recorded!r}" in caplog.text

    @pytest.mark.asyncio
    async def test_the_log_counts_elements_and_never_echoes_the_spec(self, caplog):
        caplog.set_level("DEBUG", logger="igni_core.core.tools.visualize")
        spec = {
            "root": "r",
            "elements": {
                "r": {"type": "Text", "props": {"text": "secret-payload"}},
                "s": {"type": "Text"},
            },
        }
        await call_as_agno_does(spec=spec)
        assert "2 element(s)" in caplog.text
        assert "secret-payload" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_malformed_spec_is_logged_as_no_elements(self, caplog):
        caplog.set_level("DEBUG", logger="igni_core.core.tools.visualize")
        await call_as_agno_does(spec=["junk"])
        assert "0 element(s)" in caplog.text

    @pytest.mark.asyncio
    async def test_repeated_calls_each_confirm(self):
        tool = VisualizeTools()
        assert [await tool.visualize(RAW_SPEC) for _ in range(3)] == ["Emitted visualization."] * 3
