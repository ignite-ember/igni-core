"""Build the RPC contract igni-core publishes: every method, and every message.

``docs/rpc-contract.json`` is what igni-sdk generates its typed client
from. Nothing in it is written by hand: each method's entry is read off
the method that implements it — the
:class:`~igni_core.backend.rpc_binding.RpcBinding` its ``@rpc`` tag
carries (arguments model, scope, streams), its return annotation (the
result), its docstring (the documentation) — and who may call it off
the sets the backend enforces access with. Every message is read off
its class. The schemas are Pydantic's, sharing one ``defs`` table.

The file has this shape::

    {
      "version": 1,
      "defs": {"<name>": <JSON Schema>, ...},
      "methods": {
        "<wire name>": {
          "area": "<the module implementing it>",
          "summary": "<first line of the docstring>",
          "doc": "<the whole docstring>",
          "scope": "session" | "igni",
          "access": "client" | "approver" | "window" | "page" | "platform",
          "args": <JSON Schema>,
          "result": <JSON Schema>,
          "reply": "rpc_response" | "message",
          "streams": ["<message type>" | "push_notification:<channel>", ...]
        }
      },
      "messages": {
        "<message type>": {"direction": "to_client" | "to_igni" | "both",
                           "doc": "...", "schema": <JSON Schema>}
      },
      "push_channels": {
        "<channel>": {"doc": "...", "schema": <JSON Schema of the payload>}
      }
    }

``reply`` is ``message`` for a method whose result is itself a message
(an ``info``, a ``status_update``): igni answers with that message, its
``id`` the request's, in place of an ``rpc_response``.

``tests/test_rpc_contract_is_current.py`` regenerates the file and fails
when the committed copy differs; ``make rpc-contract`` writes it.
"""

from __future__ import annotations

import inspect
import json
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

from igni_core.protocol import messages as msg
from igni_core.protocol.json_value import JSON_VALUE_SCHEMA
from igni_core.protocol.rpc import RpcAccess, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.envelope import Direction

#: Where igni-core publishes it. igni-sdk keeps a copy and generates from it.
SNAPSHOT_PATH = Path(__file__).resolve().parents[3] / "docs" / "rpc-contract.json"

VERSION = 1

_REF_TEMPLATE = "#/defs/{model}"


class WireJsonSchema(GenerateJsonSchema):
    """Pydantic's JSON Schema, describing what actually crosses the wire.

    * A field's wire name is its alias only where the model says so:
      results and messages are dumped with plain ``model_dump()``, which
      uses the alias only for a model whose config sets
      ``serialize_by_alias``; arguments are validated, which takes the
      alias whenever there is one.
    * In serialization mode a field with a default is still always sent,
      so it is required unless it can be left out.
    """

    def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
        config = schema["cls"].model_config
        by_alias = self.by_alias
        if self.mode == "serialization":
            self.by_alias = bool(config.get("serialize_by_alias", False))
        else:
            self.by_alias = bool(config.get("validate_by_alias", True))
        try:
            return super().model_schema(schema)
        finally:
            self.by_alias = by_alias

    def field_is_required(
        self,
        field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField,
        total: bool,
    ) -> bool:
        if self.mode == "serialization":
            return not (field.get("serialization_exclude") or field.get("serialization_exclude_if"))
        return super().field_is_required(field, total)


@dataclass(frozen=True)
class MethodSpec:
    """One RPC method, as read off the method implementing it."""

    method: RpcMethod
    area: str
    args: Any
    result: Any
    doc: str
    scope: RpcScope
    access: RpcAccess
    streams: tuple[str, ...]

    @property
    def summary(self) -> str:
        return self.doc.split("\n\n", 1)[0].replace("\n", " ").strip()

    @property
    def reply(self) -> str:
        """``message`` when igni answers with the result itself, a message."""
        result = self.result
        is_message = isinstance(result, type) and issubclass(result, msg.Message)
        return "message" if is_message else "rpc_response"


def stream_name(stream: type[msg.Message] | PushChannel) -> str:
    """How the contract names something a call streams."""
    if isinstance(stream, PushChannel):
        return f"push_notification:{stream.value}"
    return _wire_type(stream)


def method_specs() -> list[MethodSpec]:
    """Every method, from the classes that implement them.

    Imports are function-local so :mod:`igni_core.protocol` stays
    importable without dragging in :mod:`igni_core.backend`.
    """
    from igni_core.backend.message_admission import access_of
    from igni_core.backend.pool_rpcs import POOL_RPCS
    from igni_core.backend.rpc_binding import RpcBinding
    from igni_core.backend.rpc_router import RPC_HANDLERS

    specs: dict[RpcMethod, MethodSpec] = {}
    for owner in (*RPC_HANDLERS, *POOL_RPCS):
        for method, (name, binding) in RpcBinding.on(owner).items():
            if method in specs:
                raise RuntimeError(f"{method.value} is implemented twice")
            fn = getattr(owner, name)
            specs[method] = MethodSpec(
                method=method,
                area=owner.__module__.rsplit(".", 1)[-1],
                args=binding.args,
                result=typing.get_type_hints(fn)["return"],
                doc=inspect.getdoc(fn) or "",
                scope=binding.scope,
                access=access_of(method),
                streams=tuple(sorted({stream_name(s) for s in binding.streams})),
            )
    return [specs[m] for m in sorted(specs, key=lambda m: m.value)]


