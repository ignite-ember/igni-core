"""A call into an extension's MCP service: when it answers, when it does not, and who is blamed.

``McpExtensionService.call`` resolves the client per call, holds a busy
lease while the tool runs, and turns every way it can go wrong into an
:class:`ExtensionServiceError`. ``unanswered`` is what separates a service
that said nothing (it counts toward the extension being faulty) from a
tool that answered with an error of its own. The manager and the client
are played by small stand-ins: the seam :class:`McpExtensionService` takes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.extensions import service as service_module
from igni_core.extensions.service import ExtensionServiceError, McpExtensionService


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, content: list[Any], structured: Any = None, error: bool = False) -> None:
        self.content = content
        self.structured_content = structured
        self.is_error = error


class _Session:
    def __init__(self, behave: Any) -> None:
        self._behave = behave
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.held_during: list[list[tuple[BusyKind, str, str]]] = []

    async def call_tool(self, method: str, args: dict[str, Any]) -> Any:
        self.calls.append((method, args))
        self.held_during.append(
            [(lease.kind, lease.holder, lease.reason) for lease in ActivityRegistry.shared().busy()]
        )
        outcome = self._behave(method, args)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome


class _Client:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def get_session_for_run(self) -> _Session:
        return self._session


class _Manager:
    def __init__(self, client: Any | None) -> None:
        self._client = client
        self.asked: list[str] = []

    def get_client(self, name: str) -> Any:
        self.asked.append(name)
        return self._client


def _service(behave: Any = None) -> tuple[McpExtensionService, _Session, _Manager]:
    session = _Session(behave or (lambda m, a: _Result([_Block('{"ok": true}')])))
    manager = _Manager(_Client(session))
    return McpExtensionService(manager=manager, server_name="acme.scanner"), session, manager  # type: ignore[arg-type]


def test_the_service_names_the_server_it_is_bound_to() -> None:
    assert _service()[0].server_name == "acme.scanner"


async def test_a_call_runs_the_tool_and_hands_back_its_unwrapped_answer() -> None:
    service, session, manager = _service()

    assert await service.call("list", {"limit": 2}) == {"ok": True}
    assert session.calls == [("list", {"limit": 2})]
    assert manager.asked == ["acme.scanner"]


async def test_the_client_is_resolved_on_every_call_so_a_reconnect_is_picked_up() -> None:
    service, _, manager = _service()

    await service.call("a", {})
    await service.call("b", {})
    assert manager.asked == ["acme.scanner", "acme.scanner"]


async def test_no_connected_client_is_an_unanswered_error_that_says_why() -> None:
    service = McpExtensionService(manager=_Manager(None), server_name="acme.scanner")  # type: ignore[arg-type]

    with pytest.raises(ExtensionServiceError, match="'acme.scanner' is not connected") as raised:
        await service.call("list", {})
    assert raised.value.unanswered is True
    assert "starting, or it failed to start" in str(raised.value)


async def test_the_call_holds_an_extension_call_lease_and_lets_it_go_after_success() -> None:
    service, session, _ = _service()

    await service.call("list", {})

    assert session.held_during == [[(BusyKind.EXTENSION_CALL, "acme.scanner", "list")]]
    assert ActivityRegistry.shared().busy() == []


async def test_the_lease_is_let_go_when_the_tool_fails() -> None:
    def boom(method: str, args: dict[str, Any]) -> Any:
        raise RuntimeError("socket closed")

    service, _, _ = _service(boom)
    with pytest.raises(ExtensionServiceError):
        await service.call("list", {})
    assert ActivityRegistry.shared().busy() == []


async def test_a_call_that_outlasts_the_bound_is_unanswered_and_names_what_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module, "CALL_TIMEOUT_S", 0.05)

    async def never(method: str, args: dict[str, Any]) -> Any:
        await asyncio.sleep(5)

    service, _, _ = _service(never)

    with pytest.raises(ExtensionServiceError) as raised:
        await service.call("scan", {})

    assert raised.value.unanswered is True
    assert str(raised.value) == "'scan' on 'acme.scanner' did not answer within 0.05s"
    assert ActivityRegistry.shared().busy() == [], "the lease is let go on a timeout too"


async def test_a_session_that_raises_is_unanswered_and_carries_the_cause() -> None:
    def boom(method: str, args: dict[str, Any]) -> Any:
        raise ConnectionResetError("peer reset")

    service, _, _ = _service(boom)

    with pytest.raises(ExtensionServiceError) as raised:
        await service.call("scan", {})

    assert raised.value.unanswered is True
    assert str(raised.value) == "'scan' on 'acme.scanner' failed: peer reset"
    assert isinstance(raised.value.__cause__, ConnectionResetError)


async def test_a_tool_that_reports_an_error_answered_so_it_is_not_unanswered() -> None:
    service, _, _ = _service(lambda m, a: _Result([_Block("no such volume")], error=True))

    with pytest.raises(ExtensionServiceError) as raised:
        await service.call("scan", {})

    assert raised.value.unanswered is False
    assert str(raised.value) == "'scan' reported: no such volume"


async def test_a_tool_error_with_no_message_is_still_reported_as_the_tools_own() -> None:
    """It answered, so it is not blamed as silent, and the author is not told the UI cannot use it."""
    service, _, _ = _service(lambda m, a: _Result([], error=True))

    with pytest.raises(ExtensionServiceError) as raised:
        await service.call("scan", {})

    assert raised.value.unanswered is False
    assert str(raised.value) == "'scan' reported: an error with no message"


async def test_a_result_with_no_error_flag_at_all_is_an_answer() -> None:
    class Bare:
        content = [_Block("fine")]

    service, _, _ = _service(lambda m, a: Bare())

    assert await service.call("scan", {}) == "fine"


def test_an_error_is_answered_unless_it_says_otherwise() -> None:
    assert ExtensionServiceError("x").unanswered is False
    assert ExtensionServiceError("x", unanswered=True).unanswered is True


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_Result([_Block("a"), _Block("b")]), "a\nb"),
        (_Result([_Block("[1,"), _Block("2]")]), [1, 2]),
        (_Result([_Block("null")]), None),
        (_Result([_Block("7")]), 7),
        (_Result([], structured={}), {}),
        (_Result([], structured=0), 0),
    ],
)
def test_unwrap_edges(result: _Result, expected: Any) -> None:
    assert McpExtensionService.unwrap(result) == expected


def test_an_image_only_answer_is_not_a_value() -> None:
    image = type("Image", (), {"type": "image"})()
    with pytest.raises(ExtensionServiceError, match="no text content"):
        McpExtensionService.unwrap(_Result([image]))
