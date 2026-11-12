"""What every RPC handler stands on: the context it is built with and the
table of methods it publishes.

The context is a frozen bundle (a handler cannot swap a collaborator
under the others), its optional collaborators are ``None`` when a
headless deployment or a test builds none of them, and the controller
bag is read from the backend each time, because the backend replaces it
when a session is rebuilt. ``methods()`` lists exactly the tagged
methods, inherited ones included. ``rpc_handlers`` exports each handler
class it names.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend import rpc_handlers
from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler, RpcHandlerContext
from igni_core.protocol.rpc import RpcMethod, RpcScope


def _context(**more: object) -> RpcHandlerContext:
    parts: dict[str, object] = {
        "backend": MagicMock(),
        "login": MagicMock(),
        "push": MagicMock(),
        "file_completion": MagicMock(),
        "shell_runner": MagicMock(),
        "workflows": MagicMock(),
    }
    return RpcHandlerContext(**(parts | more))  # type: ignore[arg-type]


def test_the_optional_collaborators_are_absent_unless_given() -> None:
    ctx = _context()

    assert ctx.extensions is None
    assert ctx.origins is None
    assert ctx.extension_store is None
    assert ctx.notifications is None


def test_the_optional_collaborators_are_kept_when_given() -> None:
    center = MagicMock()
    ctx = _context(notifications=center)

    assert ctx.notifications is center


def test_a_context_is_refused_a_change_after_it_was_built() -> None:
    ctx = _context()

    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.backend = MagicMock()  # type: ignore[misc]


def test_a_context_missing_a_required_collaborator_is_refused() -> None:
    with pytest.raises(TypeError, match="workflows"):
        RpcHandlerContext(  # type: ignore[call-arg]
            backend=MagicMock(),
            login=MagicMock(),
            push=MagicMock(),
            file_completion=MagicMock(),
            shell_runner=MagicMock(),
        )


def test_a_context_takes_its_collaborators_by_keyword_only() -> None:
    with pytest.raises(TypeError):
        RpcHandlerContext(*[MagicMock()] * 6)  # type: ignore[misc]


def test_a_handler_keeps_the_context_it_was_given() -> None:
    ctx = _context()

    assert RpcHandler(ctx)._ctx is ctx


def test_the_controllers_are_read_from_the_backend_each_time() -> None:
    backend = SimpleNamespace(controllers="first bag")
    handler = RpcHandler(_context(backend=backend))
    assert handler._controllers == "first bag"

    backend.controllers = "bag after a rebuild"

    assert handler._controllers == "bag after a rebuild"


def test_a_handler_with_no_tagged_methods_publishes_none() -> None:
    class Bare(RpcHandler):
        def helper(self) -> int:
            return 1

    assert Bare(_context()).methods() == {}


def test_a_handler_publishes_its_tagged_methods_and_those_it_inherits() -> None:
    class Parent(RpcHandler):
        @rpc(RpcMethod.TOGGLE_VERBOSE, args=NoArgs, scope=RpcScope.SESSION)
        def verbose(self, args: NoArgs) -> str:
            return "parent"

        def untagged(self, args: NoArgs) -> str:
            return "never published"

    class Child(Parent):
        @rpc(RpcMethod.LOOP_PAUSE, args=NoArgs, scope=RpcScope.SESSION)
        def pause(self, args: NoArgs) -> str:
            return "child"

    table = Child(_context()).methods()

    assert set(table) == {RpcMethod.TOGGLE_VERBOSE, RpcMethod.LOOP_PAUSE}
    assert table[RpcMethod.TOGGLE_VERBOSE]({}) == "parent"
    assert table[RpcMethod.LOOP_PAUSE]({}) == "child"


def test_a_published_method_is_bound_to_the_instance_it_was_asked_of() -> None:
    class Counting(RpcHandler):
        @rpc(RpcMethod.TOGGLE_VERBOSE, args=NoArgs, scope=RpcScope.SESSION)
        def whose(self, args: NoArgs) -> int:
            return id(self)

    one, two = Counting(_context()), Counting(_context())

    assert one.methods()[RpcMethod.TOGGLE_VERBOSE]({}) == id(one)
    assert two.methods()[RpcMethod.TOGGLE_VERBOSE]({}) == id(two)


def test_every_exported_name_is_defined_by_the_package() -> None:
    for name in rpc_handlers.__all__:
        assert hasattr(rpc_handlers, name), name


def test_every_handler_class_is_exported() -> None:
    handlers = {
        name
        for name in dir(rpc_handlers)
        if name.endswith("RpcHandler") and isinstance(getattr(rpc_handlers, name), type)
    }

    assert handlers <= set(rpc_handlers.__all__)
    assert {"AuthRpcHandler", "ModelsRpcHandler", "NotificationsRpcHandler"} <= handlers
