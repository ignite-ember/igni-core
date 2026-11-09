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
      "access": "<who reaches each access level, and how a client gets one>",
      "errors": {"<rpc_response.code>": "<what it means>", ...},
      "durations": {"<duration>": {"doc": "...", "timeout_seconds": <int> | null}},
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
          "streams": ["<message type>" | "push_notification:<channel>", ...],
          "may_stream": [<the same, for what the agent's tools may push meanwhile>],
          "correlate": {"by": "<place in a streamed message>",
                        "from": "<place in the call>" | "equals": "<value>"} | null,
          "duration": "instant" | "io" | "network" | "long" | "person",
          "timeout_seconds": <a timeout leaving a healthy call room> | null
        }
      },
      "messages": {
        "<message type>": {"direction": "to_client" | "to_igni" | "both",
                           "doc": "...", "schema": <JSON Schema>,
                           "accepts": <JSON Schema, for "both": what igni takes>}
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

import functools
import inspect
import json
import operator
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue, NoDefault
from pydantic_core import core_schema

from igni_core.protocol import messages as msg
from igni_core.protocol.json_value import JSON_VALUE_SCHEMA
from igni_core.protocol.rpc import RpcAccess, RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.envelope import Direction
from igni_core.protocol.schemas.rpc import RpcErrorCode

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
    * A field with a default — a ``default_factory``'s included — says
      so and is not required, in either direction.
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

    def get_default_value(self, schema: core_schema.WithDefaultSchema) -> Any:
        """A field's default, its ``default_factory``'s included: an empty
        list or object is a default a client can rely on, and a schema
        without it reads as required."""
        factory = schema.get("default_factory")
        if factory is not None and not schema.get("default_factory_takes_data"):
            try:
                value = factory()  # type: ignore[call-arg]
            except Exception:
                return NoDefault
            dumped = _jsonable(value)
            return dumped if dumped is not NoDefault else NoDefault
        return super().get_default_value(schema)


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
    may_stream: tuple[str, ...]
    duration: RpcDuration
    correlate: dict[str, str] | None

    @property
    def summary(self) -> str:
        return self.doc.split("\n\n", 1)[0].replace("\n", " ").strip()

    @property
    def reply(self) -> str:
        """``message`` when igni answers with the result itself, a message."""
        members = [
            m for m in (typing.get_args(self.result) or (self.result,)) if m is not type(None)
        ]
        messages = [m for m in members if isinstance(m, type) and issubclass(m, msg.Message)]
        if messages and len(messages) != len(members):
            raise TypeError(
                f"{self.method.value} answers a message only sometimes: the reply's shape "
                "would depend on the result"
            )
        return "message" if messages else "rpc_response"


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
                result=typing.get_type_hints(fn, include_extras=True)["return"],
                doc=inspect.getdoc(fn) or "",
                scope=binding.scope,
                access=access_of(method),
                streams=tuple(sorted({stream_name(s) for s in binding.streams})),
                may_stream=tuple(sorted({stream_name(s) for s in binding.may_stream})),
                duration=binding.duration,
                correlate=_correlation(binding.correlate),
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
        if _direction(cls) is Direction.BOTH:
            inputs.append(((_wire_type(cls), "message"), "validation", TypeAdapter(cls)))
    pushes = push_payloads()
    for channel, payload in pushes.items():
        model = _payload_model(payload.model)
        inputs.append(((channel.value, "push"), payload.mode, TypeAdapter(model)))

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
            "may_stream": list(spec.may_stream),
            "correlate": spec.correlate,
            "duration": spec.duration.value,
            "timeout_seconds": spec.duration.timeout_seconds,
        }

    wire_messages: dict[str, Any] = {}
    for cls in messages:
        direction = _direction(cls)
        mode = "validation" if direction is Direction.TO_IGNI else "serialization"
        entry = {
            "direction": direction.value,
            "doc": inspect.getdoc(cls) or "",
            "schema": schemas[((_wire_type(cls), "message"), mode)],
        }
        if direction is Direction.BOTH:
            # What igni sends is ``schema``; what it accepts from a client
            # can leave out what it fills in itself.
            entry["accepts"] = schemas[((_wire_type(cls), "message"), "validation")]
        wire_messages[_wire_type(cls)] = entry

    push_channels: dict[str, Any] = {}
    for channel, payload in pushes.items():
        schema = schemas[((channel.value, "push"), payload.mode)]
        if "$ref" not in schema:
            # A union: named for its channel, like every other payload.
            name = _pascal(channel.value) + "Payload"
            defs[name] = {**schema, "title": name, "description": payload.doc}
            schema = {"$ref": f"#/defs/{name}"}
        push_channels[channel.value] = {"doc": payload.doc, "schema": schema}

    return {
        "version": VERSION,
        "access": inspect.getdoc(RpcAccess) or "",
        "errors": {code.value: code.doc for code in RpcErrorCode},
        "durations": {
            d.value: {"doc": d.doc, "timeout_seconds": d.timeout_seconds} for d in RpcDuration
        },
        "defs": defs,
        "methods": methods,
        "messages": wire_messages,
        "push_channels": push_channels,
    }


