"""The center tells the client what moved the counts, and keeps its two halves straight.

``announce`` is called with the merged counts and the cause: ``server``
(a read on its own), ``listed``, ``marked_read``, ``extension`` or
``signed_out``. The rest are the edges of the merged list and of the
lifecycle: ordering of odd timestamps, the cap, a stop during a read, a
restored VM, a mark that only half works.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from igni_core.extensions.service_token import IgniSession
from igni_core.notifications.center import NotificationCenter
from igni_core.notifications.local_store import LocalNotificationStore
from igni_core.notifications.schemas import (
    ExtensionNotice,
    MarkReadRequest,
    NotificationCounts,
    NotificationsChanged,
    NotificationTarget,
)
from igni_core.notifications.server_client import SignedInServer
from igni_core.notifications.stream import Backoff
from tests.notification_helpers import (
    API_URL,
    FakeNotificationServer,
    RecordingSleep,
    server_row,
    settle,
)


def notice(event_key: str = "export:e1") -> ExtensionNotice:
    return ExtensionNotice(
        kind="export_ready",
        title="Runbook.pdf",
        body="ready",
        target=NotificationTarget(page="acme.scan"),
        event_key=event_key,
    )


class Floor:
    def __init__(self, tmp_path, rows: list[dict[str, Any]] | None = None) -> None:
        self.server = FakeNotificationServer(rows)
        self.session: str | None = "s"
        self.sleep = RecordingSleep()
        self.changes: list[NotificationsChanged] = []
        self.store = LocalNotificationStore(tmp_path / "notifications.db")
        self.center = NotificationCenter(
            server=SignedInServer(
                api_url=lambda: API_URL,
                session=lambda: None if self.session is None else IgniSession(access_token="s"),
            ),
            local=self.store,
            announce=self.changes.append,
            transport=self.server.transport,
            backoff=Backoff(base=1.0, ceiling=8.0, rng=lambda: 0.5),
            sleep=self.sleep,
        )

    @property
    def causes(self) -> list[str]:
        return [change.cause for change in self.changes]


# ── What moved the counts ───────────────────────────────────────────


async def test_each_way_the_counts_move_is_announced_with_its_cause(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])

    await floor.center.refresh()
    await floor.center.listing()
    floor.center.keep_local("acme.scanner", notice())
    await floor.center.mark_read(MarkReadRequest(ids=["s1"]))
    await floor.center.signed_out()

    assert floor.causes == ["server", "listed", "extension", "marked_read", "signed_out"]
    assert floor.changes[0].counts == NotificationCounts(unread=1, by_source={"igni.artifacts": 1})
    assert floor.changes[2].counts == NotificationCounts(
        unread=2, by_source={"igni.artifacts": 1, "acme.scanner": 1}
    )
    assert floor.changes[3].counts == NotificationCounts(unread=1, by_source={"acme.scanner": 1})


async def test_a_wake_reads_the_list_on_its_own_cause(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])

    floor.center.wake()
    await settle()

    assert floor.causes == ["server"]


async def test_signing_out_forgets_the_servers_half_but_not_this_machines(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])
    floor.center.keep_local("acme.scanner", notice())
    await floor.center.refresh()
    assert floor.center.counts().unread == 2

    # The session is still valid here: only signed_out may clear the rows.
    await floor.center.signed_out()

    assert floor.changes[-1].cause == "signed_out"
    assert floor.changes[-1].counts == NotificationCounts(unread=1, by_source={"acme.scanner": 1})
    assert floor.center.counts() == floor.changes[-1].counts


async def test_signing_out_forgets_the_organisations(tmp_path):
    floor = Floor(tmp_path, [server_row("s1", org_id="org-acme")])
    floor.server.organizations = [{"id": "org-acme", "display_name": "Acme"}]
    await floor.center.listing()
    assert floor.server.calls("/v1/portal/me") == 1

    await floor.center.signed_out()
    await floor.center.listing()

    # Read again for the next person, not served from the last one.
    assert floor.server.calls("/v1/portal/me") == 2


async def test_a_repeated_notice_announces_nothing(tmp_path):
    floor = Floor(tmp_path)

    first = floor.center.keep_local("acme.scanner", notice())
    again = floor.center.keep_local("acme.scanner", notice())

    assert first is not None
    assert again is None
    assert floor.causes == ["extension"]


# ── The merged list ─────────────────────────────────────────────────


async def test_rows_without_a_time_or_without_an_offset_sort_without_raising(tmp_path):
    floor = Floor(
        tmp_path,
        [
            server_row("none", created_at=None),
            server_row("naive", created_at="2020-01-01T00:00:00"),
            server_row("aware", minutes_ago=5),
        ],
    )
    floor.center.keep_local("acme.scanner", notice())

    listed = await floor.center.listing()

    assert [n.id for n in listed.notifications[1:]] == [
        "aware",
        "naive",
        "none",
    ]
    assert listed.notifications[0].id.startswith("local-")


async def test_a_list_is_capped_across_both_halves(tmp_path):
    floor = Floor(tmp_path, [server_row(f"s{n}", minutes_ago=n + 1) for n in range(60)])
    for n in range(60):
        floor.center.keep_local("acme.scanner", notice(f"e{n}"))

    listed = await floor.center.listing()

    assert len(listed.notifications) == NotificationCenter.LIMIT == 100
    assert listed.counts.unread == 120


# ── Marking when half of it fails ───────────────────────────────────


async def test_a_mark_the_server_refuses_still_marks_here_and_says_so(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])
    local = floor.center.keep_local("acme.scanner", notice())
    assert local is not None
    await floor.center.refresh()
    floor.server.status = 500
    floor.changes.clear()

    result = await floor.center.mark_read(MarkReadRequest(ids=[local.id, "s1"]))

    assert "refused" in result.server_error
    assert result.counts == NotificationCounts(unread=1, by_source={"igni.artifacts": 1})
    assert floor.causes == ["marked_read"]
    assert floor.changes[0].counts == result.counts


async def test_no_ids_marks_nothing_and_asks_nobody(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])
    floor.center.keep_local("acme.scanner", notice())
    await floor.center.refresh()

    result = await floor.center.mark_read(MarkReadRequest(ids=[]))

    assert floor.server.calls("/v1/notifications/read") == 0
    assert result.counts.unread == 2
    assert result.server_error == ""


async def test_an_id_that_looks_local_but_nobody_holds_is_never_sent_to_the_server(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])

    await floor.center.mark_read(MarkReadRequest(ids=["local-nobody"]))

    assert floor.server.calls("/v1/notifications/read") == 0


# ── The lifecycle ───────────────────────────────────────────────────


async def test_a_restored_vm_reads_again_on_a_fresh_connection(tmp_path):
    floor = Floor(tmp_path, [server_row("s1")])
    floor.center.start()
    await floor.server.wait_connected()
    await settle()
    assert floor.server.calls("/v1/notifications") == 1
    assert len(floor.server.stream_cursors) == 1

    await floor.center.resumed()
    floor.sleep.release(5)
    await floor.server.wait_connections(2)
    await settle()

    assert floor.server.calls("/v1/notifications") >= 2
    assert len(floor.server.stream_cursors) >= 2
    await floor.center.stop()


async def test_stopping_during_a_read_cancels_it(tmp_path):
    started = asyncio.Event()

    floor = Floor(tmp_path)

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("never answered")

    floor.center._client._transport = httpx.MockTransport(hang)  # type: ignore[attr-defined]

    floor.center.wake()
    await asyncio.wait_for(started.wait(), 2)
    stopping = asyncio.create_task(floor.center.stop())
    done, _ = await asyncio.wait({stopping}, timeout=2)
    if not done:
        stopping.cancel()

    assert done, "stop waited for a read that nothing cancelled"
    assert floor.changes == []
