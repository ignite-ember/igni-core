"""``MemberTable``: every built member, and the one a request names.

A member built twice is a bug refused at build time; an answer outside its
schema type fails loudly in strict mode (tests) and is sent as given
otherwise (production); a ``set`` has no answer to check.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.member_table import MemberTable
from igni_core.extensions.host.members.call import Arguments, Call
from igni_core.extensions.host.members.tag import MemberBinding, MemberKey
from igni_core.extensions.host.request_binder import BoundRequest
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.wire import Op, Request
from tests.host_model_helpers import manifest

TYPES = SchemaTypes(HOST_SCHEMA)


class Built:
    """A built members object: the bindings its tagged methods make."""

    def __init__(self, *bindings: tuple[Op, str, Any]) -> None:
        self._bindings = [
            MemberBinding(key=MemberKey(op=op, path=path), impl=impl) for op, path, impl in bindings
        ]

    def table(self) -> list[MemberBinding]:
        return self._bindings


def answering(value: Any) -> Any:
    async def impl(call: Call) -> Any:
        return value

    return impl


def bound(request: Request) -> BoundRequest:
    spec, member = HOST_SCHEMA.resolve(request.path)
    call = Call(
        extension_id="acme.reviewer",
        grants=HostGrants(manifest("acme.reviewer")),
        window=None,
        target=SessionTarget(session_id="s1", on_screen=True),
        arguments=Arguments(),
    )
    return BoundRequest(spec=spec, member=member, call=call)


def request(op: Op, path: str) -> Request:
    return Request(id=1, op=op, path=path)


def table(*bindings: tuple[Op, str, Any], strict: bool) -> MemberTable:
    return MemberTable.of([Built(*bindings)], TYPES, strict)  # type: ignore[list-item]


class TestBuilding:
    def test_every_members_bindings_are_one_table(self) -> None:
        built = MemberTable.of(
            [  # type: ignore[list-item]
                Built((Op.CALL, "ui.back", answering([]))),
                Built(
                    (Op.GET, "ui.route", answering([])), (Op.CALL, "chat.cancel", answering(None))
                ),
            ],
            TYPES,
            True,
        )
        assert built.implemented() == {
            MemberKey(op=Op.CALL, path="ui.back"),
            MemberKey(op=Op.GET, path="ui.route"),
            MemberKey(op=Op.CALL, path="chat.cancel"),
        }

    def test_the_same_member_in_two_classes_is_refused_naming_it(self) -> None:
        with pytest.raises(RuntimeError, match=r"host member \(call, ui.back\) implemented twice"):
            MemberTable.of(
                [
                    Built((Op.CALL, "ui.back", answering([]))),
                    Built((Op.CALL, "ui.back", answering([]))),
                ],  # type: ignore[list-item]
                TYPES,
                False,
            )

    def test_the_same_path_under_another_op_is_another_member(self) -> None:
        built = table(
            (Op.GET, "ui.route", answering([])), (Op.SET, "ui.route", answering(None)), strict=False
        )
        assert len(built.implemented()) == 2


class TestRunning:
    async def test_the_answer_of_the_member_the_request_names_is_returned(self) -> None:
        built = table(
            (Op.CALL, "ui.back", answering([])),
            (Op.CALL, "chat.cancel", answering(None)),
            strict=True,
        )
        req = request(Op.CALL, "ui.back")
        assert await built.run(req, bound(req)) == []

    async def test_a_member_is_handed_the_call(self) -> None:
        seen: list[Call] = []

        async def impl(call: Call) -> Any:
            seen.append(call)
            return []

        built = table((Op.CALL, "ui.back", impl), strict=True)
        req = request(Op.CALL, "ui.back")
        context = bound(req)
        await built.run(req, context)
        assert seen == [context.call]

    async def test_strict_refuses_a_method_answer_outside_its_result_type(self) -> None:
        built = table((Op.CALL, "ui.back", answering("not a list")), strict=True)
        req = request(Op.CALL, "ui.back")
        with pytest.raises(AssertionError, match="ui.back answered outside its schema type"):
            await built.run(req, bound(req))

    async def test_strict_refuses_a_property_answer_outside_its_type(self) -> None:
        built = table((Op.GET, "ui.route", answering({"no": "list"})), strict=True)
        req = request(Op.GET, "ui.route")
        with pytest.raises(AssertionError, match="ui.route answered outside its schema type"):
            await built.run(req, bound(req))

    async def test_not_strict_sends_the_answer_as_the_backend_gave_it(self) -> None:
        built = table((Op.CALL, "ui.back", answering("not a list")), strict=False)
        req = request(Op.CALL, "ui.back")
        assert await built.run(req, bound(req)) == "not a list"

    async def test_a_set_has_no_answer_to_check(self) -> None:
        built = table((Op.SET, "ui.route", answering("anything")), strict=True)
        req = request(Op.SET, "ui.route")
        assert await built.run(req, bound(req)) == "anything"

    async def test_a_failure_of_the_member_is_not_hidden(self) -> None:
        async def impl(call: Call) -> Any:
            raise ValueError("it broke")

        built = table((Op.CALL, "ui.back", impl), strict=True)
        req = request(Op.CALL, "ui.back")
        with pytest.raises(ValueError, match="it broke"):
            await built.run(req, bound(req))
