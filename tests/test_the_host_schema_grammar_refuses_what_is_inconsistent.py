"""The grammar the host model is written in: what it refuses and how it reads.

The real schema only shows the happy path of the validator. Each
refusal is seen here on a small schema built to be wrong in one way,
and the lookups and type expressions on a small schema that is right.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.spec import (
    ErrorCode,
    FieldSpec,
    HostSchema,
    ObjectSpec,
    PermissionSpec,
    TypeExpr,
    TypeSpec,
    field,
    method,
    param,
    prop,
    schema_json,
    signal,
    type_words,
    typedef,
)

PERMISSIONS = (
    PermissionSpec(name="chat.read", doc="Read."),
    PermissionSpec(name="chat.write", doc="Write."),
)


def _schema(*objects: ObjectSpec, types: tuple[TypeSpec, ...] = (), **kw: Any) -> HostSchema:
    return HostSchema(
        version="1.0.0",
        permissions=kw.get("permissions", PERMISSIONS),
        types=types,
        objects=objects,
    )


def _chat(**kw: Any) -> ObjectSpec:
    kw.setdefault("path", "chat")
    kw.setdefault("doc", "Chat.")
    kw.setdefault("permission", "chat.read")
    return ObjectSpec(**kw)


def _refused(*objects: ObjectSpec, types: tuple[TypeSpec, ...] = (), **kw: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        _schema(*objects, types=types, **kw)
    return str(caught.value)


GOOD = _schema(
    _chat(
        path="",
        permission=None,
        properties=(prop("mood", "str", "Mood.", changed_by=["ping"]),),
        signals=(signal("ping", "Note", "Ping."),),
        methods=(method("hello", "Hello.", param("name", "str")),),
    ),
    _chat(
        properties=(
            prop("busy", "bool", "Busy.", changed_by=["state"]),
            prop("fixed", "int", "Fixed.", constant=True),
            prop("own", "int", "Own.", constant=True, permission="chat.write"),
            prop("open", "int", "Open.", constant=True, always=True),
        ),
        signals=(
            signal("state", "Note", "State.", wire="chat_state"),
            signal("quiet", "Note", "Quiet.", always=True),
            signal("later", "Note", "Later.", stage=2),
        ),
        methods=(
            method("post", "Post.", param("text", "str"), permission="chat.write"),
            method("peek", "Peek.", result="list[Note]"),
        ),
    ),
    _chat(path="chat.approvals", per_session=True),
    types=(typedef("Note", "A note.", field("text", "str"), field("tag", "str", optional=True)),),
)


# ── Refusals ─────────────────────────────────────────────────────


def test_a_consistent_schema_is_accepted():
    assert GOOD.version == "1.0.0"


def test_a_schema_defining_a_type_or_an_object_twice_is_refused():
    note = typedef("Note", "A note.", field("text", "str"))

    assert "a type is defined twice" in _refused(types=(note, note))
    assert "an object is defined twice" in _refused(_chat(), _chat())


def test_a_permission_that_is_not_declared_is_refused_on_an_object_and_on_a_member():
    assert "chat: permission 'nope' is not declared" in _refused(_chat(permission="nope"))
    text = _refused(
        _chat(
            properties=(prop("a", "int", "A.", constant=True, permission="nope"),),
            signals=(signal("s", "None", "S.", permission="nope2"),),
            methods=(method("m", "M.", permission="nope3"),),
        )
    )
    assert "chat.a: permission 'nope' is not declared" in text
    assert "chat.s: permission 'nope2' is not declared" in text
    assert "chat.m: permission 'nope3' is not declared" in text


def test_a_type_word_that_is_not_a_scalar_or_a_named_type_is_refused_wherever_it_is_written():
    text = _refused(
        _chat(
            properties=(prop("a", "Ghost", "A.", constant=True),),
            signals=(signal("s", "list[Ghost2]", "S."),),
            methods=(method("m", "M.", param("p", "dict[str, Ghost3]"), result="Ghost4 | None"),),
        ),
        types=(typedef("Note", "N.", field("f", "Ghost5")),),
    )

    for word in ("Ghost", "Ghost2", "Ghost3", "Ghost4", "Ghost5"):
        assert f"unknown type '{word}'" in text
    assert "Note.f: unknown type 'Ghost5' in 'Ghost5'" in text
    assert "chat.m(p)" in text


def test_a_nested_expression_is_checked_word_by_word():
    assert "unknown type 'Missing'" in _refused(
        _chat(properties=(prop("a", "list[dict[str, Missing | None]]", "A.", constant=True),))
    )


def test_an_object_path_that_is_not_dotted_lowercase_is_refused():
    for path in ("Chat", "chat.", ".chat", "chat..x", "1chat", "chat-x"):
        assert f"object path {path!r} is not dotted lowercase" in _refused(_chat(path=path))


def test_a_member_or_parameter_name_that_is_not_lowercase_is_refused():
    text = _refused(
        _chat(
            properties=(prop("Bad", "int", "A.", constant=True),),
            methods=(method("m", "M.", param("Worse", "int")),),
        )
    )

    assert "chat.Bad: not a lowercase name" in text
    assert "chat.m(Worse): not a lowercase name" in text


def test_a_name_used_by_a_property_a_signal_and_a_method_of_one_object_is_refused():
    text = _refused(
        _chat(
            properties=(prop("x", "int", "P.", constant=True),),
            signals=(signal("x", "None", "S."),),
            methods=(method("x", "M."),),
        )
    )

    assert text.count("chat.x: defined twice") == 2


def test_a_stage_other_than_one_or_two_is_refused():
    text = _refused(
        _chat(
            properties=(prop("a", "int", "A.", constant=True, stage=3),),
            signals=(signal("s", "None", "S.", stage=0),),
            methods=(method("m", "M.", stage=7),),
        )
    )

    assert text.count("stage must be 1 or 2") == 3


def test_a_property_must_be_constant_or_say_which_signal_changes_it():
    text = _refused(_chat(properties=(prop("a", "int", "A."),)))

    assert "chat.a: every property has a change signal" in text


def test_a_changed_by_must_name_a_signal_of_the_object_or_of_another_dotted():
    own = _chat(
        properties=(prop("a", "int", "A.", changed_by=["s"]),),
        signals=(signal("s", "None", "S."),),
    )
    elsewhere = _chat(
        path="other",
        properties=(prop("b", "int", "B.", changed_by=["chat.s"]),),
    )
    assert _schema(own, elsewhere)

    text = _refused(
        _chat(
            properties=(
                prop("a", "int", "A.", changed_by=["nope"]),
                prop("b", "int", "B.", changed_by=["nowhere.s"]),
                prop("c", "int", "C.", changed_by=["chat.nope"]),
                prop("d", "int", "D.", changed_by=["post"]),
            ),
            methods=(method("post", "Post."),),
        )
    )
    for ref in ("nope", "nowhere.s", "chat.nope", "post"):
        assert f"changed_by {ref!r} is not a signal" in text


def test_two_signals_may_not_share_a_wire_name():
    text = _refused(
        _chat(
            signals=(
                signal("a", "None", "A.", wire="same"),
                signal("b", "None", "B.", wire="same"),
            )
        )
    )

    assert "wire name 'same' also used by chat.a" in text
    text = _refused(
        _chat(signals=(signal("a", "None", "A."),)),
        _chat(path="chat.x", signals=(signal("b", "None", "B.", wire="chat.a"),)),
    )
    assert "wire name 'chat.a' also used by chat.a" in text


def test_an_object_cannot_belong_to_a_window_and_a_session_both():
    assert "belongs to a window or a session, not both" in _refused(
        _chat(per_window=True, per_session=True)
    )


def test_elsewhere_belongs_to_a_session_object_and_names_a_declared_grant():
    text = _refused(_chat(methods=(method("m", "M.", elsewhere="chat.write"),)))
    assert "chat.m: elsewhere, on an object no session owns" in text

    text = _refused(_chat(per_session=True, methods=(method("m", "M.", elsewhere="nope"),)))
    assert "chat.m: elsewhere permission 'nope' is not declared" in text

    assert _schema(_chat(per_session=True, methods=(method("m", "M.", elsewhere="chat.write"),)))


def test_every_problem_is_listed_in_one_error():
    text = _refused(
        _chat(
            permission="nope",
            properties=(prop("a", "Ghost", "A."),),
            methods=(method("m", "M.", stage=9),),
        )
    )

    assert text.count("the host schema is inconsistent:") == 1
    for part in ("permission 'nope'", "unknown type 'Ghost'", "change signal", "stage must be"):
        assert part in text


def test_a_type_name_must_be_pascal_case():
    for name in ("note", "Note_x", "1Note", "Not e"):
        with pytest.raises(ValidationError, match="must be PascalCase"):
            typedef(name, "N.")
    assert typedef("Note2", "N.").name == "Note2"


def test_the_models_reject_unknown_fields_and_cannot_be_changed():
    with pytest.raises(ValidationError):
        FieldSpec(name="a", type="str", surprise=1)  # type: ignore[call-arg]
    spec = field("a", "str")
    with pytest.raises(ValidationError):
        spec.name = "b"  # type: ignore[misc]


# ── Lookups ──────────────────────────────────────────────────────


def test_objects_and_types_are_found_by_path_and_name():
    assert GOOD.object("chat") is not None and GOOD.object("chat").path == "chat"  # type: ignore[union-attr]
    assert GOOD.object("") is not None
    assert GOOD.object("nope") is None
    assert GOOD.type("Note") is not None
    assert GOOD.type("note") is None


def test_a_member_path_resolves_to_its_object_and_member_and_a_bare_one_to_the_root():
    spec, member = GOOD.resolve("chat.post")
    assert (spec.path, member.name) == ("chat", "post")
    spec, member = GOOD.resolve("mood")
    assert (spec.path, member.name) == ("", "mood")
    spec, member = GOOD.resolve("hello")
    assert spec.path == "" and member.name == "hello"


def test_resolving_an_unknown_object_or_member_raises_key_error_naming_the_path():
    for path in ("nope.x", "chat.nope", "nope", "chat.approvals.nope", ""):
        with pytest.raises(KeyError) as caught:
            GOOD.resolve(path)
        assert caught.value.args == (path,)


def test_every_member_is_listed_as_object_dot_member_with_root_members_bare():
    assert GOOD.member_paths() == [
        "mood",
        "ping",
        "hello",
        "chat.busy",
        "chat.fixed",
        "chat.own",
        "chat.open",
        "chat.state",
        "chat.quiet",
        "chat.later",
        "chat.post",
        "chat.peek",
    ]


def test_a_signal_is_found_by_its_wire_and_a_wire_defaults_to_the_path_and_name():
    root = GOOD.object("")
    chat = GOOD.object("chat")
    assert root is not None and chat is not None

    assert GOOD.wire_name(root, root.signals[0]) == "ping"
    assert GOOD.wire_name(chat, chat.signals[0]) == "chat_state"
    assert GOOD.wire_name(chat, chat.signals[1]) == "chat.quiet"
    found = GOOD.signal_by_wire("chat_state")
    assert found is not None and (found[0].path, found[1].name) == ("chat", "state")
    found = GOOD.signal_by_wire("ping")
    assert found is not None and found[0].path == ""
    assert GOOD.signal_by_wire("chat.state") is None
    assert GOOD.signal_by_wire("nope") is None


def test_the_permission_names_and_the_members_still_to_build_are_listed():
    assert GOOD.permission_names() == {"chat.read", "chat.write"}
    assert GOOD.stage_two() == ["chat.later"]
    assert _schema(_chat()).stage_two() == []


def test_an_object_finds_a_property_a_signal_or_a_method_by_name():
    chat = GOOD.object("chat")
    assert chat is not None

    assert chat.member("busy") is chat.properties[0]
    assert chat.member("state") is chat.signals[0]
    assert chat.member("post") is chat.methods[0]
    assert chat.member("nope") is None


def test_a_member_needs_its_own_grant_else_the_objects_and_always_means_none():
    chat = GOOD.object("chat")
    assert chat is not None

    assert chat.permission_of(chat.properties[0]) == "chat.read"
    assert chat.permission_of(chat.properties[2]) == "chat.write"
    assert chat.permission_of(chat.properties[3]) is None
    assert chat.permission_of(chat.signals[1]) is None
    assert chat.permission_of(chat.methods[0]) == "chat.write"
    root = GOOD.object("")
    assert root is not None and root.permission_of(root.methods[0]) is None


def test_the_builders_make_the_specs_they_are_named_for_with_always_as_no_inheritance():
    p = prop("a", "int", "A.", constant=True, always=True, writable=True)
    assert (p.inherit_permission, p.writable, p.constant, p.changed_by) == (False, True, True, ())
    assert prop("a", "int", "A.", changed_by=["x", "y"]).changed_by == ("x", "y")
    assert prop("a", "int", "A.", constant=True).inherit_permission is True

    s = signal("s", "None", "S.", wire="w", permission="chat.read", always=True, stage=2)
    assert (s.wire, s.permission, s.inherit_permission, s.stage) == ("w", "chat.read", False, 2)

    m = method(
        "m", "M.", param("p", "int", optional=True), result="int", always=True, elsewhere="x"
    )
    assert m.inherit_permission is False and m.result == "int" and m.elsewhere == "x"
    assert m.params[0].optional is True and m.params[0].name == "p"
    assert method("m", "M.").result == "None"

    t = typedef("Note", "N.", field("a", "int", "doc", optional=True))
    assert t.fields[0].optional is True and t.fields[0].doc == "doc"


def test_the_schema_is_dumped_as_plain_json():
    dumped = schema_json(GOOD)

    assert dumped["version"] == "1.0.0"
    assert dumped["objects"][1]["signals"][0]["wire"] == "chat_state"
    assert isinstance(dumped["objects"][1]["properties"][0]["changed_by"], list)
    assert HostSchema.model_validate(dumped) == GOOD


def test_an_error_code_is_the_word_the_sdks_branch_on():
    assert {c.name: c.value for c in ErrorCode} == {
        "NOT_GRANTED": "not_granted",
        "NOT_FOUND": "not_found",
        "INVALID": "invalid",
        "READ_ONLY": "read_only",
        "STAGE_2": "stage_2",
        "REFUSED": "refused",
        "UNAVAILABLE": "unavailable",
        "FAILED": "failed",
    }


# ── Type expressions ─────────────────────────────────────────────


def test_a_type_expression_is_a_scalar_a_named_type_a_list_a_dict_or_an_optional():
    assert TypeExpr.parse("int").model_dump() == {"kind": "scalar", "name": "int", "item": None}
    assert TypeExpr.parse("json").kind == "scalar"
    assert TypeExpr.parse("None").kind == "scalar"
    assert TypeExpr.parse("Note").model_dump() == {"kind": "named", "name": "Note", "item": None}
    assert TypeExpr.parse("  Note  ").name == "Note"
    assert TypeExpr.parse("list[str]").item == TypeExpr.parse("str")
    assert TypeExpr.parse("dict[str, Note]").item == TypeExpr.parse("Note")
    assert TypeExpr.parse("dict[str,int]").kind == "dict"
    assert TypeExpr.parse("str | None").kind == "optional"


def test_type_expressions_nest():
    parsed = TypeExpr.parse("list[dict[str, Note | None]] | None")

    assert parsed.kind == "optional"
    assert parsed.item is not None and parsed.item.kind == "list"
    inner = parsed.item.item
    assert inner is not None and inner.kind == "dict"
    assert inner.item is not None and inner.item.kind == "optional"
    assert inner.item.item == TypeExpr.parse("Note")


@pytest.mark.parametrize(
    "bad",
    ["", "note", "Note x", "dict[int, str]", "set[str]", "list[", "list[note]", "str |", "1"],
)
def test_a_malformed_type_expression_is_refused_naming_it(bad: str):
    with pytest.raises(ValueError, match="not a type expression"):
        TypeExpr.parse(bad)


def test_the_words_of_an_expression_leave_out_list_and_dict():
    assert type_words("list[dict[str, Note | None]]") == ["str", "Note", "None"]
    assert type_words("int") == ["int"]
    assert type_words("Note") == ["Note"]