def push_payloads() -> Mapping[PushChannel, Any]:
    """Every push channel's payload, from :data:`~igni_core.backend.push_payloads.PUSH_PAYLOADS`."""
    from igni_core.backend.push_payloads import PUSH_PAYLOADS

    return dict(sorted(PUSH_PAYLOADS.items(), key=lambda item: item[0].value))


def render(contract: dict[str, Any] | None = None) -> str:
    """The exact bytes of the published file, for writing or comparing."""
    return json.dumps(contract or build(), indent=2, sort_keys=True) + "\n"


def write(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    contract = build()
    target = path or SNAPSHOT_PATH
    target.write_text(render(contract))
    return target, contract


def _payload_model(model: Any) -> Any:
    """The payload a push carries, as a type: a broadcast-bus event is
    sent without its ``channel`` (``BroadcastEvent.to_payload``), so it is
    published as a model of its own, ``<Event>Payload``, without it."""
    from igni_core.core.broadcast_schema import BroadcastEvent

    if isinstance(model, type) and issubclass(model, BroadcastEvent):
        return _without_channel(model)
    origin = typing.get_origin(model)
    if origin is typing.Annotated:
        inner, *extras = typing.get_args(model)
        return typing.Annotated[(_payload_model(inner), *extras)]  # type: ignore[valid-type]
    members = typing.get_args(model)
    if members and origin in (typing.Union, types.UnionType):
        return functools.reduce(operator.or_, (_payload_model(m) for m in members))
    return model


@functools.cache
def _without_channel(event: type[BaseModel]) -> type[BaseModel]:
    fields: dict[str, Any] = {
        name: (field.annotation, field)
        for name, field in event.model_fields.items()
        if name != "channel"
    }
    return create_model(  # type: ignore[call-overload, no-any-return]
        f"{event.__name__}Payload",
        __doc__=event.__doc__,
        __config__=ConfigDict(**event.model_config),
        **fields,
    )


def _pascal(wire: str) -> str:
    return "".join(part.capitalize() for part in wire.split("_"))


def _correlation(correlate: Any) -> dict[str, str] | None:
    """How the contract says a binding's correlation."""
    if correlate is None:
        return None
    out = {"by": correlate.by}
    if correlate.source:
        out["from"] = correlate.source
    if correlate.equals:
        out["equals"] = correlate.equals
    return out


def _jsonable(value: Any) -> Any:
    """``value`` as JSON, or ``NoDefault`` when it is not."""
    try:
        return json.loads(json.dumps(value, default=lambda v: v.model_dump(mode="json")))
    except (TypeError, ValueError, AttributeError):
        return NoDefault


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
