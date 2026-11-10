"""Any JSON value, said as such in a published schema.

Some values on the wire have no shape the protocol fixes: a workflow's
own input, an extension's settings, a tool call's arguments, a view the
agent drew. Pydantic describes ``Any`` (and ``JsonValue``) as ``{}`` —
"anything" — which a generated client can type nothing from. These say
what the value is: any JSON value.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import WithJsonSchema

#: The JSON Schema of any JSON value.
JSON_VALUE_SCHEMA: dict[str, Any] = {
    "description": "Any JSON value.",
    "type": ["object", "array", "string", "number", "integer", "boolean", "null"],
}

#: A JSON object with keys and values of any JSON kind.
JSON_OBJECT_EXTRA: dict[str, Any] = {"additionalProperties": JSON_VALUE_SCHEMA}

#: A field holding any JSON value. Not validated as JSON: a value dumped in
#: Python mode can hold, say, a ``datetime``, which the encoder writes as a
#: string — the schema describes the wire, where it is always JSON.
WireJson = Annotated[Any, WithJsonSchema(JSON_VALUE_SCHEMA)]

__all__ = ["JSON_OBJECT_EXTRA", "JSON_VALUE_SCHEMA", "WireJson"]
