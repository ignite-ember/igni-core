"""The transport contracts: what a subclass must supply and what is inert.

The composite dispatches on these classes with ``isinstance``; so a
transport that is client-only must not claim to know clients, an
incomplete one must not be built, and the default for telling a watcher
about views is to tell it nothing.
"""

from __future__ import annotations

import pytest

import igni_core.transport as transport_package
from igni_core.transport.base import ClientAwareTransport, ListeningTransport, Transport
from igni_core.transport.in_process import InProcessTransport
from igni_core.transport.unix_socket import (
    UnixSocketClientTransport,
    UnixSocketServerTransport,
)


class _Bare(Transport):
    async def send(self, message):
        pass

    def receive(self):
        raise NotImplementedError

    async def close(self):
        pass

    @property
    def is_closed(self) -> bool:
        return False


class _Aware(_Bare, ClientAwareTransport):
    @property
    def client_count(self) -> int:
        return 0

    def owns_client(self, client_id: str) -> bool:
        return False


def test_a_transport_must_supply_send_receive_close_and_is_closed():
    with pytest.raises(TypeError):
        Transport()  # type: ignore[abstract]
    for missing in ("send", "receive", "close", "is_closed"):
        namespace = {
            name: getattr(_Bare, name)
            for name in ("send", "receive", "close", "is_closed")
            if name != missing
        }
        incomplete = type("Incomplete", (Transport,), namespace)
        with pytest.raises(TypeError, match=missing):
            incomplete()  # type: ignore[abstract]
    assert _Bare().is_closed is False


def test_a_client_aware_transport_must_count_clients_and_say_whom_it_owns():
    for missing in ("client_count", "owns_client"):
        namespace = {
            name: getattr(_Aware, name)
            for name in ("client_count", "owns_client")
            if name != missing
        }
        incomplete = type("Incomplete", (_Bare, ClientAwareTransport), namespace)
        with pytest.raises(TypeError, match=missing):
            incomplete()  # type: ignore[abstract]
    assert _Aware().client_count == 0


def test_a_listening_transport_must_also_start_and_wait_for_a_connection():
    for missing in ("start", "wait_for_connection"):
        namespace = {
            "client_count": _Aware.client_count,
            "owns_client": _Aware.owns_client,
            "start": lambda self: None,
            "wait_for_connection": lambda self, timeout=30.0: None,
        }
        del namespace[missing]
        incomplete = type("Incomplete", (_Bare, ListeningTransport), namespace)
        with pytest.raises(TypeError, match=missing):
            incomplete()  # type: ignore[abstract]
    assert issubclass(ListeningTransport, ClientAwareTransport)


def test_the_default_for_watching_connections_tells_the_watcher_nothing():
    told: list[tuple[str, str]] = []
    transport = _Aware()
    assert (
        transport.watch_connections(lambda event, client_id: told.append((event, client_id)))
        is None
    )
    assert told == []


def test_only_the_transports_that_serve_connections_are_client_aware(tmp_path):
    assert not isinstance(InProcessTransport.create_pair()[0], ClientAwareTransport)
    assert not isinstance(UnixSocketClientTransport(tmp_path / "s"), ClientAwareTransport)
    assert isinstance(UnixSocketServerTransport(tmp_path / "s"), ListeningTransport)


def test_the_package_offers_the_transport_and_the_in_process_pair():
    assert transport_package.__all__ == ["Transport", "InProcessTransport"]
    assert transport_package.Transport is Transport
    assert transport_package.InProcessTransport is InProcessTransport
