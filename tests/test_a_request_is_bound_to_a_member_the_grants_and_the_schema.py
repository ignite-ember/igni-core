"""``RequestBinder``: everything between "an extension asked" and "a member runs".

The member exists and is built, the grant allows it, the op fits what it
is, the window and session are resolved, and the parameters or the value
are of the schema's types. A small schema of its own, so every kind of
member and every refusal is reached.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.caller import Caller
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.request_binder import BoundRequest, RequestBinder
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import (
    ErrorCode,
    HostSchema,
    ObjectSpec,
    PermissionSpec,
    method,
    param,
    prop,
    signal,
)
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.wire import Op, Request
from tests.host_model_helpers import manifest

BOX = ObjectSpec(
    path="box",
    doc="",
    permission="boxes",
    per_session=True,
    properties=(
        prop("count", "int", "", constant=True),
        prop("label", "str", "", writable=True, changed_by=["ping"]),
        prop("later", "int", "", stage=2, constant=True),
    ),
    signals=(signal("ping", "str", ""),),
    methods=(
        method("add", "", param("amount", "int"), param("note", "str", optional=True)),
        method("reset", ""),
        method("name", "", param("first", "str"), param("last", "str"), param("title", "str")),
        method("soon", "", stage=2),
        method("secret", "", permission="boxes.secret"),
    ),
)
OWN = ObjectSpec(
    path="own",
    doc="",
    permission=None,
    properties=(prop("note", "str", "", writable=True, constant=True),),
)
SCHEMA = HostSchema(
    version="1.0.0",
    permissions=(
        PermissionSpec(name="boxes", doc=""),
        PermissionSpec(name="boxes.secret", doc=""),
    ),
    types=(),
    objects=(BOX, OWN),
)


class Scope:
    """What the binder delegates the window and the session to."""

    def __init__(self) -> None:
        self.windows: list[tuple[str, str | None, str | None]] = []
        self.sessions: list[dict[str, Any]] = []

    def window(self, spec: ObjectSpec, request: Request, home: str | None) -> str | None:
        self.windows.append((spec.path, request.window, home))
        return "w-resolved"

    async def session(self, **kwargs: Any) -> SessionTarget:
        self.sessions.append(kwargs)
        return SessionTarget(session_id="s-resolved", on_screen=True)


class Rig:
    def __init__(self) -> None:
        self.scope = Scope()
        self.binder = RequestBinder(SCHEMA, SchemaTypes(SCHEMA), self.scope)  # type: ignore[arg-type]

    async def bind(
        self,
        path: str,
        op: Op = Op.GET,
        *,
        home: str | None = None,
        host: tuple[str, ...] = ("boxes",),
        **fields: Any,
    ) -> BoundRequest:
        grants = HostGrants(manifest("acme.one", host=list(host)))
        request = Request(id=1, op=op, path=path, **fields)
        return await self.binder.bind(
            Caller(extension_id="acme.one", home_window=home), grants, request
        )


async def refused(rig: Rig, path: str, op: Op = Op.GET, **kwargs: Any) -> HostModelError:
    with pytest.raises(HostModelError) as caught:
        await rig.bind(path, op, **kwargs)
    return caught.value


# ── Finding the member ─────────────────────────────────────────────


@pytest.mark.parametrize("path", ["box.nothing", "nothing.count", "count", ""])
async def test_a_path_that_is_no_member_is_not_found(path: str) -> None:
    error = await refused(Rig(), path)

    assert error.code is ErrorCode.NOT_FOUND
    assert error.message == f"the host model has no member '{path}'"


@pytest.mark.parametrize("path", ["box.later", "box.soon"])
async def test_a_member_that_is_defined_but_not_built_is_refused_as_stage_two(path: str) -> None:
    error = await refused(Rig(), path, Op.CALL if path.endswith("soon") else Op.GET)

    assert error.code is ErrorCode.STAGE_2
    assert path in error.message


async def test_a_member_the_extension_is_not_granted_is_refused() -> None:
    error = await refused(Rig(), "box.count", host=())

    assert error.code is ErrorCode.NOT_GRANTED
    assert "'acme.one' is not granted 'boxes', which 'box.count' needs" in error.message


async def test_a_member_with_a_permission_of_its_own_needs_that_one() -> None:
    error = await refused(Rig(), "box.secret", Op.CALL, host=("boxes",))

    assert error.code is ErrorCode.NOT_GRANTED
    assert "'boxes.secret'" in error.message
    assert (await Rig().bind("box.secret", Op.CALL, host=("boxes.secret",))).member.name == "secret"


async def test_the_grant_is_checked_before_the_window_and_session_are_resolved() -> None:
    rig = Rig()

    await refused(rig, "box.count", host=())

    assert rig.scope.windows == [] and rig.scope.sessions == []


async def test_an_object_every_extension_has_needs_no_grant() -> None:
    bound = await Rig().bind("own.note", Op.GET, host=())

    assert bound.spec.path == "own" and bound.member.name == "note"


# ── The scope ──────────────────────────────────────────────────────


async def test_the_window_and_session_come_from_the_scope_and_ride_on_the_call() -> None:
    rig = Rig()

    bound = await rig.bind("box.count", Op.GET, home="w-home", window="w-asked", session="s-asked")

    assert rig.scope.windows == [("box", "w-asked", "w-home")]
    (asked,) = rig.scope.sessions
    assert asked["window"] == "w-resolved"
    assert asked["spec"] is BOX and asked["member"].name == "count"
    assert asked["request"].session == "s-asked"
    assert asked["grants"].extension_id == "acme.one"
    call = bound.call
    assert call.extension_id == "acme.one"
    assert call.window == "w-resolved"
    assert call.target == SessionTarget(session_id="s-resolved", on_screen=True)
    assert bound.spec is BOX


# ── Properties ─────────────────────────────────────────────────────


async def test_a_get_or_subscribe_of_a_property_carries_no_value() -> None:
    for op in (Op.GET, Op.SUBSCRIBE):
        bound = await Rig().bind("box.count", op, value=5)

        assert bound.call.arguments.value is None and bound.call.arguments.params == {}


async def test_a_set_of_a_writable_property_carries_its_validated_value() -> None:
    bound = await Rig().bind("box.label", Op.SET, value="hello")

    assert bound.call.value == "hello"


async def test_a_set_of_a_read_only_property_is_refused() -> None:
    error = await refused(Rig(), "box.count", Op.SET, value=3)

    assert error.code is ErrorCode.READ_ONLY
    assert error.message == "'box.count' cannot be set"


async def test_a_set_with_a_value_of_the_wrong_type_is_invalid_and_says_the_type() -> None:
    error = await refused(Rig(), "box.label", Op.SET, value=12)

    assert error.code is ErrorCode.INVALID
    assert error.message.startswith("box.label =: expected str: ")


async def test_a_property_cannot_be_called() -> None:
    error = await refused(Rig(), "box.count", Op.CALL)

    assert error.code is ErrorCode.INVALID
    assert error.message == "'box.count' is a property: get or set it"


# ── Methods ────────────────────────────────────────────────────────


@pytest.mark.parametrize("op", [Op.GET, Op.SET, Op.SUBSCRIBE])
async def test_a_method_is_only_called(op: Op) -> None:
    error = await refused(Rig(), "box.reset", op)

    assert error.code is ErrorCode.INVALID
    assert error.message == "'box.reset' is a method: call it"


async def test_a_method_with_no_parameters_is_called_with_none() -> None:
    assert (await Rig().bind("box.reset", Op.CALL)).call.params == {}


async def test_parameters_are_validated_and_an_optional_one_left_out_is_none() -> None:
    bound = await Rig().bind("box.add", Op.CALL, params={"amount": 3})

    assert bound.call.params == {"amount": 3, "note": None}


async def test_an_optional_parameter_given_null_is_none_and_one_given_is_kept() -> None:
    nulled = await Rig().bind("box.add", Op.CALL, params={"amount": 3, "note": None})
    noted = await Rig().bind("box.add", Op.CALL, params={"amount": 3, "note": "hi"})

    assert nulled.call.params["note"] is None
    assert noted.call.params["note"] == "hi"


@pytest.mark.parametrize("params", [{}, {"amount": None}, {"note": "x"}])
async def test_a_required_parameter_that_is_absent_or_null_is_invalid(
    params: dict[str, Any],
) -> None:
    error = await refused(Rig(), "box.add", Op.CALL, params=params)

    assert error.code is ErrorCode.INVALID
    assert error.message == "box.add: 'amount' is required"


async def test_the_first_missing_required_parameter_is_the_one_named() -> None:
    error = await refused(Rig(), "box.name", Op.CALL, params={"title": "x"})

    assert error.message == "box.name: 'first' is required"


async def test_unknown_parameters_are_named_sorted() -> None:
    names = ["zed", "alpha", "mike", "bravo", "x", "kilo", "echo", "tango"]

    error = await refused(
        Rig(), "box.add", Op.CALL, params={"amount": 1, **dict.fromkeys(names, 1)}
    )

    assert error.code is ErrorCode.INVALID
    assert error.message == f"box.add takes no {', '.join(sorted(names))}"


async def test_unknown_parameters_are_refused_before_a_missing_one() -> None:
    error = await refused(Rig(), "box.add", Op.CALL, params={"stray": 1})

    assert error.message == "box.add takes no stray"


async def test_a_parameter_of_the_wrong_type_is_invalid_and_says_which() -> None:
    error = await refused(Rig(), "box.add", Op.CALL, params={"amount": "many"})

    assert error.code is ErrorCode.INVALID
    assert error.message.startswith("box.add(amount): expected int: ")


# ── Signals ────────────────────────────────────────────────────────


@pytest.mark.parametrize("op", [Op.GET, Op.SET, Op.CALL, Op.SUBSCRIBE])
async def test_a_signal_is_not_a_member_to_ask_of(op: Op) -> None:
    error = await refused(Rig(), "box.ping", op)

    assert error.code is ErrorCode.INVALID
    assert error.message == "'box.ping' is a signal: subscribe to it with its wire name"
