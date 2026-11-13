"""A broadcast waits for its wedged clients together, not one after another.

Several webviews the OS suspended must cost one timeout, not one each:
the backend's emit loop stalls for the whole broadcast.
"""

from __future__ import annotations

import asyncio
import json
import time

from websockets.asyncio.client import connect

from igni_core.protocol import messages as msg
from igni_core.transport.websocket import WebSocketServerTransport


async def test_three_wedged_clients_cost_one_timeout_and_all_are_evicted():
    timeout = 0.4
    tr = WebSocketServerTransport(port=0, broadcast_send_timeout=timeout)
    await tr.start()
    sockets = []
    try:
        for _ in range(3):
            ws = await connect(f"ws://127.0.0.1:{tr.port}")
            await asyncio.wait_for(ws.recv(), 5)
            sockets.append(ws)
        healthy = await connect(f"ws://127.0.0.1:{tr.port}")
        await asyncio.wait_for(healthy.recv(), 5)
        wedged_ids = list(tr._conns)[:3]

        async def parked(_payload):
            await asyncio.Event().wait()

        for client_id in wedged_ids:
            tr._conns[client_id].conn.send = parked

        started = time.monotonic()
        await tr.send(msg.Info(text="hello"))
        elapsed = time.monotonic() - started

        # Serial waits would take three timeouts (1.2 s).
        assert elapsed < 2 * timeout, f"wedged clients were waited for in turn: {elapsed:.2f}s"
        assert tr.client_count == 1
        assert json.loads(await asyncio.wait_for(healthy.recv(), 5))["text"] == "hello"
        await healthy.close()
        for ws in sockets:
            await ws.close()
    finally:
        await tr.close()
