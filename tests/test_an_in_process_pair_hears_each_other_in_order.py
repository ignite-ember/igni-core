"""The in-process pair: what one side sends the other receives, in order.

Closing is told to the peer once, ends the peer's receive, and a side
that has closed itself receives nothing more.
"""

from __future__ import annotations

import asyncio

from igni_core.protocol import messages as msg
from igni_core.transport.in_process import InProcessTransport


async def _collect(transport: InProcessTransport) -> list[msg.Message]:
    async def run() -> list[msg.Message]:
        return [m async for m in transport.receive()]

    return await asyncio.wait_for(run(), 5)


async def test_both_directions_arrive_in_order_and_end_with_the_peer_s_close():
    fe, be = InProcessTransport.create_pair()
    for i in range(5):
        await fe.send(msg.UserMessage(text=f"fe {i}", id=f"f{i}"))
        await be.send(msg.Info(text=f"be {i}"))
    await fe.close()
    assert [m.text for m in await _collect(be)] == [f"fe {i}" for i in range(5)]
    await be.close()
    assert [m.text for m in await _collect(fe_peer(fe))] == [f"be {i}" for i in range(5)]


def fe_peer(fe: InProcessTransport) -> InProcessTransport:
    return InProcessTransport(send_queue=asyncio.Queue(), recv_queue=fe._recv_q)


async def test_closing_twice_tells_the_peer_once():
    fe, be = InProcessTransport.create_pair()
    await fe.close()
    await fe.close()
    assert fe.is_closed
    assert await _collect(be) == []
    # A second sentinel would still be queued for the next reader.
    assert be._recv_q.empty()


async def test_a_side_that_has_closed_receives_nothing_even_what_is_queued():
    fe, be = InProcessTransport.create_pair()
    await fe.send(msg.Info(text="queued"))
    await be.close()
    assert await _collect(be) == []


async def test_a_receive_that_is_waiting_ends_when_the_peer_closes():
    fe, be = InProcessTransport.create_pair()
    waiting = asyncio.ensure_future(_collect(be))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await fe.send(msg.Info(text="one"))
    await asyncio.sleep(0.05)
    await fe.close()
    assert [m.text for m in await asyncio.wait_for(waiting, 5)] == ["one"]


async def test_two_pairs_do_not_hear_each_other():
    fe1, be1 = InProcessTransport.create_pair()
    fe2, be2 = InProcessTransport.create_pair()
    await fe1.send(msg.Info(text="first pair"))
    await fe2.send(msg.Info(text="second pair"))
    await fe1.close()
    await fe2.close()
    assert [m.text for m in await _collect(be1)] == ["first pair"]
    assert [m.text for m in await _collect(be2)] == ["second pair"]
