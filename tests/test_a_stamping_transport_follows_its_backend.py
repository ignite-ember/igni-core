"""The transport that stamps a session's id on what its backend sends.

The stamp is the backend's id *now*: a session renamed by ``/clear``
stamps the new id on what it sends from then on. A message that already
names a session keeps it, a backend without an id stamps nothing, and
any other attribute is the inner transport's.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.session_stamping_transport import SessionStampingTransport
from igni_core.protocol import messages as msg


class _Inner:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.port = 4242

    async def send(self, message: Any) -> None:
        self.sent.append(message)


async def test_an_unstamped_message_gets_the_backend_id_and_the_original_is_untouched() -> None:
    inner = _Inner()
    original = msg.Info(text="hello")

    await SessionStampingTransport(inner, SimpleNamespace(session_id="A")).send(original)

    assert inner.sent[0].session_id == "A"
    assert original.session_id == ""


async def test_a_backend_without_an_id_stamps_nothing() -> None:
    inner = _Inner()
    original = msg.Info(text="hello")

    await SessionStampingTransport(inner, SimpleNamespace(session_id="")).send(original)

    assert inner.sent == [original]
    assert inner.sent[0].session_id == ""


async def test_the_stamp_follows_a_rename_after_construction() -> None:
    inner = _Inner()
    backend = SimpleNamespace(session_id="old")
    transport = SessionStampingTransport(inner, backend)

    await transport.send(msg.Info(text="one"))
    backend.session_id = "renewed"
    await transport.send(msg.Info(text="two"))

    assert [m.session_id for m in inner.sent] == ["old", "renewed"]


async def test_a_message_naming_another_session_is_forwarded_as_it_is() -> None:
    inner = _Inner()
    relayed = msg.Info(text="hello", session_id="B")

    await SessionStampingTransport(inner, SimpleNamespace(session_id="A")).send(relayed)

    assert inner.sent == [relayed]


def test_an_attribute_the_inner_transport_lacks_raises_attribute_error() -> None:
    transport = SessionStampingTransport(_Inner(), SimpleNamespace(session_id="A"))

    assert transport.port == 4242
    with pytest.raises(AttributeError):
        transport.not_a_thing  # noqa: B018


async def test_a_failing_inner_send_reaches_the_caller() -> None:
    class _Broken:
        async def send(self, message: Any) -> None:
            raise ConnectionError("gone")

    with pytest.raises(ConnectionError):
        await SessionStampingTransport(_Broken(), SimpleNamespace(session_id="A")).send(
            msg.Info(text="x")
        )
