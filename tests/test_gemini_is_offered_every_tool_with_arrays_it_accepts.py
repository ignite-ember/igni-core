"""Gemini is offered every tool igni has, with arrays it accepts.

Every Gemini request was refused before the model answered: ``400
INVALID_ARGUMENT: GenerateContentRequest.tools[0].function_declarations[7]
.parameters.properties[tasks].items: missing field``. Gemini requires
``items`` on every array; ``exit_plan_mode(plan, tasks: list | None)``
reached it as an array with none, and one such parameter refuses the
whole request.

The tool's own schema is what the fine-tuned model was trained on and
what every other provider is sent, so it stays as it is: the arrays get
their ``items`` at the Gemini boundary
(:mod:`igni_core.core.config.gemini_schemas`). These build a real
session's main agent, the default way and the ``--read-only`` way, run
it once against Gemini's SDK with the network replaced, and read every
declaration it was handed — so a tool added later that breaks the same
way fails here too, whatever its name.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agno.tools.function import Function
from google.genai import types as genai

from igni_core.core.config.gemini_schemas import (
    FALLBACK_ITEMS,
    KNOWN_ITEMS,
    gemini_definitions,
)
from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.config.provider_builders import GeminiBuilder, OpenAILikeBuilder
from igni_core.core.tools.plan.schemas import PlanTaskInput
from igni_core.core.tools.plan.tool import PlanTool
from tests.session_factory import stub_project, stub_settings

# ── A real session's main agent ────────────────────────────────────


def _main_agent(root: Path, *, read_only: bool) -> Any:
    from igni_core.core.session.core import Session

    project = stub_project(root / "proj")
    settings = stub_settings(project, home=root / "home")
    if read_only:
        # What ``--read-only`` sets (``CliOverrides.from_options``).
        settings.permissions.mode = PermissionMode.PLAN
        settings.permissions.file_write = "deny"
        settings.permissions.read_only = True
    session = Session(settings=settings, project_dir=project)
    assert session.main_team is not None
    return session.main_team


def _entry(provider: str) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        provider=provider, model_id="m", api_key="k", url="http://model.test/v1"
    )


def _done() -> genai.GenerateContentResponse:
    return genai.GenerateContentResponse(
        candidates=[
            genai.Candidate(
                content=genai.Content(role="model", parts=[genai.Part(text="ok")]),
                finish_reason=genai.FinishReason.STOP,
            )
        ]
    )


async def _run(agent: Any) -> None:
    async for _ in agent.arun("hello"):
        pass


async def _gemini_declarations(agent: Any) -> list[genai.FunctionDeclaration]:
    """What the main agent hands Gemini's SDK on its first request."""
    sent: dict[str, Any] = {}

    async def generate_content(**kwargs: Any) -> genai.GenerateContentResponse:
        sent.update(kwargs)
        return _done()

    async def generate_content_stream(**kwargs: Any) -> Any:
        sent.update(kwargs)

        async def chunks() -> Any:
            yield _done()

        return chunks()

    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=generate_content)
    client.aio.models.generate_content_stream = AsyncMock(side_effect=generate_content_stream)
    model = GeminiBuilder().build(_entry("gemini"), cloud_token=None, llm_logger=LlmCallLogger())
    model.get_client = lambda: client  # type: ignore[method-assign]
    agent.model = model
    await _run(agent)
    return [d for tool in sent["config"].tools for d in tool.function_declarations]