def message_classes() -> list[type[msg.Message]]:
    """Every concrete message the wire carries, by wire type."""
    found = [
        cls
        for _name, cls in inspect.getmembers(msg, inspect.isclass)
        if issubclass(cls, msg.Message) and isinstance(_wire_type_or_none(cls), str)
    ]
    return sorted(found, key=_wire_type)


def build() -> dict[str, Any]:
    """The contract, as the JSON object ``docs/rpc-contract.json`` holds."""
    specs = method_specs()
    messages = message_classes()

    inputs: list[tuple[tuple[str, str], JsonSchemaMode, TypeAdapter[Any]]] = []
    for spec in specs:
        inputs.append(((spec.method.value, "args"), "validation", TypeAdapter(spec.args)))
        inputs.append(((spec.method.value, "result"), "serialization", TypeAdapter(spec.result)))
    for cls in messages:
        mode: JsonSchemaMode = (
            "validation" if _direction(cls) is Direction.TO_IGNI else "serialization"
        )
        inputs.append(((_wire_type(cls), "message"), mode, TypeAdapter(cls)))
    pushes = push_payloads()
    for channel, payload in pushes.items():
        inputs.append(((channel.value, "push"), payload.mode, TypeAdapter(payload.model)))

    schemas, top = TypeAdapter.json_schemas(
        inputs, ref_template=_REF_TEMPLATE, schema_generator=WireJsonSchema
    )
    defs: dict[str, Any] = dict(top.get("$defs", {}))
    if "JsonValue" in defs:
        # Pydantic describes its ``JsonValue`` as ``{}``; it is any JSON value.
        defs["JsonValue"] = JSON_VALUE_SCHEMA

    methods: dict[str, Any] = {}
    for spec in specs:
        key = spec.method.value
        methods[key] = {
            "area": spec.area,
            "summary": spec.summary,
            "doc": spec.doc,
            "scope": spec.scope.value,
            "access": spec.access.value,
            "args": schemas[((key, "args"), "validation")],
            "result": schemas[((key, "result"), "serialization")],
            "reply": spec.reply,
            "streams": list(spec.streams),
        }

    wire_messages: dict[str, Any] = {}
    for cls in messages:
        direction = _direction(cls)
        mode = "validation" if direction is Direction.TO_IGNI else "serialization"
        wire_messages[_wire_type(cls)] = {
            "direction": direction.value,
            "doc": inspect.getdoc(cls) or "",
            "schema": schemas[((_wire_type(cls), "message"), mode)],
        }

    push_channels: dict[str, Any] = {}
    for channel, payload in pushes.items():
        schema = schemas[((channel.value, "push"), payload.mode)]
        if _broadcast_events(payload.model):
            schema = _without_channel(schema, defs)
        push_channels[channel.value] = {"doc": payload.doc, "schema": schema}

    return {
        "version": VERSION,
        "defs": defs,
        "methods": methods,
        "messages": wire_messages,
        "push_channels": push_channels,
    }


def push_payloads() -> Mapping[PushChannel, Any]:
    """Every push channel's payload, from :data:`~igni_core.backend.push_payloads.PUSH_PAYLOADS`."""
    from igni_core.backend.push_payloads import PUSH_PAYLOADS

    return dict(sorted(PUSH_PAYLOADS.items(), key=lambda item: item[0].value))


def _broadcast_events(model: Any) -> bool:
    """Whether ``model`` is a broadcast-bus event (or a union of them), whose
    payload is its fields less ``channel``."""
    from igni_core.core.broadcast_schema import BroadcastEvent

    members = typing.get_args(model) or (model,)
    return all(isinstance(m, type) and issubclass(m, BroadcastEvent) for m in members)


def _without_channel(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with each event it names inlined, its ``channel`` dropped:
    the payload carries every field but the one the push names."""
    if "$ref" in schema:
        event = dict(defs[schema["$ref"].rsplit("/", 1)[-1]])
        event["properties"] = {
            k: v for k, v in event.get("properties", {}).items() if k != "channel"
        }
        if "required" in event:
            event["required"] = [k for k in event["required"] if k != "channel"]
        return event
    return {
        **schema,
        "anyOf": [_without_channel(member, defs) for member in schema.get("anyOf", [])],
    }


def render(contract: dict[str, Any] | None = None) -> str:
    """The exact bytes of the published file, for writing or comparing."""
    return json.dumps(contract or build(), indent=2, sort_keys=True) + "\n"


def write(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    contract = build()
    target = path or SNAPSHOT_PATH
    target.write_text(render(contract))
    return target, contract


def _wire_type_or_none(cls: type[msg.Message]) -> object:
    field = cls.model_fields.get("type")
    return getattr(field, "default", None) if field is not None else None


def _wire_type(cls: type[msg.Message]) -> str:
    wire = _wire_type_or_none(cls)
    if not isinstance(wire, str):
        raise TypeError(f"{cls.__name__} has no wire type")
    return wire


def _direction(cls: type[msg.Message]) -> Direction:
    return cls.direction
