"""The relay counter says whether a question pushed now would reach a person."""

from __future__ import annotations

from igni_core.core.hitl.relay import RelayCounter


def test_no_relay_is_open_at_first():
    assert RelayCounter().has_relay is False


def test_a_relay_stays_open_until_the_last_forwarder_closes():
    relays = RelayCounter()

    relays.open()
    relays.open()
    relays.close()
    assert relays.has_relay is True

    relays.close()
    assert relays.has_relay is False


def test_an_unbalanced_close_does_not_owe_a_relay_to_the_next_open():
    relays = RelayCounter()

    relays.close()
    relays.close()
    relays.open()

    assert relays.has_relay is True
    relays.close()
    assert relays.has_relay is False
