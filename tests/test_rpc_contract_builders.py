"""The pieces the RPC contract is built from, each on inputs of its own.

``test_rpc_contract_is_current.py`` holds the whole file to the code; these
hold each part to what it says, so a part that breaks fails here by name and
not as a diff of ``docs/rpc-contract.json``.
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from igni_core.backend import rpc_router
from igni_core.backend.rpc_binding import Correlation
from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.protocol import messages as msg
from igni_core.protocol import rpc_contract
from igni_core.protocol.rpc import RpcAccess, RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.rpc import RpcErrorCode


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return rpc_contract.build()


# ── markdown: docstrings are reStructuredText, the page is Markdown ──


@pytest.mark.parametrize(
    ("rst", "md"),
    [
        ("a ``literal`` here", "a `literal` here"),
        (":class:`Name`", "`Name`"),
        (":class:`~igni_core.protocol.rpc.RpcMethod`", "`RpcMethod`"),
        (":class:`~a.B <x.Y>`", "`B`"),
        (":meth:`a.b.c <d>` and :func:`f`", "`c` and `f`"),
        (":class:`  spaced.Name  `", "`Name`"),
        ("``one`` and ``two``", "`one` and `two`"),
        ("plain text, no markup", "plain text, no markup"),
        ("a `single` backtick pair", "a `single` backtick pair"),
        ("", ""),
    ],
)
def test_markdown_turns_rst_roles_and_literals_into_code_spans(rst: str, md: str) -> None:
    assert rpc_contract.markdown(rst) == md


def test_a_role_with_a_display_target_reads_as_the_name_before_it() -> None:
    assert rpc_contract.markdown(":class:`~a.B <x>`") == "`B`"


def test_only_prose_keys_are_converted_and_nested_values_are_walked() -> None:
    contract = {
        "doc": "a ``x``",
        "summary": ":class:`m.Y`",
        "description": "``d``",
        "access": "``a``",
        "title": "``t``",
        "default": "``v``",
        "nested": {"description": ["``in list``", {"doc": "``deep``"}], "other": "``o``"},
        "list": ["``bare``", {"summary": "``s``"}],
        "count": 3,
    }

    assert rpc_contract._published(contract) == {
        "doc": "a `x`",
        "summary": "`Y`",
        "description": "`d`",
        "access": "`a`",
        "title": "``t``",
        "default": "``v``",
        "nested": {"description": ["`in list`", {"doc": "`deep`"}], "other": "``o``"},
        "list": ["``bare``", {"summary": "`s`"}],
        "count": 3,
    }


def test_no_rst_markup_is_left_in_the_published_prose(contract: dict[str, Any]) -> None:
    text = json.dumps(contract)

    assert "``" not in text
    assert ":class:`" not in text
    assert ":meth:`" not in text


# ── render and write ──


def test_render_is_sorted_two_space_json_with_one_trailing_newline() -> None:
    text = rpc_contract.render({"b": 1, "a": {"d": [1], "c": None}})

    assert text == json.dumps({"a": {"c": None, "d": [1]}, "b": 1}, indent=2) + "\n"
    assert text.endswith("}\n") and not text.endswith("\n\n")


def test_render_of_an_empty_contract_is_empty_not_the_built_one() -> None:
    assert rpc_contract.render({}) == "{}\n"


def test_render_without_a_contract_builds_it(contract: dict[str, Any]) -> None:
    assert rpc_contract.render() == rpc_contract.render(contract)


def test_write_puts_the_rendered_contract_at_the_path_given(tmp_path) -> None:
    target = tmp_path / "out.json"

    path, contract = rpc_contract.write(target)

    assert path == target
    assert target.read_text() == rpc_contract.render(contract)
    assert json.loads(target.read_text())["version"] == rpc_contract.VERSION


def test_write_without_a_path_targets_the_published_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(rpc_contract, "SNAPSHOT_PATH", tmp_path / "rpc-contract.json")

    path, _ = rpc_contract.write()

    assert path == tmp_path / "rpc-contract.json"
    assert path.exists()


# ── the contract's top-level parts (access, errors, durations) ──


def test_the_contract_publishes_each_access_level_s_story(contract: dict[str, Any]) -> None:
    story = contract["access"]

    assert story == rpc_contract.markdown(inspect.getdoc(RpcAccess) or "")
    for level in RpcAccess:
        assert level.value in story


def test_every_method_names_an_access_level_the_backend_gives_it(contract: dict[str, Any]) -> None:
    from igni_core.backend.message_admission import access_of

    for name, entry in contract["methods"].items():
        assert entry["access"] == access_of(RpcMethod(name)).value


def test_the_contract_explains_every_error_code(contract: dict[str, Any]) -> None:
    assert contract["errors"] == {
        code.value: rpc_contract.markdown(code.doc) for code in RpcErrorCode
    }


def test_each_duration_publishes_its_text_and_timeout(contract: dict[str, Any]) -> None:
    assert contract["durations"] == {
        d.value: {"doc": rpc_contract.markdown(d.doc), "timeout_seconds": d.timeout_seconds}
        for d in RpcDuration
    }


def test_a_method_carries_its_duration_and_that_duration_s_timeout(
    contract: dict[str, Any],
) -> None:
    for entry in contract["methods"].values():
        assert (
            entry["timeout_seconds"] == contract["durations"][entry["duration"]]["timeout_seconds"]
        )
    assert contract["methods"]["attach_session"]["duration"] == "io"
    assert contract["methods"]["attach_session"]["timeout_seconds"] == 30


# ── methods: listing, reply, summary, streams, correlation ──


def test_methods_are_listed_by_wire_name_each_once() -> None:
    names = [spec.method.value for spec in rpc_contract.method_specs()]

    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_a_method_implemented_twice_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        rpc_router, "RPC_HANDLERS", (*rpc_router.RPC_HANDLERS, rpc_router.RPC_HANDLERS[0])
    )

    with pytest.raises(RuntimeError, match="is implemented twice"):
        rpc_contract.method_specs()


def _spec(result: Any, doc: str = "") -> rpc_contract.MethodSpec:
    return rpc_contract.MethodSpec(
        method=RpcMethod("attach_session"),
        area="x",
        args=BaseModel,
        result=result,
        doc=doc,
        scope=RpcScope.SESSION,
        access=RpcAccess.CLIENT,
        streams=(),
        may_stream=(),
        duration=RpcDuration.INSTANT,
        correlate=None,
    )


@pytest.mark.parametrize(
    "result",
    [
        dict,
        dict[str, int],
        int | None,
        list[msg.Info],
        dict[str, msg.Info],
        str,
        Annotated[dict, "x"],
    ],
)
def test_a_result_that_is_not_a_message_is_answered_as_an_rpc_response(result: Any) -> None:
    assert _spec(result).reply == "rpc_response"


@pytest.mark.parametrize(
    "result",
    [
        msg.Info,
        None | msg.Info,
        msg.Info | msg.Error,
        msg.Info | msg.Error | None,
        Annotated[msg.Info | msg.Error, Field(discriminator="type")],
    ],
)
def test_a_result_that_is_a_message_is_answered_as_that_message(result: Any) -> None:
    assert _spec(result).reply == "message"


def test_a_result_that_is_a_message_only_sometimes_is_refused() -> None:
    with pytest.raises(TypeError, match="answers a message only sometimes"):
        _ = _spec(msg.Info | dict).reply


def test_a_summary_is_the_first_paragraph_on_one_line() -> None:
    doc = "Attach to a session\nand stream it.\n\nThe long story\nof how."

    assert _spec(dict, doc).summary == "Attach to a session and stream it."


def test_a_method_without_a_docstring_has_an_empty_summary() -> None:
    assert _spec(dict, "").summary == ""


def test_a_stream_is_named_by_wire_type_and_a_push_by_its_channel() -> None:
    assert rpc_contract.stream_name(msg.Info) == "info"
    assert rpc_contract.stream_name(PushChannel.PLAN_DECIDED) == "push_notification:plan_decided"


def test_a_binding_s_correlation_is_by_with_a_source_or_a_fixed_value() -> None:
    assert rpc_contract._correlation(None) is None
    assert rpc_contract._correlation(Correlation(by="id", source="request.id")) == {
        "by": "id",
        "from": "request.id",
    }
    assert rpc_contract._correlation(Correlation(by="payload.cause", equals="listed")) == {
        "by": "payload.cause",
        "equals": "listed",
    }
    assert rpc_contract._correlation(Correlation(by="channel")) == {"by": "channel"}


def test_streams_may_stream_and_correlate_are_published_per_method(
    contract: dict[str, Any],
) -> None:
    methods = contract["methods"]

    assert methods["approve_plan"]["streams"] == [
        "push_notification:permission_mode_changed",
        "push_notification:plan_decided",
    ]
    assert methods["approve_plan"]["correlate"] == {"by": "payload.run_id", "from": "args.run_id"}
    assert methods["notifications_list"]["correlate"] == {
        "by": "payload.cause",
        "equals": "listed",
    }
    for entry in methods.values():
        assert entry["streams"] == sorted(entry["streams"])
        assert entry["may_stream"] == sorted(entry["may_stream"])


# ── messages ──


def test_messages_are_every_concrete_class_with_a_string_wire_type_sorted_by_it() -> None:
    classes = rpc_contract.message_classes()
    types_ = [rpc_contract._wire_type(c) for c in classes]

    assert types_ == sorted(types_)
    assert msg.Info in classes and msg.Message not in classes
    assert all(isinstance(t, str) for t in types_)


def test_a_class_with_no_wire_type_is_refused_by_name() -> None:
    class Typeless(msg.Message):
        pass

    with pytest.raises(TypeError, match="Typeless has no wire type"):
        rpc_contract._wire_type(Typeless)


def test_a_message_that_goes_both_ways_publishes_what_it_sends_and_what_it_accepts(
    contract: dict[str, Any],
) -> None:
    both = {k: v for k, v in contract["messages"].items() if v["direction"] == "both"}

    assert both
    assert all("accepts" in v for v in both.values())
    assert all(
        "accepts" not in v for v in contract["messages"].values() if v["direction"] != "both"
    )


def test_a_message_s_type_field_says_it_is_always_that_type(contract: dict[str, Any]) -> None:
    kind = contract["defs"]["Info"]["properties"]["type"]

    assert kind["const"] == "info"
    assert kind["description"] == "Always `info`: what kind of message this is."


# ── push channels ──


def test_push_payloads_are_every_channel_sorted_by_value() -> None:
    payloads = rpc_contract.push_payloads()
    values = [c.value for c in payloads]

    assert values == sorted(values)
    assert set(values) == {c.value for c in payloads}


def test_every_push_channel_is_published_with_a_named_payload(contract: dict[str, Any]) -> None:
    channels = contract["push_channels"]

    assert list(channels) == [c.value for c in rpc_contract.push_payloads()]
    for entry in channels.values():
        assert entry["doc"].strip()
        assert entry["schema"]["$ref"].startswith("#/defs/")
        assert entry["schema"]["$ref"].removeprefix("#/defs/") in contract["defs"]


def test_a_broadcast_event_s_payload_has_no_channel(contract: dict[str, Any]) -> None:
    payload = contract["defs"]["BackgroundProcessDonePayload"]

    assert "channel" not in payload["properties"]
    assert "channel" not in payload.get("required", [])


class _Ping(BroadcastEvent):
    """A ping."""

    channel: Literal["ping"] = "ping"
    n: int = 0


class _Pong(BroadcastEvent):
    """A pong."""

    channel: Literal["pong"] = "pong"
    word: str


def test_a_broadcast_event_is_published_as_its_own_payload_model_without_its_channel() -> None:
    model = rpc_contract._payload_model(_Ping)

    assert model.__name__ == "_PingPayload"
    assert list(model.model_fields) == ["n"]
    assert model.__doc__ == "A ping."
    assert model.model_config == _Ping.model_config
    assert model(n=2).model_dump() == {"n": 2}


def test_the_channel_less_model_of_an_event_is_made_once() -> None:
    assert rpc_contract._without_channel(_Ping) is rpc_contract._without_channel(_Ping)
    assert rpc_contract._payload_model(_Ping) is rpc_contract._payload_model(_Ping)


def test_a_union_of_events_is_a_union_of_their_payloads() -> None:
    model = rpc_contract._payload_model(_Ping | _Pong)

    schema = TypeAdapter(model).json_schema()
    names = sorted(s["$ref"].rsplit("/", 1)[-1] for s in schema["anyOf"])
    assert names == ["_PingPayload", "_PongPayload"]


def test_an_annotated_union_keeps_its_annotation_and_loses_the_channels() -> None:
    model = rpc_contract._payload_model(Annotated[_Ping | _Pong, Field(description="either")])

    schema = TypeAdapter(model).json_schema()
    assert schema["description"] == "either"
    assert len(schema["anyOf"]) == 2


def test_a_payload_that_is_not_an_event_is_left_as_it_is() -> None:
    class Plain(BaseModel):
        x: int

    assert rpc_contract._payload_model(Plain) is Plain
    assert rpc_contract._payload_model(dict[str, int]) == dict[str, int]
    assert rpc_contract._payload_model(int) is int


# ── schema: aliases and defaults ──


def _schema(tp: Any, mode: str) -> dict[str, Any]:
    return TypeAdapter(tp).json_schema(mode=mode, schema_generator=rpc_contract.WireJsonSchema)  # type: ignore[arg-type]


class _Aliased(BaseModel):
    inner_name: int = Field(default=1, alias="innerName")


class _AliasedOut(_Aliased):
    model_config = {"serialize_by_alias": True}


class _NoValidateAlias(_Aliased):
    model_config = {"validate_by_alias": False, "validate_by_name": True}


def test_a_result_uses_the_alias_only_when_the_model_serializes_by_alias() -> None:
    assert list(_schema(_Aliased, "serialization")["properties"]) == ["inner_name"]
    assert list(_schema(_AliasedOut, "serialization")["properties"]) == ["innerName"]


def test_an_argument_uses_the_alias_unless_the_model_turns_validating_by_it_off() -> None:
    assert list(_schema(_Aliased, "validation")["properties"]) == ["innerName"]
    assert list(_schema(_NoValidateAlias, "validation")["properties"]) == ["inner_name"]


class _Defaults(BaseModel):
    items: list[int] = Field(default_factory=list)
    table: dict[str, int] = Field(default_factory=dict)
    nested: _Aliased = Field(default_factory=lambda: _Aliased())
    raises: int = Field(default_factory=lambda: 1 / 0)
    opaque: object = Field(default_factory=object)
    from_data: int = Field(default_factory=lambda data: 7)  # type: ignore[call-arg, arg-type]
    plain: str = "x"


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_a_default_factory_s_value_is_published_as_a_default_and_not_required(mode: str) -> None:
    props = _schema(_Defaults, mode)["properties"]

    assert props["items"]["default"] == []
    assert props["table"]["default"] == {}
    assert props["nested"]["default"] == {"inner_name": 1}
    assert props["plain"]["default"] == "x"


def test_a_factory_that_raises_or_yields_non_json_publishes_no_default() -> None:
    props = _schema(_Defaults, "validation")["properties"]

    assert "default" not in props["raises"]
    assert "default" not in props["opaque"]


def test_a_factory_that_takes_the_other_fields_is_not_called() -> None:
    props = _schema(_Defaults, "validation")["properties"]

    assert "default" not in props["from_data"]


def test_jsonable_gives_json_or_nothing() -> None:
    from pydantic.json_schema import NoDefault

    assert rpc_contract._jsonable({"a": [1, None]}) == {"a": [1, None]}
    assert rpc_contract._jsonable(_Aliased()) == {"inner_name": 1}
    assert rpc_contract._jsonable(object()) is NoDefault


def test_a_union_payload_is_named_for_its_channel_in_the_defs(contract: dict[str, Any]) -> None:
    unions = [
        name
        for name in contract["defs"]
        if name.endswith("Payload") and "anyOf" in contract["defs"][name]
    ]

    for name in unions:
        assert contract["defs"][name]["title"] == name
        assert contract["defs"][name]["description"].strip()


def test_json_value_is_published_as_any_json(contract: dict[str, Any]) -> None:
    from igni_core.protocol.json_value import JSON_VALUE_SCHEMA

    if "JsonValue" in contract["defs"]:
        assert contract["defs"]["JsonValue"] == JSON_VALUE_SCHEMA


def test_the_pascal_name_of_a_channel_joins_its_words() -> None:
    assert rpc_contract._pascal("host_signal") == "HostSignal"
    assert rpc_contract._pascal("plan") == "Plan"
