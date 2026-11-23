"""Tool schemas as Gemini accepts them: every array says what it holds.

Gemini's API refuses a function declaration with an array that has no
``items`` — ``400 INVALID_ARGUMENT: ...parameters.properties[tasks].items:
missing field`` — and refuses the whole request with it, so one such
parameter on one tool took every Gemini request down. OpenAI-compatible
servers and Anthropic accept the same array without ``items``.

igni has such a parameter: ``exit_plan_mode(plan, tasks: list | None)``.
A bare ``list`` becomes ``{"type": "array"}`` in the JSON schema Agno
builds from the signature, and Agno's Gemini conversion
(``agno.utils.gemini.convert_schema``) fills in ``items`` only when the
key is there and empty — a missing key becomes an ``ARRAY`` with no
``items``. An MCP server's tool can declare the same.

The tool's own schema is what the shipped, fine-tuned model was trained
on, and is what every other provider is sent: it does not change. So the
gap is closed **at the Gemini boundary and nowhere else**, the way
:mod:`igni_core.core.config.tool_names` closes the name gap:
:class:`GeminiToolSchemas` sits in ``CheckedGemini`` and hands Agno a
copy of each tool definition in which every array, at every depth, has
``items``:

* the element schema igni knows for that parameter (:data:`KNOWN_ITEMS` —
  the rows ``exit_plan_mode`` reads as :class:`PlanTaskInput`), or
* ``{"type": "string"}``, what Agno itself sends for ``items: {}``, when
  nothing more is known.

The definitions Agno shares with every other provider are never touched:
the copy is deep, so Agno's conversion, which rewrites a nullable
property's ``type`` in place, rewrites the copy too.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

#: What an array holds when nothing says: Agno's own choice for ``items: {}``.
FALLBACK_ITEMS: Mapping[str, Any] = MappingProxyType({"type": "string"})

#: The element schema of an array parameter igni's own tools declare as a
#: bare ``list``, by ``(tool, parameter)``. ``exit_plan_mode``'s rows are
#: :class:`~igni_core.core.tools.plan.schemas.PlanTaskInput` read by its wire
#: keys; its docstring says ``content`` is required.
KNOWN_ITEMS: Mapping[tuple[str, str], Mapping[str, Any]] = MappingProxyType(
    {
        ("exit_plan_mode", "tasks"): MappingProxyType(
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "activeForm": {"type": "string"},
                },
                "required": ["content"],
            }
        ),
    }
)

#: Keywords whose value is one subschema, or a list or map of them.
_ONE = ("items", "additionalProperties", "not", "contains")
_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
_MAP = ("properties", "$defs", "definitions", "patternProperties")


def _is_array(schema: Mapping[str, Any]) -> bool:
    kind = schema.get("type")
    return kind == "array" or (isinstance(kind, list) and "array" in kind)


def _with_items(schema: Any, tool: str, parameter: str | None, *, top: bool = False) -> Any:
    """``schema`` with ``items`` on every array in it, filled in place.

    ``parameter`` is the top-level parameter the walk is under — kept
    through ``anyOf`` and friends, since ``list | None`` is an
    ``anyOf`` of the array and ``null``. ``top`` marks the tool's
    ``parameters`` object, whose ``properties`` are the parameters.
    """
    if not isinstance(schema, dict):
        return schema
    if _is_array(schema) and "items" not in schema:
        known = KNOWN_ITEMS.get((tool, parameter)) if parameter else None
        schema["items"] = copy.deepcopy(dict(known if known is not None else FALLBACK_ITEMS))
    for key in _ONE:
        _with_items(schema.get(key), tool, None if key == "items" else parameter)
    for key in _LIST:
        for sub in schema.get(key) or []:
            _with_items(sub, tool, parameter)
    for key in _MAP:
        subs = schema.get(key)
        if isinstance(subs, dict):
            for name, sub in subs.items():
                _with_items(sub, tool, name if top and key == "properties" else None)
    return schema


def gemini_definitions(
    tools: list[Any] | None, registered: Callable[[str], str] = lambda name: name
) -> list[Any] | None:
    """A copy of ``tools`` in which every array has ``items``.

    ``tools`` are Agno's ``{"type": "function", "function": {...}}``
    dicts; anything else is passed through as it is. ``registered`` maps
    a name the provider is sent back to igni's, so :data:`KNOWN_ITEMS`
    is looked up by igni's name.
    """
    if not tools:
        return tools
    out: list[Any] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("parameters"), dict):
            out.append(tool)
            continue
        name = function.get("name")
        key = registered(name) if isinstance(name, str) else ""
        function = copy.deepcopy(function)
        _with_items(function["parameters"], key, None, top=True)
        out.append({**tool, "function": function})
    return out


class GeminiToolSchemas:
    """Mixed into ``CheckedGemini``, before Agno's ``Gemini``.

    ``get_request_params`` is where Agno turns the tool dicts into
    Gemini's ``FunctionDeclaration``\\ s for a request, and
    ``_format_tools`` is what its token counting uses: both are handed
    :func:`gemini_definitions`.
    """

    def _registered_name(self, name: str) -> str:
        names = getattr(self, "_tool_names", None)
        return names().registered(name) if callable(names) else name

    def get_request_params(self, *args: Any, **kwargs: Any) -> Any:
        if "tools" in kwargs:
            kwargs["tools"] = gemini_definitions(kwargs["tools"], self._registered_name)
        elif len(args) >= 3:
            args = (*args[:2], gemini_definitions(args[2], self._registered_name), *args[3:])
        return super().get_request_params(*args, **kwargs)  # type: ignore[misc]

    def _format_tools(self, tools: Any) -> Any:
        formatted = super()._format_tools(tools)  # type: ignore[misc]
        return gemini_definitions(formatted, self._registered_name)


__all__ = ["FALLBACK_ITEMS", "KNOWN_ITEMS", "GeminiToolSchemas", "gemini_definitions"]
