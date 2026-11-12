"""The edges of the notification RPCs: nothing to ask, too many rows,
and what "no ids" means.

Without a notification center (a headless backend) every call answers
empty rather than failing. A list answers at most 100 notifications, the
newest, though more were delivered. ``ids`` of ``null`` marks every
notification read; ``ids`` of ``[]`` names none and marks nothing, and
asks igni server nothing. The calls publish the push that follows them
and the cause that tells which call moved the counts.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from igni_core.backend.rpc_binding import Correlation, RpcBinding
from igni_core.backend.rpc_handlers.base import RpcHandlerContext
from igni_core.backend.rpc_handlers.notifications import MarkReadArgs, NotificationsRpcHandler
from igni_core.notifications.center import NotificationCenter
from igni_core.notifications.local_store import LocalNotificationStore
from igni_core.notifications.schemas import (
    ExtensionNotice,
    NotificationCounts,
    NotificationsListResult,
    NotificationsReadResult,
    NotificationTarget,
)
from igni_core.protocol.rpc import RpcDuration, RpcMethod
from igni_core.protocol.schemas.enums import PushChannel
from tests.notification_helpers import FakeNotificationServer, server_row, signed_in


def _handler(center: NotificationCenter | None) -> NotificationsRpcHandler:
    return NotificationsRpcHandler(
        RpcHandlerContext(
            backend=None,  # type: ignore[arg-type]
            file_completion=MagicMock(),
            shell_runner=MagicMock(),
            login=MagicMock(),
            push=MagicMock(),
            workflows=MagicMock(),
            notifications=center,
        )
    )


def _center(
    tmp_path, rows: list[dict[str, Any]]
) -> tuple[NotificationCenter, FakeNotificationServer]:
    server = FakeNotificationServer(rows)
    center = NotificationCenter(
        server=signed_in(),
        local=LocalNotificationStore(tmp_path / "notifications.db"),
        announce=lambda change: None,
        transport=server.transport,
    )
    return center, server


def _notice(key: str) -> ExtensionNotice:
    return ExtensionNotice(
        kind="export_ready",
        title="Runbook.pdf",
        target=NotificationTarget(page="acme.scan", params={}),
        event_key=key,
    )


async def test_without_a_center_every_call_answers_empty() -> None:
    methods = _handler(None).methods()

    assert await methods[RpcMethod.NOTIFICATIONS_LIST]({}) == NotificationsListResult()
    assert methods[RpcMethod.NOTIFICATIONS_COUNTS]({}) == NotificationCounts()
    assert await methods[RpcMethod.NOTIFICATIONS_MARK_READ]({}) == NotificationsReadResult()


async def test_a_list_answers_the_hundred_newest_of_many(tmp_path) -> None:
    rows = [server_row(f"s-{n:03d}", minutes_ago=n) for n in range(150)]
    center, _ = _center(tmp_path, rows)

    result = await _handler(center).methods()[RpcMethod.NOTIFICATIONS_LIST]({})

    assert len(result.notifications) == NotificationCenter.LIMIT == 100
    assert result.notifications[0].id == "s-000"
    assert result.notifications[-1].id == "s-099"
    assert result.counts.unread == 150


async def test_counts_come_from_what_was_last_read_without_asking(tmp_path) -> None:
    center, server = _center(tmp_path, [server_row("s-1")])
    handler = _handler(center)
    await handler.methods()[RpcMethod.NOTIFICATIONS_LIST]({})
    server.status = 500

    counts = handler.methods()[RpcMethod.NOTIFICATIONS_COUNTS]({})

    assert counts == NotificationCounts(unread=1, by_source={"igni.artifacts": 1})


async def test_ids_of_null_mark_everything_read(tmp_path) -> None:
    center, server = _center(tmp_path, [server_row("s-1"), server_row("s-2")])
    center.keep_local("acme.scanner", _notice("k1"))

    result = await _handler(center).methods()[RpcMethod.NOTIFICATIONS_MARK_READ]({"ids": None})

    assert server.read_bodies == [{}]
    assert result.counts.unread == 0


async def test_an_empty_list_of_ids_marks_nothing_and_asks_nobody(tmp_path) -> None:
    center, server = _center(tmp_path, [server_row("s-1")])
    kept = center.keep_local("acme.scanner", _notice("k1"))
    assert kept is not None

    result = await _handler(center).methods()[RpcMethod.NOTIFICATIONS_MARK_READ]({"ids": []})

    assert server.read_bodies == []
    assert result.counts.unread == 2


async def test_an_id_nobody_holds_marks_nothing_here(tmp_path) -> None:
    center, _ = _center(tmp_path, [server_row("s-1")])
    center.keep_local("acme.scanner", _notice("k1"))

    result = await _handler(center).methods()[RpcMethod.NOTIFICATIONS_MARK_READ]({"ids": ["nope"]})

    assert result.counts.unread == 2


def test_keys_a_newer_client_adds_are_ignored() -> None:
    args = MarkReadArgs.model_validate({"ids": ["a"], "source": "x", "later": 1})

    assert (args.ids, args.source) == (["a"], "x")
    assert MarkReadArgs.model_validate({}).ids is None


def test_each_call_publishes_the_push_it_is_followed_by_and_its_cause() -> None:
    bindings = {
        m: RpcBinding.on(NotificationsRpcHandler)[m][1]
        for m in (
            RpcMethod.NOTIFICATIONS_LIST,
            RpcMethod.NOTIFICATIONS_COUNTS,
            RpcMethod.NOTIFICATIONS_MARK_READ,
        )
    }

    listed = bindings[RpcMethod.NOTIFICATIONS_LIST]
    marked = bindings[RpcMethod.NOTIFICATIONS_MARK_READ]
    counted = bindings[RpcMethod.NOTIFICATIONS_COUNTS]
    assert listed.streams == marked.streams == (PushChannel.NOTIFICATIONS_CHANGED,)
    assert listed.correlate == Correlation(by="payload.cause", equals="listed")
    assert marked.correlate == Correlation(by="payload.cause", equals="marked_read")
    assert counted.streams == () and counted.correlate is None
    assert listed.duration is marked.duration is RpcDuration.NETWORK
    assert counted.duration is RpcDuration.INSTANT
