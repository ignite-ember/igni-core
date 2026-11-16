"""A run cut off because igni stopped is an interrupted run, reason ``abandoned``.

rpc-contract ``get_interrupted_runs`` lists runs "cancelled, errored, or
abandoned when igni stopped while it ran", and ``InterruptedReason`` has
``abandoned``. Nothing ever wrote it: a crash leaves the message ``pending``
with no stamp, so ``get_interrupted_runs`` never offered it to retry or
discard (campaign R181). A pending message that arrived before this backend
started cannot be running here: it is stamped ``abandoned`` when asked.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from igni_core.backend.server_context import ContextController
from igni_core.core.session.pending_messages import PendingMessageStore

SESSION = "s1"


def _controller(store: PendingMessageStore) -> ContextController:
    return ContextController(
        session=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        pending_store=store,
        models=SimpleNamespace(),  # type: ignore[arg-type]
        pack_meta=SimpleNamespace(),  # type: ignore[arg-type]
    )


async def test_a_message_from_before_the_backend_started_is_abandoned(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "pending.db")
    crashed = await store.arecord_received(SESSION, "a long prompt")
    time.sleep(1.1)  # received_at has a one-second grain
    controller = _controller(store)
    running = await store.arecord_received(SESSION, "sent to this backend")

    runs = await controller.get_interrupted_runs(SESSION)

    assert [(r.message_id, r.reason) for r in runs] == [(crashed, "abandoned")]
    assert running not in {r.message_id for r in runs}
    result = await controller.discard_interrupted_run(SESSION, crashed)
    assert result.ok
    assert await controller.get_interrupted_runs(SESSION) == []


async def test_a_cancelled_run_keeps_its_own_reason(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "pending.db")
    cancelled = await store.arecord_received(SESSION, "x")
    await store.amark_interrupted(cancelled, "cancelled")
    time.sleep(1.1)

    runs = await _controller(store).get_interrupted_runs(SESSION)

    assert [(r.message_id, r.reason) for r in runs] == [(cancelled, "cancelled")]