async def _openai_tools(agent: Any) -> list[dict[str, Any]]:
    """The ``tools`` the main agent sends an OpenAI-compatible server."""
    bodies: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            chunk = {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
            return httpx.Response(
                200,
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    model = OpenAILikeBuilder().build(
        _entry("openai_like"), cloud_token=None, llm_logger=LlmCallLogger()
    )
    model.http_client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    agent.model = model
    await _run(agent)
    return bodies[0]["tools"]


# ── Every array, at every depth ────────────────────────────────────


def _schemas(schema: genai.Schema | None, path: str) -> Iterator[tuple[str, genai.Schema]]:
    if schema is None:
        return
    yield path, schema
    for name, sub in (schema.properties or {}).items():
        yield from _schemas(sub, f"{path}.properties[{name}]")
    yield from _schemas(schema.items, f"{path}.items")
    for i, sub in enumerate(schema.any_of or []):
        yield from _schemas(sub, f"{path}.any_of[{i}]")


def _arrays_without_items(declarations: list[genai.FunctionDeclaration]) -> list[str]:
    return [
        f"function_declarations[{i}]({d.name}){path}"
        for i, d in enumerate(declarations)
        for path, schema in _schemas(d.parameters, ".parameters")
        if schema.type == genai.Type.ARRAY and schema.items is None
    ]


@pytest.mark.parametrize("read_only", [False, True], ids=["default", "read-only"])
async def test_every_array_gemini_is_sent_says_what_it_holds(tmp_path, read_only):
    declarations = await _gemini_declarations(_main_agent(tmp_path, read_only=read_only))

    names = [d.name for d in declarations]
    assert "exit_plan_mode" in names, names
    assert _arrays_without_items(declarations) == []
    # And each one is a declaration Gemini's own types read back.
    for d in declarations:
        dumped = d.model_dump(mode="json", exclude_none=True)
        assert genai.FunctionDeclaration.model_validate(dumped) == d


async def test_a_plan_s_tasks_reach_gemini_as_the_rows_exit_plan_mode_reads(tmp_path):
    declarations = await _gemini_declarations(_main_agent(tmp_path, read_only=False))
    plan = next(d for d in declarations if d.name == "exit_plan_mode")
    assert plan.parameters is not None and plan.parameters.properties is not None
    tasks = plan.parameters.properties["tasks"]

    assert tasks.type == genai.Type.ARRAY
    assert tasks.nullable is True
    assert tasks.items is not None
    assert tasks.items.type == genai.Type.OBJECT
    assert set(tasks.items.properties or {}) == {"content", "activeForm"}
    assert tasks.items.required == ["content"]


# ── Nothing else moves ─────────────────────────────────────────────


async def test_an_openai_compatible_server_is_sent_the_tools_as_they_were(tmp_path):
    """The model's schema, byte for byte — before and after a Gemini request."""
    agent = _main_agent(tmp_path, read_only=False)
    before = await _openai_tools(agent)
    await _gemini_declarations(agent)
    after = await _openai_tools(agent)

    assert json.dumps(after) == json.dumps(before)
    plan = next(t["function"] for t in before if t["function"]["name"] == "exit_plan_mode")
    own = Function.from_callable(PlanTool.exit_plan_mode).to_dict()
    assert plan["description"] == own["description"]
    assert plan["parameters"] == own["parameters"]
    assert plan["parameters"]["properties"]["tasks"]["anyOf"] == [
        {"type": "array"},
        {"type": "null"},
    ]


def test_the_definitions_handed_in_are_left_as_they_were():
    tools = [_tool("t", {"xs": {"type": "array"}})]
    kept = copy.deepcopy(tools)
    gemini_definitions(tools)
    assert tools == kept


# ── The boundary, one shape at a time ──────────────────────────────


def _tool(name: str, properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "d",
            "parameters": {"type": "object", "properties": properties, **extra},
        },
    }


def _parameters(tool: dict[str, Any]) -> dict[str, Any]:
    out = gemini_definitions([tool])
    assert out is not None
    return out[0]["function"]["parameters"]


def test_an_array_with_no_items_gets_the_loosest_items_gemini_takes():
    params = _parameters(_tool("t", {"xs": {"type": "array", "description": "x"}}))
    assert params["properties"]["xs"] == {
        "type": "array",
        "description": "x",
        "items": dict(FALLBACK_ITEMS),
    }


def test_arrays_at_every_depth_get_items():
    params = _parameters(
        _tool(
            "t",
            {
                "grid": {"type": "array", "items": {"type": "array"}},
                "maybe": {"type": ["array", "null"]},
                "nested": {
                    "type": "object",
                    "properties": {"inner": {"anyOf": [{"type": "array"}, {"type": "null"}]}},
                },
                "ref": {"$ref": "#/$defs/Row"},
            },
            **{"$defs": {"Row": {"type": "object", "properties": {"tags": {"type": "array"}}}}},
        )
    )
    p = params["properties"]
    assert p["grid"]["items"]["items"] == dict(FALLBACK_ITEMS)
    assert p["maybe"]["items"] == dict(FALLBACK_ITEMS)
    assert p["nested"]["properties"]["inner"]["anyOf"][0]["items"] == dict(FALLBACK_ITEMS)
    assert params["$defs"]["Row"]["properties"]["tags"]["items"] == dict(FALLBACK_ITEMS)


def test_an_array_that_says_what_it_holds_is_left_alone():
    typed = {"type": "array", "items": {"type": "integer"}}
    assert _parameters(_tool("t", {"xs": typed}))["properties"]["xs"] == typed


def test_a_known_parameter_gets_its_real_rows_only_on_its_own_tool():
    known = _parameters(_tool("exit_plan_mode", {"tasks": {"type": "array"}}))
    other = _parameters(_tool("something_else", {"tasks": {"type": "array"}}))
    assert known["properties"]["tasks"]["items"]["type"] == "object"
    assert other["properties"]["tasks"]["items"] == dict(FALLBACK_ITEMS)


def test_a_known_parameter_is_found_by_igni_s_name_not_the_wire_name():
    wire = {"exit_plan_mode_wire": "exit_plan_mode"}
    out = gemini_definitions(
        [_tool("exit_plan_mode_wire", {"tasks": {"type": "array"}})],
        lambda name: wire.get(name, name),
    )
    assert out is not None
    assert out[0]["function"]["parameters"]["properties"]["tasks"]["items"]["type"] == "object"


def test_the_plan_rows_gemini_is_told_are_the_keys_plan_task_input_reads():
    rows = KNOWN_ITEMS[("exit_plan_mode", "tasks")]
    keys = {f.alias or name for name, f in PlanTaskInput.model_fields.items()}
    assert set(rows["properties"]) == keys
    row = {"content": "Add the endpoint", "activeForm": "Adding the endpoint"}
    assert PlanTaskInput.validate_batch([row]).errors == []


def test_what_is_not_a_function_definition_passes_through():
    builtin = {"type": "google_search"}
    assert gemini_definitions([builtin]) == [builtin]
    assert gemini_definitions(None) is None
    assert gemini_definitions([]) == []
